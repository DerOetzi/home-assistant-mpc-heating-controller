from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import ceil

from ...const import CapacityOutcome, TrvSetpointMode
from .hot_water import HotWaterChargeDetector
from .learner import LearnerPrediction, RoomMpcModelLearner
from .math_helper import clamp
from .models.capacity import ThermalCapacityModel
from .models.emitter import HeatEmitterModel, below_activation
from .models.gains import RoomGainModel, SunSource
from .models.loss import RoomLossModel
from .models.valve import ValveCharacteristic
from .observer import ObserverEstimate, RoomStateObserver, step_radiator_power_w
from .radiator_monitor import RadiatorDeliveryMonitor
from .results import (
    RoomMpcError,
    RoomMpcInput,
    RoomMpcResult,
    RoomTemperatureResult,
)
from .sensors import RoomMpcSensors
from .types import (
    FlowGateState,
    LearningFactors,
    RoomModelLearningState,
    RoomThermalConfig,
    TrvConfig,
    UaBalanceInfo,
)
from .valve_curve import ValveCurveBin, ValveCurveLearner, ValveCurveStatus

MPC_PREDICTION_HORIZON_S = 1800
# How quickly the minimum flow temperature asks to bring a room back to its
# setpoint. Much longer than the demand horizon: a heat pump recovers more
# efficiently at a lower flow over a longer time, and the heat source serves
# the maximum over all rooms, so one slightly cool room must not drive the
# whole house to a high flow temperature.
FLOW_RECOVERY_HORIZON_S = 6 * 3600
# At the hold flow a room only approaches its setpoint asymptotically. Without
# a tolerance a deficit of a few hundredths of a kelvin already demands a flow
# well above the hold flow.
FLOW_RECOVERY_TOLERANCE_C = 0.1
FLOW_RECOVERY_SEARCH_PRECISION_C = 0.1
MPC_SIMULATION_STEP_S = 150
MPC_DEMAND_SEARCH_STEP_PCT = 5
# Predictions this close count as equally good, so the hold demand decides.
MPC_TIE_DECIMALS = 6
MPC_SIMULATION_CONVERGENCE_DELTA_C = 0.001
MPC_SIMULATION_CONVERGENCE_POWER_W = 1.0
# max_demand_step_pct is the step allowed per this interval. compute() also runs
# on every tracked state change, so a step per call let demand jump 85 -> 5 %
# within seconds; the allowed step scales with the time since the last call.
# Fixed, not the poll interval: the option is labelled "per 5 minutes".
DEMAND_STEP_REFERENCE_S = 5 * 60
# After setup the demand ramp starts from the openings the TRVs report, if
# one arrives within this window (they report seconds after a restart).
DEMAND_SEED_WINDOW_S = 10 * 60


@dataclass
class MpcRateLimitConfig:
    demand_hysteresis_pct: float = 5.0
    hold_time_s: float = 300.0
    hold_override_demand_pct: float = 40.0
    max_demand_step_pct: float = 20.0


@dataclass
class FlowGateConfig:
    close_surplus_c: float = 0.5
    open_surplus_c: float = 0.2
    hold_time_s: float = 900.0


class _SurplusFlowGate:
    """Suppresses the hold-flow requirement while the room is coasting.

    The hold-flow branch answers "what flow holds the setpoint", computed from
    the *target* temperature, so it stays positive all through the shoulder
    season even for a room sitting well above setpoint on stored heat. That is
    the right steady-state floor at setpoint -- without it the source would
    drop its flow the moment demand hits zero and the room would oscillate --
    but it is a phantom requirement while a real surplus exists.

    So the gate closes only on both signals together: the optimizer wants no
    heat over its horizon (demand zero), and the room is above target by more
    than a deadband. The deadband is what keeps the gate off the steady-state
    case, where demand is also zero but the floor must stay.

    Hysteresis is asymmetric and time-guarded in one direction only: closing
    withdraws a heat requirement and can gate a heat-source start, so it must
    be deliberate; reopening restores one and happens immediately.
    """

    def __init__(self, config: FlowGateConfig) -> None:
        self._config = config
        self._closed = False
        self._surplus_since_ts: float | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def restore(self, closed: bool) -> None:
        self._closed = closed
        self._surplus_since_ts = None

    def update(self, surplus_c: float, demand_pct: float, now_ts: float) -> bool:
        if self._closed:
            if demand_pct > 0 or surplus_c < self._config.open_surplus_c:
                self._closed = False
                self._surplus_since_ts = None
            return self._closed

        if demand_pct > 0 or surplus_c <= self._config.close_surplus_c:
            self._surplus_since_ts = None
            return self._closed

        if self._surplus_since_ts is None:
            self._surplus_since_ts = now_ts

        if now_ts - self._surplus_since_ts >= self._config.hold_time_s:
            self._closed = True

        return self._closed


@dataclass
class _DemandPrediction:
    demand_pct: float
    predicted_temperature_c: float
    prediction_error: float


@dataclass
class _RecoveryFlow:
    flow_temperature_c: float
    saturated: bool = False


@dataclass
class RoomMpcComputeResult:
    valid: bool
    result: RoomMpcResult | None = None
    error: RoomMpcError | None = None


class RoomMpcController:
    def __init__(
        self,
        thermal_config: RoomThermalConfig,
        trvs: list[TrvConfig],
        rate_limit_config: MpcRateLimitConfig,
        max_sensor_age_s: float,
        room_sensor_configured: bool,
        target_band_c: float,
        flow_gate_config: FlowGateConfig | None = None,
        trv_setpoint_mode: TrvSetpointMode = TrvSetpointMode.ABSOLUTE,
    ) -> None:
        self._rate_limit_config = rate_limit_config
        self._trv_setpoint_mode = trv_setpoint_mode
        self._current_trv_targets: list[float | None] = [None] * len(trvs)
        self._target_band_c = max(0.0, target_band_c)

        gate_config = flow_gate_config or FlowGateConfig()
        self._live_flow_gate = _SurplusFlowGate(gate_config)
        self._preview_flow_gate = _SurplusFlowGate(gate_config)

        self._sensors = RoomMpcSensors(trvs, max_sensor_age_s, room_sensor_configured)
        self._emitter_model = HeatEmitterModel(thermal_config, trvs)
        self._loss_model = RoomLossModel(thermal_config)
        self._capacity_model = ThermalCapacityModel(thermal_config)
        self._gain_model = RoomGainModel(
            thermal_config.room_heat_load_w,
            self._loss_model.base_ua_w_per_k,
            window_surfaces=thermal_config.window_surfaces,
            opaque_surfaces=thermal_config.opaque_surfaces,
        )
        self._learner = RoomMpcModelLearner(
            self._loss_model,
            self._capacity_model,
            target_band_c=self._target_band_c,
            gain_model=self._gain_model,
        )
        self._observer = RoomStateObserver(
            self._emitter_model, self._loss_model, self._capacity_model
        )
        self._heat_available = True
        self._hot_water = HotWaterChargeDetector()
        self._radiator_monitors = [RadiatorDeliveryMonitor() for _ in trvs]
        self._trv_names = [trv.name for trv in trvs]
        # By TRV name; only a TRV that reports its valve opening can learn
        # its curve.
        self._valve_curve_learners: dict[str, ValveCurveLearner] = {
            trv.name: ValveCurveLearner(
                trv.valve_characteristic_seed,
                min_target_c=trv.min_target_temperature_c,
            )
            for trv in trvs
            if trv.valve_position_configured
        }
        # The setpoints the TRVs report, as reported. Unlike in
        # _current_trv_targets a just-written target does not stand in for
        # the echo: the curve needs what the TRV really regulates to.
        self._reported_trv_targets: list[float | None] = [None] * len(trvs)

        self._target_demand_pct = 0.0
        self._last_output_demand_pct = 0.0
        self._last_demand_update_ts = 0.0
        self._last_rate_limit_ts: float | None = None
        # Until when the ramp may still start from the reported valve
        # openings; None before the first smoothed computation.
        self._demand_seed_until_ts: float | None = None
        self._outdoor_temperature_missing = False

    def destroy(self) -> None:
        self._learner.destroy()

    @property
    def learned_ua_factor(self) -> float:
        return self._loss_model.learned_ua_factor

    @property
    def gain_model(self) -> RoomGainModel:
        return self._gain_model

    @property
    def learned_capacity_factor(self) -> float:
        return self._capacity_model.learned_capacity_factor

    @property
    def flow_gate_state(self) -> FlowGateState:
        return FlowGateState(
            live_closed=self._live_flow_gate.closed,
            preview_closed=self._preview_flow_gate.closed,
        )

    def restore_flow_gate_state(self, state: FlowGateState) -> None:
        self._live_flow_gate.restore(state.live_closed)
        self._preview_flow_gate.restore(state.preview_closed)

    @property
    def outdoor_temperature_missing(self) -> bool:
        """The last applied calculation found no fresh outdoor temperature."""
        return self._outdoor_temperature_missing

    @property
    def radiators_not_heating(self) -> list[bool]:
        """Per TRV: the radiator gives off no heat although its valve is open."""
        return [monitor.not_heating for monitor in self._radiator_monitors]

    def set_heat_available(self, available: bool) -> None:
        """Whether the TRVs get hot water; without it the radiators cool down."""
        self._heat_available = available

    def get_room_temperature_result(self) -> RoomTemperatureResult:
        return self._sensors.get_room_temperature()

    def get_learning_state(self) -> RoomModelLearningState:
        return self._learner.get_learning_state()

    def get_ua_balance_info(self) -> UaBalanceInfo:
        return self._learner.get_ua_balance_info()

    def get_capacity_outcome(self) -> CapacityOutcome:
        return self._learner.get_capacity_outcome()

    @property
    def effective_capacity_j_per_k(self) -> float:
        return self._capacity_model.effective_capacity_j_per_k

    def set_trv_temperature(self, index: int, value: float | None) -> None:
        self._sensors.set_trv_temperature(index, value)

    def set_room_sensor_temperature(self, value: float | None) -> None:
        self._sensors.set_room_sensor_temperature(value)

    def set_current_trv_targets(self, targets: list[float | None]) -> None:
        """The targets the TRVs report now, so a relative target can stay."""
        self._current_trv_targets = list(targets)

    def set_valve_position(self, index: int, value: float | None) -> None:
        self._sensors.set_valve_position(index, value)

    def set_trv_target(self, index: int, value: float | None) -> None:
        """The setpoint a TRV reports, for its valve curve."""
        if index < len(self._reported_trv_targets):
            self._reported_trv_targets[index] = value

    @property
    def valve_curve_learners(self) -> dict[str, ValveCurveLearner]:
        """By TRV name, only TRVs with a valve entity."""
        return self._valve_curve_learners

    def valve_characteristics(self) -> list[ValveCharacteristic | None]:
        """Per TRV, in order: its curve; None without valve entity or curve."""
        return [
            learner.characteristic()
            if (learner := self._valve_curve_learners.get(name)) is not None
            else None
            for name in self._trv_names
        ]

    def valve_curve_report(
        self,
    ) -> dict[str, tuple[ValveCurveStatus, list[ValveCurveBin]]]:
        """Status and bins per TRV with a valve entity, for diagnosis."""
        return {
            name: (learner.status, learner.bins())
            for name, learner in self._valve_curve_learners.items()
        }

    def consume_valve_curves_changed(self) -> bool:
        """Whether any TRV finished a sample since the last call."""
        # Collected first: any() would stop early and leave flags unconsumed.
        changed = [
            learner.consume_changed() for learner in self._valve_curve_learners.values()
        ]
        return any(changed)

    def export_valve_curves(self) -> dict[str, dict]:
        """Finished samples per TRV name, to persist."""
        return {
            name: learner.export()
            for name, learner in self._valve_curve_learners.items()
        }

    def restore_valve_curves(self, data: Mapping[str, dict]) -> None:
        for name, learner in self._valve_curve_learners.items():
            if isinstance(data.get(name), dict):
                learner.restore(data[name])

    def set_trv_adapting(self, adapting: bool) -> None:
        self._sensors.set_trv_adapting(adapting)

    def set_outdoor_temperature(self, value: float | None) -> None:
        self._sensors.set_outdoor_temperature(value)

    def set_flow_temperature(self, value: float | None) -> None:
        self._sensors.set_flow_temperature(value)

    def set_flow_threshold_c(self, flow_threshold_c: float) -> None:
        self._emitter_model.set_flow_threshold_c(flow_threshold_c)

    def compute(
        self,
        target_temperature_c: float,
        apply_side_effects: bool = True,
        skip_demand_smoothing: bool = False,
    ) -> RoomMpcComputeResult:
        """Compute demand, TRV targets and flow requirements.

        skip_demand_smoothing applies the optimal demand at once, for forced
        frost protection: an open window must not keep the valve open for
        the hold time while the demand steps down.
        """
        now_ts = time.time()
        if apply_side_effects:
            # Learning pauses while the outdoor temperature is missing, also
            # when the room temperature is missing as well.
            self._outdoor_temperature_missing = (
                self._sensors.is_outdoor_temperature_missing(now_ts)
            )
            self._learner.set_outdoor_temperature_missing(
                self._outdoor_temperature_missing, now_ts
            )
        input_result = self._sensors.create_input(target_temperature_c, now_ts)
        if not input_result.valid:
            return RoomMpcComputeResult(valid=False, error=input_result.error)

        mpc_input = input_result.input
        if apply_side_effects:
            self._hot_water.update(
                mpc_input.now_ts, mpc_input.flow_temp_c, self._heat_available
            )
        if self._hot_water.charging:
            # The radiators get no water; the valves keep the demand of the
            # heating flow they get back once the charge is done.
            mpc_input.hot_water_charge = True
            if self._hot_water.heating_flow_c is not None:
                mpc_input.flow_temp_c = self._hot_water.heating_flow_c
        if apply_side_effects:
            for monitor, opening_pct, trv_temperature_c, output_fraction in zip(
                self._radiator_monitors,
                mpc_input.valve_positions_pct or [None] * len(self._radiator_monitors),
                mpc_input.trv_temperatures,
                self._emitter_model.full_output_fractions(
                    mpc_input.flow_temp_c, mpc_input.room_temp_c
                ),
            ):
                monitor.update(
                    mpc_input.now_ts,
                    opening_pct,
                    trv_temperature_c,
                    mpc_input.room_temp_c,
                    self._heat_available,
                    output_fraction,
                    hot_water_charge=mpc_input.hot_water_charge,
                )
            self._observe_valve_curves(mpc_input, skip_demand_smoothing)
        mpc_input.radiator_not_heating = any(self.radiators_not_heating)
        mpc_input.radiator_not_heating_since_ts = min(
            (
                monitor.not_heating_since_ts
                for monitor in self._radiator_monitors
                if monitor.not_heating_since_ts is not None
            ),
            default=None,
        )
        estimate = self._observer.estimate(mpc_input)

        available_heating_power_w = max(
            0.0,
            self._emitter_model.calculate_available_heating_power_w(
                mpc_input.room_temp_c, mpc_input.flow_temp_c
            ),
        )

        # Below this a demand writes the TRVs their minimum target (see the
        # emitter): neither the search nor the smoothing may settle there.
        activation_pct = self._emitter_model.activation_threshold_pct(
            self._current_trv_targets
        )
        optimal_demand = self._find_optimal_demand_prediction(
            mpc_input, estimate, activation_pct
        )
        if apply_side_effects and skip_demand_smoothing:
            stabilized_demand_pct = self._reset_demand_rate_limiting(
                mpc_input, optimal_demand.demand_pct
            )
        elif apply_side_effects:
            stabilized_demand_pct = self._apply_demand_rate_limiting(
                mpc_input, optimal_demand.demand_pct, activation_pct
            )
        else:
            stabilized_demand_pct = optimal_demand.demand_pct

        # The demand is the target valve opening: what the radiators give
        # off there, on the same curve the observer uses for a reported one.
        requested_heating_power_w = self._emitter_model.planned_heating_power_w(
            mpc_input.room_temp_c, mpc_input.flow_temp_c, stabilized_demand_pct
        )

        # The TRVs are thermostats: a small demand can open a valve fully. Where
        # they report their opening, that is what the radiators get.
        measured_heating_power_w = (
            self._emitter_model.calculate_measured_heating_power_w(
                mpc_input.room_temp_c,
                mpc_input.flow_temp_c,
                mpc_input.valve_positions_pct,
                stabilized_demand_pct,
            )
        )
        reported_openings_pct = [
            opening for opening in mpc_input.valve_positions_pct if opening is not None
        ]
        effective_opening_pct = (
            sum(reported_openings_pct) / len(reported_openings_pct)
            if reported_openings_pct
            else stabilized_demand_pct
        )

        flow_gate = (
            self._live_flow_gate if apply_side_effects else self._preview_flow_gate
        )
        flow_gate_closed = flow_gate.update(
            surplus_c=mpc_input.room_temp_c - mpc_input.target_temp_c,
            demand_pct=stabilized_demand_pct,
            now_ts=mpc_input.now_ts,
        )

        hold_flow_temperature_c = self._calculate_hold_flow_temperature_c(mpc_input)
        recovery_flow = self._calculate_recovery_flow(mpc_input, estimate)
        gated_hold_flow_c = (
            0.0 if flow_gate_closed else (hold_flow_temperature_c or 0.0)
        )
        recommended_flow_temperature_c = max(
            gated_hold_flow_c, recovery_flow.flow_temperature_c
        )

        if apply_side_effects:
            water_flowing = self._heat_available and not mpc_input.hot_water_charge
            self._observer.commit(
                estimate,
                mpc_input,
                target_power_w=measured_heating_power_w if water_flowing else 0.0,
                demand_pct=effective_opening_pct if water_flowing else 0.0,
            )
            self._record_learning_telemetry(
                mpc_input,
                requested_heating_power_w,
                optimal_demand.predicted_temperature_c,
                estimate,
            )

        result = RoomMpcResult(
            trv_targets=self._emitter_model.calculate_target_temperatures(
                stabilized_demand_pct,
                self._trv_setpoint_mode,
                mpc_input.trv_temperatures,
                self._current_trv_targets,
                # Fetched on every call: the curves keep learning.
                self.valve_characteristics(),
            ),
            input=mpc_input,
            demand_pct=stabilized_demand_pct,
            requested_heating_power_w=requested_heating_power_w,
            available_heating_power_w=available_heating_power_w,
            recommended_flow_temperature_c=recommended_flow_temperature_c,
            hold_flow_temperature_c=hold_flow_temperature_c,
            gated_hold_flow_temperature_c=gated_hold_flow_c,
            recovery_flow_temperature_c=recovery_flow.flow_temperature_c,
            recovery_flow_saturated=recovery_flow.saturated,
            flow_gate_closed=flow_gate_closed,
            estimated_room_temperature_c=estimate.room_temperature_c,
            radiator_power_w=estimate.radiator_power_w,
            measured_heating_power_w=measured_heating_power_w,
        )
        return RoomMpcComputeResult(valid=True, result=result)

    def _observe_valve_curves(
        self, mpc_input: RoomMpcInput, frost_protection_forced: bool
    ) -> None:
        """Feed each TRV's valve curve with what it reports now.

        A sample only counts while the valve answers the setpoint alone: an
        adapting TRV moves it by itself, without water (hot-water charge) or
        with an open window the PI winds up differently, a radiator that
        does not heat leaves the TRV reading behind, and forced frost
        protection shuts the valves whatever the curve says.
        """
        room_allowed = not (
            mpc_input.trv_adapting
            or mpc_input.hot_water_charge
            or self._learner.window_open
            or frost_protection_forced
        )
        for name, monitor, target_c, reading_c, opening_pct in zip(
            self._trv_names,
            self._radiator_monitors,
            self._reported_trv_targets,
            mpc_input.trv_temperatures,
            mpc_input.valve_positions_pct or [None] * len(self._trv_names),
        ):
            learner = self._valve_curve_learners.get(name)
            if learner is None:
                continue
            learner.observe(
                mpc_input.now_ts,
                target_c,
                reading_c,
                opening_pct,
                sample_allowed=room_allowed
                and not monitor.not_heating
                and opening_pct is not None,
            )

    def _calculate_hold_flow_temperature_c(
        self, mpc_input: RoomMpcInput
    ) -> float | None:
        """Flow needed to hold the setpoint -- ungated, weather-driven only."""
        hold_power_w = max(
            0.0,
            self._loss_model.calculate_heat_loss_w(
                mpc_input.target_temp_c, mpc_input.outdoor_temp_c
            ),
        )
        return self._emitter_model.calculate_recommended_flow_temperature_c(
            hold_power_w, mpc_input.target_temp_c
        )

    def _calculate_recovery_flow(
        self, mpc_input: RoomMpcInput, estimate: ObserverEstimate
    ) -> _RecoveryFlow:
        """Lowest flow that brings the room to its setpoint within the horizon.

        Simulated with the valves fully open and independent of the measured flow
        temperature, so a stale reading or a DHW charge on a shared leaving-water
        sensor cannot drive the requirement.
        """
        deficit_c = mpc_input.target_temp_c - estimate.room_temperature_c
        hold_power_w = self._loss_model.calculate_heat_loss_w(
            mpc_input.target_temp_c, mpc_input.outdoor_temp_c
        )
        if deficit_c <= FLOW_RECOVERY_TOLERANCE_C or hold_power_w <= 0:
            return _RecoveryFlow(0.0)

        def reaches_target(flow_temperature_c: float) -> bool:
            return (
                self._simulate_room_temperature_c(
                    mpc_input,
                    estimate,
                    demand_pct=100,
                    flow_temperature_c=flow_temperature_c,
                    duration_s=FLOW_RECOVERY_HORIZON_S,
                )
                >= mpc_input.target_temp_c - FLOW_RECOVERY_TOLERANCE_C
            )

        low = self._emitter_model.flow_search_min_c
        high = self._emitter_model.flow_search_max_c
        if not reaches_target(high):
            return _RecoveryFlow(high, saturated=True)

        while high - low > FLOW_RECOVERY_SEARCH_PRECISION_C:
            mid = (low + high) / 2
            if reaches_target(mid):
                high = mid
            else:
                low = mid

        return _RecoveryFlow(
            min(
                self._emitter_model.round_up_flow_temperature_c(high),
                self._emitter_model.flow_search_max_c,
            )
        )

    def _find_optimal_demand_prediction(
        self,
        mpc_input: RoomMpcInput,
        estimate: ObserverEstimate,
        activation_pct: float,
    ) -> _DemandPrediction:
        """Demand whose prediction lands closest to the target band.

        Any prediction between setpoint minus the band and the setpoint is on
        target. A heavy room with little radiator reserve cannot climb the
        last 0.1 K within the horizon at any demand; aiming at the exact
        setpoint then opened the valve fully for hours over one sensor step.
        Among demands on target the one nearest the hold demand wins, so the
        valve neither shuts at the setpoint (the room only drifts within the
        band over the horizon) nor opens fully just below it.
        """
        best: _DemandPrediction | None = None
        best_key: tuple[float, float] | None = None
        hold_demand_pct = self._hold_demand_pct(mpc_input)

        for demand_pct in range(0, 101, MPC_DEMAND_SEARCH_STEP_PCT):
            if below_activation(demand_pct, activation_pct):
                # The TRVs would get their minimum target, not this opening:
                # a small hold opening otherwise planned heat a shut valve
                # never got, and the room cycled shut-open-overshoot.
                continue
            predicted_temperature_c = self._predict_room_temperature_c(
                mpc_input, estimate, demand_pct, MPC_PREDICTION_HORIZON_S
            )
            prediction_error = self._target_band_error_c(
                mpc_input.target_temp_c, predicted_temperature_c
            )
            rounded_error = round(prediction_error, MPC_TIE_DECIMALS)
            # Outside the band a tie (radiators that deliver nothing at this
            # flow) keeps the lowest demand.
            key = (
                rounded_error,
                abs(demand_pct - hold_demand_pct) if rounded_error == 0 else demand_pct,
            )

            if best_key is None or key < best_key:
                best_key = key
                best = _DemandPrediction(
                    demand_pct=demand_pct,
                    predicted_temperature_c=predicted_temperature_c,
                    prediction_error=prediction_error,
                )

        return best

    def _target_band_error_c(
        self, target_temperature_c: float, predicted_temperature_c: float
    ) -> float:
        if predicted_temperature_c > target_temperature_c:
            return predicted_temperature_c - target_temperature_c
        lower_c = target_temperature_c - self._target_band_c
        if predicted_temperature_c < lower_c:
            return lower_c - predicted_temperature_c
        return 0.0

    def _hold_demand_pct(self, mpc_input: RoomMpcInput) -> float:
        """Valve opening that holds the setpoint in steady state."""
        hold_power_w = max(
            0.0,
            self._loss_model.calculate_heat_loss_w(
                mpc_input.target_temp_c, mpc_input.outdoor_temp_c
            ),
        )
        return self._emitter_model.opening_for_power_pct(
            mpc_input.target_temp_c, mpc_input.flow_temp_c, hold_power_w
        )

    def _predict_room_temperature_c(
        self,
        mpc_input: RoomMpcInput,
        estimate: ObserverEstimate,
        demand_pct: float,
        duration_s: float,
    ) -> float:
        return self._simulate_room_temperature_c(
            mpc_input,
            estimate,
            demand_pct=demand_pct,
            flow_temperature_c=mpc_input.flow_temp_c,
            duration_s=duration_s,
        )

    def _simulate_room_temperature_c(
        self,
        mpc_input: RoomMpcInput,
        estimate: ObserverEstimate,
        demand_pct: float,
        flow_temperature_c: float | None,
        duration_s: float,
    ) -> float:
        """Room temperature after holding a demand, from the observed state.

        The radiators start from the output the observer estimates and follow
        the requested power with their own lag, so heat already in them (or
        not yet in them) counts.
        """
        simulated_room_temperature_c = estimate.room_temperature_c
        radiator_power_w = estimate.radiator_power_w
        step_count = max(1, ceil(duration_s / MPC_SIMULATION_STEP_S))

        for _ in range(step_count):
            target_power_w = self._emitter_model.planned_heating_power_w(
                simulated_room_temperature_c, flow_temperature_c, demand_pct
            )
            time_constant_s = self._emitter_model.time_constant_s(
                simulated_room_temperature_c,
                flow_temperature_c,
                demand_pct,
                heating_up=target_power_w > radiator_power_w,
            )
            next_radiator_power_w = step_radiator_power_w(
                radiator_power_w, target_power_w, time_constant_s, MPC_SIMULATION_STEP_S
            )
            heating_power_w = (radiator_power_w + next_radiator_power_w) / 2
            radiator_power_w = next_radiator_power_w
            heat_loss_w = self._loss_model.calculate_heat_loss_w(
                simulated_room_temperature_c, mpc_input.outdoor_temp_c
            )
            net_heating_power_w = heating_power_w - heat_loss_w

            predicted_delta_c = self._capacity_model.predict_temperature_change_c(
                net_heating_power_w, MPC_SIMULATION_STEP_S
            )

            simulated_room_temperature_c += predicted_delta_c
            simulated_room_temperature_c = clamp(simulated_room_temperature_c, 0, 35)

            if (
                abs(predicted_delta_c) < MPC_SIMULATION_CONVERGENCE_DELTA_C
                and abs(target_power_w - radiator_power_w)
                < MPC_SIMULATION_CONVERGENCE_POWER_W
            ):
                break

        return simulated_room_temperature_c

    def _apply_demand_rate_limiting(
        self, mpc_input: RoomMpcInput, demand_pct: float, activation_pct: float
    ) -> float:
        """Step-limited demand, held against small and quick changes.

        Never an output strictly between 0 and activation_pct: the TRVs get
        their minimum target there, so a ramp value the hysteresis held
        (0 -> 5.33 % on the way to 10 % with a shut valve) kept the valve
        shut while the planner counted it open. Below the threshold the
        output is 0 until the stepped target reaches it.
        """
        config = self._rate_limit_config
        if self._demand_seed_until_ts is None:
            self._demand_seed_until_ts = mpc_input.now_ts + DEMAND_SEED_WINDOW_S
        if mpc_input.now_ts <= self._demand_seed_until_ts:
            self._seed_demand_from_valves(mpc_input)
        if below_activation(self._last_output_demand_pct, activation_pct):
            # Output while the threshold was lower: the TRVs shut on it now,
            # so hysteresis and hold time must not keep it.
            self._last_output_demand_pct = 0.0

        # Stamped on every call, also when the output is held below.
        if self._last_rate_limit_ts is None:
            elapsed_fraction = 1.0
        else:
            elapsed_fraction = clamp(
                (mpc_input.now_ts - self._last_rate_limit_ts) / DEMAND_STEP_REFERENCE_S,
                0.0,
                1.0,
            )
        self._last_rate_limit_ts = mpc_input.now_ts
        max_step_pct = config.max_demand_step_pct * elapsed_fraction

        self._target_demand_pct = clamp(
            demand_pct,
            self._target_demand_pct - max_step_pct,
            self._target_demand_pct + max_step_pct,
        )
        output_pct = (
            0.0
            if below_activation(self._target_demand_pct, activation_pct)
            else self._target_demand_pct
        )

        if (
            abs(output_pct - self._last_output_demand_pct)
            < config.demand_hysteresis_pct
        ):
            return self._last_output_demand_pct

        if (
            mpc_input.now_ts - self._last_demand_update_ts < config.hold_time_s
            and abs(output_pct - self._last_output_demand_pct)
            < config.hold_override_demand_pct
        ):
            return self._last_output_demand_pct

        self._last_output_demand_pct = output_pct
        self._last_demand_update_ts = mpc_input.now_ts

        return self._last_output_demand_pct

    def _seed_demand_from_valves(self, mpc_input: RoomMpcInput) -> None:
        """Start the ramp where the valves stand, not at zero.

        After a restart the TRVs still hold the previous target and report
        their opening. Ramping up from 0 wrote them a low target first and
        shut a radiator that had been fully open for ~20 minutes. The TRVs
        often report only some seconds after the first computation, so the
        seed waits for the first reported opening, at most
        DEMAND_SEED_WINDOW_S; without one the ramp starts at 0 as before.
        """
        openings = [
            opening for opening in mpc_input.valve_positions_pct if opening is not None
        ]
        if not openings:
            return
        seed_pct = clamp(sum(openings) / len(openings), 0.0, 100.0)
        self._target_demand_pct = seed_pct
        self._last_output_demand_pct = seed_pct
        # Seeded once; the ramp takes over from here.
        self._demand_seed_until_ts = float("-inf")

    def _reset_demand_rate_limiting(
        self, mpc_input: RoomMpcInput, demand_pct: float
    ) -> float:
        # Smoothing resumes from this demand once the forced mode ends.
        self._target_demand_pct = demand_pct
        self._last_output_demand_pct = demand_pct
        self._last_demand_update_ts = mpc_input.now_ts
        self._last_rate_limit_ts = mpc_input.now_ts
        return demand_pct

    def _record_learning_telemetry(
        self,
        mpc_input: RoomMpcInput,
        requested_heating_power_w: float,
        predicted_room_temperature_c: float,
        estimate: ObserverEstimate,
    ) -> None:
        self._learner.append_history(
            mpc_input,
            requested_heating_power_w,
            radiator_power_w=estimate.radiator_power_w,
        )
        self._learner.set_prediction(
            LearnerPrediction(
                timestamp=mpc_input.now_ts,
                predicted_room_temperature_c=predicted_room_temperature_c,
                prediction_horizon_s=MPC_PREDICTION_HORIZON_S,
            )
        )

    def run_learning_cycle(self) -> None:
        self._learner.run_learning_cycle()

    def set_gain_context(
        self,
        profile_index_at: Callable[[float], int],
        sun_source: Callable[[], SunSource | None],
    ) -> None:
        self._learner.set_gain_context(profile_index_at, sun_source)

    def consume_persisted_learning_factors(self) -> LearningFactors | None:
        return self._learner.consume_persisted_learning_factors()

    def recalibrate_learning_factors(self, factors: LearningFactors) -> None:
        self._learner.recalibrate(factors)

    def enable_learning(self) -> None:
        self._learner.enable()

    def disable_learning(self) -> None:
        self._learner.disable()

    def suppress_learning_for_interval(
        self, ua_duration_s: float, capacity_duration_s: float
    ) -> None:
        self._learner.suppress_for_interval(ua_duration_s, capacity_duration_s)

    def set_stationary_range_c(self, stationary_range_c: float) -> None:
        self._learner.set_stationary_range_c(stationary_range_c)

    def set_learning_window_active(self, active: bool, now_ts: float) -> None:
        self._learner.set_learning_window_active(active, now_ts)

    def set_learning_paused(self, paused: bool, now_ts: float) -> None:
        self._learner.set_learning_paused(paused, now_ts)

    def set_window_open(self, window_open: bool, now_ts: float) -> None:
        self._learner.set_window_open(window_open, now_ts)

    def set_sun_condition(self, sun_condition: callable[[float], bool]) -> None:
        self._learner.set_sun_condition(sun_condition)

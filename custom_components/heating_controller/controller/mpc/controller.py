from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from ...const import CapacityOutcome, TrvSetpointMode
from .learner import LearnerPrediction, RoomMpcModelLearner
from .math_helper import clamp, round_to_step
from .models.capacity import ThermalCapacityModel
from .models.emitter import HeatEmitterModel
from .models.loss import RoomLossModel
from .observer import ObserverEstimate, RoomStateObserver, step_radiator_power_w
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

        self._sensors = RoomMpcSensors(
            trvs, max_sensor_age_s, room_sensor_configured
        )
        self._emitter_model = HeatEmitterModel(thermal_config, trvs)
        self._loss_model = RoomLossModel(thermal_config)
        self._capacity_model = ThermalCapacityModel(thermal_config)
        self._learner = RoomMpcModelLearner(
            self._loss_model, self._capacity_model, target_band_c=self._target_band_c
        )
        # A disturbance beyond the design heat load is a model error, not heat
        # from neighbours or appliances.
        self._observer = RoomStateObserver(
            self._emitter_model,
            self._loss_model,
            self._capacity_model,
            disturbance_limit_w=thermal_config.room_heat_load_w,
            target_band_c=self._target_band_c,
        )
        self._heat_available = True

        self._target_demand_pct = 0.0
        self._last_output_demand_pct = 0.0
        self._last_demand_update_ts = 0.0

    def destroy(self) -> None:
        self._learner.destroy()

    @property
    def learned_ua_factor(self) -> float:
        return self._loss_model.learned_ua_factor

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
    def disturbance_w(self) -> float:
        return self._observer.disturbance_w

    def restore_disturbance(self, disturbance_w: float) -> None:
        self._observer.restore_disturbance(disturbance_w)

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
        input_result = self._sensors.create_input(target_temperature_c)
        if not input_result.valid:
            return RoomMpcComputeResult(valid=False, error=input_result.error)

        mpc_input = input_result.input
        estimate = self._observer.estimate(
            mpc_input,
            # Without a reported valve opening the radiator heat is a guess,
            # and an adapting TRV ignores the demand; the disturbance would
            # take up the difference.
            disturbance_update_allowed=not mpc_input.valve_position_missing
            and not mpc_input.trv_adapting
            and self._learner.disturbance_update_allowed(
                mpc_input.now_ts, mpc_input.room_sensor_fallback
            ),
        )

        available_heating_power_w = max(
            0.0,
            self._emitter_model.calculate_available_heating_power_w(
                mpc_input.room_temp_c, mpc_input.flow_temp_c
            ),
        )

        optimal_demand = self._find_optimal_demand_prediction(mpc_input, estimate)
        if apply_side_effects and skip_demand_smoothing:
            stabilized_demand_pct = self._reset_demand_rate_limiting(
                mpc_input, optimal_demand.demand_pct
            )
        elif apply_side_effects:
            stabilized_demand_pct = self._apply_demand_rate_limiting(
                mpc_input, optimal_demand.demand_pct
            )
        else:
            stabilized_demand_pct = optimal_demand.demand_pct

        requested_heating_power_w = round_to_step(
            available_heating_power_w * (stabilized_demand_pct / 100), 0.01
        )

        # The TRVs are thermostats: a small demand can open a valve fully. Where
        # they report their opening, that is what the radiators get.
        measured_heating_power_w = self._emitter_model.calculate_measured_heating_power_w(
            mpc_input.room_temp_c,
            mpc_input.flow_temp_c,
            mpc_input.valve_positions_pct,
            stabilized_demand_pct,
        )
        reported_openings_pct = [
            opening for opening in mpc_input.valve_positions_pct if opening is not None
        ]
        effective_opening_pct = (
            sum(reported_openings_pct) / len(reported_openings_pct)
            if reported_openings_pct
            else stabilized_demand_pct
        )

        flow_gate = self._live_flow_gate if apply_side_effects else self._preview_flow_gate
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
            self._observer.commit(
                estimate,
                mpc_input,
                target_power_w=(
                    measured_heating_power_w if self._heat_available else 0.0
                ),
                demand_pct=effective_opening_pct if self._heat_available else 0.0,
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
            disturbance_w=estimate.disturbance_w,
            measured_heating_power_w=measured_heating_power_w,
        )
        return RoomMpcComputeResult(valid=True, result=result)

    def _calculate_hold_flow_temperature_c(self, mpc_input: RoomMpcInput) -> float | None:
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
        sensor cannot drive the requirement. Like the hold flow it leaves the
        disturbance out: the flow requirement stays weather-driven.
        """
        deficit_c = mpc_input.target_temp_c - estimate.room_temperature_c
        hold_power_w = self._loss_model.calculate_heat_loss_w(
            mpc_input.target_temp_c, mpc_input.outdoor_temp_c
        )
        if deficit_c <= FLOW_RECOVERY_TOLERANCE_C or hold_power_w <= 0:
            return _RecoveryFlow(0.0)

        estimated_power_w = hold_power_w + (
            self._capacity_model.effective_capacity_j_per_k
            * deficit_c
            / FLOW_RECOVERY_HORIZON_S
        )
        spread_c = self._emitter_model.part_load_spread_c(estimated_power_w)

        def reaches_target(flow_temperature_c: float) -> bool:
            return (
                self._simulate_room_temperature_c(
                    mpc_input,
                    estimate,
                    demand_pct=100,
                    flow_temperature_c=flow_temperature_c,
                    duration_s=FLOW_RECOVERY_HORIZON_S,
                    spread_c=spread_c,
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
        self, mpc_input: RoomMpcInput, estimate: ObserverEstimate
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
        hold_demand_pct = self._hold_demand_pct(mpc_input, estimate)

        for demand_pct in range(0, 101, MPC_DEMAND_SEARCH_STEP_PCT):
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

    def _hold_demand_pct(
        self, mpc_input: RoomMpcInput, estimate: ObserverEstimate
    ) -> float:
        """Demand that holds the setpoint in steady state."""
        hold_power_w = max(
            0.0,
            self._loss_model.calculate_heat_loss_w(
                mpc_input.target_temp_c, mpc_input.outdoor_temp_c
            )
            - estimate.disturbance_w,
        )
        available_heating_power_w = (
            self._emitter_model.calculate_available_heating_power_w(
                mpc_input.target_temp_c, mpc_input.flow_temp_c
            )
        )
        if available_heating_power_w <= 0:
            return 100.0
        return clamp(100 * hold_power_w / available_heating_power_w, 0, 100)

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
            disturbance_w=estimate.disturbance_w,
        )

    def _simulate_room_temperature_c(
        self,
        mpc_input: RoomMpcInput,
        estimate: ObserverEstimate,
        demand_pct: float,
        flow_temperature_c: float | None,
        duration_s: float,
        spread_c: float | None = None,
        disturbance_w: float = 0.0,
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
            available_heating_power_w = (
                self._emitter_model.calculate_available_heating_power_w(
                    simulated_room_temperature_c, flow_temperature_c, spread_c
                )
            )
            target_power_w = available_heating_power_w * (demand_pct / 100)
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
            net_heating_power_w = heating_power_w + disturbance_w - heat_loss_w

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
        self, mpc_input: RoomMpcInput, demand_pct: float
    ) -> float:
        config = self._rate_limit_config

        self._target_demand_pct = clamp(
            demand_pct,
            self._target_demand_pct - config.max_demand_step_pct,
            self._target_demand_pct + config.max_demand_step_pct,
        )

        if (
            abs(self._target_demand_pct - self._last_output_demand_pct)
            < config.demand_hysteresis_pct
        ):
            return self._last_output_demand_pct

        if (
            mpc_input.now_ts - self._last_demand_update_ts < config.hold_time_s
            and abs(self._target_demand_pct - self._last_output_demand_pct)
            < config.hold_override_demand_pct
        ):
            return self._last_output_demand_pct

        self._last_output_demand_pct = self._target_demand_pct
        self._last_demand_update_ts = mpc_input.now_ts

        return self._last_output_demand_pct

    def _reset_demand_rate_limiting(
        self, mpc_input: RoomMpcInput, demand_pct: float
    ) -> float:
        # Smoothing resumes from this demand once the forced mode ends.
        self._target_demand_pct = demand_pct
        self._last_output_demand_pct = demand_pct
        self._last_demand_update_ts = mpc_input.now_ts
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
            disturbance_w=estimate.disturbance_w,
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

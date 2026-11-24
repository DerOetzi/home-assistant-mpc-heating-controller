from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from ...const import CapacityOutcome
from .hot_water import HotWaterChargeDetector
from .learner import RoomMpcModelLearner
from .math_helper import clamp
from .models.capacity import ThermalCapacityModel
from .models.emitter import HeatEmitterModel
from .models.gains import RoomGainModel, SunSource
from .models.loss import RoomLossModel
from .observer import ObserverEstimate, RoomStateObserver, step_radiator_power_w
from .radiator_monitor import RadiatorCheckSample, RadiatorDeliveryMonitor
from .results import (
    RoomMpcError,
    RoomMpcInput,
    RoomMpcResult,
    RoomTemperature,
)
from .sensors import RoomMpcSensors
from .types import (
    CapacityStretchInfo,
    FlowGateState,
    LearningFactors,
    RoomContext,
    RoomModelLearningState,
    RoomThermalConfig,
    TrvConfig,
    UaBalanceInfo,
)

# How quickly the boost flow asks to bring a room back to its
# setpoint. Long: a heat pump recovers more
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
MPC_SIMULATION_CONVERGENCE_DELTA_C = 0.001
MPC_SIMULATION_CONVERGENCE_POWER_W = 1.0


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

    So the gate closes only on both signals together: the TRVs report their
    valves shut (demand zero), and the room is above target by more
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
class _RecoveryFlow:
    flow_temperature_c: float
    saturated: bool = False


class RoomMpcController:
    def __init__(
        self,
        thermal_config: RoomThermalConfig,
        trvs: list[TrvConfig],
        max_sensor_age_s: float,
        room_sensor_configured: bool,
        flow_gate_config: FlowGateConfig | None = None,
    ) -> None:
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
            gain_model=self._gain_model,
        )
        self._observer = RoomStateObserver(
            self._emitter_model, self._loss_model, self._capacity_model
        )
        self._heat_available = True
        self._hot_water = HotWaterChargeDetector()
        # A charge seen outside the room: the house's one signal (decisive,
        # replaces the room's own detection on its flow) or the hot water
        # control's (adds to it).
        self._external_hot_water_charge = False
        self._external_hot_water_decisive = False
        self._radiator_monitors = [RadiatorDeliveryMonitor() for _ in trvs]
        self._valve_position_configured = [
            trv.valve_position_configured for trv in trvs
        ]
        self._room_heat_load_w = thermal_config.room_heat_load_w
        self._window_open = False
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

    @property
    def design_flow_temperature_c(self) -> float:
        """The radiators' design flow, the top of every flow search."""
        return self._emitter_model.flow_search_max_c

    @property
    def hot_water_charging(self) -> bool:
        """A hot water charge takes the flow, seen on it or by the hot water
        control."""
        if self._external_hot_water_decisive:
            return self._external_hot_water_charge
        return self._hot_water.charging or self._external_hot_water_charge

    def apply_context(self, context: RoomContext) -> None:
        """Take what the house tells the room; once before each calculation."""
        self._emitter_model.set_flow_threshold_c(context.flow_threshold_c)
        self._sensors.set_trv_adapting(context.trv_adapting)
        self._external_hot_water_charge = context.external_hot_water_charge
        self._external_hot_water_decisive = context.external_hot_water_decisive
        self._heat_available = context.heat_available
        if context.heat_available:
            self._learner.enable(context.now_ts)
        else:
            self._learner.disable(context.now_ts)

    def get_room_temperature(self) -> RoomTemperature | RoomMpcError:
        return self._sensors.get_room_temperature()

    def get_learning_state(self) -> RoomModelLearningState:
        return self._learner.get_learning_state()

    def get_ua_balance_info(self) -> UaBalanceInfo:
        return self._learner.get_ua_balance_info()

    def get_capacity_outcome(self) -> CapacityOutcome:
        return self._learner.get_capacity_outcome()

    def get_capacity_info(self) -> CapacityStretchInfo:
        return self._learner.get_capacity_info()

    @property
    def vent_capacity_factor(self) -> float:
        return self._learner.vent_capacity_factor

    @property
    def learned_vent_capacity_factor(self) -> float | None:
        """None until an airing stretch learned it."""
        return self._learner.learned_vent_capacity_factor

    def get_vent_info(self) -> CapacityStretchInfo:
        return self._learner.get_vent_info()

    @property
    def effective_capacity_j_per_k(self) -> float:
        return self._capacity_model.effective_capacity_j_per_k

    def set_trv_temperature(self, index: int, value: float | None) -> None:
        self._sensors.set_trv_temperature(index, value)

    def set_room_sensor_temperature(
        self, value: float | None, reported_ts: float | None = None
    ) -> None:
        """reported_ts: when the sensor last reported, now if not given."""
        self._sensors.set_room_sensor_temperature(value, reported_ts)

    def set_valve_position(
        self, index: int, value: float | None, reported_ts: float | None = None
    ) -> None:
        """reported_ts: when the TRV last reported, now if not given."""
        self._sensors.set_valve_position(index, value, reported_ts)

    def set_outdoor_temperature(self, value: float | None) -> None:
        self._sensors.set_outdoor_temperature(value)

    def set_flow_temperature(self, value: float | None) -> None:
        self._sensors.set_flow_temperature(value)

    def compute(
        self,
        target_temperature_c: float,
        mode_target_temperature_c: float | None = None,
    ) -> RoomMpcResult | RoomMpcError:
        """Compute the flow requirements and feed the observer and learner.

        The TRVs regulate the room themselves; the demand is what their
        valves report. The learner judges the room on the mode's setpoint,
        target_temperature_c where none is given.
        """
        now_ts = time.time()
        # Learning pauses while the outdoor temperature is missing, also when
        # the room temperature is missing as well.
        self._outdoor_temperature_missing = (
            self._sensors.is_outdoor_temperature_missing(now_ts)
        )
        self._learner.set_outdoor_temperature_missing(
            self._outdoor_temperature_missing, now_ts
        )
        mpc_input = self._sensors.create_input(target_temperature_c, now_ts)
        if isinstance(mpc_input, RoomMpcError):
            return mpc_input
        mpc_input.mode_target_temp_c = mode_target_temperature_c
        self._hot_water.update(
            mpc_input.now_ts,
            mpc_input.flow_temp_c,
            self._heat_available,
            self._external_hot_water_charge
            if self._external_hot_water_decisive
            else None,
        )
        self._apply_hot_water_charge(mpc_input)
        self._update_radiator_monitors(mpc_input)
        self._apply_radiator_state(mpc_input)
        estimate = self._observer.estimate(mpc_input)
        result = self._derive_result(mpc_input, estimate, self._live_flow_gate)
        self._commit(mpc_input, estimate, result.measured_heating_power_w)
        return result

    def preview(self, target_temperature_c: float) -> RoomMpcResult | RoomMpcError:
        """The flow requirements for another setpoint, without side effects.

        Only the preview's own surplus gate moves.
        """
        mpc_input = self._sensors.create_input(target_temperature_c, time.time())
        if isinstance(mpc_input, RoomMpcError):
            return mpc_input
        self._apply_hot_water_charge(mpc_input)
        self._apply_radiator_state(mpc_input)
        estimate = self._observer.estimate(mpc_input)
        return self._derive_result(mpc_input, estimate, self._preview_flow_gate)

    def _apply_hot_water_charge(self, mpc_input: RoomMpcInput) -> None:
        if not self.hot_water_charging:
            return
        # The radiators get no water; the valves keep the demand of the
        # heating flow they get back once the charge is done.
        mpc_input.hot_water_charge = True
        if self._hot_water.heating_flow_c is not None:
            mpc_input.flow_temp_c = self._hot_water.heating_flow_c

    def _apply_radiator_state(self, mpc_input: RoomMpcInput) -> None:
        mpc_input.radiator_not_heating = any(self.radiators_not_heating)
        mpc_input.radiator_not_heating_since_ts = min(
            (
                monitor.not_heating_since_ts
                for monitor in self._radiator_monitors
                if monitor.not_heating_since_ts is not None
            ),
            default=None,
        )

    def _derive_result(
        self,
        mpc_input: RoomMpcInput,
        estimate: ObserverEstimate,
        flow_gate: _SurplusFlowGate,
    ) -> RoomMpcResult:
        """The flow requirements from the input and the observed state."""
        hold_flow_c = self._calculate_hold_flow_temperature_c(mpc_input)
        flow_gate_closed = self._update_flow_gate(mpc_input, flow_gate)
        recovery_flow = self._calculate_recovery_flow(mpc_input, estimate)
        return RoomMpcResult(
            input=mpc_input,
            available_heating_power_w=max(
                0.0,
                self._emitter_model.calculate_available_heating_power_w(
                    mpc_input.room_temp_c, mpc_input.flow_temp_c
                ),
            ),
            hold_flow_temperature_c=hold_flow_c,
            gated_hold_flow_temperature_c=(
                0.0 if flow_gate_closed else (hold_flow_c or 0.0)
            ),
            recovery_flow_temperature_c=recovery_flow.flow_temperature_c,
            recovery_flow_saturated=recovery_flow.saturated,
            flow_gate_closed=flow_gate_closed,
            estimated_room_temperature_c=estimate.room_temperature_c,
            radiator_power_w=estimate.radiator_power_w,
            # A radiator without a reported opening counts with the room's
            # highest one.
            measured_heating_power_w=(
                self._emitter_model.calculate_measured_heating_power_w(
                    mpc_input.room_temp_c,
                    mpc_input.flow_temp_c,
                    mpc_input.valve_positions_pct,
                    _room_demand_pct(mpc_input),
                )
            ),
        )

    def _update_flow_gate(
        self, mpc_input: RoomMpcInput, flow_gate: _SurplusFlowGate
    ) -> bool:
        """Whether the surplus gate holds the hold flow back."""
        # A TRV that should report its opening and does not may well be open:
        # the gate stays as it is.
        demand_unknown = any(
            configured and opening is None
            for configured, opening in zip(
                self._valve_position_configured,
                mpc_input.valve_positions_pct,
                strict=True,
            )
        )
        if demand_unknown:
            return flow_gate.closed
        return flow_gate.update(
            surplus_c=mpc_input.room_temp_c - mpc_input.target_temp_c,
            demand_pct=_room_demand_pct(mpc_input),
            now_ts=mpc_input.now_ts,
        )

    def _commit(
        self,
        mpc_input: RoomMpcInput,
        estimate: ObserverEstimate,
        measured_heating_power_w: float,
    ) -> None:
        """Hand the cycle to the observer and the learner."""
        water_flowing = self._heat_available and not mpc_input.hot_water_charge
        applied_heating_power_w = measured_heating_power_w if water_flowing else 0.0
        self._observer.commit(
            estimate,
            mpc_input,
            target_power_w=applied_heating_power_w,
            demand_pct=_mean_opening_pct(mpc_input) if water_flowing else 0.0,
        )
        self._learner.append_history(
            mpc_input,
            applied_heating_power_w,
            radiator_power_w=estimate.radiator_power_w,
        )

    def _calculate_hold_flow_temperature_c(
        self, mpc_input: RoomMpcInput
    ) -> float | None:
        """Flow needed to hold the setpoint -- ungated, weather-driven only."""
        return self.calculate_hold_flow_temperature_c(
            mpc_input.target_temp_c, mpc_input.outdoor_temp_c
        )

    def calculate_hold_flow_temperature_c(
        self, target_temp_c: float, outdoor_temp_c: float, *, round_up: bool = True
    ) -> float | None:
        """Flow that holds target_temp_c at outdoor_temp_c with the learned UA.

        Also asked for outdoor values other than the current one, e.g. the
        heating curve's end points, to read off a curve the room would need.
        Without round_up the flow is not stepped up to the search grid, for
        drawing a smooth curve.
        """
        hold_power_w = max(
            0.0,
            self._loss_model.calculate_heat_loss_w(target_temp_c, outdoor_temp_c),
        )
        return self._emitter_model.calculate_recommended_flow_temperature_c(
            hold_power_w, target_temp_c, round_up=round_up
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

    def run_learning_cycle(self) -> None:
        self._learner.run_learning_cycle()

    def set_gain_context(
        self,
        profile_index_at: Callable[[float], int],
        local_day_at: Callable[[float], int],
        sun_source: Callable[[], SunSource | None],
    ) -> None:
        self._learner.set_gain_context(profile_index_at, local_day_at, sun_source)

    def take_factors_to_save(self) -> LearningFactors | None:
        """Learned factors not saved yet; None if nothing new. Clears them."""
        return self._learner.take_factors_to_save()

    def take_gains_to_save(self) -> dict | None:
        """The gain model's export if it changed since; None otherwise."""
        return self._gain_model.export() if self._gain_model.take_changed() else None

    def reset_gains(self) -> None:
        self._gain_model.reset()

    def restore_gains(self, data: dict) -> None:
        self._gain_model.restore(data)

    def recalibrate_learning_factors(self, factors: LearningFactors) -> None:
        self._learner.recalibrate(factors)

    def suppress_learning_for_interval(
        self, ua_duration_s: float, capacity_duration_s: float
    ) -> None:
        self._learner.suppress_for_interval(ua_duration_s, capacity_duration_s)

    def set_learning_paused(self, paused: bool, now_ts: float) -> None:
        self._learner.set_learning_paused(paused, now_ts)

    def set_window_open(self, window_open: bool, now_ts: float) -> None:
        self._window_open = window_open
        self._learner.set_window_open(window_open, now_ts)

    def _update_radiator_monitors(self, mpc_input: RoomMpcInput) -> None:
        room_c = mpc_input.room_temp_c
        surplus_share = (
            (
                self._emitter_model.calculate_available_heating_power_w(
                    room_c, mpc_input.flow_temp_c
                )
                - self._loss_model.effective_ua_w_per_k
                * (room_c - mpc_input.outdoor_temp_c)
            )
            / self._room_heat_load_w
            if mpc_input.flow_temp_c is not None and self._room_heat_load_w > 0
            else None
        )
        mode_target_c = (
            mpc_input.mode_target_temp_c
            if mpc_input.mode_target_temp_c is not None
            else mpc_input.target_temp_c
        )
        excluded = (
            mpc_input.hot_water_charge or self._window_open or mpc_input.trv_adapting
        )
        for monitor, demand_pct, output_fraction in zip(
            self._radiator_monitors,
            mpc_input.valve_positions_pct or [None] * len(self._radiator_monitors),
            self._emitter_model.full_output_fractions(mpc_input.flow_temp_c, room_c),
            strict=True,
        ):
            monitor.update(
                RadiatorCheckSample(
                    now_ts=mpc_input.now_ts,
                    demand_pct=demand_pct,
                    room_temperature_c=room_c,
                    mode_target_c=mode_target_c,
                    heat_available=self._heat_available,
                    full_output_fraction=output_fraction,
                    surplus_share=surplus_share,
                    excluded=excluded,
                )
            )

    def set_window_unknown(self, unknown: bool) -> None:
        self._learner.set_window_unknown(unknown)

    def set_sun_condition(self, sun_condition: Callable[[float], bool]) -> None:
        self._learner.set_sun_condition(sun_condition)


def _reported_openings_pct(mpc_input: RoomMpcInput) -> list[float]:
    return [o for o in mpc_input.valve_positions_pct if o is not None]


def _room_demand_pct(mpc_input: RoomMpcInput) -> float:
    """The highest valve opening any TRV of the room reports."""
    return max(_reported_openings_pct(mpc_input), default=0.0)


def _mean_opening_pct(mpc_input: RoomMpcInput) -> float:
    openings = _reported_openings_pct(mpc_input)
    return sum(openings) / len(openings) if openings else 0.0

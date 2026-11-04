from __future__ import annotations

from dataclasses import dataclass
from math import ceil, exp

from .models.capacity import ThermalCapacityModel
from .models.emitter import HeatEmitterModel
from .models.loss import RoomLossModel
from .results import RoomMpcInput

OBSERVER_STEP_S = 150
# How fast the estimate follows the measured room temperature. Short enough
# that it never strays far from what the sensor says, long enough to average
# 0.1 K steps.
OBSERVER_TEMPERATURE_TIME_CONSTANT_S = 300
# A difference this large is no model error but a jump: a sensor source
# switching over, an open window not reported. The estimate takes the
# measurement as it is.
OBSERVER_RESYNC_ERROR_C = 1.0
# After a longer gap (no valid input) the propagated temperature is no longer
# worth correcting; the estimate restarts from the measurement.
OBSERVER_MAX_GAP_S = 30 * 60


def step_radiator_power_w(
    radiator_power_w: float,
    target_power_w: float,
    time_constant_s: float,
    duration_s: float,
) -> float:
    """First-order lag of the radiator output towards the requested power."""
    if time_constant_s <= 0:
        return target_power_w
    return target_power_w + (radiator_power_w - target_power_w) * exp(
        -duration_s / time_constant_s
    )


@dataclass
class ObserverEstimate:
    timestamp: float
    room_temperature_c: float
    radiator_power_w: float


@dataclass
class _CommittedState:
    estimate: ObserverEstimate
    target_power_w: float
    demand_pct: float
    flow_temperature_c: float | None
    outdoor_temperature_c: float


class RoomStateObserver:
    """Runs the room model alongside the room as a state observer.

    The model carries what no sensor measures: the heat still in the radiators
    after the valve closed (or not yet in them after it opened). Each update
    propagates the model with the power requested last time and pulls the
    estimate a fraction of the way towards the measurement. A lasting
    difference between model and room is left to the learned heat-loss
    factor.
    """

    def __init__(
        self,
        emitter_model: HeatEmitterModel,
        loss_model: RoomLossModel,
        capacity_model: ThermalCapacityModel,
    ) -> None:
        self._emitter_model = emitter_model
        self._loss_model = loss_model
        self._capacity_model = capacity_model
        self._state: _CommittedState | None = None

    def estimate(self, mpc_input: RoomMpcInput) -> ObserverEstimate:
        """The corrected state at the input's time; nothing is stored."""
        measured_c = mpc_input.room_temp_c
        state = self._state
        if state is None:
            return ObserverEstimate(
                timestamp=mpc_input.now_ts,
                room_temperature_c=measured_c,
                radiator_power_w=0.0,
            )

        duration_s = mpc_input.now_ts - state.estimate.timestamp
        if duration_s <= 0:
            return state.estimate

        room_temperature_c, radiator_power_w = self._propagate(state, duration_s)
        error_c = measured_c - room_temperature_c
        if duration_s > OBSERVER_MAX_GAP_S or abs(error_c) > OBSERVER_RESYNC_ERROR_C:
            return ObserverEstimate(
                timestamp=mpc_input.now_ts,
                room_temperature_c=measured_c,
                radiator_power_w=radiator_power_w,
            )
        room_temperature_c += (
            1 - exp(-duration_s / OBSERVER_TEMPERATURE_TIME_CONSTANT_S)
        ) * error_c

        return ObserverEstimate(
            timestamp=mpc_input.now_ts,
            room_temperature_c=room_temperature_c,
            radiator_power_w=radiator_power_w,
        )

    def commit(
        self,
        estimate: ObserverEstimate,
        mpc_input: RoomMpcInput,
        target_power_w: float,
        demand_pct: float,
    ) -> None:
        """Store the estimate and the power the radiators now head for."""
        self._state = _CommittedState(
            estimate=estimate,
            target_power_w=target_power_w,
            demand_pct=demand_pct,
            flow_temperature_c=mpc_input.flow_temp_c,
            outdoor_temperature_c=mpc_input.outdoor_temp_c,
        )

    def _propagate(
        self, state: _CommittedState, duration_s: float
    ) -> tuple[float, float]:
        room_temperature_c = state.estimate.room_temperature_c
        radiator_power_w = state.estimate.radiator_power_w
        step_count = max(1, ceil(duration_s / OBSERVER_STEP_S))
        step_s = duration_s / step_count
        for _ in range(step_count):
            time_constant_s = self._emitter_model.time_constant_s(
                room_temperature_c,
                state.flow_temperature_c,
                state.demand_pct,
                heating_up=state.target_power_w > radiator_power_w,
            )
            next_radiator_power_w = step_radiator_power_w(
                radiator_power_w, state.target_power_w, time_constant_s, step_s
            )
            net_heating_power_w = (
                radiator_power_w + next_radiator_power_w
            ) / 2 - self._loss_model.calculate_heat_loss_w(
                room_temperature_c, state.outdoor_temperature_c
            )
            room_temperature_c += self._capacity_model.predict_temperature_change_c(
                net_heating_power_w, step_s
            )
            radiator_power_w = next_radiator_power_w
        return room_temperature_c, radiator_power_w

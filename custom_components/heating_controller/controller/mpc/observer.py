from __future__ import annotations

from dataclasses import dataclass
from math import ceil, exp

from .math_helper import clamp
from .models.capacity import ThermalCapacityModel
from .models.emitter import MIN_ACTIVE_DEMAND, HeatEmitterModel
from .models.loss import RoomLossModel
from .results import RoomMpcInput

OBSERVER_STEP_S = 150
# How fast the estimate follows the measured room temperature. Short enough
# that it never strays far from what the sensor says, long enough to average
# 0.1 K steps.
OBSERVER_TEMPERATURE_TIME_CONSTANT_S = 300
# How fast the disturbance takes up a lasting difference between the model and
# the room. Much slower than the temperature correction, so a passing
# disturbance (a shower, a door left open) hardly moves it. A lasting 100 W
# shows as only about 0.01 K between model and room, while most room sensors
# step in 0.1 K: at three hours those steps alone swung the disturbance by
# 50-100 W an hour. It is also the shortest time the disturbance takes from
# zero to its limit.
OBSERVER_DISTURBANCE_TIME_CONSTANT_S = 12 * 3600
# A difference this large is no model error but a jump: a sensor source
# switching over, an open window not reported. The estimate takes the
# measurement as it is and the disturbance stays.
OBSERVER_RESYNC_ERROR_C = 1.0
# After a longer gap (no valid input) the propagated temperature is no longer
# worth correcting; the estimate restarts from the measurement.
OBSERVER_MAX_GAP_S = 30 * 60
# Anti-windup: with the valve shut the controller cannot heat any less, with it
# fully open not any more. A room coasting above its setpoint on stored heat
# (walls after the day, an overshoot) would otherwise be booked as a lasting
# gain, a room the radiators cannot lift as a lasting loss -- both push the
# demand further into the limit it already sits at. Moving the other way is
# only allowed where the controller actually needs out of the limit: below the
# target band with the valve shut, above the setpoint with it fully open.
# Otherwise sensor steps against the held direction would ratchet the
# disturbance one way (a room above setpoint, valve shut, 0.1 K steps down).
# Below the TRVs' minimum active demand the valve counts as shut.
VALVE_SHUT_DEMAND_PCT = MIN_ACTIVE_DEMAND * 100
VALVE_FULL_DEMAND_PCT = 95.0


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


def _held_at_valve_limit(
    step_w: float,
    demand_pct: float,
    room_temperature_c: float,
    target_temperature_c: float,
    target_band_c: float,
) -> bool:
    """Whether a disturbance step is held while the valve sits at a limit.

    More disturbance means less demand. With the valve shut a rise is always
    held and a fall only passes while the room is below the target band;
    with it fully open a fall is always held and a rise only passes while
    the room is above the setpoint.
    """
    if step_w == 0:
        return False
    if demand_pct < VALVE_SHUT_DEMAND_PCT:
        return step_w > 0 or room_temperature_c >= target_temperature_c - target_band_c
    if demand_pct >= VALVE_FULL_DEMAND_PCT:
        return step_w < 0 or room_temperature_c <= target_temperature_c
    return False


@dataclass
class ObserverEstimate:
    timestamp: float
    room_temperature_c: float
    radiator_power_w: float
    disturbance_w: float


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
    after the valve closed (or not yet in them after it opened), and a lasting
    disturbance -- neighbouring rooms, appliances, a heat load or radiator
    sized off -- that the room shows as a steady offset from the model. Each
    update propagates the model with the power requested last time and pulls
    the estimate a fraction of the way towards the measurement; the remaining
    difference slowly feeds the disturbance while that is allowed.
    """

    def __init__(
        self,
        emitter_model: HeatEmitterModel,
        loss_model: RoomLossModel,
        capacity_model: ThermalCapacityModel,
        disturbance_limit_w: float,
        target_band_c: float = 0.0,
    ) -> None:
        self._emitter_model = emitter_model
        self._target_band_c = max(0.0, target_band_c)
        self._loss_model = loss_model
        self._capacity_model = capacity_model
        self._disturbance_limit_w = max(0.0, disturbance_limit_w)
        self._state: _CommittedState | None = None
        self._disturbance_w = 0.0

    @property
    def disturbance_w(self) -> float:
        return self._disturbance_w

    def restore_disturbance(self, disturbance_w: float) -> None:
        self._disturbance_w = clamp(
            disturbance_w, -self._disturbance_limit_w, self._disturbance_limit_w
        )
        if self._state is not None:
            self._state.estimate.disturbance_w = self._disturbance_w

    def estimate(
        self, mpc_input: RoomMpcInput, disturbance_update_allowed: bool
    ) -> ObserverEstimate:
        """The corrected state at the input's time; nothing is stored."""
        measured_c = mpc_input.room_temp_c
        state = self._state
        if state is None:
            return ObserverEstimate(
                timestamp=mpc_input.now_ts,
                room_temperature_c=measured_c,
                radiator_power_w=0.0,
                disturbance_w=self._disturbance_w,
            )

        duration_s = mpc_input.now_ts - state.estimate.timestamp
        if duration_s <= 0:
            return state.estimate

        room_temperature_c, radiator_power_w = self._propagate(state, duration_s)
        if duration_s > OBSERVER_MAX_GAP_S:
            return ObserverEstimate(
                timestamp=mpc_input.now_ts,
                room_temperature_c=measured_c,
                radiator_power_w=radiator_power_w,
                disturbance_w=self._disturbance_w,
            )

        error_c = measured_c - room_temperature_c
        if abs(error_c) > OBSERVER_RESYNC_ERROR_C:
            return ObserverEstimate(
                timestamp=mpc_input.now_ts,
                room_temperature_c=measured_c,
                radiator_power_w=radiator_power_w,
                disturbance_w=self._disturbance_w,
            )
        room_temperature_c += (
            1 - exp(-duration_s / OBSERVER_TEMPERATURE_TIME_CONSTANT_S)
        ) * error_c

        disturbance_w = self._disturbance_w
        if disturbance_update_allowed:
            max_step_w = (
                self._disturbance_limit_w
                * duration_s
                / OBSERVER_DISTURBANCE_TIME_CONSTANT_S
            )
            step_w = clamp(
                self._capacity_model.effective_capacity_j_per_k
                * error_c
                * duration_s
                / (
                    OBSERVER_TEMPERATURE_TIME_CONSTANT_S
                    * OBSERVER_DISTURBANCE_TIME_CONSTANT_S
                ),
                -max_step_w,
                max_step_w,
            )
            if _held_at_valve_limit(
                step_w,
                state.demand_pct,
                measured_c,
                mpc_input.target_temp_c,
                self._target_band_c,
            ):
                step_w = 0.0
            disturbance_w = clamp(
                disturbance_w + step_w,
                -self._disturbance_limit_w,
                self._disturbance_limit_w,
            )

        return ObserverEstimate(
            timestamp=mpc_input.now_ts,
            room_temperature_c=room_temperature_c,
            radiator_power_w=radiator_power_w,
            disturbance_w=disturbance_w,
        )

    def commit(
        self,
        estimate: ObserverEstimate,
        mpc_input: RoomMpcInput,
        target_power_w: float,
        demand_pct: float,
    ) -> None:
        """Store the estimate and the power the radiators now head for."""
        self._disturbance_w = estimate.disturbance_w
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
                (radiator_power_w + next_radiator_power_w) / 2
                + state.estimate.disturbance_w
                - self._loss_model.calculate_heat_loss_w(
                    room_temperature_c, state.outdoor_temperature_c
                )
            )
            room_temperature_c += self._capacity_model.predict_temperature_change_c(
                net_heating_power_w, step_s
            )
            radiator_power_w = next_radiator_power_w
        return room_temperature_c, radiator_power_w

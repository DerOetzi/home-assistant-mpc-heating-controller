from __future__ import annotations

import time
from dataclasses import dataclass

from ...const import (
    DEFAULT_STATIONARY_RANGE_C,
    SENSOR_POLL_INTERVAL_MINUTES,
    CapacityOutcome,
    LearningStatus,
    UaBalanceOutcome,
)
from .models.capacity import ThermalCapacityModel
from .models.loss import RoomLossModel
from .results import RoomMpcInput
from .types import LearningFactors, RoomModelLearningState, UaBalanceInfo

MIN_HISTORY_SAMPLES = 5
MIN_ROOM_DELTA_C = 0.15
# Capacity only scales how far the room moves. Below this simulated change the
# error comes from power or losses, and its sign says nothing about capacity.
MIN_SIMULATED_CHANGE_C = 0.1
MAX_OUTDOOR_DELTA_C = 1.0
MAX_FLOW_DELTA_C = 5.0
CAPACITY_LEARNING_RATE = 0.005

# UA is learned from a heat balance over a long window rather than from single
# prediction windows: in closed loop the room sits at the setpoint the
# prediction aims for, so a wrong UA hardly shows as a prediction error. Over
# the window, the heat the controller requested has to match the losses plus
# what the room stored; the UA that balances this is the learning target.
#
# The window is a stretch of usable time: it runs until the learning window
# closes, learning is paused, a window contact opens, the configured room
# sensor drops out, heating stays off for longer than a short interruption, or
# the sun condition ends, and is balanced
# once at that end. A night shortened by a disturbance still counts as long as
# the remaining stretch is long enough; a stretch that reaches the maximum is
# balanced right away so a learning window that never closes still learns.
UA_BALANCE_MIN_WINDOW_S = 4 * 3600
UA_BALANCE_MAX_WINDOW_S = 8 * 3600
# Heating briefly unavailable (a missed heat-source poll, the flow dipping
# under the threshold) does not end the stretch: up to three controller cycles
# are bridged as no heating, which is what the radiators got. The extra minute
# absorbs the jitter of the poll that ends the interruption.
UA_BALANCE_MAX_INTERRUPTION_S = 3 * SENSOR_POLL_INTERVAL_MINUTES * 60 + 60
HISTORY_RETENTION_S = UA_BALANCE_MAX_WINDOW_S + 3600
UA_BALANCE_MAX_GAP_S = 20 * 60
# The stored heat relies on the (uncertain) capacity, so only windows where the
# room ends close to where it started are used. The drift is the trend over
# the whole stretch, not the difference of its edges: heating pulses, a room
# sensor reporting once an hour or a hot-water dip at the start move a short
# edge by tenths of a kelvin without the room storing any heat.
UA_BALANCE_MAX_DRIFT_C = 0.3
# A room still coasting down from above its setpoint with the valve shut would
# end the night far below where it started, one recovering from below (after a
# long valve calibration, say) far above. The stretch starts at the first
# sample no more than this above the setpoint or below the target band, or
# once the room has held its temperature for an hour, then from the start of
# that hour: a room kept above its setpoint by its neighbours never comes down
# and would otherwise never learn.
UA_BALANCE_SETTLED_MARGIN_C = 0.1
UA_BALANCE_STATIONARY_WINDOW_S = 3600
UA_BALANCE_STATIONARY_SLOPE_C_PER_H = 0.05
# In mild nights little heat is requested, and heat from neighbouring rooms or
# appliances weighs too much in the balance.
UA_BALANCE_MIN_MEAN_DELTA_C = 8.0
# Even within the drift limit the stored heat must stay a minor part of the
# balance; otherwise the result depends on the capacity more than on the
# losses.
UA_BALANCE_MAX_STORAGE_SHARE = 0.5
# A target this low means the radiators supplied almost nothing: the room
# lives off its neighbours, whose heat the model has no coupling for. The
# factor still moves, but only down to this limit; the rest stays with the
# observer's disturbance. Rooms that never learned otherwise kept the design
# heat load and with it an inflated hold flow.
UA_BALANCE_MIN_TARGET_FACTOR = 0.15
# Above this the stretch is a measurement artefact and is discarded.
UA_BALANCE_MAX_TARGET_FACTOR = 2.0
UA_BALANCE_WEIGHT = 0.3
# The heat assumed for a fully open valve is the least certain one: a radiator
# that is flow-limited gives off less (study, 28/29 October: target 0.54 with
# the valve open all night, 0.40-0.41 on nights it held its setpoint). A
# stretch that spent more than this share with every reported valve at least
# this open and the room below its target band is not learned from.
VALVE_SATURATED_OPENING_PCT = 95.0
UA_BALANCE_MAX_SATURATED_SHARE = 0.5


@dataclass
class LearnerHistoryEntry:
    timestamp: float
    room_temperature_c: float
    outdoor_temperature_c: float
    applied_heating_power_w: float
    # What the observer estimates the radiators gave off at this time: the
    # requested power, lagged by the radiator mass.
    radiator_power_w: float
    disturbance_w: float = 0.0
    target_temperature_c: float | None = None
    flow_temperature_c: float | None = None
    room_sensor_fallback: bool = False
    valve_position_missing: bool = False
    trv_adapting: bool = False
    radiator_not_heating: bool = False
    # A domestic hot-water charge ran: the radiators got no water.
    hot_water_charge: bool = False
    # All reported valves at least VALVE_SATURATED_OPENING_PCT open with the
    # room below its target band.
    valve_saturated: bool = False
    window_open: bool = False


@dataclass
class LearnerPrediction:
    timestamp: float
    predicted_room_temperature_c: float
    prediction_horizon_s: float


@dataclass
class _FactorSuppression:
    suppressed_until_ts: float = 0.0
    current_window_invalid: bool = False
    next_window_invalid: bool = False

    def suppress(self, until_ts: float) -> None:
        self.suppressed_until_ts = until_ts
        self.current_window_invalid = True

    def mark_next_window(self, now_ts: float) -> None:
        if now_ts < self.suppressed_until_ts:
            self.next_window_invalid = True

    def rotate(self) -> None:
        self.current_window_invalid = self.next_window_invalid
        self.next_window_invalid = False

    def reset(self) -> None:
        self.current_window_invalid = False
        self.next_window_invalid = False


class RoomMpcModelLearner:
    def __init__(
        self,
        loss_model: RoomLossModel,
        capacity_model: ThermalCapacityModel,
        target_band_c: float = 0.0,
    ) -> None:
        self._loss_model = loss_model
        self._capacity_model = capacity_model
        self._target_band_c = max(0.0, target_band_c)

        self._history: list[LearnerHistoryEntry] = []
        self._active_prediction: LearnerPrediction | None = None
        self._last_prediction: LearnerPrediction | None = None

        self._enabled = False
        self._ua_blocked_until_ts = 0.0
        self._ua_balance_from_ts = 0.0
        self._ua_balance_closed: tuple[float, float] | None = None
        self._ua_balance_bridged: list[tuple[float, float]] = []
        self._disabled_since_ts: float | None = None
        self._ua_balance_info = UaBalanceInfo()
        self._capacity_suppression = _FactorSuppression()
        self._capacity_outcome = CapacityOutcome.DISABLED
        self._stationary_range_c = DEFAULT_STATIONARY_RANGE_C
        self._learning_window_active = True
        self._learning_window_active_since_ts = 0.0
        self._learning_paused = False
        self._learning_resumed_ts = 0.0
        self._room_sensor_fallback = False
        self._valve_position_missing = False
        self._trv_adapting = False
        self._radiator_not_heating = False
        self._window_open = False
        self._sun_condition: callable[[float], bool] = lambda _ts: True
        self._pending_persisted_factors: LearningFactors | None = None
        self._pending_disturbance_transfer_w = 0.0

        self._learning_state = self._create_learning_state(LearningStatus.DISABLED)

    def destroy(self) -> None:
        self.disable()

    def enable(self, now_ts: float | None = None) -> None:
        if self._enabled:
            return
        now_ts = time.time() if now_ts is None else now_ts
        self._active_prediction = None
        self._last_prediction = None
        self._pending_persisted_factors = None
        # History keeps growing while disabled, with power the TRVs never got:
        # a short interruption is bridged as no heating, a longer one ends the
        # heat-balance stretch.
        disabled_since_ts = self._disabled_since_ts
        self._disabled_since_ts = None
        if (
            disabled_since_ts is not None
            and now_ts - disabled_since_ts <= UA_BALANCE_MAX_INTERRUPTION_S
        ):
            self._ua_balance_bridged.append((disabled_since_ts, now_ts))
        else:
            if disabled_since_ts is not None:
                self._close_ua_balance(disabled_since_ts)
            if self._history:
                self._ua_balance_from_ts = max(
                    self._ua_balance_from_ts, self._history[-1].timestamp
                )
        self._enabled = True
        self._capacity_outcome = CapacityOutcome.WAITING
        self._learning_state = self._create_learning_state(
            LearningStatus.PAUSED
            if self._learning_paused
            else LearningStatus.WAITING_INTERVAL
        )

    def disable(self, now_ts: float | None = None) -> None:
        if self._enabled:
            self._enabled = False
            self._pending_persisted_factors = None
            self._disabled_since_ts = time.time() if now_ts is None else now_ts
        self._learning_state = self._create_learning_state(LearningStatus.DISABLED)
        self._capacity_outcome = CapacityOutcome.DISABLED
        self._active_prediction = None
        self._last_prediction = None
        self._capacity_suppression.reset()

    def suppress_for_interval(
        self, ua_duration_s: float, capacity_duration_s: float
    ) -> None:
        now = time.time()
        self._close_ua_balance(now)
        self._ua_blocked_until_ts = now + ua_duration_s
        self._capacity_suppression.suppress(now + capacity_duration_s)
        self._pending_persisted_factors = None
        self._learning_state = self._create_learning_state(LearningStatus.SUPPRESSED)

    def set_stationary_range_c(self, stationary_range_c: float) -> None:
        self._stationary_range_c = stationary_range_c

    def set_learning_window_active(self, active: bool, now_ts: float) -> None:
        if active and not self._learning_window_active:
            self._learning_window_active_since_ts = now_ts
        if not active and self._learning_window_active:
            self._close_ua_balance(now_ts)
        self._learning_window_active = active

    def set_learning_paused(self, paused: bool, now_ts: float) -> None:
        # Unlike the learning window this blocks both factors: it is an external
        # veto for when the inputs themselves are not trustworthy (e.g. an
        # estimated outdoor temperature). Windows that started before the
        # resume carry paused samples and are discarded.
        if self._learning_paused and not paused:
            self._learning_resumed_ts = now_ts
        if paused and not self._learning_paused:
            self._close_ua_balance(now_ts)
        self._learning_paused = paused
        if paused and self._enabled:
            self._learning_state = self._create_learning_state(LearningStatus.PAUSED)

    def set_window_open(self, window_open: bool, now_ts: float) -> None:
        # Blocks both factors for as long as the window is open, however long
        # that is: the stretch before it is still balanced, the next one starts
        # no earlier than the close (the caller adds its settling time).
        if window_open and not self._window_open:
            self._close_ua_balance(now_ts)
            self._pending_persisted_factors = None
        if self._window_open and not window_open:
            self._ua_blocked_until_ts = max(self._ua_blocked_until_ts, now_ts)
        self._window_open = window_open
        if window_open and self._enabled:
            self._learning_state = self._create_learning_state(LearningStatus.SUPPRESSED)

    def set_sun_condition(self, sun_condition: callable[[float], bool]) -> None:
        self._sun_condition = sun_condition

    def disturbance_update_allowed(
        self, now_ts: float, room_sensor_fallback: bool
    ) -> bool:
        """Whether the observer may move its disturbance estimate now.

        Under the same conditions the factors learn from, minus the learning
        window: heating available, inputs trusted, window closed and settled,
        room sensor reporting, no sun. During the day the disturbance from the
        night is kept; sun would otherwise be taken up and then be missing for
        hours after sunset. Without heating the radiator model's error drops
        out of the residual, so what is left would not fit once heating
        resumes; the disturbance of the last heating night is kept instead.
        """
        return (
            self._enabled
            and not self._learning_paused
            and not self._window_open
            and not room_sensor_fallback
            and not self._room_sensor_fallback
            and not self._valve_position_missing
            and not self._trv_adapting
            and not self._radiator_not_heating
            and now_ts >= self._ua_blocked_until_ts
            and self._sun_condition(now_ts)
        )

    def append_history(
        self,
        mpc_input: RoomMpcInput,
        applied_heating_power_w: float,
        radiator_power_w: float,
        disturbance_w: float = 0.0,
    ) -> None:
        self._track_degraded_inputs(mpc_input)
        self._history.append(
            LearnerHistoryEntry(
                timestamp=mpc_input.now_ts,
                room_temperature_c=mpc_input.room_temp_c,
                outdoor_temperature_c=mpc_input.outdoor_temp_c,
                flow_temperature_c=mpc_input.flow_temp_c,
                applied_heating_power_w=applied_heating_power_w,
                radiator_power_w=radiator_power_w,
                disturbance_w=disturbance_w,
                target_temperature_c=mpc_input.target_temp_c,
                room_sensor_fallback=mpc_input.room_sensor_fallback,
                valve_position_missing=mpc_input.valve_position_missing,
                trv_adapting=mpc_input.trv_adapting,
                radiator_not_heating=mpc_input.radiator_not_heating,
                hot_water_charge=mpc_input.hot_water_charge,
                valve_saturated=self._valve_saturated(mpc_input),
                window_open=self._window_open,
            )
        )
        self._cleanup_history(mpc_input.now_ts)

    def _track_degraded_inputs(self, mpc_input: RoomMpcInput) -> None:
        # The TRVs sit at the radiator and read up to a kelvin off the room
        # sensor, and without a reported valve opening the radiator heat is a
        # guess, and an adapting TRV moves its valve by itself, so samples
        # taken then are invalid, as with an open window: the stretch up to
        # the last good sample is still balanced, the next one starts once the
        # inputs are back.
        degraded = (
            mpc_input.room_sensor_fallback
            or mpc_input.valve_position_missing
            or mpc_input.trv_adapting
            or mpc_input.radiator_not_heating
        )
        was_degraded = (
            self._room_sensor_fallback
            or self._valve_position_missing
            or self._trv_adapting
            or self._radiator_not_heating
        )
        if degraded and not was_degraded and self._history:
            end_ts = self._history[-1].timestamp
            if (
                mpc_input.radiator_not_heating
                and mpc_input.radiator_not_heating_since_ts is not None
            ):
                # The radiator is judged an hour after its valve opened; that
                # hour's heat is in doubt as well.
                end_ts = min(end_ts, mpc_input.radiator_not_heating_since_ts)
            self._close_ua_balance(end_ts)
        if was_degraded and not degraded:
            self._ua_blocked_until_ts = max(
                self._ua_blocked_until_ts, mpc_input.now_ts
            )
        self._room_sensor_fallback = mpc_input.room_sensor_fallback
        self._valve_position_missing = mpc_input.valve_position_missing
        self._trv_adapting = mpc_input.trv_adapting
        self._radiator_not_heating = mpc_input.radiator_not_heating

    def _cleanup_history(self, now_ts: float) -> None:
        self._history = [
            entry
            for entry in self._history
            if now_ts - entry.timestamp <= HISTORY_RETENTION_S
        ]
        self._ua_balance_bridged = [
            (start_ts, end_ts)
            for start_ts, end_ts in self._ua_balance_bridged
            if now_ts - end_ts <= HISTORY_RETENTION_S
        ]

    def set_prediction(self, prediction: LearnerPrediction) -> None:
        self._last_prediction = prediction

    def get_learning_state(self) -> RoomModelLearningState:
        return self._learning_state

    def get_ua_balance_info(self) -> UaBalanceInfo:
        return self._ua_balance_info

    def get_capacity_outcome(self) -> CapacityOutcome:
        return self._capacity_outcome

    def run_learning_cycle(self) -> None:
        if not self._enabled:
            self._capacity_outcome = CapacityOutcome.DISABLED
            self._run_disabled_heat_balance()
            return

        self._capacity_suppression.mark_next_window(time.time())

        ua_learned = self._learn_ua_from_heat_balance()
        status = self._evaluate_capacity_window()
        if ua_learned:
            status = LearningStatus.LEARNED
        if status == LearningStatus.LEARNED:
            self._pending_persisted_factors = self._get_current_learning_factors()

        self._learning_state = self._create_learning_state(status)
        self._rotate_learning_window()

    def _evaluate_capacity_window(self) -> LearningStatus:
        status, outcome = self._capacity_window_result()
        self._capacity_outcome = outcome
        return status

    def _capacity_window_result(self) -> tuple[LearningStatus, CapacityOutcome]:
        if self._is_paused_window(self._active_prediction):
            return LearningStatus.PAUSED, CapacityOutcome.PAUSED

        if self._capacity_suppression.current_window_invalid:
            return LearningStatus.SUPPRESSED, CapacityOutcome.SUPPRESSED

        if self._active_prediction is None:
            return LearningStatus.WAITING_INTERVAL, CapacityOutcome.WAITING

        relevant_history = self._get_relevant_history(self._active_prediction)
        if len(relevant_history) < MIN_HISTORY_SAMPLES:
            return LearningStatus.WAITING_INTERVAL, CapacityOutcome.WAITING

        if any(entry.room_sensor_fallback for entry in relevant_history):
            return LearningStatus.SUPPRESSED, CapacityOutcome.ROOM_SENSOR_FALLBACK

        if any(entry.valve_position_missing for entry in relevant_history):
            return LearningStatus.SUPPRESSED, CapacityOutcome.VALVE_POSITION_MISSING

        if any(entry.trv_adapting for entry in relevant_history):
            return LearningStatus.SUPPRESSED, CapacityOutcome.TRV_ADAPTING

        if any(entry.radiator_not_heating for entry in relevant_history):
            return LearningStatus.SUPPRESSED, CapacityOutcome.RADIATOR_NOT_HEATING

        if any(entry.window_open for entry in relevant_history):
            return LearningStatus.SUPPRESSED, CapacityOutcome.SUPPRESSED

        if not self._is_history_valid(relevant_history):
            return LearningStatus.DISTURBED, CapacityOutcome.DISTURBED

        if self._is_stationary(relevant_history):
            # UA comes from the heat balance; a window that barely moved says
            # nothing about capacity.
            return LearningStatus.NO_CORRECTION, CapacityOutcome.STATIONARY

        if not self._is_sun_free_window(self._active_prediction):
            return LearningStatus.OUTSIDE_WINDOW, CapacityOutcome.OUTSIDE_WINDOW

        # Replay the window with the power the radiators gave off, not the plan
        # the prediction was made with: the controller rarely applies the
        # planned demand for the whole window, and the radiators keep heating
        # after the valve closed.
        simulated_temperatures_c = self._simulate_room_temperatures_c(
            relevant_history
        )
        simulation_error_c = self._mean_of_last_samples(
            [entry.room_temperature_c for entry in relevant_history]
        ) - self._mean_of_last_samples(simulated_temperatures_c)
        if abs(simulation_error_c) < MIN_ROOM_DELTA_C:
            return LearningStatus.NO_CORRECTION, CapacityOutcome.SMALL_ERROR

        simulated_change_c = (
            simulated_temperatures_c[-1] - relevant_history[0].room_temperature_c
        )
        if abs(simulated_change_c) < MIN_SIMULATED_CHANGE_C:
            return LearningStatus.NO_CORRECTION, CapacityOutcome.SMALL_CHANGE

        self._learn_capacity_factor(simulation_error_c, simulated_change_c)
        return LearningStatus.LEARNED, CapacityOutcome.LEARNED

    def _simulate_room_temperatures_c(
        self, history: list[LearnerHistoryEntry]
    ) -> list[float]:
        room_temperature_c = history[0].room_temperature_c
        simulated = [room_temperature_c]
        for previous, current in zip(history, history[1:]):
            net_heating_power_w = (
                _interval_radiator_power_w(previous, current)
                + previous.disturbance_w
                - self._loss_model.calculate_heat_loss_w(
                    room_temperature_c, previous.outdoor_temperature_c
                )
            )
            room_temperature_c += self._capacity_model.predict_temperature_change_c(
                net_heating_power_w, current.timestamp - previous.timestamp
            )
            simulated.append(room_temperature_c)
        return simulated

    def _run_disabled_heat_balance(self) -> None:
        # Heating may stay off past the end of the night; the stretch before
        # it is still worth balancing.
        if (
            self._disabled_since_ts is not None
            and self._history
            and self._history[-1].timestamp - self._disabled_since_ts
            > UA_BALANCE_MAX_INTERRUPTION_S
        ):
            self._close_ua_balance(self._disabled_since_ts)
        if self._ua_balance_closed is None:
            return
        if self._learn_ua_from_heat_balance():
            self._pending_persisted_factors = self._get_current_learning_factors()
            self._learning_state = self._create_learning_state(LearningStatus.DISABLED)

    def _ua_balance_start_ts(self) -> float:
        return max(
            self._ua_balance_from_ts,
            self._learning_window_active_since_ts,
            self._learning_resumed_ts,
            self._ua_blocked_until_ts,
            self._history[0].timestamp if self._history else 0.0,
        )

    def _close_ua_balance(self, end_ts: float) -> None:
        """End the running heat-balance stretch; the next cycle balances it."""
        if self._ua_balance_closed is not None:
            # Heating went off before the learning window closed, and only
            # turned out to stay off for too long afterwards.
            start_ts, closed_end_ts = self._ua_balance_closed
            if start_ts < end_ts < closed_end_ts:
                self._ua_balance_closed = (start_ts, end_ts)
            return
        if (
            not self._history
            or self._learning_paused
            or not self._learning_window_active
            or self._room_sensor_fallback
            or self._valve_position_missing
            or self._trv_adapting
            or self._radiator_not_heating
            or self._window_open
        ):
            return
        start_ts = self._ua_balance_start_ts()
        if end_ts <= start_ts or not self._has_sun_free_sample(start_ts, end_ts):
            # Nothing to balance: the stretch lies after sunrise, say, when
            # the night mode ends later.
            return
        self._ua_balance_closed = (start_ts, end_ts)
        self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)

    def _learn_ua_from_heat_balance(self) -> bool:
        outcome = self._evaluate_heat_balance()
        self._ua_balance_info.outcome = outcome
        return outcome in (
            UaBalanceOutcome.LEARNED,
            UaBalanceOutcome.LEARNED_AT_LIMIT,
        )

    def _evaluate_heat_balance(self) -> UaBalanceOutcome:
        info = self._ua_balance_info
        info.collecting_since_ts = None
        if self._ua_balance_closed is not None:
            start_ts, end_ts = self._ua_balance_closed
            self._ua_balance_closed = None
            return self._balance_stretch(start_ts, end_ts)
        if self._learning_paused:
            return UaBalanceOutcome.PAUSED
        if self._room_sensor_fallback:
            return UaBalanceOutcome.ROOM_SENSOR_FALLBACK
        if self._valve_position_missing:
            return UaBalanceOutcome.VALVE_POSITION_MISSING
        if self._trv_adapting:
            return UaBalanceOutcome.TRV_ADAPTING
        if self._radiator_not_heating:
            return UaBalanceOutcome.RADIATOR_NOT_HEATING
        if self._window_open:
            return UaBalanceOutcome.WINDOW_OPEN
        if not self._learning_window_active:
            return UaBalanceOutcome.OUTSIDE_WINDOW
        if not self._history:
            return UaBalanceOutcome.COLLECTING

        # Checked before the room has to settle: after sunrise a room warming
        # up is no coasting room.
        if not self._has_sun_free_sample(
            self._ua_balance_start_ts(), self._history[-1].timestamp
        ):
            return UaBalanceOutcome.OUTSIDE_WINDOW
        start_ts = self._settled_since_ts(self._ua_balance_start_ts())
        if start_ts is None:
            return UaBalanceOutcome.COASTING
        end_ts = self._history[-1].timestamp
        sun_free_ts = [
            entry.timestamp
            for entry in self._history
            if entry.timestamp >= start_ts and self._sun_condition(entry.timestamp)
        ]
        if not sun_free_ts:
            return UaBalanceOutcome.OUTSIDE_WINDOW
        if not self._sun_condition(end_ts):
            # The stretch ran into sunrise.
            self._ua_balance_from_ts = end_ts
            return self._balance_stretch(sun_free_ts[0], sun_free_ts[-1])

        if end_ts - sun_free_ts[0] < UA_BALANCE_MAX_WINDOW_S:
            info.collecting_since_ts = sun_free_ts[0]
            return UaBalanceOutcome.COLLECTING
        self._ua_balance_from_ts = end_ts
        return self._balance_stretch(sun_free_ts[0], end_ts)

    def _balance_stretch(
        self, start_ts: float, end_ts: float, retried: bool = False
    ) -> UaBalanceOutcome:
        info = self._ua_balance_info
        if self._loss_model.base_ua_w_per_k <= 0:
            return UaBalanceOutcome.COLLECTING

        settled_ts = self._settled_since_ts(start_ts)
        if settled_ts is None or settled_ts > end_ts:
            return UaBalanceOutcome.COASTING
        start_ts = settled_ts

        window = [
            entry
            for entry in self._history
            if start_ts <= entry.timestamp <= end_ts
            and self._sun_condition(entry.timestamp)
        ]
        if (
            len(window) < MIN_HISTORY_SAMPLES
            or window[-1].timestamp - window[0].timestamp < UA_BALANCE_MIN_WINDOW_S
        ):
            return UaBalanceOutcome.TOO_SHORT
        if any(entry.room_sensor_fallback for entry in window):
            return UaBalanceOutcome.ROOM_SENSOR_FALLBACK
        if any(entry.valve_position_missing for entry in window):
            return UaBalanceOutcome.VALVE_POSITION_MISSING
        if any(entry.trv_adapting for entry in window):
            return UaBalanceOutcome.TRV_ADAPTING
        if any(entry.radiator_not_heating for entry in window):
            return UaBalanceOutcome.RADIATOR_NOT_HEATING
        if any(entry.window_open for entry in window):
            return UaBalanceOutcome.WINDOW_OPEN
        end_ts = window[-1].timestamp

        heat_j = 0.0
        temperature_difference_ks = 0.0
        saturated_s = 0.0
        for previous, current in zip(window, window[1:]):
            duration_s = current.timestamp - previous.timestamp
            if duration_s > UA_BALANCE_MAX_GAP_S:
                return UaBalanceOutcome.GAP
            if previous.valve_saturated:
                saturated_s += duration_s
            heat_j += (
                self._balance_heating_power_w(previous, current) * duration_s
            )
            temperature_difference_ks += (
                previous.room_temperature_c - previous.outdoor_temperature_c
            ) * duration_s

        window_duration_s = end_ts - window[0].timestamp
        if temperature_difference_ks / window_duration_s < UA_BALANCE_MIN_MEAN_DELTA_C:
            return UaBalanceOutcome.SMALL_TEMPERATURE_DIFFERENCE

        drift_c = self._trend_drift_c(window)
        if abs(drift_c) > UA_BALANCE_MAX_DRIFT_C and not retried:
            # A room found holding still above or below its setpoint was
            # sometimes only coasting slower than its sensor steps and then
            # moved on. The same night counts from where it came near its
            # setpoint, if enough of it is left.
            near_ts = next(
                (entry.timestamp for entry in window if self._near_setpoint(entry)),
                None,
            )
            if (
                not self._near_setpoint(window[0])
                and near_ts is not None
                and end_ts - near_ts >= UA_BALANCE_MIN_WINDOW_S
            ):
                return self._balance_stretch(near_ts, end_ts, retried=True)
        if abs(drift_c) > UA_BALANCE_MAX_DRIFT_C:
            # Kept for diagnostics, without a target: none was computed.
            info.last_target_factor = None
            info.last_drift_c = drift_c
            return UaBalanceOutcome.DRIFT

        stored_heat_j = self._capacity_model.effective_capacity_j_per_k * drift_c
        target_ua_w_per_k = (heat_j - stored_heat_j) / temperature_difference_ks
        target_factor = target_ua_w_per_k / self._loss_model.base_ua_w_per_k

        rejection = None
        if saturated_s > UA_BALANCE_MAX_SATURATED_SHARE * window_duration_s:
            rejection = UaBalanceOutcome.VALVE_SATURATED
        elif abs(stored_heat_j) > UA_BALANCE_MAX_STORAGE_SHARE * heat_j:
            rejection = UaBalanceOutcome.STORAGE_DOMINATED
        elif target_factor > UA_BALANCE_MAX_TARGET_FACTOR:
            rejection = UaBalanceOutcome.IMPLAUSIBLE_TARGET
        if rejection is not None:
            # Kept for diagnostics; the factor and the last learned time stay.
            self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)
            info.last_target_factor = target_factor
            info.last_drift_c = drift_c
            return rejection

        learned_target_factor = max(target_factor, UA_BALANCE_MIN_TARGET_FACTOR)
        current_factor = self._loss_model.learned_ua_factor
        self._loss_model.learned_ua_factor = current_factor + UA_BALANCE_WEIGHT * (
            learned_target_factor - current_factor
        )
        # The balance leaves the disturbance out, so the target takes up the
        # heat from neighbours; the observer's disturbance held the same heat
        # against the old factor. Handing over what the factor change now
        # explains keeps the model's net loss of this stretch unchanged.
        self._pending_disturbance_transfer_w += (
            (current_factor - self._loss_model.learned_ua_factor)
            * self._loss_model.base_ua_w_per_k
            * temperature_difference_ks
            / window_duration_s
        )
        self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)
        info.last_learned_ts = end_ts
        info.last_target_factor = target_factor
        info.last_drift_c = drift_c
        info.last_window_s = window_duration_s
        if target_factor < UA_BALANCE_MIN_TARGET_FACTOR:
            return UaBalanceOutcome.LEARNED_AT_LIMIT
        return UaBalanceOutcome.LEARNED

    def _has_sun_free_sample(self, start_ts: float, end_ts: float) -> bool:
        return any(
            start_ts <= entry.timestamp <= end_ts
            and self._sun_condition(entry.timestamp)
            for entry in self._history
        )

    def _settled_since_ts(self, start_ts: float) -> float | None:
        """First sample from start_ts on where the room has settled.

        Settled is near the setpoint -- within the margin around the band the
        controller aims for -- or holding its temperature wherever it is. A
        room coasting down from above or recovering from below would book its
        stored heat as a drift. A room found holding still counts from the
        start of the hour it held still: that hour stored next to nothing.
        """
        for index, entry in enumerate(self._history):
            if entry.timestamp < start_ts:
                continue
            if self._near_setpoint(entry):
                return entry.timestamp
            stationary_since_ts = self._stationary_since_ts(index, start_ts)
            if stationary_since_ts is not None:
                return stationary_since_ts
        return None

    def _near_setpoint(self, entry: LearnerHistoryEntry) -> bool:
        """Within the margin around the band the controller aims for."""
        return entry.target_temperature_c is None or (
            entry.target_temperature_c
            - self._target_band_c
            - UA_BALANCE_SETTLED_MARGIN_C
            <= entry.room_temperature_c
            <= entry.target_temperature_c + UA_BALANCE_SETTLED_MARGIN_C
        )

    def _valve_saturated(self, mpc_input: RoomMpcInput) -> bool:
        openings_pct = [
            opening for opening in mpc_input.valve_positions_pct if opening is not None
        ]
        return (
            bool(openings_pct)
            and min(openings_pct) >= VALVE_SATURATED_OPENING_PCT
            and mpc_input.room_temp_c
            < mpc_input.target_temp_c - self._target_band_c
        )

    def _stationary_since_ts(self, index: int, start_ts: float) -> float | None:
        """Start of the hour up to a sample over which the room held still.

        Only samples from start_ts on count, so a stretch never reaches back
        before its own start. None while the room still moves.
        """
        end_ts = self._history[index].timestamp
        window = [
            entry
            for entry in self._history[: index + 1]
            if entry.timestamp >= max(start_ts, end_ts - UA_BALANCE_STATIONARY_WINDOW_S)
        ]
        if (
            len(window) < MIN_HISTORY_SAMPLES
            or end_ts - window[0].timestamp < UA_BALANCE_STATIONARY_WINDOW_S * 0.9
        ):
            return None
        slope_c_per_h = (
            self._trend_drift_c(window) / (end_ts - window[0].timestamp) * 3600
        )
        if abs(slope_c_per_h) >= UA_BALANCE_STATIONARY_SLOPE_C_PER_H:
            return None
        return window[0].timestamp

    def _balance_heating_power_w(
        self, entry: LearnerHistoryEntry, next_entry: LearnerHistoryEntry
    ) -> float:
        # Mean of both ends of the interval, see _interval_radiator_power_w.
        return (
            self._balance_point_power_w(entry)
            + self._balance_point_power_w(next_entry)
        ) / 2

    def _balance_point_power_w(self, entry: LearnerHistoryEntry) -> float:
        # During a domestic hot-water charge the radiators get no water at
        # all, however slowly the observer lets them cool.
        if entry.hot_water_charge:
            return 0.0
        # While heating was unavailable the TRVs were closed, whatever the
        # controller kept requesting.
        if any(
            start_ts <= entry.timestamp < end_ts
            for start_ts, end_ts in self._ua_balance_bridged
        ):
            return 0.0
        return entry.radiator_power_w

    @staticmethod
    def _trend_drift_c(window: list[LearnerHistoryEntry]) -> float:
        """Room temperature change over the stretch from a fitted straight line.

        Samples are weighted by the time they stand for (half the interval to
        each neighbour): the history gains a sample on every sensor change, so
        a pulse that moves the sensor often would otherwise count more than a
        quiet hour.
        """
        times = [entry.timestamp for entry in window]
        weights = [
            (times[min(index + 1, len(times) - 1)] - times[max(index - 1, 0)]) / 2
            for index in range(len(times))
        ]
        total_weight = sum(weights)
        if total_weight <= 0:
            return 0.0
        temperatures = [entry.room_temperature_c for entry in window]
        mean_time = sum(w * t for w, t in zip(weights, times)) / total_weight
        mean_temperature = (
            sum(w * c for w, c in zip(weights, temperatures)) / total_weight
        )
        spread = sum(w * (t - mean_time) ** 2 for w, t in zip(weights, times))
        if spread <= 0:
            return 0.0
        slope = (
            sum(
                w * (t - mean_time) * (c - mean_temperature)
                for w, t, c in zip(weights, times, temperatures)
            )
            / spread
        )
        return slope * (times[-1] - times[0])

    def _get_relevant_history(
        self, prediction: LearnerPrediction
    ) -> list[LearnerHistoryEntry]:
        prediction_end_ts = prediction.timestamp + prediction.prediction_horizon_s
        return [
            entry
            for entry in self._history
            if prediction.timestamp <= entry.timestamp <= prediction_end_ts
        ]

    @staticmethod
    def _is_history_valid(history: list[LearnerHistoryEntry]) -> bool:
        outdoor_temperatures = [entry.outdoor_temperature_c for entry in history]
        outdoor_delta_c = max(outdoor_temperatures) - min(outdoor_temperatures)
        if outdoor_delta_c > MAX_OUTDOOR_DELTA_C:
            return False

        flow_temperatures = [
            entry.flow_temperature_c
            for entry in history
            if entry.flow_temperature_c is not None
        ]
        if flow_temperatures:
            flow_delta_c = max(flow_temperatures) - min(flow_temperatures)
            if flow_delta_c > MAX_FLOW_DELTA_C:
                return False

        return True

    @staticmethod
    def _mean_of_last_samples(values: list[float]) -> float:
        last_samples = values[-5:]
        return sum(last_samples) / len(last_samples)

    def _is_stationary(self, history: list[LearnerHistoryEntry]) -> bool:
        room_temperatures = [entry.room_temperature_c for entry in history]
        return max(room_temperatures) - min(room_temperatures) < self._stationary_range_c

    def _is_paused_window(self, prediction: LearnerPrediction | None) -> bool:
        if self._learning_paused:
            return True
        return (
            prediction is not None
            and prediction.timestamp < self._learning_resumed_ts
        )

    def _is_sun_free_window(self, prediction: LearnerPrediction) -> bool:
        start_ts = prediction.timestamp
        end_ts = start_ts + prediction.prediction_horizon_s
        return self._sun_condition(start_ts) and self._sun_condition(end_ts)

    def _learn_capacity_factor(
        self, simulation_error_c: float, simulated_change_c: float
    ) -> None:
        # A room that overshoots a simulated rise is lighter than modelled; one
        # that falls less than a simulated drop is heavier. The error alone
        # cannot tell these apart, the direction of the simulation can.
        direction = 1.0 if simulated_change_c > 0 else -1.0
        self._capacity_model.learned_capacity_factor = (
            self._capacity_model.learned_capacity_factor
            * (1 - simulation_error_c * direction * CAPACITY_LEARNING_RATE)
        )

    def _rotate_learning_window(self) -> None:
        self._capacity_suppression.rotate()
        self._active_prediction = self._last_prediction
        self._last_prediction = None

    def recalibrate(self, factors: LearningFactors) -> None:
        self._pending_persisted_factors = None
        self._pending_disturbance_transfer_w = 0.0
        self._active_prediction = None
        self._last_prediction = None
        self._ua_balance_closed = None
        if self._history:
            self._ua_balance_from_ts = self._history[-1].timestamp
        self._loss_model.learned_ua_factor = factors.ua_factor
        self._capacity_model.learned_capacity_factor = factors.capacity_factor
        self._learning_state = self._create_learning_state(self._learning_state.status)

    def consume_disturbance_transfer_w(self) -> float:
        """Disturbance the learned factor took over since the last call."""
        transfer_w = self._pending_disturbance_transfer_w
        self._pending_disturbance_transfer_w = 0.0
        return transfer_w

    def consume_persisted_learning_factors(self) -> LearningFactors | None:
        factors = self._pending_persisted_factors
        self._pending_persisted_factors = None
        return factors

    def _create_learning_state(self, status: LearningStatus) -> RoomModelLearningState:
        return RoomModelLearningState(
            status=status, learned_factors=self._get_current_learning_factors()
        )

    def _get_current_learning_factors(self) -> LearningFactors:
        return LearningFactors(
            ua_factor=self._loss_model.learned_ua_factor,
            capacity_factor=self._capacity_model.learned_capacity_factor,
        )


def _interval_radiator_power_w(
    entry: LearnerHistoryEntry, next_entry: LearnerHistoryEntry
) -> float:
    # The observer's estimate at the next sample already followed the lag
    # over the interval, so the mean of both ends is close to what the
    # radiators gave off in between.
    return (entry.radiator_power_w + next_entry.radiator_power_w) / 2

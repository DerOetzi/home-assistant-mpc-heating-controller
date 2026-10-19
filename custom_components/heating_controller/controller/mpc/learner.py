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
# closes, learning is paused, a window contact opens, heating stays off for
# longer than a short interruption, or the sun condition ends, and is balanced
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
UA_BALANCE_EDGE_S = 30 * 60
UA_BALANCE_MAX_GAP_S = 20 * 60
# The stored heat relies on the (uncertain) capacity, so only windows where the
# room ends close to where it started are used.
UA_BALANCE_MAX_DRIFT_C = 0.3
# In mild nights little heat is requested, and heat from neighbouring rooms or
# appliances weighs too much in the balance.
UA_BALANCE_MIN_MEAN_DELTA_C = 8.0
# Even within the drift limit the stored heat must stay a minor part of the
# balance; otherwise the result depends on the capacity more than on the
# losses.
UA_BALANCE_MAX_STORAGE_SHARE = 0.5
# A target near zero or negative is a measurement artefact, not a signal:
# the stretch is discarded instead of pulling the factor towards it.
UA_BALANCE_MIN_TARGET_FACTOR = 0.15
UA_BALANCE_MAX_TARGET_FACTOR = 2.0
UA_BALANCE_WEIGHT = 0.3


@dataclass
class LearnerHistoryEntry:
    timestamp: float
    room_temperature_c: float
    outdoor_temperature_c: float
    applied_heating_power_w: float
    flow_temperature_c: float | None = None


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
        self, loss_model: RoomLossModel, capacity_model: ThermalCapacityModel
    ) -> None:
        self._loss_model = loss_model
        self._capacity_model = capacity_model

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
        self._sun_condition: callable[[float], bool] = lambda _ts: True
        self._pending_persisted_factors: LearningFactors | None = None

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

    def set_sun_condition(self, sun_condition: callable[[float], bool]) -> None:
        self._sun_condition = sun_condition

    def append_history(self, mpc_input: RoomMpcInput, applied_heating_power_w: float) -> None:
        self._history.append(
            LearnerHistoryEntry(
                timestamp=mpc_input.now_ts,
                room_temperature_c=mpc_input.room_temp_c,
                outdoor_temperature_c=mpc_input.outdoor_temp_c,
                flow_temperature_c=mpc_input.flow_temp_c,
                applied_heating_power_w=applied_heating_power_w,
            )
        )
        self._cleanup_history(mpc_input.now_ts)

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

        if not self._is_history_valid(relevant_history):
            return LearningStatus.DISTURBED, CapacityOutcome.DISTURBED

        if self._is_stationary(relevant_history):
            # UA comes from the heat balance; a window that barely moved says
            # nothing about capacity.
            return LearningStatus.NO_CORRECTION, CapacityOutcome.STATIONARY

        if not self._is_sun_free_window(self._active_prediction):
            return LearningStatus.OUTSIDE_WINDOW, CapacityOutcome.OUTSIDE_WINDOW

        # Replay the window with the power that was actually requested, not the
        # plan the prediction was made with: the controller rarely applies the
        # planned demand for the whole window.
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
                previous.applied_heating_power_w
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
        ):
            return
        start_ts = self._ua_balance_start_ts()
        if end_ts <= start_ts:
            return
        self._ua_balance_closed = (start_ts, end_ts)
        self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)

    def _learn_ua_from_heat_balance(self) -> bool:
        outcome = self._evaluate_heat_balance()
        self._ua_balance_info.outcome = outcome
        return outcome == UaBalanceOutcome.LEARNED

    def _evaluate_heat_balance(self) -> UaBalanceOutcome:
        info = self._ua_balance_info
        info.collecting_since_ts = None
        if self._ua_balance_closed is not None:
            start_ts, end_ts = self._ua_balance_closed
            self._ua_balance_closed = None
            return self._balance_stretch(start_ts, end_ts)
        if self._learning_paused:
            return UaBalanceOutcome.PAUSED
        if not self._learning_window_active:
            return UaBalanceOutcome.OUTSIDE_WINDOW
        if not self._history:
            return UaBalanceOutcome.COLLECTING

        start_ts = self._ua_balance_start_ts()
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

    def _balance_stretch(self, start_ts: float, end_ts: float) -> UaBalanceOutcome:
        info = self._ua_balance_info
        if self._loss_model.base_ua_w_per_k <= 0:
            return UaBalanceOutcome.COLLECTING

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
        end_ts = window[-1].timestamp

        flow_temperatures = sorted(
            entry.flow_temperature_c
            for entry in window
            if entry.flow_temperature_c is not None
        )
        median_flow_c = (
            flow_temperatures[len(flow_temperatures) // 2] if flow_temperatures else None
        )

        heat_j = 0.0
        temperature_difference_ks = 0.0
        for previous, current in zip(window, window[1:]):
            duration_s = current.timestamp - previous.timestamp
            if duration_s > UA_BALANCE_MAX_GAP_S:
                return UaBalanceOutcome.GAP
            heat_j += self._balance_heating_power_w(previous, median_flow_c) * duration_s
            temperature_difference_ks += (
                previous.room_temperature_c - previous.outdoor_temperature_c
            ) * duration_s

        window_duration_s = end_ts - window[0].timestamp
        if temperature_difference_ks / window_duration_s < UA_BALANCE_MIN_MEAN_DELTA_C:
            return UaBalanceOutcome.SMALL_TEMPERATURE_DIFFERENCE

        start_temperature_c = self._mean_room_temperature_c(
            window, window[0].timestamp, window[0].timestamp + UA_BALANCE_EDGE_S
        )
        end_temperature_c = self._mean_room_temperature_c(
            window, end_ts - UA_BALANCE_EDGE_S, end_ts
        )
        drift_c = end_temperature_c - start_temperature_c
        if abs(drift_c) > UA_BALANCE_MAX_DRIFT_C:
            return UaBalanceOutcome.DRIFT

        stored_heat_j = self._capacity_model.effective_capacity_j_per_k * drift_c
        target_ua_w_per_k = (heat_j - stored_heat_j) / temperature_difference_ks
        target_factor = target_ua_w_per_k / self._loss_model.base_ua_w_per_k

        rejection = None
        if abs(stored_heat_j) > UA_BALANCE_MAX_STORAGE_SHARE * heat_j:
            rejection = UaBalanceOutcome.STORAGE_DOMINATED
        elif not (
            UA_BALANCE_MIN_TARGET_FACTOR
            <= target_factor
            <= UA_BALANCE_MAX_TARGET_FACTOR
        ):
            rejection = UaBalanceOutcome.IMPLAUSIBLE_TARGET
        if rejection is not None:
            # Kept for diagnostics; the factor and the last learned time stay.
            self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)
            info.last_target_factor = target_factor
            info.last_drift_c = drift_c
            return rejection

        current_factor = self._loss_model.learned_ua_factor
        self._loss_model.learned_ua_factor = current_factor + UA_BALANCE_WEIGHT * (
            target_factor - current_factor
        )
        self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)
        info.last_learned_ts = end_ts
        info.last_target_factor = target_factor
        info.last_drift_c = drift_c
        info.last_window_s = window_duration_s
        return UaBalanceOutcome.LEARNED

    def _balance_heating_power_w(
        self, entry: LearnerHistoryEntry, median_flow_c: float | None
    ) -> float:
        # A domestic hot water charge shows as a flow spike on the shared
        # leaving-water sensor while the radiators get no water at all.
        if (
            median_flow_c is not None
            and entry.flow_temperature_c is not None
            and entry.flow_temperature_c > median_flow_c + MAX_FLOW_DELTA_C
        ):
            return 0.0
        # While heating was unavailable the TRVs were closed, whatever the
        # controller kept requesting.
        if any(
            start_ts <= entry.timestamp < end_ts
            for start_ts, end_ts in self._ua_balance_bridged
        ):
            return 0.0
        return entry.applied_heating_power_w

    @staticmethod
    def _mean_room_temperature_c(
        history: list[LearnerHistoryEntry], start_ts: float, end_ts: float
    ) -> float:
        temperatures = [
            entry.room_temperature_c
            for entry in history
            if start_ts <= entry.timestamp <= end_ts
        ]
        return sum(temperatures) / len(temperatures)

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
        self._active_prediction = None
        self._last_prediction = None
        self._ua_balance_closed = None
        if self._history:
            self._ua_balance_from_ts = self._history[-1].timestamp
        self._loss_model.learned_ua_factor = factors.ua_factor
        self._capacity_model.learned_capacity_factor = factors.capacity_factor
        self._learning_state = self._create_learning_state(self._learning_state.status)

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

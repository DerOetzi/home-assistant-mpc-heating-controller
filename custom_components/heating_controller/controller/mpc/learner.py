from __future__ import annotations

import time
from dataclasses import dataclass

from ...const import DEFAULT_STATIONARY_RANGE_C, LearningStatus
from .models.capacity import ThermalCapacityModel
from .models.loss import RoomLossModel
from .results import RoomMpcInput
from .types import LearningFactors, RoomModelLearningState

HISTORY_RETENTION_S = 180 * 60
MIN_HISTORY_SAMPLES = 5
MIN_ROOM_DELTA_C = 0.15
MAX_OUTDOOR_DELTA_C = 1.0
MAX_FLOW_DELTA_C = 5.0
UA_LEARNING_RATE = 0.0025
CAPACITY_LEARNING_RATE = 0.005


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
        self._ua_suppression = _FactorSuppression()
        self._capacity_suppression = _FactorSuppression()
        self._stationary_range_c = DEFAULT_STATIONARY_RANGE_C
        self._learning_window_active = True
        self._learning_window_active_since_ts = 0.0
        self._sun_condition: callable[[float], bool] = lambda _ts: True
        self._pending_persisted_factors: LearningFactors | None = None

        self._learning_state = self._create_learning_state(LearningStatus.DISABLED)

    def destroy(self) -> None:
        self.disable()

    def enable(self) -> None:
        if self._enabled:
            return
        self._active_prediction = None
        self._last_prediction = None
        self._pending_persisted_factors = None
        self._enabled = True
        self._learning_state = self._create_learning_state(
            LearningStatus.WAITING_INTERVAL
        )

    def disable(self) -> None:
        if self._enabled:
            self._enabled = False
            self._pending_persisted_factors = None
        self._learning_state = self._create_learning_state(LearningStatus.DISABLED)
        self._active_prediction = None
        self._last_prediction = None
        self._ua_suppression.reset()
        self._capacity_suppression.reset()

    def suppress_for_interval(
        self, ua_duration_s: float, capacity_duration_s: float
    ) -> None:
        now = time.time()
        self._ua_suppression.suppress(now + ua_duration_s)
        self._capacity_suppression.suppress(now + capacity_duration_s)
        self._pending_persisted_factors = None
        self._learning_state = self._create_learning_state(LearningStatus.SUPPRESSED)

    def set_stationary_range_c(self, stationary_range_c: float) -> None:
        self._stationary_range_c = stationary_range_c

    def set_learning_window_active(self, active: bool, now_ts: float) -> None:
        if active and not self._learning_window_active:
            self._learning_window_active_since_ts = now_ts
        self._learning_window_active = active

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

    def set_prediction(self, prediction: LearnerPrediction) -> None:
        self._last_prediction = prediction

    def get_learning_state(self) -> RoomModelLearningState:
        return self._learning_state

    def run_learning_cycle(self) -> None:
        if not self._enabled:
            return

        now = time.time()
        self._ua_suppression.mark_next_window(now)
        self._capacity_suppression.mark_next_window(now)

        if (
            self._ua_suppression.current_window_invalid
            and self._capacity_suppression.current_window_invalid
        ):
            self._learning_state = self._create_learning_state(
                LearningStatus.SUPPRESSED
            )
            self._rotate_learning_window()
            return

        if self._active_prediction is None:
            self._learning_state = self._create_learning_state(
                LearningStatus.WAITING_INTERVAL
            )
            self._rotate_learning_window()
            return

        relevant_history = self._get_relevant_history(self._active_prediction)

        if len(relevant_history) < MIN_HISTORY_SAMPLES:
            self._learning_state = self._create_learning_state(
                LearningStatus.WAITING_INTERVAL
            )
            self._rotate_learning_window()
            return

        if not self._is_history_valid(relevant_history):
            self._learning_state = self._create_learning_state(
                LearningStatus.DISTURBED
            )
            self._rotate_learning_window()
            return

        stationary = self._is_stationary(relevant_history)
        suppression = (
            self._ua_suppression if stationary else self._capacity_suppression
        )
        if suppression.current_window_invalid:
            self._learning_state = self._create_learning_state(
                LearningStatus.SUPPRESSED
            )
            self._rotate_learning_window()
            return

        if not self._is_inside_learning_window(self._active_prediction, stationary):
            self._learning_state = self._create_learning_state(
                LearningStatus.OUTSIDE_WINDOW
            )
            self._rotate_learning_window()
            return

        predicted_room_temperature_c = (
            self._active_prediction.predicted_room_temperature_c
        )
        actual_room_temperature_c = self._calculate_actual_room_temperature_c(
            relevant_history
        )
        prediction_error_c = actual_room_temperature_c - predicted_room_temperature_c

        if abs(prediction_error_c) < MIN_ROOM_DELTA_C:
            self._learning_state = self._create_learning_state(
                LearningStatus.NO_CORRECTION
            )
            self._rotate_learning_window()
            return

        if stationary:
            self._learn_ua_factor(prediction_error_c)
        else:
            self._learn_capacity_factor(prediction_error_c)

        self._pending_persisted_factors = self._get_current_learning_factors()
        self._learning_state = self._create_learning_state(LearningStatus.LEARNED)
        self._rotate_learning_window()

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
    def _calculate_actual_room_temperature_c(
        history: list[LearnerHistoryEntry],
    ) -> float:
        last_samples = history[-5:]
        return sum(entry.room_temperature_c for entry in last_samples) / len(
            last_samples
        )

    def _is_stationary(self, history: list[LearnerHistoryEntry]) -> bool:
        room_temperatures = [entry.room_temperature_c for entry in history]
        return max(room_temperatures) - min(room_temperatures) < self._stationary_range_c

    def _is_inside_learning_window(
        self, prediction: LearnerPrediction, stationary: bool
    ) -> bool:
        start_ts = prediction.timestamp
        end_ts = start_ts + prediction.prediction_horizon_s
        if not (self._sun_condition(start_ts) and self._sun_condition(end_ts)):
            return False
        if not stationary:
            return True
        return (
            self._learning_window_active
            and self._learning_window_active_since_ts <= start_ts
        )

    def _learn_ua_factor(self, prediction_error_c: float) -> None:
        self._loss_model.learned_ua_factor = self._loss_model.learned_ua_factor * (
            1 - prediction_error_c * UA_LEARNING_RATE
        )

    def _learn_capacity_factor(self, prediction_error_c: float) -> None:
        self._capacity_model.learned_capacity_factor = (
            self._capacity_model.learned_capacity_factor
            * (1 - prediction_error_c * CAPACITY_LEARNING_RATE)
        )

    def _rotate_learning_window(self) -> None:
        self._ua_suppression.rotate()
        self._capacity_suppression.rotate()
        self._active_prediction = self._last_prediction
        self._last_prediction = None

    def recalibrate(self, factors: LearningFactors) -> None:
        self._pending_persisted_factors = None
        self._active_prediction = None
        self._last_prediction = None
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

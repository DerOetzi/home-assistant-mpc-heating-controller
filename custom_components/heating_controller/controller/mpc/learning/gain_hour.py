"""Hourly gains: what each full clock hour leaves unexplained."""

from __future__ import annotations

from itertools import pairwise

from ..models.gains import HourOutcome, RoomGainModel
from .context import LearningContext
from .history import (
    UA_BALANCE_MAX_GAP_S,
    LearnerHistoryEntry,
    mean_outdoor_c,
    mean_room_c,
    trend_drift_c,
)

# Hourly gain learning: every full clock hour with samples near both edges
# (a restart within the hour leaves its start uncovered).
GAIN_HOUR_S = 3600
GAIN_HOUR_MIN_SAMPLES = 6
GAIN_HOUR_EDGE_S = 10 * 60


class GainHourLearner:
    def __init__(self, ctx: LearningContext) -> None:
        self._ctx = ctx
        self._done_ts: float | None = None

    def on_sample(self, now_ts: float) -> None:
        if self._ctx.gain_model is None or len(self._ctx.history) < 2:
            return
        hour_end_ts = (now_ts // GAIN_HOUR_S) * GAIN_HOUR_S
        hour_start_ts = hour_end_ts - GAIN_HOUR_S
        if self._ctx.history[-2].timestamp >= hour_end_ts:
            return
        if self._done_ts is not None and hour_start_ts < self._done_ts:
            return
        self._done_ts = hour_end_ts
        outcome = self._evaluate(hour_start_ts, hour_end_ts)
        self._ctx.gain_model.set_hour_outcome(outcome, hour_start_ts)

    def _evaluate(self, start_ts: float, end_ts: float) -> HourOutcome:
        gains = self._ctx.gain_model
        if gains is None:
            return HourOutcome.TOO_SHORT
        window = self._window(start_ts, end_ts)
        if window is None:
            return HourOutcome.TOO_SHORT
        rejection = _flag_outcome(window)
        if rejection is not None:
            return rejection
        if self._ctx.ua_blocked_until_ts > start_ts:
            # A window just closed, or the configuration changed.
            return HourOutcome.SUPPRESSED
        integrals = self._integrals(window)
        if integrals is None:
            return HourOutcome.GAP
        heat_j, room_ks, outdoor_ks = integrals
        mid_ts = (start_ts + end_ts) / 2
        sun_values = self._sun_values(gains, start_ts, end_ts)
        if sun_values is None:
            return HourOutcome.SUN_MISSING

        duration_s = window[-1].timestamp - window[0].timestamp
        slope_c_per_s = trend_drift_c(window) / duration_s
        # Base capacity, as in the night balance: over an hour the whole mass
        # stores heat, not only what a setpoint jump reaches.
        stored_w = self._ctx.capacity_model.base_capacity_j_per_k * slope_c_per_s
        loss_w = self._ctx.loss_model.effective_ua_w_per_k * (
            (room_ks - outdoor_ks) / duration_s
        )
        unexplained_w = stored_w - heat_j / duration_s + loss_w - gains.steady_gain_w
        gains.add_hour(
            self._ctx.profile_index_at(mid_ts),
            sun_values,
            unexplained_w,
            self._ctx.local_day_at(mid_ts),
        )
        return HourOutcome.LEARNED

    def _sun_values(
        self, gains: RoomGainModel, start_ts: float, end_ts: float
    ) -> list[float] | None:
        """Sun regressors at the middle of the hour; zeros if it was dark
        throughout, None if unknown."""
        sun_values = gains.sun_regressors(
            self._ctx.sun_source(), (start_ts + end_ts) / 2
        )
        if sun_values is not None:
            return sun_values
        if self._ctx.sun_condition(start_ts) and self._ctx.sun_condition(end_ts):
            return [0.0] * len(gains.sun_terms)
        return None

    def _window(
        self, start_ts: float, end_ts: float
    ) -> list[LearnerHistoryEntry] | None:
        """The hour's samples; None unless enough of them cover both edges."""
        window = [
            entry
            for entry in self._ctx.history
            if start_ts <= entry.timestamp <= end_ts
        ]
        if (
            len(window) < GAIN_HOUR_MIN_SAMPLES
            or window[0].timestamp > start_ts + GAIN_HOUR_EDGE_S
            or window[-1].timestamp < end_ts - GAIN_HOUR_EDGE_S
        ):
            return None
        return window

    def _integrals(
        self, window: list[LearnerHistoryEntry]
    ) -> tuple[float, float, float] | None:
        """Heat in J and room and outdoor temperature in K*s; None on a gap."""
        heat_j = 0.0
        room_ks = 0.0
        outdoor_ks = 0.0
        for previous, current in pairwise(window):
            duration_s = current.timestamp - previous.timestamp
            if duration_s > UA_BALANCE_MAX_GAP_S:
                return None
            heat_j += self._ctx.balance_heating_power_w(previous, current) * duration_s
            room_ks += mean_room_c(previous, current) * duration_s
            outdoor_ks += mean_outdoor_c(previous, current) * duration_s
        return heat_j, room_ks, outdoor_ks


_FLAG_OUTCOMES = (
    ("learning_paused", HourOutcome.PAUSED),
    ("window_open", HourOutcome.WINDOW_OPEN),
    ("room_sensor_fallback", HourOutcome.ROOM_SENSOR_FALLBACK),
    ("valve_position_missing", HourOutcome.VALVE_POSITION_MISSING),
    ("trv_adapting", HourOutcome.TRV_ADAPTING),
    ("radiator_not_heating", HourOutcome.RADIATOR_NOT_HEATING),
    ("valve_saturated", HourOutcome.VALVE_SATURATED),
)


def _flag_outcome(window: list[LearnerHistoryEntry]) -> HourOutcome | None:
    """The first flag set anywhere in the hour that rules it out."""
    for flag, outcome in _FLAG_OUTCOMES:
        if any(getattr(entry, flag) for entry in window):
            return outcome
    return None

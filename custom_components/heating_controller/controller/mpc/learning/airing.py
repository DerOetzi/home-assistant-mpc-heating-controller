"""Airing capacity: the room's recovery after a window was open."""

from __future__ import annotations

from bisect import bisect_left

from ....const import CapacityOutcome
from ..math_helper import clamp
from ..models.capacity import MAX_LEARNED_CAPACITY_FACTOR, MIN_LEARNED_CAPACITY_FACTOR
from ..types import CapacityStretchInfo
from .capacity_jump import (
    CAPACITY_JUMP_WEIGHT,
    JUMP_MAX_S,
    JUMP_SETTLED_C,
    JUMP_TARGET_CHANGE_C,
    VENT,
    estimate_stretch,
)
from .context import LearningContext
from .history import LearnerHistoryEntry, invalid_entry_outcome, trend_drift_c

# A second capacity is watched after airing: an open window cools mostly the
# air, which then warms back from walls and furniture faster than the room's
# mass would allow. The stretch starts once that rebound is over -- the room
# no longer warms faster than the model expects at the current heat, with the
# airing capacity -- and is balanced like a jump stretch. It only is
# observed: nothing uses it yet.
VENT_MIN_WINDOW_OPEN_S = 5 * 60
VENT_MIN_COOLING_C = 0.3
# The rebound is judged on the trend over this long before each sample.
VENT_REBOUND_WINDOW_S = 15 * 60
# Until the first airing stretch learns, the airing capacity is this share
# of the mass capacity.
VENT_START_SHARE = 0.3


class AiringLearner:
    def __init__(self, ctx: LearningContext) -> None:
        self._ctx = ctx
        self.info = CapacityStretchInfo()
        # None until a stretch learned it.
        self.learned_factor: float | None = None
        # Airings (opened, closed) not judged yet.
        self.pending: list[tuple[float, float]] = []

    @property
    def factor(self) -> float:
        """Airing capacity factor; until learned a share of the mass one."""
        if self.learned_factor is not None:
            return self.learned_factor
        return clamp(
            VENT_START_SHARE * self._ctx.capacity_model.learned_capacity_factor,
            MIN_LEARNED_CAPACITY_FACTOR,
            MAX_LEARNED_CAPACITY_FACTOR,
        )

    def evaluate(self) -> bool:
        """Judge every airing not judged yet; True if the factor moved."""
        learned = False
        while self.pending:
            opened_ts, closed_ts = self.pending[0]
            outcome = self._evaluate_airing(opened_ts, closed_ts)
            if outcome is None:
                return learned
            self.pending.pop(0)
            learned = learned or outcome == CapacityOutcome.LEARNED
        return learned

    def _evaluate_airing(
        self, opened_ts: float, closed_ts: float
    ) -> CapacityOutcome | None:
        """Judge the stretch after one airing; None while it is still running."""
        info = self.info
        history = self._ctx.history
        before = next(
            (entry for entry in reversed(history) if entry.timestamp <= opened_ts),
            None,
        )
        if closed_ts - opened_ts < VENT_MIN_WINDOW_OPEN_S:
            return self._end_airing(closed_ts, None, CapacityOutcome.TOO_SHORT)
        if before is None:
            return self._end_airing(closed_ts, None, CapacityOutcome.GAP)

        earliest_ts = max(closed_ts, self._ctx.capacity_blocked_until_ts)
        start = self._rebound_end_index(closed_ts, earliest_ts)
        if start is None:
            if history and history[-1].timestamp - closed_ts >= JUMP_MAX_S:
                # The room kept warming faster than its model the whole time.
                return self._end_airing(closed_ts, None, CapacityOutcome.IMPLAUSIBLE)
            self._reset_vent_info(closed_ts, None)
            info.outcome = CapacityOutcome.RUNNING
            return None
        if start < 0:
            # A flag ended the airing before the rebound was over.
            entry = history[-start - 1]
            outcome = invalid_entry_outcome(entry)
            assert outcome is not None
            return self._end_airing(entry.timestamp, None, outcome)

        coolest_c = min(
            entry.room_temperature_c
            for entry in history[: start + 1]
            if entry.timestamp >= opened_ts
        )
        if before.room_temperature_c - coolest_c < VENT_MIN_COOLING_C:
            return self._end_airing(
                history[start].timestamp, None, CapacityOutcome.TOO_SMALL
            )

        end = self._recovery_end_index(start)
        if end is None:
            self._reset_vent_info(history[start].timestamp, None)
            info.outcome = CapacityOutcome.RUNNING
            return None
        return self._learn_from_airing(history[start : end + 1], history[start])

    def _recovery_end_index(self, start: int) -> int | None:
        """Last sample of the recovery after an airing; None while it runs.

        It ends at its setpoint, after the longest jump, or just before a
        sample that is invalid or has another setpoint.
        """
        history = self._ctx.history
        target_c = history[start].target_temperature_c
        for index in range(start, len(history)):
            entry = history[index]
            if (
                invalid_entry_outcome(entry) is not None
                or target_c is None
                or entry.target_temperature_c is None
                or abs(entry.target_temperature_c - target_c) >= JUMP_TARGET_CHANGE_C
            ):
                return index - 1
            if (
                entry.room_temperature_c >= target_c - JUMP_SETTLED_C
                or entry.timestamp - history[start].timestamp >= JUMP_MAX_S
            ):
                return index
        return None

    def _learn_from_airing(
        self, stretch: list[LearnerHistoryEntry], first: LearnerHistoryEntry
    ) -> CapacityOutcome:
        info = self.info
        self._reset_vent_info(
            first.timestamp, stretch[-1].timestamp if stretch else None
        )
        rejection = estimate_stretch(self._ctx, VENT, stretch, info)
        if rejection is not None:
            info.outcome = rejection
            return rejection
        assert info.estimate_factor is not None
        current_factor = self.factor
        self.learned_factor = clamp(
            current_factor
            + CAPACITY_JUMP_WEIGHT * (info.estimate_factor - current_factor),
            MIN_LEARNED_CAPACITY_FACTOR,
            MAX_LEARNED_CAPACITY_FACTOR,
        )
        info.last_learned_ts = stretch[-1].timestamp
        info.outcome = CapacityOutcome.LEARNED
        return info.outcome

    def _rebound_end_index(self, closed_ts: float, earliest_ts: float) -> int | None:
        """First sample from earliest_ts on where the rebound is over.

        None while it still runs; -(index + 1) of a flagged sample that ends
        the airing first.
        """
        history = self._ctx.history
        ua_w_per_k = self._ctx.loss_model.effective_ua_w_per_k
        capacity_j_per_k = self._ctx.capacity_model.base_capacity_j_per_k * self.factor
        for index, entry in enumerate(history):
            if entry.timestamp <= closed_ts:
                continue
            if invalid_entry_outcome(entry) is not None:
                return -(index + 1)
            if entry.timestamp < earliest_ts:
                continue
            if (
                entry.target_temperature_c is not None
                and entry.room_temperature_c
                >= entry.target_temperature_c - JUMP_SETTLED_C
            ):
                # Back at the setpoint already: the stretch is too short.
                return index
            first = bisect_left(
                history,
                max(closed_ts, entry.timestamp - VENT_REBOUND_WINDOW_S),
                hi=index + 1,
                key=lambda sample: sample.timestamp,
            )
            window = history[first : index + 1]
            if (
                len(window) < 2
                or window[-1].timestamp - window[0].timestamp
                < VENT_REBOUND_WINDOW_S / 2
            ):
                continue
            duration_s = window[-1].timestamp - window[0].timestamp
            observed_c_per_s = trend_drift_c(window) / duration_s
            expected_c_per_s = (
                self._ctx.balance_point_power_w(entry)
                - ua_w_per_k * (entry.room_temperature_c - entry.outdoor_temperature_c)
            ) / capacity_j_per_k
            if observed_c_per_s <= expected_c_per_s:
                return index
        return None

    def _end_airing(
        self, start_ts: float, end_ts: float | None, outcome: CapacityOutcome
    ) -> CapacityOutcome:
        self._reset_vent_info(start_ts, end_ts)
        self.info.outcome = outcome
        return outcome

    def _reset_vent_info(self, start_ts: float, end_ts: float | None) -> None:
        info = self.info
        info.kind = VENT
        info.start_ts = start_ts
        info.end_ts = end_ts
        info.room_delta_c = None
        info.estimate_factor = None
        info.saturated_share = None

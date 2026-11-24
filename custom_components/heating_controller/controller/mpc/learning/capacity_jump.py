"""Capacity from setpoint jumps: the room's course after its mode's setpoint stepped."""

from __future__ import annotations

from itertools import pairwise

from ....const import CapacityOutcome
from ..types import CapacityStretchInfo
from .context import LearningContext
from .history import (
    UA_BALANCE_MAX_GAP_S,
    LearnerHistoryEntry,
    invalid_entry_outcome,
    mean_outdoor_c,
)

# Capacity is learned from jump stretches: the mode's setpoint steps and the
# room follows over tens of minutes. Held at its setpoint the room hardly
# moves, and its capacity does not show. Over the stretch the net heat --
# radiator heat minus losses -- divided by the room's change is the capacity
# it showed.
JUMP_MIN_TARGET_STEP_C = 0.5
# A later change of the mode's setpoint ends the stretch.
JUMP_TARGET_CHANGE_C = 0.1
# The stretch ends once the room is this close to its new setpoint.
JUMP_SETTLED_C = 0.2
JUMP_MAX_S = 3 * 3600
JUMP_MIN_S = 10 * 60
JUMP_MIN_ROOM_DELTA_C = 0.4
# A sensor with 0.1 K steps needs a few distinct values to show a course.
JUMP_MIN_DISTINCT_READINGS = 3
JUMP_MIN_ESTIMATE_FACTOR = 0.1
JUMP_MAX_ESTIMATE_FACTOR = 4.0
# Share of the way from the current factor to a stretch's estimate.
CAPACITY_JUMP_WEIGHT = 0.3
HEAT_UP = "heat_up"
COOL_DOWN = "cool_down"
VENT = "vent"


def estimate_stretch(
    ctx: LearningContext,
    kind: str,
    stretch: list[LearnerHistoryEntry],
    info: CapacityStretchInfo,
) -> CapacityOutcome | None:
    """Capacity factor the stretch showed, into info; a rejection or None."""
    if len(stretch) < 2 or stretch[-1].timestamp - stretch[0].timestamp < JUMP_MIN_S:
        return CapacityOutcome.TOO_SHORT
    if any(
        current.timestamp - previous.timestamp > UA_BALANCE_MAX_GAP_S
        for previous, current in pairwise(stretch)
    ):
        return CapacityOutcome.GAP
    room_delta_c = stretch[-1].room_temperature_c - stretch[0].room_temperature_c
    if (
        abs(room_delta_c) < JUMP_MIN_ROOM_DELTA_C
        or len({entry.room_temperature_c for entry in stretch})
        < JUMP_MIN_DISTINCT_READINGS
    ):
        return CapacityOutcome.TOO_SMALL
    if not ctx.sun_free(stretch[0].timestamp, stretch[-1].timestamp):
        return CapacityOutcome.OUTSIDE_WINDOW

    ua_w_per_k = ctx.loss_model.effective_ua_w_per_k
    net_j = 0.0
    saturated_s = 0.0
    for previous, current in pairwise(stretch):
        duration_s = current.timestamp - previous.timestamp
        mean_room_c = (previous.room_temperature_c + current.room_temperature_c) / 2
        net_j += (
            ctx.balance_heating_power_w(previous, current)
            - ua_w_per_k * (mean_room_c - mean_outdoor_c(previous, current))
        ) * duration_s
        if previous.valve_saturated:
            saturated_s += duration_s
    info.room_delta_c = room_delta_c
    # Heating up mostly runs with the valve fully open; the share is kept
    # for diagnostics rather than discarding those stretches, which would
    # leave almost nothing to learn from.
    info.saturated_share = saturated_s / (stretch[-1].timestamp - stretch[0].timestamp)
    if net_j == 0 or (net_j > 0) != (room_delta_c > 0):
        return CapacityOutcome.IMPLAUSIBLE
    estimate = (net_j / room_delta_c) / ctx.capacity_model.base_capacity_j_per_k
    info.estimate_factor = estimate
    if not JUMP_MIN_ESTIMATE_FACTOR <= estimate <= JUMP_MAX_ESTIMATE_FACTOR:
        return CapacityOutcome.IMPLAUSIBLE
    return None


class JumpLearner:
    def __init__(self, ctx: LearningContext) -> None:
        self._ctx = ctx
        self.info = CapacityStretchInfo()
        # Timestamp of the last setpoint jump already judged.
        self.done_ts = 0.0

    def evaluate(self) -> bool:
        """Judge every setpoint jump not judged yet; True if the factor moved."""
        learned = False
        while True:
            jump_index = self._next_jump_index()
            if jump_index is None:
                if self.info.outcome == CapacityOutcome.RUNNING:
                    # Its jump left the history before the stretch ended.
                    self.info.outcome = CapacityOutcome.GAP
                return learned
            outcome = self._evaluate_jump(jump_index)
            if outcome is None:
                return learned
            learned = learned or outcome == CapacityOutcome.LEARNED

    def _next_jump_index(self) -> int | None:
        history = self._ctx.history
        for index in range(1, len(history)):
            current = history[index]
            previous = history[index - 1]
            if (
                current.timestamp > self.done_ts
                and current.target_temperature_c is not None
                and previous.target_temperature_c is not None
                and abs(current.target_temperature_c - previous.target_temperature_c)
                >= JUMP_MIN_TARGET_STEP_C
            ):
                return index
        return None

    def _evaluate_jump(self, jump_index: int) -> CapacityOutcome | None:
        """Judge the stretch after one jump; None while it is still running."""
        history = self._ctx.history
        jump = history[jump_index]
        target_c = jump.target_temperature_c
        previous_target_c = history[jump_index - 1].target_temperature_c
        assert target_c is not None and previous_target_c is not None
        kind = HEAT_UP if target_c > previous_target_c else COOL_DOWN
        info = self.info

        start_ts = max(jump.timestamp, self._ctx.capacity_blocked_until_ts)
        start = next(
            (
                index
                for index in range(jump_index, len(history))
                if history[index].timestamp >= start_ts
            ),
            None,
        )
        if start is None:
            self._set_running(kind, start_ts)
            return None

        end: int | None = None
        for index in range(start, len(history)):
            entry = history[index]
            flag_outcome = invalid_entry_outcome(entry)
            if flag_outcome is not None:
                if index == start:
                    self.done_ts = jump.timestamp
                    self.info.begin(kind, entry.timestamp, None)
                    info.outcome = flag_outcome
                    return flag_outcome
                end = index - 1
                break
            if (
                entry.target_temperature_c is None
                or abs(entry.target_temperature_c - target_c) >= JUMP_TARGET_CHANGE_C
            ):
                end = index - 1
                break
            if (
                kind == HEAT_UP
                and entry.room_temperature_c >= target_c - JUMP_SETTLED_C
            ):
                end = index
                break
            if (
                kind == COOL_DOWN
                and entry.room_temperature_c <= target_c + JUMP_SETTLED_C
            ):
                end = index
                break
            if entry.timestamp - history[start].timestamp >= JUMP_MAX_S:
                end = index
                break
        if end is None:
            self._set_running(kind, history[start].timestamp)
            return None

        self.done_ts = jump.timestamp
        stretch = history[start : end + 1]
        self.info.begin(
            kind,
            history[start].timestamp,
            stretch[-1].timestamp if stretch else None,
        )
        info.outcome = self._judge_stretch(kind, stretch)
        return info.outcome

    def _set_running(self, kind: str, start_ts: float) -> None:
        self.info.begin(kind, start_ts, None)
        self.info.outcome = CapacityOutcome.RUNNING

    def _judge_stretch(
        self, kind: str, stretch: list[LearnerHistoryEntry]
    ) -> CapacityOutcome:
        info = self.info
        rejection = estimate_stretch(self._ctx, kind, stretch, info)
        if rejection is not None:
            return rejection
        assert info.estimate_factor is not None
        if kind == COOL_DOWN:
            # Cooling down leaves the gains, unknown here, in the balance;
            # the estimate is kept for comparison only.
            return CapacityOutcome.ESTIMATED
        current_factor = self._ctx.capacity_model.learned_capacity_factor
        self._ctx.capacity_model.learned_capacity_factor = current_factor + (
            CAPACITY_JUMP_WEIGHT * (info.estimate_factor - current_factor)
        )
        info.last_learned_ts = stretch[-1].timestamp
        return CapacityOutcome.LEARNED

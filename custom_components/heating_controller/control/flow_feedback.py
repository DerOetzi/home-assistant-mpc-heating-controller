"""Flow feedback: the measured valve demand of the flow lead rooms.

The feedforward (hold flow from the learned room model) says what the rooms
should need; the valves say what they do need. Valves near fully open with
the room below its target mean too little flow: one step up. Valves well
below open, or open only in rooms already above their target, for an hour
mean the flow can drop: one step down. A room that holds at its target
counts that hour back, only a room short of its target starts it over.
The sum of the
steps, feedback_k, is added to the feedforward. It only moves while what
was decided also reaches the heat source and the heat source heats.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

FEEDBACK_LIMIT_K = 5.0
# Up: valves this far open and the room short of its target, or saturated.
DEMAND_UP_PCT = 90.0
DEMAND_SATURATED_PCT = 98.0
UP_DEFICIT_C = 0.1
UP_SPACING_S = 30 * 60
# Down: no lead room holding the flow for this long, net of the time one
# held it at its target; a holding room this far short starts over.
DEMAND_DOWN_PCT = 80.0
DOWN_AFTER_S = 3600
SHORT_DEFICIT_C = UP_DEFICIT_C
# A room this far above its target holds the flow neither up nor down: its
# open valve comes from the TRV's own reading, more flow would not help it.
OVER_TARGET_C = 0.2
# A longer gap between two updates (restart, stalled loop) counts as this.
MAX_TICK_S = 300.0


class FeedbackFreeze(StrEnum):
    WRITE_OFF = "write_off"
    HEATING_OFF = "heating_off"
    UNREACHABLE = "unreachable"
    FALLBACK = "fallback"
    HOT_WATER = "hot_water"
    DEFROST = "defrost"
    BOOST = "boost"
    NOT_WRITTEN = "not_written"


@dataclass(frozen=True)
class DemandSample:
    """One flow lead room: its highest valve opening and how far it is short."""

    room: str
    demand_pct: float
    # Mode setpoint minus room sensor; None if unknown.
    deficit_c: float | None


class FlowFeedback:
    def __init__(self) -> None:
        self.feedback_k = 0.0
        self.last_change_ts: float | None = None
        self.frozen: FeedbackFreeze | None = None
        # The room that decides: one that wants more, else the highest one
        # that holds the flow, else the highest opening.
        self.lead: DemandSample | None = None
        self._last_ts: float | None = None
        # Unfrozen time since the last step, and the down count (see
        # DOWN_AFTER_S).
        # A start counts as a step: the valves first get the spacing to
        # answer the flow that runs now.
        self._since_step_s = 0.0
        self._below_s = 0.0

    def update(
        self,
        samples: list[DemandSample],
        step_k: float,
        frozen: FeedbackFreeze | None,
        now_ts: float,
        at_lower_limit: bool = False,
        at_upper_limit: bool = False,
    ) -> None:
        """Advance the timers and take at most one step.

        At a limit of the target the step that would push further is left
        out: the target would not move, the integrator would only wind up.
        """
        dt = (
            0.0
            if self._last_ts is None
            else max(0.0, min(MAX_TICK_S, now_ts - self._last_ts))
        )
        self._last_ts = now_ts
        self.frozen = frozen
        wanting = [s for s in samples if wants_more(s)]
        holding = [s for s in samples if holds(s)]
        self.lead = _highest(wanting) or _highest(holding) or _highest(samples)
        if frozen is not None:
            return
        self._since_step_s += dt
        # Without a usable room the valves say nothing: the count waits.
        if not samples:
            return
        if wanting or any(_short(s) for s in holding):
            self._below_s = 0.0
        elif holding:
            self._below_s = max(0.0, self._below_s - dt)
        else:
            self._below_s += dt

        if wanting:
            if self._since_step_s >= UP_SPACING_S and not at_upper_limit:
                self._step(step_k, now_ts)
        elif self._below_s >= DOWN_AFTER_S:
            self._below_s = 0.0
            if not at_lower_limit:
                self._step(-step_k, now_ts)

    @property
    def down_count_s(self) -> float:
        """How far the count towards the next step down has come."""
        return self._below_s

    def _step(self, delta_k: float, now_ts: float) -> None:
        value = max(
            -FEEDBACK_LIMIT_K,
            min(FEEDBACK_LIMIT_K, round(self.feedback_k + delta_k, 3)),
        )
        if value == self.feedback_k:
            return
        self.feedback_k = value
        self.last_change_ts = now_ts
        self._since_step_s = 0.0

    def reset(self, now_ts: float) -> None:
        """Back to no feedback; the valves first get the spacing to answer."""
        self.feedback_k = 0.0
        self.last_change_ts = now_ts
        self._last_ts = now_ts
        self._since_step_s = 0.0
        self._below_s = 0.0

    def export(self) -> dict[str, Any]:
        return {"feedback_k": self.feedback_k, "last_change_ts": self.last_change_ts}

    def restore(self, data: dict[str, Any]) -> None:
        try:
            value = float(data.get("feedback_k", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        self.feedback_k = max(-FEEDBACK_LIMIT_K, min(FEEDBACK_LIMIT_K, value))
        last = data.get("last_change_ts")
        self.last_change_ts = float(last) if isinstance(last, int | float) else None


def _highest(samples: list[DemandSample]) -> DemandSample | None:
    return max(samples, key=lambda s: s.demand_pct, default=None)


def wants_more(sample: DemandSample) -> bool:
    """Open and short of the target; saturated unless above it."""
    if sample.deficit_c is None:
        return sample.demand_pct >= DEMAND_SATURATED_PCT
    if sample.demand_pct >= DEMAND_SATURATED_PCT and sample.deficit_c >= 0.0:
        return True
    return sample.demand_pct > DEMAND_UP_PCT and sample.deficit_c >= UP_DEFICIT_C


def _short(sample: DemandSample) -> bool:
    return sample.deficit_c is not None and sample.deficit_c >= SHORT_DEFICIT_C


def holds(sample: DemandSample) -> bool:
    """Open enough to keep the flow from going down."""
    if sample.demand_pct < DEMAND_DOWN_PCT:
        return False
    return sample.deficit_c is None or sample.deficit_c > -OVER_TARGET_C

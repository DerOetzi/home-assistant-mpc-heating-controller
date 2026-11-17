"""Flow feedback: the measured valve demand of the flow lead rooms.

The feedforward (hold flow from the learned room model) says what the rooms
should need; the valves say what they do need. One count adds up what they
say: a room near fully open and clearly below its target counts it down,
valves well below open, or open only in rooms above their target, count it
up, a room that holds the flow brings it back towards zero. Half an hour
down means too little flow: one step up; an hour up means the flow can drop:
one step down. The sum of the steps, feedback_k, is added to the
feedforward. It only moves while what
was decided also reaches the heat source and the heat source heats.

For comparison only, without effect on the flow, the same count also runs
per room on that room alone: a room at -UP_AFTER_S alone would have stepped
up, all rooms at DOWN_AFTER_S together would have stepped down. Those would-be
steps are counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

FEEDBACK_LIMIT_K = 3.0
# Up: valves this far open and the room short of its target, or saturated
# and a little short; the count goes down while one is, a step at
# -UP_AFTER_S. 0.1 K short is still the TRV's own control band.
DEMAND_UP_PCT = 90.0
DEMAND_SATURATED_PCT = 98.0
UP_DEFICIT_C = 0.2
SATURATED_DEFICIT_C = 0.1
UP_AFTER_S = 30 * 60
# Down: no lead room holds the flow; the count goes up, a step at
# DOWN_AFTER_S. A room this far open holds it: the count goes back to zero.
DEMAND_DOWN_PCT = 80.0
DOWN_AFTER_S = 3600
# A room this far above its target holds the flow neither up nor down: its
# open valve comes from the TRV's own reading, more flow would not help it.
OVER_TARGET_C = 0.1
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
        # The count: below zero towards a step up, above towards a step down.
        # A start and a step set it to zero, a freeze drops what it has
        # towards up: the valves first answer the flow that runs now.
        self._count_s = 0.0
        # Comparison only: the count per room, its would-be steps up, and
        # the steps a count per room would have taken. Not stored: like the
        # count it starts over at a start; the step tallies are stored.
        self._room_count_s: dict[str, float] = {}
        self.room_steps_up: dict[str, int] = {}
        self.variant_up = 0
        self.variant_down = 0

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
            self._count_s = max(0.0, self._count_s)
            self._room_count_s = {
                room: max(0.0, count) for room, count in self._room_count_s.items()
            }
            return
        # Without a usable room the valves say nothing: the count waits.
        if not samples:
            return
        self._update_rooms(samples, dt)
        self._count_s = _advance(self._count_s, bool(wanting), bool(holding), dt)

        if self._count_s <= -UP_AFTER_S:
            self._count_s = 0.0
            self._room_count_s.clear()
            if not at_upper_limit:
                self._step(step_k, now_ts)
        elif self._count_s >= DOWN_AFTER_S:
            self._count_s = 0.0
            self._room_count_s.clear()
            if not at_lower_limit:
                self._step(-step_k, now_ts)

    def _update_rooms(self, samples: list[DemandSample], dt: float) -> None:
        """The comparison count per room; rooms without a sample wait."""
        stepped_up = False
        for sample in samples:
            count = _advance(
                self._room_count_s.get(sample.room, 0.0),
                wants_more(sample),
                holds(sample),
                dt,
            )
            if count <= -UP_AFTER_S:
                count = 0.0
                self.room_steps_up[sample.room] = (
                    self.room_steps_up.get(sample.room, 0) + 1
                )
                stepped_up = True
            self._room_count_s[sample.room] = min(float(DOWN_AFTER_S), count)
        if stepped_up:
            self.variant_up += 1
        elif all(
            self._room_count_s[sample.room] >= DOWN_AFTER_S for sample in samples
        ):
            self.variant_down += 1
            self._room_count_s.clear()

    def room_count_s(self, room: str) -> float | None:
        """A room's comparison count; None before its first sample."""
        return self._room_count_s.get(room)

    @property
    def count_s(self) -> float:
        """The count: below zero towards a step up, above towards one down."""
        return self._count_s

    def _step(self, delta_k: float, now_ts: float) -> None:
        value = max(
            -FEEDBACK_LIMIT_K,
            min(FEEDBACK_LIMIT_K, round(self.feedback_k + delta_k, 3)),
        )
        if value == self.feedback_k:
            return
        self.feedback_k = value
        self.last_change_ts = now_ts

    def reset(self, now_ts: float) -> None:
        """Back to no feedback; the count starts over."""
        self.feedback_k = 0.0
        self.last_change_ts = now_ts
        self._last_ts = now_ts
        self._count_s = 0.0
        self._room_count_s.clear()

    def export(self) -> dict[str, Any]:
        return {
            "feedback_k": self.feedback_k,
            "last_change_ts": self.last_change_ts,
            "room_steps_up": dict(self.room_steps_up),
            "variant_up": self.variant_up,
            "variant_down": self.variant_down,
        }

    def restore(self, data: dict[str, Any]) -> None:
        try:
            value = float(data.get("feedback_k", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        self.feedback_k = max(-FEEDBACK_LIMIT_K, min(FEEDBACK_LIMIT_K, value))
        last = data.get("last_change_ts")
        self.last_change_ts = float(last) if isinstance(last, int | float) else None
        steps = data.get("room_steps_up")
        self.room_steps_up = (
            {
                str(room): int(count)
                for room, count in steps.items()
                if isinstance(count, int)
            }
            if isinstance(steps, dict)
            else {}
        )
        self.variant_up = _tally(data.get("variant_up"))
        self.variant_down = _tally(data.get("variant_down"))


def _tally(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _advance(count: float, wanting: bool, holding: bool, dt: float) -> float:
    """One tick of the count: down while wanting, to zero while holding."""
    if wanting:
        return count - dt
    if holding:
        return max(0.0, count - dt) if count > 0.0 else min(0.0, count + dt)
    return count + dt


def _highest(samples: list[DemandSample]) -> DemandSample | None:
    return max(samples, key=lambda s: s.demand_pct, default=None)


def wants_more(sample: DemandSample) -> bool:
    """Open and short of the target; saturated and a little short."""
    if sample.deficit_c is None:
        return sample.demand_pct >= DEMAND_SATURATED_PCT
    if (
        sample.demand_pct >= DEMAND_SATURATED_PCT
        and sample.deficit_c >= SATURATED_DEFICIT_C
    ):
        return True
    return sample.demand_pct > DEMAND_UP_PCT and sample.deficit_c >= UP_DEFICIT_C


def holds(sample: DemandSample) -> bool:
    """Open enough to keep the flow from going down."""
    if sample.demand_pct < DEMAND_DOWN_PCT:
        return False
    return sample.deficit_c is None or sample.deficit_c > -OVER_TARGET_C

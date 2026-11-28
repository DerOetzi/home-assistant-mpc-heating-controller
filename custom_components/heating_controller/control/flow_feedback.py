"""Flow feedback: the measured valve demand of the flow lead rooms.

The feedforward (hold flow from the learned room model) says what the rooms
should need; the valves say what they do need. One count adds up what they
say: a room near fully open and clearly below its target counts it down,
the faster the further it is short; a room well below open and at its target
(at most a tenth above, less than a tenth below) counts it up; a room nearly
fully open and at most at its target holds the flow and brings the count back
towards zero. A room above its target (cooking, sun) or short
of it with a shut valve counts neither way. Half an
hour down means too little flow: one step up; an hour up (at night an hour
and a half) means the flow can drop: one step down. The radiators and rooms
answer a step only over hours: for a lock after a step the count does not
move towards another one in the same direction, the other direction stays
open. The lock is shorter up than down, and at night shorter still up and
longer down: a room short at night cools further until the morning, a step
down at night has little use. The sum of the steps,
feedback_k, is added to the feedforward. It only moves while what was
decided also reaches the heat source and the heat source heats.

A count ratchets: one room wanting more is enough to step up, one room
holding is enough to block every step down, and a TRV that keeps its valve
open for reasons of its own does either. So feedback_k also returns to zero
on its own: after LEAK_AFTER_S (at night NIGHT_LEAK_AFTER_S) in which no room
wanted more, a positive value takes one step down; a negative one takes a
step up after as long without a room that says the flow can drop. A room
really short pulls it back up through the count.

After a pause of the room heating (hot water charge, defrost) the rooms
catch up on what cooled off: for as long as the pause lasted, at most
CATCH_UP_MAX_S, a room short of its target does not count towards a step
up. A room that holds the flow still brings the count back to zero.

For comparison only, without effect on the flow, the same count also runs
per room on that room alone: a room at -UP_AFTER_S alone would have stepped
up, all rooms at DOWN_AFTER_S together would have stepped down. Those would-be
steps are counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

FEEDBACK_LIMIT_K = 3.0
# Deficits are compared rounded half up to DEFICIT_STEP_C, the resolution of
# the room sensors: a few hundredths do not decide.
DEFICIT_STEP_C = Decimal("0.1")
# Up: valves this far open and the room more than this short of its target,
# or saturated and more than a little short; the count goes down while one
# is, a step at -UP_AFTER_S. 0.1 K short is still the TRV's own control band.
DEMAND_UP_PCT = 90.0
DEMAND_SATURATED_PCT = 98.0
UP_DEFICIT_C = 0.2
SATURATED_DEFICIT_C = 0.1
UP_AFTER_S = 30 * 60
# Further short counts faster: the deficit over UP_RATE_DEFICIT_C, at least
# once and at most UP_RATE_MAX times as fast.
UP_RATE_DEFICIT_C = 0.2
UP_RATE_MAX = 3.0
# Down: no lead room holds the flow and one is below this and at its target;
# the count goes up, a step at DOWN_AFTER_S (at night
# NIGHT_DOWN_AFTER_S).
DEMAND_DOWN_PCT = 80.0
# A room this far open and at most at its target (rounded) holds the flow:
# the count goes back to zero.
DEMAND_HOLD_PCT = 90.0
# A room this far short of its target or further does not count down, however
# far its valve is shut: a TRV on its own reading can close while the room
# is still short.
DOWN_BLOCK_DEFICIT_C = 0.1
DOWN_AFTER_S = 3600
NIGHT_DOWN_AFTER_S = 90 * 60
# After a step the count stays at zero on that step's side this long, by
# direction and day or night at the time of asking.
STEP_LOCK_UP_S = 60 * 60
STEP_LOCK_DOWN_S = 90 * 60
NIGHT_STEP_LOCK_UP_S = 45 * 60
NIGHT_STEP_LOCK_DOWN_S = 120 * 60
# A room with a low valve more than this above its target does not count
# down: gains (cooking, sun) would lower the flow.
OVER_TARGET_C = 0.1
# Return to zero: this long without a room against it, one step towards zero.
LEAK_AFTER_S = 4 * 3600
NIGHT_LEAK_AFTER_S = 6 * 3600
# A longer gap between two updates (restart, stalled loop) counts as this.
MAX_TICK_S = 300.0


class TargetLimit(StrEnum):
    """The limit of the flow target the decided value already sits at."""

    LOWER = "lower"
    UPPER = "upper"


class FeedbackFreeze(StrEnum):
    WRITE_OFF = "write_off"
    HEATING_OFF = "heating_off"
    UNREACHABLE = "unreachable"
    FALLBACK = "fallback"
    HOT_WATER = "hot_water"
    DEFROST = "defrost"
    BOOST = "boost"
    NOT_WRITTEN = "not_written"


# Freezes in which the radiators get no heat: afterwards the rooms catch up.
PAUSE_FREEZES = frozenset({FeedbackFreeze.HOT_WATER, FeedbackFreeze.DEFROST})
CATCH_UP_MAX_S = 3600.0


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
        # The last step's direction (+1 up, -1 down) and time, for the lock.
        self.last_step_sign = 0
        self.last_step_ts: float | None = None
        self.frozen: FeedbackFreeze | None = None
        # Whether the last update ran at night: the night's times apply.
        self.night = False
        # The room that decides: one that wants more, else the highest one
        # that holds the flow, else the highest opening.
        self.lead: DemandSample | None = None
        self._last_ts: float | None = None
        # The count: below zero towards a step up, above towards a step down.
        # A start and a step set it to zero, a freeze drops what it has
        # towards up: the valves first answer the flow that runs now. While
        # a step's lock runs it stays at zero on that step's side.
        self._count_s = 0.0
        # Time towards the next step back to zero. Not stored, like the
        # count; any step starts it over.
        self._leak_s = 0.0
        # Comparison only: the count per room, its would-be steps up, and
        # the steps a count per room would have taken. Not stored: like the
        # count it starts over at a start; the step tallies are stored.
        self._room_count_s: dict[str, float] = {}
        self.room_steps_up: dict[str, int] = {}
        self.variant_up = 0
        self.variant_down = 0
        # The running pause and, once it ended, the catch-up after it. Not
        # stored: a start does not know how long the pause before it was.
        self._pause_since_ts: float | None = None
        self._pause_reason: FeedbackFreeze | None = None
        self.catch_up_until_ts: float | None = None
        self.catch_up_reason: FeedbackFreeze | None = None

    def catching_up(self, now_ts: float) -> bool:
        """Whether the rooms still catch up after a pause."""
        return self.catch_up_until_ts is not None and now_ts < self.catch_up_until_ts

    def _track_pause(self, frozen: FeedbackFreeze | None, now_ts: float) -> None:
        if frozen in PAUSE_FREEZES:
            if self._pause_since_ts is None:
                self._pause_since_ts = now_ts
                self._pause_reason = frozen
            return
        if self._pause_since_ts is None:
            return
        until = now_ts + min(CATCH_UP_MAX_S, now_ts - self._pause_since_ts)
        if self.catch_up_until_ts is None or until > self.catch_up_until_ts:
            self.catch_up_until_ts = until
            self.catch_up_reason = self._pause_reason
        self._pause_since_ts = None
        self._pause_reason = None

    def update(  # noqa: PLR0913 -- the situation flags are keyword-only
        self,
        samples: list[DemandSample],
        step_k: float,
        frozen: FeedbackFreeze | None,
        now_ts: float,
        *,
        limit: TargetLimit | None = None,
        night: bool = False,
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
        self.night = night
        self._track_pause(frozen, now_ts)
        wanting = [s for s in samples if wants_more(s)]
        holding = [s for s in samples if holds(s)]
        lowering = [s for s in samples if lowers(s)]
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
        catching_up = self.catching_up(now_ts)
        self._update_rooms(samples, dt, catching_up)
        up_rate = 0.0 if catching_up else max(map(up_rate_of, wanting), default=0.0)
        self._count_s = self._locked(
            _advance(self._count_s, up_rate, bool(holding), bool(lowering), dt),
            now_ts,
        )

        if self._count_s <= -UP_AFTER_S:
            self._count_s = 0.0
            self._room_count_s.clear()
            if limit is not TargetLimit.UPPER:
                self._step(step_k, now_ts)
        elif self._count_s >= self.down_after_s:
            self._count_s = 0.0
            self._room_count_s.clear()
            if limit is not TargetLimit.LOWER:
                self._step(-step_k, now_ts)
        self._advance_leak(bool(wanting), bool(lowering), step_k, dt, now_ts, limit)

    def _advance_leak(  # noqa: PLR0913
        self,
        wanting: bool,
        lowering: bool,
        step_k: float,
        dt: float,
        now_ts: float,
        limit: TargetLimit | None,
    ) -> None:
        """Run the return to zero; a room against it pauses it."""
        if self.feedback_k == 0.0:
            self._leak_s = 0.0
            return
        sign = -1 if self.feedback_k > 0 else 1
        if wanting if sign < 0 else lowering:
            return
        self._leak_s += dt
        if self._leak_s < self.leak_after_s:
            return
        self._leak_s = 0.0
        if limit is (TargetLimit.LOWER if sign < 0 else TargetLimit.UPPER):
            return
        self._step(sign * min(step_k, abs(self.feedback_k)), now_ts)

    @property
    def leak_after_s(self) -> float:
        """How long without a room against it until a step back to zero."""
        return float(NIGHT_LEAK_AFTER_S if self.night else LEAK_AFTER_S)

    @property
    def leak_s(self) -> float:
        """Time run towards the next step back to zero."""
        return self._leak_s

    @property
    def down_after_s(self) -> float:
        """How long the count runs up towards a step down."""
        return float(NIGHT_DOWN_AFTER_S if self.night else DOWN_AFTER_S)

    def lock_s(self, sign: int) -> float:
        """How long a step in this direction locks its own direction."""
        if sign > 0:
            return float(NIGHT_STEP_LOCK_UP_S if self.night else STEP_LOCK_UP_S)
        return float(NIGHT_STEP_LOCK_DOWN_S if self.night else STEP_LOCK_DOWN_S)

    def locked_sign(self, now_ts: float) -> int:
        """The direction a step's lock still holds back: +1, -1 or 0."""
        if (
            self.last_step_ts is None
            or self.last_step_sign == 0
            or now_ts - self.last_step_ts >= self.lock_s(self.last_step_sign)
        ):
            return 0
        return self.last_step_sign

    def locked_until_ts(self, now_ts: float) -> float | None:
        """When the running lock ends; None without one."""
        if self.locked_sign(now_ts) == 0 or self.last_step_ts is None:
            return None
        return self.last_step_ts + self.lock_s(self.last_step_sign)

    def _locked(self, count: float, now_ts: float) -> float:
        sign = self.locked_sign(now_ts)
        if sign > 0:
            return max(0.0, count)
        if sign < 0:
            return min(0.0, count)
        return count

    def _update_rooms(
        self, samples: list[DemandSample], dt: float, catching_up: bool
    ) -> None:
        """The comparison count per room; rooms without a sample wait."""
        stepped_up = False
        for sample in samples:
            count = _advance(
                self._room_count_s.get(sample.room, 0.0),
                0.0 if catching_up or not wants_more(sample) else up_rate_of(sample),
                holds(sample),
                lowers(sample),
                dt,
            )
            if count <= -UP_AFTER_S:
                count = 0.0
                self.room_steps_up[sample.room] = (
                    self.room_steps_up.get(sample.room, 0) + 1
                )
                stepped_up = True
            self._room_count_s[sample.room] = min(self.down_after_s, count)
        if stepped_up:
            self.variant_up += 1
        elif all(
            self._room_count_s[sample.room] >= self.down_after_s for sample in samples
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
        self._leak_s = 0.0
        self.feedback_k = value
        self.last_change_ts = now_ts
        self.last_step_sign = 1 if delta_k > 0 else -1
        self.last_step_ts = now_ts

    def reset(self, now_ts: float) -> None:
        """Back to no feedback; the count starts over."""
        self.feedback_k = 0.0
        self.last_change_ts = now_ts
        self.last_step_sign = 0
        self.last_step_ts = None
        self._last_ts = now_ts
        self._count_s = 0.0
        self._leak_s = 0.0
        self._room_count_s.clear()
        self._pause_since_ts = None
        self._pause_reason = None
        self.catch_up_until_ts = None
        self.catch_up_reason = None

    def export(self) -> dict[str, Any]:
        return {
            "feedback_k": self.feedback_k,
            "last_change_ts": self.last_change_ts,
            "last_step_sign": self.last_step_sign,
            "last_step_ts": self.last_step_ts,
            "room_steps_up": dict(self.room_steps_up),
            "variant_up": self.variant_up,
            "variant_down": self.variant_down,
        }

    def restore(self, data: dict[str, Any]) -> None:
        try:
            value = float(data.get("feedback_k", 0.0))
        except TypeError, ValueError:
            value = 0.0
        self.feedback_k = max(-FEEDBACK_LIMIT_K, min(FEEDBACK_LIMIT_K, value))
        last = data.get("last_change_ts")
        self.last_change_ts = float(last) if isinstance(last, int | float) else None
        sign = data.get("last_step_sign")
        self.last_step_sign = sign if sign in (-1, 0, 1) else 0
        step_ts = data.get("last_step_ts")
        self.last_step_ts = float(step_ts) if isinstance(step_ts, int | float) else None
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


def _advance(
    count: float, up_rate: float, holding: bool, lowering: bool, dt: float
) -> float:
    """One tick of the count.

    Down at up_rate while a room wants more, to zero while one holds, up
    while one is low and at its target, else it stays.
    """
    if up_rate > 0.0:
        return count - dt * up_rate
    if holding:
        return max(0.0, count - dt) if count > 0.0 else min(0.0, count + dt)
    if lowering:
        return count + dt
    return count


def _highest(samples: list[DemandSample]) -> DemandSample | None:
    return max(samples, key=lambda s: s.demand_pct, default=None)


def rounded_deficit_c(deficit_c: float) -> float:
    """The deficit rounded half up to DEFICIT_STEP_C."""
    return float(
        Decimal(str(deficit_c)).quantize(DEFICIT_STEP_C, rounding=ROUND_HALF_UP)
    )


def wants_more(sample: DemandSample) -> bool:
    """Open and short of the target; saturated and a little short."""
    if sample.deficit_c is None:
        return sample.demand_pct >= DEMAND_SATURATED_PCT
    deficit_c = rounded_deficit_c(sample.deficit_c)
    if sample.demand_pct >= DEMAND_SATURATED_PCT and deficit_c > SATURATED_DEFICIT_C:
        return True
    return sample.demand_pct > DEMAND_UP_PCT and deficit_c > UP_DEFICIT_C


def up_rate_of(sample: DemandSample) -> float:
    """How fast a room that wants more counts towards a step up."""
    if sample.deficit_c is None:
        return 1.0
    deficit_c = rounded_deficit_c(sample.deficit_c)
    return min(UP_RATE_MAX, max(1.0, deficit_c / UP_RATE_DEFICIT_C))


def _not_above_target(sample: DemandSample) -> bool:
    return (
        sample.deficit_c is None
        or rounded_deficit_c(sample.deficit_c) >= -OVER_TARGET_C
    )


def _not_short(sample: DemandSample) -> bool:
    return (
        sample.deficit_c is None
        or rounded_deficit_c(sample.deficit_c) < DOWN_BLOCK_DEFICIT_C
    )


def lowers(sample: DemandSample) -> bool:
    """Well below open and at its target: the flow can drop."""
    return (
        sample.demand_pct < DEMAND_DOWN_PCT
        and _not_above_target(sample)
        and _not_short(sample)
    )


def holds(sample: DemandSample) -> bool:
    """Nearly fully open and at most at its target: keeps the flow up."""
    if sample.demand_pct < DEMAND_HOLD_PCT:
        return False
    return sample.deficit_c is None or rounded_deficit_c(sample.deficit_c) >= 0.0

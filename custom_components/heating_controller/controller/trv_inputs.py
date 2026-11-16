"""What the room hands its TRVs besides the setpoint.

The room sensor's reading goes to every TRV that takes an external
temperature: both TRVs of a room then work on the same measurement. A TRV
on an exposed radiator only offsets its own sensor by it and needs it
rarely; one on a covered radiator regulates on it alone and drops it soon
after it stops coming. TRVs that share a room also share its load: each
reports its own, the room's mean goes back to all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Danfoss Ally: an exposed radiator needs the reading every 3 hours at most,
# a covered one every 30 minutes; it takes one every 30 or 5 minutes at most
# often, or on a change of 0.1 K. The upper ends leave a margin to the
# TRV's timeout.
EXPOSED_MIN_INTERVAL_S = 30 * 60
EXPOSED_MAX_INTERVAL_S = 150 * 60
COVERED_MIN_INTERVAL_S = 5 * 60
COVERED_MAX_INTERVAL_S = 25 * 60
EXTERNAL_CHANGE_C = 0.1

LOAD_MEAN_INTERVAL_S = 15 * 60
# A TRV's load estimate counts while it reported this recently and is not
# below this (it reports -8000 and other large negatives while unknown).
LOAD_ESTIMATE_MAX_AGE_S = 90 * 60
LOAD_ESTIMATE_MIN = -500


@dataclass
class _Sent:
    value_c: float
    ts: float
    covered: bool


class ExternalTemperatureSchedule:
    """When to send one TRV the room sensor's reading again."""

    def __init__(self) -> None:
        self._sent: _Sent | None = None

    def due(self, value_c: float, covered: bool, now_ts: float) -> bool:
        sent = self._sent
        if sent is None or sent.covered != covered:
            return True
        elapsed_s = now_ts - sent.ts
        min_s, max_s = (
            (COVERED_MIN_INTERVAL_S, COVERED_MAX_INTERVAL_S)
            if covered
            else (EXPOSED_MIN_INTERVAL_S, EXPOSED_MAX_INTERVAL_S)
        )
        if elapsed_s >= max_s:
            return True
        return (
            elapsed_s >= min_s
            and abs(value_c - sent.value_c) >= EXTERNAL_CHANGE_C - 1e-9
        )

    def sent(self, value_c: float, covered: bool, now_ts: float) -> None:
        self._sent = _Sent(value_c, now_ts, covered)

    def reset(self) -> None:
        """Nothing is being sent: the next reading goes out at once."""
        self._sent = None


def raw_value(value_c: float, scale: float) -> float:
    """The reading in the TRV's unit; a whole number when it is scaled."""
    raw = value_c * scale
    return float(round(raw)) if scale != 1 else round(raw, 2)


def load_room_mean(
    estimates: list[tuple[float | None, float | None]], now_ts: float
) -> int | None:
    """The mean of the load estimates still current, each (value, reported_ts)."""
    current = [
        value
        for value, reported_ts in estimates
        if value is not None
        and reported_ts is not None
        and now_ts - reported_ts <= LOAD_ESTIMATE_MAX_AGE_S
        and value >= LOAD_ESTIMATE_MIN
    ]
    if not current:
        return None
    return round(sum(current) / len(current))

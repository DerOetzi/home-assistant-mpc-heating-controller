"""Slow correction of a room's setpoint for the TRV's own reading.

The TRV regulates on what it measures at the radiator, which is warmer than
the room. Every few hours the room's mean temperature is compared with the
mode's setpoint and the setpoint handed to the TRV moves by one step. The
correction stays slow so it does not fight the TRV's own PI.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# How often the correction may move.
CORRECTION_INTERVAL_S = 2 * 3600
# The room's temperature and demand are averaged over this window.
CORRECTION_WINDOW_S = 3600
# A room error smaller than this leaves the correction where it is.
CORRECTION_DEADBAND_C = 0.3
CORRECTION_STEP_C = 0.5
CORRECTION_LIMIT_C = 2.0
# A room whose temperature spans more than this within the window is still
# moving, and its mean says little about where it settles.
CORRECTION_MAX_SPAN_C = 0.3
CORRECTION_MIN_SAMPLES = 2
# The window counts as covered when its first sample lies this close to its
# start; a sensor that reports less often leaves the room uncorrected.
CORRECTION_MAX_START_GAP_S = 15 * 60
# A valve this far open already gives what it can; raising the setpoint
# further would only wind up.
CORRECTION_MAX_DEMAND_UP_PCT = 90.0
# Below this the valve is shut; lowering the setpoint cannot cool the room.
CORRECTION_MIN_DEMAND_DOWN_PCT = 0.0


@dataclass
class _Sample:
    timestamp: float
    room_temperature_c: float
    demand_pct: float | None


class SetpointCorrection:
    """One correction value for all TRVs of a room."""

    def __init__(self, now_ts: float, correction_c: float = 0.0) -> None:
        self._correction_c = _limit(correction_c)
        self._samples: deque[_Sample] = deque()
        # The first step waits a full interval after startup.
        self._last_evaluation_ts = now_ts

    @property
    def correction_c(self) -> float:
        return self._correction_c

    def add_sample(
        self, now_ts: float, room_temperature_c: float, demand_pct: float | None
    ) -> None:
        self._samples.append(_Sample(now_ts, room_temperature_c, demand_pct))
        # One sample before the window stays: it holds the value at its start.
        while (
            len(self._samples) > 1
            and self._samples[1].timestamp <= now_ts - CORRECTION_WINDOW_S
        ):
            self._samples.popleft()

    def due(self, now_ts: float) -> bool:
        return now_ts - self._last_evaluation_ts >= CORRECTION_INTERVAL_S

    def evaluate(self, now_ts: float, target_c: float) -> bool:
        """Move the correction one step if the room calls for it.

        Called once due() and the room's conditions allow a step. Returns
        whether the correction changed. A room whose samples do not describe
        a settled hour waits for the next call.
        """
        window = self._window(now_ts)
        if window is None:
            return False
        temperatures = [sample.room_temperature_c for sample in window]
        if max(temperatures) - min(temperatures) > CORRECTION_MAX_SPAN_C:
            return False
        self._last_evaluation_ts = now_ts
        error_c = target_c - _time_mean(
            [(s.timestamp, s.room_temperature_c) for s in window], now_ts
        )
        if abs(error_c) < CORRECTION_DEADBAND_C:
            return False
        demands = [
            (sample.timestamp, sample.demand_pct)
            for sample in window
            if sample.demand_pct is not None
        ]
        demand_pct = _time_mean(demands, now_ts) if demands else None
        if error_c > 0:
            if demand_pct is not None and demand_pct >= CORRECTION_MAX_DEMAND_UP_PCT:
                return False
            step_c = CORRECTION_STEP_C
        else:
            if demand_pct is not None and demand_pct <= CORRECTION_MIN_DEMAND_DOWN_PCT:
                return False
            step_c = -CORRECTION_STEP_C
        corrected_c = _limit(self._correction_c + step_c)
        if corrected_c == self._correction_c:
            return False
        self._correction_c = corrected_c
        return True

    def _window(self, now_ts: float) -> list[_Sample] | None:
        start_ts = now_ts - CORRECTION_WINDOW_S
        samples = list(self._samples)
        inside = [sample for sample in samples if sample.timestamp > start_ts]
        if len(inside) < CORRECTION_MIN_SAMPLES:
            return None
        if samples[0].timestamp > start_ts + CORRECTION_MAX_START_GAP_S:
            return None
        # The sample before the window holds the value at its start.
        before = [sample for sample in samples if sample.timestamp <= start_ts]
        if before:
            first = before[-1]
            return [
                _Sample(start_ts, first.room_temperature_c, first.demand_pct),
                *inside,
            ]
        return inside


def _limit(correction_c: float) -> float:
    return max(-CORRECTION_LIMIT_C, min(CORRECTION_LIMIT_C, correction_c))


def _time_mean(points: list[tuple[float, float]], end_ts: float) -> float:
    """Mean of a value that holds from each point until the next."""
    if len(points) == 1:
        return points[0][1]
    total = 0.0
    duration = 0.0
    for (start_ts, value), next_ts in zip(
        points, [point[0] for point in points[1:]] + [end_ts], strict=True
    ):
        total += value * (next_ts - start_ts)
        duration += next_ts - start_ts
    if duration <= 0:
        return points[-1][1]
    return total / duration

"""Per-TRV valve curve: how far the valve opens for a setpoint offset.

A TRV with a relative setpoint (its own reading plus an offset) opens its
valve by its own PI controller. Which opening an offset gives differs from
valve to valve (pre-setting, valve insert, how the TRV sits on it), so each
TRV learns it while it runs: whenever its reported setpoint changes, the
opening it holds some minutes later is one sample for that offset.

In relative mode the controller sets each TRV's offset for the target
opening from this curve (see HeatEmitterModel.calculate_target_temperatures):
the learned one, until then and for a scattered TRV the profile's seed. Only
a TRV with neither falls back to the weighted mapping.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from math import floor
from statistics import median, quantiles
from typing import Any, NamedTuple

from .models.emitter import RELATIVE_MAX_OFFSET_C, RELATIVE_MIN_OFFSET_C
from .models.valve import ValveCharacteristic

# A sample is the opening this long after the setpoint changed: the Danfoss
# PI has moved the valve by then, but the room has not yet answered.
VALVE_CURVE_SETTLE_S = 600
# Later than this the reading says more about the room than about the
# setpoint step. compute() runs at least every 5 minutes, so one call always
# lands between the two.
VALVE_CURVE_SAMPLE_LATEST_S = 900
# Offsets are grouped into bins this wide; one point per bin.
VALVE_CURVE_BIN_WIDTH_C = 0.5
# Each bin keeps the latest samples only, so the curve follows a valve that
# changes (new pre-setting, recalibration).
VALVE_CURVE_BIN_SAMPLES = 20
# A bin counts from this many samples; fewer are chance.
VALVE_CURVE_MIN_SAMPLES = 3
# The spread of a bin is judged from this many samples on.
VALVE_CURVE_SPREAD_MIN_SAMPLES = 5
# p90 - p10 of a bin's openings beyond this many percentage points: the TRV's
# PI integrates too much for a static curve, so the TRV has none.
VALVE_CURVE_MAX_SPREAD_PCT = 30
# Seed points closer than this to the learned span would sit between two
# learned points and bend the learned curve.
VALVE_CURVE_SEED_MARGIN_C = 0.25
# LEARNED needs this many valid bins, one of them at least this far above
# the reading, so the curve covers the range where the valve opens fully.
VALVE_CURVE_LEARNED_MIN_BINS = 3
VALVE_CURVE_LEARNED_MIN_OFFSET_C = 2.0
# Without a seed the largest relative offset opens the valve fully; added as
# long as no learned point reaches this far.
VALVE_CURVE_ANCHOR_BELOW_C = 2.5
VALVE_CURVE_FULL_OPEN_ANCHOR = (3.0, 100.0)
# Only a setpoint the relative mode hands out for heating starts a sample.
# An off write (the TRV's minimum target, ~15 K below its reading) or an
# absolute/eco jump would add bins far from the curve, with opening 0 or
# anything, and a later inverse would map opening 0 to -15 K.
VALVE_CURVE_MIN_OFFSET_C = RELATIVE_MIN_OFFSET_C
# The largest relative offset plus up to one 0.5 K rounding step, with margin.
VALVE_CURVE_MAX_OFFSET_C = RELATIVE_MAX_OFFSET_C + 1.0
# Float noise of reading + offset (20.1 - 20.0) must not drop the minimum.
_OFFSET_TOLERANCE_C = 1e-6
# Reported setpoints closer than this are the same setpoint.
VALVE_CURVE_TARGET_TOLERANCE_C = 0.01
# Keeps an offset like 0.9999999 (20.7 - 19.7) in its bin.
_BIN_ROUNDING_DIGITS = 6


class ValveCurveStatus(StrEnum):
    # No valid bin yet: the seed (if any) is the curve.
    SEED = "seed"
    # Valid bins, but too few or none where the valve opens wide: still the
    # seed, partial bins are not merged.
    LEARNING = "learning"
    # Enough valid bins, one of them where the valve opens wide: learned
    # points, the seed beyond them.
    LEARNED = "learned"
    # One bin scatters too much for a static curve: the seed, as the
    # nearest to how the valve opens.
    SCATTERED = "scattered"


class ValveCurveBin(NamedTuple):
    """One bin of samples, as the curve and the diagnosis see it."""

    offset_c: float  # median offset
    opening_pct: float  # median opening
    samples: int
    spread_pct: float  # p90 - p10 of the openings


@dataclass
class _OpenSample:
    started_ts: float
    offset_c: float


def _bin_key(offset_c: float) -> int:
    return floor(round(offset_c / VALVE_CURVE_BIN_WIDTH_C, _BIN_ROUNDING_DIGITS))


def _is_heating_offset(offset_c: float) -> bool:
    return (
        VALVE_CURVE_MIN_OFFSET_C - _OFFSET_TOLERANCE_C
        <= offset_c
        <= VALVE_CURVE_MAX_OFFSET_C + _OFFSET_TOLERANCE_C
    )


def _spread_pct(openings_pct: list[float]) -> float:
    if len(openings_pct) < 2:
        return 0.0
    deciles = quantiles(openings_pct, n=10, method="inclusive")
    return deciles[-1] - deciles[0]


class ValveCurveLearner:
    def __init__(
        self, seed: ValveCharacteristic | None, min_target_c: float | None = None
    ) -> None:
        self._seed = seed
        # The TRV's minimum target: writing it switches the TRV off.
        self._min_target_c = min_target_c
        # Bin key -> latest (offset °C, opening %) samples.
        self._bins: dict[int, deque[tuple[float, float]]] = {}
        # bins() is read by the sensor and the curve on every state write;
        # it only changes with a new sample.
        self._bins_cache: list[ValveCurveBin] | None = None
        self._last_target_c: float | None = None
        self._open: _OpenSample | None = None
        self._changed = False

    def observe(
        self,
        now_ts: float,
        trv_target_c: float | None,
        trv_reading_c: float | None,
        opening_pct: float | None,
        sample_allowed: bool,
    ) -> None:
        if trv_target_c is None:
            # Unknown whether the setpoint moved meanwhile.
            self._open = None
            return

        if (
            self._last_target_c is not None
            and abs(trv_target_c - self._last_target_c) > VALVE_CURVE_TARGET_TOLERANCE_C
        ):
            # A new setpoint ends whatever sample was open; only a heating
            # offset starts the next one.
            self._open = None
            if trv_reading_c is not None:
                offset_c = trv_target_c - trv_reading_c
                if self._starts_sample(trv_target_c, offset_c):
                    self._open = _OpenSample(now_ts, offset_c)
        self._last_target_c = trv_target_c

        if self._open is None:
            return
        if not sample_allowed:
            self._open = None
            return
        age_s = now_ts - self._open.started_ts
        if age_s < VALVE_CURVE_SETTLE_S:
            return
        sample = self._open
        self._open = None
        if age_s > VALVE_CURVE_SAMPLE_LATEST_S or opening_pct is None:
            return
        self._add_sample(sample.offset_c, opening_pct)
        self._changed = True

    def consume_changed(self) -> bool:
        """Whether a sample finished since the last call, so the owner saves."""
        changed = self._changed
        self._changed = False
        return changed

    def _starts_sample(self, target_c: float, offset_c: float) -> bool:
        if (
            self._min_target_c is not None
            and target_c <= self._min_target_c + VALVE_CURVE_TARGET_TOLERANCE_C
        ):
            return False
        return _is_heating_offset(offset_c)

    def _add_sample(self, offset_c: float, opening_pct: float) -> None:
        samples = self._bins.setdefault(
            _bin_key(offset_c), deque(maxlen=VALVE_CURVE_BIN_SAMPLES)
        )
        samples.append((offset_c, opening_pct))
        self._bins_cache = None

    def bins(self) -> list[ValveCurveBin]:
        """One entry per bin, ascending by offset."""
        if self._bins_cache is None:
            self._bins_cache = [
                self._summarize(self._bins[key]) for key in sorted(self._bins)
            ]
        return self._bins_cache

    @staticmethod
    def _summarize(samples: deque[tuple[float, float]]) -> ValveCurveBin:
        openings = [opening for _, opening in samples]
        return ValveCurveBin(
            offset_c=median(offset for offset, _ in samples),
            opening_pct=median(openings),
            samples=len(samples),
            spread_pct=_spread_pct(openings),
        )

    def _valid_bins(self) -> list[ValveCurveBin]:
        return [bin_ for bin_ in self.bins() if bin_.samples >= VALVE_CURVE_MIN_SAMPLES]

    @property
    def status(self) -> ValveCurveStatus:
        if any(
            bin_.samples >= VALVE_CURVE_SPREAD_MIN_SAMPLES
            and bin_.spread_pct > VALVE_CURVE_MAX_SPREAD_PCT
            for bin_ in self.bins()
        ):
            return ValveCurveStatus.SCATTERED
        valid = self._valid_bins()
        if not valid:
            return ValveCurveStatus.SEED
        if (
            len(valid) >= VALVE_CURVE_LEARNED_MIN_BINS
            and valid[-1].offset_c >= VALVE_CURVE_LEARNED_MIN_OFFSET_C
        ):
            return ValveCurveStatus.LEARNED
        return ValveCurveStatus.LEARNING

    def characteristic(self) -> ValveCharacteristic | None:
        """Learned points with the seed beyond them; else the seed.

        Until learned (and for a scattered TRV) the profile's seed is the
        curve: the planner counts in openings, and the seed maps them to
        offsets far better than the power-weighted fallback. Partial bins
        are not merged; a curve of one or two bins says nothing about the
        rest of the range. None only without seed and not learned: the
        control then keeps its weighted mapping (unknown TRV models).
        """
        if self.status is not ValveCurveStatus.LEARNED:
            return self._seed
        learned = [(bin_.offset_c, bin_.opening_pct) for bin_ in self._valid_bins()]

        points = list(learned)
        if self._seed is not None:
            lowest_c = learned[0][0] - VALVE_CURVE_SEED_MARGIN_C
            highest_c = learned[-1][0] + VALVE_CURVE_SEED_MARGIN_C
            points.extend(
                point
                for point in self._seed.points
                if point[0] < lowest_c or point[0] > highest_c
            )
        elif learned[-1][0] < VALVE_CURVE_ANCHOR_BELOW_C:
            points.append(VALVE_CURVE_FULL_OPEN_ANCHOR)

        points.sort()
        # A bin's median can sit below the one before (noise, a slow valve);
        # the curve stays monotone so the inverse is unique.
        monotone: list[tuple[float, float]] = []
        highest_pct = 0.0
        for offset_c, opening_pct in points:
            highest_pct = max(highest_pct, opening_pct)
            monotone.append((offset_c, highest_pct))
        return ValveCharacteristic(points=tuple(monotone))

    def export(self) -> dict[str, Any]:
        """Finished samples only; an open one would restart without its start."""
        return {
            "samples": [
                [offset_c, opening_pct]
                for key in sorted(self._bins)
                for offset_c, opening_pct in self._bins[key]
            ]
        }

    def restore(self, data: dict[str, Any]) -> None:
        self._bins = {}
        self._bins_cache = None
        for sample in data.get("samples", []):
            try:
                offset_c, opening_pct = (float(value) for value in sample)
            except (TypeError, ValueError):
                continue
            # Data saved before the offset range existed may hold off writes.
            if not _is_heating_offset(offset_c):
                continue
            self._add_sample(offset_c, opening_pct)

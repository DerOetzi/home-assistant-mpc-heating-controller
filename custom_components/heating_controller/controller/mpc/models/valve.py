from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise


@dataclass(frozen=True)
class ValveCharacteristic:
    """How far a TRV opens its valve for a setpoint above its own reading.

    points are (setpoint minus TRV reading in °C, valve opening in %), both
    columns ascending. Whoever builds one (profile schema, learner) keeps
    them ascending; there is no check here.
    """

    points: tuple[tuple[float, float], ...]

    def opening_pct(self, offset_c: float) -> float:
        """Opening at a setpoint offset, piecewise linear between the points.

        Below the first point the TRV regulates to its setpoint and keeps the
        valve shut; beyond the last one it stays at the last opening.
        """
        points = self.points
        if offset_c < points[0][0]:
            return 0.0
        for (offset_low, opening_low), (offset_high, opening_high) in pairwise(points):
            if offset_c <= offset_high:
                if offset_high == offset_low:
                    return opening_high
                return opening_low + (opening_high - opening_low) * (
                    offset_c - offset_low
                ) / (offset_high - offset_low)
        return points[-1][1]

    def offset_c(self, opening_pct: float) -> float:
        """Smallest setpoint offset that reaches an opening.

        A flat stretch answers with its start: more offset buys no more
        opening there.
        """
        points = self.points
        if opening_pct <= points[0][1]:
            return points[0][0]
        for (offset_low, opening_low), (offset_high, opening_high) in pairwise(points):
            # opening_pct > opening_low here, so a flat stretch never matches.
            if opening_pct <= opening_high:
                return offset_low + (offset_high - offset_low) * (
                    opening_pct - opening_low
                ) / (opening_high - opening_low)
        return points[-1][0]

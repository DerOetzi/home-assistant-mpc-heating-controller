"""Measured-flow correction: where the heat source really lands against its curve.

The offset is computed against the configured curve. The heat source does
not always deliver what curve plus offset promise: it may average its own
outdoor reading, or its curve may not be the one entered. The correction is
the measured flow minus the effective curve (curve plus offset), smoothed
with a time constant of TIME_CONSTANT_S and bounded to +-LIMIT_K. The offset
is then computed against curve plus correction, so the hysteresis compares
with the real gap.

The heat source reports whole degrees and swings with defrost and charges:
the correction only learns in calm operation, while the feedback may move,
the rooms do not catch up after a pause, and at least SETTLE_S after the
last change of the actuator.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

LIMIT_K = 2.0
TIME_CONSTANT_S = 2 * 3600.0
# After a change of the actuator the heat source needs this long to get there.
SETTLE_S = 30 * 60.0
# A longer gap between two updates (restart, stalled loop) counts as this.
MAX_TICK_S = 300.0


class CorrectionFreeze(StrEnum):
    """Why the correction does not learn now."""

    FEEDBACK_FROZEN = "feedback_frozen"
    CATCH_UP = "catch_up"
    SETTLING = "settling"
    NO_MEASUREMENT = "no_measurement"


class FlowCorrection:
    def __init__(self) -> None:
        self.correction_k = 0.0
        self.frozen: CorrectionFreeze | None = None
        self._last_ts: float | None = None

    def update(
        self,
        measured_c: float | None,
        effective_c: float | None,
        frozen: CorrectionFreeze | None,
        now_ts: float,
    ) -> None:
        """Move the correction towards measured minus effective curve."""
        dt = (
            0.0
            if self._last_ts is None
            else max(0.0, min(MAX_TICK_S, now_ts - self._last_ts))
        )
        self._last_ts = now_ts
        if frozen is None and (measured_c is None or effective_c is None):
            frozen = CorrectionFreeze.NO_MEASUREMENT
        self.frozen = frozen
        if frozen is not None:
            return
        gap = measured_c - effective_c
        share = 1 - math.exp(-dt / TIME_CONSTANT_S)
        value = self.correction_k + share * (gap - self.correction_k)
        self.correction_k = max(-LIMIT_K, min(LIMIT_K, value))

    def reset(self) -> None:
        self.correction_k = 0.0

    def export(self) -> dict[str, Any]:
        return {"correction_k": round(self.correction_k, 3)}

    def restore(self, data: dict[str, Any]) -> None:
        value = data.get("correction_k", 0.0)
        if not isinstance(value, int | float) or not math.isfinite(value):
            value = 0.0
        self.correction_k = max(-LIMIT_K, min(LIMIT_K, float(value)))

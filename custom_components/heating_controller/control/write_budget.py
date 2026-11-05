"""Budget for writes to a heat source's cloud API.

Clouds such as Daikin Onecta allow a fixed number of calls per day, shared
by the integration's polling and every write. A required write (fallback,
forced charge, legionella protection) always goes out. A normal write goes
out only while the cloud still has more calls than the reserve left and the
integration has not used up its own daily maximum.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class WritePriority(StrEnum):
    REQUIRED = "required"
    NORMAL = "normal"


def _utc_day(now_ts: float) -> str:
    # Onecta resets its daily counter at midnight UTC.
    return datetime.fromtimestamp(now_ts, UTC).date().isoformat()


@dataclass
class WriteBudget:
    reserve: int
    max_per_day: int
    day: str = ""
    used: int = 0
    dropped: int = 0

    def _roll(self, now_ts: float) -> None:
        day = _utc_day(now_ts)
        if day != self.day:
            self.day = day
            self.used = 0
            self.dropped = 0

    def counts(self, now_ts: float) -> tuple[int, int]:
        """Own writes and dropped writes of the current day."""
        self._roll(now_ts)
        return self.used, self.dropped

    def request(
        self, priority: WritePriority, remaining_calls: int | None, now_ts: float
    ) -> bool:
        """Whether a write may go out now; counts it, or counts the drop."""
        self._roll(now_ts)
        allowed = priority == WritePriority.REQUIRED or (
            self.used < self.max_per_day
            and (remaining_calls is None or remaining_calls > self.reserve)
        )
        if allowed:
            self.used += 1
        else:
            self.dropped += 1
        return allowed

    def export(self) -> dict[str, Any]:
        return {"day": self.day, "used": self.used, "dropped": self.dropped}

    def restore(self, data: dict[str, Any]) -> None:
        self.day = str(data.get("day", ""))
        self.used = int(data.get("used", 0))
        self.dropped = int(data.get("dropped", 0))

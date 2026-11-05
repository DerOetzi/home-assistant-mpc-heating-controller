"""Heating season: how long the lead rooms asked for heat, and what follows.

Keeps the periods in which the hub's heating demand was on (the lead rooms'
hold flow at or above the heat pump's lowest useful flow), sums them over
the last 24 h and keeps the last time that sum was above the hold threshold.
From these it recommends switching the heating on or off; it never switches
anything itself.
"""

from __future__ import annotations

from typing import Any

DAY_S = 24 * 3600


class HeatingSeason:
    def __init__(self) -> None:
        # (start, end); end None while the demand is on.
        self._periods: list[tuple[float, float | None]] = []
        self.last_demand_ts: float | None = None
        self._alive_ts: float | None = None

    @property
    def demand_running(self) -> bool:
        return bool(self._periods) and self._periods[-1][1] is None

    def update(
        self, demand: bool | None, now_ts: float, hold_threshold_h: float
    ) -> None:
        """Take the current demand; unknown counts as no demand."""
        if demand and not self.demand_running:
            self._periods.append((now_ts, None))
        elif not demand and self.demand_running:
            self._periods[-1] = (self._periods[-1][0], now_ts)
        self._periods = [
            (start, end)
            for start, end in self._periods
            if end is None or end > now_ts - DAY_S
        ]
        self._alive_ts = now_ts
        if self.hours(now_ts) > hold_threshold_h:
            self.last_demand_ts = now_ts

    def hours(self, now_ts: float) -> float:
        """Hours with demand in the last 24 h."""
        window_start = now_ts - DAY_S
        total_s = 0.0
        for start, end in self._periods:
            end_ts = now_ts if end is None else min(end, now_ts)
            total_s += max(0.0, end_ts - max(start, window_start))
        return total_s / 3600

    def switch_on_recommended(
        self, now_ts: float, on_threshold_h: float, heating_on: bool | None
    ) -> bool:
        """Demand over the switch-on threshold while the heating is not on."""
        return heating_on is not True and self.hours(now_ts) > on_threshold_h

    def switch_off_recommended(
        self, now_ts: float, off_after_h: float, heating_on: bool | None
    ) -> bool:
        """No demand over the hold threshold for off_after_h while heating."""
        return (
            heating_on is not False
            and self.last_demand_ts is not None
            and now_ts - self.last_demand_ts >= off_after_h * 3600
        )

    def export(self) -> dict[str, Any]:
        return {
            "periods": [[start, end] for start, end in self._periods],
            "last_demand_ts": self.last_demand_ts,
            "alive_ts": self._alive_ts,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Load a saved state; a running period ends where the save last saw it.

        The time HA was down counts as no demand, like a history_stats helper
        that sees the sensor unavailable.
        """
        alive_ts = data.get("alive_ts")
        periods: list[tuple[float, float | None]] = []
        for start, end in data.get("periods", []):
            if end is None:
                if alive_ts is None:
                    continue
                end = max(float(start), float(alive_ts))
            periods.append((float(start), float(end)))
        self._periods = periods
        last = data.get("last_demand_ts")
        self.last_demand_ts = None if last is None else float(last)
        self._alive_ts = None if alive_ts is None else float(alive_ts)

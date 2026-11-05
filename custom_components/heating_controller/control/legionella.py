"""Legionella protection: a weekly high-temperature cycle, verified.

Port of the "Legionella Protection Schedule" blueprint. On the scheduled
weekdays the cycle starts at the start time when the PV forecast condition
holds, otherwise at the fallback time regardless of it. It raises the hot
water target (the hot water controller's legionella phase), asks the
circulation for a continuous run, and counts as done once the outlet has
reached its threshold and the circulation return has held its own for the
hold time. A cycle that cannot start, or whose verification times out,
counts as failed until the next successful one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any

ALREADY_HOT_MARGIN_K = 3.0
DEADLINE_GRACE_MIN = 5


class LegionellaState(StrEnum):
    IDLE = "idle"
    WAITING = "waiting"  # target requested, not yet applied by the heater
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class LegionellaFailure(StrEnum):
    NOT_STARTED = "not_started"
    OUTLET = "outlet"
    RETURN = "return"


@dataclass(frozen=True)
class LegionellaConfig:
    weekdays: frozenset[int] = frozenset({1})  # Monday = 0
    start: time = time(13, 0)
    fallback: time | None = time(15, 0)
    retry_window_min: float = 120.0
    forecast_above: bool = True
    forecast_threshold: float = 0.0
    outlet_threshold_c: float = 60.0
    return_threshold_c: float | None = 58.0
    hold_min: float = 30.0
    verify_timeout_min: float = 240.0


@dataclass(frozen=True)
class LegionellaInputs:
    now: datetime
    outlet_c: float | None
    return_c: float | None
    heater_off: bool
    forecast: float | None
    target_c: float | None


@dataclass
class LegionellaCycle:
    state: LegionellaState = LegionellaState.IDLE
    failure: LegionellaFailure | None = None
    last_start: datetime | None = None
    last_success: datetime | None = None
    started: datetime | None = None
    outlet_ok: bool = False
    return_ok: bool = False
    return_hot_since: datetime | None = None
    forced: bool = False


class LegionellaController:
    def __init__(self, config: LegionellaConfig) -> None:
        self.config = config
        self.cycle = LegionellaCycle()

    @property
    def active(self) -> bool:
        """The hot water target has to be the legionella temperature."""
        return self.cycle.state in (LegionellaState.WAITING, LegionellaState.RUNNING)

    @property
    def wants_circulation(self) -> bool:
        return self.cycle.state is LegionellaState.RUNNING

    @property
    def failed(self) -> bool:
        return self.cycle.state is LegionellaState.FAILED

    def start_now(self) -> None:
        """Start by hand, regardless of schedule and forecast."""
        self.cycle.state = LegionellaState.WAITING
        self.cycle.forced = True
        self.cycle.failure = None

    def _windows(self, now: datetime) -> list[tuple[datetime, datetime]]:
        cfg = self.config
        window = timedelta(minutes=cfg.retry_window_min)
        times = [cfg.start] + ([cfg.fallback] if cfg.fallback else [])
        return [
            (start, start + window)
            for start in (datetime.combine(now.date(), t, now.tzinfo) for t in times)
        ]

    def step(self, inputs: LegionellaInputs) -> None:
        cfg = self.config
        cycle = self.cycle
        now = inputs.now
        outlet = inputs.outlet_c
        already_hot = (
            outlet is not None
            and outlet >= cfg.outlet_threshold_c - ALREADY_HOT_MARGIN_K
        )
        started_today = (
            cycle.last_start is not None and cycle.last_start.date() == now.date()
        )
        scheduled_day = now.weekday() in cfg.weekdays

        if cycle.state in (
            LegionellaState.IDLE,
            LegionellaState.DONE,
            LegionellaState.FAILED,
        ):
            if scheduled_day and not started_today and not inputs.heater_off:
                windows = self._windows(now)
                in_window = any(start <= now <= end for start, end in windows)
                fallback_reached = cfg.fallback is not None and now >= windows[-1][0]
                forecast_ok = inputs.forecast is not None and (
                    inputs.forecast > cfg.forecast_threshold
                    if cfg.forecast_above
                    else inputs.forecast < cfg.forecast_threshold
                )
                if in_window and not already_hot and (forecast_ok or fallback_reached):
                    cycle.state = LegionellaState.WAITING
                    cycle.forced = False
                elif (
                    not already_hot
                    and not started_today
                    and now >= windows[-1][1] + timedelta(minutes=DEADLINE_GRACE_MIN)
                    and cycle.failure is not LegionellaFailure.NOT_STARTED
                    and (
                        cycle.last_success is None
                        or cycle.last_success.date() != now.date()
                    )
                ):
                    cycle.state = LegionellaState.FAILED
                    cycle.failure = LegionellaFailure.NOT_STARTED
            return

        if cycle.state is LegionellaState.WAITING:
            if inputs.heater_off and not cycle.forced:
                cycle.state = LegionellaState.IDLE
                return
            if (
                inputs.target_c is not None
                and inputs.target_c >= cfg.outlet_threshold_c
            ):
                cycle.state = LegionellaState.RUNNING
                cycle.started = now
                cycle.last_start = now
                cycle.outlet_ok = False
                cycle.return_ok = cfg.return_threshold_c is None
                cycle.return_hot_since = None
                return
            # Not applied within the retry window (cloud down, say): give up.
            if not cycle.forced and not any(
                start <= now <= end for start, end in self._windows(now)
            ):
                cycle.state = LegionellaState.FAILED
                cycle.failure = LegionellaFailure.NOT_STARTED
            return

        # RUNNING: outlet and return checked side by side; both stick.
        if outlet is not None and outlet >= cfg.outlet_threshold_c:
            cycle.outlet_ok = True
        if not cycle.return_ok and cfg.return_threshold_c is not None:
            if (
                inputs.return_c is not None
                and inputs.return_c >= cfg.return_threshold_c
            ):
                if cycle.return_hot_since is None:
                    cycle.return_hot_since = now
                if now - cycle.return_hot_since >= timedelta(minutes=cfg.hold_min):
                    cycle.return_ok = True
            else:
                cycle.return_hot_since = None
        if cycle.outlet_ok and cycle.return_ok:
            cycle.state = LegionellaState.DONE
            cycle.failure = None
            cycle.last_success = now
        elif cycle.started is not None and now - cycle.started >= timedelta(
            minutes=cfg.verify_timeout_min
        ):
            cycle.state = LegionellaState.FAILED
            cycle.failure = (
                LegionellaFailure.OUTLET
                if not cycle.outlet_ok
                else LegionellaFailure.RETURN
            )

    def export(self) -> dict[str, Any]:
        c = self.cycle
        return {
            "state": c.state.value,
            "failure": c.failure.value if c.failure else None,
            "last_start": c.last_start.isoformat() if c.last_start else None,
            "last_success": c.last_success.isoformat() if c.last_success else None,
            "started": c.started.isoformat() if c.started else None,
            "forced": c.forced,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restore results; a running cycle goes on with its verification."""
        c = self.cycle
        c.state = LegionellaState(data.get("state", "idle"))
        c.forced = bool(data.get("forced", False))
        c.started = (
            datetime.fromisoformat(data["started"]) if data.get("started") else None
        )
        if c.state is LegionellaState.RUNNING and c.started is None:
            c.state = LegionellaState.WAITING
        c.return_ok = self.config.return_threshold_c is None
        c.failure = LegionellaFailure(data["failure"]) if data.get("failure") else None
        c.last_start = (
            datetime.fromisoformat(data["last_start"])
            if data.get("last_start")
            else None
        )
        c.last_success = (
            datetime.fromisoformat(data["last_success"])
            if data.get("last_success")
            else None
        )

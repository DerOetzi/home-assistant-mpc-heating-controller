"""Hot water circulation pump, controlled by the measured return.

Port of the "Domestic Hot Water Circulation Control (Thermostatic)"
blueprint. Each evaluation decides the pump from the current values, first
match wins:

0. Main switch off -> off, nothing else is evaluated (e.g. pump under repair).
1. Continuous run requested -> on. Released once the return has held the
   release temperature for the release time (loop disinfected).
2. Water heater blocked, or presence/day mode off -> off (a draw may still
   start a pulse when draws override the gates).
3. No return value -> fixed on/off interval.
4. Running -> off once the return reaches the stop target or the maximum run
   time is over.
5. Off for at least the minimum pause -> on when the return dropped below
   the start threshold (peak or off-peak), or on a draw while not hot.

Between switches the decision keeps the reason of the last one, as long as the
pump still is in the state that reason set. IDLE only remains when that is
unknown (after a start, or the pump was switched from outside).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from enum import StrEnum

from .hot_water import in_window


class CirculationReason(StrEnum):
    DISABLED = "disabled"
    CONTINUOUS = "continuous"
    BLOCKED = "blocked"
    GATES_CLOSED = "gates_closed"
    FALLBACK = "fallback"
    HOT = "hot"
    MAX_RUN = "max_run"
    COOLED = "cooled"
    DRAW = "draw"
    IDLE = "idle"


@dataclass(frozen=True)
class CirculationConfig:
    peak1_start: time = time(6, 0)
    peak1_end: time = time(9, 30)
    peak2_start: time = time(17, 0)
    peak2_end: time = time(22, 0)
    peak_start_below_c: float = 40.0
    offpeak_start_below_c: float = 35.0
    stop_delta_k: float = 4.0
    stop_temperature_c: float = 44.0
    max_run_min: float = 3.0
    min_pause_min: float = 5.0
    fallback_on_min: float = 2.0
    fallback_off_min: float = 15.0
    draw_overrides_gates: bool = False
    release_enabled: bool = False
    release_temperature_c: float = 58.0
    release_min: float = 30.0


@dataclass(frozen=True)
class CirculationInputs:
    now: datetime
    enabled: bool
    pump_on: bool
    pump_minutes: float
    return_c: float | None
    outlet_c: float | None
    continuous: bool
    heater_ok: bool
    people_gates_open: bool
    draw: bool


@dataclass(frozen=True)
class CirculationDecision:
    pump_on: bool | None  # None: leave as is
    reason: CirculationReason
    release: bool = False


class CirculationController:
    def __init__(self, config: CirculationConfig) -> None:
        self.config = config
        self._release_hot_since: datetime | None = None
        self._last: tuple[CirculationReason, bool] | None = None

    def decide(self, inputs: CirculationInputs) -> CirculationDecision:
        decision = self._decide(inputs)
        if decision.reason is not CirculationReason.IDLE:
            pump_on = inputs.pump_on if decision.pump_on is None else decision.pump_on
            self._last = (decision.reason, pump_on)
        elif self._last is not None and self._last[1] == inputs.pump_on:
            decision = replace(decision, reason=self._last[0])
        return decision

    def _decide(self, inputs: CirculationInputs) -> CirculationDecision:
        cfg = self.config
        ret = inputs.return_c

        if not inputs.enabled:
            self._release_hot_since = None
            return CirculationDecision(False, CirculationReason.DISABLED)

        if inputs.continuous:
            release = False
            if (
                cfg.release_enabled
                and ret is not None
                and ret >= cfg.release_temperature_c
            ):
                if self._release_hot_since is None:
                    self._release_hot_since = inputs.now
                release = (
                    inputs.now - self._release_hot_since
                ).total_seconds() >= cfg.release_min * 60
            else:
                self._release_hot_since = None
            if release:
                self._release_hot_since = None
            return CirculationDecision(True, CirculationReason.CONTINUOUS, release)
        self._release_hot_since = None

        draw_only = not inputs.people_gates_open and cfg.draw_overrides_gates
        if not inputs.heater_ok:
            return CirculationDecision(False, CirculationReason.BLOCKED)
        if not (inputs.people_gates_open or draw_only):
            return CirculationDecision(False, CirculationReason.GATES_CLOSED)

        if ret is None:
            if inputs.pump_on and inputs.pump_minutes >= cfg.fallback_on_min:
                return CirculationDecision(False, CirculationReason.FALLBACK)
            if not inputs.pump_on and (
                (inputs.pump_minutes >= cfg.fallback_off_min and not draw_only)
                or (inputs.draw and inputs.pump_minutes >= cfg.min_pause_min)
            ):
                return CirculationDecision(True, CirculationReason.FALLBACK)
            return CirculationDecision(None, CirculationReason.FALLBACK)

        stop_target = (
            inputs.outlet_c - cfg.stop_delta_k
            if inputs.outlet_c is not None
            else cfg.stop_temperature_c
        )
        clock = inputs.now.time()
        in_peak = in_window(clock, cfg.peak1_start, cfg.peak1_end) or in_window(
            clock, cfg.peak2_start, cfg.peak2_end
        )
        start_below = min(
            cfg.peak_start_below_c if in_peak else cfg.offpeak_start_below_c,
            stop_target - 2,
        )

        if inputs.pump_on:
            if ret >= stop_target:
                return CirculationDecision(False, CirculationReason.HOT)
            if inputs.pump_minutes >= cfg.max_run_min:
                return CirculationDecision(False, CirculationReason.MAX_RUN)
            return CirculationDecision(None, CirculationReason.IDLE)

        if inputs.pump_minutes >= cfg.min_pause_min:
            if ret < start_below and not draw_only:
                return CirculationDecision(True, CirculationReason.COOLED)
            if inputs.draw and ret < stop_target - 1:
                return CirculationDecision(True, CirculationReason.DRAW)
        return CirculationDecision(None, CirculationReason.IDLE)

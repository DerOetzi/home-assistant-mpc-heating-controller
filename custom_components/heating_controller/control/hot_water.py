"""Domestic hot water: mode, target and charge detection.

Port of the "Domestic Hot Water Controller" blueprint. Chooses between a PV
surplus mode and the normal mode, and turns the mode, a charge window, a
night setback, a boost and the legionella cycle into the water heater's
target. Gates, release threshold and minimum hold time keep the surplus mode
from oscillating against its own load.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum

from .write_budget import WritePriority


class HotWaterMode(StrEnum):
    NORMAL = "normal"
    SURPLUS = "surplus"


class ManualMode(StrEnum):
    AUTO = "auto"
    NORMAL = "normal"
    SURPLUS = "surplus"


class HotWaterPhase(StrEnum):
    LEGIONELLA = "legionella"
    BOOST = "boost"
    SURPLUS = "surplus"
    CHARGE_WINDOW = "charge_window"
    NIGHT = "night"
    NORMAL = "normal"


# Phases that raise the target to at least tank + hysteresis + 1, so a
# heater that only reheats below target - hysteresis starts at once.
FORCED_PHASES = (
    HotWaterPhase.LEGIONELLA,
    HotWaterPhase.BOOST,
    HotWaterPhase.SURPLUS,
    HotWaterPhase.CHARGE_WINDOW,
)


@dataclass(frozen=True)
class HotWaterConfig:
    normal_c: float = 46.0
    surplus_c: float = 57.0
    boost_c: float = 60.0
    legionella_c: float = 60.0
    boost_max_s: float = 6 * 3600
    night_enabled: bool = True
    night_start: time = time(21, 0)
    night_end: time = time(5, 0)
    night_c: float = 38.0
    window_enabled: bool = True
    window_start: time = time(13, 0)
    window_end: time = time(16, 0)
    window_c: float = 53.0
    forced_charge: bool = True
    reheat_hysteresis_k: float = 5.0
    max_setpoint_c: float = 60.0
    grid_lower_w: float = 3000.0
    grid_upper_w: float = 5000.0
    grid_release_w: float = 1000.0
    grid_stable_s: float = 300.0
    battery_soc_min: float | None = 50.0
    discharge_max_w: float | None = 300.0
    discharge_stable_s: float = 120.0
    outdoor_lower_c: float = 12.0
    outdoor_upper_c: float = 24.0
    tank_gate_c: float | None = 40.0
    min_hold_s: float = 900.0
    manual_hold_s: float = 3600.0


@dataclass(frozen=True)
class HotWaterInputs:
    now: datetime
    grid_export_w: float | None
    battery_soc: float | None
    battery_discharge_w: float | None
    outdoor_c: float | None
    tank_c: float | None
    current_target_c: float | None
    boost_on: bool
    legionella_active: bool


@dataclass(frozen=True)
class HotWaterDecision:
    mode: HotWaterMode
    phase: HotWaterPhase
    target_c: float
    write: float | None
    priority: WritePriority


def in_window(now: time, start: time, end: time) -> bool:
    """Whether now lies in [start, end); the window may cross midnight."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class _Debounce:
    """A condition that counts only once it has held for a while."""

    def __init__(self) -> None:
        self.state: bool | None = None
        self._pending: bool | None = None
        self._since: float = 0.0

    def update(self, raw: bool | None, now_ts: float, hold_s: float) -> bool | None:
        if raw is None:
            return self.state
        if self.state is None:
            self.state = raw
        if raw == self.state:
            self._pending = None
            return self.state
        if self._pending != raw:
            self._pending, self._since = raw, now_ts
        if now_ts - self._since >= hold_s:
            self.state, self._pending = raw, None
        return self.state


class HotWaterController:
    def __init__(self, config: HotWaterConfig) -> None:
        self.config = config
        self.mode = HotWaterMode.NORMAL
        self.mode_changed_ts = 0.0
        self.manual = ManualMode.AUTO
        self.manual_since_ts = 0.0
        self.boost_since_ts: float | None = None
        self._grid_upper_since: float | None = None
        self._discharge_low = _Debounce()

    def set_manual(self, manual: ManualMode, now_ts: float) -> None:
        self.manual = manual
        self.manual_since_ts = now_ts

    def boost_expired(self, boost_on: bool, now_ts: float) -> bool:
        """Track the boost; True once it ran longer than allowed."""
        if not boost_on:
            self.boost_since_ts = None
            return False
        if self.boost_since_ts is None:
            self.boost_since_ts = now_ts
        return now_ts - self.boost_since_ts >= self.config.boost_max_s

    def _surplus_wanted(self, inputs: HotWaterInputs, now_ts: float) -> bool:
        cfg = self.config
        grid = inputs.grid_export_w
        # Above the upper threshold counts only once it held for the
        # stabilize time; dropping below ends it at once.
        if grid is not None and grid > cfg.grid_upper_w:
            if self._grid_upper_since is None:
                self._grid_upper_since = now_ts
        elif grid is not None:
            self._grid_upper_since = None
        upper_stable = (
            self._grid_upper_since is not None
            and now_ts - self._grid_upper_since >= cfg.grid_stable_s
        )
        discharge_ok = True
        if cfg.discharge_max_w is not None:
            low = self._discharge_low.update(
                None
                if inputs.battery_discharge_w is None
                else inputs.battery_discharge_w < cfg.discharge_max_w,
                now_ts,
                cfg.discharge_stable_s,
            )
            discharge_ok = bool(low)
        if grid is None:
            return False
        above = grid > cfg.grid_lower_w or (
            self.mode is HotWaterMode.SURPLUS and grid > cfg.grid_release_w
        )
        battery_ok = cfg.battery_soc_min is None or (
            inputs.battery_soc is not None and inputs.battery_soc > cfg.battery_soc_min
        )
        tank_ok = cfg.tank_gate_c is None or (
            inputs.tank_c is not None and inputs.tank_c > cfg.tank_gate_c
        )
        outdoor = inputs.outdoor_c
        season_ok = outdoor is not None and (
            outdoor > cfg.outdoor_upper_c
            or (upper_stable and outdoor > cfg.outdoor_lower_c)
        )
        return above and battery_ok and discharge_ok and tank_ok and season_ok

    def decide(self, inputs: HotWaterInputs) -> HotWaterDecision:
        cfg = self.config
        now_ts = inputs.now.timestamp()

        if (
            self.manual is not ManualMode.AUTO
            and now_ts - self.manual_since_ts >= cfg.manual_hold_s
        ):
            self.manual = ManualMode.AUTO
        wanted = self._surplus_wanted(inputs, now_ts)
        if self.manual is ManualMode.AUTO:
            new_mode = HotWaterMode.SURPLUS if wanted else HotWaterMode.NORMAL
            if new_mode is not self.mode and (
                now_ts - self.mode_changed_ts >= cfg.min_hold_s
            ):
                self.mode, self.mode_changed_ts = new_mode, now_ts
        else:
            new_mode = HotWaterMode(self.manual.value)
            if new_mode is not self.mode:
                self.mode, self.mode_changed_ts = new_mode, now_ts

        clock = inputs.now.time()
        if inputs.legionella_active:
            phase, base = HotWaterPhase.LEGIONELLA, cfg.legionella_c
        elif inputs.boost_on:
            phase, base = HotWaterPhase.BOOST, cfg.boost_c
        elif self.mode is HotWaterMode.SURPLUS:
            phase, base = HotWaterPhase.SURPLUS, cfg.surplus_c
        elif cfg.window_enabled and in_window(clock, cfg.window_start, cfg.window_end):
            phase, base = HotWaterPhase.CHARGE_WINDOW, cfg.window_c
        elif cfg.night_enabled and in_window(clock, cfg.night_start, cfg.night_end):
            phase, base = HotWaterPhase.NIGHT, cfg.night_c
        else:
            phase, base = HotWaterPhase.NORMAL, cfg.normal_c

        forced = cfg.forced_charge and phase in FORCED_PHASES
        current = inputs.current_target_c
        if not forced:
            target = base
        elif current is not None and current >= base:
            # Keep a target already at or above the phase's own, so it does
            # not climb along with the heating tank.
            target = min(current, cfg.max_setpoint_c)
        else:
            tank = inputs.tank_c if inputs.tank_c is not None else 0.0
            target = min(
                max(base, tank + cfg.reheat_hysteresis_k + 1), cfg.max_setpoint_c
            )
        target = float(round(target))

        write = None
        if current is None or not math.isclose(current, target):
            write = target
        # Only the surplus switching may be dropped when the budget is low;
        # schedule, boost and legionella targets have to arrive.
        priority = (
            WritePriority.NORMAL
            if phase is HotWaterPhase.SURPLUS or self.mode is HotWaterMode.SURPLUS
            else WritePriority.REQUIRED
        )
        return HotWaterDecision(self.mode, phase, target, write, priority)


# --- Charge detection ------------------------------------------------------

# The tank rises about 2-4 K per 10-minute poll while charging (5 Nov:
# 42 -> 44 -> 48 -> 52 -> 54 °C).
CHARGE_TANK_RISE_K = 1.0
# A write that lifts the target this far above the tank announces a charge
# before the first poll shows it.
CHARGE_ANNOUNCE_S = 15 * 60
CHARGE_END_QUIET_S = 15 * 60


class ChargeDetector:
    """Tells a running hot-water charge from the tank and own writes.

    The heat source's cloud has no "hot water active" signal; the tank
    temperature rising between polls and a target just written above the
    reheat point are what is left.
    """

    def __init__(self, reheat_hysteresis_k: float) -> None:
        self._hysteresis = reheat_hysteresis_k
        self._last_tank: float | None = None
        self._last_rise_ts: float | None = None
        self._announced_ts: float | None = None
        self.charging = False
        self.started_ts: float | None = None

    def announce(self, target_c: float, tank_c: float | None, now_ts: float) -> None:
        """A target was written; a charge follows if it is above reheat."""
        if tank_c is not None and tank_c < target_c - self._hysteresis:
            self._announced_ts = now_ts

    def update(self, tank_c: float | None, now_ts: float) -> None:
        if tank_c is not None:
            if self._last_tank is not None and tank_c >= (
                self._last_tank + CHARGE_TANK_RISE_K
            ):
                self._last_rise_ts = now_ts
            if self._last_tank is None or not math.isclose(tank_c, self._last_tank):
                self._last_tank = tank_c
        rising = (
            self._last_rise_ts is not None
            and now_ts - self._last_rise_ts < CHARGE_END_QUIET_S
        )
        announced = (
            self._announced_ts is not None
            and now_ts - self._announced_ts < CHARGE_ANNOUNCE_S
        )
        charging = rising or announced
        if charging and not self.charging:
            self.started_ts = now_ts
        self.charging = charging

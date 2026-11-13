"""The hot water device: target, legionella cycle and circulation pump."""

from __future__ import annotations

import logging
import time as time_mod
from collections.abc import Callable
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .control.circulation import (
    CirculationConfig,
    CirculationController,
    CirculationDecision,
    CirculationInputs,
)
from .control.hot_water import (
    ChargeDetector,
    HotWaterConfig,
    HotWaterController,
    HotWaterDecision,
    HotWaterInputs,
    HotWaterMode,
    ManualMode,
)
from .control.legionella import (
    LegionellaConfig,
    LegionellaController,
    LegionellaInputs,
    LegionellaState,
)
from .control.write_budget import WritePriority

if TYPE_CHECKING:
    from .hub import HeatingHub

_LOGGER = logging.getLogger(__name__)

CIRCULATION_TICK = timedelta(seconds=15)
# A cloud shows a written target only with its next poll; the same value is
# not written again before this.
WRITE_REPEAT_S = 15 * 60
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Temperatures as numbers on the device, everything else in the subentry.
HOT_WATER_NUMBER_DEFAULTS: dict[str, float] = {
    "normal": 46.0,
    "surplus": 57.0,
    "boost": 60.0,
    "night": 38.0,
    "window": 53.0,
    "legionella": 60.0,
}

# Subentry settings and their starting values.
HOT_WATER_DEFAULTS: dict[str, Any] = {
    "grid_lower_w": 3000.0,
    "grid_upper_w": 5000.0,
    "grid_release_w": 1000.0,
    "grid_stable_min": 5.0,
    "battery_soc_min": 50.0,
    "discharge_max_w": 300.0,
    "discharge_stable_min": 2.0,
    "outdoor_lower_c": 12.0,
    "outdoor_upper_c": 24.0,
    "tank_gate_c": 40.0,
    "min_hold_min": 15.0,
    "manual_hold_min": 60.0,
    "boost_max_h": 6.0,
    "night_enabled": True,
    "night_start": "21:00:00",
    "night_end": "05:00:00",
    "window_enabled": True,
    "window_start": "13:00:00",
    "window_end": "16:00:00",
    "forced_charge": True,
    "reheat_hysteresis_k": 5.0,
    "max_setpoint_c": 60.0,
    "legionella_enabled": False,
    "legionella_weekdays": ["tue"],
    "legionella_start": "13:00:00",
    "legionella_fallback": "15:00:00",
    "legionella_retry_min": 120.0,
    "forecast_above": True,
    "forecast_threshold": 0.0,
    "outlet_threshold_c": 60.0,
    "return_threshold_c": 58.0,
    "legionella_hold_min": 30.0,
    "legionella_timeout_min": 240.0,
    "circulation_enabled": False,
    "blocked_states": ["off"],
    "peak1_start": "06:00:00",
    "peak1_end": "09:30:00",
    "peak2_start": "17:00:00",
    "peak2_end": "22:00:00",
    "peak_start_below_c": 40.0,
    "offpeak_start_below_c": 35.0,
    "stop_delta_k": 4.0,
    "stop_temperature_c": 44.0,
    "max_run_min": 3.0,
    "min_pause_min": 5.0,
    "fallback_on_min": 2.0,
    "fallback_off_min": 15.0,
    "draw_overrides_gates": False,
    "release_enabled": True,
    "release_temperature_c": 58.0,
    "release_min": 30.0,
}


def _time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


class HotWaterDevice:
    def __init__(
        self, hass: HomeAssistant, hub: HeatingHub, subentry: ConfigSubentry
    ) -> None:
        self.hass = hass
        self.hub = hub
        self.subentry_id = subentry.subentry_id
        self.title = subentry.title
        self.data: dict[str, Any] = {**HOT_WATER_DEFAULTS, **subentry.data}
        # Optional, so not taken from the defaults when cleared.
        self._fallback = subentry.data.get("legionella_fallback")
        d = self.data
        self.water_heater: str = d["water_heater_entity"]
        self.tank_entity: str | None = d.get("tank_temperature_entity")
        self.legionella_enabled = bool(d["legionella_enabled"])
        self.circulation_enabled = bool(d["circulation_enabled"]) and bool(
            d.get("pump_entity")
        )
        self.numbers: dict[str, float] = dict(HOT_WATER_NUMBER_DEFAULTS)
        self.controller = HotWaterController(self._config())
        self.detector = ChargeDetector(float(d["reheat_hysteresis_k"]))
        self.legionella = LegionellaController(self._legionella_config())
        self.circulation = CirculationController(self._circulation_config())
        self.boost_on = False
        self.continuous_request = False
        self.circulation_main = True
        self.decision: HotWaterDecision | None = None
        self.circulation_decision: CirculationDecision | None = None
        self.last_written: float | None = None
        self.last_write_ts: float | None = None
        self.last_error: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsub: list[Callable[[], None]] = []
        self._context = Context()
        self._store: Store[dict] = Store(
            hass, 1, f"{DOMAIN}_{hub.entry.entry_id}_{self.subentry_id}_hot_water"
        )

    # --- Configuration -----------------------------------------------------

    def _config(self) -> HotWaterConfig:
        d, n = self.data, self.numbers

        def gate(entity_key: str, value_key: str) -> float | None:
            return float(d[value_key]) if d.get(entity_key) else None

        return HotWaterConfig(
            normal_c=n["normal"],
            surplus_c=n["surplus"],
            boost_c=n["boost"],
            legionella_c=n["legionella"],
            night_c=n["night"],
            window_c=n["window"],
            boost_max_s=float(d["boost_max_h"]) * 3600,
            night_enabled=bool(d["night_enabled"]),
            night_start=_time(d["night_start"]),
            night_end=_time(d["night_end"]),
            window_enabled=bool(d["window_enabled"]),
            window_start=_time(d["window_start"]),
            window_end=_time(d["window_end"]),
            forced_charge=bool(d["forced_charge"]),
            reheat_hysteresis_k=float(d["reheat_hysteresis_k"]),
            max_setpoint_c=float(d["max_setpoint_c"]),
            grid_lower_w=float(d["grid_lower_w"]),
            grid_upper_w=float(d["grid_upper_w"]),
            grid_release_w=float(d["grid_release_w"]),
            grid_stable_s=float(d["grid_stable_min"]) * 60,
            battery_soc_min=gate("battery_soc_entity", "battery_soc_min"),
            discharge_max_w=gate("battery_discharge_entity", "discharge_max_w"),
            discharge_stable_s=float(d["discharge_stable_min"]) * 60,
            outdoor_lower_c=float(d["outdoor_lower_c"]),
            outdoor_upper_c=float(d["outdoor_upper_c"]),
            tank_gate_c=(
                float(d["tank_gate_c"]) if d.get("tank_gate_c") is not None else None
            ),
            min_hold_s=float(d["min_hold_min"]) * 60,
            manual_hold_s=float(d["manual_hold_min"]) * 60,
        )

    def _legionella_config(self) -> LegionellaConfig:
        d = self.data
        return LegionellaConfig(
            weekdays=frozenset(WEEKDAYS.index(day) for day in d["legionella_weekdays"]),
            start=_time(d["legionella_start"]),
            fallback=_time(self._fallback),
            retry_window_min=float(d["legionella_retry_min"]),
            forecast_above=bool(d["forecast_above"]),
            forecast_threshold=float(d["forecast_threshold"]),
            outlet_threshold_c=float(d["outlet_threshold_c"]),
            return_threshold_c=(
                float(d["return_threshold_c"]) if d.get("return_entity") else None
            ),
            hold_min=float(d["legionella_hold_min"]),
            verify_timeout_min=float(d["legionella_timeout_min"]),
        )

    def _circulation_config(self) -> CirculationConfig:
        d = self.data
        return CirculationConfig(
            peak1_start=_time(d["peak1_start"]),
            peak1_end=_time(d["peak1_end"]),
            peak2_start=_time(d["peak2_start"]),
            peak2_end=_time(d["peak2_end"]),
            peak_start_below_c=float(d["peak_start_below_c"]),
            offpeak_start_below_c=float(d["offpeak_start_below_c"]),
            stop_delta_k=float(d["stop_delta_k"]),
            stop_temperature_c=float(d["stop_temperature_c"]),
            max_run_min=float(d["max_run_min"]),
            min_pause_min=float(d["min_pause_min"]),
            fallback_on_min=float(d["fallback_on_min"]),
            fallback_off_min=float(d["fallback_off_min"]),
            draw_overrides_gates=bool(d["draw_overrides_gates"]),
            release_enabled=bool(d["release_enabled"]),
            release_temperature_c=float(d["release_temperature_c"]),
            release_min=float(d["release_min"]),
        )

    def set_number(self, key: str, value: float) -> None:
        if key not in HOT_WATER_NUMBER_DEFAULTS:
            raise ValueError(f"Unknown hot water number {key}")
        self.numbers[key] = float(value)
        self.controller.config = self._config()
        self._schedule_evaluate()

    # --- Setup -------------------------------------------------------------

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self.legionella.restore(stored.get("legionella", {}))
        self.controller.mode = HotWaterMode(stored.get("mode", HotWaterMode.NORMAL))
        self.controller.mode_changed_ts = float(stored.get("mode_changed_ts", 0.0))
        self.continuous_request = bool(stored.get("continuous_request", False))
        self.circulation_main = bool(stored.get("circulation_main", True))
        if self.circulation_enabled:
            d = self.data
            tracked = [
                entity
                for entity in [
                    d.get("pump_entity"),
                    d.get("circ_return_entity"),
                    d.get("circ_outlet_entity"),
                    d.get("presence_entity"),
                    d.get("daymode_entity"),
                    self.water_heater,
                    *(d.get("continuous_entities") or []),
                    *(d.get("draw_entities") or []),
                ]
                if entity
            ]
            self._unsub.append(
                async_track_state_change_event(
                    self.hass, tracked, self._async_circulation_state_changed
                )
            )
            self._unsub.append(
                async_track_time_interval(
                    self.hass, self._async_circulation_tick, CIRCULATION_TICK
                )
            )

    def async_unload(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []

    async def async_flush(self) -> None:
        await self._store.async_save(self._export())

    def _export(self) -> dict[str, Any]:
        return {
            "legionella": self.legionella.export(),
            "mode": self.controller.mode.value,
            "mode_changed_ts": self.controller.mode_changed_ts,
            "continuous_request": self.continuous_request,
            "circulation_main": self.circulation_main,
        }

    # --- Readings ----------------------------------------------------------

    def _state(self, entity_id: str | None) -> State | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return state

    def _float(self, entity_id: str | None) -> float | None:
        state = self._state(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    @property
    def current_target_c(self) -> float | None:
        state = self._state(self.water_heater)
        if state is None:
            return None
        value = state.attributes.get("temperature")
        return float(value) if isinstance(value, int | float) else None

    @property
    def tank_c(self) -> float | None:
        if self.tank_entity:
            return self._float(self.tank_entity)
        state = self._state(self.water_heater)
        if state is None:
            return None
        value = state.attributes.get("current_temperature")
        return float(value) if isinstance(value, int | float) else None

    @property
    def heater_off(self) -> bool:
        state = self.hass.states.get(self.water_heater)
        return state is not None and state.state == STATE_OFF

    @property
    def charging(self) -> bool:
        return self.detector.charging

    # --- Commands ----------------------------------------------------------

    def set_boost(self, on: bool) -> None:
        self.boost_on = on
        self._schedule_evaluate()

    def set_manual(self, manual: ManualMode) -> None:
        self.controller.set_manual(manual, time_mod.time())
        self._schedule_evaluate()

    def set_continuous_request(self, on: bool) -> None:
        self.continuous_request = on
        self._store.async_delay_save(self._export, 10)
        self._schedule_circulation()

    def set_circulation_main(self, on: bool) -> None:
        self.circulation_main = on
        self._store.async_delay_save(self._export, 10)
        self._schedule_circulation()

    def start_legionella(self) -> None:
        self.legionella.start_now()
        self._schedule_evaluate()

    def _schedule_evaluate(self) -> None:
        self.hass.async_create_task(self.async_evaluate())

    # --- Hot water and legionella (every minute) ---------------------------

    async def async_evaluate(self) -> None:
        now = dt_util.now()
        now_ts = now.timestamp()
        if self.controller.boost_expired(self.boost_on, now_ts):
            _LOGGER.info("Hot water boost ran out after its maximum duration")
            self.boost_on = False

        if self.legionella_enabled or self.legionella.cycle.forced:
            before = self.legionella.cycle.state
            self.legionella.step(
                LegionellaInputs(
                    now=now,
                    outlet_c=(
                        self._float(self.data.get("outlet_entity"))
                        if self.data.get("outlet_entity")
                        else self.tank_c
                    ),
                    return_c=self._float(self.data.get("return_entity")),
                    heater_off=self.heater_off,
                    forecast=self._float(self.data.get("forecast_entity")),
                    target_c=self.current_target_c,
                )
            )
            after = self.legionella.cycle.state
            if after is not before:
                _LOGGER.info("Legionella cycle %s -> %s", before, after)
                if after is LegionellaState.RUNNING:
                    self.continuous_request = True
                elif after is LegionellaState.FAILED:
                    self.continuous_request = False
                self._store.async_delay_save(self._export, 10)

        decision = self.controller.decide(
            HotWaterInputs(
                now=now,
                grid_export_w=self._float(self.data.get("grid_export_entity")),
                battery_soc=self._float(self.data.get("battery_soc_entity")),
                battery_discharge_w=self._float(
                    self.data.get("battery_discharge_entity")
                ),
                outdoor_c=self.hub.outdoor_temperature_c,
                tank_c=self.tank_c,
                current_target_c=self.current_target_c,
                boost_on=self.boost_on,
                legionella_active=self.legionella.active,
            )
        )
        self.decision = decision
        tank = self.tank_c
        if decision.write is not None and not self._recently_written(
            decision.write, now_ts
        ):
            await self._async_write(decision.write, decision.priority, tank, now_ts)
        self.detector.update(tank, now_ts)
        self._store.async_delay_save(self._export, 300)
        self._notify()

    def _recently_written(self, value: float, now_ts: float) -> bool:
        return (
            self.last_written == value
            and self.last_write_ts is not None
            and now_ts - self.last_write_ts < WRITE_REPEAT_S
        )

    async def _async_write(
        self,
        value: float,
        priority: WritePriority,
        tank: float | None,
        now_ts: float,
    ) -> None:
        if not self.hub.request_write(priority):
            return
        try:
            await self.hass.services.async_call(
                "water_heater",
                "set_temperature",
                {ATTR_ENTITY_ID: self.water_heater, "temperature": value},
                blocking=True,
                context=self._context,
            )
        except HomeAssistantError as err:
            self.last_error = str(err)
            _LOGGER.warning(
                "Writing %s to %s failed: %s", value, self.water_heater, err
            )
            return
        self.last_error = None
        self.last_written, self.last_write_ts = value, now_ts
        self.detector.announce(value, tank, now_ts)
        _LOGGER.info("Hot water target %s set to %s", self.water_heater, value)

    # --- Circulation (on changes and every 15 s) ---------------------------

    @callback
    def _async_circulation_state_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        new = event.data["new_state"]
        old = event.data["old_state"]
        draw = (
            event.data["entity_id"] in (self.data.get("draw_entities") or [])
            and new is not None
            and new.state == STATE_ON
            and (old is None or old.state != STATE_ON)
        )
        self._schedule_circulation(draw)

    @callback
    def _async_circulation_tick(self, _now: Any) -> None:
        self._schedule_circulation()

    def _schedule_circulation(self, draw: bool = False) -> None:
        if self.circulation_enabled:
            self.hass.async_create_task(self.async_evaluate_circulation(draw))

    @property
    def continuous(self) -> bool:
        external = any(
            (state := self.hass.states.get(entity)) is not None
            and state.state == STATE_ON
            for entity in self.data.get("continuous_entities") or []
        )
        return self.continuous_request or external or self.legionella.wants_circulation

    async def async_evaluate_circulation(self, draw: bool = False) -> None:
        d = self.data
        pump = self.hass.states.get(d["pump_entity"])
        if pump is None or pump.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        now = dt_util.now()
        pump_on = pump.state == STATE_ON
        heater = self.hass.states.get(self.water_heater)
        heater_ok = heater is not None and heater.state not in [
            *d["blocked_states"],
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ]
        gates = all(
            (state := self.hass.states.get(entity)) is not None
            and state.state == STATE_ON
            for entity in (d.get("presence_entity"), d.get("daymode_entity"))
            if entity
        )
        decision = self.circulation.decide(
            CirculationInputs(
                now=now,
                enabled=self.circulation_main,
                pump_on=pump_on,
                pump_minutes=(now - pump.last_changed).total_seconds() / 60,
                return_c=self._float(d.get("circ_return_entity")),
                outlet_c=self._float(d.get("circ_outlet_entity")),
                continuous=self.continuous,
                heater_ok=heater_ok,
                people_gates_open=gates,
                draw=draw,
            )
        )
        changed = decision != self.circulation_decision
        self.circulation_decision = decision
        if decision.release:
            _LOGGER.info("Circulation loop hot: continuous run released")
            self.continuous_request = False
            on = [
                entity
                for entity in d.get("continuous_entities") or []
                if (state := self.hass.states.get(entity)) is not None
                and state.state == STATE_ON
            ]
            if on:
                await self._async_call("homeassistant", "turn_off", on)
            self._store.async_delay_save(self._export, 10)
        if decision.pump_on is not None and decision.pump_on != pump_on:
            await self._async_call(
                "switch",
                "turn_on" if decision.pump_on else "turn_off",
                [pump.entity_id],
            )
            changed = True
        if changed:
            self._notify()

    async def _async_call(self, domain: str, service: str, entities: list[str]) -> None:
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {ATTR_ENTITY_ID: entities},
                blocking=True,
                context=self._context,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("%s.%s on %s failed: %s", domain, service, entities, err)

    # --- Listeners ---------------------------------------------------------

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

"""The hot water device: target, legionella cycle and circulation pump."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
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
    CirculationController,
    CirculationDecision,
    CirculationInputs,
)
from .control.hot_water import (
    ChargeDetector,
    HotWaterController,
    HotWaterDecision,
    HotWaterInputs,
    HotWaterMode,
    ManualMode,
)
from .control.legionella import (
    LegionellaController,
    LegionellaInputs,
    LegionellaState,
)
from .control.write_budget import WritePriority
from .hot_water_settings import (
    HOT_WATER_NUMBER_DEFAULTS,
    HotWaterKey,
    HotWaterNumber,
    HotWaterSettings,
)
from .listeners import Observable
from .state_reader import attribute_float, available_state, entity_float

if TYPE_CHECKING:
    from .hub import HeatingHub

_LOGGER = logging.getLogger(__name__)

K = HotWaterKey

CIRCULATION_TICK = timedelta(seconds=15)
# A cloud shows a written target only with its next poll; the same value is
# not written again before this.
WRITE_REPEAT_S = 15 * 60


class HotWaterDevice(Observable):
    def __init__(
        self, hass: HomeAssistant, hub: HeatingHub, subentry: ConfigSubentry
    ) -> None:
        super().__init__()
        self.hass = hass
        self.hub = hub
        self.subentry_id = subentry.subentry_id
        self.title = subentry.title
        settings = HotWaterSettings.from_data(subentry.data)
        self.settings = settings
        self.water_heater = settings.water_heater
        self.tank_entity = settings.entity(K.TANK_TEMPERATURE_ENTITY)
        self.legionella_enabled = settings.legionella_enabled
        self.circulation_enabled = settings.circulation_enabled
        self.numbers: dict[HotWaterNumber, float] = dict(HOT_WATER_NUMBER_DEFAULTS)
        self.controller = HotWaterController(settings.hot_water_config(self.numbers))
        self.detector = ChargeDetector(settings.reheat_hysteresis_k)
        self.legionella = LegionellaController(settings.legionella_config())
        self.circulation = CirculationController(settings.circulation_config())
        self.boost_on = False
        self.continuous_request = False
        self.circulation_main = True
        self.decision: HotWaterDecision | None = None
        self.circulation_decision: CirculationDecision | None = None
        self.last_written: float | None = None
        self.last_write_ts: float | None = None
        self.last_error: str | None = None
        self._unsub: list[Callable[[], None]] = []
        self._context = Context()
        self._store: Store[dict] = Store(
            hass, 1, f"{DOMAIN}_{hub.entry.entry_id}_{self.subentry_id}_hot_water"
        )

    # --- Configuration -----------------------------------------------------

    def set_number(self, key: HotWaterNumber, value: float) -> None:
        self.numbers[HotWaterNumber(key)] = float(value)
        self.controller.config = self.settings.hot_water_config(self.numbers)
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
            settings = self.settings
            tracked = [
                entity
                for entity in [
                    settings.entity(K.PUMP_ENTITY),
                    settings.entity(K.CIRC_RETURN_ENTITY),
                    settings.entity(K.CIRC_OUTLET_ENTITY),
                    settings.entity(K.PRESENCE_ENTITY),
                    settings.entity(K.DAYMODE_ENTITY),
                    self.water_heater,
                    *settings.entities(K.CONTINUOUS_ENTITIES),
                    *settings.entities(K.DRAW_ENTITIES),
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

    def _reading(self, key: HotWaterKey) -> float | None:
        """A configured sensor's number; None if unset or without one."""
        return entity_float(self.hass, self.settings.entity(key))

    @property
    def current_target_c(self) -> float | None:
        return attribute_float(
            available_state(self.hass, self.water_heater), "temperature"
        )

    @property
    def tank_c(self) -> float | None:
        if self.tank_entity:
            return entity_float(self.hass, self.tank_entity)
        return attribute_float(
            available_state(self.hass, self.water_heater), "current_temperature"
        )

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
        self.controller.set_manual(manual, time.time())
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
        if not self.hub.started:
            return
        now = dt_util.now()
        now_ts = now.timestamp()
        if self.controller.boost_expired(self.boost_on, now_ts):
            _LOGGER.info("Hot water boost ran out after its maximum duration")
            self.boost_on = False

        if self.legionella_enabled or self.legionella.cycle.forced:
            self._step_legionella(now)

        decision = self.controller.decide(
            HotWaterInputs(
                now=now,
                grid_export_w=self._reading(K.GRID_EXPORT_ENTITY),
                battery_soc=self._reading(K.BATTERY_SOC_ENTITY),
                battery_discharge_w=self._reading(K.BATTERY_DISCHARGE_ENTITY),
                outdoor_c=self.hub.outdoor_temperature_c,
                tank_c=self.tank_c,
                current_target_c=self.current_target_c,
                boost_on=self.boost_on,
                legionella_active=self.legionella.active,
                heating_season=self.hub.season.heating_on is True,
            )
        )
        self.decision = decision
        tank = self.tank_c
        if decision.write is not None and not self._recently_written(
            decision.write, now_ts
        ):
            await self._async_write(decision.write, decision.priority, tank, now_ts)
        self.detector.update(tank, now_ts)
        # A charge the tank starts reaches rooms and feedback at once.
        self.hub.update_charge()
        self._store.async_delay_save(self._export, 300)
        self._notify_listeners()

    def _step_legionella(self, now: datetime) -> None:
        """Advance the legionella cycle; its start and failure set the
        continuous request."""
        before = self.legionella.cycle.state
        self.legionella.step(
            LegionellaInputs(
                now=now,
                outlet_c=(
                    entity_float(self.hass, outlet)
                    if (outlet := self.settings.entity(K.OUTLET_ENTITY))
                    else self.tank_c
                ),
                return_c=self._reading(K.RETURN_ENTITY),
                heater_off=self.heater_off,
                forecast=self._reading(K.FORECAST_ENTITY),
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
            event.data["entity_id"] in self.settings.entities(K.DRAW_ENTITIES)
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
            for entity in self.settings.entities(K.CONTINUOUS_ENTITIES)
        )
        return self.continuous_request or external or self.legionella.wants_circulation

    async def async_evaluate_circulation(self, draw: bool = False) -> None:
        if not self.hub.started:
            return
        settings = self.settings
        pump = available_state(self.hass, settings.entity(K.PUMP_ENTITY))
        if pump is None:
            return
        pump_on = pump.state == STATE_ON
        decision = self.circulation.decide(self._circulation_inputs(pump, draw))
        changed = decision != self.circulation_decision
        self.circulation_decision = decision
        if decision.release:
            await self._async_release_continuous()
        if decision.pump_on is not None and decision.pump_on != pump_on:
            await self._async_call(
                "switch",
                "turn_on" if decision.pump_on else "turn_off",
                [pump.entity_id],
            )
            changed = True
        if changed:
            self._notify_listeners()

    def _circulation_inputs(self, pump: State, draw: bool) -> CirculationInputs:
        settings = self.settings
        now = dt_util.now()
        heater = self.hass.states.get(self.water_heater)
        heater_ok = heater is not None and heater.state not in [
            *settings.blocked_states,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ]
        gates = all(
            (state := self.hass.states.get(entity)) is not None
            and state.state == STATE_ON
            for entity in (
                settings.entity(K.PRESENCE_ENTITY),
                settings.entity(K.DAYMODE_ENTITY),
            )
            if entity
        )
        return CirculationInputs(
            now=now,
            enabled=self.circulation_main,
            pump_on=pump.state == STATE_ON,
            pump_minutes=(now - pump.last_changed).total_seconds() / 60,
            return_c=self._reading(K.CIRC_RETURN_ENTITY),
            outlet_c=self._reading(K.CIRC_OUTLET_ENTITY),
            continuous=self.continuous,
            heater_ok=heater_ok,
            people_gates_open=gates,
            draw=draw,
        )

    async def _async_release_continuous(self) -> None:
        """The loop is hot: drop the continuous request and its switches."""
        _LOGGER.info("Circulation loop hot: continuous run released")
        self.continuous_request = False
        on = [
            entity
            for entity in self.settings.entities(K.CONTINUOUS_ENTITIES)
            if (state := self.hass.states.get(entity)) is not None
            and state.state == STATE_ON
        ]
        if on:
            await self._async_call("homeassistant", "turn_off", on)
        self._store.async_delay_save(self._export, 10)

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

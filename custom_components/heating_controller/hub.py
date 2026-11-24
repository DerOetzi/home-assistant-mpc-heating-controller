"""The hub: the house-wide part of the integration.

Holds the rooms of one installation and derives the figures of the lead
rooms from them -- the hold and boost flow temperatures, the heating
demand and the reserve of the heat pump's curve.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_BUILDING_ROTATION,
    CONF_FLOW_CURVE_ENTITY,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_MAX_FLOW_GAP_MIN,
    CONF_MAX_WRITES_PER_DAY,
    CONF_OPAQUE_SURFACES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_RATE_LIMIT_REMAINING_ENTITY,
    CONF_RATE_LIMIT_RESERVE,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_WINDOW_SURFACES,
    CONF_WORKDAY_ENTITY,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_MAX_FLOW_GAP_MIN,
    DEFAULT_MAX_WRITES_PER_DAY,
    DEFAULT_RATE_LIMIT_RESERVE,
    DEFAULT_ROOF_TILT_DEG,
    RoofOrientation,
)
from .control.flow_correction import FlowCorrection
from .control.flow_feedback import FlowFeedback
from .control.hot_water import FlowChargeDetector
from .control.write_budget import WriteBudget, WritePriority
from .coordinator import HeatingRoomCoordinator
from .flow_device import FlowDevice
from .hot_water_device import HotWaterDevice
from .irradiance import IrradianceProvider, SurfaceGeometry, building_surfaces
from .lead_rooms import LEAD_ROUND_DIGITS, LeadRooms, LeadValue
from .listeners import Observable
from .season_control import SeasonControl
from .startup_gate import StartupGate
from .state_reader import entity_float
from .store import HubStore

_LOGGER = logging.getLogger(__name__)

HUB_TICK = timedelta(minutes=1)
HUB_SAVE_DELAY_S = 300


class HeatingHub(Observable):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        self.hass = hass
        self.entry = entry
        self.rooms: dict[str, HeatingRoomCoordinator] = {}
        self.leads = LeadRooms(self.rooms)
        # Room id to name of rooms left out because their device is disabled.
        self.disabled_rooms: dict[str, str] = {}
        self._flow_threshold_entity: str | None = entry.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        self.flow_curve_entity: str | None = entry.data.get(CONF_FLOW_CURVE_ENTITY)
        self._unsub: list[Callable[[], None]] = []
        self.workday_entity: str | None = entry.data.get(CONF_WORKDAY_ENTITY)
        self.irradiance: IrradianceProvider | None = None
        self.rate_limit_remaining_entity: str | None = entry.data.get(
            CONF_RATE_LIMIT_REMAINING_ENTITY
        )
        self.write_budget = WriteBudget(
            reserve=int(
                entry.data.get(CONF_RATE_LIMIT_RESERVE, DEFAULT_RATE_LIMIT_RESERVE)
            ),
            max_per_day=int(
                entry.data.get(CONF_MAX_WRITES_PER_DAY, DEFAULT_MAX_WRITES_PER_DAY)
            ),
        )
        self._flow_temperature_entity: str | None = entry.data.get(
            CONF_FLOW_TEMPERATURE_ENTITY
        )
        self._heat_source_entity: str | None = entry.data.get(
            CONF_HEAT_SOURCE_CLIMATE_ENTITY
        )
        self._max_flow_gap_s = (
            float(entry.data.get(CONF_MAX_FLOW_GAP_MIN, DEFAULT_MAX_FLOW_GAP_MIN)) * 60
        )
        # A start counts as a fresh value; the gap runs from here.
        self.flow_seen_ts = time.time()
        self.season = SeasonControl(
            hass,
            self._heat_source_entity,
            lambda: self.started and self.heat_source_reachable,
            self.request_write,
        )
        self.flow_feedback = FlowFeedback()
        self.flow_correction = FlowCorrection()
        self.charge_detector = FlowChargeDetector()
        # Nothing computes or writes before the inputs are there.
        self.startup_gate = StartupGate(hass, self._async_start)
        self._store = HubStore(hass, entry.entry_id)
        self._outdoor_entity: str | None = entry.data.get(
            CONF_OUTDOOR_TEMPERATURE_ENTITY
        )
        self.flow: FlowDevice | None = None
        self.hot_water: HotWaterDevice | None = None
        # The house-wide comfort switch; off e.g. on holiday.
        self.comfort_on = True
        # PV surplus; set by hand or by an automation.
        self.pv_boost_on = False

    def add_room(self, coordinator: HeatingRoomCoordinator) -> None:
        self.rooms[coordinator.room_id] = coordinator
        coordinator.house = self
        self._unsub.append(coordinator.async_add_listener(self._notify_listeners))

    def used_surfaces(self) -> list[SurfaceGeometry]:
        """The building's surfaces some room has windows or walls on."""
        used = {
            surface
            for room in self.rooms.values()
            for key in (CONF_WINDOW_SURFACES, CONF_OPAQUE_SURFACES)
            for surface in room.data.get(key) or []
        }
        return [
            geometry
            for geometry in building_surfaces(
                float(self.entry.data.get(CONF_BUILDING_ROTATION, 0.0)),
                RoofOrientation(
                    self.entry.data.get(CONF_ROOF_ORIENTATION, RoofOrientation.NONE)
                ),
                float(self.entry.data.get(CONF_ROOF_TILT, DEFAULT_ROOF_TILT_DEG)),
            )
            if geometry.surface in used
        ]

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        self.write_budget.restore(stored.get("write_budget", {}))
        self.season.demand.restore(stored.get("heating_season", {}))
        self.flow_feedback.restore(stored.get("flow_feedback", {}))
        self.flow_correction.restore(stored.get("flow_correction", {}))
        surfaces = self.used_surfaces()
        if surfaces:
            self.irradiance = IrradianceProvider(
                self.hass, surfaces, self._notify_listeners
            )
            self.irradiance.async_start()
            for room in self.rooms.values():
                room.gain_clock.set_sun_provider(self.irradiance, self.workday_entity)
        tracked = [
            entity_id
            for entity_id in (
                self._flow_threshold_entity,
                self.flow_curve_entity,
                self._heat_source_entity,
                self.rate_limit_remaining_entity,
            )
            if entity_id
        ]
        if tracked:
            self._unsub.append(
                async_track_state_change_event(
                    self.hass, tracked, self._async_handle_state_change
                )
            )
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_tick, HUB_TICK)
        )
        gate = self.startup_gate
        for room in self.rooms.values():
            gate.require(room.required_entities)
        gate.require([self.flow_curve_entity, self._heat_source_entity])
        if self.flow is not None:
            gate.require([self.flow.actuator_entity])

    def async_start_gate(self) -> None:
        """Called once the platforms are set up: on a reload the gate opens at
        once, so the entities that restore setpoints and switches must be in."""
        self.startup_gate.async_start()

    @property
    def started(self) -> bool:
        return self.startup_gate.is_open

    async def _async_start(self) -> None:
        for room in self.rooms.values():
            await room.async_start()
        self._tick(time.time())
        self._notify_listeners()

    def async_unload(self) -> None:
        self.startup_gate.async_stop()
        if self.hot_water is not None:
            self.hot_water.async_unload()
        if self.irradiance is not None:
            self.irradiance.async_stop()
        for unsub in self._unsub:
            unsub()
        self._unsub = []

    async def async_flush(self) -> None:
        """Write the hub's state now, e.g. before an unload."""
        await self._store.async_save(self._export())
        if self.hot_water is not None:
            await self.hot_water.async_flush()

    def _export(self) -> dict[str, Any]:
        return {
            "write_budget": self.write_budget.export(),
            "heating_season": self.season.demand.export(),
            "flow_feedback": self.flow_feedback.export(),
            "flow_correction": self.flow_correction.export(),
        }

    @callback
    def _async_tick(self, _now: Any) -> None:
        self._tick(time.time())
        self._notify_listeners()

    def _tick(self, now_ts: float) -> None:
        if not self.started:
            return
        if (
            self._flow_temperature_entity
            and entity_float(self.hass, self._flow_temperature_entity) is not None
        ):
            self.flow_seen_ts = now_ts
        self.update_charge()
        self.season.update(self.heating_demand, now_ts)
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)
        if self.hot_water is not None:
            self.hass.async_create_task(self.hot_water.async_evaluate())
        if self.flow is not None:
            self.hass.async_create_task(self.flow.async_evaluate())

    # --- Flow control ------------------------------------------------------

    def set_flow(self, flow: FlowDevice) -> None:
        """Hand the flow device the actuator; rooms take its threshold."""
        self.flow = flow

    # --- What the rooms ask the house for ----------------------------------

    def flow_threshold_override_c(self) -> float | None:
        """The flow device's threshold; None without a flow device."""
        return None if self.flow is None else self.flow.threshold_c

    def curve_outdoor_ends_c(self) -> tuple[float, float] | None:
        """The heating curve's outdoor end points (cold, warm); None without."""
        if self.flow is None:
            return None
        return (
            self.flow.numbers["curve_outdoor_cold"],
            self.flow.numbers["curve_outdoor_warm"],
        )

    def rooms_catching_up(self) -> bool:
        """The rooms catch up after a pause of the room heating."""
        return self.flow_feedback.catching_up(time.time())

    def reset_flow_feedback(self) -> None:
        """Drop the valve feedback to zero, e.g. after a change of its rules."""
        self.flow_feedback.reset(time.time())
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)

    def set_hot_water(self, hot_water: HotWaterDevice) -> None:
        self.hot_water = hot_water

    def update_charge(self) -> None:
        """Take the flow, the heat source's aim and the tank detector."""
        self.charge_detector.update(
            self.measured_flow_c,
            self.effective_curve_c,
            self.hot_water is not None and self.hot_water.charging,
            time.time(),
        )

    def room_hot_water_charge(self) -> tuple[bool, bool]:
        """The charge as rooms take it, and whether it is the house's own.

        With the measured flow and the heat source's aim the house decides
        for all rooms and the feedback alike. Without, each room adds what
        the hot water device sees to its own detection on its flow.
        """
        if self.charge_detector.decisive:
            return self.charge_detector.charging, True
        return self.hot_water is not None and self.hot_water.charging, False

    @property
    def outdoor_temperature_c(self) -> float | None:
        return entity_float(self.hass, self._outdoor_entity)

    @property
    def charge_flow_c(self) -> float | None:
        """Flow the rooms need around a planned hot-water charge; not built yet."""
        return None

    # --- Writes to the heat source -----------------------------------------

    @property
    def rate_limit_remaining(self) -> int | None:
        """Calls the heat source's cloud still allows today, if known."""
        value = entity_float(self.hass, self.rate_limit_remaining_entity)
        return None if value is None else int(value)

    def request_write(self, priority: WritePriority) -> bool:
        """Whether a write to the heat source may go out now; counts it."""
        allowed = self.write_budget.request(
            priority, self.rate_limit_remaining, time.time()
        )
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)
        self._notify_listeners()
        return allowed

    # --- Heat source reachable ---------------------------------------------

    @property
    def heat_source_reachable(self) -> bool:
        """False once the measured flow has been missing longer than the gap."""
        return time.time() - self.flow_seen_ts <= self._max_flow_gap_s

    # --- Heating season ----------------------------------------------------

    async def async_set_season_auto(self, value: bool) -> None:
        """Switch the automatic heat/off of the heat source."""
        self.season.set_auto(value)
        self._notify_listeners()

    async def async_set_comfort(self, value: bool) -> None:
        """Switch house-wide comfort; rooms that follow it recompute."""
        self.comfort_on = value
        for room in self.rooms.values():
            await room.async_set_house_comfort(value)
        self._notify_listeners()

    async def async_set_pv_boost(self, value: bool) -> None:
        """Switch the PV surplus; every room recomputes."""
        self.pv_boost_on = value
        for room in self.rooms.values():
            await room.async_set_pv_boost(value)
        self._notify_listeners()

    def set_season_threshold(self, name: str, value: float) -> None:
        """Set one of the season numbers by its short name."""
        self.season.set_threshold(name, value)
        self._notify_listeners()

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] == self._heat_source_entity:
            self.season.heat_source_changed(event)
        self._notify_listeners()

    @property
    def measured_flow_c(self) -> float | None:
        return entity_float(self.hass, self._flow_temperature_entity)

    @property
    def hot_water_charging(self) -> bool:
        """A hot-water charge takes the flow.

        One signal from the flow against the heat source's aim; without it,
        as any room or the device sees it.
        """
        if self.charge_detector.decisive:
            return self.charge_detector.charging
        if self.hot_water is not None and self.hot_water.charging:
            return True
        return any(room.mpc.hot_water_charging for room in self.rooms.values())

    @property
    def flow_threshold_c(self) -> float | None:
        """The threshold the demand compares to; None while its entity is empty."""
        if self.flow is not None:
            return self.flow.threshold_c
        if self._flow_threshold_entity:
            return entity_float(self.hass, self._flow_threshold_entity)
        return self.entry.data.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C)

    @property
    def heating_demand(self) -> bool | None:
        """The lead rooms need more than the heat pump's lowest useful flow."""
        hold_c = self.leads.hold_flow.value_c
        threshold_c = self.flow_threshold_c
        if hold_c is None or threshold_c is None:
            return None
        return hold_c >= threshold_c

    def _reserve(self, lead: LeadValue) -> float | None:
        # A lead value of 0 means all lead rooms sit in their surplus buffer;
        # the curve minus 0 would say nothing.
        if lead.value_c is None or lead.value_c <= 0:
            return None
        curve_c = self.effective_curve_c
        if curve_c is None:
            return None
        return round(curve_c - lead.value_c, LEAD_ROUND_DIGITS)

    @property
    def has_flow_curve(self) -> bool:
        return self.flow is not None or bool(self.flow_curve_entity)

    @property
    def effective_curve_c(self) -> float | None:
        """The flow the heat source aims at, from the flow device or entity."""
        if self.flow is not None:
            return self.flow.effective_curve_c
        if not self.flow_curve_entity:
            return None
        return entity_float(self.hass, self.flow_curve_entity)

    @property
    def hold_flow_reserve(self) -> float | None:
        return self._reserve(self.leads.hold_flow)

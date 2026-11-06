"""The hub: the house-wide part of the integration.

Holds the rooms of one installation and derives the figures of the lead
rooms from them -- the hold and minimum flow temperatures, the heating
demand and the reserve of the heat pump's curve.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
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
    CONF_LEAD_FLOW,
    CONF_LEAD_HEATING_LIMIT,
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
    DEFAULT_SEASON_HOLD_THRESHOLD_H,
    DEFAULT_SEASON_OFF_AFTER_H,
    DEFAULT_SEASON_ON_THRESHOLD_H,
    HEAT_SOURCE_ACTIVE_STATE,
    RoofOrientation,
)
from .control.heating_season import HeatingSeason
from .control.write_budget import WriteBudget, WritePriority
from .coordinator import HeatingRoomCoordinator
from .flow_device import FlowDevice
from .hot_water_device import HotWaterDevice
from .irradiance import IrradianceProvider, SurfaceGeometry, building_surfaces
from .store import HubStore

HUB_TICK = timedelta(minutes=1)
HUB_SAVE_DELAY_S = 300
SEASON_THRESHOLDS = ("on_threshold_h", "hold_threshold_h", "off_after_h")

# Like the min/max helpers the lead figures replace.
LEAD_ROUND_DIGITS = 1


@dataclass(frozen=True)
class LeadValue:
    """The highest value among the lead rooms and the room it comes from."""

    value_c: float | None
    room: str | None
    rooms: list[str]


class HeatingHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.rooms: dict[str, HeatingRoomCoordinator] = {}
        self._flow_threshold_entity: str | None = entry.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        self.flow_curve_entity: str | None = entry.data.get(CONF_FLOW_CURVE_ENTITY)
        self._listeners: list[Callable[[], None]] = []
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
        self.season = HeatingSeason()
        self.season_on_threshold_h = DEFAULT_SEASON_ON_THRESHOLD_H
        self.season_hold_threshold_h = DEFAULT_SEASON_HOLD_THRESHOLD_H
        self.season_off_after_h = DEFAULT_SEASON_OFF_AFTER_H
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
        self.season.restore(stored.get("heating_season", {}))
        surfaces = self.used_surfaces()
        if surfaces:
            self.irradiance = IrradianceProvider(
                self.hass, surfaces, self._notify_listeners
            )
            self.irradiance.async_start()
            for room in self.rooms.values():
                room.set_sun_provider(self.irradiance, self.workday_entity)
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
        self._tick(time.time())

    def async_unload(self) -> None:
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
            "heating_season": self.season.export(),
        }

    @callback
    def _async_tick(self, _now: Any) -> None:
        self._tick(time.time())
        self._notify_listeners()

    def _tick(self, now_ts: float) -> None:
        if (
            self._flow_temperature_entity
            and self._float_state(self._flow_temperature_entity) is not None
        ):
            self.flow_seen_ts = now_ts
        self.season.update(self.heating_demand, now_ts, self.season_hold_threshold_h)
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)
        if self.hot_water is not None:
            self.hass.async_create_task(self.hot_water.async_evaluate())
        if self.flow is not None:
            self.hass.async_create_task(self.flow.async_evaluate())

    # --- Flow control ------------------------------------------------------

    def set_flow(self, flow: FlowDevice) -> None:
        """Hand the flow device the actuator; rooms take its threshold."""
        self.flow = flow
        for room in self.rooms.values():
            room.flow_threshold_provider = lambda: flow.threshold_c

    def set_hot_water(self, hot_water: HotWaterDevice) -> None:
        """Rooms take the charges the hot water device sees."""
        self.hot_water = hot_water
        for room in self.rooms.values():
            room.hot_water_charge_provider = lambda: hot_water.charging

    @property
    def outdoor_temperature_c(self) -> float | None:
        if not self._outdoor_entity:
            return None
        return self._float_state(self._outdoor_entity)

    @property
    def charge_flow_c(self) -> float | None:
        """Flow the rooms need around a planned hot-water charge (phase 4)."""
        return None

    # --- Writes to the heat source -----------------------------------------

    @property
    def rate_limit_remaining(self) -> int | None:
        """Calls the heat source's cloud still allows today, if known."""
        if not self.rate_limit_remaining_entity:
            return None
        value = self._float_state(self.rate_limit_remaining_entity)
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

    @property
    def heating_on(self) -> bool | None:
        """Whether the heat source heats; None without or with an empty entity."""
        if not self._heat_source_entity:
            return None
        state = self.hass.states.get(self._heat_source_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return state.state == HEAT_SOURCE_ACTIVE_STATE

    @property
    def demand_hours_24h(self) -> float:
        return round(self.season.hours(time.time()), 2)

    @property
    def season_switch_on_recommended(self) -> bool:
        return self.season.switch_on_recommended(
            time.time(), self.season_on_threshold_h, self.heating_on
        )

    @property
    def season_switch_off_recommended(self) -> bool:
        return self.season.switch_off_recommended(
            time.time(), self.season_off_after_h, self.heating_on
        )

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
        if name not in SEASON_THRESHOLDS:
            raise ValueError(f"Unknown heating season threshold {name}")
        setattr(self, f"season_{name}", float(value))
        self._notify_listeners()

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    @callback
    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def _async_handle_state_change(self, _event: Event[EventStateChangedData]) -> None:
        self._notify_listeners()

    def _lead(
        self, flag: str, value: Callable[[HeatingRoomCoordinator], float | None]
    ) -> LeadValue:
        leads = [room for room in self.rooms.values() if room.data.get(flag)]
        best: tuple[float, str] | None = None
        for room in leads:
            room_value = value(room)
            if room_value is not None and (best is None or room_value > best[0]):
                best = (room_value, room.room_name)
        names = [room.room_name for room in leads]
        if best is None:
            return LeadValue(None, None, names)
        return LeadValue(round(best[0], LEAD_ROUND_DIGITS), best[1], names)

    @property
    def lead_hold_flow(self) -> LeadValue:
        """Flow the heating-limit lead rooms need to hold their setpoint."""
        return self._lead(
            CONF_LEAD_HEATING_LIMIT,
            lambda room: room.normal_gated_hold_flow_temperature_c,
        )

    @property
    def lead_min_flow(self) -> LeadValue:
        """Flow the flow lead rooms need, recovery included."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.normal_min_flow_temperature_c
        )

    def _float_state(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    @property
    def flow_threshold_c(self) -> float | None:
        """The threshold the demand compares to; None while its entity is empty."""
        if self.flow is not None:
            return self.flow.threshold_c
        if self._flow_threshold_entity:
            return self._float_state(self._flow_threshold_entity)
        return self.entry.data.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C)

    @property
    def heating_demand(self) -> bool | None:
        """The lead rooms need more than the heat pump's lowest useful flow."""
        hold_c = self.lead_hold_flow.value_c
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
        return self._float_state(self.flow_curve_entity)

    @property
    def hold_flow_reserve(self) -> float | None:
        return self._reserve(self.lead_hold_flow)

    @property
    def min_flow_reserve(self) -> float | None:
        return self._reserve(self.lead_min_flow)

"""The hub: the house-wide part of the integration.

Holds the rooms of one installation and derives the figures of the lead
rooms from them -- the hold and minimum flow temperatures, the heating
demand and the reserve of the heat pump's curve.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_BUILDING_ROTATION,
    CONF_FLOW_CURVE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_LEAD_FLOW,
    CONF_LEAD_HEATING_LIMIT,
    CONF_OPAQUE_SURFACES,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_WINDOW_SURFACES,
    CONF_WORKDAY_ENTITY,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_ROOF_TILT_DEG,
    RoofOrientation,
)
from .coordinator import HeatingRoomCoordinator
from .irradiance import IrradianceProvider, SurfaceGeometry, building_surfaces

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

    def async_setup(self) -> None:
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
            for entity_id in (self._flow_threshold_entity, self.flow_curve_entity)
            if entity_id
        ]
        if tracked:
            self._unsub.append(
                async_track_state_change_event(
                    self.hass, tracked, self._async_handle_state_change
                )
            )

    def async_unload(self) -> None:
        if self.irradiance is not None:
            self.irradiance.async_stop()
        for unsub in self._unsub:
            unsub()
        self._unsub = []

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
        if not self.flow_curve_entity or lead.value_c is None or lead.value_c <= 0:
            return None
        curve_c = self._float_state(self.flow_curve_entity)
        if curve_c is None:
            return None
        return round(curve_c - lead.value_c, LEAD_ROUND_DIGITS)

    @property
    def hold_flow_reserve(self) -> float | None:
        return self._reserve(self.lead_hold_flow)

    @property
    def min_flow_reserve(self) -> float | None:
        return self._reserve(self.lead_min_flow)

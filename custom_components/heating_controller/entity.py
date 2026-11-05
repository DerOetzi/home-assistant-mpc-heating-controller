from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, HUB_TITLE
from .coordinator import HeatingRoomCoordinator
from .flow_device import FlowDevice
from .hot_water_device import HotWaterDevice
from .hub import HeatingHub


class HeatingControllerEntity(Entity):
    """An entity of one room; ids hang on the room's subentry id."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: HeatingRoomCoordinator, unique_id_suffix: str
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.room_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.room_id)},
            name=coordinator.room_name,
            manufacturer="Heating Controller",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class HeatingHubEntity(Entity):
    """An entity of the whole house, on the hub's device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: HeatingHub, unique_id_suffix: str) -> None:
        self._hub = hub
        hub_id = f"{hub.entry.entry_id}_hub"
        self._attr_unique_id = f"{hub_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub_id)},
            name=HUB_TITLE,
            manufacturer="Heating Controller",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._hub.async_add_listener(self._handle_hub_update))

    def _handle_hub_update(self) -> None:
        self.async_write_ha_state()


def async_add_room_entities(
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[HeatingRoomCoordinator], Iterable[Entity]],
) -> None:
    """Add each room's entities to the room's subentry."""
    hub: HeatingHub = entry.runtime_data
    for coordinator in hub.rooms.values():
        async_add_entities(
            list(build(coordinator)), config_subentry_id=coordinator.room_id
        )


class FlowEntity(Entity):
    """An entity of the flow device; ids hang on the flow subentry id."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, flow: FlowDevice, unique_id_suffix: str) -> None:
        self._flow = flow
        self._attr_unique_id = f"{flow.subentry_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, flow.subentry_id)},
            name=flow.title,
            manufacturer="Heating Controller",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._flow.async_add_listener(self._handle_flow_update))

    def _handle_flow_update(self) -> None:
        self.async_write_ha_state()


def async_add_flow_entities(
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[FlowDevice], Iterable[Entity]],
) -> None:
    """Add the flow device's entities to its subentry, if there is one."""
    hub: HeatingHub = entry.runtime_data
    if hub.flow is not None:
        async_add_entities(
            list(build(hub.flow)), config_subentry_id=hub.flow.subentry_id
        )


class HotWaterEntity(Entity):
    """An entity of the hot water device; ids hang on its subentry id."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hot_water: HotWaterDevice, unique_id_suffix: str) -> None:
        self._hot_water = hot_water
        self._attr_unique_id = f"{hot_water.subentry_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hot_water.subentry_id)},
            name=hot_water.title,
            manufacturer="Heating Controller",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._hot_water.async_add_listener(self._handle_hot_water_update)
        )

    def _handle_hot_water_update(self) -> None:
        self.async_write_ha_state()


def async_add_hot_water_entities(
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[HotWaterDevice], Iterable[Entity]],
) -> None:
    """Add the hot water device's entities to its subentry, if there is one."""
    hub: HeatingHub = entry.runtime_data
    if hub.hot_water is not None:
        async_add_entities(
            list(build(hub.hot_water)), config_subentry_id=hub.hot_water.subentry_id
        )

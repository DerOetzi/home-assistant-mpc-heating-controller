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
from .listeners import Observable


class _DeviceEntity(Entity):
    """An entity on one device of the integration, updated by its source."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        source: Observable,
        device_id: str,
        device_name: str,
        unique_id_suffix: str,
    ) -> None:
        self._source = source
        self._attr_unique_id = f"{device_id}_{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="Heating Controller",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._source.async_add_listener(self._handle_update))
        await super().async_added_to_hass()

    def _handle_update(self) -> None:
        self.async_write_ha_state()


class HeatingControllerEntity(_DeviceEntity):
    """An entity of one room; ids hang on the room's subentry id."""

    def __init__(
        self, coordinator: HeatingRoomCoordinator, unique_id_suffix: str
    ) -> None:
        super().__init__(
            coordinator, coordinator.room_id, coordinator.room_name, unique_id_suffix
        )
        self._coordinator = coordinator


class HeatingHubEntity(_DeviceEntity):
    """An entity of the whole house, on the hub's device."""

    def __init__(self, hub: HeatingHub, unique_id_suffix: str) -> None:
        super().__init__(hub, f"{hub.entry.entry_id}_hub", HUB_TITLE, unique_id_suffix)
        self._hub = hub


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


class FlowEntity(_DeviceEntity):
    """An entity of the flow device; ids hang on the flow subentry id."""

    def __init__(self, flow: FlowDevice, unique_id_suffix: str) -> None:
        super().__init__(flow, flow.subentry_id, flow.title, unique_id_suffix)
        self._flow = flow


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


class HotWaterEntity(_DeviceEntity):
    """An entity of the hot water device; ids hang on its subentry id."""

    def __init__(self, hot_water: HotWaterDevice, unique_id_suffix: str) -> None:
        super().__init__(
            hot_water, hot_water.subentry_id, hot_water.title, unique_id_suffix
        )
        self._hot_water = hot_water


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

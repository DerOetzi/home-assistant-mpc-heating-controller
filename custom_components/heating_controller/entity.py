from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, HUB_TITLE
from .coordinator import HeatingRoomCoordinator
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

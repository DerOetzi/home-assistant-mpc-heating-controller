from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er, target

from . import frontend
from .const import CONF_ROOM_NAME, DOMAIN, SERVICE_UNBLOCK
from .coordinator import HeatingRoomCoordinator
from .store import FlowGateStateStore, LearningFactorsStore

__all__ = ["DOMAIN"]

PLATFORMS = [
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await frontend.async_setup(hass)

    coordinator = HeatingRoomCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: HeatingRoomCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_unload()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    room_name = entry.data[CONF_ROOM_NAME]
    await LearningFactorsStore(hass, room_name, entry.entry_id).async_remove()
    await FlowGateStateStore(hass, room_name, entry.entry_id).async_remove()


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_UNBLOCK):
        return

    async def async_handle_unblock(call: ServiceCall) -> None:
        entity_registry = er.async_get(hass)
        selected = target.async_extract_referenced_entity_ids(
            hass, target.TargetSelection(call.data)
        )

        entry_ids: set[str] = set()
        for entity_id in selected.referenced | selected.indirectly_referenced:
            entity_entry = entity_registry.async_get(entity_id)
            if entity_entry is not None and entity_entry.config_entry_id is not None:
                entry_ids.add(entity_entry.config_entry_id)

        coordinators: dict[str, HeatingRoomCoordinator] = hass.data.get(DOMAIN, {})
        for entry_id in entry_ids:
            coordinator = coordinators.get(entry_id)
            if coordinator is not None:
                await coordinator.async_unblock()

    hass.services.async_register(DOMAIN, SERVICE_UNBLOCK, async_handle_unblock)

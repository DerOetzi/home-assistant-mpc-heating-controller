from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    target,
)
from homeassistant.helpers.service import async_register_admin_service

from . import frontend
from .const import (
    ATTR_CAPACITY_FACTOR,
    ATTR_UA_FACTOR,
    CONF_FLOW_THRESHOLD,
    CONF_ROOM_NAME,
    DOMAIN,
    SERVICE_RECALIBRATE,
    SERVICE_UNBLOCK,
)
from .controller.mpc.models.capacity import (
    MAX_LEARNED_CAPACITY_FACTOR,
    MIN_LEARNED_CAPACITY_FACTOR,
)
from .controller.mpc.models.loss import MAX_LEARNED_UA_FACTOR, MIN_LEARNED_UA_FACTOR
from .coordinator import HeatingRoomCoordinator
from .store import FlowGateStateStore, LearningFactorsStore

_LOGGER = logging.getLogger(__name__)

__all__ = ["DOMAIN"]

RECALIBRATE_SCHEMA = vol.All(
    vol.Schema(
        {
            **cv.TARGET_SERVICE_FIELDS,
            vol.Optional(ATTR_UA_FACTOR): vol.All(
                vol.Coerce(float),
                vol.Range(min=MIN_LEARNED_UA_FACTOR, max=MAX_LEARNED_UA_FACTOR),
            ),
            vol.Optional(ATTR_CAPACITY_FACTOR): vol.All(
                vol.Coerce(float),
                vol.Range(
                    min=MIN_LEARNED_CAPACITY_FACTOR, max=MAX_LEARNED_CAPACITY_FACTOR
                ),
            ),
        }
    ),
    cv.has_at_least_one_key(ATTR_UA_FACTOR, ATTR_CAPACITY_FACTOR),
)

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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version == 1:
        data = dict(entry.data)
        data.pop(CONF_FLOW_THRESHOLD, None)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info(
            "Room %s migrated to the flow threshold entity; link a helper in the "
            "entities step to control it, until then the default applies",
            entry.data.get(CONF_ROOM_NAME),
        )
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


def _referenced_coordinators(
    hass: HomeAssistant, call: ServiceCall
) -> list[HeatingRoomCoordinator]:
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
    return [
        coordinators[entry_id] for entry_id in entry_ids if entry_id in coordinators
    ]


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_UNBLOCK):
        return

    async def async_handle_unblock(call: ServiceCall) -> None:
        for coordinator in _referenced_coordinators(hass, call):
            await coordinator.async_unblock()

    async def async_handle_recalibrate(call: ServiceCall) -> None:
        for coordinator in _referenced_coordinators(hass, call):
            await coordinator.async_recalibrate(
                call.data.get(ATTR_UA_FACTOR), call.data.get(ATTR_CAPACITY_FACTOR)
            )

    hass.services.async_register(DOMAIN, SERVICE_UNBLOCK, async_handle_unblock)
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RECALIBRATE,
        async_handle_recalibrate,
        schema=RECALIBRATE_SCHEMA,
    )

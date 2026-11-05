from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
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
from homeassistant.helpers.typing import ConfigType

from . import frontend
from .const import (
    ATTR_CAPACITY_FACTOR,
    ATTR_RESET_GAINS,
    ATTR_RESET_VALVE_CURVES,
    ATTR_UA_FACTOR,
    CONF_ROOM_NAME,
    CONF_TRVS,
    DOMAIN,
    SERVICE_RECALIBRATE,
    SERVICE_UNBLOCK,
    SUBENTRY_TYPE_FLOW,
    SUBENTRY_TYPE_ROOM,
)
from .controller.mpc.models.capacity import (
    MAX_LEARNED_CAPACITY_FACTOR,
    MIN_LEARNED_CAPACITY_FACTOR,
)
from .controller.mpc.models.loss import MAX_LEARNED_UA_FACTOR, MIN_LEARNED_UA_FACTOR
from .coordinator import HeatingRoomCoordinator
from .flow_device import FlowDevice
from .hub import HeatingHub
from .migration import HUB_VERSION, async_migrate_rooms_to_hub
from .profiles import (
    DeviceProfile,
    TrvResolveError,
    async_get_profiles,
    resolve_trv,
)
from .store import (
    FlowGateStateStore,
    GainStore,
    HubStore,
    LearningFactorsStore,
    ValveCurveStore,
)

_LOGGER = logging.getLogger(__name__)

__all__ = ["DOMAIN"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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
            vol.Optional(ATTR_RESET_GAINS): cv.boolean,
            vol.Optional(ATTR_RESET_VALVE_CURVES): cv.boolean,
        }
    ),
    cv.has_at_least_one_key(
        ATTR_UA_FACTOR, ATTR_CAPACITY_FACTOR, ATTR_RESET_GAINS, ATTR_RESET_VALVE_CURVES
    ),
)

PLATFORMS = [
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
]

type HeatingConfigEntry = ConfigEntry[HeatingHub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # Runs before any entry is set up, so the rooms of config version 7 can
    # move into the hub before HA lists the entries to set up.
    await async_migrate_rooms_to_hub(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HeatingConfigEntry) -> bool:
    await frontend.async_setup(hass)

    profiles = await async_get_profiles(hass)
    hub = HeatingHub(hass, entry)
    rooms: dict[str, HeatingRoomCoordinator] = hass.data.setdefault(DOMAIN, {})
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_ROOM:
            continue
        coordinator = await _async_setup_room(hass, entry, subentry, profiles)
        if coordinator is not None:
            rooms[subentry.subentry_id] = coordinator
            hub.add_room(coordinator)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_FLOW:
            hub.set_flow(FlowDevice(hass, hub, subentry))
    await hub.async_setup()
    entry.runtime_data = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)

    return True


async def _async_setup_room(
    hass: HomeAssistant,
    entry: HeatingConfigEntry,
    subentry: ConfigSubentry,
    profiles: tuple[DeviceProfile, ...],
) -> HeatingRoomCoordinator | None:
    """Set up one room; a room that cannot bind its TRVs stays out."""
    try:
        trvs = [resolve_trv(hass, profiles, trv) for trv in subentry.data[CONF_TRVS]]
    except TrvResolveError as err:
        _LOGGER.error(
            "Room %s: %s; pick the TRV again through the room's reconfigure flow",
            subentry.data[CONF_ROOM_NAME],
            err,
        )
        return None
    coordinator = HeatingRoomCoordinator(
        hass, entry, subentry.subentry_id, {**entry.data, **subentry.data}, trvs
    )
    await coordinator.async_setup()
    return coordinator


async def _async_update_listener(
    hass: HomeAssistant, entry: HeatingConfigEntry
) -> None:
    """A changed hub or room reloads all rooms; a removed room drops its files."""
    hub: HeatingHub = entry.runtime_data
    for room_id, coordinator in hub.rooms.items():
        if room_id not in entry.subentries:
            await _async_remove_room_stores(hass, coordinator.room_name, room_id)
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Rooms of version 7 move into the hub in async_setup; anything older is
    # not supported any more.
    if entry.version < HUB_VERSION:
        _LOGGER.error(
            "Config entry %s has version %s; only version 7 rooms migrate to the "
            "hub. Remove it and add the room to the hub again",
            entry.title,
            entry.version,
        )
        return False
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeatingConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hub: HeatingHub = entry.runtime_data
        hub.async_unload()
        await hub.async_flush()
        for room_id, coordinator in hub.rooms.items():
            hass.data[DOMAIN].pop(room_id, None)
            coordinator.async_unload()
            await coordinator.async_flush_valve_curves()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # A room entry of version 7 is removed by the move into the hub; its
    # stores belong to its subentry now and stay.
    if entry.version < HUB_VERSION:
        return
    await HubStore(hass, entry.entry_id).async_remove()
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_ROOM:
            continue
        await _async_remove_room_stores(
            hass, subentry.data[CONF_ROOM_NAME], subentry.subentry_id
        )


async def _async_remove_room_stores(
    hass: HomeAssistant, room_name: str, room_id: str
) -> None:
    await LearningFactorsStore(hass, room_name, room_id).async_remove()
    await FlowGateStateStore(hass, room_name, room_id).async_remove()
    await ValveCurveStore(hass, room_name, room_id).async_remove()
    await GainStore(hass, room_name, room_id).async_remove()


def _referenced_coordinators(
    hass: HomeAssistant, call: ServiceCall
) -> list[HeatingRoomCoordinator]:
    entity_registry = er.async_get(hass)
    selected = target.async_extract_referenced_entity_ids(
        hass, target.TargetSelection(call.data)
    )

    room_ids: set[str] = set()
    for entity_id in selected.referenced | selected.indirectly_referenced:
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry is not None and entity_entry.config_subentry_id is not None:
            room_ids.add(entity_entry.config_subentry_id)

    coordinators: dict[str, HeatingRoomCoordinator] = hass.data.get(DOMAIN, {})
    return [coordinators[room_id] for room_id in room_ids if room_id in coordinators]


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_UNBLOCK):
        return

    async def async_handle_unblock(call: ServiceCall) -> None:
        for coordinator in _referenced_coordinators(hass, call):
            await coordinator.async_unblock()

    async def async_handle_recalibrate(call: ServiceCall) -> None:
        for coordinator in _referenced_coordinators(hass, call):
            await coordinator.async_recalibrate(
                call.data.get(ATTR_UA_FACTOR),
                call.data.get(ATTR_CAPACITY_FACTOR),
                call.data.get(ATTR_RESET_GAINS, False),
                call.data.get(ATTR_RESET_VALVE_CURVES, False),
            )

    hass.services.async_register(DOMAIN, SERVICE_UNBLOCK, async_handle_unblock)
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RECALIBRATE,
        async_handle_recalibrate,
        schema=RECALIBRATE_SCHEMA,
    )

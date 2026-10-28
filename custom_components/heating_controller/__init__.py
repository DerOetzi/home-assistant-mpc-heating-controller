from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
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
    ATTR_DISTURBANCE_W,
    ATTR_UA_FACTOR,
    CONF_FLOW_THRESHOLD,
    CONF_ROOM_NAME,
    CONF_TARGET_BAND,
    CONF_TRV_DEVICE_ID,
    CONF_TRV_SETPOINT_MODE,
    CONF_TRVS,
    DEFAULT_TARGET_BAND_C,
    DEFAULT_TRV_SETPOINT_MODE,
    DOMAIN,
    LEGACY_CONF_TRV_ENTITY_ID,
    LEGACY_TRV_KEYS,
    SERVICE_RECALIBRATE,
    SERVICE_UNBLOCK,
)
from .controller.mpc.models.capacity import (
    MAX_LEARNED_CAPACITY_FACTOR,
    MIN_LEARNED_CAPACITY_FACTOR,
)
from .controller.mpc.models.loss import MAX_LEARNED_UA_FACTOR, MIN_LEARNED_UA_FACTOR
from .coordinator import HeatingRoomCoordinator
from .profiles import (
    DeviceProfile,
    TrvResolveError,
    async_get_profiles,
    find_profile,
    resolve_trv,
)
from .store import FlowGateStateStore, LearningFactorsStore, ObserverStateStore

_LOGGER = logging.getLogger(__name__)

__all__ = ["DOMAIN"]

MAX_RECALIBRATE_DISTURBANCE_W = 10000.0

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
            # Each room limits it to its own heat load in either direction.
            vol.Optional(ATTR_DISTURBANCE_W): vol.All(
                vol.Coerce(float),
                vol.Range(
                    min=-MAX_RECALIBRATE_DISTURBANCE_W,
                    max=MAX_RECALIBRATE_DISTURBANCE_W,
                ),
            ),
        }
    ),
    cv.has_at_least_one_key(ATTR_UA_FACTOR, ATTR_CAPACITY_FACTOR, ATTR_DISTURBANCE_W),
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

    profiles = await async_get_profiles(hass)
    try:
        trvs = [resolve_trv(hass, profiles, trv) for trv in entry.data[CONF_TRVS]]
    except TrvResolveError as err:
        raise ConfigEntryError(
            f"Room {entry.data[CONF_ROOM_NAME]}: {err}; pick the TRV again "
            "through the options flow"
        ) from err

    coordinator = HeatingRoomCoordinator(hass, entry, trvs)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
    if entry.version == 2:
        # Room area, construction and the flow temperature entity have no
        # sensible value to derive; setup asks for them until they are set.
        hass.config_entries.async_update_entry(entry, version=3)
        _LOGGER.warning(
            "Room %s needs its room area, construction and flow temperature "
            "entity; fill them in through the options flow",
            entry.data.get(CONF_ROOM_NAME),
        )
    if entry.version == 3:
        # Before the band the controller aimed at the exact setpoint; rooms
        # take the default, which the options flow can change.
        data = dict(entry.data)
        data[CONF_TARGET_BAND] = DEFAULT_TARGET_BAND_C
        hass.config_entries.async_update_entry(entry, data=data, version=4)
    if entry.version == 4:
        # Linked a guessed valve position entity; the device profile of
        # version 7 names it instead.
        hass.config_entries.async_update_entry(entry, version=5)
    if entry.version == 5:
        # Relative targets for every room: the rooms influence each other, so
        # they switch together; the options flow can set one back.
        data = dict(entry.data)
        data[CONF_TRV_SETPOINT_MODE] = DEFAULT_TRV_SETPOINT_MODE.value
        hass.config_entries.async_update_entry(entry, data=data, version=6)
    if entry.version == 6:
        # A TRV is its device now; the profile names its entities and range.
        profiles = await async_get_profiles(hass)
        trvs = []
        for trv in entry.data[CONF_TRVS]:
            migrated = _trv_by_device(hass, profiles, trv)
            if migrated is None:
                _LOGGER.error(
                    "Room %s: %s is not a supported TRV device; see the README "
                    "on supported devices and open an issue for this model",
                    entry.data.get(CONF_ROOM_NAME),
                    trv.get(LEGACY_CONF_TRV_ENTITY_ID),
                )
                return False
            trvs.append(migrated)
        data = {**entry.data, CONF_TRVS: trvs}
        hass.config_entries.async_update_entry(entry, data=data, version=7)
    return True


def _trv_by_device(
    hass: HomeAssistant, profiles: tuple[DeviceProfile, ...], trv: dict[str, Any]
) -> dict[str, Any] | None:
    """The TRV stored by the device of its climate entity, if a profile covers it."""
    if CONF_TRV_DEVICE_ID in trv:
        return trv
    registry_entry = er.async_get(hass).async_get(trv.get(LEGACY_CONF_TRV_ENTITY_ID, ""))
    if registry_entry is None or registry_entry.device_id is None:
        return None
    device = dr.async_get(hass).async_get(registry_entry.device_id)
    if device is None or find_profile(hass, profiles, device) is None:
        return None
    return {
        CONF_TRV_DEVICE_ID: device.id,
        **{key: value for key, value in trv.items() if key not in LEGACY_TRV_KEYS},
    }


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: HeatingRoomCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_unload()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    room_name = entry.data[CONF_ROOM_NAME]
    await LearningFactorsStore(hass, room_name, entry.entry_id).async_remove()
    await FlowGateStateStore(hass, room_name, entry.entry_id).async_remove()
    await ObserverStateStore(hass, room_name, entry.entry_id).async_remove()


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
                call.data.get(ATTR_UA_FACTOR),
                call.data.get(ATTR_CAPACITY_FACTOR),
                call.data.get(ATTR_DISTURBANCE_W),
            )

    hass.services.async_register(DOMAIN, SERVICE_UNBLOCK, async_handle_unblock)
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RECALIBRATE,
        async_handle_recalibrate,
        schema=RECALIBRATE_SCHEMA,
    )

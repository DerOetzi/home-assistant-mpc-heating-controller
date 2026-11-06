"""Move the room entries of config version 7 into one hub entry.

Up to version 7 every room was a config entry of its own and carried the
house-wide settings itself. Version 8 has one hub entry with the rooms as
subentries. Each room keeps its entry id as subentry id, so unique ids,
device identifiers and store keys stay as they are.

Drop after season 2026/27.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import (
    CONF_FLOW_CURVE_ENTITY,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_LEAD_FLOW,
    CONF_LEAD_HEATING_LIMIT,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_ROOM_COMFORT_SWITCH,
    CONF_ROOM_NAME,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    DEFAULT_FLOW_THRESHOLD_C,
    DOMAIN,
    HUB_KEYS,
    HUB_TITLE,
    ROOM_KEYS,
    SUBENTRY_TYPE_ROOM,
)

_LOGGER = logging.getLogger(__name__)

HUB_VERSION = 8
ROOM_ENTRY_VERSION = 7

LEGACY_LEARNING_ENABLED_ENTITY = "learning_enabled_entity"
LEGACY_COMFORT_CONDITION_ENTITIES = "comfort_condition_entities"
LEGACY_ROOM_COMFORT_CONDITION_ENTITIES = "room_comfort_condition_entities"

# The one installation that ran version 7, recognised by its outdoor helper.
# Its helpers built from these values are replaced by the hub.
_HOUSE_OUTDOOR_HELPER = "sensor.aussentemperatur_regler"
_HOUSE_FLOW_HELPER = "sensor.vorlauf_regler"
_HOUSE_OUTDOOR_SENSOR = "sensor.heizung_climatecontrol_outdoor_temperature"
_HOUSE_FLOW_SENSOR = "sensor.heizung_climatecontrol_leaving_water_temperature"
_HOUSE_FLOW_CURVE = "sensor.heizkurve_vorlauftemperatur_effektiv"
_HOUSE_LEARNING_BLOCK = {
    "binary_sensor.lernen_schlafzimmer_erlaubt": (
        "binary_sensor.klimaanlage_schlafzimmer_heizt"
    ),
}
_HOUSE_LEAD_HEATING_LIMIT = {"wohnzimmer", "katharina", "rebekka", "schlafzimmer"}
_HOUSE_LEAD_FLOW = {"wohnzimmer", "esszimmer", "katharina", "schlafzimmer", "dani"}


async def async_migrate_rooms_to_hub(hass: HomeAssistant) -> None:
    rooms = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.version == ROOM_ENTRY_VERSION
    ]
    if not rooms:
        return
    if any(
        entry.version >= HUB_VERSION
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        _LOGGER.error(
            "A hub exists next to rooms of version 7; add those rooms to the hub "
            "again and remove the old entries"
        )
        return

    this_house = rooms[0].data.get(CONF_OUTDOOR_TEMPERATURE_ENTITY) == (
        _HOUSE_OUTDOOR_HELPER
    )
    hub_data = _hub_data(rooms, this_house)
    # Built before the first room's entry turns into the hub and loses its data.
    subentries = [
        ConfigSubentry(
            data=MappingProxyType(_room_data(room, this_house)),
            subentry_id=room.entry_id,
            subentry_type=SUBENTRY_TYPE_ROOM,
            title=room.data[CONF_ROOM_NAME],
            unique_id=slugify(room.data[CONF_ROOM_NAME]),
        )
        for room in rooms
    ]
    hub = rooms[0]
    hass.config_entries.async_update_entry(
        hub,
        data=hub_data,
        options={},
        title=HUB_TITLE,
        unique_id=DOMAIN,
        version=HUB_VERSION,
        minor_version=1,
    )
    for subentry in subentries:
        hass.config_entries.async_add_subentry(hub, subentry)
        _move_registry_entries(hass, subentry.subentry_id, hub.entry_id)

    for room in rooms[1:]:
        await hass.config_entries.async_remove(room.entry_id)
    _LOGGER.info("Moved %d rooms into the hub entry", len(rooms))


def _hub_data(rooms: list[ConfigEntry], this_house: bool) -> dict[str, Any]:
    data: dict[str, Any] = {CONF_FLOW_THRESHOLD: DEFAULT_FLOW_THRESHOLD_C}
    for key in HUB_KEYS:
        values = [room.data[key] for room in rooms if key in room.data]
        if not values:
            continue
        data[key] = values[0]
        if any(value != values[0] for value in values):
            _LOGGER.warning(
                "Rooms differ in %s; the hub takes %s from room %s",
                key,
                values[0],
                rooms[0].data[CONF_ROOM_NAME],
            )
    if this_house:
        if data.get(CONF_OUTDOOR_TEMPERATURE_ENTITY) == _HOUSE_OUTDOOR_HELPER:
            data[CONF_OUTDOOR_TEMPERATURE_ENTITY] = _HOUSE_OUTDOOR_SENSOR
        if data.get(CONF_FLOW_TEMPERATURE_ENTITY) == _HOUSE_FLOW_HELPER:
            data[CONF_FLOW_TEMPERATURE_ENTITY] = _HOUSE_FLOW_SENSOR
        data[CONF_FLOW_CURVE_ENTITY] = _HOUSE_FLOW_CURVE
    return data


def _room_data(room: ConfigEntry, this_house: bool) -> dict[str, Any]:
    data = {key: room.data[key] for key in ROOM_KEYS if key in room.data}
    # Comfort entities became switches: a room with house-wide entities
    # follows the hub's switch, one with entities of its own gets a switch.
    data[CONF_USE_HOUSE_COMFORT_CONDITIONS] = bool(
        room.data.get(LEGACY_COMFORT_CONDITION_ENTITIES)
    )
    data[CONF_ROOM_COMFORT_SWITCH] = bool(
        room.data.get(LEGACY_ROOM_COMFORT_CONDITION_ENTITIES)
    )
    learning_block = _HOUSE_LEARNING_BLOCK.get(
        room.data.get(LEGACY_LEARNING_ENABLED_ENTITY, "")
    )
    if learning_block:
        data[CONF_LEARNING_BLOCK_ENTITY] = learning_block
    slug = slugify(room.data[CONF_ROOM_NAME])
    data[CONF_LEAD_HEATING_LIMIT] = this_house and slug in _HOUSE_LEAD_HEATING_LIMIT
    data[CONF_LEAD_FLOW] = this_house and slug in _HOUSE_LEAD_FLOW
    return data


def _move_registry_entries(hass: HomeAssistant, room_id: str, hub_id: str) -> None:
    """Hang the room's entities and device on the hub and the room's subentry."""
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(entity_registry, room_id):
        entity_registry.async_update_entity(
            entity_entry.entity_id,
            config_entry_id=hub_id,
            config_subentry_id=room_id,
        )

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, room_id):
        device_registry.async_update_device(
            device.id, add_config_entry_id=hub_id, add_config_subentry_id=room_id
        )
        # Drops the link without subentry; for the first room, whose entry
        # became the hub, the device keeps the hub through its subentry.
        device_registry.async_update_device(
            device.id, remove_config_entry_id=room_id, remove_config_subentry_id=None
        )

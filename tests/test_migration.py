"""The move of the ten real version 7 rooms into the hub."""

import json
from pathlib import Path

from heating_controller.const import DOMAIN, HUB_TITLE, SUBENTRY_TYPE_ROOM
from heating_controller.migration import HUB_VERSION, async_migrate_rooms_to_hub
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

FIXTURE = Path(__file__).parent / "fixtures" / "config_entries_v7.json"
ROOMS = json.loads(FIXTURE.read_text())
FIRST_ID = ROOMS[0]["entry_id"]
IDS = {room["title"]: room["entry_id"] for room in ROOMS}


def _add_rooms(hass: HomeAssistant, hass_storage: dict) -> None:
    """The ten room entries with their device, two entities and a store each."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    for room in ROOMS:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id=room["entry_id"],
            version=room["version"],
            minor_version=room["minor_version"],
            title=room["title"],
            unique_id=room["unique_id"],
            data=room["data"],
        )
        entry.add_to_hass(hass)
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name=room["title"],
        )
        slug = slugify(room["title"])
        for suffix in ("ua_factor", "heating_mode"):
            entity_registry.async_get_or_create(
                "sensor",
                DOMAIN,
                f"{entry.entry_id}_{suffix}",
                config_entry=entry,
                device_id=device.id,
                suggested_object_id=f"{slug}_{suffix}",
            )
        hass_storage[_store_key(room)] = {
            "version": 1,
            "minor_version": 1,
            "key": _store_key(room),
            "data": {
                "room_name": room["title"],
                "ua_factor": 0.5,
                "capacity_factor": 1.0,
            },
        }


def _store_key(room: dict) -> str:
    return f"{DOMAIN}_{slugify(room['title'])}_{room['entry_id']}_learning_factors"


def _rooms_by_title(hub: MockConfigEntry) -> dict[str, dict]:
    return {sub.title: dict(sub.data) for sub in hub.subentries.values()}


async def test_rooms_become_subentries_of_one_hub(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    _add_rooms(hass, hass_storage)

    await async_migrate_rooms_to_hub(hass)

    entries = hass.config_entries.async_entries(DOMAIN)
    assert [entry.entry_id for entry in entries] == [FIRST_ID]
    hub = entries[0]
    assert (hub.version, hub.title, hub.unique_id) == (HUB_VERSION, HUB_TITLE, DOMAIN)
    assert {sub.subentry_id: sub.title for sub in hub.subentries.values()} == {
        room["entry_id"]: room["title"] for room in ROOMS
    }
    assert all(
        sub.subentry_type == SUBENTRY_TYPE_ROOM and sub.unique_id == slugify(sub.title)
        for sub in hub.subentries.values()
    )


async def test_hub_takes_the_house_settings(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    _add_rooms(hass, hass_storage)

    await async_migrate_rooms_to_hub(hass)

    data = hass.config_entries.async_entries(DOMAIN)[0].data
    assert data["outdoor_temperature_entity"] == (
        "sensor.heizung_climatecontrol_outdoor_temperature"
    )
    assert data["flow_temperature_entity"] == (
        "sensor.heizung_climatecontrol_leaving_water_temperature"
    )
    assert data["flow_curve_entity"] == "sensor.heizkurve_vorlauftemperatur_effektiv"
    assert data["flow_threshold_entity"] == "sensor.vorlaufschwelle_effektiv"
    assert data["flow_threshold_c"] == 30.0
    assert "comfort_condition_entities" not in data
    assert data["heat_source_climate_entity"] == "climate.heizung_leaving_water_offset"
    assert data["design_outdoor_temperature_c"] == -13.5
    assert data["frost_protection_temperature_c"] == 5.0
    assert data["trv_setpoint_mode"] == "relative"
    assert "room_name" not in data
    assert "learning_window_entity" not in data
    assert "learning_enabled_entity" not in data


async def test_rooms_keep_their_own_settings(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    _add_rooms(hass, hass_storage)
    before = {room["title"]: room["data"] for room in ROOMS}

    await async_migrate_rooms_to_hub(hass)

    rooms = _rooms_by_title(hass.config_entries.async_entries(DOMAIN)[0])
    for title, data in rooms.items():
        old = before[title]
        for key in (
            "room_name",
            "trvs",
            "window_contact_entities",
            "boost_enabled",
            "pv_boost_enabled",
            "pv_boost_temperature_offset_c",
            "design_indoor_temperature_c",
            "room_heat_load_w",
            "room_area_m2",
            "construction",
        ):
            assert data[key] == old[key], (title, key)
        assert data.get("room_sensor_entity") == old.get("room_sensor_entity")
        for key in (
            "outdoor_temperature_entity",
            "learning_window_entity",
            "learning_enabled_entity",
            "heating_available_entity",
            "comfort_condition_entities",
            "room_comfort_condition_entities",
        ):
            assert key not in data, (title, key)

    assert {title for title, data in rooms.items() if data["pv_boost_enabled"]} == {
        "Wohnzimmer",
        "Esszimmer",
        "Katharina",
        "Rebekka",
        "Schlafzimmer",
    }
    assert [
        title
        for title, data in rooms.items()
        if not data["use_house_comfort_conditions"]
    ] == ["Flur EG"]
    assert [title for title, data in rooms.items() if data["room_comfort_switch"]] == [
        "Dani"
    ]
    assert {
        title: data["learning_block_entity"]
        for title, data in rooms.items()
        if "learning_block_entity" in data
    } == {"Schlafzimmer": "binary_sensor.klimaanlage_schlafzimmer_heizt"}
    assert {title for title, data in rooms.items() if data["lead_heating_limit"]} == {
        "Wohnzimmer",
        "Katharina",
        "Rebekka",
        "Schlafzimmer",
    }
    assert {title for title, data in rooms.items() if data["lead_flow"]} == {
        "Wohnzimmer",
        "Esszimmer",
        "Katharina",
        "Schlafzimmer",
        "Dani",
    }


async def test_entities_and_devices_hang_on_the_rooms_subentry(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    _add_rooms(hass, hass_storage)
    entity_registry = er.async_get(hass)
    before = {
        entity.unique_id: entity.entity_id
        for entity in entity_registry.entities.values()
        if entity.platform == DOMAIN
    }

    await async_migrate_rooms_to_hub(hass)

    entities = [
        entity
        for entity in entity_registry.entities.values()
        if entity.platform == DOMAIN
    ]
    assert {entity.unique_id: entity.entity_id for entity in entities} == before
    for entity in entities:
        room_id = entity.unique_id.rsplit("_", 2)[0]
        assert entity.config_entry_id == FIRST_ID
        assert entity.config_subentry_id == room_id

    device_registry = dr.async_get(hass)
    for room_id in IDS.values():
        device = device_registry.async_get_device(identifiers={(DOMAIN, room_id)})
        assert device is not None
        assert device.config_entries == {FIRST_ID}
        assert device.config_entries_subentries == {FIRST_ID: {room_id}}


async def test_setup_moves_the_rooms_and_keeps_their_stores(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    _add_rooms(hass, hass_storage)

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].state is ConfigEntryState.LOADED
    for room in ROOMS:
        assert _store_key(room) in hass_storage, room["title"]

    assert await hass.config_entries.async_unload(entries[0].entry_id)


async def test_nothing_moves_without_rooms_of_version_7(
    hass: HomeAssistant,
) -> None:
    hub = MockConfigEntry(domain=DOMAIN, version=HUB_VERSION, data={})
    hub.add_to_hass(hass)

    await async_migrate_rooms_to_hub(hass)

    assert hass.config_entries.async_entries(DOMAIN) == [hub]
    assert hub.data == {}

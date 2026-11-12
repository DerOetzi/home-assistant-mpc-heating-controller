"""The hot water device: writes, mode select, circulation, subentry flow."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from heating_controller.const import (
    DOMAIN,
    HUB_KEYS,
    HUB_TITLE,
    SUBENTRY_TYPE_HOT_WATER,
    SUBENTRY_TYPE_ROOM,
)
from heating_controller.const import HUB_VERSION
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from test_config_flow import _empty_hub
from test_coordinator import _room_data, _seed_entities

HEATER = "water_heater.tank"
PUMP = "switch.circulation_pump"
HOT_WATER_DATA = {
    "water_heater_entity": HEATER,
    "tank_temperature_entity": "sensor.tank",
    "legionella_enabled": True,
    "circulation_enabled": True,
    "pump_entity": PUMP,
    "circ_return_entity": "sensor.circulation_return",
}


def _hub(data: dict) -> MockConfigEntry:
    room = {key: value for key, value in data.items() if key not in HUB_KEYS}
    return MockConfigEntry(
        domain=DOMAIN,
        title=HUB_TITLE,
        version=HUB_VERSION,
        entry_id="hub-entry",
        unique_id=DOMAIN,
        data={key: value for key, value in data.items() if key in HUB_KEYS},
        subentries_data=[
            {
                "data": room,
                "subentry_id": "hub-entry",
                "subentry_type": SUBENTRY_TYPE_ROOM,
                "title": room["room_name"],
                "unique_id": slugify(room["room_name"]),
            },
            {
                "data": HOT_WATER_DATA,
                "subentry_id": "hw-entry",
                "subentry_type": SUBENTRY_TYPE_HOT_WATER,
                "title": "Warmwasser",
                "unique_id": SUBENTRY_TYPE_HOT_WATER,
            },
        ],
    )


async def _setup(hass: HomeAssistant) -> list[ServiceCall]:
    _seed_entities(hass)
    hass.states.async_set(HEATER, "heat_pump", {"temperature": 30.0})
    hass.states.async_set("sensor.tank", "43")
    hass.states.async_set(PUMP, "off")
    hass.states.async_set("sensor.circulation_return", "30")
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    for domain, service in (
        ("water_heater", "set_temperature"),
        ("switch", "turn_on"),
        ("switch", "turn_off"),
        ("climate", "set_temperature"),
    ):
        hass.services.async_register(domain, service, record)
    entry = _hub(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_writes_target_once_and_announces_charge(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    calls = await _setup(hass)
    await _tick(hass, freezer)

    target = hass.states.get("sensor.warmwasser_hot_water_target")
    writes = [c for c in calls if c.domain == "water_heater"]
    assert len(writes) == 1
    assert writes[0].data["temperature"] == float(target.state)

    await _tick(hass, freezer)
    assert len([c for c in calls if c.domain == "water_heater"]) == 1


async def test_boost_raises_target(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    calls = await _setup(hass)
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.warmwasser_hot_water_boost"},
        blocking=True,
    )
    await hass.async_block_till_done()
    writes = [c for c in calls if c.domain == "water_heater"]
    assert writes[-1].data["temperature"] == 60.0
    assert hass.states.get("sensor.warmwasser_hot_water_phase").state == "boost"
    # Tank 43 < 60 - 5: the write announces a charge, rooms see it.
    assert hass.states.get("binary_sensor.warmwasser_hot_water_charging").state == "on"
    hub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert all(room.hot_water_charge_provider() for room in hub.rooms.values())


async def test_circulation_starts_pump_when_loop_cold(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    # The pump just changed state: it waits for the minimum pause (5 min).
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # The switch platform owns switch.turn_on here, so the decision shows it.
    assert hass.states.get("sensor.warmwasser_circulation_reason").state == "cooled"


async def test_manual_mode_select(hass: HomeAssistant) -> None:
    await _setup(hass)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.warmwasser_hot_water_mode", "option": "surplus"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("select.warmwasser_hot_water_mode").state == "surplus"
    target = hass.states.get("sensor.warmwasser_hot_water_target")
    assert target.attributes["mode"] == "surplus"


async def test_legionella_entities(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.warmwasser_legionella_protection") is not None
    assert (
        hass.states.get("binary_sensor.warmwasser_legionella_protection_failed").state
        == "off"
    )
    assert hass.states.get("button.warmwasser_start_legionella_protection") is not None


async def test_subentry_flow(hass: HomeAssistant) -> None:
    entry = _empty_hub(hass)
    start = hass.config_entries.subentries.async_init
    configure = hass.config_entries.subentries.async_configure
    result = await start(
        (entry.entry_id, SUBENTRY_TYPE_HOT_WATER),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["step_id"] == "sources"
    result = await configure(
        result["flow_id"],
        {
            "water_heater_entity": HEATER,
            "legionella_enabled": False,
            "circulation_enabled": False,
        },
    )
    assert result["step_id"] == "modes"
    result = await configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.data["water_heater_entity"] == HEATER
    assert subentry.data["night_start"] == "21:00:00"

    again = await start(
        (entry.entry_id, SUBENTRY_TYPE_HOT_WATER),
        context={"source": config_entries.SOURCE_USER},
    )
    assert again["reason"] == "already_configured"

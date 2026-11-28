import time
from datetime import date, datetime, timedelta

import pytest
from heating_controller.const import (
    DEFAULT_FLOW_THRESHOLD_C,
    DOMAIN,
    HUB_KEYS,
    HUB_TITLE,
    HUB_VERSION,
    SUBENTRY_TYPE_ROOM,
    HeatMode,
    RoomSupply,
    SupplyReason,
)
from heating_controller.controller.mpc.results import RoomTemperature
from heating_controller.coordinator import (
    HeatingRoomCoordinator,
)
from heating_controller.profiles import StatusMatch, TrvBinding
from heating_controller.room.heat_availability import FLOW_DIP_TOLERANCE_S
from heating_controller.room.house import DetachedHouse
from heating_controller.room.learning_inputs import FLOW_TEMPERATURE_MAX_GAP_S
from homeassistant.const import (
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

TRV_EMITTER = {
    "emitter_type": "panel",
    "radiator_type": "22",
    "width_mm": 1000.0,
    "height_mm": 600.0,
}
TRV_IEEE = "0x0000000000000001"
# Stands in for a registered device where the coordinator gets its TRVs bound
# already; _room_data registers a real one for a full setup.
TRV_DEVICE_PLACEHOLDER = "trv-device"

ENTRY_DATA = {
    "room_name": "Wohnzimmer",
    "trvs": [{"device_id": TRV_DEVICE_PLACEHOLDER, **TRV_EMITTER}],
    "room_sensor_entity": "sensor.wohnzimmer_temperatur",
    "window_contact_entities": ["binary_sensor.fenster_wohnzimmer"],
    "outdoor_temperature_entity": "sensor.outdoor_temperature",
    "heat_source_climate_entity": "climate.heat_source",
    "flow_temperature_entity": "sensor.flow_temperature",
    "boost_enabled": False,
    "boost_temperature_offset_c": 5.0,
    "frost_protection_temperature_c": 8.0,
    "pv_boost_enabled": False,
    "pv_boost_temperature_offset_c": 1.0,
    "design_indoor_temperature_c": 20.0,
    "design_outdoor_temperature_c": -12.0,
    "design_temperature_system": "system_55_45",
    "room_heat_load_w": 1200.0,
    "room_area_m2": 20.0,
    "construction": "medium",
    "max_sensor_age_s": 1800.0,
    "flow_threshold_entity": "input_number.flow_threshold",
}


def _hub_entry(data: dict, *, entry_id: str = "hub-entry") -> MockConfigEntry:
    """A hub with one room, split from merged settings.

    The room's subentry id is the entry id, as after the move from version 7,
    so hass.data[DOMAIN][entry.entry_id] is the room.
    """
    room = {key: value for key, value in data.items() if key not in HUB_KEYS}
    return MockConfigEntry(
        domain=DOMAIN,
        title=HUB_TITLE,
        version=HUB_VERSION,
        entry_id=entry_id,
        unique_id=DOMAIN,
        data={key: value for key, value in data.items() if key in HUB_KEYS},
        subentries_data=[
            {
                "data": room,
                "subentry_id": entry_id,
                "subentry_type": SUBENTRY_TYPE_ROOM,
                "title": room["room_name"],
                "unique_id": slugify(room["room_name"]),
            }
        ],
    )


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, trvs: list[TrvBinding]
) -> HeatingRoomCoordinator:
    """A room whose subentry id is the entry id, with all settings in one dict."""
    return HeatingRoomCoordinator(hass, entry, entry.entry_id, entry.data, trvs)


def _trvs(**overrides) -> list[TrvBinding]:
    """The room's TRV as its device profile would bind it."""
    return [
        TrvBinding(
            **{
                "climate_entity": "climate.heizung_wohnzimmer",
                "emitter": TRV_EMITTER,
                "heat_available_entity": "switch.heizung_wohnzimmer_trv_active",
                **overrides,
            }
        )
    ]


def register_trv_device(
    hass: HomeAssistant,
    *,
    ieee: str = TRV_IEEE,
    object_id: str = "heizung_wohnzimmer",
    properties: tuple[tuple[str, str, str], ...] = (
        ("climate", "climate", ""),
        ("switch", "switch_heat_available", "_trv_active"),
    ),
    model_id: str = "014G2461",
) -> str:
    """Register a Danfoss Ally as Zigbee2MQTT exposes it; returns its device id."""
    mqtt_entry = MockConfigEntry(domain="mqtt", entry_id="mqtt-entry")
    if hass.config_entries.async_get_entry(mqtt_entry.entry_id) is None:
        mqtt_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        identifiers={("mqtt", f"zigbee2mqtt_{ieee}")},
        manufacturer="Danfoss",
        model="Ally thermostat",
        model_id=model_id,
        name=object_id,
    )
    entity_registry = er.async_get(hass)
    for domain, prop, suffix in properties:
        entity_registry.async_get_or_create(
            domain,
            "mqtt",
            f"{ieee}_{prop}_zigbee2mqtt",
            suggested_object_id=f"{object_id}{suffix}",
            config_entry=hass.config_entries.async_get_entry(mqtt_entry.entry_id),
            device_id=device.id,
        )
    return device.id


def _room_data(hass: HomeAssistant, **overrides) -> dict:
    """Entry data whose TRV is a registered device, for a full setup."""
    device_id = register_trv_device(hass)
    return {
        **ENTRY_DATA,
        "trvs": [{"device_id": device_id, **TRV_EMITTER}],
        **overrides,
    }


def _seed_entities(
    hass: HomeAssistant,
    *,
    heat_available=True,
    flow_temp=42.0,
    window_open=False,
) -> None:
    # Registered first, so the registry hands out the entity ids set below.
    register_trv_device(hass)
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 18.0}
    )
    hass.states.async_set("sensor.wohnzimmer_temperatur", "18.5")
    hass.states.async_set(
        "binary_sensor.fenster_wohnzimmer", "on" if window_open else "off"
    )
    hass.states.async_set("sensor.outdoor_temperature", "-3.0")
    hass.states.async_set("climate.heat_source", "heat" if heat_available else "off")
    hass.states.async_set("sensor.flow_temperature", str(flow_temp))
    hass.states.async_set("input_number.flow_threshold", "30.0")
    # The hub's startup gate waits for every entity it writes.
    hass.states.async_set("switch.heizung_wohnzimmer_trv_active", "off")


def _register_fake_climate_set_temperature(hass: HomeAssistant) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def climate_handler(call: ServiceCall) -> None:
        calls.append(call)

    async def switch_handler(call: ServiceCall) -> None:
        return None

    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)
    return calls


async def test_setup_seeds_state_and_computes_result(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)

    await coordinator.async_setup()

    await coordinator.async_start()

    assert coordinator.state.is_comfort() is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert coordinator.is_automation_active is True
    assert coordinator.last_result is not None
    assert coordinator.last_result.input.room_temp_c == 18.5

    assert calls, "expected at least one climate.set_temperature service call"
    assert calls[0].data["entity_id"] == "climate.heizung_wohnzimmer"

    coordinator.async_unload()


async def test_periodic_sensor_poll_keeps_room_temperature_fresh(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert isinstance(coordinator.mpc.get_room_temperature(), RoomTemperature)

    for _ in range(7):
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert isinstance(coordinator.mpc.get_room_temperature(), RoomTemperature)

    coordinator.async_unload()


async def test_window_open_forces_frost_protection(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)

    await coordinator.async_setup()

    await coordinator.async_start()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    coordinator.async_unload()


async def test_manual_selection_bypasses_forced_frost_protection_for_eco(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False, window_open=True)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    assert coordinator.current_heat_mode == HeatMode.ECO
    assert coordinator.is_automation_active is False

    coordinator.async_unload()


async def test_manual_selection_bypasses_forced_frost_protection_for_comfort(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False, window_open=True)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    await coordinator.async_set_manual_heat_mode(HeatMode.COMFORT)
    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert coordinator.is_automation_active is False

    coordinator.async_unload()


async def test_manual_frost_protection_selection_keeps_automation_deactivated(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    await coordinator.async_set_manual_heat_mode(HeatMode.COMFORT)
    assert coordinator.is_automation_active is False

    await coordinator.async_set_manual_heat_mode(HeatMode.FROST_PROTECTION)
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.is_automation_active is False

    coordinator.async_unload()


async def test_unblock_reactivates_automation_and_reasserts_forced_frost_protection(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False, window_open=True)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    await coordinator.async_set_manual_heat_mode(HeatMode.COMFORT)
    assert coordinator.is_automation_active is False
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    await coordinator.async_unblock()
    assert coordinator.is_automation_active is True
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    coordinator.async_unload()


async def test_manual_override_blocks_until_unblocked(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert coordinator.is_automation_active is True

    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    assert coordinator.current_heat_mode == HeatMode.ECO
    assert coordinator.is_automation_active is False

    await coordinator.async_unblock()
    assert coordinator.is_automation_active is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_heating_unavailable_disables_learning_and_keeps_the_mode(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert calls[-1].data["temperature"] == 22.0
    assert coordinator.mpc._learner._enabled is False

    coordinator.async_unload()


async def test_heat_source_off_with_hot_flow_is_inactive_but_keeps_the_mode(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False, flow_temp=49.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_heat_source_heat_mode_but_flow_below_threshold_is_inactive(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, flow_temp=25.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_heat_source_heat_mode_at_the_threshold_is_active(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, flow_temp=30.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.trv_active is True

    coordinator.async_unload()


async def test_heat_source_heat_mode_and_hot_flow_is_active(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, flow_temp=42.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.trv_active is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_min_flow_reports_normal_mode_demand_while_forced_frost(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    await hass.async_block_till_done()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.normal_heat_mode == HeatMode.COMFORT
    assert coordinator.normal_target_temperature_c == 22.0
    assert coordinator.normal_result is not None
    assert coordinator.normal_hold_flow_temperature_c > 0
    assert (
        coordinator.base_temperature_c == ENTRY_DATA["frost_protection_temperature_c"]
    )
    assert coordinator.trv_active is False
    assert coordinator.supply[:2] == (RoomSupply.NOT_RATED, SupplyReason.WINDOW_OPEN)

    coordinator.async_unload()


async def test_flow_gate_closes_while_the_room_coasts_above_target(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "12.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "25.0")
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 25.0}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA, "flow_gate_hold_time_s": 0.0}
    )
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.normal_result.flow_gate_closed is True
    assert coordinator.normal_gated_hold_flow_temperature_c == 0
    assert coordinator.normal_hold_flow_temperature_c > 0

    coordinator.async_unload()


async def test_entries_without_flow_gate_settings_fall_back_to_defaults(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "12.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "25.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.normal_result.flow_gate_closed is False
    assert coordinator.normal_gated_hold_flow_temperature_c > 0

    coordinator.async_unload()


async def test_trv_active_switch_turned_off_when_unavailable(
    hass: HomeAssistant,
) -> None:
    switch_calls: list[ServiceCall] = []

    async def climate_handler(call: ServiceCall) -> None:
        return None

    async def switch_handler(call: ServiceCall) -> None:
        switch_calls.append(call)

    _seed_entities(hass, heat_available=False)
    hass.states.async_set("switch.heizung_wohnzimmer_trv_active", "on")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    await coordinator.async_setup()

    await coordinator.async_start()

    assert coordinator.trv_active is False
    off_calls = [call for call in switch_calls if call.service == "turn_off"]
    assert off_calls
    assert off_calls[-1].data["entity_id"] == ["switch.heizung_wohnzimmer_trv_active"]

    coordinator.async_unload()


async def test_trv_target_already_reported_is_not_rewritten(
    hass: HomeAssistant,
) -> None:
    """Danfoss TRVs abort a running adaptation run on every setpoint write."""
    writes: list[tuple[float | None, float]] = []

    async def climate_handler(call: ServiceCall) -> None:
        # Behave like a TRV: report the written target back as its state.
        state = hass.states.get(call.data["entity_id"])
        writes.append((state.attributes.get("temperature"), call.data["temperature"]))
        hass.states.async_set(
            call.data["entity_id"],
            state.state,
            {**state.attributes, "temperature": call.data["temperature"]},
        )

    async def switch_handler(call: ServiceCall) -> None:
        return None

    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    assert writes

    # The MPC ramps its target over several cycles; once it settles, a
    # recompute must not write again.
    for _ in range(20):
        write_count = len(writes)
        await coordinator.async_unblock()
        await hass.async_block_till_done()
        if len(writes) == write_count:
            break
    else:
        raise AssertionError("target never settled; every recompute wrote")

    assert all(reported != written for reported, written in writes)

    coordinator.async_unload()


async def test_trv_target_differing_from_reported_is_written(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    assert calls
    written_target_c = calls[-1].data["temperature"]
    reported_target_c = written_target_c + 1.0
    calls.clear()

    # Past the window in which a differing report counts as the TRV's echo.
    later = time.time() + 300
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: later)

    # E.g. someone turned the TRV by hand: the controller must win it back.
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": reported_target_c},
    )
    await hass.async_block_till_done()

    assert calls
    assert calls[-1].data["temperature"] != reported_target_c

    coordinator.async_unload()


async def test_trv_active_switch_already_in_state_is_not_rewritten(
    hass: HomeAssistant,
) -> None:
    switch_calls: list[ServiceCall] = []

    async def climate_handler(call: ServiceCall) -> None:
        return None

    async def switch_handler(call: ServiceCall) -> None:
        switch_calls.append(call)

    _seed_entities(hass, heat_available=True)
    hass.states.async_set("switch.heizung_wohnzimmer_trv_active", "on")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    await coordinator.async_setup()

    await coordinator.async_start()
    await hass.async_block_till_done()

    assert coordinator.trv_active is True
    assert switch_calls == []

    hass.states.async_set("switch.heizung_wohnzimmer_trv_active", "off")
    await coordinator.async_unblock()
    await hass.async_block_till_done()

    on_calls = [call for call in switch_calls if call.service == "turn_on"]
    assert on_calls
    assert on_calls[-1].data["entity_id"] == ["switch.heizung_wohnzimmer_trv_active"]

    coordinator.async_unload()


async def test_setup_tolerates_missing_heat_source_climate_entity(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    legacy_data = {
        key: value
        for key, value in ENTRY_DATA.items()
        if key != "heat_source_climate_entity"
    }
    entry = MockConfigEntry(domain=DOMAIN, data=legacy_data)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.trv_active is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_house_and_room_switch_both_gate_comfort(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    data = {**ENTRY_DATA, "room_comfort_switch": True}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    # The room's own switch starts off.
    assert coordinator.state.is_comfort() is False

    await coordinator.async_set_room_comfort(True)
    assert coordinator.state.is_comfort() is True

    await coordinator.async_set_house_comfort(False)
    assert coordinator.state.is_comfort() is False

    coordinator.async_unload()


async def test_room_off_the_house_switch_follows_only_its_own(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    data = {
        **ENTRY_DATA,
        "use_house_comfort_conditions": False,
        "room_comfort_switch": True,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    await coordinator.async_set_house_comfort(False)
    await coordinator.async_set_room_comfort(True)
    assert coordinator.state.is_comfort() is True

    await coordinator.async_set_room_comfort(False)
    assert coordinator.state.is_comfort() is False

    coordinator.async_unload()


async def test_closed_flow_gate_survives_a_restart(hass: HomeAssistant) -> None:
    """A restart must not reopen the gate for a room coasting in the band
    between the open and close thresholds."""
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "21.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "25.0")
    _register_fake_climate_set_temperature(hass)

    closing_entry = MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA, "flow_gate_hold_time_s": 0.0}
    )
    closing_entry.add_to_hass(hass)
    before_restart = _coordinator(hass, closing_entry, _trvs())
    await before_restart.async_setup()
    await before_restart.async_start()
    assert before_restart.normal_result.flow_gate_closed is True
    before_restart.async_unload()

    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.4")

    # Same entry (same store), now with the default close hold time, so the
    # gate cannot simply close again on the first cycle.
    restarted_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, entry_id=closing_entry.entry_id
    )
    after_restart = _coordinator(hass, restarted_entry, _trvs())
    await after_restart.async_setup()
    await after_restart.async_start()

    assert after_restart.normal_result.flow_gate_closed is True
    assert after_restart.normal_gated_hold_flow_temperature_c == 0
    after_restart.async_unload()

    fresh_entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    fresh_entry.add_to_hass(hass)
    without_stored_state = _coordinator(hass, fresh_entry, _trvs())
    await without_stored_state.async_setup()
    await without_stored_state.async_start()

    assert without_stored_state.normal_result.flow_gate_closed is False
    without_stored_state.async_unload()


async def test_compute_falls_back_to_trvs_without_the_room_sensor(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.last_result is not None
    assert coordinator.last_result.input.room_temp_c == 18.0

    coordinator.async_unload()


async def test_flow_threshold_follows_the_linked_helper(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass, flow_temp=32.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.flow_threshold_c == 30.0
    assert coordinator.trv_active is True

    hass.states.async_set("input_number.flow_threshold", "35.0")
    await hass.async_block_till_done()
    assert coordinator.flow_threshold_c == 35.0

    await _flow_stays_below_the_threshold(hass, monkeypatch, "31.0")
    assert coordinator.trv_active is False

    coordinator.async_unload()


async def test_flow_threshold_falls_back_to_the_default(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            key: value
            for key, value in ENTRY_DATA.items()
            if key != "flow_threshold_entity"
        },
    )
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.flow_threshold_c == DEFAULT_FLOW_THRESHOLD_C

    coordinator.async_unload()


async def test_flow_threshold_falls_back_while_the_helper_is_unavailable(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("input_number.flow_threshold", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.flow_threshold_c == DEFAULT_FLOW_THRESHOLD_C

    coordinator.async_unload()


async def _setup_coordinator(
    hass: HomeAssistant, trvs: list[TrvBinding] | None = None, **overrides
) -> HeatingRoomCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, **overrides})
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, trvs or _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    return coordinator


async def test_learning_block_follows_the_linked_entity(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    hass.states.async_set("binary_sensor.extra_heater", "on")
    coordinator = await _setup_coordinator(
        hass, learning_block_entity="binary_sensor.extra_heater"
    )
    learner = coordinator.mpc._learner

    assert learner._learning_paused is True

    hass.states.async_set("binary_sensor.extra_heater", "off")
    await hass.async_block_till_done()
    assert learner._learning_paused is False
    assert learner._learning_resumed_ts > 0

    hass.states.async_set("binary_sensor.extra_heater", "unavailable")
    await hass.async_block_till_done()
    assert learner._learning_paused is False

    coordinator.async_unload()


async def test_learning_pauses_only_after_a_long_flow_temperature_gap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    now = [time.time()]
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: now[0])
    coordinator = await _setup_coordinator(hass)
    learner = coordinator.mpc._learner

    hass.states.async_set("sensor.flow_temperature", "unavailable")
    await hass.async_block_till_done()
    now[0] += FLOW_TEMPERATURE_MAX_GAP_S - 60
    await coordinator._async_handle_sensor_poll(None)
    assert learner._learning_paused is False

    now[0] += 120
    await coordinator._async_handle_sensor_poll(None)
    assert learner._learning_paused is True

    hass.states.async_set("sensor.flow_temperature", "41.0")
    await hass.async_block_till_done()
    assert learner._learning_paused is False

    coordinator.async_unload()


async def test_learning_is_not_paused_without_a_linked_entity(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    assert coordinator.mpc._learner._learning_paused is False

    coordinator.async_unload()


async def test_closing_the_window_frees_capacity_before_ua(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)
    learner = coordinator.mpc._learner

    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    await hass.async_block_till_done()
    assert learner._window_open is True
    before_close = time.time()
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "off")
    await hass.async_block_till_done()

    assert learner._window_open is False
    ua_until = learner._ctx.ua_blocked_until_ts - before_close
    capacity_until = learner._ctx.capacity_blocked_until_ts - before_close
    assert 29 * 60 < ua_until <= 31 * 60
    assert 9 * 60 < capacity_until <= 11 * 60

    coordinator.async_unload()


async def test_sun_condition_needs_two_hours_after_sunset_and_ends_at_sunrise(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    day = date(2026, 12, 1)
    sunset = get_astral_event_date(hass, SUN_EVENT_SUNSET, day)
    next_sunrise = get_astral_event_date(
        hass, SUN_EVENT_SUNRISE, day + timedelta(days=1)
    )

    def met(moment: datetime) -> bool:
        return coordinator.is_sun_condition_met(moment.timestamp())

    assert met(sunset - timedelta(hours=3)) is False
    assert met(sunset + timedelta(hours=1)) is False
    assert met(sunset + timedelta(hours=2, minutes=1)) is True
    assert met(next_sunrise - timedelta(minutes=1)) is True
    assert met(next_sunrise + timedelta(minutes=1)) is False

    coordinator.async_unload()


async def test_a_burst_of_state_changes_recomputes_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass, flow_temp=42.0)
    hass.states.async_set("climate.heat_source", "heat", {"current_temperature": 20.0})
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    recomputes = 0
    recompute = coordinator._async_recompute_locked

    async def counting_recompute() -> None:
        nonlocal recomputes
        recomputes += 1
        await recompute()

    monkeypatch.setattr(coordinator, "_async_recompute_locked", counting_recompute)

    hass.states.async_set("sensor.flow_temperature", "25.0")
    hass.states.async_set("sensor.flow_temperature", "26.0")
    hass.states.async_set("sensor.flow_temperature", "27.0")
    await hass.async_block_till_done()

    assert recomputes == 1
    assert coordinator.last_result.input.flow_temp_c == 27.0
    coordinator.async_unload()


async def _flow_stays_below_the_threshold(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, flow_c: str
) -> None:
    """Report a low flow again once the dip has lasted past its tolerance."""
    later = time.time() + FLOW_DIP_TOLERANCE_S + 1
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: later)
    hass.states.async_set("sensor.flow_temperature", flow_c)
    await hass.async_block_till_done()


async def test_flow_temperature_comes_from_the_flow_entity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass, flow_temp=42.0)
    hass.states.async_set("climate.heat_source", "heat", {"current_temperature": 20.0})
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.current_flow_temperature_c == 42.0
    assert coordinator.trv_active is True

    hass.states.async_set("sensor.flow_temperature", "25.0")
    await hass.async_block_till_done()

    assert coordinator.current_flow_temperature_c == 25.0
    assert coordinator.last_result.input.flow_temp_c == 25.0
    # A dip such as a defrost does not take the heat away at once.
    assert coordinator.trv_active is True

    start = time.time()
    monkeypatch.setattr(
        "heating_controller.coordinator.time.time",
        lambda: start + FLOW_DIP_TOLERANCE_S + 1,
    )
    hass.states.async_set("sensor.flow_temperature", "24.0")
    await hass.async_block_till_done()

    assert coordinator.trv_active is False

    coordinator.async_unload()


async def test_flow_back_above_the_threshold_ends_the_dip(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass, flow_temp=42.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    start = time.time()
    now = [start]
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: now[0])

    hass.states.async_set("sensor.flow_temperature", "25.0")
    await hass.async_block_till_done()
    now[0] = start + FLOW_DIP_TOLERANCE_S - 60
    hass.states.async_set("sensor.flow_temperature", "40.0")
    await hass.async_block_till_done()
    now[0] = start + FLOW_DIP_TOLERANCE_S + 60
    hass.states.async_set("sensor.flow_temperature", "25.0")
    await hass.async_block_till_done()

    # The new dip counts from its own start.
    assert coordinator.trv_active is True

    coordinator.async_unload()


async def test_unreachable_heat_source_keeps_its_last_known_heat_state(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    assert coordinator.trv_active is True

    hass.states.async_set("climate.heat_source", "unavailable")
    await hass.async_block_till_done()

    assert coordinator.trv_active is True
    assert coordinator.mpc._learner._enabled is True

    hass.states.async_set("climate.heat_source", "off")
    await hass.async_block_till_done()
    hass.states.async_set("climate.heat_source", "unknown")
    await hass.async_block_till_done()

    assert coordinator.trv_active is False

    coordinator.async_unload()


async def test_heat_source_unreachable_since_startup_stays_inactive(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heat_source", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_opening_a_window_writes_frost_protection_at_once(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "17.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    await hass.async_block_till_done()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert calls[-1].data["temperature"] == ENTRY_DATA["frost_protection_temperature_c"]

    coordinator.async_unload()


async def test_unavailable_flow_entity_keeps_the_last_flow_temperature(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch_calls: list[ServiceCall] = []

    async def climate_handler(call: ServiceCall) -> None:
        return None

    async def switch_handler(call: ServiceCall) -> None:
        switch_calls.append(call)

    _seed_entities(hass, flow_temp=42.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)
    await coordinator.async_setup()
    await coordinator.async_start()
    assert coordinator.trv_active is True

    # A template reload drops the sensor for a moment.
    hass.states.async_set("sensor.flow_temperature", "unavailable")
    await hass.async_block_till_done()

    assert coordinator.current_flow_temperature_c == 42.0
    assert coordinator.trv_active is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert not [call for call in switch_calls if call.service == "turn_off"]

    hass.states.async_set("sensor.flow_temperature", "25.0")
    await hass.async_block_till_done()
    await _flow_stays_below_the_threshold(hass, monkeypatch, "24.0")

    assert coordinator.trv_active is False

    coordinator.async_unload()


async def test_flow_entity_unavailable_since_startup_stays_inactive(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.flow_temperature", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()

    assert coordinator.current_flow_temperature_c is None
    assert coordinator.trv_active is False

    coordinator.async_unload()


VALVE_ENTITY = "sensor.heizung_wohnzimmer_pi_heating_demand"


def _trv_with_valve() -> TrvBinding:
    return _trvs(valve_position_entity=VALVE_ENTITY)[0]


async def test_valve_position_feeds_the_measured_heating_power(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "90")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])

    result = coordinator.last_result
    assert result.input.valve_positions_pct == [90.0]
    assert result.input.valve_position_missing is False
    assert coordinator.trv_valve_positions == {"climate.heizung_wohnzimmer": 90.0}
    # 90 % opening gives nearly the full available power, whatever the demand.
    assert result.measured_heating_power_w > 0.9 * result.available_heating_power_w

    hass.states.async_set(VALVE_ENTITY, "0")
    await hass.async_block_till_done()
    assert coordinator.last_result.measured_heating_power_w == 0

    coordinator.async_unload()


async def test_unavailable_valve_position_is_reported_missing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "unavailable")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])

    assert coordinator.last_result.input.valve_position_missing is True
    assert coordinator.trv_valve_positions == {"climate.heizung_wohnzimmer": None}

    coordinator.async_unload()


async def test_a_trv_silent_for_75_minutes_has_no_known_demand(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "100")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])

    for _ in range(14):
        await _poll(hass, freezer, 5)
    # An unchanged opening is not reported again; a TRV with constant values
    # reports about once an hour and is still current.
    assert coordinator.room_demand_pct == 100.0

    for _ in range(2):
        await _poll(hass, freezer, 5)
    assert coordinator.room_demand_pct is None
    assert coordinator.trv_valve_positions == {"climate.heizung_wohnzimmer": None}
    assert coordinator.last_result.input.valve_position_missing is True

    # The TRV reports its temperature again: the opening is current again.
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 18.1}
    )
    await _poll(hass, freezer, 5)
    assert coordinator.room_demand_pct == 100.0
    assert coordinator.last_result.input.valve_position_missing is False

    coordinator.async_unload()


async def test_without_valve_position_the_room_reports_no_demand(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    result = coordinator.last_result
    assert result.input.valve_positions_pct == [None]
    # Without any measured demand the room has no heat to learn from.
    assert result.input.valve_position_missing is True
    assert coordinator.trv_valve_positions == {"climate.heizung_wohnzimmer": None}
    assert coordinator.room_demand_pct is None
    assert result.measured_heating_power_w == 0

    coordinator.async_unload()


async def test_a_setpoint_echo_does_not_trigger_a_second_write(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_fake_climate_set_temperature(hass)
    coordinator = await _setup_coordinator(hass)
    await hass.async_block_till_done()
    written = coordinator.trv_target_temperatures["climate.heizung_wohnzimmer"]
    writes = coordinator.trv_writer.setpoint_writes.get("climate.heizung_wohnzimmer", 0)
    assert writes >= 1

    # The TRV reports its previous target once more, then the new one.
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": written - 1.0},
    )
    await hass.async_block_till_done()

    assert (
        coordinator.trv_writer.setpoint_writes["climate.heizung_wohnzimmer"] == writes
    )

    coordinator.async_unload()


async def test_no_target_is_written_to_an_unavailable_trv(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heizung_wohnzimmer", "unavailable")
    calls = _register_fake_climate_set_temperature(hass)
    coordinator = await _setup_coordinator(hass)
    await hass.async_block_till_done()

    assert calls == []
    assert coordinator.trv_writer.setpoint_writes == {}

    coordinator.async_unload()


ADAPTATION_ENTITY = "sensor.heizung_wohnzimmer_adaptation_run_status"


def _trvs_with_adaptation() -> list[TrvBinding]:
    return _trvs(
        adaptation_status_entity=ADAPTATION_ENTITY,
        adaptation_busy=StatusMatch(states=frozenset({"in_progress"})),
    )


MOUNTED_ENTITY = "binary_sensor.heizung_wohnzimmer_mounted_mode_active"


def _trvs_with_calibration() -> list[TrvBinding]:
    return _trvs(
        adaptation_status_entity=ADAPTATION_ENTITY,
        adaptation_busy=StatusMatch(states=frozenset({"in_progress"})),
        adaptation_ready=StatusMatch(states=frozenset({"found"})),
        mounted_mode_entity=MOUNTED_ENTITY,
    )


@pytest.mark.parametrize(
    ("adaptation", "mounted"),
    [
        pytest.param("none", "off", id="not_calibrated"),
        pytest.param("lost", "off", id="calibration_lost"),
        pytest.param("unavailable", "off", id="status_unavailable"),
        pytest.param("found", "on", id="mounting"),
    ],
)
async def test_an_uncalibrated_trv_stops_learning_but_not_writes(
    hass: HomeAssistant, adaptation: str, mounted: str
) -> None:
    _seed_entities(hass)
    hass.states.async_set(ADAPTATION_ENTITY, adaptation)
    hass.states.async_set(MOUNTED_ENTITY, mounted)
    coordinator = await _setup_coordinator(hass, trvs=_trvs_with_calibration())
    await hass.async_block_till_done()

    assert coordinator.last_result.input.trv_adapting is True
    assert coordinator.trv_writer.setpoint_writes["climate.heizung_wohnzimmer"] == 1

    # Calibrated and mounted: the room learns again.
    hass.states.async_set(ADAPTATION_ENTITY, "found")
    hass.states.async_set(MOUNTED_ENTITY, "off")
    await hass.async_block_till_done()

    assert coordinator.last_result.input.trv_adapting is False

    coordinator.async_unload()


async def test_nothing_is_written_to_a_trv_while_it_adapts(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    hass.states.async_set("switch.heizung_wohnzimmer_trv_active", "on")
    hass.states.async_set(ADAPTATION_ENTITY, "in_progress")
    now = [time.time()]
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: now[0])
    switch_calls: list[ServiceCall] = []

    async def switch_handler(call: ServiceCall) -> None:
        switch_calls.append(call)

    coordinator = await _setup_coordinator(hass, trvs=_trvs_with_adaptation())
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)
    await hass.async_block_till_done()

    assert coordinator.trv_writer.setpoint_writes == {}
    assert coordinator.last_result.input.trv_adapting is True
    hass.states.async_set("sensor.flow_temperature", "20.0")
    await hass.async_block_till_done()
    now[0] += FLOW_DIP_TOLERANCE_S + 1
    hass.states.async_set("sensor.flow_temperature", "19.0")
    await hass.async_block_till_done()
    assert coordinator.trv_active is False
    assert switch_calls == []

    # The run ends: the held target and switch go out at once.
    hass.states.async_set(ADAPTATION_ENTITY, "found")
    await hass.async_block_till_done()

    assert coordinator.trv_writer.setpoint_writes["climate.heizung_wohnzimmer"] == 1
    assert [call.service for call in switch_calls] == ["turn_off"]
    assert coordinator.last_result.input.trv_adapting is False

    coordinator.async_unload()


async def test_renaming_a_trv_entity_reloads_the_room(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    reloads: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reloads.append)
    coordinator = await _setup_coordinator(hass)

    er.async_get(hass).async_update_entity(
        "switch.heizung_wohnzimmer_trv_active",
        new_entity_id="switch.heizung_wohnzimmer_heat_available",
    )
    await hass.async_block_till_done()

    assert reloads == [coordinator.hub_entry.entry_id]

    coordinator.async_unload()


async def _setup_with_calls(
    hass: HomeAssistant, **overrides
) -> tuple[HeatingRoomCoordinator, list[ServiceCall]]:
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, **overrides})
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator, calls


async def _poll(hass: HomeAssistant, freezer, minutes: float) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup_recording(
    hass: HomeAssistant, **overrides
) -> tuple[HeatingRoomCoordinator, list[ServiceCall], list[ServiceCall]]:
    """Coordinator with climate and switch calls recorded separately."""
    climate_calls: list[ServiceCall] = []
    switch_calls: list[ServiceCall] = []

    async def climate_handler(call: ServiceCall) -> None:
        climate_calls.append(call)

    async def switch_handler(call: ServiceCall) -> None:
        switch_calls.append(call)

    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, **overrides})
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs())
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator, climate_calls, switch_calls


async def test_an_unknown_heat_source_cuts_heat_but_keeps_the_mode(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("switch.heizung_wohnzimmer_trv_active", "on")
    hass.states.async_set("climate.heat_source", "unavailable")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert [call.data["temperature"] for call in climate_calls] == [22.0]
    assert [call.service for call in switch_calls] == ["turn_off"]

    coordinator.async_unload()


async def test_nothing_is_computed_or_written_before_start(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    climate_calls: list[ServiceCall] = []

    async def climate_handler(call: ServiceCall) -> None:
        climate_calls.append(call)

    hass.services.async_register("climate", "set_temperature", climate_handler)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs())
    await coordinator.async_setup()
    hass.states.async_set("sensor.wohnzimmer_temperatur", "19.0")
    await hass.async_block_till_done()

    assert coordinator.last_result is None
    assert climate_calls == []

    await coordinator.async_start()
    await hass.async_block_till_done()

    assert coordinator.last_result is not None
    assert coordinator.last_result.input.room_temp_c == 19.0
    assert [call.data["temperature"] for call in climate_calls] == [22.0]

    coordinator.async_unload()


async def test_setup_removes_the_old_valve_curve_file(
    hass: HomeAssistant, hass_storage
) -> None:
    _seed_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    key = f"{DOMAIN}_wohnzimmer_{entry.entry_id}_valve_curve"
    hass_storage[key] = {"version": 1, "key": key, "data": {"trvs": {}}}
    _register_fake_climate_set_temperature(hass)
    coordinator = _coordinator(hass, entry, [_trv_with_valve()])

    await coordinator.async_setup()

    await coordinator.async_start()

    assert key not in hass_storage
    coordinator.async_unload()


async def test_flow_boost_after_a_mode_jump_until_the_room_is_close(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "100")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])
    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    await _poll(hass, freezer, 5)
    assert coordinator.flow_boost_active is False
    assert coordinator.flow_boost_temperature_c is None

    await coordinator.async_set_manual_heat_mode(HeatMode.COMFORT)
    await _poll(hass, freezer, 1)
    assert coordinator.flow_boost_active is True
    recovery_c = coordinator.normal_result.recovery_flow_temperature_c
    assert recovery_c > 0
    assert coordinator.flow_boost_temperature_c == recovery_c
    # A recovering room says nothing about the flow.
    assert coordinator.feedback_sample is None
    assert coordinator.supply[:2] == (RoomSupply.NOT_RATED, SupplyReason.BOOST)

    target_c = coordinator.normal_target_temperature_c
    for room_c in (19.0, 19.5, 20.0, 20.5, target_c - 0.1):
        hass.states.async_set("sensor.wohnzimmer_temperatur", f"{room_c:.1f}")
        await _poll(hass, freezer, 5)
    await _poll(hass, freezer, 5)
    assert coordinator.flow_boost_active is False

    coordinator.async_unload()


async def test_flow_boost_ends_after_the_recovery_horizon(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)
    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    await _poll(hass, freezer, 5)
    await coordinator.async_set_manual_heat_mode(HeatMode.COMFORT)
    await _poll(hass, freezer, 1)
    assert coordinator.flow_boost_active is True

    for _ in range(6 * 12):
        hass.states.async_set("sensor.wohnzimmer_temperatur", "18.5")
        await _poll(hass, freezer, 5)
    assert coordinator.flow_boost_active is False

    coordinator.async_unload()


async def test_feedback_sample_from_valve_and_room_sensor(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "95")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])
    await _poll(hass, freezer, 5)

    sample = coordinator.feedback_sample
    assert sample.room == "Wohnzimmer"
    assert sample.demand_pct == 95.0
    assert sample.deficit_c == pytest.approx(
        coordinator.normal_target_temperature_c - 18.5, abs=0.01
    )

    # An open window takes the room out.
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    await _poll(hass, freezer, 1)
    assert coordinator.feedback_sample is None

    coordinator.async_unload()


async def test_feedback_sample_has_no_deficit_while_the_room_sensor_is_stale(
    hass: HomeAssistant, freezer
) -> None:
    from heating_controller.controller.mpc.sensors import ROOM_SENSOR_MAX_AGE_S

    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "98")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])
    await _poll(hass, freezer, 5)
    assert coordinator.feedback_sample.deficit_c is not None

    # The room sensor goes quiet; the TRV keeps reporting, warm at the
    # radiator. Its reading must not stand in for the room.
    for _ in range(int(ROOM_SENSOR_MAX_AGE_S / 600) + 2):
        hass.states.async_set(
            "climate.heizung_wohnzimmer", "heat", {"current_temperature": 23.0}
        )
        hass.states.async_set(VALVE_ENTITY, "98")
        await _poll(hass, freezer, 10)

    assert coordinator.last_result.input.room_sensor_fallback
    sample = coordinator.feedback_sample
    assert sample.demand_pct == 98.0
    assert sample.deficit_c is None
    # Saturated: judged on the opening alone.
    assert coordinator.supply[0] is RoomSupply.UNDERSUPPLIED

    coordinator.async_unload()


async def test_feedback_sample_needs_a_known_demand(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    # No valve position configured: nothing to judge the flow by.
    assert coordinator.feedback_sample is None
    assert coordinator.supply[:2] == (
        RoomSupply.NOT_RATED,
        SupplyReason.DEMAND_UNKNOWN,
    )

    coordinator.async_unload()


@pytest.mark.parametrize(
    ("valve_pct", "room_c", "expected"),
    [
        ("100", "18.5", RoomSupply.UNDERSUPPLIED),
        ("92", "22.0", RoomSupply.SUPPLIED),
        # Open, but below ninety: not at the limit.
        ("85", "22.0", RoomSupply.OVERSUPPLIED),
        # Open, but well above the target: the TRV's own reading, not the flow.
        ("95", "22.5", RoomSupply.OVERSUPPLIED),
        ("30", "22.0", RoomSupply.OVERSUPPLIED),
    ],
)
async def test_supply_judged_on_the_valves_like_the_feedback(
    hass: HomeAssistant, freezer, valve_pct, room_c, expected
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, valve_pct)
    hass.states.async_set("sensor.wohnzimmer_temperatur", room_c)
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])
    await _poll(hass, freezer, 5)

    assert coordinator.trv_active is True
    supply, reason, sample = coordinator.supply
    assert supply is expected
    assert reason is None
    assert sample.demand_pct == float(valve_pct)

    coordinator.async_unload()


async def test_supply_not_rated_without_heat_at_the_valves(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass, heat_available=False)
    hass.states.async_set(VALVE_ENTITY, "100")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])
    await _poll(hass, freezer, 5)

    supply, reason, sample = coordinator.supply
    assert (supply, reason) == (RoomSupply.NOT_RATED, SupplyReason.NO_HEAT)
    # The opening is still reported.
    assert sample.demand_pct == 100.0

    coordinator.async_unload()


class _CatchingUpHouse(DetachedHouse):
    catching_up = True

    def rooms_catching_up(self) -> bool:
        return self.catching_up


async def test_supply_not_rated_while_catching_up_after_a_pause(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set(VALVE_ENTITY, "100")
    coordinator = await _setup_coordinator(hass, trvs=[_trv_with_valve()])
    await _poll(hass, freezer, 5)
    house = _CatchingUpHouse()
    coordinator.house = house

    assert coordinator.supply[:2] == (RoomSupply.NOT_RATED, SupplyReason.CATCH_UP)

    house.catching_up = False
    assert coordinator.supply[:2] == (RoomSupply.UNDERSUPPLIED, None)

    coordinator.async_unload()


async def test_a_room_sensor_that_stopped_reporting_goes_stale(
    hass: HomeAssistant, freezer
) -> None:
    from heating_controller.controller.mpc.sensors import ROOM_SENSOR_MAX_AGE_S

    _seed_entities(hass)
    _register_fake_climate_set_temperature(hass)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs())
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    assert coordinator.mpc.get_room_temperature().room_sensor_temp_c == 18.5

    # The sensor stays available with its last value; the poll reads it again.
    freezer.tick(timedelta(seconds=ROOM_SENSOR_MAX_AGE_S + 60))
    coordinator._refresh_temperature_inputs()

    room = coordinator.mpc.get_room_temperature()
    assert room.room_sensor_fallback
    assert room.room_sensor_temp_c is None

    # A report of the same value makes it current again.
    hass.states.async_set("sensor.wohnzimmer_temperatur", "18.5")
    coordinator._refresh_temperature_inputs()

    assert coordinator.mpc.get_room_temperature().room_sensor_temp_c == 18.5

    coordinator.async_unload()

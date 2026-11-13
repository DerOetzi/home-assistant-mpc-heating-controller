import dataclasses
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
    FlowSupplyStatus,
    HeatMode,
    UaBalanceOutcome,
)
from heating_controller.coordinator import (
    ADAPTATION_MAX_HOLD_S,
    FLOW_TEMPERATURE_MAX_GAP_S,
    ROOM_SENSOR_STARTUP_GRACE_S,
    HeatingRoomCoordinator,
)
from heating_controller.profiles import StatusMatch, TrvBinding
from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
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
    "mpc_demand_hysteresis_pct": 5.0,
    "mpc_hold_time_s": 300.0,
    "mpc_hold_override_demand_pct": 40.0,
    "mpc_max_demand_step_pct": 20.0,
    "max_sensor_age_s": 1800.0,
    "target_band_c": 0.1,
    "trv_setpoint_mode": "absolute",
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
                "min_target_temperature_c": 5.0,
                "max_target_temperature_c": 28.0,
                "target_temperature_step_c": 0.5,
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
) -> None:
    # Registered first, so the registry hands out the entity ids set below.
    register_trv_device(hass)
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 18.0}
    )
    hass.states.async_set("sensor.wohnzimmer_temperatur", "18.5")
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "off")
    hass.states.async_set("sensor.outdoor_temperature", "-3.0")
    hass.states.async_set("climate.heat_source", "heat" if heat_available else "off")
    hass.states.async_set("sensor.flow_temperature", str(flow_temp))
    hass.states.async_set("input_number.flow_threshold", "30.0")


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

    assert coordinator.mpc.get_room_temperature_result().valid is True

    for _ in range(7):
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.mpc.get_room_temperature_result().valid is True

    coordinator.async_unload()


async def test_window_open_forces_frost_protection(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)

    await coordinator.async_setup()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    coordinator.async_unload()


async def test_manual_selection_bypasses_forced_frost_protection_for_eco(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    assert coordinator.current_heat_mode == HeatMode.ECO
    assert coordinator.is_automation_active is False

    coordinator.async_unload()


async def test_manual_selection_bypasses_forced_frost_protection_for_comfort(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

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

    await coordinator.async_set_manual_heat_mode(HeatMode.COMFORT)
    assert coordinator.is_automation_active is False

    await coordinator.async_set_manual_heat_mode(HeatMode.FROST_PROTECTION)
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.is_automation_active is False

    coordinator.async_unload()


async def test_unblock_reactivates_automation_and_reasserts_forced_frost_protection(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

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

    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert coordinator.is_automation_active is True

    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    assert coordinator.current_heat_mode == HeatMode.ECO
    assert coordinator.is_automation_active is False

    await coordinator.async_unblock()
    assert coordinator.is_automation_active is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT

    coordinator.async_unload()


async def test_heating_unavailable_disables_learning(hass: HomeAssistant) -> None:
    _seed_entities(hass, heat_available=False)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.mpc._learner._enabled is False

    coordinator.async_unload()


async def test_heat_source_off_with_hot_flow_forces_frost_protection(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, heat_available=False, flow_temp=49.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

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

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

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

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.normal_heat_mode == HeatMode.COMFORT
    assert coordinator.normal_target_temperature_c == 22.0
    assert coordinator.normal_result is not None
    assert coordinator.normal_min_flow_temperature_c is not None
    assert coordinator.normal_min_flow_temperature_c > 0
    assert (
        coordinator.base_temperature_c == ENTRY_DATA["frost_protection_temperature_c"]
    )
    assert coordinator.trv_active is False
    assert coordinator.flow_supply_status == FlowSupplyStatus.SOURCE_INACTIVE

    coordinator.async_unload()


async def test_flow_supply_status_undersupplied_when_running_below_requirement(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, flow_temp=31.0)
    hass.states.async_set("sensor.outdoor_temperature", "5.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.trv_active is True
    required = coordinator.normal_min_flow_temperature_c
    assert required is not None and required > 31.0
    assert coordinator.flow_supply_status == FlowSupplyStatus.UNDERSUPPLIED

    coordinator.async_unload()


async def test_flow_supply_status_sufficient_when_flow_covers_requirement(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, flow_temp=60.0)
    hass.states.async_set("sensor.outdoor_temperature", "5.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.trv_active is True
    required = coordinator.normal_min_flow_temperature_c
    assert required is not None and required < 60.0
    assert coordinator.flow_supply_status == FlowSupplyStatus.SUFFICIENT

    coordinator.async_unload()


async def test_flow_supply_status_below_threshold_for_a_never_binding_requirement(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    # Close to the setpoint: at the design spread the hold flow only drops
    # below the threshold for a small loss.
    hass.states.async_set("sensor.outdoor_temperature", "21.5")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.5")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    required = coordinator.normal_min_flow_temperature_c
    assert required is not None
    assert 0 < required < coordinator.flow_threshold_c
    assert coordinator.flow_supply_status == FlowSupplyStatus.BELOW_THRESHOLD
    assert coordinator.normal_heat_mode == HeatMode.COMFORT
    assert coordinator.normal_target_temperature_c == 22.0

    coordinator.async_unload()


async def test_flow_supply_status_no_requirement_when_ambient_holds_target(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "24.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "24.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.normal_min_flow_temperature_c == 0
    assert coordinator.flow_supply_status == FlowSupplyStatus.NO_REQUIREMENT

    coordinator.async_unload()


async def test_flow_supply_status_surplus_while_the_room_coasts_above_target(
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

    assert coordinator.normal_flow_gate_closed is True
    assert coordinator.normal_min_flow_temperature_c == 0
    assert coordinator.flow_supply_status == FlowSupplyStatus.SURPLUS
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

    assert coordinator.normal_flow_gate_closed is False
    assert coordinator.normal_min_flow_temperature_c > 0

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
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    await coordinator.async_setup()

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

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    coordinator.async_unload()


async def test_house_and_room_switch_both_gate_comfort(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    data = {**ENTRY_DATA, "room_comfort_switch": True}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

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
    assert before_restart.normal_flow_gate_closed is True
    before_restart.async_unload()

    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.4")

    # Same entry (same store), now with the default close hold time, so the
    # gate cannot simply close again on the first cycle.
    restarted_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, entry_id=closing_entry.entry_id
    )
    after_restart = _coordinator(hass, restarted_entry, _trvs())
    await after_restart.async_setup()

    assert after_restart.normal_flow_gate_closed is True
    assert after_restart.flow_supply_status == FlowSupplyStatus.SURPLUS
    after_restart.async_unload()

    fresh_entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    fresh_entry.add_to_hass(hass)
    without_stored_state = _coordinator(hass, fresh_entry, _trvs())
    await without_stored_state.async_setup()

    assert without_stored_state.normal_flow_gate_closed is False
    without_stored_state.async_unload()


async def test_compute_waits_for_the_room_sensor_after_startup(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.last_result is None
    assert coordinator.normal_result is None
    assert not calls

    hass.states.async_set("sensor.wohnzimmer_temperatur", "21.0")
    await hass.async_block_till_done()

    assert coordinator.last_result is not None
    assert coordinator.last_result.input.room_temp_c == 21.0

    coordinator.async_unload()


async def test_compute_falls_back_to_trvs_when_room_sensor_stays_silent(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    assert coordinator.last_result is None

    freezer.tick(timedelta(seconds=ROOM_SENSOR_STARTUP_GRACE_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator.last_result is not None
    assert coordinator.last_result.input.room_temp_c == 18.0

    coordinator.async_unload()


async def test_flow_threshold_follows_the_linked_helper(hass: HomeAssistant) -> None:
    _seed_entities(hass, flow_temp=32.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.flow_threshold_c == 30.0
    assert coordinator.trv_active is True

    hass.states.async_set("input_number.flow_threshold", "35.0")
    await hass.async_block_till_done()

    assert coordinator.flow_threshold_c == 35.0
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
    ua_until = learner._ua_blocked_until_ts - before_close
    capacity_until = learner._capacity_blocked_until_ts - before_close
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


async def test_flow_temperature_comes_from_the_flow_entity(hass: HomeAssistant) -> None:
    _seed_entities(hass, flow_temp=42.0)
    hass.states.async_set("climate.heat_source", "heat", {"current_temperature": 20.0})
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.current_flow_temperature_c == 42.0
    assert coordinator.trv_active is True

    hass.states.async_set("sensor.flow_temperature", "25.0")
    await hass.async_block_till_done()

    assert coordinator.current_flow_temperature_c == 25.0
    assert coordinator.last_result.input.flow_temp_c == 25.0
    assert coordinator.trv_active is False

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

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    coordinator.async_unload()


async def test_opening_a_window_closes_the_valve_without_smoothing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "17.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = _coordinator(hass, entry, _trvs())
    calls = _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    # Without the bypass the hold time would keep this demand for 300 s.
    assert coordinator.last_result.demand_pct > 0

    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    await hass.async_block_till_done()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.last_result.demand_pct == 0
    assert calls[-1].data["temperature"] == _trvs()[0].min_target_temperature_c

    coordinator.async_unload()


async def test_unavailable_flow_entity_keeps_the_last_flow_temperature(
    hass: HomeAssistant,
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

    assert coordinator.current_flow_temperature_c is None
    assert coordinator.trv_active is False

    coordinator.async_unload()


async def test_target_band_comes_from_the_entry(hass: HomeAssistant) -> None:
    coordinator = await _setup_coordinator(hass, target_band_c=0.25)

    assert coordinator.mpc._target_band_c == 0.25


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


async def test_without_valve_position_the_demand_stands_in(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    result = coordinator.last_result
    assert result.input.valve_positions_pct == [None]
    assert result.input.valve_position_missing is False
    assert coordinator.trv_valve_positions == {"climate.heizung_wohnzimmer": None}
    assert result.measured_heating_power_w == pytest.approx(
        result.requested_heating_power_w, abs=0.05
    )

    coordinator.async_unload()


async def test_relative_mode_sends_a_target_just_above_the_trv_reading(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    calls = _register_fake_climate_set_temperature(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA, "trv_setpoint_mode": "relative"}
    )
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs())
    await coordinator.async_setup()
    await hass.async_block_till_done()

    # TRV reads 18.0 °C; the room is below its setpoint and asks for heat.
    target = coordinator.last_result.trv_targets[0]
    assert coordinator.last_result.demand_pct > 0
    assert 18.5 <= target <= 21.0
    assert calls and calls[-1].data["temperature"] == target
    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] >= 1

    coordinator.async_unload()


async def test_a_setpoint_echo_does_not_trigger_a_second_write(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_fake_climate_set_temperature(hass)
    coordinator = await _setup_coordinator(hass)
    await hass.async_block_till_done()
    written = coordinator.last_result.trv_targets[0]
    writes = coordinator.trv_setpoint_writes.get("climate.heizung_wohnzimmer", 0)
    assert writes >= 1

    # The TRV reports its previous target once more, then the new one.
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": written - 1.0},
    )
    await hass.async_block_till_done()

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == writes

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
    assert coordinator.trv_setpoint_writes == {}

    coordinator.async_unload()


async def test_a_trv_switches_between_shut_and_open_at_most_every_15_minutes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    now = [time.time()]
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: now[0])
    coordinator = await _setup_coordinator(hass)
    await hass.async_block_till_done()
    result = coordinator.last_result
    assert result.trv_targets[0] > 5.0
    opened = coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"]
    shut = dataclasses.replace(result, trv_targets=[5.0])
    # The TRV took the target.
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": result.trv_targets[0]},
    )
    await hass.async_block_till_done()
    opened = coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"]

    # Five minutes after opening the controller wants it shut: held back.
    now[0] += 5 * 60
    await coordinator._async_write_trv_targets(shut.trv_targets)
    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == opened

    # Forced frost protection (open window) shuts it at once.
    await coordinator._async_write_trv_targets(shut.trv_targets, forced=True)
    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == opened + 1

    coordinator.async_unload()


async def test_a_trv_switch_goes_through_after_15_minutes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    now = [time.time()]
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: now[0])
    coordinator = await _setup_coordinator(hass)
    await hass.async_block_till_done()
    shut = dataclasses.replace(coordinator.last_result, trv_targets=[5.0])
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {
            "current_temperature": 18.0,
            "temperature": coordinator.last_result.trv_targets[0],
        },
    )
    await hass.async_block_till_done()
    opened = coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"]

    now[0] += 16 * 60
    await coordinator._async_write_trv_targets(shut.trv_targets)

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == opened + 1

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
    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == 1

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

    assert coordinator.trv_setpoint_writes == {}
    assert coordinator.last_result.input.trv_adapting is True
    hass.states.async_set("sensor.flow_temperature", "20.0")
    await hass.async_block_till_done()
    assert coordinator.trv_active is False
    assert switch_calls == []

    # The run ends: the held target and switch go out at once.
    hass.states.async_set(ADAPTATION_ENTITY, "found")
    await hass.async_block_till_done()

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == 1
    assert [call.service for call in switch_calls] == ["turn_off"]
    assert coordinator.last_result.input.trv_adapting is False

    coordinator.async_unload()


async def test_forced_frost_protection_writes_during_an_adaptation(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(ADAPTATION_ENTITY, "in_progress")
    coordinator = await _setup_coordinator(hass, trvs=_trvs_with_adaptation())
    await hass.async_block_till_done()
    assert coordinator.trv_setpoint_writes == {}

    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    await hass.async_block_till_done()

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == 1

    coordinator.async_unload()


async def test_an_adaptation_holds_writes_back_no_longer_than_the_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    hass.states.async_set(ADAPTATION_ENTITY, "in_progress")
    now = [time.time()]
    monkeypatch.setattr("heating_controller.coordinator.time.time", lambda: now[0])
    coordinator = await _setup_coordinator(hass, trvs=_trvs_with_adaptation())
    await hass.async_block_till_done()
    assert coordinator.trv_setpoint_writes == {}

    now[0] += ADAPTATION_MAX_HOLD_S + 1
    await coordinator._async_write_trv_targets(coordinator.last_result.trv_targets)

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == 1

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
    await hass.async_block_till_done()
    return coordinator, calls


async def _poll(hass: HomeAssistant, freezer, minutes: float) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_startup_without_outdoor_temperature_writes_nothing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    coordinator, calls = await _setup_with_calls(hass, trv_setpoint_mode="relative")
    coordinator.mpc.run_learning_cycle()

    assert calls == []
    assert coordinator.outdoor_fallback_active is False
    assert coordinator.last_result is None
    # Learning pauses all the same.
    assert coordinator.mpc.outdoor_temperature_missing is True
    assert coordinator.mpc.get_ua_balance_info().outcome == (
        UaBalanceOutcome.OUTDOOR_TEMPERATURE_MISSING
    )

    coordinator.async_unload()


async def test_missing_outdoor_temperature_hands_the_trvs_the_base_target(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    coordinator, calls = await _setup_with_calls(hass, trv_setpoint_mode="relative")

    # Still missing after a full poll cycle: an outage, not a startup.
    await _poll(hass, freezer, 5)

    # Comfort is 22 °C; the TRV reads 18 °C. A relative target would sit
    # just above that reading, the fallback is the absolute target.
    assert calls and calls[-1].data["temperature"] == 22.0
    assert coordinator.outdoor_fallback_active is True
    assert coordinator.outdoor_fallback_target_c == 22.0
    assert coordinator.last_result is None
    assert coordinator.mpc.outdoor_temperature_missing is True

    coordinator.async_unload()


async def test_missing_outdoor_temperature_keeps_forced_frost_protection(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    coordinator, calls = await _setup_with_calls(hass)
    await _poll(hass, freezer, 5)

    assert calls and calls[-1].data["temperature"] == 8.0
    assert coordinator.outdoor_fallback_target_c == 8.0

    coordinator.async_unload()


async def test_outdoor_temperature_arriving_after_startup_needs_no_fallback(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    coordinator, calls = await _setup_with_calls(hass)

    hass.states.async_set("sensor.outdoor_temperature", "-3.0")
    await hass.async_block_till_done()

    assert coordinator.outdoor_fallback_active is False
    assert coordinator.last_result is not None
    assert (
        calls
        and calls[-1].data["temperature"] == (coordinator.last_result.trv_targets[0])
    )

    coordinator.async_unload()


async def test_the_mpc_takes_over_once_the_outdoor_temperature_is_back(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator, calls = await _setup_with_calls(hass, trv_setpoint_mode="relative")
    assert coordinator.outdoor_fallback_active is False

    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    await hass.async_block_till_done()
    assert coordinator.outdoor_fallback_active is True
    assert calls[-1].data["temperature"] == 22.0

    hass.states.async_set("sensor.outdoor_temperature", "-3.0")
    await hass.async_block_till_done()

    assert coordinator.outdoor_fallback_active is False
    assert coordinator.outdoor_fallback_target_c is None
    assert coordinator.last_result is not None
    assert coordinator.mpc.outdoor_temperature_missing is False
    assert calls[-1].data["temperature"] == coordinator.last_result.trv_targets[0]
    assert calls[-1].data["temperature"] != 22.0

    coordinator.async_unload()


async def test_a_missing_room_temperature_alone_writes_nothing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "unavailable")
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": None}
    )
    coordinator, calls = await _setup_with_calls(hass)
    coordinator._awaiting_room_sensor = False
    await coordinator.async_unblock()
    await hass.async_block_till_done()

    assert calls == []
    assert coordinator.outdoor_fallback_active is False

    coordinator.async_unload()


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
    await hass.async_block_till_done()
    return coordinator, climate_calls, switch_calls


async def test_startup_with_an_unknown_heat_source_writes_and_switches_nothing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heat_source", "unavailable")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    assert climate_calls == []
    assert switch_calls == []
    assert coordinator.last_result is None

    coordinator.async_unload()


async def test_startup_without_a_flow_temperature_writes_and_switches_nothing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.flow_temperature", "unavailable")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    assert climate_calls == []
    assert switch_calls == []

    coordinator.async_unload()


async def test_the_mpc_runs_once_the_heat_source_reports(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heat_source", "unavailable")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    hass.states.async_set("climate.heat_source", "heat")
    await hass.async_block_till_done()

    assert coordinator.trv_active is True
    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert coordinator.last_result is not None
    assert climate_calls
    assert (
        climate_calls[-1].data["temperature"]
        == (coordinator.last_result.trv_targets[0])
    )
    assert all(call.service == "turn_on" for call in switch_calls)

    coordinator.async_unload()


async def test_a_heat_source_unknown_past_the_grace_forces_frost_protection(
    hass: HomeAssistant, freezer
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heat_source", "unavailable")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert climate_calls and climate_calls[-1].data["temperature"] == 5.0
    assert switch_calls and switch_calls[-1].service == "turn_off"

    coordinator.async_unload()


async def test_an_open_window_forces_frost_protection_during_the_grace(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heat_source", "unavailable")
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "on")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert climate_calls and climate_calls[-1].data["temperature"] == 5.0
    # Whether the TRVs get heat is still unknown: the switches wait.
    assert switch_calls == []

    coordinator.async_unload()


async def test_a_mode_the_user_picked_goes_through_during_the_grace(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("climate.heat_source", "unavailable")
    coordinator, climate_calls, switch_calls = await _setup_recording(hass)

    await coordinator.async_set_manual_heat_mode(HeatMode.FROST_PROTECTION)
    await hass.async_block_till_done()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert climate_calls and climate_calls[-1].data["temperature"] == 5.0
    assert switch_calls == []

    coordinator.async_unload()


async def test_the_reported_trv_target_reaches_the_controller(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": 19.0},
    )
    hass.states.async_set(VALVE_ENTITY, "40")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    _register_fake_climate_set_temperature(hass)
    coordinator = _coordinator(hass, entry, [_trv_with_valve()])
    reported: list[tuple[int, float | None]] = []
    set_trv_target = coordinator.mpc.set_trv_target

    def spy(index: int, value: float | None) -> None:
        reported.append((index, value))
        set_trv_target(index, value)

    coordinator.mpc.set_trv_target = spy
    await coordinator.async_setup()
    assert (0, 19.0) in reported

    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": 21.5},
    )
    await hass.async_block_till_done()

    assert reported[-1] == (0, 21.5)
    coordinator.async_unload()


async def _learn_one_valve_sample(
    hass: HomeAssistant, freezer
) -> tuple[HeatingRoomCoordinator, MockConfigEntry]:
    """Set up a room with a valve TRV and let it finish one curve sample."""
    _seed_entities(hass)
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": 18.0},
    )
    hass.states.async_set(VALVE_ENTITY, "0")
    _register_fake_climate_set_temperature(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, [_trv_with_valve()])
    await coordinator.async_setup()

    # The TRV takes a setpoint 2 K above its reading and opens to 60 %.
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": 20.0},
    )
    hass.states.async_set(VALVE_ENTITY, "60")
    await hass.async_block_till_done()
    await _poll(hass, freezer, 5)
    await _poll(hass, freezer, 5.5)
    # The valve stood still for 5 minutes: the ramp has ended.
    await _poll(hass, freezer, 5)
    return coordinator, entry


def _valve_curve_storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}_wohnzimmer_{entry.entry_id}_valve_curve"


async def test_valve_curve_samples_survive_a_restart(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    before_restart, entry = await _learn_one_valve_sample(hass, freezer)

    bins = before_restart.mpc.valve_curve_learners["climate.heizung_wohnzimmer"].bins()
    assert [(round(b.offset_c, 2), b.opening_pct, b.samples) for b in bins] == [
        (2.0, 60.0, 1)
    ]
    # One delayed write bundles the samples of the next 10 minutes.
    assert _valve_curve_storage_key(entry) not in hass_storage
    await _poll(hass, freezer, 11)
    assert _valve_curve_storage_key(entry) in hass_storage
    before_restart.async_unload()

    restarted_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, entry_id=entry.entry_id
    )
    after_restart = _coordinator(hass, restarted_entry, [_trv_with_valve()])
    await after_restart.async_setup()

    assert (
        after_restart.mpc.valve_curve_learners["climate.heizung_wohnzimmer"].bins()
        == bins
    )
    after_restart.async_unload()


async def test_recalibrate_resets_the_valve_curves_and_keeps_the_factors(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    coordinator, entry = await _learn_one_valve_sample(hass, freezer)
    ua_factor = coordinator.mpc.learned_ua_factor

    await coordinator.async_recalibrate(None, None, reset_valve_curves=True)

    learner = coordinator.mpc.valve_curve_learners["climate.heizung_wohnzimmer"]
    assert learner.bins() == []
    assert (
        hass_storage[_valve_curve_storage_key(entry)]["data"]["trvs"][
            "climate.heizung_wohnzimmer"
        ]["samples"]
        == []
    )
    assert coordinator.mpc.learned_ua_factor == ua_factor
    # The delayed write planned for the sample does not bring it back.
    await _poll(hass, freezer, 11)
    assert (
        hass_storage[_valve_curve_storage_key(entry)]["data"]["trvs"][
            "climate.heizung_wohnzimmer"
        ]["samples"]
        == []
    )
    coordinator.async_unload()


async def test_a_pending_valve_curve_write_is_flushed_on_shutdown(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    coordinator, entry = await _learn_one_valve_sample(hass, freezer)
    assert _valve_curve_storage_key(entry) not in hass_storage

    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()

    assert hass_storage[_valve_curve_storage_key(entry)]["data"]["trvs"][
        "climate.heizung_wohnzimmer"
    ]["samples"] == [[2.0, 60.0]]
    coordinator.async_unload()

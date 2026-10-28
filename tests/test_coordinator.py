import dataclasses
import time
from datetime import date, datetime, timedelta

import pytest
from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.sun import get_astral_event_date
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from heating_controller.const import (
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_STATIONARY_RANGE_C,
    DOMAIN,
    FlowSupplyStatus,
    HeatMode,
)
from heating_controller.coordinator import (
    ADAPTATION_MAX_HOLD_S,
    ROOM_SENSOR_STARTUP_GRACE_S,
    HeatingRoomCoordinator,
)
from heating_controller.profiles import AdaptationBusy, TrvBinding

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
    "comfort_condition_entities": ["input_boolean.comfort_release"],
    "outdoor_temperature_entity": "sensor.outdoor_temperature",
    "heat_source_climate_entity": "climate.heat_source",
    "flow_temperature_entity": "sensor.flow_temperature",
    "pv_boost_entity": "binary_sensor.pv_boost",
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
    comfort_release=True,
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
    hass.states.async_set(
        "input_boolean.comfort_release", "on" if comfort_release else "off"
    )
    hass.states.async_set("sensor.outdoor_temperature", "-3.0")
    hass.states.async_set("climate.heat_source", "heat" if heat_available else "off")
    hass.states.async_set("sensor.flow_temperature", str(flow_temp))
    hass.states.async_set("binary_sensor.pv_boost", "off")
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert coordinator.normal_heat_mode == HeatMode.COMFORT
    assert coordinator.normal_target_temperature_c == 22.0
    assert coordinator.normal_result is not None
    assert coordinator.normal_min_flow_temperature_c is not None
    assert coordinator.normal_min_flow_temperature_c > 0
    assert (
        coordinator.base_temperature_c
        == ENTRY_DATA["frost_protection_temperature_c"]
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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
    hass.states.async_set("sensor.outdoor_temperature", "19.5")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.5")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    await coordinator.async_setup()

    assert coordinator.trv_active is False
    off_calls = [call for call in switch_calls if call.service == "turn_off"]
    assert off_calls
    assert off_calls[-1].data["entity_id"] == [
        "switch.heizung_wohnzimmer_trv_active"
    ]

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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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
    assert on_calls[-1].data["entity_id"] == [
        "switch.heizung_wohnzimmer_trv_active"
    ]

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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    coordinator.async_unload()


async def test_legacy_learning_factors_imported_from_room_prefixed_text_entities(
    hass: HomeAssistant,
) -> None:
    """Legacy entities are named <room>_ua_factor / <room>_capacity_factor
    (room-prefixed), not ua_factor_<room> — this pins that exact order."""
    _seed_entities(hass)
    hass.states.async_set("text.wohnzimmer_ua_factor", "0.9982289915877139")
    hass.states.async_set("text.wohnzimmer_capacity_factor", "1.0074397084454827")

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.mpc.learned_ua_factor == 0.9982289915877139
    assert coordinator.mpc.learned_capacity_factor == 1.0074397084454827

    saved = await coordinator.store.async_load()
    assert saved.ua_factor == 0.9982289915877139
    assert saved.capacity_factor == 1.0074397084454827

    coordinator.async_unload()


async def test_room_comfort_conditions_gate_comfort_like_global_ones(
    hass: HomeAssistant,
) -> None:
    """Room-specific conditions are split out only for the card's benefit;
    for the control logic they are AND-ed with the global ones like before."""
    _seed_entities(hass)
    hass.states.async_set("switch.arbeitszimmer_aktiv", "off")

    data = {
        **ENTRY_DATA,
        "room_comfort_condition_entities": ["switch.arbeitszimmer_aktiv"],
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.state.is_comfort() is False

    hass.states.async_set("switch.arbeitszimmer_aktiv", "on")
    await hass.async_block_till_done()
    assert coordinator.state.is_comfort() is True

    hass.states.async_set("switch.arbeitszimmer_aktiv", "off")
    await hass.async_block_till_done()
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
    before_restart = HeatingRoomCoordinator(hass, closing_entry, _trvs())
    await before_restart.async_setup()
    assert before_restart.normal_flow_gate_closed is True
    before_restart.async_unload()

    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.4")

    # Same entry (same store), now with the default close hold time, so the
    # gate cannot simply close again on the first cycle.
    restarted_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, entry_id=closing_entry.entry_id
    )
    after_restart = HeatingRoomCoordinator(hass, restarted_entry, _trvs())
    await after_restart.async_setup()

    assert after_restart.normal_flow_gate_closed is True
    assert after_restart.flow_supply_status == FlowSupplyStatus.SURPLUS
    after_restart.async_unload()

    fresh_entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    fresh_entry.add_to_hass(hass)
    without_stored_state = HeatingRoomCoordinator(hass, fresh_entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.flow_threshold_c == DEFAULT_FLOW_THRESHOLD_C

    coordinator.async_unload()


async def _setup_coordinator(
    hass: HomeAssistant, trvs: list[TrvBinding] | None = None, **overrides
) -> HeatingRoomCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, **overrides})
    entry.add_to_hass(hass)
    coordinator = HeatingRoomCoordinator(hass, entry, trvs or _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()
    return coordinator


async def test_learning_window_follows_the_linked_entity(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    hass.states.async_set("input_boolean.nachtmodus", "off")
    coordinator = await _setup_coordinator(
        hass, learning_window_entity="input_boolean.nachtmodus"
    )
    learner = coordinator.mpc._learner

    assert learner._learning_window_active is False

    hass.states.async_set("input_boolean.nachtmodus", "on")
    await hass.async_block_till_done()
    assert learner._learning_window_active is True

    hass.states.async_set("input_boolean.nachtmodus", "unavailable")
    await hass.async_block_till_done()
    assert learner._learning_window_active is False

    coordinator.async_unload()


async def test_learning_window_stays_open_without_a_linked_entity(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    assert coordinator.mpc._learner._learning_window_active is True

    coordinator.async_unload()


async def test_learning_pause_follows_the_linked_entity(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    hass.states.async_set("binary_sensor.learning_enabled", "off")
    coordinator = await _setup_coordinator(
        hass, learning_enabled_entity="binary_sensor.learning_enabled"
    )
    learner = coordinator.mpc._learner

    assert learner._learning_paused is True

    hass.states.async_set("binary_sensor.learning_enabled", "on")
    await hass.async_block_till_done()
    assert learner._learning_paused is False
    assert learner._learning_resumed_ts > 0

    hass.states.async_set("binary_sensor.learning_enabled", "unavailable")
    await hass.async_block_till_done()
    assert learner._learning_paused is True

    coordinator.async_unload()


async def test_learning_is_not_paused_without_a_linked_entity(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    assert coordinator.mpc._learner._learning_paused is False

    coordinator.async_unload()


async def test_stationary_range_comes_from_the_entry(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass, stationary_range_c=0.5)

    assert coordinator.mpc._learner._stationary_range_c == 0.5

    coordinator.async_unload()


async def test_stationary_range_defaults_without_a_setting(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    assert coordinator.mpc._learner._stationary_range_c == DEFAULT_STATIONARY_RANGE_C

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
    capacity_until = learner._capacity_suppression.suppressed_until_ts - before_close
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
    next_sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, day + timedelta(days=1))

    def met(moment: datetime) -> bool:
        return coordinator.is_sun_condition_met(moment.timestamp())

    assert met(sunset - timedelta(hours=3)) is False
    assert met(sunset + timedelta(hours=1)) is False
    assert met(sunset + timedelta(hours=2, minutes=1)) is True
    assert met(next_sunrise - timedelta(minutes=1)) is True
    assert met(next_sunrise + timedelta(minutes=1)) is False

    coordinator.async_unload()


async def test_flow_temperature_comes_from_the_flow_entity(hass: HomeAssistant) -> None:
    _seed_entities(hass, flow_temp=42.0)
    hass.states.async_set("climate.heat_source", "heat", {"current_temperature": 20.0})
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    coordinator.async_unload()


async def test_missing_flow_temperature_entity_stops_the_room(
    hass: HomeAssistant,
) -> None:
    data = {k: v for k, v in ENTRY_DATA.items() if k != "flow_temperature_entity"}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError, match="flow_temperature_entity"):
        HeatingRoomCoordinator(hass, entry, _trvs())


async def test_opening_a_window_closes_the_valve_without_smoothing(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.wohnzimmer_temperatur", "17.0")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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

    coordinator.async_unload()


async def test_without_valve_position_the_demand_stands_in(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator = await _setup_coordinator(hass)

    result = coordinator.last_result
    assert result.input.valve_positions_pct == [None]
    assert result.input.valve_position_missing is False
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
    coordinator = HeatingRoomCoordinator(hass, entry, _trvs())
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
    calls = _register_fake_climate_set_temperature(hass)
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
    await coordinator._async_apply_result(shut)
    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == opened

    # Forced frost protection (open window) shuts it at once.
    await coordinator._async_apply_result(shut, forced=True)
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
        {"current_temperature": 18.0, "temperature": coordinator.last_result.trv_targets[0]},
    )
    await hass.async_block_till_done()
    opened = coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"]

    now[0] += 16 * 60
    await coordinator._async_apply_result(shut)

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == opened + 1

    coordinator.async_unload()


ADAPTATION_ENTITY = "sensor.heizung_wohnzimmer_adaptation_run_status"


def _trvs_with_adaptation() -> list[TrvBinding]:
    return _trvs(
        adaptation_status_entity=ADAPTATION_ENTITY,
        adaptation_busy=AdaptationBusy(states=frozenset({"in_progress"})),
    )


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
    await coordinator._async_apply_result(coordinator.last_result)

    assert coordinator.trv_setpoint_writes["climate.heizung_wohnzimmer"] == 1

    coordinator.async_unload()


async def test_renaming_a_trv_entity_reloads_the_room(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_entities(hass)
    reloads: list[str] = []
    monkeypatch.setattr(
        hass.config_entries, "async_schedule_reload", reloads.append
    )
    coordinator = await _setup_coordinator(hass)

    er.async_get(hass).async_update_entity(
        "switch.heizung_wohnzimmer_trv_active",
        new_entity_id="switch.heizung_wohnzimmer_heat_available",
    )
    await hass.async_block_till_done()

    assert reloads == [coordinator.entry.entry_id]

    coordinator.async_unload()

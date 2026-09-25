import time
from datetime import date, datetime, timedelta

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant, ServiceCall
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
    ROOM_SENSOR_STARTUP_GRACE_S,
    HeatingRoomCoordinator,
)

ENTRY_DATA = {
    "room_name": "Wohnzimmer",
    "trvs": [
        {
            "entity_id": "climate.heizung_wohnzimmer",
            "trv_active_switch": "switch.heizung_wohnzimmer_trv_active",
            "emitter_type": "panel",
            "min_target_temperature_c": 5.0,
            "max_target_temperature_c": 28.0,
            "target_temperature_step_c": 0.5,
            "radiator_type": "22",
            "width_mm": 1000.0,
            "height_mm": 600.0,
        }
    ],
    "room_sensor_entity": "sensor.wohnzimmer_temperatur",
    "window_contact_entities": ["binary_sensor.fenster_wohnzimmer"],
    "comfort_condition_entities": ["input_boolean.comfort_release"],
    "outdoor_temperature_entity": "sensor.outdoor_temperature",
    "heat_source_climate_entity": "climate.heat_source",
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
    "mpc_demand_hysteresis_pct": 5.0,
    "mpc_hold_time_s": 300.0,
    "mpc_hold_override_demand_pct": 40.0,
    "mpc_max_demand_step_pct": 20.0,
    "max_sensor_age_s": 1800.0,
    "flow_threshold_entity": "input_number.flow_threshold",
}


def _seed_entities(
    hass: HomeAssistant,
    *,
    comfort_release=True,
    heat_available=True,
    flow_temp=42.0,
) -> None:
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 18.0}
    )
    hass.states.async_set("sensor.wohnzimmer_temperatur", "18.5")
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "off")
    hass.states.async_set(
        "input_boolean.comfort_release", "on" if comfort_release else "off"
    )
    hass.states.async_set("sensor.outdoor_temperature", "-3.0")
    hass.states.async_set(
        "climate.heat_source",
        "heat" if heat_available else "off",
        {"current_temperature": flow_temp},
    )
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.trv_active is False
    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION

    coordinator.async_unload()


async def test_heat_source_heat_mode_and_hot_flow_is_active(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass, flow_temp=42.0)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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
    before_restart = HeatingRoomCoordinator(hass, closing_entry)
    await before_restart.async_setup()
    assert before_restart.normal_flow_gate_closed is True
    before_restart.async_unload()

    hass.states.async_set("sensor.wohnzimmer_temperatur", "22.4")

    # Same entry (same store), now with the default close hold time, so the
    # gate cannot simply close again on the first cycle.
    restarted_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, entry_id=closing_entry.entry_id
    )
    after_restart = HeatingRoomCoordinator(hass, restarted_entry)
    await after_restart.async_setup()

    assert after_restart.normal_flow_gate_closed is True
    assert after_restart.flow_supply_status == FlowSupplyStatus.SURPLUS
    after_restart.async_unload()

    fresh_entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    fresh_entry.add_to_hass(hass)
    without_stored_state = HeatingRoomCoordinator(hass, fresh_entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
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

    coordinator = HeatingRoomCoordinator(hass, entry)
    _register_fake_climate_set_temperature(hass)
    await coordinator.async_setup()

    assert coordinator.flow_threshold_c == DEFAULT_FLOW_THRESHOLD_C

    coordinator.async_unload()


async def _setup_coordinator(
    hass: HomeAssistant, **overrides
) -> HeatingRoomCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, **overrides})
    entry.add_to_hass(hass)
    coordinator = HeatingRoomCoordinator(hass, entry)
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
    before_close = time.time()
    hass.states.async_set("binary_sensor.fenster_wohnzimmer", "off")
    await hass.async_block_till_done()

    ua_until = learner._ua_suppression.suppressed_until_ts - before_close
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

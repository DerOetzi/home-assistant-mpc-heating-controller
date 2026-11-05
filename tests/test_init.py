import pytest
import voluptuous as vol
from heating_controller.const import DOMAIN, HeatMode
from heating_controller.controller.mpc.types import LearningFactors
from heating_controller.store import LearningFactorsStore
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from test_coordinator import (
    ENTRY_DATA,
    TRV_EMITTER,
    _hub_entry,
    _room_data,
    _seed_entities,
    register_trv_device,
)


async def test_setup_entry_registers_service_and_entities(hass: HomeAssistant) -> None:
    _seed_entities(hass)

    async def climate_handler(call: ServiceCall) -> None:
        pass

    async def switch_handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "unblock")
    assert hass.states.get("select.wohnzimmer_heating_mode") is not None
    assert hass.states.get("button.wohnzimmer_unblock") is not None

    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_set_manual_heat_mode(HeatMode.ECO)
    assert coordinator.is_automation_active is False

    await hass.services.async_call(
        DOMAIN,
        "unblock",
        {"entity_id": "button.wohnzimmer_unblock"},
        blocking=True,
    )
    assert coordinator.is_automation_active is True

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_remove_entry_deletes_learning_factors_store(hass: HomeAssistant) -> None:
    _seed_entities(hass)

    async def climate_handler(call: ServiceCall) -> None:
        pass

    async def switch_handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    from heating_controller.controller.mpc.types import LearningFactors

    await coordinator.store.async_save(
        LearningFactors(ua_factor=1.1, capacity_factor=1.2)
    )
    assert await coordinator.store.async_load() is not None

    assert await hass.config_entries.async_remove(entry.entry_id)

    from heating_controller.store import LearningFactorsStore

    leftover_store = LearningFactorsStore(hass, ENTRY_DATA["room_name"], entry.entry_id)
    assert await leftover_store.async_load() is None

    from heating_controller.store import FlowGateStateStore

    leftover_gate_store = FlowGateStateStore(
        hass, ENTRY_DATA["room_name"], entry.entry_id
    )
    assert await leftover_gate_store.async_load() is None


async def _setup_room(hass: HomeAssistant) -> MockConfigEntry:
    _seed_entities(hass)

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)

    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_recalibrate_sets_both_factors_and_persists_them(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup_room(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await hass.services.async_call(
        DOMAIN,
        "recalibrate",
        {
            "entity_id": "button.wohnzimmer_unblock",
            "ua_factor": 1.2,
            "capacity_factor": 0.8,
        },
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )

    assert coordinator.mpc.learned_ua_factor == 1.2
    assert coordinator.mpc.learned_capacity_factor == 0.8
    persisted = await coordinator.store.async_load()
    assert (persisted.ua_factor, persisted.capacity_factor) == (1.2, 0.8)

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_keeps_a_factor_that_is_left_out(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup_room(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.mpc.recalibrate_learning_factors(
        LearningFactors(ua_factor=0.9, capacity_factor=1.3)
    )

    await hass.services.async_call(
        DOMAIN,
        "recalibrate",
        {"entity_id": "button.wohnzimmer_unblock", "ua_factor": 1.0},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )

    assert coordinator.mpc.learned_ua_factor == 1.0
    assert coordinator.mpc.learned_capacity_factor == 1.3

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_resets_gains_and_keeps_the_factors(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup_room(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.mpc.recalibrate_learning_factors(
        LearningFactors(ua_factor=0.9, capacity_factor=1.3)
    )
    gains = coordinator.mpc.gain_model
    gains.add_night_point(14.0, 230.0, 6.0, 20.0)

    await hass.services.async_call(
        DOMAIN,
        "recalibrate",
        {"entity_id": "button.wohnzimmer_unblock", "reset_gains": True},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )

    assert gains.night_points == 0
    assert coordinator.mpc.learned_ua_factor == 0.9
    assert coordinator.mpc.learned_capacity_factor == 1.3
    persisted = await coordinator.gain_store.async_load()
    assert persisted["night"] is None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_accepts_a_valve_curve_reset_alone(
    hass: HomeAssistant, hass_admin_user: MockUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await _setup_room(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    resets: list[bool] = []
    monkeypatch.setattr(
        coordinator.mpc, "reset_valve_curves", lambda: resets.append(True)
    )

    await hass.services.async_call(
        DOMAIN,
        "recalibrate",
        {"entity_id": "button.wohnzimmer_unblock", "reset_valve_curves": True},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )

    assert resets == [True]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_rejects_disturbance(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup_room(hass)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "recalibrate",
            {
                "entity_id": "button.wohnzimmer_unblock",
                "ua_factor": 1.0,
                "disturbance_w": 0,
            },
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_drops_the_old_observer_store(hass: HomeAssistant) -> None:
    entry_id = "old_observer"
    old_store = Store(
        hass, 1, f"{DOMAIN}_{slugify(ENTRY_DATA['room_name'])}_{entry_id}_observer"
    )
    await old_store.async_save({"room_name": "x", "disturbance_w": -40.0})

    _seed_entities(hass)

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)
    entry = _hub_entry(_room_data(hass), entry_id=entry_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await old_store.async_load() is None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_needs_at_least_one_factor(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup_room(hass)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "recalibrate",
            {"entity_id": "button.wohnzimmer_unblock"},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_rejects_a_factor_out_of_range(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup_room(hass)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "recalibrate",
            {"entity_id": "button.wohnzimmer_unblock", "ua_factor": 2.5},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_recalibrate_is_admin_only(
    hass: HomeAssistant, hass_read_only_user: MockUser
) -> None:
    entry = await _setup_room(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "recalibrate",
            {"entity_id": "button.wohnzimmer_unblock", "ua_factor": 1.5},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    assert coordinator.mpc.learned_ua_factor == 1.0

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_flushes_the_valve_curve_and_removal_leaves_no_file(
    hass: HomeAssistant, freezer, hass_storage
) -> None:
    from test_coordinator import VALVE_ENTITY, _poll

    device_id = register_trv_device(
        hass,
        properties=(
            ("climate", "climate", ""),
            ("sensor", "pi_heating_demand", "_pi_heating_demand"),
            ("switch", "switch_heat_available", "_trv_active"),
        ),
    )
    _seed_entities(hass)
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": 18.0},
    )
    hass.states.async_set(VALVE_ENTITY, "0")

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)
    entry = _hub_entry(
        {**ENTRY_DATA, "trvs": [{"device_id": device_id, **TRV_EMITTER}]}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # One sample finishes; its delayed write is still pending.
    hass.states.async_set(
        "climate.heizung_wohnzimmer",
        "heat",
        {"current_temperature": 18.0, "temperature": 20.0},
    )
    hass.states.async_set(VALVE_ENTITY, "60")
    await hass.async_block_till_done()
    await _poll(hass, freezer, 5)
    await _poll(hass, freezer, 5.5)
    await _poll(hass, freezer, 5)
    key = f"{DOMAIN}_wohnzimmer_{entry.entry_id}_valve_curve"
    assert key not in hass_storage

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["trvs"]["climate.heizung_wohnzimmer"][
        "samples"
    ] == [[2.0, 60.0]]

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage

    # No delayed write is left to bring the file back.
    await _poll(hass, freezer, 11)
    assert key not in hass_storage


async def test_removing_a_room_drops_its_stores_and_keeps_the_hub(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_room(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.store.async_save(
        LearningFactors(ua_factor=0.8, capacity_factor=1.0)
    )

    hass.config_entries.async_remove_subentry(entry, entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
    assert hass.states.get("select.wohnzimmer_heating_mode") is None
    leftover = LearningFactorsStore(hass, ENTRY_DATA["room_name"], entry.entry_id)
    assert await leftover.async_load() is None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_room_whose_trv_is_gone_stays_out(hass: HomeAssistant) -> None:
    _register_noop_services(hass)
    _seed_entities(hass)
    entry = _hub_entry({**ENTRY_DATA, "trvs": [{"device_id": "gone", **TRV_EMITTER}]})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id not in hass.data[DOMAIN]

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hub_reports_the_lead_rooms(hass: HomeAssistant) -> None:
    _register_noop_services(hass)
    _seed_entities(hass)
    hass.states.async_set("sensor.flow_curve", "36.0")
    entry = _hub_entry(
        {
            **_room_data(hass),
            "lead_heating_limit": True,
            "lead_flow": True,
            "flow_curve_entity": "sensor.flow_curve",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    hold_c = coordinator.normal_gated_hold_flow_temperature_c
    min_c = coordinator.normal_min_flow_temperature_c
    assert hold_c is not None and min_c is not None

    hold = hass.states.get("sensor.heizung_lead_rooms_hold_flow_temperature")
    assert float(hold.state) == round(hold_c, 1)
    assert hold.attributes["room"] == "Wohnzimmer"
    assert hold.attributes["rooms"] == ["Wohnzimmer"]
    minimum = hass.states.get("sensor.heizung_lead_rooms_minimum_flow_temperature")
    assert float(minimum.state) == round(min_c, 1)

    demand = hass.states.get("binary_sensor.heizung_lead_rooms_heating_demand")
    assert demand.state == ("on" if round(hold_c, 1) >= 30.0 else "off")

    reserve = hass.states.get("sensor.heizung_flow_reserve_hold")
    if round(hold_c, 1) > 0:
        assert float(reserve.state) == round(36.0 - round(hold_c, 1), 1)

    hass.states.async_set("input_number.flow_threshold", "unavailable")
    await hass.async_block_till_done()
    demand = hass.states.get("binary_sensor.heizung_lead_rooms_heating_demand")
    assert demand.state == "unavailable"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hub_without_lead_rooms_reports_nothing(hass: HomeAssistant) -> None:
    entry = await _setup_room(hass)

    hold = hass.states.get("sensor.heizung_lead_rooms_hold_flow_temperature")
    assert hold.state == "unknown"
    assert hold.attributes["rooms"] == []
    demand = hass.states.get("binary_sensor.heizung_lead_rooms_heating_demand")
    assert demand.state == "unavailable"
    assert hass.states.get("sensor.heizung_flow_reserve_hold") is None

    assert await hass.config_entries.async_unload(entry.entry_id)


def _register_noop_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)

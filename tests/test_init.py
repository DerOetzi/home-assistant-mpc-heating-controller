import pytest
import voluptuous as vol
from heating_controller.const import (
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_TARGET_BAND_C,
    DOMAIN,
    HeatMode,
)
from heating_controller.controller.mpc.types import LearningFactors
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from test_coordinator import (
    ENTRY_DATA,
    TRV_EMITTER,
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

    entry = MockConfigEntry(domain=DOMAIN, data=_room_data(hass))
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

    entry = MockConfigEntry(domain=DOMAIN, data=_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    from heating_controller.controller.mpc.types import LearningFactors

    await coordinator.store.async_save(LearningFactors(ua_factor=1.1, capacity_factor=1.2))
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


async def test_migration_drops_the_numeric_flow_threshold(hass: HomeAssistant) -> None:
    _seed_entities(hass)

    async def climate_handler(call: ServiceCall) -> None:
        pass

    async def switch_handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    legacy_data = {
        key: value
        for key, value in _room_data(hass).items()
        if key != "flow_threshold_entity"
    }
    legacy_data["flow_threshold_c"] = 30.0
    entry = MockConfigEntry(domain=DOMAIN, data=legacy_data, version=1)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 7
    assert "flow_threshold_c" not in entry.data

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.flow_threshold_c == DEFAULT_FLOW_THRESHOLD_C

    assert await hass.config_entries.async_unload(entry.entry_id)


async def _setup_room(hass: HomeAssistant) -> MockConfigEntry:
    _seed_entities(hass)

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)

    entry = MockConfigEntry(domain=DOMAIN, data=_room_data(hass))
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
    entry = MockConfigEntry(domain=DOMAIN, data=_room_data(hass), entry_id=entry_id)
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


_ADDED_IN_V3 = ("room_area_m2", "construction", "flow_temperature_entity")


def _register_noop_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)


async def test_migration_to_v3_invents_no_values(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    data = {key: value for key, value in _room_data(hass).items() if key not in _ADDED_IN_V3}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=2)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 7
    assert all(key not in entry.data for key in _ADDED_IN_V3)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_migration_to_v6_switches_to_relative_targets(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    data = {key: value for key, value in _room_data(hass).items() if key != "trv_setpoint_mode"}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=5)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 7
    assert entry.data["trv_setpoint_mode"] == "relative"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_to_v7_binds_a_trv_by_its_device(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    device_id = register_trv_device(
        hass,
        properties=(
            ("climate", "climate", ""),
            ("switch", "switch_heat_available", "_trv_active"),
            ("sensor", "pi_heating_demand", "_pi_heating_demand"),
        ),
    )
    data = {
        **ENTRY_DATA,
        "trvs": [
            {
                "entity_id": "climate.heizung_wohnzimmer",
                "trv_active_switch": "switch.heizung_wohnzimmer_trv_active",
                "valve_position_entity": "sensor.heizung_wohnzimmer_pi_heating_demand",
                "min_target_temperature_c": 5.0,
                "max_target_temperature_c": 28.0,
                "target_temperature_step_c": 0.5,
                **TRV_EMITTER,
            }
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=6)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 7
    assert entry.data["trvs"] == [{"device_id": device_id, **TRV_EMITTER}]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.trv_entity_ids == ["climate.heizung_wohnzimmer"]
    assert coordinator._valve_position_entities == [
        "sensor.heizung_wohnzimmer_pi_heating_demand"
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_to_v7_stops_at_a_trv_without_profile(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    register_trv_device(hass, model_id="unknown-model")
    data = {
        **ENTRY_DATA,
        "trvs": [{"entity_id": "climate.heizung_wohnzimmer", **TRV_EMITTER}],
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=6)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 6


async def test_migration_to_v4_sets_the_default_target_band(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    data = {key: value for key, value in _room_data(hass).items() if key != "target_band_c"}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=3)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 7
    assert entry.data["target_band_c"] == DEFAULT_TARGET_BAND_C

    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("missing", _ADDED_IN_V3)
async def test_setup_fails_without_a_required_value(
    hass: HomeAssistant, missing: str
) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    data = {key: value for key, value in _room_data(hass).items() if key != missing}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=3)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert missing in entry.reason


async def test_options_flow_loads_a_room_whose_setup_failed(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _register_noop_services(hass)
    data = {key: value for key, value in _room_data(hass).items() if key not in _ADDED_IN_V3}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=3)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"flow_temperature_entity": "sensor.flow_temperature"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"room_area_m2": 20.0, "construction": "medium"}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id].mpc.effective_capacity_j_per_k == (
        20.0 * 165 * 1000
    )

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
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, "trvs": [{"device_id": device_id, **TRV_EMITTER}]},
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

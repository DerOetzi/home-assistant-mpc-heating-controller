import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from heating_controller.const import DEFAULT_FLOW_THRESHOLD_C, DOMAIN, HeatMode
from heating_controller.controller.mpc.types import LearningFactors

from test_coordinator import ENTRY_DATA, _seed_entities


async def test_setup_entry_registers_service_and_entities(hass: HomeAssistant) -> None:
    _seed_entities(hass)

    async def climate_handler(call: ServiceCall) -> None:
        pass

    async def switch_handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", climate_handler)
    hass.services.async_register("switch", "turn_on", switch_handler)
    hass.services.async_register("switch", "turn_off", switch_handler)

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
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

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
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
        for key, value in ENTRY_DATA.items()
        if key != "flow_threshold_entity"
    }
    legacy_data["flow_threshold_c"] = 30.0
    entry = MockConfigEntry(domain=DOMAIN, data=legacy_data, version=1)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
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

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
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

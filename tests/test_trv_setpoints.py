"""What the room hands its TRVs: setpoint, room temperature, shared load."""

import time
from datetime import timedelta

import pytest
from heating_controller.const import DOMAIN, HeatMode
from heating_controller.coordinator import HeatingRoomCoordinator
from heating_controller.profiles import StatusMatch, TrvBinding
from homeassistant.core import HomeAssistant, ServiceCall, State
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_coordinator import (
    ADAPTATION_ENTITY,
    ENTRY_DATA,
    TRV_EMITTER,
    _coordinator,
    _seed_entities,
    _trvs,
)

CLIMATE = "climate.heizung_wohnzimmer"
HEAT_SWITCH = "switch.heizung_wohnzimmer_trv_active"
ROOM_SENSOR = "sensor.wohnzimmer_temperatur"
WINDOW = "binary_sensor.fenster_wohnzimmer"


def _record_calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Climate and switch calls in the order they were made."""
    calls: list[ServiceCall] = []

    async def handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)
    hass.services.async_register("number", "set_value", handler)
    return calls


async def _setup(
    hass: HomeAssistant, **trv_overrides
) -> tuple[HeatingRoomCoordinator, list[ServiceCall]]:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs(**trv_overrides))
    calls = _record_calls(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator, calls


def _setpoints(calls: list[ServiceCall]) -> list[float]:
    return [call.data["temperature"] for call in calls if call.domain == "climate"]


def _other_switch_calls(calls: list[ServiceCall]) -> list[ServiceCall]:
    """Switch calls to anything but the heat-available switch."""
    return [
        call
        for call in calls
        if call.domain == "switch" and call.data["entity_id"] != [HEAT_SWITCH]
    ]


@pytest.mark.parametrize(
    ("attributes", "target", "expected"),
    [
        pytest.param({"target_temp_step": 0.5}, 21.3, 21.5, id="step"),
        pytest.param({}, 21.3, 21.5, id="default_step"),
        pytest.param({"target_temp_step": 1.0}, 21.4, 21.0, id="whole_degrees"),
        pytest.param(
            {"target_temp_step": 0.5, "min_temp": 5.0, "max_temp": 20.0},
            22.0,
            20.0,
            id="max",
        ),
        pytest.param(
            {"target_temp_step": 0.5, "min_temp": 7.0, "max_temp": 35.0},
            5.0,
            7.0,
            id="min",
        ),
    ],
)
def test_the_setpoint_follows_the_climate_attributes(
    attributes: dict, target: float, expected: float
) -> None:
    state = State(CLIMATE, "heat", attributes)
    assert HeatingRoomCoordinator._trv_setpoint_c(state, target) == expected


async def test_comfort_hands_the_trv_its_target(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    coordinator, calls = await _setup(hass)

    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert _setpoints(calls) == [22.0]
    assert calls[0].data["entity_id"] == CLIMATE

    coordinator.async_unload()


async def test_the_setpoint_goes_out_without_an_outdoor_temperature(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    coordinator, calls = await _setup(hass)

    assert _setpoints(calls) == [22.0]
    assert coordinator.mpc.outdoor_temperature_missing is True

    coordinator.async_unload()


async def test_an_open_window_drops_the_setpoint_to_frost_protection(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    coordinator, calls = await _setup(hass)
    calls.clear()

    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()

    assert coordinator.current_heat_mode == HeatMode.FROST_PROTECTION
    assert _setpoints(calls) == [8.0]

    calls.clear()
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()

    # Back at once, no lock.
    assert coordinator.current_heat_mode == HeatMode.COMFORT
    assert _setpoints(calls) == [22.0]
    # The TRV's own open-window flag is left to automations.
    assert _other_switch_calls(calls) == []

    coordinator.async_unload()


async def test_an_adapting_trv_gets_frost_protection_after_the_run(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(ADAPTATION_ENTITY, "in_progress")
    coordinator, calls = await _setup(
        hass,
        adaptation_status_entity=ADAPTATION_ENTITY,
        adaptation_busy=StatusMatch(states=frozenset({"in_progress"})),
    )
    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()
    assert _setpoints(calls) == []

    hass.states.async_set(ADAPTATION_ENTITY, "found")
    await hass.async_block_till_done()

    assert _setpoints(calls) == [8.0]

    coordinator.async_unload()


EXTERNAL = "number.heizung_wohnzimmer_external_measured_room_sensor"
COVERED = "switch.heizung_wohnzimmer_radiator_covered"
EXTERNAL_TRV = {
    "external_temperature_entity": EXTERNAL,
    "external_scale": 100.0,
    "external_off_value": -8000.0,
    "radiator_covered_entity": COVERED,
}


def _numbers(calls: list[ServiceCall], entity_id: str) -> list[float]:
    return [
        call.data["value"]
        for call in calls
        if call.domain == "number" and call.data["entity_id"] == entity_id
    ]


async def _report(
    hass: HomeAssistant,
    coordinator: HeatingRoomCoordinator,
    freezer,
    minutes: int,
    room_c: float,
) -> None:
    """Room sensor reports room_c every 5 min for the given minutes."""
    for _ in range(minutes // 5):
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(ROOM_SENSOR, str(room_c), force_update=True)
        await hass.async_block_till_done()
        await coordinator._async_recompute()


async def _setup_external(
    hass: HomeAssistant, covered: str = "off"
) -> tuple[HeatingRoomCoordinator, list[ServiceCall]]:
    _seed_entities(hass)
    hass.states.async_set(EXTERNAL, "-8000")
    hass.states.async_set(COVERED, covered)
    return await _setup(hass, **EXTERNAL_TRV)


async def test_the_room_sensor_goes_to_an_exposed_trv_rarely(
    hass: HomeAssistant, freezer
) -> None:
    coordinator, calls = await _setup_external(hass)
    assert _numbers(calls, EXTERNAL) == [1850.0]

    # A change waits for the shortest spacing.
    await _report(hass, coordinator, freezer, 25, 18.7)
    assert _numbers(calls, EXTERNAL) == [1850.0]
    await _report(hass, coordinator, freezer, 5, 18.7)
    assert _numbers(calls, EXTERNAL) == [1850.0, 1870.0]

    # Without a change only after the longest one.
    await _report(hass, coordinator, freezer, 145, 18.7)
    assert _numbers(calls, EXTERNAL) == [1850.0, 1870.0]
    await _report(hass, coordinator, freezer, 5, 18.7)
    assert _numbers(calls, EXTERNAL) == [1850.0, 1870.0, 1870.0]
    assert coordinator.trv_external_writes == {EXTERNAL: 3}

    coordinator.async_unload()


async def test_a_covered_radiator_gets_the_room_sensor_often(
    hass: HomeAssistant, freezer
) -> None:
    coordinator, calls = await _setup_external(hass, covered="on")

    await _report(hass, coordinator, freezer, 5, 18.6)
    assert _numbers(calls, EXTERNAL) == [1850.0, 1860.0]
    await _report(hass, coordinator, freezer, 20, 18.6)
    assert _numbers(calls, EXTERNAL) == [1850.0, 1860.0]
    await _report(hass, coordinator, freezer, 5, 18.6)
    assert _numbers(calls, EXTERNAL) == [1850.0, 1860.0, 1860.0]

    coordinator.async_unload()


async def test_covering_a_radiator_sends_at_once(hass: HomeAssistant) -> None:
    coordinator, calls = await _setup_external(hass)

    hass.states.async_set(COVERED, "on")
    await hass.async_block_till_done()

    assert _numbers(calls, EXTERNAL) == [1850.0, 1850.0]

    coordinator.async_unload()


async def test_an_unknown_room_sensor_sends_nothing(
    hass: HomeAssistant, freezer
) -> None:
    coordinator, calls = await _setup_external(hass)
    for _ in range(36):
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(ROOM_SENSOR, "unavailable", force_update=True)
        await hass.async_block_till_done()
        await coordinator._async_recompute()

    assert _numbers(calls, EXTERNAL) == [1850.0]

    coordinator.async_unload()


async def test_without_a_room_sensor_the_trv_gets_its_own_back_once(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(EXTERNAL, "1850")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={k: v for k, v in ENTRY_DATA.items() if k != "room_sensor_entity"},
    )
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs(**EXTERNAL_TRV))
    calls = _record_calls(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    await coordinator._async_recompute()

    assert _numbers(calls, EXTERNAL) == [-8000.0]

    coordinator.async_unload()


def _sharing_trv(name: str) -> TrvBinding:
    return TrvBinding(
        climate_entity=f"climate.heizung_{name}",
        emitter=TRV_EMITTER,
        load_balancing_entity=f"switch.heizung_{name}_load_balancing_enable",
        load_estimate_entity=f"sensor.heizung_{name}_load_estimate",
        load_room_mean_entity=f"number.heizung_{name}_load_room_mean",
    )


async def _setup_sharing(
    hass: HomeAssistant, trvs: list[TrvBinding]
) -> tuple[HeatingRoomCoordinator, list[ServiceCall]]:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, trvs)
    calls = _record_calls(hass)
    await coordinator.async_setup()
    await coordinator.async_start()
    await hass.async_block_till_done()
    return coordinator, calls


def _seed_sharing(hass: HomeAssistant, name: str, load: str) -> None:
    hass.states.async_set(
        f"climate.heizung_{name}", "heat", {"current_temperature": 18.0}
    )
    hass.states.async_set(f"switch.heizung_{name}_load_balancing_enable", "off")
    hass.states.async_set(f"sensor.heizung_{name}_load_estimate", load)
    hass.states.async_set(f"number.heizung_{name}_load_room_mean", "-8000")


async def test_two_trvs_share_the_room_load(hass: HomeAssistant, freezer) -> None:
    _seed_entities(hass)
    _seed_sharing(hass, "gross", "438")
    _seed_sharing(hass, "klein", "287")
    coordinator, calls = await _setup_sharing(
        hass, [_sharing_trv("gross"), _sharing_trv("klein")]
    )

    switched_on = [
        call.data["entity_id"]
        for call in calls
        if call.domain == "switch" and call.service == "turn_on"
    ]
    assert "switch.heizung_gross_load_balancing_enable" in switched_on
    assert "switch.heizung_klein_load_balancing_enable" in switched_on
    assert _numbers(calls, "number.heizung_gross_load_room_mean") == [362]
    assert _numbers(calls, "number.heizung_klein_load_room_mean") == [362]
    assert coordinator.load_room_mean == 362

    # An unknown load leaves the other alone in the mean.
    hass.states.async_set("sensor.heizung_klein_load_estimate", "-8000")
    calls.clear()
    await coordinator._async_update_load_sharing(time.time())
    assert _numbers(calls, "number.heizung_gross_load_room_mean") == [438]

    coordinator.async_unload()


async def test_a_switch_back_from_an_outage_shares_at_once(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    _seed_sharing(hass, "gross", "438")
    _seed_sharing(hass, "klein", "287")
    hass.states.async_set("switch.heizung_klein_load_balancing_enable", "unavailable")
    coordinator, calls = await _setup_sharing(
        hass, [_sharing_trv("gross"), _sharing_trv("klein")]
    )
    hass.states.async_set("switch.heizung_gross_load_balancing_enable", "on")
    await hass.async_block_till_done()
    calls.clear()

    hass.states.async_set("switch.heizung_klein_load_balancing_enable", "off")
    await hass.async_block_till_done()

    assert [
        call.data["entity_id"]
        for call in calls
        if call.domain == "switch" and call.service == "turn_on"
    ] == ["switch.heizung_klein_load_balancing_enable"]

    coordinator.async_unload()


async def test_a_single_trv_works_alone(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    _seed_sharing(hass, "gross", "438")
    hass.states.async_set("switch.heizung_gross_load_balancing_enable", "on")
    coordinator, calls = await _setup_sharing(hass, [_sharing_trv("gross")])

    assert [
        call.data["entity_id"]
        for call in calls
        if call.domain == "switch" and call.service == "turn_off"
    ] == ["switch.heizung_gross_load_balancing_enable"]
    assert _numbers(calls, "number.heizung_gross_load_room_mean") == []
    assert coordinator.load_room_mean is None

    coordinator.async_unload()


async def test_an_adaptation_holds_the_setpoint_back_no_longer_than_the_limit(
    hass: HomeAssistant, freezer
) -> None:
    from heating_controller.coordinator import ADAPTATION_MAX_HOLD_S

    _seed_entities(hass)
    hass.states.async_set(ADAPTATION_ENTITY, "in_progress")
    coordinator, calls = await _setup(
        hass,
        adaptation_status_entity=ADAPTATION_ENTITY,
        adaptation_busy=StatusMatch(states=frozenset({"in_progress"})),
    )
    assert _setpoints(calls) == []

    freezer.tick(timedelta(seconds=ADAPTATION_MAX_HOLD_S + 1))
    await coordinator._async_recompute()

    assert _setpoints(calls) == [22.0]

    coordinator.async_unload()

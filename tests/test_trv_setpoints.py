"""What the room hands its TRVs: setpoint and correction."""

from datetime import timedelta

import pytest
from heating_controller.const import DOMAIN, HeatMode
from heating_controller.controller.setpoint_correction import CORRECTION_INTERVAL_S
from heating_controller.coordinator import HeatingRoomCoordinator
from heating_controller.profiles import StatusMatch
from homeassistant.core import HomeAssistant, ServiceCall, State
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_coordinator import (
    ADAPTATION_ENTITY,
    ENTRY_DATA,
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
    return calls


async def _setup(
    hass: HomeAssistant, **trv_overrides
) -> tuple[HeatingRoomCoordinator, list[ServiceCall]]:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, _trvs(**trv_overrides))
    calls = _record_calls(hass)
    await coordinator.async_setup()
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


async def _run_hours(
    hass: HomeAssistant,
    coordinator: HeatingRoomCoordinator,
    freezer,
    hours: float,
    room_c: float,
) -> None:
    """Room sensor reports room_c every 5 min for the given hours."""
    for _ in range(int(hours * 12)):
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(ROOM_SENSOR, str(room_c), force_update=True)
        await hass.async_block_till_done()
        await coordinator._async_recompute()


async def _setup_for_correction(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, room_c: float = 21.0
) -> tuple[HeatingRoomCoordinator, list[ServiceCall]]:
    _seed_entities(hass)
    hass.states.async_set(ROOM_SENSOR, str(room_c))
    coordinator, calls = await _setup(hass)
    # The night's heat balance holds the correction; these tests run by day.
    monkeypatch.setattr(coordinator, "is_sun_condition_met", lambda _ts: False)
    return coordinator, calls


async def test_a_cold_room_raises_the_setpoint_step_by_step(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, calls = await _setup_for_correction(hass, monkeypatch)

    await _run_hours(hass, coordinator, freezer, CORRECTION_INTERVAL_S / 3600, 21.0)
    assert coordinator.setpoint_correction.correction_c == 0.5
    assert _setpoints(calls)[-1] == 22.5
    assert await coordinator.setpoint_correction_store.async_load() == 0.5

    await _run_hours(hass, coordinator, freezer, CORRECTION_INTERVAL_S / 3600, 21.0)
    assert coordinator.setpoint_correction.correction_c == 1.0
    assert _setpoints(calls)[-1] == 23.0

    coordinator.async_unload()


async def test_a_small_room_error_leaves_the_setpoint(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, calls = await _setup_for_correction(hass, monkeypatch, 21.8)

    await _run_hours(hass, coordinator, freezer, 2.5, 21.8)

    assert coordinator.setpoint_correction.correction_c == 0.0
    assert set(_setpoints(calls)) == {22.0}

    coordinator.async_unload()


async def test_a_fully_open_valve_does_not_raise_the_setpoint(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _calls = await _setup_for_correction(hass, monkeypatch)
    monkeypatch.setattr(
        HeatingRoomCoordinator, "room_demand_pct", property(lambda _self: 100.0)
    )

    await _run_hours(hass, coordinator, freezer, 2.5, 21.0)

    assert coordinator.setpoint_correction.correction_c == 0.0

    coordinator.async_unload()


async def test_an_opened_window_holds_the_correction(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _calls = await _setup_for_correction(hass, monkeypatch)
    await _run_hours(hass, coordinator, freezer, 1.5, 21.0)
    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()
    await _run_hours(hass, coordinator, freezer, 0.25, 21.0)
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()

    # Due, but the mode target is back for less than two hours.
    await _run_hours(hass, coordinator, freezer, 1.75, 21.0)
    assert coordinator.setpoint_correction.correction_c == 0.0

    await _run_hours(hass, coordinator, freezer, 0.5, 21.0)
    assert coordinator.setpoint_correction.correction_c == 0.5

    coordinator.async_unload()


async def test_frost_protection_ignores_the_correction(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, calls = await _setup_for_correction(hass, monkeypatch)
    await _run_hours(hass, coordinator, freezer, 2.0, 21.0)
    assert coordinator.setpoint_correction.correction_c == 0.5

    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()

    assert _setpoints(calls)[-1] == 8.0

    coordinator.async_unload()


async def test_without_a_room_sensor_reading_nothing_is_corrected(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _calls = await _setup_for_correction(hass, monkeypatch)
    for _ in range(30):
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(ROOM_SENSOR, "unavailable", force_update=True)
        await hass.async_block_till_done()
        await coordinator._async_recompute()

    assert coordinator.setpoint_correction.correction_c == 0.0

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

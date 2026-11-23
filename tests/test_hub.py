"""House-wide entities of the hub: write budget, reachability, heating season."""

import time
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from heating_controller.const import DOMAIN
from heating_controller.control.write_budget import WritePriority
from homeassistant.core import HomeAssistant, ServiceCall, State
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)
from test_binary_sensor import _setup
from test_coordinator import _room_data


def _hub(hass: HomeAssistant):
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return entry.runtime_data


async def test_heat_source_unreachable_after_flow_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, {**_room_data(hass), "max_flow_gap_min": 30})
    assert hass.states.get("binary_sensor.heizung_heat_source_reachable").state == "on"

    hass.states.async_set("sensor.flow_temperature", "unavailable")
    for _ in range(31):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.heizung_heat_source_reachable").state == "off"

    hass.states.async_set("sensor.flow_temperature", "40.0")
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.heizung_heat_source_reachable").state == "on"


async def test_write_budget_reads_rate_limit_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.ratelimit_remaining_day", "21")
    await _setup(
        hass,
        {
            **_room_data(hass),
            "rate_limit_remaining_entity": "sensor.ratelimit_remaining_day",
            "rate_limit_reserve": 20,
        },
    )
    hub = _hub(hass)

    assert hub.request_write(WritePriority.NORMAL)
    hass.states.async_set("sensor.ratelimit_remaining_day", "20")
    await hass.async_block_till_done()
    assert not hub.request_write(WritePriority.NORMAL)
    assert hub.request_write(WritePriority.REQUIRED)

    state = hass.states.get("sensor.heizung_write_budget")
    assert state.state == "20"
    assert state.attributes["own_writes_today"] == 2
    assert state.attributes["dropped_today"] == 1


async def test_write_budget_without_entity_counts_down_own_maximum(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {**_room_data(hass), "max_writes_per_day": 3})
    _hub(hass).request_write(WritePriority.NORMAL)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.heizung_write_budget").state == "2"


async def test_heating_season_entities(hass: HomeAssistant) -> None:
    await _setup(hass, _room_data(hass))

    assert hass.states.get("sensor.heizung_heating_demand_last_24_h") is not None
    assert hass.states.get("binary_sensor.heizung_heating_season_switch_on") is not None
    assert (
        hass.states.get("binary_sensor.heizung_heating_season_switch_off") is not None
    )
    switch_on = hass.states.get("binary_sensor.heizung_heating_season_switch_on")
    assert switch_on.attributes["heat_source_entity"] == "climate.heat_source"
    on_threshold = hass.states.get("number.heizung_heating_season_switch_on_threshold")
    assert on_threshold is not None
    assert float(on_threshold.state) == 6.0

    await hass.services.async_call(
        "number",
        "set_value",
        {
            "entity_id": "number.heizung_heating_season_switch_on_threshold",
            "value": 8.0,
        },
        blocking=True,
    )
    assert _hub(hass).season_on_threshold_h == 8.0


AUTO_SWITCH = "switch.heizung_heating_season_automatic"


def _fake_set_hvac_mode(hass: HomeAssistant, *, follow: bool = True) -> list[str]:
    """Record set_hvac_mode; the heat source follows unless told not to."""
    modes: list[str] = []

    async def handler(call: ServiceCall) -> None:
        modes.append(call.data["hvac_mode"])
        if follow:
            hass.states.async_set(call.data["entity_id"], call.data["hvac_mode"])

    hass.services.async_register("climate", "set_hvac_mode", handler)
    return modes


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes=1):
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_season_switches_heat_source_off_and_stays_automatic(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, _room_data(hass))
    modes = _fake_set_hvac_mode(hass)
    hub = _hub(hass)
    assert hass.states.get(AUTO_SWITCH).state == "on"

    hub.season.last_demand_ts = time.time() - 49 * 3600
    await _tick(hass, freezer)

    assert modes == ["off"]
    assert hass.states.get("climate.heat_source").state == "off"
    assert hass.states.get(AUTO_SWITCH).state == "on"
    assert hub.write_budget.counts(time.time())[0] == 1


async def test_season_does_not_switch_while_automatic_is_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, _room_data(hass))
    modes = _fake_set_hvac_mode(hass)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": AUTO_SWITCH}, blocking=True
    )

    _hub(hass).season.last_demand_ts = time.time() - 49 * 3600
    await _tick(hass, freezer)

    assert modes == []
    assert hass.states.get("climate.heat_source").state == "heat"


async def test_season_repeats_a_command_only_after_the_retry_time(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, _room_data(hass))
    modes = _fake_set_hvac_mode(hass, follow=False)

    _hub(hass).season.last_demand_ts = time.time() - 49 * 3600
    await _tick(hass, freezer, 29)
    assert modes == ["off"]
    await _tick(hass, freezer, 2)
    assert modes == ["off", "off"]


async def test_season_does_not_switch_an_unreachable_heat_source(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, {**_room_data(hass), "max_flow_gap_min": 30})
    modes = _fake_set_hvac_mode(hass)
    hass.states.async_set("sensor.flow_temperature", "unavailable")
    await _tick(hass, freezer, 31)

    _hub(hass).season.last_demand_ts = time.time() - 49 * 3600
    await _tick(hass, freezer)

    assert modes == []


async def test_hand_switch_turns_automatic_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, _room_data(hass))
    modes = _fake_set_hvac_mode(hass)

    hass.states.async_set("climate.heat_source", "off")
    await hass.async_block_till_done()
    assert hass.states.get(AUTO_SWITCH).state == "off"

    # The criterion asks for heat, but the hand switch holds.
    hub = _hub(hass)
    hub.season.restore(
        {"periods": [[time.time() - 7 * 3600, None]], "alive_ts": time.time()}
    )
    hub.season.update(True, time.time(), hub.season_hold_threshold_h)
    assert hub.season_switch_on_recommended
    await _tick(hass, freezer)
    assert modes == []


async def test_heat_source_back_from_unavailable_is_no_hand_switch(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, _room_data(hass))

    hass.states.async_set("climate.heat_source", "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set("climate.heat_source", "off")
    await hass.async_block_till_done()

    assert hass.states.get(AUTO_SWITCH).state == "on"


async def test_season_automatic_restores_off(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(AUTO_SWITCH, "off")])
    await _setup(hass, _room_data(hass))

    assert hass.states.get(AUTO_SWITCH).state == "off"
    assert _hub(hass).season_auto_on is False

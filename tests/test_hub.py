"""House-wide entities of the hub: write budget, reachability, heating season."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from heating_controller.const import DOMAIN
from heating_controller.control.write_budget import WritePriority
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed
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

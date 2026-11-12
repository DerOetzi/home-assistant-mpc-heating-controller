"""The flow device: subentry flow, shadow mode, writes, hub threshold."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from heating_controller.const import (
    DOMAIN,
    HUB_KEYS,
    HUB_TITLE,
    SUBENTRY_TYPE_FLOW,
    SUBENTRY_TYPE_ROOM,
)
from heating_controller.control.flow import FALLBACK_GRACE_S
from heating_controller.const import HUB_VERSION
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from test_config_flow import _empty_hub
from test_coordinator import _room_data, _seed_entities

ACTUATOR = "climate.flow_offset"
FLOW_DATA = {
    "actuator_mode": "offset",
    "actuator_entity": ACTUATOR,
    "actuator_min": -10.0,
    "actuator_max": 10.0,
    "actuator_step": 1.0,
    "actuator_fallback": 0.0,
}


def _hub_with_flow(data: dict) -> MockConfigEntry:
    room = {key: value for key, value in data.items() if key not in HUB_KEYS}
    return MockConfigEntry(
        domain=DOMAIN,
        title=HUB_TITLE,
        version=HUB_VERSION,
        entry_id="hub-entry",
        unique_id=DOMAIN,
        data={key: value for key, value in data.items() if key in HUB_KEYS},
        subentries_data=[
            {
                "data": room,
                "subentry_id": "hub-entry",
                "subentry_type": SUBENTRY_TYPE_ROOM,
                "title": room["room_name"],
                "unique_id": slugify(room["room_name"]),
            },
            {
                "data": FLOW_DATA,
                "subentry_id": "flow-entry",
                "subentry_type": SUBENTRY_TYPE_FLOW,
                "title": "Vorlauf",
                "unique_id": SUBENTRY_TYPE_FLOW,
            },
        ],
    )


async def _setup(hass: HomeAssistant, offset: float = 2.0) -> list[ServiceCall]:
    _seed_entities(hass)
    hass.states.async_set(
        ACTUATOR,
        "heat",
        {"temperature": offset, "min_temp": -10, "max_temp": 10},
    )
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    async def ignore(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", record)
    hass.services.async_register("switch", "turn_on", ignore)
    hass.services.async_register("switch", "turn_off", ignore)
    entry = _hub_with_flow(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


def _actuator_calls(calls: list[ServiceCall]) -> list[ServiceCall]:
    return [call for call in calls if call.data.get("entity_id") == ACTUATOR]


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _past_fallback_grace(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    for _ in range(int(FALLBACK_GRACE_S // 60) + 1):
        await _tick(hass, freezer)


async def test_shadow_mode_computes_without_writing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    calls = await _setup(hass)
    await _tick(hass, freezer)
    # No lead room, but right after setup: nothing pending yet.
    target = hass.states.get("sensor.vorlauf_actuator_target")
    assert target.state == "0.0"
    assert target.attributes["pending_write"] is None

    await _past_fallback_grace(hass, freezer)

    # No lead room: fallback 0 is what would be written.
    target = hass.states.get("sensor.vorlauf_actuator_target")
    assert target.state == "0.0"
    assert target.attributes["writing"] is False
    assert target.attributes["pending_write"] == 0.0
    assert hass.states.get("sensor.vorlauf_actuator_actual").state == "2.0"
    assert _actuator_calls(calls) == []


async def test_writes_once_switched_on(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    calls = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.vorlauf_write_flow"}, blocking=True
    )
    await _past_fallback_grace(hass, freezer)

    (call,) = _actuator_calls(calls)
    assert call.data["temperature"] == 0.0

    # The cloud has not shown it yet: not written again.
    await _tick(hass, freezer)
    assert len(_actuator_calls(calls)) == 1
    budget = hass.states.get("sensor.heizung_write_budget")
    assert budget.attributes["own_writes_today"] == 1


async def test_hub_and_rooms_take_the_flow_threshold(hass: HomeAssistant) -> None:
    await _setup(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hub = entry.runtime_data

    # Offset mode: the curve's warm end, whatever offset the heat source has.
    assert hub.flow_threshold_c == 29.0
    assert all(room.flow_threshold_c == 29.0 for room in hub.rooms.values())
    assert hass.states.get("number.vorlauf_flow_threshold") is None
    assert (
        float(hass.states.get("sensor.vorlauf_flow_threshold_effective").state) == 29.0
    )

    hass.states.async_set(ACTUATOR, "heat", {"temperature": -5.0})
    assert hub.flow_threshold_c == 29.0

    await hass.services.async_call(
        "number",
        "set_value",
        {
            "entity_id": "number.vorlauf_heating_curve_flow_temperature_warm",
            "value": 27.0,
        },
        blocking=True,
    )
    assert hub.flow_threshold_c == 27.0


async def test_curve_sensors(hass: HomeAssistant) -> None:
    await _setup(hass)
    # Outdoor -3 °C on the default curve (-13/54, 18/29): 54 - 25/31*10.
    curve = round(54 - 25 / 31 * 10, 1)
    assert (
        float(hass.states.get("sensor.vorlauf_heating_curve_flow_temperature").state)
        == curve
    )
    effective = hass.states.get(
        "sensor.vorlauf_heating_curve_flow_temperature_effective"
    )
    assert float(effective.state) == round(curve + 2.0, 1)


async def test_subentry_flow_creates_and_allows_only_one(hass: HomeAssistant) -> None:
    hass.states.async_set(
        ACTUATOR,
        "heat",
        {"temperature": 0, "min_temp": -10, "max_temp": 10, "target_temp_step": 1},
    )
    entry = _empty_hub(hass)
    start = hass.config_entries.subentries.async_init
    configure = hass.config_entries.subentries.async_configure

    result = await start(
        (entry.entry_id, SUBENTRY_TYPE_FLOW),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["step_id"] == "actuator"
    result = await configure(
        result["flow_id"],
        {"actuator_mode": "offset", "actuator_entity": ACTUATOR},
    )
    assert result["step_id"] == "limits"
    result = await configure(
        result["flow_id"],
        {
            "actuator_min": -10,
            "actuator_max": 10,
            "actuator_step": 1,
            "actuator_fallback": 0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.data == FLOW_DATA

    again = await start(
        (entry.entry_id, SUBENTRY_TYPE_FLOW),
        context={"source": config_entries.SOURCE_USER},
    )
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"


async def test_curve_recommendation_from_the_lead_rooms_hold_flow(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set(
        ACTUATOR, "heat", {"temperature": 0, "min_temp": -10, "max_temp": 10}
    )

    async def ignore(call: ServiceCall) -> None:
        pass

    for domain, service in (
        ("climate", "set_temperature"),
        ("switch", "turn_on"),
        ("switch", "turn_off"),
    ):
        hass.services.async_register(domain, service, ignore)
    entry = _hub_with_flow({**_room_data(hass), "lead_flow": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    room = "sensor.wohnzimmer_hold_flow_temperature_at_curve"
    cold = hass.states.get(f"{room}_cold_end")
    warm = hass.states.get(f"{room}_warm_end")
    assert cold.attributes["outdoor_temperature_c"] == -13.0
    assert warm.attributes["outdoor_temperature_c"] == 18.0
    assert float(cold.state) > float(warm.state) > 0

    recommendation = hass.states.get(
        "sensor.vorlauf_heating_curve_recommendation_cold"
    )
    assert float(recommendation.state) == pytest.approx(
        float(cold.state) + 1.0, abs=0.06
    )
    assert recommendation.attributes["room"] == "Wohnzimmer"
    assert recommendation.attributes["current_curve_flow_c"] == 54.0
    assert recommendation.attributes["difference_k"] == pytest.approx(
        54.0 - float(recommendation.state), abs=0.06
    )

    curves = hass.states.get("sensor.vorlauf_hold_flow_curves")
    outdoor = curves.attributes["outdoor_c"]
    assert outdoor[0] == -15.0 and outdoor[-1] == 20.0
    (room,) = curves.attributes["rooms"]
    assert room["name"] == "Wohnzimmer" and room["lead"] is True
    assert len(room["flow_c"]) == len(outdoor)
    assert room["flow_c"][outdoor.index(-13.0)] == float(cold.state)
    assert room["flow_c"][outdoor.index(18.0)] == float(warm.state)

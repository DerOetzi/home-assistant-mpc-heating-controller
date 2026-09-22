"""The hold-flow sensor is the ungated counterpart to the min-flow sensor."""

from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from heating_controller.const import DOMAIN, FlowSupplyStatus

from test_coordinator import ENTRY_DATA, _seed_entities


def _register_stub_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)


async def _setup_coasting_room(hass: HomeAssistant, data: dict) -> None:
    """Room above target on stored heat, outdoor below target."""
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "12.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "25.0")
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 25.0}
    )
    _register_stub_services(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_hold_flow_sensor_keeps_reporting_while_the_gate_is_closed(
    hass: HomeAssistant,
) -> None:
    await _setup_coasting_room(hass, {**ENTRY_DATA, "flow_gate_hold_time_s": 0.0})

    gated = hass.states.get("sensor.wohnzimmer_minimum_flow_temperature")
    hold = hass.states.get("sensor.wohnzimmer_hold_flow_temperature")
    assert gated is not None and hold is not None

    assert float(gated.state) == 0
    assert gated.attributes["supply_status"] == FlowSupplyStatus.SURPLUS
    assert gated.attributes["flow_gate_closed"] is True

    assert float(hold.state) > 0
    assert hold.attributes["flow_gate_closed"] is True
    assert hold.attributes["gated_min_flow_temperature_c"] == 0
    assert hold.attributes["room_surplus_c"] > 0

    gated_hold = hass.states.get("sensor.wohnzimmer_gated_hold_flow_temperature")
    assert gated_hold is not None
    assert float(gated_hold.state) == 0
    assert gated_hold.attributes["flow_gate_closed"] is True
    assert gated_hold.attributes["hold_flow_temperature_c"] == float(hold.state)


async def test_hold_flow_sensor_matches_min_flow_while_the_gate_is_open(
    hass: HomeAssistant,
) -> None:
    await _setup_coasting_room(hass, ENTRY_DATA)

    gated = hass.states.get("sensor.wohnzimmer_minimum_flow_temperature")
    hold = hass.states.get("sensor.wohnzimmer_hold_flow_temperature")
    assert gated is not None and hold is not None

    assert gated.attributes["flow_gate_closed"] is False
    assert float(gated.state) > 0
    assert float(hold.state) == float(gated.state)


async def test_min_flow_sensor_reports_the_recovery_flow_for_a_cold_room(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "5.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "15.0")
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "off", {"current_temperature": 58.0}
    )
    _register_stub_services(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    gated = hass.states.get("sensor.wohnzimmer_minimum_flow_temperature")
    assert gated is not None

    recovery_c = gated.attributes["recovery_flow_temperature_c"]
    assert recovery_c > gated.attributes["hold_flow_temperature_c"]
    assert float(gated.state) == recovery_c

    gated_hold = hass.states.get("sensor.wohnzimmer_gated_hold_flow_temperature")
    assert gated_hold is not None
    assert float(gated_hold.state) == gated.attributes["hold_flow_temperature_c"]
    assert float(gated_hold.state) < recovery_c
    assert gated.attributes["recovery_flow_saturated"] in (True, False)

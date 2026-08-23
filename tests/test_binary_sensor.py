"""The heating_automation attributes are public API consumed by the Lovelace card."""

from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from heating_controller.const import DOMAIN

from test_coordinator import ENTRY_DATA, _seed_entities


def _register_stub_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)


async def _setup(hass: HomeAssistant, data: dict) -> None:
    _seed_entities(hass)
    _register_stub_services(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_exposes_configured_window_contacts(hass: HomeAssistant) -> None:
    await _setup(hass, ENTRY_DATA)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["window_contact_entities"] == [
        "binary_sensor.fenster_wohnzimmer"
    ]


async def test_window_contacts_empty_when_room_has_none(hass: HomeAssistant) -> None:
    data = {**ENTRY_DATA, "window_contact_entities": []}
    await _setup(hass, data)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["window_contact_entities"] == []


async def test_window_contacts_present_when_key_missing(hass: HomeAssistant) -> None:
    data = {k: v for k, v in ENTRY_DATA.items() if k != "window_contact_entities"}
    await _setup(hass, data)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["window_contact_entities"] == []


async def test_exposes_only_room_specific_comfort_conditions(
    hass: HomeAssistant,
) -> None:
    data = {
        **ENTRY_DATA,
        "room_comfort_condition_entities": ["input_boolean.gast_zu_besuch"],
    }
    await _setup(hass, data)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["comfort_condition_entities"] == [
        "input_boolean.gast_zu_besuch"
    ]


async def test_comfort_conditions_empty_when_key_missing(hass: HomeAssistant) -> None:
    assert "room_comfort_condition_entities" not in ENTRY_DATA
    await _setup(hass, ENTRY_DATA)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["comfort_condition_entities"] == []

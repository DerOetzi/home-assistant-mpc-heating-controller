"""The heating_automation attributes are public API consumed by the Lovelace card."""

from heating_controller.const import DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall
from test_coordinator import _hub_entry, _room_data, _seed_entities


def _register_stub_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)


async def _setup(hass: HomeAssistant, data: dict) -> None:
    _seed_entities(hass)
    _register_stub_services(hass)
    entry = _hub_entry(data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_exposes_configured_window_contacts(hass: HomeAssistant) -> None:
    await _setup(hass, _room_data(hass))

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["window_contact_entities"] == [
        "binary_sensor.fenster_wohnzimmer"
    ]


async def test_window_contacts_empty_when_room_has_none(hass: HomeAssistant) -> None:
    data = {**_room_data(hass), "window_contact_entities": []}
    await _setup(hass, data)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["window_contact_entities"] == []


async def test_window_contacts_present_when_key_missing(hass: HomeAssistant) -> None:
    data = {k: v for k, v in _room_data(hass).items() if k != "window_contact_entities"}
    await _setup(hass, data)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["window_contact_entities"] == []


async def test_exposes_only_room_specific_comfort_conditions(
    hass: HomeAssistant,
) -> None:
    data = {
        **_room_data(hass),
        "room_comfort_condition_entities": ["input_boolean.gast_zu_besuch"],
    }
    await _setup(hass, data)

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["comfort_condition_entities"] == [
        "input_boolean.gast_zu_besuch"
    ]


async def test_comfort_conditions_empty_when_key_missing(hass: HomeAssistant) -> None:
    assert "room_comfort_condition_entities" not in _room_data(hass)
    await _setup(hass, _room_data(hass))

    state = hass.states.get("binary_sensor.wohnzimmer_heating_automation")
    assert state is not None
    assert state.attributes["comfort_condition_entities"] == []


async def test_reports_a_radiator_that_stays_cold(hass: HomeAssistant) -> None:
    await _setup(hass, _room_data(hass))
    entity_id = "binary_sensor.wohnzimmer_radiator_not_heating"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["trv_entities"] == []

    coordinator = hass.data[DOMAIN][
        hass.config_entries.async_entries(DOMAIN)[0].entry_id
    ]
    coordinator.mpc._radiator_monitors[0].not_heating = True
    coordinator._notify_listeners()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["trv_entities"] == ["climate.heizung_wohnzimmer"]


async def test_outdoor_temperature_missing_is_off_while_the_mpc_runs(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, _room_data(hass))

    state = hass.states.get("binary_sensor.wohnzimmer_outdoor_temperature_missing")
    assert state is not None
    assert state.state == "off"
    assert state.attributes["since"] is None
    assert state.attributes["fallback_target_temperature_c"] is None


async def test_outdoor_temperature_missing_reports_the_fallback(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, _room_data(hass))
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.wohnzimmer_outdoor_temperature_missing")
    assert state.state == "on"
    assert state.attributes["since"] is not None
    assert state.attributes["fallback_target_temperature_c"] == 22.0


async def test_outdoor_temperature_missing_stays_off_right_after_startup(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "unavailable")
    _register_stub_services(hass)
    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.wohnzimmer_outdoor_temperature_missing")
    assert state.state == "off"

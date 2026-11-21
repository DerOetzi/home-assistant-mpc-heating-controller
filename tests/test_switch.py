"""The comfort switches: one on the hub, one per room that asks for it."""

from heating_controller.const import DOMAIN, HeatMode
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache
from test_binary_sensor import _setup
from test_coordinator import _hub_entry, _room_data, _seed_entities

ROOM_ID = "hub-entry"


def _switch_id(hass: HomeAssistant, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, unique_id)


def _room(hass: HomeAssistant):
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    (room,) = entry.runtime_data.rooms.values()
    return room


async def test_house_switch_starts_on_and_rooms_follow(hass: HomeAssistant) -> None:
    await _setup(hass, _room_data(hass))
    house = _switch_id(hass, "hub-entry_hub_comfort")
    assert hass.states.get(house).state == "on"
    assert _room(hass).state.is_comfort() is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": house}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(house).state == "off"
    room = _room(hass)
    assert room.state.is_comfort() is False
    assert room.current_heat_mode == HeatMode.ECO


async def test_house_switch_restores_off(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State("switch.heizung_comfort", "off")])
    await _setup(hass, _room_data(hass))

    house = _switch_id(hass, "hub-entry_hub_comfort")
    assert hass.states.get(house).state == "off"
    assert _room(hass).state.is_comfort() is False


async def test_room_switch_starts_off_and_releases_comfort(
    hass: HomeAssistant,
) -> None:
    await _setup(hass, {**_room_data(hass), "room_comfort_switch": True})
    own = _switch_id(hass, f"{ROOM_ID}_comfort")
    assert hass.states.get(own).state == "off"
    assert _room(hass).state.is_comfort() is False

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": own}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(own).state == "on"
    assert _room(hass).state.is_comfort() is True


async def test_pv_switch_raises_the_setpoint(hass: HomeAssistant) -> None:
    await _setup(hass, {**_room_data(hass), "pv_boost_enabled": True})
    pv = _switch_id(hass, "hub-entry_hub_pv_boost")
    assert hass.states.get(pv).state == "off"
    room = _room(hass)
    assert room.state.is_pv_boost_active is False

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": pv}, blocking=True
    )
    await hass.async_block_till_done()
    assert room.state.is_pv_boost_active is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": pv}, blocking=True
    )
    await hass.async_block_till_done()
    assert room.state.is_pv_boost_active is False


async def test_deselected_room_switch_leaves_the_registry(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch", DOMAIN, f"{ROOM_ID}_comfort", suggested_object_id="old_comfort"
    )
    await _setup(hass, _room_data(hass))

    assert _switch_id(hass, f"{ROOM_ID}_comfort") is None


async def test_a_reload_starts_with_the_restored_switches(hass: HomeAssistant) -> None:
    """The gate opens once the switches restored: no setpoint from the defaults."""
    mock_restore_cache(hass, [State("switch.heizung_comfort", "off")])
    _seed_entities(hass)
    written: list[float] = []

    async def set_temperature(call: ServiceCall) -> None:
        written.append(call.data["temperature"])

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", set_temperature)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)
    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    room = _room(hass)
    assert room.current_heat_mode == HeatMode.ECO
    eco_c = room.base_temperature_c
    assert written and set(written) == {eco_c}
    targets = {entry.target_temperature_c for entry in room.mpc._learner._history}
    assert targets == {eco_c}

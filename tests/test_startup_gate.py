from datetime import timedelta

from heating_controller.startup_gate import (
    STARTUP_QUIET_S,
    STARTUP_TIMEOUT_S,
    StartupGate,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

ENTITY = "switch.heizung_trv"


def _gate(hass: HomeAssistant) -> tuple[StartupGate, list[bool]]:
    opened: list[bool] = []

    async def on_open() -> None:
        opened.append(True)

    gate = StartupGate(hass, on_open)
    gate.require([ENTITY, None])
    return gate, opened


async def _wait(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_a_reload_with_everything_there_opens_at_once(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(ENTITY, "off")
    gate, opened = _gate(hass)
    gate.async_start()
    await hass.async_block_till_done()

    assert gate.is_open
    assert opened == [True]


async def test_opens_after_the_last_entity_came_in_and_quiet(
    hass: HomeAssistant, freezer
) -> None:
    gate, opened = _gate(hass)
    gate.async_start()
    await _wait(hass, freezer, STARTUP_QUIET_S + 1)
    assert not gate.is_open
    assert gate.missing == [ENTITY]

    hass.states.async_set(ENTITY, "off")
    await _wait(hass, freezer, STARTUP_QUIET_S - 5)
    assert not gate.is_open

    await _wait(hass, freezer, 10)
    assert gate.is_open
    assert opened == [True]


async def test_an_entity_dropping_out_again_holds_it(
    hass: HomeAssistant, freezer
) -> None:
    gate, _opened = _gate(hass)
    gate.async_start()
    hass.states.async_set(ENTITY, "off")
    await _wait(hass, freezer, 10)
    hass.states.async_set(ENTITY, "unavailable")
    await _wait(hass, freezer, STARTUP_QUIET_S)

    assert not gate.is_open


async def test_opens_on_timeout_without_an_entity(
    hass: HomeAssistant, freezer, caplog
) -> None:
    gate, opened = _gate(hass)
    gate.async_start()
    await _wait(hass, freezer, STARTUP_TIMEOUT_S + 1)

    assert gate.is_open
    assert opened == [True]
    assert ENTITY in caplog.text


async def test_waits_for_home_assistant_to_start(hass: HomeAssistant, freezer) -> None:
    hass.set_state(CoreState.not_running)
    hass.states.async_set(ENTITY, "off")
    gate, _opened = _gate(hass)
    gate.async_start()
    await _wait(hass, freezer, STARTUP_QUIET_S + 1)
    assert not gate.is_open

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert not gate.is_open

    await _wait(hass, freezer, STARTUP_QUIET_S + 1)
    assert gate.is_open

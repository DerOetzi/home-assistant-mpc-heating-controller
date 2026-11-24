"""Read numbers and on/off out of Home Assistant states."""

from __future__ import annotations

from typing import Any

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, State


def to_float(value: Any) -> float | None:
    """The value as a number; None if it is none (unavailable, text)."""
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def state_float(state: State | None) -> float | None:
    """The state as a number; None without a state or a number in it."""
    return None if state is None else to_float(state.state)


def attribute_float(state: State | None, name: str) -> float | None:
    """An attribute as a number; None without the state or a number in it."""
    return None if state is None else to_float(state.attributes.get(name))


def state_is_on(state: State | None) -> bool:
    return state is not None and state.state == STATE_ON


def entity_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """The entity's state as a number; None without the entity or a number."""
    return None if not entity_id else state_float(hass.states.get(entity_id))


def entity_is_on(hass: HomeAssistant, entity_id: str | None) -> bool:
    return entity_id is not None and state_is_on(hass.states.get(entity_id))

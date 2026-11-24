"""Field helpers shared by the config and subentry flows."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.helpers import selector

BOOLEAN_SIGNAL_DOMAINS = ["binary_sensor", "input_boolean", "switch"]


def marker(
    key: type[vol.Required] | type[vol.Optional], name: str, defaults: dict[str, Any]
) -> vol.Marker:
    # Plain strings, so the stored data never holds an enum member.
    if name in defaults and defaults[name] is not None:
        return key(str(name), default=defaults[name])
    return key(str(name))


def optional(name: str, defaults: dict[str, Any]) -> vol.Optional:
    """An optional field that can be cleared again: suggested, not defaulted."""
    if defaults.get(name):
        return vol.Optional(str(name), description={"suggested_value": defaults[name]})
    return vol.Optional(str(name))


def merge(data: dict[str, Any], schema: vol.Schema, user_input: dict[str, Any]) -> None:
    """Take a step's input; a cleared optional field drops its old value."""
    for key in schema.schema:
        data.pop(str(key), None)
    data.update(user_input)


def temperature_sensor() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
    )


def whole(minimum: int, maximum: int, unit: str | None = None) -> vol.All:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return vol.All(selector.NumberSelector(config), vol.Coerce(int))

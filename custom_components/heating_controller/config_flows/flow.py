"""The flow subentry: the heat source's actuator and its limits."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import State
from homeassistant.helpers import selector

from ..const import (
    CONF_ACTUATOR_ENTITY,
    CONF_ACTUATOR_FALLBACK,
    CONF_ACTUATOR_MAX,
    CONF_ACTUATOR_MIN,
    CONF_ACTUATOR_MODE,
    CONF_ACTUATOR_STEP,
    FLOW_TITLE,
    SUBENTRY_TYPE_FLOW,
)
from ..control.flow import ActuatorMode
from .fields import (
    marker,
    merge,
)

# --- Flow --------------------------------------------------------------------

_ACTUATOR_DOMAINS = ["climate", "number", "input_number"]


def _actuator_limits(state: State | None) -> dict[str, float]:
    """Limits of the actuator as the entity reports them, for pre-filling."""
    if state is None:
        return {}
    attrs = state.attributes
    if state.domain == "climate":
        keys = ("min_temp", "max_temp", "target_temp_step")
    else:
        keys = ("min", "max", "step")
    limits: dict[str, float] = {}
    for conf, key in zip(
        (CONF_ACTUATOR_MIN, CONF_ACTUATOR_MAX, CONF_ACTUATOR_STEP), keys, strict=True
    ):
        try:
            limits[conf] = float(attrs[key])
        except KeyError, TypeError, ValueError:
            continue
    return limits


def _flow_actuator_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ACTUATOR_MODE,
                default=defaults.get(CONF_ACTUATOR_MODE, ActuatorMode.OFFSET),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[mode.value for mode in ActuatorMode],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_ACTUATOR_MODE,
                )
            ),
            marker(vol.Required, CONF_ACTUATOR_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=_ACTUATOR_DOMAINS)
                )
            ),
        }
    )


def _flow_limits_schema(defaults: dict[str, Any]) -> vol.Schema:
    def number(key: str, fallback: float) -> vol.Required:
        return vol.Required(key, default=defaults.get(key, fallback))

    box = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=-100, max=100, step="any", mode=selector.NumberSelectorMode.BOX
        )
    )
    return vol.Schema(
        {
            number(CONF_ACTUATOR_MIN, -10.0): vol.All(box, vol.Coerce(float)),
            number(CONF_ACTUATOR_MAX, 10.0): vol.All(box, vol.Coerce(float)),
            number(CONF_ACTUATOR_STEP, 1.0): vol.All(box, vol.Coerce(float)),
            number(CONF_ACTUATOR_FALLBACK, 0.0): vol.All(box, vol.Coerce(float)),
        }
    )


class FlowSubentryFlow(ConfigSubentryFlow):
    """The heat source's flow actuator; one per hub."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._reconfiguring = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if any(
            subentry.subentry_type == SUBENTRY_TYPE_FLOW
            for subentry in self._get_entry().subentries.values()
        ):
            return self.async_abort(reason="already_configured")
        return await self.async_step_actuator(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._reconfiguring = True
        self._data = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_actuator()

    async def async_step_actuator(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _flow_actuator_schema(self._data)
        if user_input is not None:
            changed = user_input[CONF_ACTUATOR_ENTITY] != self._data.get(
                CONF_ACTUATOR_ENTITY
            )
            merge(self._data, schema, user_input)
            if changed:
                self._data.update(
                    _actuator_limits(
                        self.hass.states.get(user_input[CONF_ACTUATOR_ENTITY])
                    )
                )
            return await self.async_step_limits()
        return self.async_show_form(step_id="actuator", data_schema=schema)

    async def async_step_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _flow_limits_schema(self._data)
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input[CONF_ACTUATOR_MIN] >= user_input[CONF_ACTUATOR_MAX]
                or user_input[CONF_ACTUATOR_STEP] <= 0
            ):
                errors["base"] = "invalid_limits"
            else:
                merge(self._data, schema, user_input)
                if self._reconfiguring:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        data=self._data,
                    )
                return self.async_create_entry(
                    title=FLOW_TITLE, data=self._data, unique_id=SUBENTRY_TYPE_FLOW
                )
        return self.async_show_form(step_id="limits", data_schema=schema, errors=errors)

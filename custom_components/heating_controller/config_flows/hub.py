"""Schemas of the hub's own steps."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.helpers import selector

from ..const import (
    CONF_BUILDING_ROTATION,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_CURVE_ENTITY,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_FROST_PROTECTION_TEMPERATURE,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_MAX_FLOW_GAP_MIN,
    CONF_MAX_SENSOR_AGE,
    CONF_MAX_WRITES_PER_DAY,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_RATE_LIMIT_REMAINING_ENTITY,
    CONF_RATE_LIMIT_RESERVE,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_WORKDAY_ENTITY,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_MAX_FLOW_GAP_MIN,
    DEFAULT_MAX_WRITES_PER_DAY,
    DEFAULT_RATE_LIMIT_RESERVE,
    DEFAULT_ROOF_TILT_DEG,
    DesignTemperatureSystem,
    RoofOrientation,
)
from .fields import (
    BOOLEAN_SIGNAL_DOMAINS,
    marker,
    optional,
    temperature_sensor,
    whole,
)

_FLOW_THRESHOLD_DOMAINS = ["input_number", "number", "sensor"]


# --- Hub ---------------------------------------------------------------------


def hub_sources_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            marker(vol.Required, CONF_OUTDOOR_TEMPERATURE_ENTITY, defaults): (
                temperature_sensor()
            ),
            marker(vol.Required, CONF_HEAT_SOURCE_CLIMATE_ENTITY, defaults): (
                selector.EntitySelector(selector.EntitySelectorConfig(domain="climate"))
            ),
            marker(vol.Required, CONF_FLOW_TEMPERATURE_ENTITY, defaults): (
                temperature_sensor()
            ),
            vol.Required(
                CONF_FLOW_THRESHOLD,
                default=defaults.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C),
            ): vol.Coerce(float),
            optional(CONF_FLOW_THRESHOLD_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=_FLOW_THRESHOLD_DOMAINS)
            ),
            optional(CONF_FLOW_CURVE_ENTITY, defaults): temperature_sensor(),
            vol.Required(
                CONF_FROST_PROTECTION_TEMPERATURE,
                default=defaults.get(CONF_FROST_PROTECTION_TEMPERATURE, 8.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_FLOW_GAP_MIN,
                default=defaults.get(CONF_MAX_FLOW_GAP_MIN, DEFAULT_MAX_FLOW_GAP_MIN),
            ): whole(5, 240, "min"),
            optional(CONF_RATE_LIMIT_REMAINING_ENTITY, defaults): (
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            ),
            vol.Required(
                CONF_RATE_LIMIT_RESERVE,
                default=defaults.get(
                    CONF_RATE_LIMIT_RESERVE, DEFAULT_RATE_LIMIT_RESERVE
                ),
            ): whole(0, 1000),
            vol.Required(
                CONF_MAX_WRITES_PER_DAY,
                default=defaults.get(
                    CONF_MAX_WRITES_PER_DAY, DEFAULT_MAX_WRITES_PER_DAY
                ),
            ): whole(1, 1000),
        }
    )


def hub_control_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_DESIGN_OUTDOOR_TEMPERATURE,
                default=defaults.get(CONF_DESIGN_OUTDOOR_TEMPERATURE, -12.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_DESIGN_TEMPERATURE_SYSTEM,
                default=defaults.get(
                    CONF_DESIGN_TEMPERATURE_SYSTEM,
                    DesignTemperatureSystem.SYSTEM_55_45,
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[s.value for s in DesignTemperatureSystem],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_DESIGN_TEMPERATURE_SYSTEM,
                )
            ),
            vol.Required(
                CONF_FLOW_GATE_CLOSE_SURPLUS,
                default=defaults.get(
                    CONF_FLOW_GATE_CLOSE_SURPLUS, DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C
                ),
            ): vol.Coerce(float),
            vol.Required(
                CONF_FLOW_GATE_OPEN_SURPLUS,
                default=defaults.get(
                    CONF_FLOW_GATE_OPEN_SURPLUS, DEFAULT_FLOW_GATE_OPEN_SURPLUS_C
                ),
            ): vol.Coerce(float),
            vol.Required(
                CONF_FLOW_GATE_HOLD_TIME,
                default=defaults.get(
                    CONF_FLOW_GATE_HOLD_TIME, DEFAULT_FLOW_GATE_HOLD_TIME_S
                ),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_SENSOR_AGE, default=defaults.get(CONF_MAX_SENSOR_AGE, 1800.0)
            ): vol.Coerce(float),
        }
    )


def hub_sun_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_BUILDING_ROTATION,
                default=defaults.get(CONF_BUILDING_ROTATION, 0.0),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-180,
                    max=180,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°",
                )
            ),
            vol.Required(
                CONF_ROOF_ORIENTATION,
                default=defaults.get(CONF_ROOF_ORIENTATION, RoofOrientation.NONE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[o.value for o in RoofOrientation],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_ROOF_ORIENTATION,
                )
            ),
            vol.Required(
                CONF_ROOF_TILT,
                default=defaults.get(CONF_ROOF_TILT, DEFAULT_ROOF_TILT_DEG),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=90,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°",
                )
            ),
            optional(CONF_WORKDAY_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=BOOLEAN_SIGNAL_DOMAINS)
            ),
        }
    )

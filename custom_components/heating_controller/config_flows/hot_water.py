"""The hot water subentry: sources, modes, legionella, circulation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers import selector

from ..const import (
    HOT_WATER_TITLE,
    SUBENTRY_TYPE_HOT_WATER,
)
from ..hot_water_settings import HOT_WATER_DEFAULTS, WEEKDAYS, HotWaterKey
from .fields import (
    BOOLEAN_SIGNAL_DOMAINS,
    marker,
    merge,
    optional,
)

# --- Hot water ---------------------------------------------------------------

K = HotWaterKey


def _hw_number(key: str, defaults: dict[str, Any]) -> tuple[vol.Required, Any]:
    box = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=-10000, max=100000, step="any", mode=selector.NumberSelectorMode.BOX
        )
    )
    return vol.Required(str(key), default=defaults[key]), vol.All(
        box, vol.Coerce(float)
    )


def _hw_time(key: str, defaults: dict[str, Any]) -> tuple[vol.Required, Any]:
    return vol.Required(str(key), default=defaults[key]), selector.TimeSelector()


def _hw_bool(key: str, defaults: dict[str, Any]) -> tuple[vol.Required, Any]:
    return vol.Required(str(key), default=defaults[key]), selector.BooleanSelector()


def _hw_entity(
    key: str, defaults: dict[str, Any], domain: str | list[str], required: bool = False
) -> tuple[vol.Marker, Any]:
    field = marker(vol.Required, key, defaults) if required else optional(key, defaults)
    return field, selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _hw_entities(
    key: str, defaults: dict[str, Any], domain: str | list[str]
) -> tuple[vol.Optional, Any]:
    return vol.Optional(
        str(key), default=defaults.get(key) or []
    ), selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=True)
    )


def _schema(*fields: tuple[vol.Marker, Any]) -> vol.Schema:
    return vol.Schema(dict(fields))


def _hw_sources_schema(d: dict[str, Any]) -> vol.Schema:
    return _schema(
        _hw_entity(K.WATER_HEATER_ENTITY, d, "water_heater", required=True),
        _hw_entity(K.TANK_TEMPERATURE_ENTITY, d, "sensor"),
        _hw_entity(K.GRID_EXPORT_ENTITY, d, "sensor"),
        _hw_entity(K.BATTERY_SOC_ENTITY, d, "sensor"),
        _hw_entity(K.BATTERY_DISCHARGE_ENTITY, d, "sensor"),
        _hw_bool(K.LEGIONELLA_ENABLED, d),
        _hw_bool(K.CIRCULATION_ENABLED, d),
    )


def _hw_modes_schema(d: dict[str, Any]) -> vol.Schema:
    numbers = (
        K.GRID_LOWER_W,
        K.GRID_UPPER_W,
        K.GRID_RELEASE_W,
        K.GRID_STABLE_MIN,
        K.BATTERY_SOC_MIN,
        K.DISCHARGE_MAX_W,
        K.DISCHARGE_STABLE_MIN,
        K.OUTDOOR_LOWER_C,
        K.OUTDOOR_UPPER_C,
        K.TANK_GATE_C,
        K.MIN_HOLD_MIN,
        K.MANUAL_HOLD_MIN,
        K.BOOST_MAX_H,
        K.REHEAT_HYSTERESIS_K,
        K.MAX_SETPOINT_C,
    )
    return _schema(
        *(_hw_number(key, d) for key in numbers),
        _hw_bool(K.NIGHT_ENABLED, d),
        _hw_time(K.NIGHT_START, d),
        _hw_time(K.NIGHT_END, d),
        _hw_time(K.HEATING_NIGHT_END, d),
        _hw_bool(K.WINDOW_ENABLED, d),
        _hw_time(K.WINDOW_START, d),
        _hw_time(K.WINDOW_END, d),
        _hw_bool(K.FORCED_CHARGE, d),
    )


def _hw_legionella_schema(d: dict[str, Any]) -> vol.Schema:
    return _schema(
        (
            vol.Required(str(K.LEGIONELLA_WEEKDAYS), default=d[K.LEGIONELLA_WEEKDAYS]),
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(WEEKDAYS),
                    multiple=True,
                    translation_key="weekday",
                )
            ),
        ),
        _hw_time(K.LEGIONELLA_START, d),
        (
            optional(K.LEGIONELLA_FALLBACK, d),
            selector.TimeSelector(),
        ),
        _hw_number(K.LEGIONELLA_RETRY_MIN, d),
        _hw_entity(K.FORECAST_ENTITY, d, "sensor"),
        _hw_bool(K.FORECAST_ABOVE, d),
        _hw_number(K.FORECAST_THRESHOLD, d),
        _hw_entity(K.OUTLET_ENTITY, d, "sensor"),
        _hw_number(K.OUTLET_THRESHOLD_C, d),
        _hw_entity(K.RETURN_ENTITY, d, "sensor"),
        _hw_number(K.RETURN_THRESHOLD_C, d),
        _hw_number(K.LEGIONELLA_HOLD_MIN, d),
        _hw_number(K.LEGIONELLA_TIMEOUT_MIN, d),
    )


def _hw_circulation_schema(d: dict[str, Any]) -> vol.Schema:
    numbers = (
        K.PEAK_START_BELOW_C,
        K.OFFPEAK_START_BELOW_C,
        K.STOP_DELTA_K,
        K.STOP_TEMPERATURE_C,
        K.MAX_RUN_MIN,
        K.MIN_PAUSE_MIN,
        K.FALLBACK_ON_MIN,
        K.FALLBACK_OFF_MIN,
        K.RELEASE_TEMPERATURE_C,
        K.RELEASE_MIN,
    )
    return _schema(
        _hw_entity(K.PUMP_ENTITY, d, "switch", required=True),
        _hw_entity(K.CIRC_RETURN_ENTITY, d, "sensor", required=True),
        _hw_entity(K.CIRC_OUTLET_ENTITY, d, "sensor"),
        _hw_entities(K.DRAW_ENTITIES, d, "binary_sensor"),
        _hw_entities(K.CONTINUOUS_ENTITIES, d, ["input_boolean", "switch"]),
        (
            vol.Required(str(K.BLOCKED_STATES), default=d[K.BLOCKED_STATES]),
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["off", "eco", "heat_pump", "performance"],
                    multiple=True,
                    custom_value=True,
                )
            ),
        ),
        _hw_entity(K.PRESENCE_ENTITY, d, BOOLEAN_SIGNAL_DOMAINS),
        _hw_entity(K.DAYMODE_ENTITY, d, BOOLEAN_SIGNAL_DOMAINS),
        _hw_time(K.PEAK1_START, d),
        _hw_time(K.PEAK1_END, d),
        _hw_time(K.PEAK2_START, d),
        _hw_time(K.PEAK2_END, d),
        *(_hw_number(key, d) for key in numbers),
        _hw_bool(K.DRAW_OVERRIDES_GATES, d),
        _hw_bool(K.RELEASE_ENABLED, d),
    )


class HotWaterSubentryFlow(ConfigSubentryFlow):
    """Hot water target, legionella cycle and circulation; one per hub."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._reconfiguring = False

    def _defaults(self) -> dict[str, Any]:
        return {**HOT_WATER_DEFAULTS, **self._data}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if any(
            subentry.subentry_type == SUBENTRY_TYPE_HOT_WATER
            for subentry in self._get_entry().subentries.values()
        ):
            return self.async_abort(reason="already_configured")
        return await self.async_step_sources(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._reconfiguring = True
        self._data = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_sources()

    async def _step(
        self,
        step_id: str,
        schema: vol.Schema,
        user_input: dict[str, Any] | None,
        next_step: Callable[[], Any],
    ) -> SubentryFlowResult:
        if user_input is not None:
            merge(self._data, schema, user_input)
            return await next_step()
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._step(
            "sources",
            _hw_sources_schema(self._defaults()),
            user_input,
            self.async_step_modes,
        )

    async def async_step_modes(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._step(
            "modes", _hw_modes_schema(self._defaults()), user_input, self._after_modes
        )

    async def _after_modes(self) -> SubentryFlowResult:
        if self._data.get(K.LEGIONELLA_ENABLED):
            return await self.async_step_legionella()
        return await self._after_legionella()

    async def async_step_legionella(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._step(
            "legionella",
            _hw_legionella_schema(self._defaults()),
            user_input,
            self._after_legionella,
        )

    async def _after_legionella(self) -> SubentryFlowResult:
        if self._data.get(K.CIRCULATION_ENABLED):
            return await self.async_step_circulation()
        return self._finish()

    async def async_step_circulation(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _hw_circulation_schema(self._defaults())
        if user_input is not None:
            merge(self._data, schema, user_input)
            return self._finish()
        return self.async_show_form(step_id="circulation", data_schema=schema)

    def _finish(self) -> SubentryFlowResult:
        if self._reconfiguring:
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), data=self._data
            )
        return self.async_create_entry(
            title=HOT_WATER_TITLE, data=self._data, unique_id=SUBENTRY_TYPE_HOT_WATER
        )

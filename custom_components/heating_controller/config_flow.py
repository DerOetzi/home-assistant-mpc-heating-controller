from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_BOOST_ENABLED,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_COMFORT_CONDITION_ENTITIES,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_FROST_PROTECTION_TEMPERATURE,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEARNING_WINDOW_ENTITY,
    CONF_MAX_SENSOR_AGE,
    CONF_MPC_DEMAND_HYSTERESIS_PCT,
    CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
    CONF_MPC_HOLD_TIME,
    CONF_MPC_MAX_DEMAND_STEP_PCT,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_ENTITY,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOM_COMFORT_CONDITION_ENTITIES,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_STATIONARY_RANGE,
    CONF_TRV_ACTIVE_SWITCH,
    CONF_TRV_COUNT,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_ENTITY_ID,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_MAX_TARGET_TEMPERATURE,
    CONF_TRV_MIN_TARGET_TEMPERATURE,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_TARGET_TEMPERATURE_STEP,
    CONF_TRV_WIDTH_MM,
    CONF_TRVS,
    CONF_WINDOW_CONTACT_ENTITIES,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_STATIONARY_RANGE_C,
    DOMAIN,
    MAX_STATIONARY_RANGE_C,
    MAX_TRV_COUNT,
    MIN_STATIONARY_RANGE_C,
    STATIONARY_RANGE_STEP_C,
    DesignTemperatureSystem,
    HeatEmitterType,
    PanelRadiatorType,
)

TRV_COUNT_OPTIONS = list(range(1, MAX_TRV_COUNT + 1))

_BOOLEAN_SIGNAL_DOMAINS = ["binary_sensor", "input_boolean", "switch"]
_WINDOW_DEVICE_CLASSES = ["window", "door", "garage_door", "opening"]
_FLOW_THRESHOLD_DOMAINS = ["input_number", "number", "sensor"]


def _trv_step_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            _marker(vol.Required, CONF_TRV_ENTITY_ID, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                )
            ),
            _marker(vol.Optional, CONF_TRV_ACTIVE_SWITCH, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                )
            ),
            vol.Required(
                CONF_TRV_EMITTER_TYPE,
                default=defaults.get(CONF_TRV_EMITTER_TYPE, HeatEmitterType.PANEL),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[e.value for e in HeatEmitterType],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_TRV_EMITTER_TYPE,
                )
            ),
            vol.Required(
                CONF_TRV_MIN_TARGET_TEMPERATURE,
                default=defaults.get(CONF_TRV_MIN_TARGET_TEMPERATURE, 5.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_TRV_MAX_TARGET_TEMPERATURE,
                default=defaults.get(CONF_TRV_MAX_TARGET_TEMPERATURE, 30.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_TRV_TARGET_TEMPERATURE_STEP,
                default=defaults.get(CONF_TRV_TARGET_TEMPERATURE_STEP, 0.5),
            ): vol.Coerce(float),
        }
    )


def _trv_details_schema(
    emitter_type: str, defaults: dict[str, Any] | None = None
) -> vol.Schema:
    defaults = defaults or {}
    if emitter_type == HeatEmitterType.TOWEL:
        return vol.Schema(
            {
                vol.Required(
                    CONF_TRV_NOMINAL_POWER_W,
                    default=defaults.get(CONF_TRV_NOMINAL_POWER_W, 500.0),
                ): vol.Coerce(float)
            }
        )
    return vol.Schema(
        {
            vol.Required(
                CONF_TRV_RADIATOR_TYPE,
                default=defaults.get(CONF_TRV_RADIATOR_TYPE, PanelRadiatorType.TYPE_22),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[t.value for t in PanelRadiatorType],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_TRV_RADIATOR_TYPE,
                )
            ),
            vol.Required(
                CONF_TRV_WIDTH_MM, default=defaults.get(CONF_TRV_WIDTH_MM, 1000.0)
            ): vol.Coerce(float),
            vol.Required(
                CONF_TRV_HEIGHT_MM, default=defaults.get(CONF_TRV_HEIGHT_MM, 600.0)
            ): vol.Coerce(float),
        }
    )


def _marker(key: type[vol.Marker], name: str, defaults: dict[str, Any]) -> vol.Marker:
    if name in defaults and defaults[name] is not None:
        return key(name, default=defaults[name])
    return key(name)


def _entities_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            _marker(vol.Optional, CONF_ROOM_SENSOR_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                )
            ),
            vol.Optional(
                CONF_WINDOW_CONTACT_ENTITIES,
                default=defaults.get(CONF_WINDOW_CONTACT_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor",
                    device_class=_WINDOW_DEVICE_CLASSES,
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_COMFORT_CONDITION_ENTITIES,
                default=defaults.get(CONF_COMFORT_CONDITION_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=_BOOLEAN_SIGNAL_DOMAINS + ["schedule"], multiple=True
                )
            ),
            vol.Optional(
                CONF_ROOM_COMFORT_CONDITION_ENTITIES,
                default=defaults.get(CONF_ROOM_COMFORT_CONDITION_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=_BOOLEAN_SIGNAL_DOMAINS + ["schedule"], multiple=True
                )
            ),
            _marker(vol.Required, CONF_OUTDOOR_TEMPERATURE_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                )
            ),
            _marker(vol.Required, CONF_HEAT_SOURCE_CLIMATE_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                )
            ),
            _marker(vol.Optional, CONF_FLOW_THRESHOLD_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=_FLOW_THRESHOLD_DOMAINS)
                )
            ),
            _marker(vol.Required, CONF_PV_BOOST_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=_BOOLEAN_SIGNAL_DOMAINS)
                )
            ),
            _marker(vol.Optional, CONF_LEARNING_WINDOW_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=_BOOLEAN_SIGNAL_DOMAINS)
                )
            ),
        }
    )


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_BOOST_ENABLED, default=defaults.get(CONF_BOOST_ENABLED, False)
            ): bool,
            vol.Required(
                CONF_BOOST_TEMPERATURE_OFFSET,
                default=defaults.get(CONF_BOOST_TEMPERATURE_OFFSET, 5.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_FROST_PROTECTION_TEMPERATURE,
                default=defaults.get(CONF_FROST_PROTECTION_TEMPERATURE, 8.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_PV_BOOST_ENABLED,
                default=defaults.get(CONF_PV_BOOST_ENABLED, False),
            ): bool,
            vol.Required(
                CONF_PV_BOOST_TEMPERATURE_OFFSET,
                default=defaults.get(CONF_PV_BOOST_TEMPERATURE_OFFSET, 1.0),
            ): vol.Coerce(float),
        }
    )


def _mpc_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_DESIGN_INDOOR_TEMPERATURE,
                default=defaults.get(CONF_DESIGN_INDOOR_TEMPERATURE, 20.0),
            ): vol.Coerce(float),
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
                CONF_ROOM_HEAT_LOAD, default=defaults.get(CONF_ROOM_HEAT_LOAD, 1000.0)
            ): vol.Coerce(float),
            vol.Required(
                CONF_MPC_DEMAND_HYSTERESIS_PCT,
                default=defaults.get(CONF_MPC_DEMAND_HYSTERESIS_PCT, 5.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MPC_HOLD_TIME, default=defaults.get(CONF_MPC_HOLD_TIME, 300.0)
            ): vol.Coerce(float),
            vol.Required(
                CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
                default=defaults.get(CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT, 40.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MPC_MAX_DEMAND_STEP_PCT,
                default=defaults.get(CONF_MPC_MAX_DEMAND_STEP_PCT, 20.0),
            ): vol.Coerce(float),
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
            vol.Required(
                CONF_STATIONARY_RANGE,
                default=defaults.get(CONF_STATIONARY_RANGE, DEFAULT_STATIONARY_RANGE_C),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_STATIONARY_RANGE_C,
                    max=MAX_STATIONARY_RANGE_C,
                    step=STATIONARY_RANGE_STEP_C,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="K",
                )
            ),
        }
    )


class HeatingControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._trvs: list[dict[str, Any]] = []
        self._pending_trv: dict[str, Any] = {}
        self._trv_index = 0
        self._trv_count = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            await self.async_set_unique_id(slugify(user_input[CONF_ROOM_NAME]))
            self._abort_if_unique_id_configured()

            self._data[CONF_ROOM_NAME] = user_input[CONF_ROOM_NAME]
            self._trv_count = user_input[CONF_TRV_COUNT]
            self._trvs = []
            self._trv_index = 0
            return await self.async_step_trv()

        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_NAME): str,
                vol.Required(CONF_TRV_COUNT, default=1): vol.In(TRV_COUNT_OPTIONS),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_trv(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._pending_trv = dict(user_input)
            return await self.async_step_trv_details()

        return self.async_show_form(
            step_id="trv",
            data_schema=_trv_step_schema(),
            description_placeholders={"index": str(self._trv_index + 1)},
        )

    async def async_step_trv_details(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        emitter_type = self._pending_trv[CONF_TRV_EMITTER_TYPE]

        if user_input is not None:
            self._pending_trv.update(user_input)
            self._trvs.append(self._pending_trv)
            self._pending_trv = {}
            self._trv_index += 1

            if self._trv_index < self._trv_count:
                return await self.async_step_trv()
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="trv_details",
            data_schema=_trv_details_schema(emitter_type),
            description_placeholders={"index": str(self._trv_index + 1)},
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_settings()

        return self.async_show_form(
            step_id="entities", data_schema=_entities_schema({})
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_mpc()

        return self.async_show_form(
            step_id="settings", data_schema=_settings_schema({})
        )

    async def async_step_mpc(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._data.update(user_input)
            self._data[CONF_TRVS] = self._trvs
            return self.async_create_entry(
                title=self._data[CONF_ROOM_NAME], data=self._data
            )

        return self.async_show_form(step_id="mpc", data_schema=_mpc_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HeatingControllerOptionsFlow:
        return HeatingControllerOptionsFlow(config_entry)


class HeatingControllerOptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data = dict(config_entry.data)
        self._existing_trvs: list[dict[str, Any]] = list(
            config_entry.data.get(CONF_TRVS, [])
        )
        self._trvs: list[dict[str, Any]] = []
        self._pending_trv: dict[str, Any] = {}
        self._trv_index = 0
        self._trv_count = len(self._existing_trvs)

    def _trv_defaults(self) -> dict[str, Any]:
        if self._trv_index < len(self._existing_trvs):
            return self._existing_trvs[self._trv_index]
        return {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        self._trvs = []
        self._trv_index = 0
        return await self.async_step_trv()

    async def async_step_trv(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._pending_trv = dict(user_input)
            return await self.async_step_trv_details()

        return self.async_show_form(
            step_id="trv",
            data_schema=_trv_step_schema(self._trv_defaults()),
            description_placeholders={"index": str(self._trv_index + 1)},
        )

    async def async_step_trv_details(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        emitter_type = self._pending_trv[CONF_TRV_EMITTER_TYPE]

        if user_input is not None:
            self._pending_trv.update(user_input)
            self._trvs.append(self._pending_trv)
            self._pending_trv = {}
            self._trv_index += 1

            if self._trv_index < self._trv_count:
                return await self.async_step_trv()
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="trv_details",
            data_schema=_trv_details_schema(emitter_type, self._trv_defaults()),
            description_placeholders={"index": str(self._trv_index + 1)},
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_settings()

        return self.async_show_form(
            step_id="entities", data_schema=_entities_schema(self._data)
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_mpc()

        return self.async_show_form(
            step_id="settings", data_schema=_settings_schema(self._data)
        )

    async def async_step_mpc(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._data.update(user_input)
            self._data[CONF_TRVS] = self._trvs
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=self._data
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="mpc", data_schema=_mpc_schema(self._data))

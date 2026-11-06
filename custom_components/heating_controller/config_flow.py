from __future__ import annotations

from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import State, callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_ACTUATOR_ENTITY,
    CONF_ACTUATOR_FALLBACK,
    CONF_ACTUATOR_MAX,
    CONF_ACTUATOR_MIN,
    CONF_ACTUATOR_MODE,
    CONF_ACTUATOR_STEP,
    CONF_BOOST_ENABLED,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_BUILDING_ROTATION,
    CONF_CONSTRUCTION,
    CONF_DESIGN_INDOOR_TEMPERATURE,
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
    CONF_LEAD_FLOW,
    CONF_LEAD_HEATING_LIMIT,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_MAX_FLOW_GAP_MIN,
    CONF_MAX_SENSOR_AGE,
    CONF_MAX_WRITES_PER_DAY,
    CONF_MPC_DEMAND_HYSTERESIS_PCT,
    CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
    CONF_MPC_HOLD_TIME,
    CONF_MPC_MAX_DEMAND_STEP_PCT,
    CONF_OPAQUE_SURFACES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_RATE_LIMIT_REMAINING_ENTITY,
    CONF_RATE_LIMIT_RESERVE,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_ROOM_AREA,
    CONF_ROOM_COMFORT_SWITCH,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_STATIONARY_RANGE,
    CONF_TARGET_BAND,
    CONF_TRV_COUNT,
    CONF_TRV_DEVICE_ID,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_SETPOINT_MODE,
    CONF_TRV_WIDTH_MM,
    CONF_TRVS,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_WINDOW_SURFACES,
    CONF_WORKDAY_ENTITY,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_MAX_FLOW_GAP_MIN,
    DEFAULT_MAX_WRITES_PER_DAY,
    DEFAULT_RATE_LIMIT_RESERVE,
    DEFAULT_ROOF_TILT_DEG,
    DEFAULT_STATIONARY_RANGE_C,
    DEFAULT_TARGET_BAND_C,
    DEFAULT_TRV_SETPOINT_MODE,
    DOMAIN,
    FACADES,
    FLOW_TITLE,
    HOT_WATER_TITLE,
    HUB_TITLE,
    MAX_STATIONARY_RANGE_C,
    MAX_TARGET_BAND_C,
    MAX_TRV_COUNT,
    MIN_STATIONARY_RANGE_C,
    MIN_TARGET_BAND_C,
    STATIONARY_RANGE_STEP_C,
    SUBENTRY_TYPE_FLOW,
    SUBENTRY_TYPE_HOT_WATER,
    SUBENTRY_TYPE_ROOM,
    TARGET_BAND_STEP_C,
    Construction,
    DesignTemperatureSystem,
    HeatEmitterType,
    PanelRadiatorType,
    RoofOrientation,
    Surface,
    TrvSetpointMode,
)
from .control.flow import ActuatorMode
from .hot_water_device import HOT_WATER_DEFAULTS, WEEKDAYS
from .migration import HUB_VERSION
from .profiles import (
    DeviceProfile,
    TrvResolveError,
    async_get_profiles,
    device_selector_filters,
    resolve_trv,
)

TRV_COUNT_OPTIONS = list(range(1, MAX_TRV_COUNT + 1))

_BOOLEAN_SIGNAL_DOMAINS = ["binary_sensor", "input_boolean", "switch"]
_WINDOW_DEVICE_CLASSES = ["window", "door", "garage_door", "opening"]
_FLOW_THRESHOLD_DOMAINS = ["input_number", "number", "sensor"]


def _marker(key: type[vol.Marker], name: str, defaults: dict[str, Any]) -> vol.Marker:
    if name in defaults and defaults[name] is not None:
        return key(name, default=defaults[name])
    return key(name)


def _optional(name: str, defaults: dict[str, Any]) -> vol.Optional:
    """An optional field that can be cleared again: suggested, not defaulted."""
    if defaults.get(name):
        return vol.Optional(name, description={"suggested_value": defaults[name]})
    return vol.Optional(name)


def _merge(
    data: dict[str, Any], schema: vol.Schema, user_input: dict[str, Any]
) -> None:
    """Take a step's input; a cleared optional field drops its old value."""
    for key in schema.schema:
        data.pop(str(key), None)
    data.update(user_input)


def _temperature_sensor() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
    )


def _kelvin(minimum: float, maximum: float, step: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="K",
        )
    )


# --- Hub ---------------------------------------------------------------------


def _hub_sources_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            _marker(vol.Required, CONF_OUTDOOR_TEMPERATURE_ENTITY, defaults): (
                _temperature_sensor()
            ),
            _marker(vol.Required, CONF_HEAT_SOURCE_CLIMATE_ENTITY, defaults): (
                selector.EntitySelector(selector.EntitySelectorConfig(domain="climate"))
            ),
            _marker(vol.Required, CONF_FLOW_TEMPERATURE_ENTITY, defaults): (
                _temperature_sensor()
            ),
            vol.Required(
                CONF_FLOW_THRESHOLD,
                default=defaults.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C),
            ): vol.Coerce(float),
            _optional(CONF_FLOW_THRESHOLD_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=_FLOW_THRESHOLD_DOMAINS)
            ),
            _optional(CONF_FLOW_CURVE_ENTITY, defaults): _temperature_sensor(),
            vol.Required(
                CONF_FROST_PROTECTION_TEMPERATURE,
                default=defaults.get(CONF_FROST_PROTECTION_TEMPERATURE, 8.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_FLOW_GAP_MIN,
                default=defaults.get(CONF_MAX_FLOW_GAP_MIN, DEFAULT_MAX_FLOW_GAP_MIN),
            ): _whole(5, 240, "min"),
            _optional(CONF_RATE_LIMIT_REMAINING_ENTITY, defaults): (
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            ),
            vol.Required(
                CONF_RATE_LIMIT_RESERVE,
                default=defaults.get(
                    CONF_RATE_LIMIT_RESERVE, DEFAULT_RATE_LIMIT_RESERVE
                ),
            ): _whole(0, 1000),
            vol.Required(
                CONF_MAX_WRITES_PER_DAY,
                default=defaults.get(
                    CONF_MAX_WRITES_PER_DAY, DEFAULT_MAX_WRITES_PER_DAY
                ),
            ): _whole(1, 1000),
        }
    )


def _whole(minimum: int, maximum: int, unit: str | None = None) -> vol.All:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return vol.All(selector.NumberSelector(config), vol.Coerce(int))


def _hub_control_schema(defaults: dict[str, Any]) -> vol.Schema:
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
            ): _kelvin(
                MIN_STATIONARY_RANGE_C, MAX_STATIONARY_RANGE_C, STATIONARY_RANGE_STEP_C
            ),
            vol.Required(
                CONF_TARGET_BAND,
                default=defaults.get(CONF_TARGET_BAND, DEFAULT_TARGET_BAND_C),
            ): _kelvin(MIN_TARGET_BAND_C, MAX_TARGET_BAND_C, TARGET_BAND_STEP_C),
            vol.Required(
                CONF_TRV_SETPOINT_MODE,
                default=defaults.get(CONF_TRV_SETPOINT_MODE, DEFAULT_TRV_SETPOINT_MODE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[mode.value for mode in TrvSetpointMode],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_TRV_SETPOINT_MODE,
                )
            ),
        }
    )


class HeatingControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """The hub: the house-wide settings, once per installation."""

    VERSION = HUB_VERSION

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._reconfiguring = False

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {
            SUBENTRY_TYPE_ROOM: RoomSubentryFlow,
            SUBENTRY_TYPE_FLOW: FlowSubentryFlow,
            SUBENTRY_TYPE_HOT_WATER: HotWaterSubentryFlow,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_sources(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._reconfiguring = True
        self._data = dict(self._get_reconfigure_entry().data)
        return await self.async_step_sources()

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = _hub_sources_schema(self._data)
        if user_input is not None:
            _merge(self._data, schema, user_input)
            return await self.async_step_control()
        return self.async_show_form(step_id="sources", data_schema=schema)

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = _hub_control_schema(self._data)
        if user_input is not None:
            _merge(self._data, schema, user_input)
            return await self.async_step_sun()
        return self.async_show_form(step_id="control", data_schema=schema)

    async def async_step_sun(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = _hub_sun_schema(self._data)
        if user_input is not None:
            _merge(self._data, schema, user_input)
            if self._reconfiguring:
                # The update listener reloads the hub and with it every room.
                return self.async_update_and_abort(
                    self._get_reconfigure_entry(), data=self._data
                )
            return self.async_create_entry(title=HUB_TITLE, data=self._data)
        return self.async_show_form(step_id="sun", data_schema=schema)


# --- Room --------------------------------------------------------------------


def _trv_step_schema(
    profiles: tuple[DeviceProfile, ...], defaults: dict[str, Any] | None = None
) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            _marker(vol.Required, CONF_TRV_DEVICE_ID, defaults): (
                selector.DeviceSelector(
                    selector.DeviceSelectorConfig(
                        filter=device_selector_filters(profiles)
                    )
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


def _room_signals_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            _optional(CONF_ROOM_SENSOR_ENTITY, defaults): _temperature_sensor(),
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
            vol.Required(
                CONF_USE_HOUSE_COMFORT_CONDITIONS,
                default=defaults.get(CONF_USE_HOUSE_COMFORT_CONDITIONS, True),
            ): bool,
            vol.Required(
                CONF_ROOM_COMFORT_SWITCH,
                default=defaults.get(CONF_ROOM_COMFORT_SWITCH, False),
            ): bool,
            _optional(CONF_LEARNING_BLOCK_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=_BOOLEAN_SIGNAL_DOMAINS)
            ),
        }
    )


def _room_settings_schema(defaults: dict[str, Any]) -> vol.Schema:
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
                CONF_PV_BOOST_ENABLED,
                default=defaults.get(CONF_PV_BOOST_ENABLED, False),
            ): bool,
            vol.Required(
                CONF_PV_BOOST_TEMPERATURE_OFFSET,
                default=defaults.get(CONF_PV_BOOST_TEMPERATURE_OFFSET, 1.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_LEAD_HEATING_LIMIT,
                default=defaults.get(CONF_LEAD_HEATING_LIMIT, False),
            ): bool,
            vol.Required(
                CONF_LEAD_FLOW, default=defaults.get(CONF_LEAD_FLOW, False)
            ): bool,
        }
    )


def _hub_sun_schema(defaults: dict[str, Any]) -> vol.Schema:
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
            _optional(CONF_WORKDAY_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=_BOOLEAN_SIGNAL_DOMAINS)
            ),
        }
    )


def _surface_selector(surfaces: list[str]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=surfaces,
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
            translation_key="surface",
        )
    )


def _room_design_schema(
    defaults: dict[str, Any], roof: RoofOrientation = RoofOrientation.NONE
) -> vol.Schema:
    roof_surfaces = {
        RoofOrientation.EAST_WEST: [Surface.ROOF_EAST, Surface.ROOF_WEST],
        RoofOrientation.NORTH_SOUTH: [Surface.ROOF_NORTH, Surface.ROOF_SOUTH],
        RoofOrientation.NONE: [],
    }[roof]
    facades = [surface.value for surface in FACADES]
    opaque = facades + [surface.value for surface in roof_surfaces]
    return vol.Schema(
        {
            vol.Optional(
                CONF_WINDOW_SURFACES,
                default=[
                    s for s in defaults.get(CONF_WINDOW_SURFACES, []) if s in facades
                ],
            ): _surface_selector(facades),
            vol.Optional(
                CONF_OPAQUE_SURFACES,
                default=[
                    s for s in defaults.get(CONF_OPAQUE_SURFACES, []) if s in opaque
                ],
            ): _surface_selector(opaque),
            vol.Required(
                CONF_DESIGN_INDOOR_TEMPERATURE,
                default=defaults.get(CONF_DESIGN_INDOOR_TEMPERATURE, 20.0),
            ): vol.Coerce(float),
            vol.Required(
                CONF_ROOM_HEAT_LOAD, default=defaults.get(CONF_ROOM_HEAT_LOAD, 1000.0)
            ): vol.Coerce(float),
            _marker(vol.Required, CONF_ROOM_AREA, defaults): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=200,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="m²",
                )
            ),
            _marker(vol.Required, CONF_CONSTRUCTION, defaults): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[c.value for c in Construction],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_CONSTRUCTION,
                )
            ),
        }
    )


class RoomSubentryFlow(ConfigSubentryFlow):
    """Add or change one room of the hub."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._existing_trvs: list[dict[str, Any]] = []
        self._trvs: list[dict[str, Any]] = []
        self._pending_trv: dict[str, Any] = {}
        self._trv_index = 0
        self._trv_count = 1
        self._reconfiguring = False

    def _trv_defaults(self) -> dict[str, Any]:
        if self._trv_index < len(self._existing_trvs):
            return self._existing_trvs[self._trv_index]
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            unique_id = slugify(user_input[CONF_ROOM_NAME])
            if any(
                subentry.unique_id == unique_id
                for subentry in self._get_entry().subentries.values()
            ):
                errors[CONF_ROOM_NAME] = "already_configured"
            else:
                self._data[CONF_ROOM_NAME] = user_input[CONF_ROOM_NAME]
                self._trv_count = user_input[CONF_TRV_COUNT]
                return await self.async_step_trv()

        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_NAME): str,
                vol.Required(CONF_TRV_COUNT, default=1): vol.In(TRV_COUNT_OPTIONS),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        # The name stays: the room's stores are keyed by it.
        self._reconfiguring = True
        self._data = dict(self._get_reconfigure_subentry().data)
        self._existing_trvs = list(self._data.get(CONF_TRVS, []))
        self._trv_count = len(self._existing_trvs)
        return await self.async_step_trv()

    async def async_step_trv(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        profiles = await async_get_profiles(self.hass)
        errors: dict[str, str] = {}
        defaults = self._trv_defaults()
        if user_input is not None:
            try:
                resolve_trv(self.hass, profiles, user_input)
            except TrvResolveError as err:
                errors[CONF_TRV_DEVICE_ID] = err.reason
            else:
                self._pending_trv = dict(user_input)
                return await self.async_step_trv_details()
            defaults = user_input
        return self.async_show_form(
            step_id="trv",
            data_schema=_trv_step_schema(profiles, defaults),
            errors=errors,
            description_placeholders={"index": str(self._trv_index + 1)},
        )

    async def async_step_trv_details(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        emitter_type = self._pending_trv[CONF_TRV_EMITTER_TYPE]

        if user_input is not None:
            self._pending_trv.update(user_input)
            self._trvs.append(self._pending_trv)
            self._pending_trv = {}
            self._trv_index += 1

            if self._trv_index < self._trv_count:
                return await self.async_step_trv()
            self._data[CONF_TRVS] = self._trvs
            return await self.async_step_signals()

        return self.async_show_form(
            step_id="trv_details",
            data_schema=_trv_details_schema(emitter_type, self._trv_defaults()),
            description_placeholders={"index": str(self._trv_index + 1)},
        )

    async def async_step_signals(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _room_signals_schema(self._data)
        if user_input is not None:
            _merge(self._data, schema, user_input)
            return await self.async_step_settings()
        return self.async_show_form(step_id="signals", data_schema=schema)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _room_settings_schema(self._data)
        if user_input is not None:
            _merge(self._data, schema, user_input)
            return await self.async_step_design()
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_design(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _room_design_schema(
            self._data,
            RoofOrientation(
                self._get_entry().data.get(CONF_ROOF_ORIENTATION, RoofOrientation.NONE)
            ),
        )
        if user_input is not None:
            _merge(self._data, schema, user_input)
            if self._reconfiguring:
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data=self._data,
                )
            return self.async_create_entry(
                title=self._data[CONF_ROOM_NAME],
                data=self._data,
                unique_id=slugify(self._data[CONF_ROOM_NAME]),
            )
        return self.async_show_form(step_id="design", data_schema=schema)


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
        except (KeyError, TypeError, ValueError):
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
            _marker(vol.Required, CONF_ACTUATOR_ENTITY, defaults): (
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
            _merge(self._data, schema, user_input)
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
                _merge(self._data, schema, user_input)
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


# --- Hot water ---------------------------------------------------------------


def _hw_number(key: str, defaults: dict[str, Any]) -> tuple[vol.Required, Any]:
    box = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=-10000, max=100000, step="any", mode=selector.NumberSelectorMode.BOX
        )
    )
    return vol.Required(key, default=defaults[key]), vol.All(box, vol.Coerce(float))


def _hw_time(key: str, defaults: dict[str, Any]) -> tuple[vol.Required, Any]:
    return vol.Required(key, default=defaults[key]), selector.TimeSelector()


def _hw_bool(key: str, defaults: dict[str, Any]) -> tuple[vol.Required, Any]:
    return vol.Required(key, default=defaults[key]), selector.BooleanSelector()


def _hw_entity(
    key: str, defaults: dict[str, Any], domain: str | list[str], required: bool = False
) -> tuple[vol.Marker, Any]:
    marker = (
        _marker(vol.Required, key, defaults) if required else _optional(key, defaults)
    )
    return marker, selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _hw_entities(
    key: str, defaults: dict[str, Any], domain: str | list[str]
) -> tuple[vol.Optional, Any]:
    return vol.Optional(key, default=defaults.get(key) or []), selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=True)
    )


def _schema(*fields: tuple[vol.Marker, Any]) -> vol.Schema:
    return vol.Schema(dict(fields))


def _hw_sources_schema(d: dict[str, Any]) -> vol.Schema:
    return _schema(
        _hw_entity("water_heater_entity", d, "water_heater", required=True),
        _hw_entity("tank_temperature_entity", d, "sensor"),
        _hw_entity("grid_export_entity", d, "sensor"),
        _hw_entity("battery_soc_entity", d, "sensor"),
        _hw_entity("battery_discharge_entity", d, "sensor"),
        _hw_bool("legionella_enabled", d),
        _hw_bool("circulation_enabled", d),
    )


def _hw_modes_schema(d: dict[str, Any]) -> vol.Schema:
    numbers = (
        "grid_lower_w",
        "grid_upper_w",
        "grid_release_w",
        "grid_stable_min",
        "battery_soc_min",
        "discharge_max_w",
        "discharge_stable_min",
        "outdoor_lower_c",
        "outdoor_upper_c",
        "tank_gate_c",
        "min_hold_min",
        "manual_hold_min",
        "boost_max_h",
        "reheat_hysteresis_k",
        "max_setpoint_c",
    )
    return _schema(
        *(_hw_number(key, d) for key in numbers),
        _hw_bool("night_enabled", d),
        _hw_time("night_start", d),
        _hw_time("night_end", d),
        _hw_bool("window_enabled", d),
        _hw_time("window_start", d),
        _hw_time("window_end", d),
        _hw_bool("forced_charge", d),
    )


def _hw_legionella_schema(d: dict[str, Any]) -> vol.Schema:
    return _schema(
        (
            vol.Required("legionella_weekdays", default=d["legionella_weekdays"]),
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(WEEKDAYS),
                    multiple=True,
                    translation_key="weekday",
                )
            ),
        ),
        _hw_time("legionella_start", d),
        (
            _optional("legionella_fallback", d),
            selector.TimeSelector(),
        ),
        _hw_number("legionella_retry_min", d),
        _hw_entity("forecast_entity", d, "sensor"),
        _hw_bool("forecast_above", d),
        _hw_number("forecast_threshold", d),
        _hw_entity("outlet_entity", d, "sensor"),
        _hw_number("outlet_threshold_c", d),
        _hw_entity("return_entity", d, "sensor"),
        _hw_number("return_threshold_c", d),
        _hw_number("legionella_hold_min", d),
        _hw_number("legionella_timeout_min", d),
    )


def _hw_circulation_schema(d: dict[str, Any]) -> vol.Schema:
    numbers = (
        "peak_start_below_c",
        "offpeak_start_below_c",
        "stop_delta_k",
        "stop_temperature_c",
        "max_run_min",
        "min_pause_min",
        "fallback_on_min",
        "fallback_off_min",
        "release_temperature_c",
        "release_min",
    )
    return _schema(
        _hw_entity("pump_entity", d, "switch", required=True),
        _hw_entity("circ_return_entity", d, "sensor", required=True),
        _hw_entity("circ_outlet_entity", d, "sensor"),
        _hw_entities("draw_entities", d, "binary_sensor"),
        _hw_entities("continuous_entities", d, ["input_boolean", "switch"]),
        (
            vol.Required("blocked_states", default=d["blocked_states"]),
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["off", "eco", "heat_pump", "performance"],
                    multiple=True,
                    custom_value=True,
                )
            ),
        ),
        _hw_entity("presence_entity", d, _BOOLEAN_SIGNAL_DOMAINS),
        _hw_entity("daymode_entity", d, _BOOLEAN_SIGNAL_DOMAINS),
        _hw_time("peak1_start", d),
        _hw_time("peak1_end", d),
        _hw_time("peak2_start", d),
        _hw_time("peak2_end", d),
        *(_hw_number(key, d) for key in numbers),
        _hw_bool("draw_overrides_gates", d),
        _hw_bool("release_enabled", d),
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
            _merge(self._data, schema, user_input)
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
        if self._data.get("legionella_enabled"):
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
        if self._data.get("circulation_enabled"):
            return await self.async_step_circulation()
        return self._finish()

    async def async_step_circulation(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _hw_circulation_schema(self._defaults())
        if user_input is not None:
            _merge(self._data, schema, user_input)
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

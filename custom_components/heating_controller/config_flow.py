from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_BOOST_ENABLED,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_BUILDING_ROTATION,
    CONF_COMFORT_CONDITION_ENTITIES,
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
    CONF_MAX_SENSOR_AGE,
    CONF_MPC_DEMAND_HYSTERESIS_PCT,
    CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
    CONF_MPC_HOLD_TIME,
    CONF_MPC_MAX_DEMAND_STEP_PCT,
    CONF_OPAQUE_SURFACES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_ENTITY,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_ROOM_AREA,
    CONF_ROOM_COMFORT_CONDITION_ENTITIES,
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
    DEFAULT_ROOF_TILT_DEG,
    DEFAULT_STATIONARY_RANGE_C,
    DEFAULT_TARGET_BAND_C,
    DEFAULT_TRV_SETPOINT_MODE,
    DOMAIN,
    FACADES,
    HUB_TITLE,
    MAX_STATIONARY_RANGE_C,
    MAX_TARGET_BAND_C,
    MAX_TRV_COUNT,
    MIN_STATIONARY_RANGE_C,
    MIN_TARGET_BAND_C,
    STATIONARY_RANGE_STEP_C,
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
            _marker(vol.Required, CONF_PV_BOOST_ENTITY, defaults): (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=_BOOLEAN_SIGNAL_DOMAINS)
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
            vol.Required(
                CONF_FROST_PROTECTION_TEMPERATURE,
                default=defaults.get(CONF_FROST_PROTECTION_TEMPERATURE, 8.0),
            ): vol.Coerce(float),
        }
    )


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
        return {SUBENTRY_TYPE_ROOM: RoomSubentryFlow}

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
            vol.Optional(
                CONF_ROOM_COMFORT_CONDITION_ENTITIES,
                default=defaults.get(CONF_ROOM_COMFORT_CONDITION_ENTITIES, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=_BOOLEAN_SIGNAL_DOMAINS + ["schedule"], multiple=True
                )
            ),
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

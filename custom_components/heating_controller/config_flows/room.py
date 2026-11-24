"""The room subentry: its TRVs, signals, settings and design."""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers import selector
from homeassistant.util import slugify

from ..const import (
    CONF_BOOST_ENABLED,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_CONSTRUCTION,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_LEAD_FLOW,
    CONF_LEAD_HEATING_LIMIT,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_OPAQUE_SURFACES,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOF_ORIENTATION,
    CONF_ROOM_AREA,
    CONF_ROOM_COMFORT_SWITCH,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_TRV_COUNT,
    CONF_TRV_DEVICE_ID,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_WIDTH_MM,
    CONF_TRVS,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_WINDOW_SURFACES,
    FACADES,
    MAX_TRV_COUNT,
    Construction,
    HeatEmitterType,
    PanelRadiatorType,
    RoofOrientation,
    Surface,
)
from ..profiles import (
    DeviceProfile,
    TrvResolveError,
    async_get_profiles,
    device_selector_filters,
    resolve_trv,
)
from .fields import (
    BOOLEAN_SIGNAL_DOMAINS,
    marker,
    merge,
    optional,
    temperature_sensor,
)

TRV_COUNT_OPTIONS = list(range(1, MAX_TRV_COUNT + 1))
_WINDOW_DEVICE_CLASSES = ["window", "door", "garage_door", "opening"]


# --- Room --------------------------------------------------------------------


def _trv_step_schema(
    profiles: tuple[DeviceProfile, ...], defaults: dict[str, Any] | None = None
) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            marker(vol.Required, CONF_TRV_DEVICE_ID, defaults): (
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
            optional(CONF_ROOM_SENSOR_ENTITY, defaults): temperature_sensor(),
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
            optional(CONF_LEARNING_BLOCK_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=BOOLEAN_SIGNAL_DOMAINS)
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
            marker(vol.Required, CONF_ROOM_AREA, defaults): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=200,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="m²",
                )
            ),
            marker(vol.Required, CONF_CONSTRUCTION, defaults): selector.SelectSelector(
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
            merge(self._data, schema, user_input)
            return await self.async_step_settings()
        return self.async_show_form(step_id="signals", data_schema=schema)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        schema = _room_settings_schema(self._data)
        if user_input is not None:
            merge(self._data, schema, user_input)
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
            merge(self._data, schema, user_input)
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

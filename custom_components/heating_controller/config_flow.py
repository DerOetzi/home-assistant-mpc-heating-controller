from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
)
from homeassistant.core import callback

from .config_flows.fields import merge
from .config_flows.flow import FlowSubentryFlow
from .config_flows.hot_water import HotWaterSubentryFlow
from .config_flows.hub import hub_control_schema, hub_sources_schema, hub_sun_schema
from .config_flows.room import RoomSubentryFlow
from .const import (
    DOMAIN,
    HUB_MINOR_VERSION,
    HUB_TITLE,
    HUB_VERSION,
    SUBENTRY_TYPE_FLOW,
    SUBENTRY_TYPE_HOT_WATER,
    SUBENTRY_TYPE_ROOM,
)


class HeatingControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """The hub: the house-wide settings, once per installation."""

    VERSION = HUB_VERSION
    MINOR_VERSION = HUB_MINOR_VERSION

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
        schema = hub_sources_schema(self._data)
        if user_input is not None:
            merge(self._data, schema, user_input)
            return await self.async_step_control()
        return self.async_show_form(step_id="sources", data_schema=schema)

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = hub_control_schema(self._data)
        if user_input is not None:
            merge(self._data, schema, user_input)
            return await self.async_step_sun()
        return self.async_show_form(step_id="control", data_schema=schema)

    async def async_step_sun(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = hub_sun_schema(self._data)
        if user_input is not None:
            merge(self._data, schema, user_input)
            if self._reconfiguring:
                # The update listener reloads the hub and with it every room.
                return self.async_update_and_abort(
                    self._get_reconfigure_entry(), data=self._data
                )
            return self.async_create_entry(title=HUB_TITLE, data=self._data)
        return self.async_show_form(step_id="sun", data_schema=schema)

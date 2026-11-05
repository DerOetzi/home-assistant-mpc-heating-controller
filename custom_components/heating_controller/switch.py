from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import FlowEntity, async_add_flow_entities
from .flow_device import FlowDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_flow_entities(
        entry, async_add_entities, lambda flow: [FlowWriteSwitch(flow)]
    )


class FlowWriteSwitch(FlowEntity, SwitchEntity, RestoreEntity):
    """Off: the flow device computes and shows, but writes nothing (shadow)."""

    _attr_translation_key = "flow_write"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:pencil-lock-outline"

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_write")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._flow.set_write_enabled(last is not None and last.state == STATE_ON)

    @property
    def is_on(self) -> bool:
        return self._flow.write_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._flow.set_write_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._flow.set_write_enabled(False)

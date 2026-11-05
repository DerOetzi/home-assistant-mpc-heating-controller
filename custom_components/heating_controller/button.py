from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HeatingRoomCoordinator
from .entity import (
    HeatingControllerEntity,
    HotWaterEntity,
    async_add_hot_water_entities,
    async_add_room_entities,
)
from .hot_water_device import HotWaterDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: [HeatingUnblockButton(coordinator)],
    )
    async_add_hot_water_entities(
        entry,
        async_add_entities,
        lambda hot_water: (
            [LegionellaStartButton(hot_water)] if hot_water.legionella_enabled else []
        ),
    )


class LegionellaStartButton(HotWaterEntity, ButtonEntity):
    """Start a legionella cycle now, regardless of schedule and forecast."""

    _attr_translation_key = "legionella_start"
    _attr_icon = "mdi:bacteria-outline"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "legionella_start")

    async def async_press(self) -> None:
        self._hot_water.start_legionella()


class HeatingUnblockButton(HeatingControllerEntity, ButtonEntity):
    _attr_translation_key = "heating_unblock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "heating_unblock")

    async def async_press(self) -> None:
        await self._coordinator.async_unblock()

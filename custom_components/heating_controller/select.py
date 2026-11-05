from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BOOST_ENABLED, HeatMode
from .control.hot_water import ManualMode
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
        lambda coordinator: [HeatingModeSelect(coordinator)],
    )
    async_add_hot_water_entities(
        entry, async_add_entities, lambda hot_water: [HotWaterModeSelect(hot_water)]
    )


class HotWaterModeSelect(HotWaterEntity, SelectEntity):
    """Automatic, or a mode chosen by hand for the manual hold time."""

    _attr_translation_key = "hot_water_mode"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "hot_water_mode")
        self._attr_options = [mode.value for mode in ManualMode]

    @property
    def current_option(self) -> str:
        return self._hot_water.controller.manual.value

    async def async_select_option(self, option: str) -> None:
        self._hot_water.set_manual(ManualMode(option))
        self.async_write_ha_state()


class HeatingModeSelect(HeatingControllerEntity, SelectEntity):
    _attr_translation_key = "heating_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "heating_mode")
        options = [HeatMode.COMFORT.value, HeatMode.ECO.value]
        if coordinator.data.get(CONF_BOOST_ENABLED):
            options.append(HeatMode.BOOST.value)
        options.append(HeatMode.FROST_PROTECTION.value)
        self._attr_options = options

    @property
    def current_option(self) -> str:
        return self._coordinator.current_heat_mode.value

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_manual_heat_mode(HeatMode(option))

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BOOST_ENABLED, HeatMode
from .coordinator import HeatingRoomCoordinator
from .entity import HeatingControllerEntity, async_add_room_entities


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

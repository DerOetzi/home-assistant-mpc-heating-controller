from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import HeatingRoomCoordinator
from .entity import (
    FlowEntity,
    HeatingControllerEntity,
    HeatingHubEntity,
    HotWaterEntity,
    async_add_flow_entities,
    async_add_hot_water_entities,
    async_add_room_entities,
)
from .flow_device import FlowDevice
from .hot_water_device import HotWaterDevice
from .hub import HeatingHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub: HeatingHub = entry.runtime_data
    async_add_entities(
        [HouseComfortSwitch(hub), PvBoostSwitch(hub), HeatingSeasonAutoSwitch(hub)]
    )
    _remove_dropped_room_comfort_switches(hass, hub)
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: (
            [RoomComfortSwitch(coordinator)]
            if coordinator.has_room_comfort_switch
            else []
        ),
    )
    async_add_flow_entities(
        entry, async_add_entities, lambda flow: [FlowWriteSwitch(flow)]
    )
    async_add_hot_water_entities(
        entry,
        async_add_entities,
        lambda hot_water: (
            [HotWaterBoostSwitch(hot_water)]
            + (
                [
                    CirculationMainSwitch(hot_water),
                    CirculationContinuousSwitch(hot_water),
                ]
                if hot_water.circulation_enabled
                else []
            )
        ),
    )


def _remove_dropped_room_comfort_switches(hass: HomeAssistant, hub: HeatingHub) -> None:
    """Drop the switch of a room whose own comfort switch was deselected."""
    registry = er.async_get(hass)
    for coordinator in hub.rooms.values():
        if coordinator.has_room_comfort_switch:
            continue
        entity_id = registry.async_get_entity_id(
            "switch", DOMAIN, f"{coordinator.room_id}_comfort"
        )
        if entity_id:
            registry.async_remove(entity_id)


class _ToggleSwitch(SwitchEntity):
    """A switch that hands on and off to one method."""

    async def _async_apply(self, on: bool) -> None:
        raise NotImplementedError

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_apply(False)


class _RestoredToggleSwitch(_ToggleSwitch, RestoreEntity):
    """A toggle switch that takes its last state back after a restart."""

    # The state without a saved one.
    _restore_default = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        await self._async_apply(
            self._restore_default if last is None else last.state == STATE_ON
        )


class HouseComfortSwitch(HeatingHubEntity, _RestoredToggleSwitch):
    """House-wide comfort; off (e.g. on holiday) every following room is eco."""

    _attr_translation_key = "house_comfort"
    _attr_icon = "mdi:sofa"
    _restore_default = True

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "comfort")

    @property
    def is_on(self) -> bool:
        return self._hub.comfort_on

    async def _async_apply(self, on: bool) -> None:
        await self._hub.async_set_comfort(on)


class PvBoostSwitch(HeatingHubEntity, _RestoredToggleSwitch):
    """PV surplus: rooms with PV boost raise their setpoint.

    Switched by hand or by an automation.
    """

    _attr_translation_key = "pv_boost"
    _attr_icon = "mdi:solar-power"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "pv_boost")

    @property
    def is_on(self) -> bool:
        return self._hub.pv_boost_on

    async def _async_apply(self, on: bool) -> None:
        await self._hub.async_set_pv_boost(on)


class HeatingSeasonAutoSwitch(HeatingHubEntity, _RestoredToggleSwitch):
    """The hub switches the heat source heat/off by the season criterion.

    A switch of the heat source by hand turns this off.
    """

    _attr_translation_key = "heating_season_auto"
    _attr_icon = "mdi:thermostat-auto"
    _restore_default = True

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_season_auto")

    @property
    def is_on(self) -> bool:
        return self._hub.season_auto_on

    async def _async_apply(self, on: bool) -> None:
        await self._hub.async_set_season_auto(on)


class RoomComfortSwitch(HeatingControllerEntity, _RestoredToggleSwitch):
    """The room's own comfort release (e.g. a guest in the guest room)."""

    _attr_translation_key = "room_comfort"
    _attr_icon = "mdi:sofa-single-outline"

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "comfort")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.state.room_comfort)

    async def _async_apply(self, on: bool) -> None:
        await self._coordinator.async_set_room_comfort(on)


class HotWaterBoostSwitch(HotWaterEntity, _ToggleSwitch):
    """Boost target until switched off or the maximum duration is over.

    Not restored: a boost never survives a restart.
    """

    _attr_translation_key = "hot_water_boost"
    _attr_icon = "mdi:rocket-launch-outline"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "hot_water_boost")

    @property
    def is_on(self) -> bool:
        return self._hot_water.boost_on

    async def _async_apply(self, on: bool) -> None:
        self._hot_water.set_boost(on)
        self.async_write_ha_state()


class CirculationMainSwitch(HotWaterEntity, _ToggleSwitch):
    """Off: pump stays off, overriding continuous runs and legionella."""

    _attr_translation_key = "circulation_main"
    _attr_icon = "mdi:power"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "circulation_main")

    @property
    def is_on(self) -> bool:
        return self._hot_water.circulation_main

    async def _async_apply(self, on: bool) -> None:
        self._hot_water.set_circulation_main(on)
        self.async_write_ha_state()


class CirculationContinuousSwitch(HotWaterEntity, _ToggleSwitch):
    """Continuous circulation run; released once the loop held hot."""

    _attr_translation_key = "circulation_continuous"
    _attr_icon = "mdi:pump"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "circulation_continuous")

    @property
    def is_on(self) -> bool:
        return self._hot_water.continuous_request

    async def _async_apply(self, on: bool) -> None:
        self._hot_water.set_continuous_request(on)
        self.async_write_ha_state()


class FlowWriteSwitch(FlowEntity, _RestoredToggleSwitch):
    """Off: the flow device computes and shows, but writes nothing (shadow)."""

    _attr_translation_key = "flow_write"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:pencil-lock-outline"

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_write")

    @property
    def is_on(self) -> bool:
        return self._flow.write_enabled

    async def _async_apply(self, on: bool) -> None:
        self._flow.set_write_enabled(on)

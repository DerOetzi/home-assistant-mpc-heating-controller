from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_WINDOW_CONTACT_ENTITIES
from .coordinator import HeatingRoomCoordinator
from .entity import (
    HeatingControllerEntity,
    HeatingHubEntity,
    HotWaterEntity,
    async_add_hot_water_entities,
    async_add_room_entities,
)
from .hot_water_device import HotWaterDevice
from .hub import HeatingHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub: HeatingHub = entry.runtime_data
    async_add_entities(
        [
            LeadHeatingDemandBinarySensor(hub),
            HeatSourceReachableBinarySensor(hub),
            HeatingSeasonSwitchOnBinarySensor(hub),
            HeatingSeasonSwitchOffBinarySensor(hub),
        ]
    )
    async_add_hot_water_entities(
        entry,
        async_add_entities,
        lambda hot_water: (
            [HotWaterChargingBinarySensor(hot_water)]
            + (
                [LegionellaFailedBinarySensor(hot_water)]
                if hot_water.legionella_enabled
                else []
            )
        ),
    )
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: [
            HeatingAutomationBinarySensor(coordinator),
            RadiatorNotHeatingBinarySensor(coordinator),
            OutdoorTemperatureMissingBinarySensor(coordinator),
        ],
    )


class LeadHeatingDemandBinarySensor(HeatingHubEntity, BinarySensorEntity):
    """On while the lead rooms need at least the heat pump's lowest flow."""

    _attr_translation_key = "lead_heating_demand"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "lead_heating_demand")

    @property
    def available(self) -> bool:
        return self._hub.heating_demand is not None

    @property
    def is_on(self) -> bool | None:
        return self._hub.heating_demand

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._hub.lead_hold_flow
        return {
            "hold_flow_temperature_c": lead.value_c,
            "flow_threshold_c": self._hub.flow_threshold_c,
            "rooms": lead.rooms,
        }


class HotWaterChargingBinarySensor(HotWaterEntity, BinarySensorEntity):
    """On while a hot water charge runs (tank rising or just announced)."""

    _attr_translation_key = "hot_water_charging"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "hot_water_charging")

    @property
    def is_on(self) -> bool:
        return self._hot_water.charging

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        started = self._hot_water.detector.started_ts
        return {
            "started": None
            if started is None
            else dt_util.as_local(dt_util.utc_from_timestamp(started)).isoformat()
        }


class LegionellaFailedBinarySensor(HotWaterEntity, BinarySensorEntity):
    """On after a failed legionella cycle until the next successful one."""

    _attr_translation_key = "legionella_failed"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "legionella_failed")

    @property
    def is_on(self) -> bool:
        return self._hot_water.legionella.failed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        failure = self._hot_water.legionella.cycle.failure
        return {"failure": None if failure is None else failure.value}


class HeatSourceReachableBinarySensor(HeatingHubEntity, BinarySensorEntity):
    """Off once the measured flow has been missing longer than the hub's gap."""

    _attr_translation_key = "heat_source_reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heat_source_reachable")

    @property
    def is_on(self) -> bool:
        return self._hub.heat_source_reachable

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_flow_value": dt_util.as_local(
                dt_util.utc_from_timestamp(self._hub.flow_seen_ts)
            ).isoformat()
        }


class _HeatingSeasonBinarySensor(HeatingHubEntity, BinarySensorEntity):
    _attr_icon = "mdi:radiator"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_ts = self._hub.season.last_demand_ts
        return {
            "demand_hours_24h": self._hub.demand_hours_24h,
            "on_threshold_h": self._hub.season_on_threshold_h,
            "hold_threshold_h": self._hub.season_hold_threshold_h,
            "off_after_h": self._hub.season_off_after_h,
            "last_demand": None
            if last_ts is None
            else dt_util.as_local(dt_util.utc_from_timestamp(last_ts)).isoformat(),
            "heating_on": self._hub.heating_on,
        }


class HeatingSeasonSwitchOnBinarySensor(_HeatingSeasonBinarySensor):
    """On while the lead rooms ask for heat long enough and heating is off."""

    _attr_translation_key = "heating_season_switch_on"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_season_switch_on")

    @property
    def is_on(self) -> bool:
        return self._hub.season_switch_on_recommended


class HeatingSeasonSwitchOffBinarySensor(_HeatingSeasonBinarySensor):
    """On while heating although the lead rooms have not asked for long."""

    _attr_translation_key = "heating_season_switch_off"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_season_switch_off")

    @property
    def is_on(self) -> bool:
        return self._hub.season_switch_off_recommended


class HeatingAutomationBinarySensor(HeatingControllerEntity, BinarySensorEntity):
    _attr_translation_key = "heating_automation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "heating_automation")

    @property
    def is_on(self) -> bool:
        return self._coordinator.is_automation_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "window_contact_entities": list(
                self._coordinator.data.get(CONF_WINDOW_CONTACT_ENTITIES, [])
            ),
            "comfort_condition_entities": list(
                self._coordinator.room_comfort_condition_entities
            ),
        }


class RadiatorNotHeatingBinarySensor(HeatingControllerEntity, BinarySensorEntity):
    """On while a radiator stays cold although its valve reports open.

    Air in the radiator or a valve stuck shut: worth a look at the radiator.
    The room does not learn meanwhile.
    """

    _attr_translation_key = "radiator_not_heating"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "radiator_not_heating")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.radiators_not_heating)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"trv_entities": self._coordinator.radiators_not_heating}


class OutdoorTemperatureMissingBinarySensor(
    HeatingControllerEntity, BinarySensorEntity
):
    """On while the room runs without an outdoor temperature.

    The TRVs then hold the target of the active mode as an absolute setpoint
    on their own reading, and the room does not learn. Off again once the
    outdoor temperature is back and the MPC has taken over.
    """

    _attr_translation_key = "outdoor_temperature_missing"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "outdoor_temperature_missing")

    @property
    def is_on(self) -> bool:
        return self._coordinator.outdoor_fallback_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        since_ts = self._coordinator.outdoor_fallback_since_ts
        return {
            "since": (
                dt_util.as_local(dt_util.utc_from_timestamp(since_ts)).isoformat()
                if since_ts is not None
                else None
            ),
            "fallback_target_temperature_c": (
                self._coordinator.outdoor_fallback_target_c
            ),
        }

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
from .entity import HeatingControllerEntity, HeatingHubEntity, async_add_room_entities
from .hub import HeatingHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([LeadHeatingDemandBinarySensor(entry.runtime_data)])
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


class OutdoorTemperatureMissingBinarySensor(HeatingControllerEntity, BinarySensorEntity):
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

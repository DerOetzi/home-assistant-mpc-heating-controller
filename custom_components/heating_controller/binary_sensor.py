from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_WINDOW_CONTACT_ENTITIES, DOMAIN
from .coordinator import HeatingRoomCoordinator
from .entity import HeatingControllerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HeatingRoomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HeatingAutomationBinarySensor(coordinator),
            RadiatorNotHeatingBinarySensor(coordinator),
            OutdoorTemperatureMissingBinarySensor(coordinator),
        ]
    )


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

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LearningStatus
from .coordinator import HeatingRoomCoordinator
from .entity import HeatingControllerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: HeatingRoomCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            MinFlowTemperatureSensor(coordinator),
            HoldFlowTemperatureSensor(coordinator),
            GatedHoldFlowTemperatureSensor(coordinator),
            HeatingDemandSensor(coordinator),
            RequestedHeatingPowerSensor(coordinator),
            AvailableHeatingPowerSensor(coordinator),
            RoomTemperatureSensor(coordinator),
            BaseTemperatureSensor(coordinator),
            MpcLearningStatusSensor(coordinator),
            UaFactorSensor(coordinator),
            CapacityFactorSensor(coordinator),
        ]
    )


class _DiagnosticSensor(HeatingControllerEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class MinFlowTemperatureSensor(_DiagnosticSensor):
    _attr_translation_key = "min_flow_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "min_flow_temperature")

    @property
    def native_value(self) -> float | None:
        return self._coordinator.normal_min_flow_temperature_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "current_flow_temperature_c": self._coordinator.current_flow_temperature_c,
            "supply_status": self._coordinator.flow_supply_status.value,
            "calculation_heat_mode": (
                self._coordinator.normal_heat_mode.value
                if self._coordinator.normal_heat_mode is not None
                else None
            ),
            "calculation_target_temperature_c": (
                self._coordinator.normal_target_temperature_c
            ),
            "flow_gate_closed": self._coordinator.normal_flow_gate_closed,
            "hold_flow_temperature_c": (
                self._coordinator.normal_hold_flow_temperature_c
            ),
            "recovery_flow_temperature_c": (
                self._coordinator.normal_recovery_flow_temperature_c
            ),
            "recovery_flow_saturated": (
                self._coordinator.normal_recovery_flow_saturated
            ),
        }


class HoldFlowTemperatureSensor(_DiagnosticSensor):
    """The hold requirement before the surplus gate.

    Same number the minimum-flow sensor used to report unconditionally: what
    this room would need to hold its setpoint at the current outdoor
    temperature, regardless of how much stored heat it is currently sitting
    on. Carries a state class so it keeps accruing long-term statistics
    through the shoulder season, when the gated sensor reads zero.
    """

    _attr_translation_key = "hold_flow_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "hold_flow_temperature")

    @property
    def native_value(self) -> float | None:
        return self._coordinator.normal_hold_flow_temperature_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.normal_result
        return {
            "flow_gate_closed": self._coordinator.normal_flow_gate_closed,
            "gated_min_flow_temperature_c": (
                self._coordinator.normal_min_flow_temperature_c
            ),
            "calculation_target_temperature_c": (
                self._coordinator.normal_target_temperature_c
            ),
            "room_surplus_c": (
                round(result.input.room_temp_c - result.input.target_temp_c, 2)
                if result
                else None
            ),
        }


class GatedHoldFlowTemperatureSensor(_DiagnosticSensor):
    """The hold requirement after the surplus gate, without the recovery part.

    Reads the weather-driven hold flow while the room needs heat to keep its
    setpoint and 0 while it coasts on stored heat. Unlike the minimum-flow
    sensor it ignores how far the room is below setpoint, so a room briefly
    cooled by airing does not register as a heating-season demand.
    """

    _attr_translation_key = "gated_hold_flow_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "gated_hold_flow_temperature")

    @property
    def native_value(self) -> float | None:
        return self._coordinator.normal_gated_hold_flow_temperature_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "flow_gate_closed": self._coordinator.normal_flow_gate_closed,
            "hold_flow_temperature_c": (
                self._coordinator.normal_hold_flow_temperature_c
            ),
            "calculation_target_temperature_c": (
                self._coordinator.normal_target_temperature_c
            ),
        }


class HeatingDemandSensor(_DiagnosticSensor):
    _attr_translation_key = "heating_demand"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "heating_demand")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        return result.demand_pct if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.last_result
        if result is None:
            return {}
        return {
            "trv_target_temps": dict(
                zip(self._coordinator.trv_entity_ids, result.trv_targets, strict=True)
            )
        }


class RequestedHeatingPowerSensor(_DiagnosticSensor):
    _attr_translation_key = "requested_heating_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "requested_heating_power")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        return result.requested_heating_power_w if result else None


class AvailableHeatingPowerSensor(_DiagnosticSensor):
    _attr_translation_key = "available_heating_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "available_heating_power")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        return result.available_heating_power_w if result else None


class RoomTemperatureSensor(_DiagnosticSensor):
    _attr_translation_key = "room_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "room_temperature")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.mpc.get_room_temperature_result()
        return result.temperature_c if result.valid else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.mpc.get_room_temperature_result()
        if not result.valid:
            return {}
        return {
            "used_strategy": result.used_strategy,
            "room_sensor_temp_c": result.room_sensor_temp_c,
            "trv_temperatures": dict(
                zip(
                    self._coordinator.trv_entity_ids,
                    result.trv_temperatures,
                    strict=True,
                )
            ),
        }


class BaseTemperatureSensor(_DiagnosticSensor):
    _attr_translation_key = "base_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "base_temperature")

    @property
    def native_value(self) -> float:
        return self._coordinator.base_temperature_c


class MpcLearningStatusSensor(_DiagnosticSensor):
    _attr_translation_key = "mpc_learning_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [status.value for status in LearningStatus]

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "mpc_learning_status")

    @property
    def native_value(self) -> str:
        return self._coordinator.mpc.get_learning_state().status.value


class UaFactorSensor(_DiagnosticSensor):
    _attr_translation_key = "ua_factor"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "ua_factor")

    @property
    def native_value(self) -> float:
        return self._coordinator.mpc.learned_ua_factor


class CapacityFactorSensor(_DiagnosticSensor):
    _attr_translation_key = "capacity_factor"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "capacity_factor")

    @property
    def native_value(self) -> float:
        return self._coordinator.mpc.learned_capacity_factor

"""Diagnostic sensors of a room."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)

from ..const import (
    CapacityOutcome,
    CurveEnd,
    RoomSupply,
    UaBalanceOutcome,
)
from ..controller.mpc.learner import UA_BALANCE_MIN_WINDOW_S
from ..controller.mpc.results import RoomMpcError
from ..coordinator import HeatingRoomCoordinator
from ..entity import (
    HeatingControllerEntity,
)
from ..hub import HeatingHub
from .format import iso, rounded


class _DiagnosticSensor(HeatingControllerEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class SupplySensor(_DiagnosticSensor):
    """How the flow supplies the room, read from its valves.

    Same judgement as the flow feedback: valves near fully open with the
    room short of its target are undersupplied, open enough to hold the flow
    supplied, further closed oversupplied.
    """

    _attr_translation_key = "supply"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "supply")
        self._attr_options = [supply.value for supply in RoomSupply]

    @property
    def native_value(self) -> str:
        return self._coordinator.supply[0].value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _, reason, sample = self._coordinator.supply
        return {
            "reason": reason.value if reason is not None else None,
            "demand_pct": sample.demand_pct if sample is not None else None,
            "deficit_c": sample.deficit_c if sample is not None else None,
            "current_flow_temperature_c": self._coordinator.current_flow_temperature_c,
        }


class SupplyCountSensor(_DiagnosticSensor):
    """The flow feedback's count kept on this flow lead room alone, minutes.

    Comparison only, no effect on the flow: at -30 the room alone would
    have stepped up (would_step_up counts those), at +60 it agrees to a
    step down.
    """

    _attr_translation_key = "supply_count"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: HeatingRoomCoordinator, hub: HeatingHub) -> None:
        super().__init__(coordinator, "supply_count")
        self._hub = hub

    async def async_added_to_hass(self) -> None:
        """Also follow the flow device: it moves the count, not the room."""
        await super().async_added_to_hass()
        if self._hub.flow is not None:
            self.async_on_remove(self._hub.flow.async_add_listener(self._handle_update))

    @property
    def native_value(self) -> int | None:
        count = self._hub.flow_feedback.room_count_s(self._coordinator.room_name)
        return None if count is None else round(count / 60)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "would_step_up": self._hub.flow_feedback.room_steps_up.get(
                self._coordinator.room_name, 0
            )
        }


class BoostFlowTemperatureSensor(_DiagnosticSensor):
    """The recovery flow while the room recovers after a setpoint rise; else 0."""

    _attr_translation_key = "boost_flow_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "boost_flow_temperature")

    @property
    def native_value(self) -> float | None:
        if not self._coordinator.flow_boost_active:
            return 0.0
        return self._coordinator.flow_boost_temperature_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.normal_result
        return {
            "boost_active": self._coordinator.flow_boost_active,
            "recovery_flow_temperature_c": (
                result.recovery_flow_temperature_c if result else None
            ),
            "recovery_flow_saturated": (
                result.recovery_flow_saturated if result else None
            ),
        }


class HoldFlowTemperatureSensor(_DiagnosticSensor):
    """The hold requirement before the surplus gate.

    What this room would need to hold its setpoint at the current outdoor
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
            "flow_gate_closed": result.flow_gate_closed if result else None,
            "calculation_heat_mode": (
                self._coordinator.normal_heat_mode.value
                if self._coordinator.normal_heat_mode is not None
                else None
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
    """The hold requirement after the surplus gate.

    Reads the weather-driven hold flow while the room needs heat to keep its
    setpoint and 0 while it coasts on stored heat. It ignores how far the
    room is below setpoint, so a room briefly cooled by airing does not
    register as a heating-season demand.
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
        result = self._coordinator.normal_result
        return {
            "flow_gate_closed": result.flow_gate_closed if result else None,
            "hold_flow_temperature_c": (
                self._coordinator.normal_hold_flow_temperature_c
            ),
            "calculation_target_temperature_c": (
                self._coordinator.normal_target_temperature_c
            ),
        }


class CurveHoldFlowTemperatureSensor(_DiagnosticSensor):
    """Hold flow at one end of the heating curve, at comfort, learned UA.

    The room's own point for setting the heat source's curve; the flow
    device's recommendation takes the highest of the flow lead rooms.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: HeatingRoomCoordinator, end: CurveEnd) -> None:
        super().__init__(coordinator, f"hold_flow_temperature_curve_{end}")
        self._end = end
        self._attr_translation_key = f"hold_flow_temperature_curve_{end}"

    @property
    def available(self) -> bool:
        return self._coordinator.curve_outdoor_c(self._end) is not None

    @property
    def native_value(self) -> float | None:
        return self._coordinator.hold_flow_at_curve_end_c(self._end)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._coordinator.mpc.learning.get_ua_balance_info()
        return {
            "outdoor_temperature_c": self._coordinator.curve_outdoor_c(self._end),
            "target_temperature_c": self._coordinator.comfort_temperature_c,
            "ua_factor": rounded(self._coordinator.mpc.learned_ua_factor, 4),
            "ua_last_learned": iso(info.last_learned_ts),
        }


class HeatingDemandSensor(_DiagnosticSensor):
    _attr_translation_key = "heating_demand"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    # The TRVs record their own targets and valve positions.
    _unrecorded_attributes = frozenset({"trv_target_temps", "trv_valve_positions"})

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "heating_demand")

    @property
    def native_value(self) -> float | None:
        return self._coordinator.room_demand_pct

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "trv_target_temps": self._coordinator.trv_target_temperatures,
            "trv_setpoint_writes": dict(self._coordinator.trv_writer.setpoint_writes),
            "trv_external_writes": dict(self._coordinator.trv_writer.external_writes),
            "load_room_mean": self._coordinator.load_sharing.room_mean,
            "trv_valve_positions": self._coordinator.trv_valve_positions,
        }


class AdditionalHeatSensor(_DiagnosticSensor):
    """Heat the radiators do not supply: steady gain, day profile and sun.

    The state counts only determined terms -- what the model may use; the
    attribute ungated_w adds the ones still being learned.
    """

    _attr_translation_key = "additional_heat"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    # The learned profile and sun terms live in the gain store; recorded with
    # every change of the current gain they would be copied each time.
    _unrecorded_attributes = frozenset(
        {
            "gain_profile_workday_w",
            "gain_profile_other_days_w",
            "profile_determined",
            "profile_days",
            "sun_coefficients_w",
            "sun_determined",
        }
    )

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "additional_heat")

    @property
    def native_value(self) -> float:
        return round(self._coordinator.current_gain().total_w, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        gains = self._coordinator.mpc.gain_model
        now = self._coordinator.current_gain()
        profile = gains.profile_w()
        blocks = len(profile) // 2
        return {
            "steady_gain_w": round(now.steady_w, 1),
            "profile_now_w": round(now.profile_w, 1),
            "sun_now_w": round(now.sun_w, 1),
            "ungated_w": round(now.ungated_w, 1),
            "gain_profile_workday_w": [round(v) for v in profile[:blocks]],
            "gain_profile_other_days_w": [round(v) for v in profile[blocks:]],
            "profile_determined": [
                gains.profile_determined(index) for index in range(len(profile))
            ],
            "profile_days": [
                gains.profile_days(index) for index in range(len(profile))
            ],
            "sun_coefficients_w": {
                term: round(value, 1)
                for term, value in gains.sun_coefficients_w().items()
            },
            "sun_determined": {
                term: gains.sun_determined(index)
                for index, term in enumerate(gains.sun_terms)
            },
            "hour_samples": gains.hour_samples,
            "residual_rms_w": rounded(gains.residual_rms_w, 1),
            "last_hour": iso(gains.last_hour_ts),
            "last_hour_outcome": (
                gains.last_hour_outcome.value if gains.last_hour_outcome else None
            ),
        }


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


class RadiatorHeatingPowerSensor(_DiagnosticSensor):
    """What the observer estimates the radiators give off right now.

    Follows the requested power with the radiator lag: still positive for a
    while after the valve closed, behind the request after it opened.
    """

    _attr_translation_key = "radiator_heating_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "radiator_heating_power")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is None or result.radiator_power_w is None:
            return None
        return round(result.radiator_power_w, 1)


class MeasuredHeatingPowerSensor(_DiagnosticSensor):
    """Heat at the valve openings the TRVs report, before the radiator lag.

    Where a TRV reports no opening its radiator counts with the demand, like
    the planning does.
    """

    _attr_translation_key = "measured_heating_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "measured_heating_power")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.last_result
        if result is None:
            return None
        return round(result.measured_heating_power_w, 1)


class RoomTemperatureSensor(_DiagnosticSensor):
    _attr_translation_key = "room_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    # The room sensor and the TRVs record their own readings.
    _unrecorded_attributes = frozenset({"room_sensor_temp_c", "trv_temperatures"})

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "room_temperature")

    @property
    def native_value(self) -> float | None:
        result = self._coordinator.mpc.get_room_temperature()
        return None if isinstance(result, RoomMpcError) else result.temperature_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._coordinator.mpc.get_room_temperature()
        if isinstance(result, RoomMpcError):
            return {}
        last_result = self._coordinator.last_result
        return {
            "used_strategy": result.used_strategy,
            "room_sensor_temp_c": result.room_sensor_temp_c,
            "estimated_temp_c": (
                round(last_result.estimated_room_temperature_c, 2)
                if last_result is not None
                and last_result.estimated_room_temperature_c is not None
                else None
            ),
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
    def available(self) -> bool:
        """Until the startup gate opens, the mode and offsets are not settled."""
        return self._coordinator.running

    @property
    def native_value(self) -> float:
        return self._coordinator.base_temperature_c


class UaLearningStatusSensor(_DiagnosticSensor):
    """Where the heat balance for the heat-loss factor stands."""

    _attr_translation_key = "ua_learning_status"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "ua_learning_status")
        self._attr_options = [outcome.value for outcome in UaBalanceOutcome]

    @property
    def native_value(self) -> str | None:
        outcome = self._coordinator.mpc.learning.get_ua_balance_info().outcome
        return outcome.value if outcome else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._coordinator.mpc.learning.get_ua_balance_info()
        gains = self._coordinator.mpc.gain_model
        since_ts = info.collecting_since_ts
        return {
            "collecting_since": iso(since_ts),
            "due": iso(
                since_ts + UA_BALANCE_MIN_WINDOW_S if since_ts is not None else None
            ),
            "last_learned": iso(info.last_learned_ts),
            "last_target_factor": rounded(info.last_target_factor, 4),
            "last_drift_c": rounded(info.last_drift_c, 2),
            "last_window_h": (
                round(info.last_window_s / 3600, 2)
                if info.last_window_s is not None
                else None
            ),
            "last_mean_outdoor_c": rounded(info.last_mean_outdoor_c, 2),
            "last_mean_room_c": rounded(info.last_mean_room_c, 2),
            "last_mean_flow_c": rounded(info.last_mean_flow_c, 2),
            "last_mean_heating_w": rounded(info.last_mean_heating_w, 1),
            # Night regression of heat-loss factor and steady gain.
            "learned_gain_w": rounded(gains.learned_gain_w, 1),
            "gain_sigma_w": rounded(gains.gain_sigma_w, 1),
            "gain_fit_points": gains.night_points,
            "gain_determined": gains.gain_determined,
            "regression_ua_factor": rounded(gains.regression_ua_factor, 4),
        }


class CapacityLearningStatusSensor(_DiagnosticSensor):
    """Outcome of the last jump stretch."""

    _attr_translation_key = "capacity_learning_status"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "capacity_learning_status")
        self._attr_options = [outcome.value for outcome in CapacityOutcome]

    @property
    def native_value(self) -> str:
        return self._coordinator.mpc.learning.get_capacity_outcome().value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mpc = self._coordinator.mpc
        info = mpc.learning.get_capacity_info()
        vent = mpc.learning.get_vent_info()
        return {
            "capacity_j_per_k": round(mpc.effective_capacity_j_per_k),
            "last_stretch_kind": info.kind,
            "last_stretch_start": iso(info.start_ts),
            "last_stretch_end": iso(info.end_ts),
            "last_stretch_room_delta_c": rounded(info.room_delta_c, 2),
            "last_stretch_estimate_factor": rounded(info.estimate_factor, 3),
            "last_stretch_saturated_share": rounded(info.saturated_share, 2),
            "last_learned": iso(info.last_learned_ts),
            # Airing capacity after a window closed: observed only.
            "vent_capacity_factor": rounded(mpc.learning.vent_capacity_factor, 4),
            "vent_learned": mpc.learning.learned_vent_capacity_factor is not None,
            "vent_outcome": vent.outcome.value,
            "vent_stretch_start": iso(vent.start_ts),
            "vent_stretch_end": iso(vent.end_ts),
            "vent_stretch_room_delta_c": rounded(vent.room_delta_c, 2),
            "vent_stretch_estimate_factor": rounded(vent.estimate_factor, 3),
            "vent_last_learned": iso(vent.last_learned_ts),
        }


class UaFactorSensor(_DiagnosticSensor):
    _attr_translation_key = "ua_factor"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "ua_factor")

    @property
    def native_value(self) -> float:
        return self._coordinator.mpc.learned_ua_factor


class CapacityFactorSensor(_DiagnosticSensor):
    _attr_translation_key = "capacity_factor"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "capacity_factor")

    @property
    def native_value(self) -> float:
        return self._coordinator.mpc.learned_capacity_factor

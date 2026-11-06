from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfIrradiance,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LEAD_FLOW,
    CapacityOutcome,
    CurveEnd,
    Surface,
    UaBalanceOutcome,
)
from .control.circulation import CirculationReason
from .control.flow import ActuatorMode
from .control.hot_water import HotWaterPhase
from .control.legionella import LegionellaState
from .controller.mpc.learner import UA_BALANCE_MIN_WINDOW_S
from .controller.mpc.valve_curve import ValveCurveStatus
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
from .hub import HeatingHub, LeadValue


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub: HeatingHub = entry.runtime_data
    hub_entities: list[SensorEntity] = [
        LeadHoldFlowTemperatureSensor(hub),
        LeadMinFlowTemperatureSensor(hub),
        HeatingDemandHoursSensor(hub),
        HeatingSeasonLastDemandSensor(hub),
        WriteBudgetSensor(hub),
    ]
    if hub.has_flow_curve:
        hub_entities += [HoldFlowReserveSensor(hub), MinFlowReserveSensor(hub)]
    async_add_flow_entities(
        entry,
        async_add_entities,
        lambda flow: [
            FlowCurveSensor(flow),
            FlowActuatorActualSensor(flow),
            FlowEffectiveCurveSensor(flow),
            FlowTargetSensor(flow),
            FlowActuatorTargetSensor(flow),
            FlowThresholdSensor(flow),
            FlowCurveRecommendationSensor(flow, CurveEnd.COLD),
            FlowCurveRecommendationSensor(flow, CurveEnd.WARM),
            FlowHoldCurvesSensor(flow),
        ],
    )
    async_add_hot_water_entities(
        entry,
        async_add_entities,
        lambda hot_water: (
            [HotWaterTargetSensor(hot_water), HotWaterPhaseSensor(hot_water)]
            + (
                [LegionellaStatusSensor(hot_water)]
                if hot_water.legionella_enabled
                else []
            )
            + (
                [CirculationReasonSensor(hot_water)]
                if hot_water.circulation_enabled
                else []
            )
        ),
    )
    if hub.irradiance is not None:
        hub_entities += [
            IrradianceSensor(hub, geometry.surface)
            for geometry in hub.irradiance.geometries
        ]
    async_add_entities(hub_entities)
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: [
            MinFlowTemperatureSensor(coordinator),
            HoldFlowTemperatureSensor(coordinator),
            GatedHoldFlowTemperatureSensor(coordinator),
            CurveHoldFlowTemperatureSensor(coordinator, CurveEnd.COLD),
            CurveHoldFlowTemperatureSensor(coordinator, CurveEnd.WARM),
            HeatingDemandSensor(coordinator),
            RequestedHeatingPowerSensor(coordinator),
            AvailableHeatingPowerSensor(coordinator),
            RadiatorHeatingPowerSensor(coordinator),
            MeasuredHeatingPowerSensor(coordinator),
            RoomTemperatureSensor(coordinator),
            BaseTemperatureSensor(coordinator),
            UaLearningStatusSensor(coordinator),
            CapacityLearningStatusSensor(coordinator),
            UaFactorSensor(coordinator),
            CapacityFactorSensor(coordinator),
            ValveCurveSensor(coordinator),
            AdditionalHeatSensor(coordinator),
        ],
    )


class _LeadFlowSensor(HeatingHubEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def _lead(self) -> LeadValue:
        raise NotImplementedError

    @property
    def native_value(self) -> float | None:
        return self._lead().value_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead()
        return {"room": lead.room, "rooms": lead.rooms}


class LeadHoldFlowTemperatureSensor(_LeadFlowSensor):
    """Highest gated hold flow among the heating-limit lead rooms."""

    _attr_translation_key = "lead_hold_flow_temperature"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "lead_hold_flow_temperature")

    def _lead(self) -> LeadValue:
        return self._hub.lead_hold_flow


class LeadMinFlowTemperatureSensor(_LeadFlowSensor):
    """Highest minimum flow among the flow lead rooms."""

    _attr_translation_key = "lead_min_flow_temperature"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "lead_min_flow_temperature")

    def _lead(self) -> LeadValue:
        return self._hub.lead_min_flow


class HeatingDemandHoursSensor(HeatingHubEntity, SensorEntity):
    """Hours of the last 24 h in which the lead rooms' heating demand was on."""

    _attr_translation_key = "heating_demand_hours_24h"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 1

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_demand_hours_24h")

    @property
    def native_value(self) -> float:
        return self._hub.demand_hours_24h


class HeatingSeasonLastDemandSensor(HeatingHubEntity, SensorEntity):
    """Last time the 24 h demand was above the hold threshold."""

    _attr_translation_key = "heating_season_last_demand"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_season_last_demand")

    @property
    def native_value(self) -> datetime | None:
        last_ts = self._hub.season.last_demand_ts
        return None if last_ts is None else dt_util.utc_from_timestamp(last_ts)


class WriteBudgetSensor(HeatingHubEntity, SensorEntity):
    """Calls left for writes to the heat source's cloud today.

    With a rate-limit entity its value, otherwise the integration's own daily
    maximum minus its writes.
    """

    _attr_translation_key = "write_budget"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-upload-outline"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "write_budget")

    @property
    def native_value(self) -> int | None:
        remaining = self._hub.rate_limit_remaining
        if remaining is not None:
            return remaining
        if self._hub.rate_limit_remaining_entity:
            return None
        used, _dropped = self._hub.write_budget.counts(time.time())
        return max(0, self._hub.write_budget.max_per_day - used)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        budget = self._hub.write_budget
        used, dropped = budget.counts(time.time())
        return {
            "own_writes_today": used,
            "dropped_today": dropped,
            "reserve": budget.reserve,
            "max_writes_per_day": budget.max_per_day,
            "source": self._hub.rate_limit_remaining_entity,
        }


class HotWaterTargetSensor(HotWaterEntity, SensorEntity):
    """The water heater target the control sets."""

    _attr_translation_key = "hot_water_target"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "hot_water_target")

    @property
    def native_value(self) -> float | None:
        decision = self._hot_water.decision
        return None if decision is None else decision.target_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hw = self._hot_water
        decision = hw.decision
        return {
            "mode": hw.controller.mode.value,
            "manual": hw.controller.manual.value,
            "pending_write": None if decision is None else decision.write,
            "last_written": hw.last_written,
            "last_write": _iso(hw.last_write_ts),
            "last_error": hw.last_error,
        }


class HotWaterPhaseSensor(HotWaterEntity, SensorEntity):
    """Which phase sets the target now."""

    _attr_translation_key = "hot_water_phase"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "hot_water_phase")
        self._attr_options = [phase.value for phase in HotWaterPhase]

    @property
    def native_value(self) -> str | None:
        decision = self._hot_water.decision
        return None if decision is None else decision.phase.value


class LegionellaStatusSensor(HotWaterEntity, SensorEntity):
    """State of the legionella cycle, with last start and last success."""

    _attr_translation_key = "legionella_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:bacteria-outline"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "legionella_status")
        self._attr_options = [state.value for state in LegionellaState]

    @property
    def native_value(self) -> str:
        return self._hot_water.legionella.cycle.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cycle = self._hot_water.legionella.cycle
        return {
            "last_start": cycle.last_start.isoformat() if cycle.last_start else None,
            "last_success": cycle.last_success.isoformat()
            if cycle.last_success
            else None,
            "failure": cycle.failure.value if cycle.failure else None,
            "outlet_ok": cycle.outlet_ok,
            "return_ok": cycle.return_ok,
        }


class CirculationReasonSensor(HotWaterEntity, SensorEntity):
    """Why the circulation pump runs or rests."""

    _attr_translation_key = "circulation_reason"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:pump"

    def __init__(self, hot_water: HotWaterDevice) -> None:
        super().__init__(hot_water, "circulation_reason")
        self._attr_options = [reason.value for reason in CirculationReason]

    @property
    def native_value(self) -> str | None:
        decision = self._hot_water.circulation_decision
        return None if decision is None else decision.reason.value


class _FlowTemperatureSensor(FlowEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1


class FlowCurveSensor(_FlowTemperatureSensor):
    """The heat source's curve at the current outdoor value, no offset."""

    _attr_translation_key = "flow_curve"

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_curve")

    @property
    def native_value(self) -> float | None:
        return self._flow.curve_flow_c


class FlowEffectiveCurveSensor(_FlowTemperatureSensor):
    """What the heat source aims at: curve plus offset, or the setpoint."""

    _attr_translation_key = "flow_effective_curve"

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_effective_curve")

    @property
    def native_value(self) -> float | None:
        return self._flow.effective_curve_c


class FlowThresholdSensor(_FlowTemperatureSensor):
    """The lowest useful flow: curve's warm end + offset, or the number."""

    _attr_translation_key = "flow_threshold"

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_threshold_effective")

    @property
    def native_value(self) -> float:
        return self._flow.threshold_c


class FlowTargetSensor(_FlowTemperatureSensor):
    """The flow the controller asks for."""

    _attr_translation_key = "flow_target"

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_target")

    @property
    def native_value(self) -> float | None:
        decision = self._flow.decision
        return None if decision is None else decision.target_flow_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        decision = self._flow.decision
        lead = self._flow.hub.lead_min_flow
        return {
            "source": None
            if decision is None or decision.source is None
            else str(decision.source),
            "reason": None if decision is None else decision.reason,
            "lead_min_flow_c": lead.value_c,
            "room": lead.room,
            "threshold_c": self._flow.threshold_c,
        }


class FlowCurveRecommendationSensor(_FlowTemperatureSensor):
    """Curve point the flow lead rooms need: hold flow at comfort plus margin.

    Learned UA, no gains. Compare with the curve number of the same end.
    """

    def __init__(self, flow: FlowDevice, end: CurveEnd) -> None:
        super().__init__(flow, f"flow_curve_recommendation_{end}")
        self._end = end
        self._attr_translation_key = f"flow_curve_recommendation_{end}"

    def _lead(self) -> LeadValue:
        return self._flow.hub.lead_curve_hold_flow(self._end)

    @property
    def available(self) -> bool:
        return self._lead().value_c is not None

    @property
    def native_value(self) -> float | None:
        lead = self._lead()
        if lead.value_c is None:
            return None
        return round(lead.value_c + self._flow.numbers["margin"], 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead()
        current_c = self._flow.numbers[f"curve_flow_{self._end}"]
        value = self.native_value
        return {
            "outdoor_temperature_c": self._flow.numbers[f"curve_outdoor_{self._end}"],
            "lead_hold_flow_c": lead.value_c,
            "margin_k": self._flow.numbers["margin"],
            "room": lead.room,
            "rooms": lead.rooms,
            "current_curve_flow_c": current_c,
            "difference_k": None if value is None else round(current_c - value, 1),
        }


class FlowHoldCurvesSensor(_FlowTemperatureSensor):
    """Every room's comfort hold flow over the curve's outdoor range.

    State: the flow lead rooms' highest hold flow plus margin at the current
    outdoor value, the curve point they would ask for right now. The curves
    feed the heating curve card; they change only with the learned UA and
    are left out of the recorder.
    """

    _attr_translation_key = "hold_flow_curves"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"outdoor_c", "rooms"})

    # Outdoor values beyond the curve's ends, so its flat parts show.
    _EXTEND_K = 2

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "hold_flow_curves")

    @property
    def native_value(self) -> float | None:
        outdoor = self._flow.hub.outdoor_temperature_c
        if outdoor is None:
            return None
        values = [
            value
            for room in self._flow.hub.rooms.values()
            if room.data.get(CONF_LEAD_FLOW)
            and (value := room.comfort_hold_flow_c(outdoor)) is not None
        ]
        if not values:
            return None
        return round(max(values) + self._flow.numbers["margin"], 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        numbers = self._flow.numbers
        cold = math.floor(numbers["curve_outdoor_cold"]) - self._EXTEND_K
        warm = math.ceil(numbers["curve_outdoor_warm"]) + self._EXTEND_K
        outdoor_values = [float(value) for value in range(cold, warm + 1)]
        return {
            "outdoor_temperature_c": self._flow.hub.outdoor_temperature_c,
            "margin_k": numbers["margin"],
            "outdoor_c": outdoor_values,
            "rooms": self._flow.hub.hold_flow_curves(outdoor_values),
        }


class FlowActuatorActualSensor(FlowEntity, SensorEntity):
    """The actuator as the heat source reports it (offset in K or setpoint)."""

    _attr_translation_key = "flow_actuator_actual"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_actuator_actual")
        self._attr_native_unit_of_measurement = (
            UnitOfTemperature.KELVIN
            if flow.mode is ActuatorMode.OFFSET
            else UnitOfTemperature.CELSIUS
        )

    @property
    def native_value(self) -> float | None:
        return self._flow.actual


class FlowActuatorTargetSensor(FlowEntity, SensorEntity):
    """The actuator value the controller writes, or would write in shadow."""

    _attr_translation_key = "flow_actuator_target"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_actuator_target")
        self._attr_native_unit_of_measurement = (
            UnitOfTemperature.KELVIN
            if flow.mode is ActuatorMode.OFFSET
            else UnitOfTemperature.CELSIUS
        )

    @property
    def native_value(self) -> float | None:
        decision = self._flow.decision
        return None if decision is None else decision.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        controller = self._flow.controller
        decision = self._flow.decision
        return {
            "writing": self._flow.write_enabled,
            "pending_write": None if decision is None else decision.write,
            "last_written": controller.last_written,
            "last_write": _iso(controller.last_write_ts),
            "external_change": _iso(controller.external_change_ts),
            "last_error": self._flow.last_error,
        }


class _FlowReserveSensor(HeatingHubEntity, SensorEntity):
    """The heating curve minus a lead flow; a temperature difference in K."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:thermometer-chevron-up"

    def _reserve(self) -> float | None:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return self._reserve() is not None

    @property
    def native_value(self) -> float | None:
        return self._reserve()


class HoldFlowReserveSensor(_FlowReserveSensor):
    _attr_translation_key = "hold_flow_reserve"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "hold_flow_reserve")

    def _reserve(self) -> float | None:
        return self._hub.hold_flow_reserve


class MinFlowReserveSensor(_FlowReserveSensor):
    _attr_translation_key = "min_flow_reserve"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "min_flow_reserve")

    def _reserve(self) -> float | None:
        return self._hub.min_flow_reserve


class IrradianceSensor(HeatingHubEntity, SensorEntity):
    """Sun on one surface of the building, from Open-Meteo."""

    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfIrradiance.WATTS_PER_SQUARE_METER
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: HeatingHub, surface: Surface) -> None:
        super().__init__(hub, f"irradiance_{surface}")
        self._surface = surface
        self._attr_translation_key = f"irradiance_{surface}"

    @property
    def available(self) -> bool:
        provider = self._hub.irradiance
        return provider is not None and provider.series.fresh(time.time())

    @property
    def native_value(self) -> float | None:
        provider = self._hub.irradiance
        if provider is None:
            return None
        return _rounded(provider.series.value_at(self._surface, time.time()), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        provider = self._hub.irradiance
        if provider is None:
            return {}
        now_ts = time.time()
        geometry = next(
            (g for g in provider.geometries if g.surface == self._surface), None
        )
        return {
            "azimuth_deg": geometry.azimuth_deg if geometry else None,
            "tilt_deg": geometry.tilt_deg if geometry else None,
            "last_update": _iso(provider.series.fetched_ts),
            "last_error": provider.last_error,
            "forecast": [
                [_iso(start_ts), round(value, 1)]
                for start_ts, value in provider.series.forecast(
                    self._surface, now_ts, 24
                )
            ],
        }


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
        info = self._coordinator.mpc.get_ua_balance_info()
        return {
            "outdoor_temperature_c": self._coordinator.curve_outdoor_c(self._end),
            "target_temperature_c": self._coordinator.comfort_temperature_c,
            "ua_factor": _rounded(self._coordinator.mpc.learned_ua_factor, 4),
            "ua_last_learned": _iso(info.last_learned_ts),
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
            ),
            "trv_setpoint_writes": dict(self._coordinator.trv_setpoint_writes),
            "trv_valve_positions": self._coordinator.trv_valve_positions,
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
            "residual_rms_w": _rounded(gains.residual_rms_w, 1),
            "last_hour": _iso(gains.last_hour_ts),
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
        if result is None or result.measured_heating_power_w is None:
            return None
        return round(result.measured_heating_power_w, 1)


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
    def native_value(self) -> float:
        return self._coordinator.base_temperature_c


class UaLearningStatusSensor(_DiagnosticSensor):
    """Where the heat balance for the heat-loss factor stands."""

    _attr_translation_key = "ua_learning_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [outcome.value for outcome in UaBalanceOutcome]  # noqa: RUF012

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "ua_learning_status")

    @property
    def native_value(self) -> str | None:
        outcome = self._coordinator.mpc.get_ua_balance_info().outcome
        return outcome.value if outcome else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._coordinator.mpc.get_ua_balance_info()
        gains = self._coordinator.mpc.gain_model
        since_ts = info.collecting_since_ts
        return {
            "collecting_since": _iso(since_ts),
            "due": _iso(
                since_ts + UA_BALANCE_MIN_WINDOW_S if since_ts is not None else None
            ),
            "last_learned": _iso(info.last_learned_ts),
            "last_target_factor": _rounded(info.last_target_factor, 4),
            "last_drift_c": _rounded(info.last_drift_c, 2),
            "last_window_h": (
                round(info.last_window_s / 3600, 2)
                if info.last_window_s is not None
                else None
            ),
            "last_mean_outdoor_c": _rounded(info.last_mean_outdoor_c, 2),
            "last_mean_room_c": _rounded(info.last_mean_room_c, 2),
            "last_mean_flow_c": _rounded(info.last_mean_flow_c, 2),
            "last_mean_heating_w": _rounded(info.last_mean_heating_w, 1),
            # Night regression of heat-loss factor and steady gain.
            "learned_gain_w": _rounded(gains.learned_gain_w, 1),
            "gain_sigma_w": _rounded(gains.gain_sigma_w, 1),
            "gain_fit_points": gains.night_points,
            "gain_determined": gains.gain_determined,
            "regression_ua_factor": _rounded(gains.regression_ua_factor, 4),
        }


class CapacityLearningStatusSensor(_DiagnosticSensor):
    """What the last 30-minute replay did with the capacity factor."""

    _attr_translation_key = "capacity_learning_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [outcome.value for outcome in CapacityOutcome]  # noqa: RUF012

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "capacity_learning_status")

    @property
    def native_value(self) -> str:
        return self._coordinator.mpc.get_capacity_outcome().value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "capacity_j_per_k": round(self._coordinator.mpc.effective_capacity_j_per_k)
        }


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).isoformat()


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


class ValveCurveSensor(_DiagnosticSensor):
    """How far each TRV has learned its valve curve.

    The state counts the TRVs whose curve is learned; per TRV the attributes
    show its status and the bins (offset °C, opening %, samples, p90-p10
    spread in percentage points), keyed by the TRV's climate entity. TRVs
    without a valve entity learn nothing and are left out.
    """

    _attr_translation_key = "valve_curve"

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "valve_curve")

    @property
    def native_value(self) -> int:
        return sum(
            status is ValveCurveStatus.LEARNED
            for status, _ in self._coordinator.mpc.valve_curve_report().values()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            name: {
                "status": status.value,
                "bins": [
                    [
                        round(bin_.offset_c, 2),
                        round(bin_.opening_pct, 1),
                        bin_.samples,
                        round(bin_.spread_pct, 1),
                    ]
                    for bin_ in bins
                ],
            }
            for name, (status, bins) in (
                self._coordinator.mpc.valve_curve_report().items()
            )
        }

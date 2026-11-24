"""Sensors of the flow device."""

from __future__ import annotations

import math
import time
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_LEAD_FLOW,
    CurveEnd,
)
from ..control.flow import ActuatorMode
from ..control.flow_feedback import UP_AFTER_S
from ..entity import (
    FlowEntity,
)
from ..flow_device import FlowDevice
from ..lead_rooms import LeadValue
from .format import iso


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
        hub = self._flow.hub
        lead = hub.leads.feedforward_flow
        boost = hub.leads.boost_flow
        return {
            "source": None
            if decision is None or decision.source is None
            else str(decision.source),
            "reason": None if decision is None else decision.reason,
            "feedforward_flow_c": lead.value_c,
            "room": lead.room,
            "feedback_k": hub.flow_feedback.feedback_k,
            "measured_correction_k": round(hub.flow_correction.correction_k, 2),
            "measured_correction_frozen_reason": None
            if hub.flow_correction.frozen is None
            else str(hub.flow_correction.frozen),
            "boost_flow_c": boost.value_c,
            "boost_room": boost.room,
            "threshold_c": self._flow.threshold_c,
        }


class FlowFeedbackSensor(FlowEntity, SensorEntity):
    """The valve feedback added to the feedforward flow."""

    _attr_translation_key = "flow_feedback"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 1

    def __init__(self, flow: FlowDevice) -> None:
        super().__init__(flow, "flow_feedback")

    @property
    def native_value(self) -> float:
        return self._flow.hub.flow_feedback.feedback_k

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        feedback = self._flow.hub.flow_feedback
        lead = feedback.lead
        last = feedback.last_change_ts
        catching_up = feedback.catching_up(time.time())
        return {
            "lead_demand_pct": None if lead is None else lead.demand_pct,
            "lead_room": None if lead is None else lead.room,
            "lead_deficit_c": None if lead is None else lead.deficit_c,
            "feedforward_flow_c": self._flow.hub.leads.feedforward_flow.value_c,
            "frozen": feedback.frozen is not None,
            "frozen_reason": None if feedback.frozen is None else str(feedback.frozen),
            "flow_timer_min": round(feedback.count_s / 60),
            "step_up_at_min": -round(UP_AFTER_S / 60),
            "step_down_at_min": round(feedback.down_after_s / 60),
            "night": feedback.night,
            "locked_direction": feedback.locked_sign(time.time()),
            "locked_until": iso(feedback.locked_until_ts(time.time())),
            "catch_up_until": (
                dt_util.utc_from_timestamp(feedback.catch_up_until_ts).isoformat()
                if catching_up and feedback.catch_up_until_ts is not None
                else None
            ),
            "catch_up_reason": str(feedback.catch_up_reason) if catching_up else None,
            "room_variant_up": feedback.variant_up,
            "room_variant_down": feedback.variant_down,
            "last_change": None
            if last is None
            else dt_util.utc_from_timestamp(last).isoformat(),
        }


class FlowCurveRecommendationSensor(_FlowTemperatureSensor):
    """Curve point the flow lead rooms need: hold flow at comfort plus feedback.

    Learned UA, no gains; the valve feedback stands for what the model
    misses. Compare with the curve number of the same end.
    """

    def __init__(self, flow: FlowDevice, end: CurveEnd) -> None:
        super().__init__(flow, f"flow_curve_recommendation_{end}")
        self._end = end
        self._attr_translation_key = f"flow_curve_recommendation_{end}"

    def _lead(self) -> LeadValue:
        return self._flow.hub.leads.curve_hold_flow(self._end)

    @property
    def available(self) -> bool:
        return self._lead().value_c is not None

    @property
    def native_value(self) -> float | None:
        lead = self._lead()
        if lead.value_c is None:
            return None
        return round(lead.value_c + self._flow.hub.flow_feedback.feedback_k, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead()
        current_c = self._flow.numbers[f"curve_flow_{self._end}"]
        value = self.native_value
        return {
            "outdoor_temperature_c": self._flow.numbers[f"curve_outdoor_{self._end}"],
            "lead_hold_flow_c": lead.value_c,
            "feedback_k": self._flow.hub.flow_feedback.feedback_k,
            "room": lead.room,
            "rooms": lead.rooms,
            "current_curve_flow_c": current_c,
            "difference_k": None if value is None else round(current_c - value, 1),
        }


class FlowHoldCurvesSensor(_FlowTemperatureSensor):
    """Every room's comfort hold flow over the curve's outdoor range.

    State: the flow lead rooms' highest hold flow plus feedback at the current
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
            and (value := room.comfort_hold_flow_c(outdoor, round_up=False)) is not None
        ]
        if not values:
            return None
        return round(max(values) + self._flow.hub.flow_feedback.feedback_k, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        numbers = self._flow.numbers
        cold = math.floor(numbers["curve_outdoor_cold"]) - self._EXTEND_K
        warm = math.ceil(numbers["curve_outdoor_warm"]) + self._EXTEND_K
        outdoor_values = [float(value) for value in range(cold, warm + 1)]
        feedback = self._flow.hub.flow_feedback
        lead = feedback.lead
        return {
            "outdoor_temperature_c": self._flow.hub.outdoor_temperature_c,
            "feedback_k": feedback.feedback_k,
            "lead_demand_pct": None if lead is None else lead.demand_pct,
            "lead_room": None if lead is None else lead.room,
            "outdoor_c": outdoor_values,
            "rooms": self._flow.hub.leads.hold_flow_curves(outdoor_values),
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
            "last_write": iso(controller.last_write_ts),
            "external_change": iso(controller.external_change_ts),
            "last_error": self._flow.last_error,
        }

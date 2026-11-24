"""Sensors of the hot water device."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfTemperature,
)

from ..control.circulation import CirculationReason
from ..control.hot_water import HotWaterPhase
from ..control.legionella import LegionellaState
from ..entity import (
    HotWaterEntity,
)
from ..hot_water_device import HotWaterDevice
from .format import iso


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
            "last_write": iso(hw.last_write_ts),
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

"""The hot water subentry: its keys, starting values and what they configure.

The one place that reads the subentry's data: the device and the config flow
name every setting by its key here, so a misspelt key fails on import.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from typing import Any

from .control.circulation import CirculationConfig
from .control.hot_water import HotWaterConfig
from .control.legionella import LegionellaConfig

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class HotWaterKey(StrEnum):
    # Sources
    WATER_HEATER_ENTITY = "water_heater_entity"
    TANK_TEMPERATURE_ENTITY = "tank_temperature_entity"
    GRID_EXPORT_ENTITY = "grid_export_entity"
    BATTERY_SOC_ENTITY = "battery_soc_entity"
    BATTERY_DISCHARGE_ENTITY = "battery_discharge_entity"
    LEGIONELLA_ENABLED = "legionella_enabled"
    CIRCULATION_ENABLED = "circulation_enabled"
    # Modes
    GRID_LOWER_W = "grid_lower_w"
    GRID_UPPER_W = "grid_upper_w"
    GRID_RELEASE_W = "grid_release_w"
    GRID_STABLE_MIN = "grid_stable_min"
    BATTERY_SOC_MIN = "battery_soc_min"
    DISCHARGE_MAX_W = "discharge_max_w"
    DISCHARGE_STABLE_MIN = "discharge_stable_min"
    OUTDOOR_LOWER_C = "outdoor_lower_c"
    OUTDOOR_UPPER_C = "outdoor_upper_c"
    TANK_GATE_C = "tank_gate_c"
    MIN_HOLD_MIN = "min_hold_min"
    MANUAL_HOLD_MIN = "manual_hold_min"
    BOOST_MAX_H = "boost_max_h"
    NIGHT_ENABLED = "night_enabled"
    NIGHT_START = "night_start"
    NIGHT_END = "night_end"
    WINDOW_ENABLED = "window_enabled"
    WINDOW_START = "window_start"
    WINDOW_END = "window_end"
    HEATING_NIGHT_END = "heating_night_end"
    FORCED_CHARGE = "forced_charge"
    REHEAT_HYSTERESIS_K = "reheat_hysteresis_k"
    MAX_SETPOINT_C = "max_setpoint_c"
    # Legionella
    LEGIONELLA_WEEKDAYS = "legionella_weekdays"
    LEGIONELLA_START = "legionella_start"
    LEGIONELLA_FALLBACK = "legionella_fallback"
    LEGIONELLA_RETRY_MIN = "legionella_retry_min"
    FORECAST_ENTITY = "forecast_entity"
    FORECAST_ABOVE = "forecast_above"
    FORECAST_THRESHOLD = "forecast_threshold"
    OUTLET_ENTITY = "outlet_entity"
    OUTLET_THRESHOLD_C = "outlet_threshold_c"
    RETURN_ENTITY = "return_entity"
    RETURN_THRESHOLD_C = "return_threshold_c"
    LEGIONELLA_HOLD_MIN = "legionella_hold_min"
    LEGIONELLA_TIMEOUT_MIN = "legionella_timeout_min"
    # Circulation
    PUMP_ENTITY = "pump_entity"
    CIRC_RETURN_ENTITY = "circ_return_entity"
    CIRC_OUTLET_ENTITY = "circ_outlet_entity"
    DRAW_ENTITIES = "draw_entities"
    CONTINUOUS_ENTITIES = "continuous_entities"
    BLOCKED_STATES = "blocked_states"
    PRESENCE_ENTITY = "presence_entity"
    DAYMODE_ENTITY = "daymode_entity"
    PEAK1_START = "peak1_start"
    PEAK1_END = "peak1_end"
    PEAK2_START = "peak2_start"
    PEAK2_END = "peak2_end"
    PEAK_START_BELOW_C = "peak_start_below_c"
    OFFPEAK_START_BELOW_C = "offpeak_start_below_c"
    STOP_DELTA_K = "stop_delta_k"
    STOP_TEMPERATURE_C = "stop_temperature_c"
    MAX_RUN_MIN = "max_run_min"
    MIN_PAUSE_MIN = "min_pause_min"
    FALLBACK_ON_MIN = "fallback_on_min"
    FALLBACK_OFF_MIN = "fallback_off_min"
    DRAW_OVERRIDES_GATES = "draw_overrides_gates"
    RELEASE_ENABLED = "release_enabled"
    RELEASE_TEMPERATURE_C = "release_temperature_c"
    RELEASE_MIN = "release_min"


class HotWaterNumber(StrEnum):
    """The temperatures, set as numbers on the device."""

    NORMAL = "normal"
    SURPLUS = "surplus"
    BOOST = "boost"
    NIGHT = "night"
    WINDOW = "window"
    HEATING_NIGHT = "heating_night"
    HEATING_WINDOW = "heating_window"
    LEGIONELLA = "legionella"


K = HotWaterKey
N = HotWaterNumber

HOT_WATER_NUMBER_DEFAULTS: dict[HotWaterNumber, float] = {
    N.NORMAL: 46.0,
    N.SURPLUS: 57.0,
    N.BOOST: 60.0,
    N.NIGHT: 38.0,
    N.WINDOW: 53.0,
    N.HEATING_NIGHT: 44.0,
    N.HEATING_WINDOW: 50.0,
    N.LEGIONELLA: 60.0,
}

# Subentry settings and their starting values; the entities have none.
HOT_WATER_DEFAULTS: dict[str, Any] = {
    K.LEGIONELLA_ENABLED: False,
    K.CIRCULATION_ENABLED: False,
    K.GRID_LOWER_W: 3000.0,
    K.GRID_UPPER_W: 5000.0,
    K.GRID_RELEASE_W: 1000.0,
    K.GRID_STABLE_MIN: 5.0,
    K.BATTERY_SOC_MIN: 50.0,
    K.DISCHARGE_MAX_W: 300.0,
    K.DISCHARGE_STABLE_MIN: 2.0,
    K.OUTDOOR_LOWER_C: 12.0,
    K.OUTDOOR_UPPER_C: 24.0,
    K.TANK_GATE_C: 40.0,
    K.MIN_HOLD_MIN: 15.0,
    K.MANUAL_HOLD_MIN: 60.0,
    K.BOOST_MAX_H: 6.0,
    K.NIGHT_ENABLED: True,
    K.NIGHT_START: "21:00:00",
    K.NIGHT_END: "05:00:00",
    K.WINDOW_ENABLED: True,
    K.WINDOW_START: "13:00:00",
    K.WINDOW_END: "16:00:00",
    K.HEATING_NIGHT_END: "13:00:00",
    K.FORCED_CHARGE: True,
    K.REHEAT_HYSTERESIS_K: 5.0,
    K.MAX_SETPOINT_C: 60.0,
    K.LEGIONELLA_WEEKDAYS: ["tue"],
    K.LEGIONELLA_START: "13:00:00",
    K.LEGIONELLA_FALLBACK: "15:00:00",
    K.LEGIONELLA_RETRY_MIN: 120.0,
    K.FORECAST_ABOVE: True,
    K.FORECAST_THRESHOLD: 0.0,
    K.OUTLET_THRESHOLD_C: 60.0,
    K.RETURN_THRESHOLD_C: 58.0,
    K.LEGIONELLA_HOLD_MIN: 30.0,
    K.LEGIONELLA_TIMEOUT_MIN: 240.0,
    K.BLOCKED_STATES: ["off"],
    K.PEAK1_START: "06:00:00",
    K.PEAK1_END: "09:30:00",
    K.PEAK2_START: "17:00:00",
    K.PEAK2_END: "22:00:00",
    K.PEAK_START_BELOW_C: 40.0,
    K.OFFPEAK_START_BELOW_C: 35.0,
    K.STOP_DELTA_K: 4.0,
    K.STOP_TEMPERATURE_C: 44.0,
    K.MAX_RUN_MIN: 3.0,
    K.MIN_PAUSE_MIN: 5.0,
    K.FALLBACK_ON_MIN: 2.0,
    K.FALLBACK_OFF_MIN: 15.0,
    K.DRAW_OVERRIDES_GATES: False,
    K.RELEASE_ENABLED: True,
    K.RELEASE_TEMPERATURE_C: 58.0,
    K.RELEASE_MIN: 30.0,
}


def _time(value: str) -> time:
    return time.fromisoformat(value)


def _optional_time(value: str | None) -> time | None:
    return _time(value) if value else None


@dataclass(frozen=True)
class HotWaterSettings:
    """The subentry's data, merged with the starting values."""

    data: Mapping[str, Any]
    # Optional, so not taken from the starting values when cleared.
    legionella_fallback: str | None

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> HotWaterSettings:
        return cls({**HOT_WATER_DEFAULTS, **data}, data.get(K.LEGIONELLA_FALLBACK))

    def entity(self, key: HotWaterKey) -> str | None:
        """A configured entity; None if left empty."""
        return self.data.get(key) or None

    def entities(self, key: HotWaterKey) -> list[str]:
        return list(self.data.get(key) or [])

    def _float(self, key: HotWaterKey) -> float:
        return float(self.data[key])

    def _bool(self, key: HotWaterKey) -> bool:
        return bool(self.data[key])

    def _time(self, key: HotWaterKey) -> time:
        return _time(self.data[key])

    @property
    def water_heater(self) -> str:
        return str(self.data[K.WATER_HEATER_ENTITY])

    @property
    def legionella_enabled(self) -> bool:
        return self._bool(K.LEGIONELLA_ENABLED)

    @property
    def circulation_enabled(self) -> bool:
        return self._bool(K.CIRCULATION_ENABLED) and bool(self.entity(K.PUMP_ENTITY))

    @property
    def reheat_hysteresis_k(self) -> float:
        return self._float(K.REHEAT_HYSTERESIS_K)

    @property
    def blocked_states(self) -> list[str]:
        return list(self.data[K.BLOCKED_STATES])

    def _gate(self, entity_key: HotWaterKey, value_key: HotWaterKey) -> float | None:
        """A threshold that only applies with its entity configured."""
        return self._float(value_key) if self.entity(entity_key) else None

    def hot_water_config(
        self, numbers: Mapping[HotWaterNumber, float]
    ) -> HotWaterConfig:
        tank_gate = self.data.get(K.TANK_GATE_C)
        return HotWaterConfig(
            normal_c=numbers[N.NORMAL],
            surplus_c=numbers[N.SURPLUS],
            boost_c=numbers[N.BOOST],
            legionella_c=numbers[N.LEGIONELLA],
            night_c=numbers[N.NIGHT],
            window_c=numbers[N.WINDOW],
            heating_window_c=numbers[N.HEATING_WINDOW],
            heating_night_c=numbers[N.HEATING_NIGHT],
            heating_night_end=self._time(K.HEATING_NIGHT_END),
            boost_max_s=self._float(K.BOOST_MAX_H) * 3600,
            night_enabled=self._bool(K.NIGHT_ENABLED),
            night_start=self._time(K.NIGHT_START),
            night_end=self._time(K.NIGHT_END),
            window_enabled=self._bool(K.WINDOW_ENABLED),
            window_start=self._time(K.WINDOW_START),
            window_end=self._time(K.WINDOW_END),
            forced_charge=self._bool(K.FORCED_CHARGE),
            reheat_hysteresis_k=self.reheat_hysteresis_k,
            max_setpoint_c=self._float(K.MAX_SETPOINT_C),
            grid_lower_w=self._float(K.GRID_LOWER_W),
            grid_upper_w=self._float(K.GRID_UPPER_W),
            grid_release_w=self._float(K.GRID_RELEASE_W),
            grid_stable_s=self._float(K.GRID_STABLE_MIN) * 60,
            battery_soc_min=self._gate(K.BATTERY_SOC_ENTITY, K.BATTERY_SOC_MIN),
            discharge_max_w=self._gate(K.BATTERY_DISCHARGE_ENTITY, K.DISCHARGE_MAX_W),
            discharge_stable_s=self._float(K.DISCHARGE_STABLE_MIN) * 60,
            outdoor_lower_c=self._float(K.OUTDOOR_LOWER_C),
            outdoor_upper_c=self._float(K.OUTDOOR_UPPER_C),
            tank_gate_c=float(tank_gate) if tank_gate is not None else None,
            min_hold_s=self._float(K.MIN_HOLD_MIN) * 60,
            manual_hold_s=self._float(K.MANUAL_HOLD_MIN) * 60,
        )

    def legionella_config(self) -> LegionellaConfig:
        return LegionellaConfig(
            weekdays=frozenset(
                WEEKDAYS.index(day) for day in self.data[K.LEGIONELLA_WEEKDAYS]
            ),
            start=self._time(K.LEGIONELLA_START),
            fallback=_optional_time(self.legionella_fallback),
            retry_window_min=self._float(K.LEGIONELLA_RETRY_MIN),
            forecast_above=self._bool(K.FORECAST_ABOVE),
            forecast_threshold=self._float(K.FORECAST_THRESHOLD),
            outlet_threshold_c=self._float(K.OUTLET_THRESHOLD_C),
            return_threshold_c=self._gate(K.RETURN_ENTITY, K.RETURN_THRESHOLD_C),
            hold_min=self._float(K.LEGIONELLA_HOLD_MIN),
            verify_timeout_min=self._float(K.LEGIONELLA_TIMEOUT_MIN),
        )

    def circulation_config(self) -> CirculationConfig:
        return CirculationConfig(
            peak1_start=self._time(K.PEAK1_START),
            peak1_end=self._time(K.PEAK1_END),
            peak2_start=self._time(K.PEAK2_START),
            peak2_end=self._time(K.PEAK2_END),
            peak_start_below_c=self._float(K.PEAK_START_BELOW_C),
            offpeak_start_below_c=self._float(K.OFFPEAK_START_BELOW_C),
            stop_delta_k=self._float(K.STOP_DELTA_K),
            stop_temperature_c=self._float(K.STOP_TEMPERATURE_C),
            max_run_min=self._float(K.MAX_RUN_MIN),
            min_pause_min=self._float(K.MIN_PAUSE_MIN),
            fallback_on_min=self._float(K.FALLBACK_ON_MIN),
            fallback_off_min=self._float(K.FALLBACK_OFF_MIN),
            draw_overrides_gates=self._bool(K.DRAW_OVERRIDES_GATES),
            release_enabled=self._bool(K.RELEASE_ENABLED),
            release_temperature_c=self._float(K.RELEASE_TEMPERATURE_C),
            release_min=self._float(K.RELEASE_MIN),
        )

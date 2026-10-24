from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil

from ....const import DesignTemperatureSystem, HeatEmitterType, PanelRadiatorType
from ..math_helper import clamp, round_to_step
from ..types import RoomThermalConfig, TrvConfig

RADIATOR_EXPONENTS: dict[PanelRadiatorType, float] = {
    PanelRadiatorType.TYPE_10: 1.25,
    PanelRadiatorType.TYPE_11: 1.3,
    PanelRadiatorType.TYPE_21: 1.3,
    PanelRadiatorType.TYPE_22: 1.3,
    PanelRadiatorType.TYPE_33: 1.35,
}

DEFAULT_EMITTER_EXPONENT = 1.3

REFERENCE_HEIGHT_MM = 600

PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER: dict[PanelRadiatorType, float] = {
    PanelRadiatorType.TYPE_10: 500,
    PanelRadiatorType.TYPE_11: 1000,
    PanelRadiatorType.TYPE_21: 1300,
    PanelRadiatorType.TYPE_22: 1700,
    PanelRadiatorType.TYPE_33: 2400,
}

HEIGHT_SCALING_EXPONENT = 0.8

# Water content (l) and steel mass (kg) per metre of a 600 mm panel radiator,
# typical catalogue values; both scale linearly with the height.
PANEL_RADIATOR_WATER_L_PER_METER: dict[PanelRadiatorType, float] = {
    PanelRadiatorType.TYPE_10: 2.8,
    PanelRadiatorType.TYPE_11: 3.0,
    PanelRadiatorType.TYPE_21: 5.3,
    PanelRadiatorType.TYPE_22: 5.9,
    PanelRadiatorType.TYPE_33: 8.6,
}
PANEL_RADIATOR_STEEL_KG_PER_METER: dict[PanelRadiatorType, float] = {
    PanelRadiatorType.TYPE_10: 11.0,
    PanelRadiatorType.TYPE_11: 13.5,
    PanelRadiatorType.TYPE_21: 22.0,
    PanelRadiatorType.TYPE_22: 27.5,
    PanelRadiatorType.TYPE_33: 40.5,
}
# Towel radiators hold far more water per watt than panels: per watt of
# nominal power (75/65).
TOWEL_RADIATOR_WATER_L_PER_W = 0.01
TOWEL_RADIATOR_STEEL_KG_PER_W = 0.02
WATER_HEAT_CAPACITY_J_PER_LK = 4186.0
STEEL_HEAT_CAPACITY_J_PER_KGK = 460.0
# Below this overtemperature the linearised emitter conductance would go to
# zero and the time constant to infinity.
MIN_TIME_CONSTANT_OVERTEMPERATURE_C = 5.0

MIN_RADIATOR_HEIGHT_MM = 150
MAX_RADIATOR_HEIGHT_MM = 1800

MIN_ACTIVE_TRV_TEMPERATURE_C = 18.0
MIN_ACTIVE_DEMAND = 0.05

DISTRIBUTION_ALPHA = 0.7

# The search starts this far below the room's flow threshold so a requirement
# below what the heat source delivers at minimum stays visible, and ends at the
# design flow temperature: the radiators are sized to cover the heat load there.
FLOW_SEARCH_BELOW_THRESHOLD_C = 10.0
FLOW_SEARCH_PRECISION_C = 0.1
FLOW_SEARCH_ROUND_STEP_C = 0.5


@dataclass
class _DesignTemperatures:
    flow_temperature_c: float
    overtemperature_c: float
    spread_c: float


DESIGN_SYSTEM_TEMPERATURES: dict[DesignTemperatureSystem, _DesignTemperatures] = {
    DesignTemperatureSystem.SYSTEM_75_65: _DesignTemperatures(75, 50, 10),
    DesignTemperatureSystem.SYSTEM_70_55: _DesignTemperatures(70, 42.5, 15),
    DesignTemperatureSystem.SYSTEM_55_45: _DesignTemperatures(55, 30, 10),
    DesignTemperatureSystem.SYSTEM_45_35: _DesignTemperatures(45, 20, 10),
    DesignTemperatureSystem.SYSTEM_35_30: _DesignTemperatures(35, 12.5, 5),
}


@dataclass
class _PreparedEmitter:
    trv: TrvConfig
    design_reference_power_w: float
    exponent: float
    heat_capacity_j_per_k: float
    distribution_weight: float = 0.0


class HeatEmitterModel:
    def __init__(self, thermal_config: RoomThermalConfig, trvs: list[TrvConfig]) -> None:
        self._design_temperatures = DESIGN_SYSTEM_TEMPERATURES[
            thermal_config.design_temperature_system
        ]
        self._room_heat_load_w = thermal_config.room_heat_load_w
        self._flow_search_max_c = self._design_temperatures.flow_temperature_c
        self.set_flow_threshold_c(thermal_config.flow_threshold_c)
        prepared = [self._create_prepared_emitter(trv) for trv in trvs]
        self._emitters = self._calculate_distribution_weights(prepared)
        self._heat_capacity_j_per_k = sum(e.heat_capacity_j_per_k for e in prepared)

    def _create_prepared_emitter(self, trv: TrvConfig) -> _PreparedEmitter:
        exponent = self._get_emitter_exponent(trv)
        table_reference_power_w = self._calculate_emitter_table_reference_power_w(trv)
        design_reference_power_w = self._convert_reference_power_to_design_system(
            table_reference_power_w, exponent
        )
        return _PreparedEmitter(
            trv=trv,
            design_reference_power_w=design_reference_power_w,
            exponent=exponent,
            heat_capacity_j_per_k=self._calculate_emitter_heat_capacity_j_per_k(trv),
        )

    @staticmethod
    def _calculate_emitter_heat_capacity_j_per_k(trv: TrvConfig) -> float:
        if trv.emitter_type == HeatEmitterType.PANEL:
            if trv.height_mm is None or trv.width_mm is None or trv.radiator_type is None:
                return 0.0
            meters = (trv.width_mm / 1000) * (
                clamp(trv.height_mm, MIN_RADIATOR_HEIGHT_MM, MAX_RADIATOR_HEIGHT_MM)
                / REFERENCE_HEIGHT_MM
            )
            water_l = PANEL_RADIATOR_WATER_L_PER_METER[trv.radiator_type] * meters
            steel_kg = PANEL_RADIATOR_STEEL_KG_PER_METER[trv.radiator_type] * meters
        elif trv.emitter_type == HeatEmitterType.TOWEL:
            nominal_power_w = trv.nominal_power_w or 0
            water_l = TOWEL_RADIATOR_WATER_L_PER_W * nominal_power_w
            steel_kg = TOWEL_RADIATOR_STEEL_KG_PER_W * nominal_power_w
        else:
            return 0.0
        return (
            water_l * WATER_HEAT_CAPACITY_J_PER_LK
            + steel_kg * STEEL_HEAT_CAPACITY_J_PER_KGK
        )

    @staticmethod
    def _get_emitter_exponent(trv: TrvConfig) -> float:
        if trv.emitter_type == HeatEmitterType.PANEL:
            return RADIATOR_EXPONENTS[trv.radiator_type or PanelRadiatorType.TYPE_22]
        return DEFAULT_EMITTER_EXPONENT

    def _calculate_emitter_table_reference_power_w(self, trv: TrvConfig) -> float:
        if trv.emitter_type == HeatEmitterType.PANEL:
            return self._calculate_panel_radiator_table_reference_power_w(trv)
        if trv.emitter_type == HeatEmitterType.TOWEL:
            return trv.nominal_power_w or 0
        return 0

    def _calculate_panel_radiator_table_reference_power_w(self, trv: TrvConfig) -> float:
        if trv.height_mm is None or trv.width_mm is None or trv.radiator_type is None:
            return 0
        scaled_power_per_meter = self._scale_panel_radiator_power_per_meter(
            trv.height_mm, trv.radiator_type
        )
        return round_to_step(scaled_power_per_meter * (trv.width_mm / 1000), 0.01)

    @staticmethod
    def _scale_panel_radiator_power_per_meter(
        height_mm: float, radiator_type: PanelRadiatorType
    ) -> float:
        reference_power = PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER.get(radiator_type, 0)
        clamped_height_mm = clamp(
            height_mm, MIN_RADIATOR_HEIGHT_MM, MAX_RADIATOR_HEIGHT_MM
        )
        return reference_power * (
            clamped_height_mm / REFERENCE_HEIGHT_MM
        ) ** HEIGHT_SCALING_EXPONENT

    def _convert_reference_power_to_design_system(
        self, table_reference_power_w: float, exponent: float
    ) -> float:
        if table_reference_power_w <= 0 or self._design_temperatures.overtemperature_c <= 0:
            return 0
        reference_overtemperature_c = DESIGN_SYSTEM_TEMPERATURES[
            DesignTemperatureSystem.SYSTEM_75_65
        ].overtemperature_c
        return table_reference_power_w * (
            self._design_temperatures.overtemperature_c / reference_overtemperature_c
        ) ** exponent

    @staticmethod
    def _calculate_distribution_weights(
        emitters: list[_PreparedEmitter],
    ) -> list[_PreparedEmitter]:
        total_reference_power = sum(e.design_reference_power_w for e in emitters)
        if total_reference_power <= 0:
            return [replace(e, distribution_weight=0.0) for e in emitters]

        weighted_distribution = [
            (e.design_reference_power_w / total_reference_power) ** DISTRIBUTION_ALPHA
            for e in emitters
        ]
        weighted_sum = sum(weighted_distribution)

        return [
            replace(e, distribution_weight=weighted_distribution[i] / weighted_sum)
            for i, e in enumerate(emitters)
        ]

    def calculate_available_heating_power_w(
        self,
        room_temperature_c: float,
        flow_temperature_c: float | None = None,
        spread_c: float | None = None,
    ) -> float:
        effective_flow_temperature_c = (
            flow_temperature_c
            if flow_temperature_c is not None
            else self._design_temperatures.flow_temperature_c
        )
        effective_spread_c = (
            spread_c if spread_c is not None else self._design_temperatures.spread_c
        )
        return_temperature_c = effective_flow_temperature_c - effective_spread_c
        current_mean_temperature_c = (
            effective_flow_temperature_c + return_temperature_c
        ) / 2
        current_overtemperature_c = current_mean_temperature_c - room_temperature_c

        if current_overtemperature_c <= 0:
            return 0.0

        total = sum(
            emitter.design_reference_power_w
            * (
                current_overtemperature_c
                / self._design_temperatures.overtemperature_c
            )
            ** emitter.exponent
            for emitter in self._emitters
        )
        return round_to_step(total, 0.01)

    @property
    def heat_capacity_j_per_k(self) -> float:
        return self._heat_capacity_j_per_k

    def time_constant_s(
        self,
        room_temperature_c: float,
        flow_temperature_c: float | None,
        demand_pct: float,
        heating_up: bool,
    ) -> float:
        """How fast the radiators follow a change of the requested power.

        Cooling down, the radiator mass only gives off heat through its
        surface: capacity over the linearised emitter conductance n * P / dT.
        Heating up, the water flowing in brings heat as well; with the valve
        at the demand share of its design flow that adds demand * P / spread
        at design conditions. The TRV's own valve travel is not modelled.
        """
        if self._heat_capacity_j_per_k <= 0:
            return 0.0
        effective_flow_temperature_c = (
            flow_temperature_c
            if flow_temperature_c is not None
            else self._design_temperatures.flow_temperature_c
        )
        overtemperature_c = max(
            MIN_TIME_CONSTANT_OVERTEMPERATURE_C,
            effective_flow_temperature_c
            - self._design_temperatures.spread_c / 2
            - room_temperature_c,
        )
        conductance_w_per_k = sum(
            emitter.exponent
            * emitter.design_reference_power_w
            * (overtemperature_c / self._design_temperatures.overtemperature_c)
            ** emitter.exponent
            / overtemperature_c
            for emitter in self._emitters
        )
        if heating_up and self._design_temperatures.spread_c > 0:
            conductance_w_per_k += (
                clamp(demand_pct / 100, 0, 1)
                * sum(e.design_reference_power_w for e in self._emitters)
                / self._design_temperatures.spread_c
            )
        if conductance_w_per_k <= 0:
            return 0.0
        return self._heat_capacity_j_per_k / conductance_w_per_k

    def calculate_target_temperatures(self, demand_pct: float) -> list[float]:
        demand = demand_pct / 100
        return [self._calculate_target_temperature(e, demand) for e in self._emitters]

    @staticmethod
    def _calculate_target_temperature(emitter: _PreparedEmitter, demand: float) -> float:
        trv = emitter.trv
        if demand < MIN_ACTIVE_DEMAND:
            return trv.min_target_temperature_c

        weighted_demand = clamp(demand * emitter.distribution_weight, 0, 1)
        effective_demand = weighted_demand ** (1 / emitter.exponent)

        min_active_target_temperature = max(
            MIN_ACTIVE_TRV_TEMPERATURE_C, trv.min_target_temperature_c
        )
        target_temperature = min_active_target_temperature + effective_demand * (
            trv.max_target_temperature_c - min_active_target_temperature
        )

        return clamp(
            round_to_step(target_temperature, trv.target_temperature_step_c),
            trv.min_target_temperature_c,
            trv.max_target_temperature_c,
        )

    def set_flow_threshold_c(self, flow_threshold_c: float) -> None:
        """Move the search window with the heat source's minimum flow."""
        self._flow_search_min_c = flow_threshold_c - FLOW_SEARCH_BELOW_THRESHOLD_C

    @property
    def flow_search_min_c(self) -> float:
        return self._flow_search_min_c

    @property
    def flow_search_max_c(self) -> float:
        return self._flow_search_max_c

    @staticmethod
    def round_up_flow_temperature_c(flow_temperature_c: float) -> float:
        return (
            ceil(flow_temperature_c / FLOW_SEARCH_ROUND_STEP_C)
            * FLOW_SEARCH_ROUND_STEP_C
        )

    def part_load_spread_c(self, heating_power_w: float) -> float:
        if self._room_heat_load_w <= 0:
            return self._design_temperatures.spread_c
        return (
            self._design_temperatures.spread_c
            * heating_power_w
            / self._room_heat_load_w
        )

    def calculate_recommended_flow_temperature_c(
        self, required_heating_power_w: float, target_room_temperature_c: float
    ) -> float | None:
        if required_heating_power_w <= 0:
            return 0.0

        spread_c = self.part_load_spread_c(required_heating_power_w)

        low, high = self._flow_search_min_c, self._flow_search_max_c

        if (
            self.calculate_available_heating_power_w(
                target_room_temperature_c, high, spread_c
            )
            < required_heating_power_w
        ):
            return high

        optimal_flow = high
        while high - low > FLOW_SEARCH_PRECISION_C:
            mid = (low + high) / 2
            available_power_w = self.calculate_available_heating_power_w(
                target_room_temperature_c, mid, spread_c
            )
            if available_power_w >= required_heating_power_w:
                optimal_flow = mid
                high = mid
            else:
                low = mid

        return min(
            self.round_up_flow_temperature_c(optimal_flow), self._flow_search_max_c
        )

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from ....const import (
    DesignTemperatureSystem,
    HeatEmitterType,
    PanelRadiatorType,
)
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
OUTPUT_SEARCH_ITERATIONS = 40
# Relative step at which the radiator balance counts as solved.
OUTPUT_SEARCH_TOLERANCE = 1e-12
# Below this overtemperature the linearised emitter conductance would go to
# zero and the time constant to infinity.
MIN_TIME_CONSTANT_OVERTEMPERATURE_C = 5.0

MIN_RADIATOR_HEIGHT_MM = 150
MAX_RADIATOR_HEIGHT_MM = 1800

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
class _PowerTerms:
    flow_temperature_c: float
    # Per radiator: available power at the design spread, emitter exponent.
    emitter_powers_w: list[tuple[float, float]]


@dataclass
class _PreparedEmitter:
    trv: TrvConfig
    design_reference_power_w: float
    exponent: float
    heat_capacity_j_per_k: float


class HeatEmitterModel:
    def __init__(
        self, thermal_config: RoomThermalConfig, trvs: list[TrvConfig]
    ) -> None:
        self._design_temperatures = DESIGN_SYSTEM_TEMPERATURES[
            thermal_config.design_temperature_system
        ]
        self._flow_search_max_c = self._design_temperatures.flow_temperature_c
        self.set_flow_threshold_c(thermal_config.flow_threshold_c)
        self._emitters = [self._create_prepared_emitter(trv) for trv in trvs]
        self._heat_capacity_j_per_k = sum(
            e.heat_capacity_j_per_k for e in self._emitters
        )

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
            if (
                trv.height_mm is None
                or trv.width_mm is None
                or trv.radiator_type is None
            ):
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

    def _calculate_panel_radiator_table_reference_power_w(
        self, trv: TrvConfig
    ) -> float:
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
        reference_power = PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER.get(
            radiator_type, 0
        )
        clamped_height_mm = clamp(
            height_mm, MIN_RADIATOR_HEIGHT_MM, MAX_RADIATOR_HEIGHT_MM
        )
        return (
            reference_power
            * (clamped_height_mm / REFERENCE_HEIGHT_MM) ** HEIGHT_SCALING_EXPONENT
        )

    def _convert_reference_power_to_design_system(
        self, table_reference_power_w: float, exponent: float
    ) -> float:
        if (
            table_reference_power_w <= 0
            or self._design_temperatures.overtemperature_c <= 0
        ):
            return 0
        reference_overtemperature_c = DESIGN_SYSTEM_TEMPERATURES[
            DesignTemperatureSystem.SYSTEM_75_65
        ].overtemperature_c
        return (
            table_reference_power_w
            * (
                self._design_temperatures.overtemperature_c
                / reference_overtemperature_c
            )
            ** exponent
        )

    def calculate_available_heating_power_w(
        self,
        room_temperature_c: float,
        flow_temperature_c: float | None = None,
    ) -> float:
        """Output with the valves fully open, at the design spread.

        Every heat figure the model uses rests on this: the valve power, the
        heat balance and the replay the factors learn from, the hold and
        recovery flows. A learned heat-loss factor fits this assumption, so a
        flow requirement computed with another spread would no longer match
        what the room was seen to need.
        """
        effective_flow_temperature_c = (
            flow_temperature_c
            if flow_temperature_c is not None
            else self._design_temperatures.flow_temperature_c
        )
        current_overtemperature_c = (
            effective_flow_temperature_c
            - self._design_temperatures.spread_c / 2
            - room_temperature_c
        )

        if current_overtemperature_c <= 0:
            return 0.0

        total = sum(
            self._emitter_power_w(emitter, current_overtemperature_c)
            for emitter in self._emitters
        )
        return round_to_step(total, 0.01)

    def _emitter_power_w(
        self, emitter: _PreparedEmitter, overtemperature_c: float
    ) -> float:
        return (
            emitter.design_reference_power_w
            * (overtemperature_c / self._design_temperatures.overtemperature_c)
            ** emitter.exponent
        )

    def calculate_measured_heating_power_w(
        self,
        room_temperature_c: float,
        flow_temperature_c: float | None,
        valve_openings_pct: list[float | None],
        demand_pct: float,
    ) -> float:
        """Heat the radiators give off at the valve openings the TRVs report.

        Each radiator gives its available power times the share that the
        reported opening lets through (see power_fraction_at_opening). A
        radiator without a reported opening falls back to demand_pct.
        """
        terms = self._power_terms(room_temperature_c, flow_temperature_c)
        if terms is None:
            return 0.0
        return self._sum_power(
            terms,
            room_temperature_c,
            [_at(valve_openings_pct, index) for index in range(len(self._emitters))],
            demand_pct,
            {},
        )

    def _power_terms(
        self, room_temperature_c: float, flow_temperature_c: float | None
    ) -> _PowerTerms | None:
        """What a power sum needs that does not depend on the openings."""
        effective_flow_temperature_c = (
            flow_temperature_c
            if flow_temperature_c is not None
            else self._design_temperatures.flow_temperature_c
        )
        overtemperature_c = (
            effective_flow_temperature_c
            - self._design_temperatures.spread_c / 2
            - room_temperature_c
        )
        if overtemperature_c <= 0:
            return None
        return _PowerTerms(
            effective_flow_temperature_c,
            [
                (self._emitter_power_w(emitter, overtemperature_c), emitter.exponent)
                for emitter in self._emitters
            ],
        )

    def _sum_power(
        self,
        terms: _PowerTerms,
        room_temperature_c: float,
        openings_pct: list[float | None],
        demand_pct: float,
        full_outputs: dict[float, float],
    ) -> float:
        """Available power per radiator times the share its opening gives.

        full_outputs memoises the full-open output per exponent; the caller
        may keep it across sums at the same flow and room temperature. Equal
        openings and exponents share one fraction within a sum.
        """
        fractions: dict[tuple[float, float], float] = {}
        total = 0.0
        for (power_w, exponent), opening_pct in zip(
            terms.emitter_powers_w, openings_pct, strict=True
        ):
            if opening_pct is None:
                fraction = clamp(demand_pct / 100, 0, 1)
            else:
                key = (opening_pct, exponent)
                if key not in fractions:
                    fractions[key] = self._fraction_at_opening(
                        opening_pct,
                        terms.flow_temperature_c,
                        room_temperature_c,
                        exponent,
                        full_outputs,
                    )
                fraction = fractions[key]
            total += power_w * fraction
        return round_to_step(total, 0.01)

    def planned_heating_power_w(
        self,
        room_temperature_c: float,
        flow_temperature_c: float | None,
        opening_pct: float,
    ) -> float:
        """Heat the radiators give off with every valve at this opening.

        The same curve the observer and the learner apply to a reported
        opening, so simulation, estimate and learning agree on what an
        opening delivers. A TRV without a valve entity is never measured that way
        and stays linear, as its measured power does.
        """
        if opening_pct >= 100:
            # Speed shortcut with the same result: fully open the curve gives
            # exactly the available power, and the recovery search simulates
            # this case for hours.
            return self.calculate_available_heating_power_w(
                room_temperature_c, flow_temperature_c
            )
        terms = self._power_terms(room_temperature_c, flow_temperature_c)
        if terms is None:
            return 0.0
        return self._planned_power_from_terms(
            terms, room_temperature_c, opening_pct, {}
        )

    def _planned_power_from_terms(
        self,
        terms: _PowerTerms,
        room_temperature_c: float,
        opening_pct: float,
        full_outputs: dict[float, float],
    ) -> float:
        return self._sum_power(
            terms,
            room_temperature_c,
            [
                opening_pct if emitter.trv.valve_position_configured else None
                for emitter in self._emitters
            ],
            opening_pct,
            full_outputs,
        )

    def full_output_fractions(
        self, flow_temperature_c: float | None, room_temperature_c: float
    ) -> list[float | None]:
        """Per radiator, its output with the valve fully open, as a share of
        its design output, at this flow and room temperature.

        From the radiator's own balance, not the design spread: the radiator
        check asks whether the water reaching the TRV is warm enough to show,
        a physical question. Its 30 % gate was set on this figure; the design
        spread gives about 21 % instead of 32 % at a 35 °C flow and would
        switch the check off at the flows the house runs on.
        """
        if flow_temperature_c is None:
            return [None] * len(self._emitters)
        return [
            self._relative_output(
                1.0, flow_temperature_c, room_temperature_c, emitter.exponent
            )
            for emitter in self._emitters
        ]

    def power_fraction_at_opening(
        self,
        opening_pct: float,
        flow_temperature_c: float,
        room_temperature_c: float,
        exponent: float = DEFAULT_EMITTER_EXPONENT,
    ) -> float:
        """Share of full output at a valve opening, from the radiator itself.

        The flow is taken as proportional to the valve opening. With less
        water the radiator cools it further, its mean temperature drops less
        than the flow, and the output stays high: at a 40 °C flow a 10 %
        opening still gives about 44 % of full output, 50 % about 87 %.
        """
        return self._fraction_at_opening(
            opening_pct, flow_temperature_c, room_temperature_c, exponent, {}
        )

    def _fraction_at_opening(
        self,
        opening_pct: float,
        flow_temperature_c: float,
        room_temperature_c: float,
        exponent: float,
        full_outputs: dict[float, float],
    ) -> float:
        """power_fraction_at_opening, the full-open output memoised per
        exponent for one flow and room temperature."""
        fraction = clamp(opening_pct / 100, 0, 1)
        if fraction <= 0:
            return 0.0
        if exponent not in full_outputs:
            full_outputs[exponent] = self._relative_output(
                1.0, flow_temperature_c, room_temperature_c, exponent
            )
        full = full_outputs[exponent]
        if full <= 0:
            return 0.0
        return min(
            1.0,
            self._relative_output(
                fraction, flow_temperature_c, room_temperature_c, exponent
            )
            / full,
        )

    def _relative_output(
        self,
        flow_fraction: float,
        flow_temperature_c: float,
        room_temperature_c: float,
        exponent: float,
    ) -> float:
        """Output relative to design output at a share of the design flow.

        Solves the radiator's own balance: the water gives off
        output = flow * cp * (flow - return), the surface gives off
        output = design * (mean overtemperature / design overtemperature)^n.
        With the design spread s at design flow, the drop at flow share f
        and relative output x is x * s / f.
        """
        spread_c = self._design_temperatures.spread_c
        design_overtemperature_c = self._design_temperatures.overtemperature_c
        inlet_overtemperature_c = flow_temperature_c - room_temperature_c
        if inlet_overtemperature_c <= 0 or design_overtemperature_c <= 0:
            return 0.0
        # Newton on f(x) = x - ((inlet - k x) / design)^n, k = s / (2 f).
        # f rises and is concave, so from x = 0 (f < 0) every step lands at
        # or below the root and the mean overtemperature stays positive. It
        # agrees with a 40-step bisection to far below 1e-6.
        drop_per_output_c = spread_c / (2 * flow_fraction)
        output = 0.0
        for _ in range(OUTPUT_SEARCH_ITERATIONS):
            relative_mean = (
                inlet_overtemperature_c - drop_per_output_c * output
            ) / design_overtemperature_c
            surface_output = relative_mean**exponent
            slope = 1 + (
                exponent
                * drop_per_output_c
                / design_overtemperature_c
                * relative_mean ** (exponent - 1)
            )
            step = (surface_output - output) / slope
            output += step
            if step <= OUTPUT_SEARCH_TOLERANCE * output:
                break
        return output

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

    def calculate_recommended_flow_temperature_c(
        self,
        required_heating_power_w: float,
        target_room_temperature_c: float,
        *,
        round_up: bool = True,
    ) -> float | None:
        """Lowest flow at which the open valves give off the required power.

        Same design spread as the valve power: with a part-load spread the
        hold flow came out about 4 K below the flow the room was seen to need
        (Esszimmer, 01.11.2026: 31.5 instead of about 35.5 °C).
        """
        if required_heating_power_w <= 0:
            return 0.0

        low, high = self._flow_search_min_c, self._flow_search_max_c

        if (
            self.calculate_available_heating_power_w(target_room_temperature_c, high)
            < required_heating_power_w
        ):
            return high

        optimal_flow = high
        while high - low > FLOW_SEARCH_PRECISION_C:
            mid = (low + high) / 2
            available_power_w = self.calculate_available_heating_power_w(
                target_room_temperature_c, mid
            )
            if available_power_w >= required_heating_power_w:
                optimal_flow = mid
                high = mid
            else:
                low = mid

        if not round_up:
            return min(optimal_flow, self._flow_search_max_c)
        return min(
            self.round_up_flow_temperature_c(optimal_flow), self._flow_search_max_c
        )


def _at[T](values: Sequence[T | None] | None, index: int) -> T | None:
    if values is None or index >= len(values):
        return None
    return values[index]

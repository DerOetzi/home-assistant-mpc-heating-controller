from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import ceil

from ....const import (
    DesignTemperatureSystem,
    HeatEmitterType,
    PanelRadiatorType,
    TrvSetpointMode,
)
from ..math_helper import clamp, round_to_step
from ..types import RoomThermalConfig, TrvConfig
from .valve import ValveCharacteristic

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

MIN_ACTIVE_TRV_TEMPERATURE_C = 18.0
MIN_ACTIVE_DEMAND_PCT = 5.0
# A shut valve opens only from this demand on; an open one stays open down to
# MIN_ACTIVE_DEMAND_PCT. The smallest demand step otherwise opened and shut a
# valve every few minutes for a room that hardly needs heat. The demand is the
# target valve opening, so both thresholds are openings.
OPEN_ACTIVE_DEMAND_PCT = 10.0

# Relative TRV setpoints: the target sits this far above the TRV's own reading,
# from the smallest active demand to full demand. The TRV regulates its valve
# on that reading, so a small step opens it only partly, where an absolute
# target of 20 °C or more opened it almost fully on a few percent of demand.
# A Danfoss Ally freshly opened about 55 % at 1.1-1.5 K and about 90 % from
# 2.4 K on; at the old 0.5 K minimum (0.5-1 K after rounding up to its 0.5 K
# step) the smallest demand thus opened it by half and the room pulsed. The
# minimum now leaves 0.1-0.6 K, so a small demand can keep a valve slightly
# open.
RELATIVE_MIN_OFFSET_C = 0.1
RELATIVE_MAX_OFFSET_C = 3.0

DISTRIBUTION_ALPHA = 0.7

# The search starts this far below the room's flow threshold so a requirement
# below what the heat source delivers at minimum stays visible, and ends at the
# design flow temperature: the radiators are sized to cover the heat load there.
FLOW_SEARCH_BELOW_THRESHOLD_C = 10.0
FLOW_SEARCH_PRECISION_C = 0.1
FLOW_SEARCH_ROUND_STEP_C = 0.5
# The demand search runs in 5 % steps; half a percent is well below that.
OPENING_SEARCH_PRECISION_PCT = 0.5


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
    distribution_weight: float = 0.0


class HeatEmitterModel:
    def __init__(
        self, thermal_config: RoomThermalConfig, trvs: list[TrvConfig]
    ) -> None:
        self._design_temperatures = DESIGN_SYSTEM_TEMPERATURES[
            thermal_config.design_temperature_system
        ]
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
        radiator without a reported opening falls back to the room's demand,
        the same assumption the planning makes.
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
        opening, so planning, estimate and learning agree on what an opening
        delivers. A TRV without a valve entity is never measured that way
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

    def opening_for_power_pct(
        self,
        room_temperature_c: float,
        flow_temperature_c: float | None,
        power_w: float,
    ) -> float:
        """Smallest valve opening at which the radiators give off power_w.

        Full opening when even that is not enough, or the flow gives nothing.
        """
        if power_w <= 0:
            return 0.0
        # Speed shortcut with the same result: the bisection would end at
        # 100 % as well, after eight steps.
        if (
            self.planned_heating_power_w(room_temperature_c, flow_temperature_c, 100)
            < power_w
        ):
            return 100.0
        terms = self._power_terms(room_temperature_c, flow_temperature_c)
        if terms is None:
            return 100.0
        # Flow, room and radiators stay; only the opening moves.
        full_outputs: dict[float, float] = {}
        low, high = 0.0, 100.0
        while high - low > OPENING_SEARCH_PRECISION_PCT:
            mid = (low + high) / 2
            if (
                self._planned_power_from_terms(
                    terms, room_temperature_c, mid, full_outputs
                )
                >= power_w
            ):
                high = mid
            else:
                low = mid
        return high

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
        # replaced a 40-step bisection, the planner's main cost; both agree
        # to far below 1e-6.
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

    def calculate_target_temperatures(
        self,
        demand_pct: float,
        mode: TrvSetpointMode = TrvSetpointMode.ABSOLUTE,
        trv_temperatures: list[float | None] | None = None,
        current_targets: list[float | None] | None = None,
        characteristics: list[ValveCharacteristic | None] | None = None,
    ) -> list[float]:
        """Per TRV the setpoint for a demand, which is the target opening.

        In relative mode a TRV with a valve curve (learned, else its
        profile's seed) gets the offset that curve needs for the opening,
        the same opening for every radiator. A TRV without one keeps the
        weighted mapping as a last resort.
        """
        demand = demand_pct / 100
        targets = []
        for index, emitter in enumerate(self._emitters):
            trv = emitter.trv
            local_c = _at(trv_temperatures, index)
            current_target_c = _at(current_targets, index)
            characteristic = _at(characteristics, index)
            if demand_pct <= 0 or below_activation(
                demand_pct, _activation_threshold_pct(trv, current_target_c)
            ):
                targets.append(trv.min_target_temperature_c)
            elif mode == TrvSetpointMode.RELATIVE and local_c is not None:
                if characteristic is not None:
                    # The nearest step: rounding up moved a small offset by
                    # up to a whole step, and a Danfoss already opens about
                    # 45 % at 0.5 K.
                    offset_c = characteristic.offset_c(demand_pct)
                    nearest = True
                else:
                    offset_c = self._weighted_offset_c(emitter, demand)
                    nearest = False
                targets.append(
                    _relative_target_c(
                        trv, offset_c, local_c, current_target_c, nearest=nearest
                    )
                )
            else:
                targets.append(self._calculate_target_temperature(emitter, demand))
        return targets

    def activation_threshold_pct(
        self, current_targets: list[float | None] | None
    ) -> float:
        """Smallest demand above 0 that opens the room's valves as planned.

        A demand below it writes the minimum target to a shut TRV (see
        calculate_target_temperatures), so planning it would count heat that
        never arrives. Any shut TRV counts, as it would get none.
        """
        return max(
            (
                _activation_threshold_pct(emitter.trv, _at(current_targets, index))
                for index, emitter in enumerate(self._emitters)
            ),
            default=MIN_ACTIVE_DEMAND_PCT,
        )

    @staticmethod
    def _weighted_offset_c(emitter: _PreparedEmitter, demand: float) -> float:
        """Offset growing with the radiator's weighted share of the demand."""
        weighted_demand = clamp(demand * emitter.distribution_weight, 0, 1)
        effective_demand = weighted_demand ** (1 / emitter.exponent)
        return RELATIVE_MIN_OFFSET_C + effective_demand * (
            RELATIVE_MAX_OFFSET_C - RELATIVE_MIN_OFFSET_C
        )

    @staticmethod
    def _calculate_target_temperature(
        emitter: _PreparedEmitter, demand: float
    ) -> float:
        trv = emitter.trv
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

    def calculate_recommended_flow_temperature_c(
        self, required_heating_power_w: float, target_room_temperature_c: float
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

        return min(
            self.round_up_flow_temperature_c(optimal_flow), self._flow_search_max_c
        )


def _at[T](values: Sequence[T | None] | None, index: int) -> T | None:
    if values is None or index >= len(values):
        return None
    return values[index]


def below_activation(demand_pct: float, threshold_pct: float) -> bool:
    """A demand above 0 that the TRVs cannot follow: they get their minimum
    target for it, the valves stay or go shut."""
    return 0 < demand_pct < threshold_pct


def _relative_target_c(
    trv: TrvConfig,
    offset_c: float,
    local_c: float,
    current_target_c: float | None,
    nearest: bool,
) -> float:
    """The TRV's reading plus an offset, on the TRV's step.

    Rounded to the nearest step or up, but at least the first step above
    the reading plus the minimum offset, so the target always lies above
    the reading. The current target is kept while it is still above the
    reading and within one step of the new one: the reading moves by
    tenths while the radiator heats, and every write wakes the TRV.
    """
    step_c = trv.target_temperature_step_c
    raw_steps = (local_c + offset_c) / step_c
    target_c = clamp(
        max(
            ceil((local_c + RELATIVE_MIN_OFFSET_C) / step_c) * step_c,
            (round(raw_steps) if nearest else ceil(raw_steps)) * step_c,
        ),
        trv.min_target_temperature_c,
        trv.max_target_temperature_c,
    )
    if (
        current_target_c is not None
        and current_target_c > local_c
        and abs(current_target_c - target_c) <= step_c
    ):
        return current_target_c
    return target_c


def _activation_threshold_pct(trv: TrvConfig, current_target_c: float | None) -> float:
    """Demand a TRV needs to be open: more to open a shut valve than to stay."""
    is_open = (
        current_target_c is not None and current_target_c > trv.min_target_temperature_c
    )
    return MIN_ACTIVE_DEMAND_PCT if is_open else OPEN_ACTIVE_DEMAND_PCT

import pytest

from heating_controller.const import (
    DesignTemperatureSystem,
    HeatEmitterType,
    PanelRadiatorType,
)
from heating_controller.controller.mpc.math_helper import round_to_step
from heating_controller.controller.mpc.models.emitter import (
    HEIGHT_SCALING_EXPONENT,
    MAX_RADIATOR_HEIGHT_MM,
    MIN_RADIATOR_HEIGHT_MM,
    PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER,
    REFERENCE_HEIGHT_MM,
    HeatEmitterModel,
)
from heating_controller.controller.mpc.types import RoomThermalConfig, TrvConfig


def make_model(trvs, design_temperature_system=DesignTemperatureSystem.SYSTEM_55_45):
    return HeatEmitterModel(
        RoomThermalConfig(design_temperature_system=design_temperature_system), trvs
    )


def _expected_power_per_meter(radiator_type, height_mm):
    clamped_height_mm = min(
        max(height_mm, MIN_RADIATOR_HEIGHT_MM), MAX_RADIATOR_HEIGHT_MM
    )
    return PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER[radiator_type] * (
        clamped_height_mm / REFERENCE_HEIGHT_MM
    ) ** HEIGHT_SCALING_EXPONENT


def _available_power_at_design_delta(radiator_type, width_mm, height_mm):
    model = make_model(
        [
            TrvConfig(
                name="trv1",
                radiator_type=radiator_type,
                width_mm=width_mm,
                height_mm=height_mm,
            )
        ],
        design_temperature_system=DesignTemperatureSystem.SYSTEM_75_65,
    )
    return model.calculate_available_heating_power_w(20)


def test_available_power_is_zero_when_room_at_or_above_mean_temperature():
    model = make_model([TrvConfig(name="trv1")])
    assert model.calculate_available_heating_power_w(50, flow_temperature_c=55) == 0


def test_available_power_increases_with_lower_room_temperature():
    model = make_model([TrvConfig(name="trv1")])
    power_at_20 = model.calculate_available_heating_power_w(20, flow_temperature_c=55)
    power_at_10 = model.calculate_available_heating_power_w(10, flow_temperature_c=55)
    assert power_at_10 > power_at_20 > 0


def test_target_temperature_at_zero_demand_is_min_target():
    model = make_model(
        [TrvConfig(name="trv1", min_target_temperature_c=6, max_target_temperature_c=28)]
    )
    assert model.calculate_target_temperatures(0) == [6]


def test_target_temperature_at_full_demand_is_max_target():
    model = make_model(
        [TrvConfig(name="trv1", min_target_temperature_c=6, max_target_temperature_c=28)]
    )
    assert model.calculate_target_temperatures(100) == [28]


def test_two_trvs_split_demand_by_distribution_weight():
    big = TrvConfig(
        name="big",
        radiator_type=PanelRadiatorType.TYPE_22,
        width_mm=1400,
        height_mm=600,
    )
    small = TrvConfig(
        name="small",
        radiator_type=PanelRadiatorType.TYPE_22,
        width_mm=500,
        height_mm=600,
    )
    model = make_model([big, small])
    targets = model.calculate_target_temperatures(80)
    assert targets[0] >= targets[1]


def test_towel_radiator_uses_nominal_power_w():
    model = make_model([TrvConfig(name="towel", emitter_type=HeatEmitterType.TOWEL, nominal_power_w=600)])
    power = model.calculate_available_heating_power_w(20, flow_temperature_c=55)
    assert power > 0


def test_recommended_flow_temperature_increases_with_required_power():
    model = make_model([TrvConfig(name="trv1")])
    low_power_flow = model.calculate_recommended_flow_temperature_c(200, 20)
    high_power_flow = model.calculate_recommended_flow_temperature_c(800, 20)
    assert high_power_flow > low_power_flow


def test_recommended_flow_temperature_zero_when_no_power_required():
    model = make_model([TrvConfig(name="trv1")])
    assert model.calculate_recommended_flow_temperature_c(0, 20) == 0


def test_recommended_flow_uses_part_load_spread():
    """At part load the spread collapses, so less flow is needed than the
    design spread implies. Pinning this against an explicit design-spread
    calculation keeps the two directions from silently converging again."""
    model = HeatEmitterModel(
        RoomThermalConfig(room_heat_load_w=1200), [TrvConfig(name="trv1")]
    )
    required_w = 300

    flow_c = model.calculate_recommended_flow_temperature_c(required_w, 20)

    assert model.calculate_available_heating_power_w(20, flow_c, 2.5) >= required_w
    assert model.calculate_available_heating_power_w(20, flow_c) < required_w


def test_forward_direction_keeps_design_spread():
    """The valves-throttling direction must not inherit the part-load spread."""
    model = HeatEmitterModel(
        RoomThermalConfig(room_heat_load_w=1200), [TrvConfig(name="trv1")]
    )
    assert model.calculate_available_heating_power_w(
        20, 45
    ) == model.calculate_available_heating_power_w(20, 45, 10.0)


def test_panel_radiator_at_reference_height_matches_the_table_value():
    power = _available_power_at_design_delta(PanelRadiatorType.TYPE_10, 1000, 600)
    assert power == PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER[PanelRadiatorType.TYPE_10]


@pytest.mark.parametrize("height_mm", [300, 450, 900])
def test_panel_radiator_height_scaling_follows_the_power_law(height_mm):
    power = _available_power_at_design_delta(PanelRadiatorType.TYPE_22, 1000, height_mm)
    expected = round_to_step(
        _expected_power_per_meter(PanelRadiatorType.TYPE_22, height_mm), 0.01
    )
    assert power == expected


def test_panel_radiator_height_below_minimum_is_clamped():
    power = _available_power_at_design_delta(PanelRadiatorType.TYPE_22, 1000, 50)
    expected = round_to_step(
        _expected_power_per_meter(PanelRadiatorType.TYPE_22, MIN_RADIATOR_HEIGHT_MM), 0.01
    )
    assert power == expected


def test_panel_radiator_height_above_maximum_is_clamped():
    power = _available_power_at_design_delta(PanelRadiatorType.TYPE_22, 1000, 3000)
    expected = round_to_step(
        _expected_power_per_meter(PanelRadiatorType.TYPE_22, MAX_RADIATOR_HEIGHT_MM), 0.01
    )
    assert power == expected


@pytest.mark.parametrize(
    ("design_temperature_system", "design_flow_c"),
    [
        (DesignTemperatureSystem.SYSTEM_55_45, 55.0),
        (DesignTemperatureSystem.SYSTEM_45_35, 45.0),
    ],
)
def test_recommended_flow_is_capped_at_the_design_flow_temperature(
    design_temperature_system, design_flow_c
):
    model = make_model([TrvConfig(name="trv1")], design_temperature_system)
    assert model.flow_search_max_c == design_flow_c
    assert model.calculate_recommended_flow_temperature_c(100_000, 20) == design_flow_c


def test_flow_search_starts_ten_kelvin_below_the_flow_threshold():
    model = HeatEmitterModel(
        RoomThermalConfig(flow_threshold_c=32.0), [TrvConfig(name="trv1")]
    )
    assert model.flow_search_min_c == 22.0

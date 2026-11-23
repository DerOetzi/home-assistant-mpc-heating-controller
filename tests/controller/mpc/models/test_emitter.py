import pytest
from heating_controller.const import (
    Construction,
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
        RoomThermalConfig(
            design_temperature_system=design_temperature_system,
            room_area_m2=90,
            construction=Construction.VERY_LIGHT,
        ),
        trvs,
    )


def _expected_power_per_meter(radiator_type, height_mm):
    clamped_height_mm = min(
        max(height_mm, MIN_RADIATOR_HEIGHT_MM), MAX_RADIATOR_HEIGHT_MM
    )
    return (
        PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER[radiator_type]
        * (clamped_height_mm / REFERENCE_HEIGHT_MM) ** HEIGHT_SCALING_EXPONENT
    )


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


def test_towel_radiator_uses_nominal_power_w():
    model = make_model(
        [
            TrvConfig(
                name="towel", emitter_type=HeatEmitterType.TOWEL, nominal_power_w=600
            )
        ]
    )
    power = model.calculate_available_heating_power_w(20, flow_temperature_c=55)
    assert power > 0


def test_recommended_flow_temperature_increases_with_required_power():
    model = make_model([TrvConfig(name="trv1")])
    low_power_flow = model.calculate_recommended_flow_temperature_c(200, 20)
    high_power_flow = model.calculate_recommended_flow_temperature_c(800, 20)
    assert high_power_flow > low_power_flow


def test_recommended_flow_temperature_without_round_up_lies_within_one_step():
    model = make_model([TrvConfig(name="trv1")])
    rounded = model.calculate_recommended_flow_temperature_c(430, 20)
    exact = model.calculate_recommended_flow_temperature_c(430, 20, round_up=False)
    assert rounded % 0.5 == 0
    assert rounded - 0.5 < exact <= rounded


def test_recommended_flow_temperature_zero_when_no_power_required():
    model = make_model([TrvConfig(name="trv1")])
    assert model.calculate_recommended_flow_temperature_c(0, 20) == 0


def _esszimmer_model() -> HeatEmitterModel:
    return HeatEmitterModel(
        RoomThermalConfig(
            design_indoor_temperature_c=20.0,
            design_outdoor_temperature_c=-13.5,
            room_heat_load_w=853,
            room_area_m2=19,
            construction=Construction.HEAVY,
        ),
        [
            TrvConfig(
                name="trv1",
                radiator_type=PanelRadiatorType.TYPE_21,
                width_mm=1200,
                height_mm=600,
            )
        ],
    )


def test_recommended_flow_is_where_the_open_valve_covers_the_power():
    """The flow requirement uses the spread the valve power and the learning
    use: at the recommended flow the open valve gives at least the required
    power, half a degree lower it does not."""
    model = _esszimmer_model()
    required_w = 173.3

    flow_c = model.calculate_recommended_flow_temperature_c(required_w, 21.0)

    assert model.calculate_available_heating_power_w(21.0, flow_c) >= required_w
    assert model.calculate_available_heating_power_w(21.0, flow_c - 0.5) < required_w


def test_esszimmer_hold_flow_matches_the_flow_seen_to_hold_it():
    """Esszimmer 01.11.2026 (outdoor 8.8 °C, loss 173 W): a part-load spread
    asked for 31.5 °C, while the room held only 20.7 °C at a 34-35 °C flow."""
    model = _esszimmer_model()

    assert model.calculate_recommended_flow_temperature_c(173.3, 21.0) == 35.5


def test_recommended_flow_does_not_depend_on_the_heat_load():
    """The configured heat load sizes no spread: two rooms with the same
    radiator need the same flow for the same power."""
    small = make_model([TrvConfig(name="trv1")])
    large = HeatEmitterModel(
        RoomThermalConfig(
            room_heat_load_w=5000,
            room_area_m2=90,
            construction=Construction.VERY_LIGHT,
        ),
        [TrvConfig(name="trv1")],
    )

    assert small.calculate_recommended_flow_temperature_c(
        300, 20
    ) == large.calculate_recommended_flow_temperature_c(300, 20)


def test_panel_radiator_at_reference_height_matches_the_table_value():
    power = _available_power_at_design_delta(PanelRadiatorType.TYPE_10, 1000, 600)
    assert (
        power == PANEL_RADIATOR_REFERENCE_POWER_W_PER_METER[PanelRadiatorType.TYPE_10]
    )


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
        _expected_power_per_meter(PanelRadiatorType.TYPE_22, MIN_RADIATOR_HEIGHT_MM),
        0.01,
    )
    assert power == expected


def test_panel_radiator_height_above_maximum_is_clamped():
    power = _available_power_at_design_delta(PanelRadiatorType.TYPE_22, 1000, 3000)
    expected = round_to_step(
        _expected_power_per_meter(PanelRadiatorType.TYPE_22, MAX_RADIATOR_HEIGHT_MM),
        0.01,
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
        RoomThermalConfig(
            flow_threshold_c=32.0,
            room_area_m2=90,
            construction=Construction.VERY_LIGHT,
        ),
        [TrvConfig(name="trv1")],
    )
    assert model.flow_search_min_c == 22.0


def test_setting_the_flow_threshold_moves_the_search_window():
    model = HeatEmitterModel(
        RoomThermalConfig(
            flow_threshold_c=32.0,
            room_area_m2=90,
            construction=Construction.VERY_LIGHT,
        ),
        [TrvConfig(name="trv1")],
    )

    model.set_flow_threshold_c(25.0)

    assert model.flow_search_min_c == 15.0
    assert model.flow_search_max_c == 55.0


def test_radiator_heat_capacity_from_water_and_steel():
    model = make_model([TrvConfig(name="trv", width_mm=1000, height_mm=600)])

    # Type 22, 1 m at 600 mm: 5.9 l water and 27.5 kg steel.
    assert model.heat_capacity_j_per_k == pytest.approx(5.9 * 4186 + 27.5 * 460)


def test_towel_radiator_heat_capacity_scales_with_nominal_power():
    model = make_model(
        [
            TrvConfig(
                name="towel",
                emitter_type=HeatEmitterType.TOWEL,
                radiator_type=None,
                width_mm=None,
                height_mm=None,
                nominal_power_w=500,
            )
        ]
    )

    assert model.heat_capacity_j_per_k == pytest.approx(5.0 * 4186 + 10.0 * 460)


def test_radiator_heats_up_faster_than_it_cools_down():
    model = make_model([TrvConfig(name="trv", width_mm=1000, height_mm=600)])

    cooling_s = model.time_constant_s(21.0, 40.0, 0.0, heating_up=False)
    heating_half_s = model.time_constant_s(21.0, 40.0, 50.0, heating_up=True)
    heating_full_s = model.time_constant_s(21.0, 40.0, 100.0, heating_up=True)

    # A type 22 radiator cools off over about 20 minutes at a 40 °C flow.
    assert 15 * 60 < cooling_s < 25 * 60
    assert heating_full_s < heating_half_s < cooling_s


def test_cold_flow_keeps_the_time_constant_finite():
    model = make_model([TrvConfig(name="trv", width_mm=1000, height_mm=600)])

    assert 0 < model.time_constant_s(21.0, 15.0, 0.0, heating_up=False) < 4 * 3600


def test_emitter_without_dimensions_has_no_lag():
    model = make_model([TrvConfig(name="trv", width_mm=None, height_mm=None)])

    assert model.heat_capacity_j_per_k == 0
    assert model.time_constant_s(21.0, 40.0, 50.0, heating_up=True) == 0


def test_power_fraction_follows_the_radiator_not_the_valve():
    model = make_model([TrvConfig(name="trv", width_mm=1000, height_mm=600)])
    fractions = [
        model.power_fraction_at_opening(pct, 40.0, 20.0, 1.3)
        for pct in (0, 10, 20, 50, 80, 100)
    ]

    assert fractions[0] == 0
    assert fractions[-1] == pytest.approx(1.0)
    assert fractions == sorted(fractions)
    # Less water cools further: a small opening still gives a large share.
    assert 0.35 < fractions[1] < 0.55
    assert 0.8 < fractions[3] < 0.95


def test_measured_power_mixes_reported_openings_and_demand():
    model = make_model(
        [
            TrvConfig(name="a", width_mm=1000, height_mm=600),
            TrvConfig(name="b", width_mm=1000, height_mm=600),
        ]
    )
    available = model.calculate_available_heating_power_w(20.0, 40.0)

    full = model.calculate_measured_heating_power_w(20.0, 40.0, [100, 100], 0)
    by_demand = model.calculate_measured_heating_power_w(20.0, 40.0, [None, None], 30)
    mixed = model.calculate_measured_heating_power_w(20.0, 40.0, [0, None], 50)

    assert full == pytest.approx(available, abs=0.05)
    assert by_demand == pytest.approx(available * 0.3, abs=0.05)
    assert mixed == pytest.approx(available / 2 * 0.5, abs=0.05)


def test_full_output_fraction_follows_the_flow():
    model = make_model([TrvConfig(name="trv", width_mm=1200, height_mm=600)])

    # 55/45 system: 30 °C flow at a 20.9 °C room gives about a fifth of
    # design output, 38 °C at 20.6 °C about two fifths.
    low = model.full_output_fractions(30.0, 20.9)[0]
    warm = model.full_output_fractions(38.0, 20.6)[0]

    assert 0.15 < low < 0.25
    assert 0.35 < warm < 0.5
    assert model.full_output_fractions(None, 20.9) == [None]


def _valve_trv(name="trv", valve_position_configured=True):
    return TrvConfig(
        name=name,
        width_mm=1000,
        height_mm=600,
        valve_position_configured=valve_position_configured,
    )


@pytest.mark.parametrize(
    ("valve_position_configured", "opening_pct", "fraction"),
    [
        # Figures from power_fraction_at_opening: little water cools further.
        pytest.param(True, 10, 0.44, id="curve_10"),
        pytest.param(True, 50, 0.87, id="curve_50"),
        # Never measured on the curve, so never planned on it.
        pytest.param(False, 30, 0.3, id="linear_without_valve_entity"),
    ],
)
def test_planned_power_follows_the_curve_only_with_a_valve_entity(
    valve_position_configured, opening_pct, fraction
):
    model = make_model(
        [_valve_trv(valve_position_configured=valve_position_configured)]
    )
    available = model.calculate_available_heating_power_w(21.0, 40.0)

    assert model.planned_heating_power_w(21.0, 40.0, opening_pct) == pytest.approx(
        fraction * available, rel=0.05 if valve_position_configured else 1e-3
    )


def test_planned_power_mixes_trvs_with_and_without_valve_entity():
    model = make_model(
        [_valve_trv("curve"), _valve_trv("linear", valve_position_configured=False)]
    )
    available = model.calculate_available_heating_power_w(21.0, 40.0)
    curve_fraction = model.power_fraction_at_opening(30, 40.0, 21.0, 1.3)

    # Two equal radiators: one follows its valve curve, one the opening.
    assert model.planned_heating_power_w(21.0, 40.0, 30) == pytest.approx(
        available / 2 * (curve_fraction + 0.3), abs=0.05
    )

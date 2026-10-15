import pytest

from heating_controller.const import Construction
from heating_controller.controller.mpc.models.capacity import (
    MAX_LEARNED_CAPACITY_FACTOR,
    MIN_LEARNED_CAPACITY_FACTOR,
    ThermalCapacityModel,
)
from heating_controller.controller.mpc.types import RoomThermalConfig


@pytest.mark.parametrize(
    ("construction", "kj_per_m2k"),
    [
        (Construction.VERY_LIGHT, 80),
        (Construction.LIGHT, 110),
        (Construction.MEDIUM, 165),
        (Construction.HEAVY, 260),
        (Construction.VERY_HEAVY, 370),
    ],
)
def test_base_capacity_from_area_and_construction(construction, kj_per_m2k):
    model = ThermalCapacityModel(
        RoomThermalConfig(room_area_m2=20, construction=construction)
    )
    assert model.effective_capacity_j_per_k == pytest.approx(20 * kj_per_m2k * 1000)


def test_base_capacity_does_not_follow_the_heat_load():
    small = ThermalCapacityModel(
        RoomThermalConfig(
            room_heat_load_w=500, room_area_m2=20, construction=Construction.HEAVY
        )
    )
    large = ThermalCapacityModel(
        RoomThermalConfig(
            room_heat_load_w=2000, room_area_m2=20, construction=Construction.HEAVY
        )
    )
    assert small.effective_capacity_j_per_k == large.effective_capacity_j_per_k


def test_predict_temperature_change_scales_with_power_and_duration():
    model = ThermalCapacityModel(
        RoomThermalConfig(room_area_m2=10, construction=Construction.MEDIUM)
    )
    delta = model.predict_temperature_change_c(
        net_heating_power_w=10 * 165 * 1000, duration_seconds=1
    )
    assert delta == pytest.approx(1.0)


def test_learned_capacity_factor_scales_the_base():
    model = ThermalCapacityModel(
        RoomThermalConfig(room_area_m2=10, construction=Construction.MEDIUM)
    )
    model.learned_capacity_factor = 2.0
    assert model.effective_capacity_j_per_k == pytest.approx(2 * 10 * 165 * 1000)


def test_learned_capacity_factor_is_clamped():
    model = ThermalCapacityModel(
        RoomThermalConfig(room_area_m2=10, construction=Construction.MEDIUM)
    )

    model.learned_capacity_factor = 100
    assert model.learned_capacity_factor == MAX_LEARNED_CAPACITY_FACTOR

    model.learned_capacity_factor = -5
    assert model.learned_capacity_factor == MIN_LEARNED_CAPACITY_FACTOR

from heating_controller.controller.mpc.models.loss import (
    MAX_LEARNED_UA_FACTOR,
    MIN_LEARNED_UA_FACTOR,
    RoomLossModel,
)
from heating_controller.controller.mpc.types import RoomThermalConfig


def test_base_ua_from_design_heat_load_and_delta():
    model = RoomLossModel(
        RoomThermalConfig(
            design_indoor_temperature_c=20,
            design_outdoor_temperature_c=-12,
            room_heat_load_w=1000,
        )
    )
    assert model.effective_ua_w_per_k == 31.25


def test_calculate_heat_loss_scales_with_temperature_delta():
    model = RoomLossModel(RoomThermalConfig(room_heat_load_w=1000))
    loss_10k = model.calculate_heat_loss_w(20, 10)
    loss_20k = model.calculate_heat_loss_w(20, 0)
    assert loss_20k == loss_10k * 2


def test_learned_ua_factor_is_clamped():
    model = RoomLossModel(RoomThermalConfig())

    model.learned_ua_factor = 100
    assert model.learned_ua_factor == MAX_LEARNED_UA_FACTOR

    model.learned_ua_factor = -5
    assert model.learned_ua_factor == MIN_LEARNED_UA_FACTOR


def test_design_temperature_delta_is_floored_to_avoid_division_by_zero():
    model = RoomLossModel(
        RoomThermalConfig(
            design_indoor_temperature_c=20,
            design_outdoor_temperature_c=20,
            room_heat_load_w=1000,
        )
    )
    assert model.effective_ua_w_per_k == 10000

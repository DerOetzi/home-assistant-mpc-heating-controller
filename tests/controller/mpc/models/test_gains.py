import pytest
from heating_controller.controller.mpc.models.gains import RoomGainModel

LOAD_W = 1000.0
BASE_UA = 31.25


def _model():
    return RoomGainModel(LOAD_W, BASE_UA)


def test_separates_steady_gain_over_nights_with_different_differences():
    model = _model()
    for delta_c in (10.0, 14.0, 18.0):
        model.add_night_point(delta_c, 20.0 * delta_c - 50.0, 6.0, BASE_UA)
    assert model.learned_gain_w == pytest.approx(50.0, abs=5)
    assert model.regression_ua_factor == pytest.approx(20.0 / BASE_UA, rel=0.03)
    assert model.gain_determined
    assert model.steady_gain_w == pytest.approx(50.0, abs=5)


def test_gain_not_determined_at_one_temperature_difference():
    model = _model()
    for _ in range(5):
        model.add_night_point(14.0, 20.0 * 14.0 - 50.0, 6.0, BASE_UA)
    assert not model.gain_determined
    assert model.steady_gain_w == 0.0


def test_gain_needs_three_points():
    model = _model()
    for delta_c in (10.0, 18.0):
        model.add_night_point(delta_c, 20.0 * delta_c - 50.0, 6.0, BASE_UA)
    assert not model.gain_determined


def test_gain_limited_to_half_the_heat_load():
    model = _model()
    for delta_c in (10.0, 14.0, 18.0):
        model.add_night_point(delta_c, 20.0 * delta_c - 2000.0, 6.0, BASE_UA)
    assert model.learned_gain_w <= 0.5 * LOAD_W


def test_empty_model_reports_nothing():
    model = _model()
    assert model.learned_gain_w is None
    assert model.gain_sigma_w is None
    assert model.regression_ua_factor is None
    assert model.steady_gain_w == 0.0
    assert not model.consume_changed()


def test_export_restore_round_trip():
    model = _model()
    for delta_c in (10.0, 14.0, 18.0):
        model.add_night_point(delta_c, 20.0 * delta_c - 50.0, 6.0, BASE_UA)
    assert model.consume_changed()
    copy = _model()
    copy.restore(model.export())
    assert copy.learned_gain_w == model.learned_gain_w
    assert copy.night_points == 3
    assert copy.gain_determined


def test_restore_ignores_other_version():
    model = _model()
    model.restore({"version": 99, "night": {"theta": [1, 2]}})
    assert model.night_points == 0


def test_reset_forgets():
    model = _model()
    model.add_night_point(14.0, 230.0, 6.0, BASE_UA)
    model.reset()
    assert model.night_points == 0
    assert model.consume_changed()

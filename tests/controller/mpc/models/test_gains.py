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


def _hours(model, profile: int, value_w: float, days: list[int], per_day: int = 3):
    """Hours of one block; other blocks get a little noise so the residual
    variance is not zero."""
    for day in days:
        for hour in range(per_day):
            model.add_hour(profile, [], value_w + (5.0 if hour % 2 else -5.0), day)
            model.add_hour((profile + 1) % 16, [], 0.0, day)


def test_profile_block_needs_three_days():
    """Duschbad 5 Nov: a block counted after one evening and kept the
    room below setpoint."""
    model = RoomGainModel(520.0, 16.25)
    _hours(model, 7, 190.0, [1])
    assert model.profile_days(7) == 1
    assert not model.profile_determined(7)
    assert model.breakdown(7, []).profile_w == 0.0

    _hours(model, 7, 190.0, [2])
    assert not model.profile_determined(7)

    _hours(model, 7, 190.0, [3])
    assert model.profile_days(7) == 3
    assert model.profile_determined(7)
    assert model.breakdown(7, []).profile_w == pytest.approx(190.0, abs=10)


def test_profile_days_survive_restore_and_reset():
    model = RoomGainModel(520.0, 16.25)
    _hours(model, 7, 190.0, [1, 2, 3])

    restored = RoomGainModel(520.0, 16.25)
    restored.restore(model.export())
    assert restored.profile_days(7) == 3
    assert restored.profile_determined(7)

    old = model.export()
    del old["profile_days"]
    without = RoomGainModel(520.0, 16.25)
    without.restore(old)
    assert without.profile_days(7) == 0
    assert not without.profile_determined(7)

    model.reset()
    assert model.profile_days(7) == 0

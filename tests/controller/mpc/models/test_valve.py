import pytest
from heating_controller.controller.mpc.models.valve import ValveCharacteristic

CURVE = ValveCharacteristic(
    points=((0.1, 0.0), (0.6, 45.0), (1.5, 60.0), (2.5, 90.0), (3.0, 100.0))
)


def test_opening_interpolates_between_points():
    assert CURVE.opening_pct(1.05) == pytest.approx(52.5)


def test_opening_below_first_point_is_zero():
    assert CURVE.opening_pct(0.0) == 0
    assert CURVE.opening_pct(-2.0) == 0


def test_opening_above_last_point_is_last_opening():
    assert CURVE.opening_pct(3.0) == 100
    assert CURVE.opening_pct(5.0) == 100


def test_offset_is_the_smallest_reaching_the_opening():
    assert CURVE.offset_c(52.5) == pytest.approx(1.05)
    assert CURVE.offset_c(0) == 0.1
    assert CURVE.offset_c(120) == 3.0


def test_offset_of_a_flat_stretch_is_its_start():
    flat = ValveCharacteristic(
        points=((0.5, 20.0), (1.0, 50.0), (2.0, 50.0), (3.0, 100.0))
    )

    assert flat.offset_c(50.0) == pytest.approx(1.0)


@pytest.mark.parametrize("opening_pct", range(0, 101, 10))
def test_offset_round_trips_the_opening(opening_pct):
    reached = CURVE.opening_pct(CURVE.offset_c(opening_pct))

    assert reached >= min(opening_pct, CURVE.points[-1][1]) - 1e-9

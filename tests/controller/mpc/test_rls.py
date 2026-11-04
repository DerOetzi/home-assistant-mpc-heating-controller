import random

import pytest
from heating_controller.controller.mpc.rls import RecursiveLeastSquares


def _line_rls(**kwargs) -> RecursiveLeastSquares:
    return RecursiveLeastSquares(
        theta0=[0.0, 0.0],
        p0_diag=[1e4, 1e6],
        forgetting=kwargs.pop("forgetting", 1.0),
        **kwargs,
    )


def _feed_line(rls, rng, count, slope=20.0, offset=50.0, noise=10.0, x_range=(5, 20)):
    for _ in range(count):
        x = rng.uniform(*x_range)
        rls.update([x, 1.0], slope * x + offset + rng.gauss(0, noise))


def test_learns_slope_and_offset():
    rng = random.Random(1)
    rls = _line_rls()
    _feed_line(rls, rng, 200)
    assert rls.theta[0] == pytest.approx(20.0, rel=0.02)
    assert rls.theta[1] == pytest.approx(50.0, abs=5)
    assert rls.std(1) < 10


def test_noise_estimate_matches_noise():
    rng = random.Random(2)
    rls = _line_rls()
    _feed_line(rls, rng, 400)
    assert rls.noise_variance**0.5 == pytest.approx(10.0, rel=0.2)


def test_offset_not_determined_without_excitation():
    rng = random.Random(3)
    rls = _line_rls()
    _feed_line(rls, rng, 50, x_range=(12, 12))
    assert rls.std(1) > 100


def test_noise_floor_keeps_unexcited_parameter_unknown():
    rls = RecursiveLeastSquares(
        theta0=[0.0, 0.0],
        p0_diag=[1e3, 1e6],
        forgetting=1.0,
        noise_floor_variance=20.0**2,
    )
    for _ in range(5):
        # Noise-free, always the same temperature difference.
        rls.update([14.0, -1.0], 20 * 14 - 50)
    assert rls.std(1) > 1000


def test_lower_bound_projects():
    rng = random.Random(4)
    rls = RecursiveLeastSquares(
        theta0=[0.0], p0_diag=[1e4], forgetting=1.0, lower_bounds=[0.0]
    )
    for _ in range(100):
        x = rng.uniform(0, 1)
        rls.update([x], -5.0 * x + rng.gauss(0, 0.1))
    assert rls.theta[0] == 0.0


def test_upper_bound_projects():
    rls = RecursiveLeastSquares(
        theta0=[0.0], p0_diag=[1e4], forgetting=1.0, upper_bounds=[3.0]
    )
    for _ in range(20):
        rls.update([1.0], 10.0)
    assert rls.theta[0] == 3.0


@pytest.mark.parametrize(("huber_k", "max_shift"), [(2.0, 0.02), (None, None)])
def test_huber_damps_outlier(huber_k, max_shift):
    rng = random.Random(5)
    rls = _line_rls(huber_k=huber_k)
    _feed_line(rls, rng, 100)
    before = rls.theta[1]
    rls.update([12.0, 1.0], 20 * 12 + 50 + 2000)
    shift = abs(rls.theta[1] - before) / before
    if max_shift is None:
        assert shift > 0.10
    else:
        assert shift < max_shift


def test_forgetting_follows_change():
    rng = random.Random(6)
    rls = RecursiveLeastSquares(theta0=[0.0], p0_diag=[1e4], forgetting=0.996)
    for _ in range(500):
        rls.update([1.0], 50 + rng.gauss(0, 5))
    for _ in range(500):
        rls.update([1.0], 100 + rng.gauss(0, 5))
    assert rls.theta[0] > 90


def test_diagonal_cap_limits_an_unexcited_parameter_only():
    rng = random.Random(8)
    rls = RecursiveLeastSquares(
        theta0=[0.0, 0.0],
        p0_diag=[100.0, 100.0],
        forgetting=0.99,
        p_max_diag=[100.0, 100.0],
    )
    for _ in range(1000):
        # Only the first parameter is excited.
        rls.update([1.0, 0.0], 30.0 + rng.gauss(0, 1))
    assert rls.p[1][1] <= 100.0 + 1e-9
    assert rls.theta[0] == pytest.approx(30.0, abs=1)
    # The excited parameter kept a small, forgetting-limited variance.
    assert rls.p[0][0] < 0.1


def test_std_infinite_before_samples():
    rls = _line_rls()
    assert rls.std(0) == float("inf")


def test_round_trip():
    rng = random.Random(7)
    rls = _line_rls()
    _feed_line(rls, rng, 30)
    copy = _line_rls()
    assert copy.restore(rls.to_dict())
    assert copy.theta == rls.theta
    assert copy.p == rls.p
    assert copy.noise_variance == rls.noise_variance
    assert copy.samples == rls.samples


def test_restore_rejects_wrong_size():
    rls = _line_rls()
    assert not rls.restore({"theta": [1.0], "p": [[1.0]]})
    assert rls.theta == [0.0, 0.0]

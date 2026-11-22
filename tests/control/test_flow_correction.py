import math

from heating_controller.control.flow_correction import (
    LIMIT_K,
    MAX_TICK_S,
    TIME_CONSTANT_S,
    CorrectionFreeze,
    FlowCorrection,
)

T0 = 1_800_000_000.0


def _run(
    correction: FlowCorrection,
    measured: float | None,
    effective: float | None,
    start: float,
    seconds: float,
    frozen: CorrectionFreeze | None = None,
) -> float:
    ts = start
    while ts <= start + seconds:
        correction.update(measured, effective, frozen, ts)
        ts += 60
    return ts - 60


def test_follows_the_gap_with_its_time_constant() -> None:
    correction = FlowCorrection()
    _run(correction, 40.0, 41.6, T0, TIME_CONSTANT_S)

    expected = -1.6 * (1 - math.exp(-1))
    assert math.isclose(correction.correction_k, expected, rel_tol=1e-6)
    assert correction.frozen is None


def test_whole_degree_reports_average_out() -> None:
    correction = FlowCorrection()
    ts = T0
    for i in range(int(6 * TIME_CONSTANT_S / 60)):
        correction.update(41.0 if i % 2 else 40.0, 41.0, None, ts)
        ts += 60
    assert abs(correction.correction_k + 0.5) < 0.05


def test_bounded() -> None:
    correction = FlowCorrection()
    _run(correction, 30.0, 40.0, T0, 10 * TIME_CONSTANT_S)
    assert correction.correction_k == -LIMIT_K


def test_frozen_keeps_the_value_and_skips_the_gap() -> None:
    correction = FlowCorrection()
    end = _run(correction, 40.0, 41.0, T0, TIME_CONSTANT_S)
    value = correction.correction_k

    end = _run(correction, 25.0, 41.0, end + 60, 3600, CorrectionFreeze.SETTLING)
    assert correction.correction_k == value
    assert correction.frozen is CorrectionFreeze.SETTLING

    # The first tick after the freeze moves by at most one tick.
    correction.update(40.0, 41.0, None, end + 60)
    assert abs(correction.correction_k - value) < MAX_TICK_S / TIME_CONSTANT_S


def test_missing_measurement_freezes() -> None:
    correction = FlowCorrection()
    _run(correction, None, 41.0, T0, 600)
    assert correction.correction_k == 0.0
    assert correction.frozen is CorrectionFreeze.NO_MEASUREMENT


def test_export_and_restore() -> None:
    correction = FlowCorrection()
    _run(correction, 40.0, 41.0, T0, TIME_CONSTANT_S)
    restored = FlowCorrection()
    restored.restore(correction.export())
    assert math.isclose(restored.correction_k, correction.correction_k, abs_tol=1e-3)

    for bad in ({"correction_k": "x"}, {"correction_k": float("nan")}, {}):
        broken = FlowCorrection()
        broken.restore(bad)
        assert broken.correction_k == 0.0
    big = FlowCorrection()
    big.restore({"correction_k": 9.0})
    assert big.correction_k == LIMIT_K

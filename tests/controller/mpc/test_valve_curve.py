import pytest
from heating_controller.controller.mpc.models.valve import ValveCharacteristic
from heating_controller.controller.mpc.valve_curve import (
    VALVE_CURVE_BIN_SAMPLES,
    ValveCurveLearner,
    ValveCurveStatus,
)

READING_C = 20.0
# The seed both Danfoss profiles ship; test_profiles checks the YAML once.
DANFOSS_SEED = ValveCharacteristic(
    points=((0.1, 5.0), (0.6, 45.0), (1.5, 55.0), (2.5, 90.0), (3.0, 100.0))
)


class _Trv:
    """Drives a learner the way the controller does: one TRV, its own clock."""

    def __init__(self, learner: ValveCurveLearner) -> None:
        self.learner = learner
        self.now_ts = 1_000_000.0

    def start(self, offset_c: float, allowed: bool = True) -> None:
        """The TRV reports a new setpoint offset_c above its reading."""
        self.now_ts += 2000
        # A different setpoint before, so the next one is a change.
        self.learner.observe(self.now_ts - 1, 5.0, READING_C, 0.0, True)
        self.learner.observe(self.now_ts, READING_C + offset_c, READING_C, 0.0, allowed)
        self._offset_c = offset_c

    def observe(
        self, after_s: float, opening_pct: float | None, allowed: bool = True
    ) -> None:
        self.learner.observe(
            self.now_ts + after_s,
            READING_C + self._offset_c,
            READING_C,
            opening_pct,
            allowed,
        )

    def sample(self, offset_c: float, opening_pct: float, times: int = 1) -> None:
        for _ in range(times):
            self.start(offset_c)
            self.observe(700, opening_pct)


def _points(learner: ValveCurveLearner) -> tuple[tuple[float, float], ...]:
    """The curve's points without float noise from reading + offset."""
    return tuple(
        (round(offset_c, 6), round(opening_pct, 6))
        for offset_c, opening_pct in learner.characteristic().points
    )


def test_sample_is_taken_after_the_settle_time():
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(0.7, 50, times=3)

    [(offset_c, opening_pct, samples, spread_pct)] = trv.learner.bins()
    assert offset_c == pytest.approx(0.7)
    assert opening_pct == pytest.approx(50)
    assert samples == 3
    assert spread_pct == 0


@pytest.mark.parametrize(
    "after_s",
    [pytest.param(300, id="before_settle"), pytest.param(1000, id="after_latest")],
)
def test_sample_outside_the_window_is_not_taken(after_s):
    trv = _Trv(ValveCurveLearner(None))

    trv.start(0.7)
    trv.observe(after_s, 50)

    assert trv.learner.bins() == []


def test_new_target_before_settle_drops_the_sample():
    trv = _Trv(ValveCurveLearner(None))

    trv.start(0.7)
    trv.learner.observe(trv.now_ts + 300, READING_C + 1.2, READING_C, 30, True)
    trv.learner.observe(trv.now_ts + 700, READING_C + 1.2, READING_C, 50, True)

    assert trv.learner.bins() == []


@pytest.mark.parametrize(
    ("start_allowed", "midway_allowed"),
    [pytest.param(True, False, id="moment"), pytest.param(False, True, id="start")],
)
def test_disallowed_moment_drops_the_sample(start_allowed, midway_allowed):
    trv = _Trv(ValveCurveLearner(None))

    trv.start(0.7, allowed=start_allowed)
    trv.observe(300, 40, allowed=midway_allowed)
    trv.observe(700, 50)

    assert trv.learner.bins() == []


def test_missing_opening_drops_the_sample():
    trv = _Trv(ValveCurveLearner(None))

    trv.start(0.7)
    trv.observe(700, None)
    trv.observe(800, 50)

    assert trv.learner.bins() == []


def test_the_first_reported_target_is_no_change():
    learner = ValveCurveLearner(None)

    learner.observe(0, READING_C + 0.7, READING_C, 0, True)
    learner.observe(700, READING_C + 0.7, READING_C, 50, True)

    assert learner.bins() == []


def test_without_samples_the_status_is_seed():
    seeded = ValveCurveLearner(DANFOSS_SEED)
    unseeded = ValveCurveLearner(None)

    assert seeded.status is ValveCurveStatus.SEED
    assert unseeded.status is ValveCurveStatus.SEED


def _no_samples(trv):
    pass


def _two_bins(trv):
    trv.sample(0.7, 30, times=3)
    trv.sample(1.2, 50, times=3)


def _scattered_bin(trv):
    for opening_pct in (10, 20, 50, 60, 80):
        trv.sample(0.7, opening_pct)


@pytest.mark.parametrize(
    ("prepare", "status"),
    [
        pytest.param(_no_samples, ValveCurveStatus.SEED, id="seed"),
        pytest.param(_two_bins, ValveCurveStatus.LEARNING, id="learning"),
        pytest.param(_scattered_bin, ValveCurveStatus.SCATTERED, id="scattered"),
    ],
)
def test_until_learned_the_seed_is_the_curve(prepare, status):
    seeded = _Trv(ValveCurveLearner(DANFOSS_SEED))
    unseeded = _Trv(ValveCurveLearner(None))
    prepare(seeded)
    prepare(unseeded)

    # The planner counts in openings: the seed maps them to offsets until
    # the TRV has learned its own curve; partial bins are not merged. Without
    # a seed there is no curve and the control keeps the weighted mapping.
    assert seeded.learner.status is status
    assert seeded.learner.characteristic() == DANFOSS_SEED
    assert unseeded.learner.status is status
    assert unseeded.learner.characteristic() is None


def test_a_bin_counts_from_three_samples():
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(0.7, 50, times=2)
    assert trv.learner.status is ValveCurveStatus.SEED
    assert trv.learner.characteristic() is None

    trv.sample(0.7, 50)
    assert trv.learner.status is ValveCurveStatus.LEARNING


def test_learned_points_replace_the_seed_in_their_span():
    trv = _Trv(ValveCurveLearner(DANFOSS_SEED))

    trv.sample(0.7, 30, times=3)
    trv.sample(1.2, 50, times=3)
    trv.sample(2.2, 85, times=3)

    # Seed points more than 0.25 K outside the learned span stay: 0.1 K
    # below, 2.5 K and 3.0 K above. 0.6 K and 1.5 K lie within it.
    assert _points(trv.learner) == (
        (0.1, 5.0),
        (0.7, 30.0),
        (1.2, 50.0),
        (2.2, 85.0),
        (2.5, 90.0),
        (3.0, 100.0),
    )
    assert trv.learner.status is ValveCurveStatus.LEARNED


def test_learned_needs_a_bin_at_two_kelvin():
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(0.2, 10, times=3)
    trv.sample(0.7, 30, times=3)
    trv.sample(1.2, 50, times=3)

    assert trv.learner.status is ValveCurveStatus.LEARNING


def test_curve_is_monotone():
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(0.7, 50, times=3)
    trv.sample(1.2, 45, times=3)
    trv.sample(2.2, 85, times=3)

    assert trv.learner.characteristic().opening_pct(1.2) == pytest.approx(50)


def test_without_seed_the_full_open_anchor_is_added():
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(0.7, 30, times=3)
    trv.sample(1.2, 50, times=3)
    trv.sample(2.2, 85, times=3)

    # Learned, but no point from 2.5 K on: 3.0 K counts as fully open.
    assert _points(trv.learner) == (
        (0.7, 30.0),
        (1.2, 50.0),
        (2.2, 85.0),
        (3.0, 100.0),
    )


def test_a_learned_wide_offset_needs_no_anchor():
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(0.7, 30, times=3)
    trv.sample(1.2, 50, times=3)
    trv.sample(2.7, 95, times=3)

    assert _points(trv.learner) == ((0.7, 30.0), (1.2, 50.0), (2.7, 95.0))


def test_spread_below_five_samples_does_not_scatter():
    trv = _Trv(ValveCurveLearner(None))

    for opening_pct in (10, 50, 90):
        trv.sample(0.7, opening_pct)

    assert trv.learner.status is ValveCurveStatus.LEARNING


def test_export_restore_keeps_finished_samples_only():
    trv = _Trv(ValveCurveLearner(DANFOSS_SEED))
    trv.sample(0.7, 30, times=3)
    trv.sample(1.2, 50, times=2)
    trv.start(2.2)

    data = trv.learner.export()
    restored = ValveCurveLearner(DANFOSS_SEED)
    restored.restore(data)
    restored.observe(trv.now_ts + 700, READING_C + 2.2, READING_C, 85, True)

    assert restored.bins() == trv.learner.bins()
    assert len(restored.bins()) == 2


def test_restore_skips_malformed_samples():
    learner = ValveCurveLearner(None)

    learner.restore({"samples": [[0.7, 50], "x", [None, 3], [0.7]]})

    assert [bin_.samples for bin_ in learner.bins()] == [1]


def test_ring_buffer_keeps_the_last_samples():
    trv = _Trv(ValveCurveLearner(None))

    for opening_pct in range(25):
        trv.sample(0.7, opening_pct)

    [(_, opening_pct, samples, _)] = trv.learner.bins()
    assert samples == VALVE_CURVE_BIN_SAMPLES == 20
    # Openings 5..24 are left; the oldest five dropped out.
    assert opening_pct == pytest.approx(14.5)


def test_a_finished_sample_marks_the_curve_changed():
    trv = _Trv(ValveCurveLearner(None))

    trv.start(0.7)
    assert trv.learner.consume_changed() is False

    trv.observe(700, 50)
    assert trv.learner.consume_changed() is True
    assert trv.learner.consume_changed() is False


def test_restore_replaces_the_bins_read_before():
    trv = _Trv(ValveCurveLearner(None))
    trv.sample(0.7, 50)
    assert len(trv.learner.bins()) == 1

    trv.learner.restore({"samples": [[1.2, 60], [2.2, 85]]})

    assert [round(bin_.offset_c, 2) for bin_ in trv.learner.bins()] == [1.2, 2.2]


@pytest.mark.parametrize(
    ("before_c", "reading_c"),
    [
        # The controller shuts the TRV by writing its minimum target.
        pytest.param(21.0, 20.5, id="off_write"),
        # A cold TRV at its minimum target: offset in range, still an off write.
        pytest.param(6.0, 4.5, id="target_at_minimum"),
    ],
)
def test_off_write_starts_no_sample(before_c, reading_c):
    learner = ValveCurveLearner(None, min_target_c=5.0)
    learner.observe(0, before_c, reading_c, 40, True)

    learner.observe(2000, 5.0, reading_c, 40, True)
    learner.observe(2700, 5.0, reading_c, 0, True)

    assert learner.bins() == []


def test_off_write_drops_the_open_sample():
    trv = _Trv(ValveCurveLearner(None, min_target_c=5.0))

    trv.start(0.7)
    trv.learner.observe(trv.now_ts + 300, 5.0, READING_C, 0, True)
    trv.learner.observe(trv.now_ts + 700, 5.0, READING_C, 0, True)

    assert trv.learner.bins() == []


@pytest.mark.parametrize(
    ("offset_c", "taken"),
    [
        pytest.param(4.5, False, id="above_max"),
        pytest.param(3.5, True, id="below_max"),
        pytest.param(0.1, True, id="at_relative_minimum"),
        pytest.param(0.05, False, id="below_relative_minimum"),
    ],
)
def test_only_a_heating_offset_starts_a_sample(offset_c, taken):
    trv = _Trv(ValveCurveLearner(None))

    trv.sample(offset_c, 50)

    assert [bin_.samples for bin_ in trv.learner.bins()] == ([1] if taken else [])


def test_restore_ignores_samples_outside_the_offset_range():
    learner = ValveCurveLearner(None)

    # Saved before the offset range existed: three off writes, one too wide.
    learner.restore(
        {"samples": [[-15.5, 0], [-15.5, 0], [-15.5, 0], [0.7, 50], [6.0, 100]]}
    )

    [(offset_c, _, samples, _)] = learner.bins()
    assert (round(offset_c, 2), samples) == (0.7, 1)

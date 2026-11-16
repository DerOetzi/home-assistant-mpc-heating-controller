from heating_controller.control.flow_feedback import (
    DOWN_AFTER_S,
    FEEDBACK_LIMIT_K,
    UP_SPACING_S,
    DemandSample,
    FeedbackFreeze,
    FlowFeedback,
)

T0 = 1_800_000_000.0
STEP = 1.0


def _sample(demand: float, deficit: float | None = 0.0, room: str = "A"):
    return DemandSample(room, demand, deficit)


def _run(
    feedback: FlowFeedback,
    samples: list[DemandSample],
    start: float,
    seconds: float,
    frozen: FeedbackFreeze | None = None,
    **limits: bool,
) -> float:
    """Update once a minute from start for seconds; returns the end time."""
    ts = start
    while ts <= start + seconds:
        feedback.update(samples, STEP, frozen, ts, **limits)
        ts += 60
    return ts - 60


def _ready() -> FlowFeedback:
    """A feedback that has run idle for the step spacing since its start."""
    feedback = FlowFeedback()
    _run(feedback, [], T0 - UP_SPACING_S, UP_SPACING_S - 60)
    return feedback


def test_no_step_in_the_first_spacing_after_start() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99)], T0, UP_SPACING_S - 60)
    assert feedback.feedback_k == 0.0

    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 1.0


def test_open_valve_and_room_short_steps_up() -> None:
    feedback = _ready()
    feedback.update([_sample(95, deficit=0.2)], STEP, None, T0)

    assert feedback.feedback_k == 1.0
    assert feedback.last_change_ts == T0


def test_open_valve_at_target_does_nothing() -> None:
    feedback = _ready()
    _run(feedback, [_sample(95, deficit=0.0)], T0, 3 * 3600)

    assert feedback.feedback_k == 0.0


def test_saturated_valve_steps_up_without_deficit() -> None:
    feedback = _ready()
    feedback.update([_sample(99, deficit=None)], STEP, None, T0)

    assert feedback.feedback_k == 1.0


def test_saturated_valve_above_target_does_not_step_up() -> None:
    feedback = _ready()
    _run(feedback, [_sample(100, deficit=-0.1)], T0, 3 * 3600)

    assert feedback.feedback_k == 0.0


def test_steps_up_at_most_every_spacing() -> None:
    feedback = _ready()
    end = _run(feedback, [_sample(99)], T0, UP_SPACING_S - 60)
    assert feedback.feedback_k == 1.0

    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 2.0


def test_low_demand_for_the_down_time_steps_down() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 60)
    assert feedback.feedback_k == 0.0
    assert feedback.down_count_s == DOWN_AFTER_S - 60

    _run(feedback, [_sample(60)], T0 + DOWN_AFTER_S, 0)
    assert feedback.feedback_k == -1.0
    assert feedback.down_count_s == 0.0


def test_room_short_of_its_target_starts_the_count_over() -> None:
    feedback = _ready()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 600)
    end = _run(feedback, [_sample(85, deficit=0.1)], end + 60, 0)
    assert feedback.down_count_s == 0.0

    _run(feedback, [_sample(60)], end + 60, DOWN_AFTER_S - 600)
    assert feedback.feedback_k == 0.0


def test_room_holding_at_its_target_counts_back() -> None:
    feedback = _ready()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 660)
    assert feedback.down_count_s == DOWN_AFTER_S - 600

    # Five minutes holding at the target take five minutes off.
    end = _run(feedback, [_sample(85, deficit=0.0)], end + 60, 240)
    assert feedback.down_count_s == DOWN_AFTER_S - 900

    end = _run(feedback, [_sample(60)], end + 60, 780)
    assert feedback.feedback_k == 0.0
    _run(feedback, [_sample(60)], end + 60, 0)
    assert feedback.feedback_k == -1.0


def test_count_back_stops_at_zero() -> None:
    feedback = _ready()
    end = _run(feedback, [_sample(60)], T0, 540)
    end = _run(feedback, [_sample(85, deficit=0.0)], end + 60, 3600)
    assert feedback.down_count_s == 0.0

    _run(feedback, [_sample(60)], end + 60, DOWN_AFTER_S - 120)
    assert feedback.feedback_k == 0.0


def test_down_uses_the_highest_room() -> None:
    feedback = _ready()
    _run(feedback, [_sample(60), _sample(85, room="B")], T0, DOWN_AFTER_S + 60)

    assert feedback.feedback_k == 0.0
    assert feedback.lead.room == "B"


def test_open_valve_above_target_does_not_hold_the_flow() -> None:
    feedback = FlowFeedback()
    _run(
        feedback,
        [_sample(60), _sample(91, deficit=-0.5, room="B")],
        T0,
        DOWN_AFTER_S,
    )

    assert feedback.feedback_k == -1.0


def test_open_valve_just_above_target_holds_the_flow() -> None:
    feedback = _ready()
    _run(feedback, [_sample(91, deficit=-0.1)], T0, DOWN_AFTER_S + 60)

    assert feedback.feedback_k == 0.0


def test_lead_is_the_room_that_steps_up() -> None:
    feedback = _ready()
    feedback.update(
        [_sample(92, deficit=0.6), _sample(95, deficit=-0.5, room="B")],
        STEP,
        None,
        T0,
    )

    assert feedback.feedback_k == 1.0
    assert feedback.lead.room == "A"


def test_clamped_to_limit() -> None:
    feedback = _ready()
    _run(feedback, [_sample(99)], T0, 10 * UP_SPACING_S)

    assert feedback.feedback_k == FEEDBACK_LIMIT_K


def test_no_step_against_a_target_limit() -> None:
    feedback = _ready()
    feedback.update([_sample(99)], STEP, None, T0, at_upper_limit=True)
    assert feedback.feedback_k == 0.0

    _run(feedback, [_sample(60)], T0, DOWN_AFTER_S + 60, at_lower_limit=True)
    assert feedback.feedback_k == 0.0


def test_frozen_neither_steps_nor_runs_timers() -> None:
    for reason in FeedbackFreeze:
        feedback = _ready()
        end = _run(feedback, [_sample(99)], T0, 3 * 3600, frozen=reason)
        assert feedback.feedback_k == 0.0
        assert feedback.frozen is reason

        # Low demand while frozen does not count towards the down time.
        feedback = _ready()
        end = _run(feedback, [_sample(60)], T0, 3 * 3600, frozen=reason)
        _run(feedback, [_sample(60)], end + 60, DOWN_AFTER_S - 120)
        assert feedback.feedback_k == 0.0


def test_no_samples_does_nothing() -> None:
    feedback = _ready()
    _run(feedback, [], T0, 3 * 3600)

    assert feedback.feedback_k == 0.0
    assert feedback.lead is None


def test_no_samples_pauses_the_down_count() -> None:
    feedback = _ready()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 660)
    end = _run(feedback, [], end + 60, 3 * 3600)
    assert feedback.down_count_s == DOWN_AFTER_S - 600

    _run(feedback, [_sample(60)], end + 60, 540)
    assert feedback.feedback_k == -1.0


def test_reset_waits_the_spacing_before_stepping_up() -> None:
    feedback = _ready()
    _run(feedback, [_sample(99)], T0, 3 * UP_SPACING_S)
    assert feedback.feedback_k > 0.0

    start = T0 + 4 * UP_SPACING_S
    feedback.reset(start)
    assert feedback.feedback_k == 0.0
    assert feedback.last_change_ts == start

    end = _run(feedback, [_sample(99)], start, UP_SPACING_S - 60)
    assert feedback.feedback_k == 0.0
    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 1.0


def test_export_and_restore() -> None:
    feedback = _ready()
    feedback.update([_sample(99)], STEP, None, T0)

    restored = FlowFeedback()
    restored.restore(feedback.export())
    assert restored.feedback_k == 1.0
    assert restored.last_change_ts == T0

    restored.restore({"feedback_k": 99})
    assert restored.feedback_k == FEEDBACK_LIMIT_K

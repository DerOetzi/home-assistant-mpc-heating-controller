import pytest
from heating_controller.control.flow_feedback import (
    CATCH_UP_MAX_S,
    DOWN_AFTER_S,
    FEEDBACK_LIMIT_K,
    NIGHT_DOWN_AFTER_S,
    NIGHT_STEP_LOCK_DOWN_S,
    NIGHT_STEP_LOCK_UP_S,
    STEP_LOCK_DOWN_S,
    STEP_LOCK_UP_S,
    UP_AFTER_S,
    DemandSample,
    FeedbackFreeze,
    FlowFeedback,
)

T0 = 1_800_000_000.0
STEP = 1.0


def _sample(demand: float, deficit: float | None = 0.2, room: str = "A"):
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
    """A feedback that has run idle for a while since its start."""
    feedback = FlowFeedback()
    _run(feedback, [], T0 - UP_AFTER_S, UP_AFTER_S - 60)
    return feedback


def test_wanting_for_the_up_time_steps_up() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99)], T0, UP_AFTER_S - 60)
    assert feedback.feedback_k == 0.0
    assert feedback.count_s == -(UP_AFTER_S - 60)

    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 1.0
    assert feedback.last_change_ts == end + 60
    assert feedback.count_s == 0.0


def test_open_valve_at_target_does_nothing() -> None:
    feedback = _ready()
    _run(feedback, [_sample(95, deficit=0.0)], T0, 3 * 3600)

    assert feedback.feedback_k == 0.0


def test_open_valve_in_the_trv_band_does_nothing() -> None:
    for deficit in (0.1, 0.2, 0.24):
        feedback = _ready()
        _run(feedback, [_sample(95, deficit=deficit)], T0, 3 * 3600)

        assert feedback.feedback_k == 0.0


def test_open_valve_rounded_beyond_the_band_steps_up() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_sample(92, deficit=0.25)], T0, UP_AFTER_S)

    assert feedback.feedback_k == 1.0


def test_saturated_valve_a_little_short_steps_up() -> None:
    for deficit in (0.15, 0.2, None):
        feedback = FlowFeedback()
        _run(feedback, [_sample(99, deficit=deficit)], T0, UP_AFTER_S)

        assert feedback.feedback_k == 1.0


def test_saturated_valve_within_a_tenth_does_not_step_up() -> None:
    for deficit in (0.097, 0.1, 0.14):
        feedback = _ready()
        _run(feedback, [_sample(99, deficit=deficit)], T0, 3 * 3600)

        assert feedback.feedback_k == 0.0


def test_saturated_valve_at_or_above_target_does_not_step_up() -> None:
    for deficit in (0.0, -0.1):
        feedback = _ready()
        _run(feedback, [_sample(100, deficit=deficit)], T0, 3 * 3600)

        assert feedback.feedback_k <= 0.0


def test_each_step_up_needs_its_own_up_time_after_the_lock() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99)], T0, UP_AFTER_S)
    assert feedback.feedback_k == 1.0

    end = _run(feedback, [_sample(99)], end + 60, STEP_LOCK_UP_S - 120)
    assert feedback.feedback_k == 1.0
    assert feedback.count_s == 0.0
    end = _run(feedback, [_sample(99)], end + 60, UP_AFTER_S - 120)
    assert feedback.feedback_k == 1.0
    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 2.0


def test_low_demand_for_the_down_time_steps_down() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 60)
    assert feedback.feedback_k == 0.0
    assert feedback.count_s == DOWN_AFTER_S - 60

    _run(feedback, [_sample(60)], T0 + DOWN_AFTER_S, 0)
    assert feedback.feedback_k == -1.0
    assert feedback.count_s == 0.0


def test_wanting_after_a_surplus_first_works_it_off() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, 1740)
    assert feedback.count_s == 1740

    end = _run(feedback, [_sample(99)], end + 60, 1740 + UP_AFTER_S - 120)
    assert feedback.feedback_k == 0.0
    assert feedback.count_s == -1740
    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 1.0


def test_a_short_want_takes_back_but_keeps_the_rest() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 600)
    end = _run(feedback, [_sample(99)], end + 60, 240)
    assert feedback.count_s == DOWN_AFTER_S - 900

    end = _run(feedback, [_sample(60)], end + 60, 780)
    assert feedback.feedback_k == 0.0
    _run(feedback, [_sample(60)], end + 60, 0)
    assert feedback.feedback_k == -1.0


def test_a_holding_room_brings_the_count_back_to_zero() -> None:
    for demand in (60, 99):
        feedback = FlowFeedback()
        end = _run(feedback, [_sample(demand)], T0, 600)
        assert feedback.count_s != 0.0

        _run(feedback, [_sample(85, deficit=0.0)], end + 60, 3600)
        assert feedback.count_s == 0.0
        assert feedback.feedback_k == 0.0


def test_a_holding_room_short_of_its_target_does_not_step_up() -> None:
    feedback = _ready()
    _run(feedback, [_sample(85, deficit=0.5)], T0, 3 * 3600)

    assert feedback.feedback_k == 0.0
    assert feedback.count_s == 0.0


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


def test_open_valve_up_to_a_tenth_above_target_holds_the_flow() -> None:
    for deficit in (-0.05, -0.1, -0.14):
        feedback = _ready()
        _run(feedback, [_sample(91, deficit=deficit)], T0, DOWN_AFTER_S + 60)

        assert feedback.feedback_k == 0.0


def test_open_valve_rounded_beyond_a_tenth_above_target_does_not_hold() -> None:
    feedback = FlowFeedback()
    _run(
        feedback, [_sample(96, deficit=-0.15), _sample(60, room="B")], T0, DOWN_AFTER_S
    )

    assert feedback.feedback_k == -1.0


def test_lead_is_the_room_that_steps_up() -> None:
    feedback = FlowFeedback()
    _run(
        feedback,
        [_sample(92, deficit=0.6), _sample(95, deficit=-0.5, room="B")],
        T0,
        UP_AFTER_S,
    )

    assert feedback.feedback_k == 1.0
    assert feedback.lead.room == "A"


def test_clamped_to_limit() -> None:
    feedback = _ready()
    _run(feedback, [_sample(99)], T0, 10 * UP_AFTER_S)

    assert feedback.feedback_k == FEEDBACK_LIMIT_K


def test_no_step_against_a_target_limit() -> None:
    feedback = _ready()
    _run(feedback, [_sample(99)], T0, 3 * UP_AFTER_S, at_upper_limit=True)
    assert feedback.feedback_k == 0.0

    _run(feedback, [_sample(60)], T0, DOWN_AFTER_S + 60, at_lower_limit=True)
    assert feedback.feedback_k == 0.0


def test_a_freeze_drops_the_count_towards_up() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99)], T0, UP_AFTER_S - 120)
    end = _run(feedback, [_sample(99)], end + 60, 600, frozen=FeedbackFreeze.HOT_WATER)
    assert feedback.count_s == 0.0

    _run(feedback, [_sample(99)], end + 60, UP_AFTER_S - 120)
    assert feedback.feedback_k == 0.0


def test_a_freeze_keeps_the_count_towards_down() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, 1200)
    _run(feedback, [_sample(60)], end + 60, 600, frozen=FeedbackFreeze.HOT_WATER)

    assert feedback.count_s == 1200


def test_frozen_neither_steps_nor_counts() -> None:
    for reason in FeedbackFreeze:
        feedback = _ready()
        end = _run(feedback, [_sample(99)], T0, 3 * 3600, frozen=reason)
        assert feedback.feedback_k == 0.0
        assert feedback.frozen is reason

        # Low demand while frozen does not count towards a step down.
        feedback = _ready()
        end = _run(feedback, [_sample(60)], T0, 3 * 3600, frozen=reason)
        _run(feedback, [_sample(60)], end + 60, DOWN_AFTER_S - 120)
        assert feedback.feedback_k == 0.0


def test_no_samples_does_nothing() -> None:
    feedback = _ready()
    _run(feedback, [], T0, 3 * 3600)

    assert feedback.feedback_k == 0.0
    assert feedback.lead is None


def test_no_samples_pauses_the_count() -> None:
    feedback = _ready()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S - 660)
    end = _run(feedback, [], end + 60, 3 * 3600)
    assert feedback.count_s == DOWN_AFTER_S - 600

    _run(feedback, [_sample(60)], end + 60, 540)
    assert feedback.feedback_k == -1.0


def test_reset_starts_the_count_over() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_sample(99)], T0, UP_AFTER_S)
    assert feedback.feedback_k > 0.0

    start = T0 + 3 * UP_AFTER_S
    feedback.reset(start)
    assert feedback.feedback_k == 0.0
    assert feedback.last_change_ts == start
    assert feedback.count_s == 0.0
    assert feedback.locked_sign(start) == 0

    end = _run(feedback, [_sample(99)], start, UP_AFTER_S - 60)
    assert feedback.feedback_k == 0.0
    feedback.update([_sample(99)], STEP, None, end + 60)
    assert feedback.feedback_k == 1.0


def test_export_and_restore() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99)], T0, UP_AFTER_S)

    restored = FlowFeedback()
    restored.restore(feedback.export())
    assert restored.feedback_k == 1.0
    assert restored.last_change_ts == end
    assert restored.locked_sign(end + 60) == 1

    restored.restore({"feedback_k": 99})
    assert restored.feedback_k == FEEDBACK_LIMIT_K


def _want(room: str) -> DemandSample:
    return _sample(99, 0.2, room)


def _hold(room: str) -> DemandSample:
    return _sample(85, 0.0, room)


def _reserve(room: str) -> DemandSample:
    return _sample(50, 0.0, room)


def test_rooms_taking_turns_step_up_only_the_shared_count() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_want("A"), _hold("B")], T0, 15 * 60)
    assert feedback.room_count_s("A") == -15 * 60
    assert feedback.room_count_s("B") == 0.0

    _run(feedback, [_hold("A"), _want("B")], end + 60, 14 * 60)
    assert feedback.feedback_k == 1.0
    assert feedback.variant_up == 0
    assert feedback.room_steps_up == {}
    # The real step starts the room counts over.
    assert feedback.room_count_s("A") is None


def test_one_room_wanting_steps_up_both() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_want("A"), _reserve("B")], T0, UP_AFTER_S)

    assert feedback.feedback_k == 1.0
    assert feedback.variant_up == 1
    assert feedback.room_steps_up == {"A": 1}


def test_room_count_stops_at_the_down_time() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_reserve("A"), _hold("B")], T0, 2 * DOWN_AFTER_S)

    assert feedback.room_count_s("A") == DOWN_AFTER_S
    assert feedback.room_count_s("B") == 0.0
    assert feedback.variant_down == 0


def test_variant_steps_down_once_every_room_is_at_the_down_time() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_reserve("A"), _hold("B")], T0, 30 * 60)
    end = _run(feedback, [_reserve("A"), _reserve("B")], end + 60, 58 * 60)
    assert feedback.room_count_s("A") == DOWN_AFTER_S
    assert feedback.room_count_s("B") == DOWN_AFTER_S - 60
    assert feedback.variant_down == 0

    feedback.update([_reserve("A"), _reserve("B")], STEP, None, end + 60)
    assert feedback.variant_down == 1
    assert feedback.room_count_s("A") is None


def test_rooms_without_a_sample_wait() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_want("A"), _hold("B")], T0, 10 * 60)
    _run(feedback, [_hold("B")], end + 60, 10 * 60)

    assert feedback.room_count_s("A") == -10 * 60


def test_freeze_drops_room_counts_towards_up() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_want("A"), _reserve("B")], T0, 10 * 60)
    feedback.update([], STEP, FeedbackFreeze.HOT_WATER, end + 60)

    assert feedback.room_count_s("A") == 0.0
    assert feedback.room_count_s("B") == 10 * 60


def test_reset_clears_room_counts_but_keeps_tallies() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_want("A")], T0, UP_AFTER_S + 10 * 60)
    feedback.reset(end + 60)

    assert feedback.room_count_s("A") is None
    assert feedback.variant_up == 1


def test_export_and_restore_tallies() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_want("A")], T0, UP_AFTER_S)

    restored = FlowFeedback()
    restored.restore(feedback.export())
    assert restored.room_steps_up == {"A": 1}
    assert restored.variant_up == 1
    assert restored.variant_down == 0
    assert restored.room_count_s("A") is None

    restored.restore({"variant_up": "x", "room_steps_up": {"A": None}})
    assert restored.variant_up == 0
    assert restored.room_steps_up == {}


def _pause(feedback: FlowFeedback, reason: FeedbackFreeze, seconds: float) -> float:
    """A pause of the room heating from T0; returns the first time after it."""
    end = _run(feedback, [_sample(99)], T0, seconds - 60, frozen=reason)
    return end + 60


@pytest.mark.parametrize(
    ("reason", "pause_s", "catch_up_s"),
    [
        (FeedbackFreeze.HOT_WATER, 45 * 60, 45 * 60),
        (FeedbackFreeze.DEFROST, 8 * 60, 8 * 60),
        (FeedbackFreeze.HOT_WATER, 70 * 60, CATCH_UP_MAX_S),
    ],
)
def test_after_a_pause_the_rooms_catch_up_as_long_as_it_lasted(
    reason: FeedbackFreeze, pause_s: float, catch_up_s: float
) -> None:
    feedback = _ready()
    after = _pause(feedback, reason, pause_s)
    feedback.update([_sample(99)], STEP, None, after)

    assert feedback.catch_up_until_ts == after + catch_up_s
    assert feedback.catch_up_reason is reason
    assert feedback.catching_up(after + catch_up_s - 1)
    assert not feedback.catching_up(after + catch_up_s)


def test_short_rooms_do_not_count_up_while_catching_up() -> None:
    feedback = _ready()
    after = _pause(feedback, FeedbackFreeze.HOT_WATER, 45 * 60)
    end = _run(feedback, [_sample(99)], after, 45 * 60 - 60)
    assert feedback.count_s == 0.0
    assert feedback.room_count_s("A") == 0.0
    assert feedback.feedback_k == 0.0

    # Afterwards a short room counts as before.
    _run(feedback, [_sample(99)], end + 60, UP_AFTER_S)
    assert feedback.feedback_k == 1.0
    assert feedback.room_steps_up == {"A": 1}


def test_catching_up_still_brings_a_surplus_back_to_zero() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, 600)
    assert feedback.count_s == 600
    after = (
        _run(feedback, [_sample(60)], end + 60, 1200, frozen=FeedbackFreeze.HOT_WATER)
        + 60
    )
    end = _run(feedback, [_sample(99)], after, 900)

    assert feedback.catching_up(end)
    assert feedback.count_s == 0.0


def test_a_charge_running_into_a_defrost_is_one_pause() -> None:
    feedback = _ready()
    end = _run(
        feedback, [_sample(99)], T0, 30 * 60 - 60, frozen=FeedbackFreeze.HOT_WATER
    )
    end = _run(
        feedback, [_sample(99)], end + 60, 10 * 60 - 60, frozen=FeedbackFreeze.DEFROST
    )
    after = end + 60
    feedback.update([_sample(99)], STEP, None, after)

    assert feedback.catch_up_until_ts == after + 40 * 60
    assert feedback.catch_up_reason is FeedbackFreeze.HOT_WATER


def test_other_freezes_are_no_pause() -> None:
    feedback = _ready()
    after = _pause(feedback, FeedbackFreeze.WRITE_OFF, 45 * 60)
    feedback.update([_sample(99)], STEP, None, after)

    assert feedback.catch_up_until_ts is None
    assert not feedback.catching_up(after)


def test_reset_ends_the_catch_up() -> None:
    feedback = _ready()
    after = _pause(feedback, FeedbackFreeze.HOT_WATER, 45 * 60)
    feedback.update([_sample(99)], STEP, None, after)
    feedback.reset(after + 60)

    assert not feedback.catching_up(after + 120)


def test_a_room_above_its_target_counts_neither_way() -> None:
    feedback = FlowFeedback()
    _run(feedback, [_sample(30, deficit=-0.5)], T0, 3 * DOWN_AFTER_S)

    assert feedback.feedback_k == 0.0
    assert feedback.count_s == 0.0


def test_neutral_keeps_the_count() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, 600)
    _run(feedback, [_sample(30, deficit=-0.5)], end + 60, 3600)

    assert feedback.count_s == 600


@pytest.mark.parametrize(
    ("deficit", "seconds"),
    [(0.2, UP_AFTER_S), (0.4, UP_AFTER_S / 2), (0.9, UP_AFTER_S / 3)],
)
def test_further_short_steps_up_faster(deficit: float, seconds: float) -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99, deficit=deficit)], T0, seconds - 120)
    assert feedback.feedback_k == 0.0
    _run(feedback, [_sample(99, deficit=deficit)], end + 60, 60)
    assert feedback.feedback_k == 1.0


def test_the_lock_leaves_the_other_direction_open() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(99)], T0, UP_AFTER_S)
    assert feedback.feedback_k == 1.0

    _run(feedback, [_sample(60)], end + 60, DOWN_AFTER_S)
    assert feedback.feedback_k == 0.0


def test_the_lock_after_a_step_down_holds_down() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S)
    assert feedback.feedback_k == -1.0

    end = _run(feedback, [_sample(60)], end + 60, STEP_LOCK_DOWN_S - 60)
    assert feedback.feedback_k == -1.0
    _run(feedback, [_sample(60)], end + 60, DOWN_AFTER_S)
    assert feedback.feedback_k == -2.0


def test_the_lock_up_is_shorter_than_down_and_shorter_still_at_night() -> None:
    for night, lock_up in ((False, STEP_LOCK_UP_S), (True, NIGHT_STEP_LOCK_UP_S)):
        feedback = FlowFeedback()
        end = _run(feedback, [_sample(99)], T0, UP_AFTER_S, night=night)
        assert feedback.feedback_k == 1.0
        assert feedback.locked_sign(end + lock_up - 60) == 1
        assert feedback.locked_sign(end + lock_up) == 0

        # The second step up: the lock, then its own up time.
        end = _run(feedback, [_sample(99)], end + 60, lock_up - 60, night=night)
        assert feedback.feedback_k == 1.0
        _run(feedback, [_sample(99)], end + 60, UP_AFTER_S, night=night)
        assert feedback.feedback_k == 2.0


def test_the_lock_down_is_longer_at_night() -> None:
    for night, lock_down in (
        (False, STEP_LOCK_DOWN_S),
        (True, NIGHT_STEP_LOCK_DOWN_S),
    ):
        feedback = FlowFeedback()
        down_after = NIGHT_DOWN_AFTER_S if night else DOWN_AFTER_S
        end = _run(feedback, [_sample(60)], T0, down_after, night=night)
        assert feedback.feedback_k == -1.0
        assert feedback.locked_sign(end + lock_down - 60) == -1
        assert feedback.locked_sign(end + lock_down) == 0


def test_at_night_a_step_down_takes_longer() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S, night=True)
    assert feedback.feedback_k == 0.0
    assert feedback.count_s == DOWN_AFTER_S

    _run(feedback, [_sample(60)], end + 60, NIGHT_DOWN_AFTER_S - DOWN_AFTER_S - 60)
    assert feedback.feedback_k == -1.0


def test_at_night_a_step_up_is_not_held_back_by_a_day_step_down() -> None:
    feedback = FlowFeedback()
    end = _run(feedback, [_sample(60)], T0, DOWN_AFTER_S)
    assert feedback.feedback_k == -1.0

    _run(feedback, [_sample(99)], end + 60, UP_AFTER_S, night=True)
    assert feedback.feedback_k == 0.0

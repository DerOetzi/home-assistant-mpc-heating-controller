from heating_controller.controller.setpoint_correction import (
    CORRECTION_INTERVAL_S,
    CORRECTION_LIMIT_C,
    CORRECTION_WINDOW_S,
    SetpointCorrection,
)

START = 1_000_000.0
TARGET = 22.0


def _settled_hour(
    correction: SetpointCorrection,
    end_ts: float,
    room_c: float,
    demand_pct: float | None = 50.0,
) -> None:
    """A sample every 5 min across the hour before end_ts."""
    for minute in range(0, 61, 5):
        correction.add_sample(
            end_ts - CORRECTION_WINDOW_S + minute * 60, room_c, demand_pct
        )


def _evaluate_after(
    correction: SetpointCorrection,
    intervals: int,
    room_c: float,
    demand_pct: float | None = 50.0,
) -> float:
    now = START + intervals * CORRECTION_INTERVAL_S
    _settled_hour(correction, now, room_c, demand_pct)
    assert correction.due(now)
    correction.evaluate(now, TARGET)
    return now


def test_not_due_before_a_full_interval() -> None:
    correction = SetpointCorrection(START)
    assert not correction.due(START + CORRECTION_INTERVAL_S - 1)
    assert correction.due(START + CORRECTION_INTERVAL_S)


def test_a_cold_room_steps_up_by_half_a_kelvin_per_interval() -> None:
    correction = SetpointCorrection(START)

    _evaluate_after(correction, 1, 21.0)
    assert correction.correction_c == 0.5

    _evaluate_after(correction, 2, 21.0)
    assert correction.correction_c == 1.0


def test_the_correction_stops_at_its_limit() -> None:
    correction = SetpointCorrection(START)
    for interval in range(1, 8):
        _evaluate_after(correction, interval, 20.0)
    assert correction.correction_c == CORRECTION_LIMIT_C


def test_a_stored_value_beyond_the_limit_is_cut() -> None:
    assert CORRECTION_LIMIT_C == 3.0
    assert SetpointCorrection(START, 4.0).correction_c == CORRECTION_LIMIT_C
    assert SetpointCorrection(START, -4.0).correction_c == -CORRECTION_LIMIT_C


def test_a_value_set_by_hand_holds_for_a_full_interval() -> None:
    correction = SetpointCorrection(START)
    set_ts = START + CORRECTION_INTERVAL_S
    correction.set(set_ts, 1.5)

    assert correction.correction_c == 1.5
    assert not correction.due(set_ts + CORRECTION_INTERVAL_S - 1)
    assert correction.due(set_ts + CORRECTION_INTERVAL_S)


def test_a_value_set_by_hand_is_cut_at_the_limit() -> None:
    correction = SetpointCorrection(START)
    correction.set(START, -5.0)
    assert correction.correction_c == -CORRECTION_LIMIT_C


def test_a_warm_room_steps_down() -> None:
    correction = SetpointCorrection(START)
    _evaluate_after(correction, 1, 23.0)
    assert correction.correction_c == -0.5


def test_a_small_error_stays_in_the_deadband() -> None:
    correction = SetpointCorrection(START)
    now = _evaluate_after(correction, 1, 21.8)
    assert correction.correction_c == 0.0
    # Judged all the same: the next look waits a full interval.
    assert not correction.due(now + 1)


def test_a_fully_open_valve_does_not_step_up() -> None:
    correction = SetpointCorrection(START)
    _evaluate_after(correction, 1, 21.0, demand_pct=100.0)
    assert correction.correction_c == 0.0


def test_a_shut_valve_does_not_step_down() -> None:
    correction = SetpointCorrection(START)
    _evaluate_after(correction, 1, 23.0, demand_pct=0.0)
    assert correction.correction_c == 0.0


def test_without_a_valve_reading_the_step_goes_through() -> None:
    correction = SetpointCorrection(START)
    _evaluate_after(correction, 1, 21.0, demand_pct=None)
    assert correction.correction_c == 0.5


def test_too_few_samples_wait_for_the_next_call() -> None:
    correction = SetpointCorrection(START)
    now = START + CORRECTION_INTERVAL_S
    correction.add_sample(now - 60, 21.0, 50.0)

    assert correction.evaluate(now, TARGET) is False
    assert correction.correction_c == 0.0
    assert correction.due(now + 1)


def test_a_window_that_starts_late_is_not_judged() -> None:
    correction = SetpointCorrection(START)
    now = START + CORRECTION_INTERVAL_S
    for minute in range(30, 61, 5):
        correction.add_sample(now - CORRECTION_WINDOW_S + minute * 60, 21.0, 50.0)

    assert correction.evaluate(now, TARGET) is False
    assert correction.correction_c == 0.0


def test_a_room_still_moving_is_not_judged() -> None:
    correction = SetpointCorrection(START)
    now = START + CORRECTION_INTERVAL_S
    for minute in range(0, 61, 5):
        correction.add_sample(
            now - CORRECTION_WINDOW_S + minute * 60, 20.0 + minute / 60, 50.0
        )

    assert correction.evaluate(now, TARGET) is False
    assert correction.due(now + 1)


def test_the_sample_before_the_window_holds_its_start() -> None:
    correction = SetpointCorrection(START)
    now = START + CORRECTION_INTERVAL_S
    # Reported once before the window and twice inside it.
    correction.add_sample(now - CORRECTION_WINDOW_S - 600, 21.0, 50.0)
    correction.add_sample(now - 1200, 21.1, 50.0)
    correction.add_sample(now - 60, 21.1, 50.0)

    assert correction.evaluate(now, TARGET) is True
    assert correction.correction_c == 0.5

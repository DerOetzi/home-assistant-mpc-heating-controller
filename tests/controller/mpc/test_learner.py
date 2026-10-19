import time

import pytest

from heating_controller.const import (
    CapacityOutcome,
    Construction,
    LearningStatus,
    RoomTemperatureStrategy,
    UaBalanceOutcome,
)
from heating_controller.controller.mpc.learner import (
    UA_BALANCE_MAX_INTERRUPTION_S,
    UA_BALANCE_MAX_WINDOW_S,
    UA_BALANCE_MIN_WINDOW_S,
    UA_BALANCE_WEIGHT,
    LearnerPrediction,
    RoomMpcModelLearner,
)
from heating_controller.controller.mpc.models.capacity import ThermalCapacityModel
from heating_controller.controller.mpc.models.loss import RoomLossModel
from heating_controller.controller.mpc.results import RoomMpcInput
from heating_controller.controller.mpc.types import LearningFactors, RoomThermalConfig


def make_learner():
    # 90 m² very light construction: 7.2 MJ/K, the capacity the windows below
    # were worked out with.
    thermal_config = RoomThermalConfig(
        room_heat_load_w=1000, room_area_m2=90, construction=Construction.VERY_LIGHT
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    return RoomMpcModelLearner(loss_model, capacity_model), loss_model, capacity_model


def make_input(
    now_ts,
    room_temp_c=20.0,
    outdoor_temp_c=-5.0,
    flow_temp_c=45.0,
    room_sensor_fallback=False,
):
    return RoomMpcInput(
        now_ts=now_ts,
        target_temp_c=21.0,
        room_temp_c=room_temp_c,
        outdoor_temp_c=outdoor_temp_c,
        flow_temp_c=flow_temp_c,
        used_room_sensor_strategy=(
            RoomTemperatureStrategy.TRV_AVERAGE
            if room_sensor_fallback
            else RoomTemperatureStrategy.ROOM_SENSOR
        ),
        trv_temperatures=[room_temp_c],
        room_sensor_fallback=room_sensor_fallback,
    )


def test_disabled_learner_is_noop():
    learner, _, _ = make_learner()
    assert learner.get_learning_state().status == LearningStatus.DISABLED
    learner.run_learning_cycle()
    assert learner.get_learning_state().status == LearningStatus.DISABLED


def test_enable_sets_waiting_interval():
    learner, _, _ = make_learner()
    learner.enable()
    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL


def test_run_learning_cycle_waits_without_active_prediction():
    learner, _, _ = make_learner()
    learner.enable()
    learner.run_learning_cycle()
    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL


START_TS = 1_000_000.0


def _evaluate_window(learner, inputs, power_w=100.0):
    """Run the cycle that evaluates a prediction against the given window."""
    learner.set_prediction(
        LearnerPrediction(
            timestamp=START_TS,
            predicted_room_temperature_c=20.0,
            prediction_horizon_s=1800,
        )
    )
    learner.run_learning_cycle()

    for mpc_input in inputs:
        learner.append_history(mpc_input, applied_heating_power_w=power_w)

    learner.run_learning_cycle()


def _inputs(temperatures, outdoor_temp_c=-5.0):
    return [
        make_input(START_TS + index * 300, room_temp_c=room_temp_c, outdoor_temp_c=outdoor_temp_c)
        for index, room_temp_c in enumerate(temperatures)
    ]


# Test room: UA 1000 W / 32 K = 31.25 W/K, capacity 90 m² * 80 kJ/m²K = 7.2 MJ/K.
# At 18 °C and -5 °C outside it loses ~720 W; 2 kW heating lets the replay rise
# by ~0.25 K over the window.
RISING = (18.0, 18.2, 18.4, 18.6, 18.8, 19.0)


def test_cycle_reports_no_correction_when_the_room_matched_the_replay():
    learner, _, capacity_model = make_learner()
    learner.enable()

    # 719 W covers the loss at 18 °C: the replay stays flat like the room.
    _evaluate_window(learner, _inputs((18.0,) * 6), power_w=719.0)

    assert learner.get_learning_state().status == LearningStatus.NO_CORRECTION
    assert capacity_model.learned_capacity_factor == 1.0


def test_cycle_reports_disturbed_when_the_outdoor_temperature_drifts():
    learner, _, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(
        learner,
        [
            make_input(START_TS + index * 300, room_temp_c=room_temp_c, outdoor_temp_c=-5.0 - index)
            for index, room_temp_c in enumerate(RISING)
        ],
        power_w=2000.0,
    )

    assert learner.get_learning_state().status == LearningStatus.DISTURBED
    assert capacity_model.learned_capacity_factor == 1.0


def test_cycle_reports_disturbed_when_the_flow_temperature_jumps():
    learner, _, capacity_model = make_learner()
    learner.enable()

    # A domestic hot water charge on the shared leaving-water sensor.
    flow_temperatures = [45.0, 45.0, 55.0, 55.0, 45.0, 45.0]
    _evaluate_window(
        learner,
        [
            make_input(START_TS + index * 300, room_temp_c=room_temp_c, flow_temp_c=flow_temp_c)
            for index, (room_temp_c, flow_temp_c) in enumerate(zip(RISING, flow_temperatures))
        ],
        power_w=2000.0,
    )

    assert learner.get_learning_state().status == LearningStatus.DISTURBED
    assert capacity_model.learned_capacity_factor == 1.0


def test_suppress_for_interval_marks_current_window_invalid():
    learner, _, _ = make_learner()
    learner.enable()
    learner.suppress_for_interval(3600, 3600)
    assert learner.get_learning_state().status == LearningStatus.SUPPRESSED

    learner.run_learning_cycle()
    assert learner.get_learning_state().status == LearningStatus.SUPPRESSED


def test_recalibrate_sets_factors_directly():
    learner, loss_model, capacity_model = make_learner()
    learner.recalibrate(LearningFactors(ua_factor=1.3, capacity_factor=0.8))
    assert loss_model.learned_ua_factor == 1.3
    assert capacity_model.learned_capacity_factor == 0.8


def test_consume_persisted_learning_factors_returns_once():
    learner, _, _ = make_learner()
    learner.enable()

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    factors = learner.consume_persisted_learning_factors()
    assert factors is not None
    assert learner.consume_persisted_learning_factors() is None


def test_stationary_window_leaves_capacity_alone():
    learner, loss_model, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(learner, _inputs((18.0,) * 6), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.NO_CORRECTION
    assert loss_model.learned_ua_factor == 1.0
    assert capacity_model.learned_capacity_factor == 1.0


def test_capacity_drops_when_the_room_overshoots_a_simulated_rise():
    learner, _, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor < 1.0


def test_capacity_rises_when_the_room_falls_less_than_simulated():
    learner, _, capacity_model = make_learner()
    learner.recalibrate(LearningFactors(ua_factor=1.0, capacity_factor=0.5))
    learner.enable()

    # With half the capacity and -30 °C outside the unheated replay drops ~0.65 K.
    _evaluate_window(
        learner,
        _inputs((20.0, 19.95, 19.9, 19.85, 19.8, 19.75), outdoor_temp_c=-30.0),
        power_w=0.0,
    )

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor > 0.5


def test_capacity_drops_when_the_room_falls_more_than_simulated():
    learner, _, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(learner, _inputs((20.0, 19.8, 19.6, 19.4, 19.2, 19.0)), power_w=0.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor < 1.0


def test_capacity_replays_the_applied_power_not_the_plan():
    learner, _, capacity_model = make_learner()
    learner.enable()

    # The prediction aimed at 20 °C, far above the room. Replayed with the power
    # that was applied, the room rose exactly as modelled: nothing to learn.
    learner.set_prediction(
        LearnerPrediction(
            timestamp=START_TS, predicted_room_temperature_c=20.0, prediction_horizon_s=1800
        )
    )
    learner.run_learning_cycle()
    for index in range(6):
        learner.append_history(
            make_input(START_TS + index * 300, room_temp_c=18.0 + index * 0.0533),
            applied_heating_power_w=2000.0,
        )
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.NO_CORRECTION
    assert capacity_model.learned_capacity_factor == 1.0


def test_capacity_ignores_windows_with_almost_no_simulated_change():
    learner, _, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(learner, _inputs((18.0, 18.1, 18.2, 18.3, 18.4, 18.5)), power_w=719.0)

    assert learner.get_learning_state().status == LearningStatus.NO_CORRECTION
    assert capacity_model.learned_capacity_factor == 1.0


def test_paused_learning_blocks_capacity():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_learning_paused(True, START_TS - 600)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.PAUSED
    assert capacity_model.learned_capacity_factor == 1.0


def test_pause_shows_immediately_while_enabled():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_learning_paused(True, START_TS)

    assert learner.get_learning_state().status == LearningStatus.PAUSED


def test_enable_while_paused_reports_paused():
    learner, _, _ = make_learner()
    learner.set_learning_paused(True, START_TS)
    learner.enable()

    assert learner.get_learning_state().status == LearningStatus.PAUSED


def test_window_spanning_the_resume_is_discarded():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_learning_paused(True, START_TS - 600)
    learner.set_learning_paused(False, START_TS + 300)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.PAUSED
    assert capacity_model.learned_capacity_factor == 1.0


def test_capacity_window_after_the_resume_learns():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_learning_paused(True, START_TS - 1200)
    learner.set_learning_paused(False, START_TS - 600)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor != 1.0


def test_inactive_learning_window_still_lets_capacity_learn():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, START_TS - 600)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor != 1.0


def test_sun_condition_blocks_capacity():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_sun_condition(lambda ts: ts < START_TS + 900)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.OUTSIDE_WINDOW
    assert capacity_model.learned_capacity_factor == 1.0


def test_capacity_learns_once_its_shorter_suppression_ended():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.suppress_for_interval(3600, 0)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor != 1.0


# Heat balance for UA. The test room loses 31.25 W/K * 20 K = 625 W at 20 °C
# room and 0 °C outside. Holding the room with 312.5 W means half the UA.
NIGHT_START_TS = 2_000_000.0
STEP_S = 300
# Longer than the maximum stretch, so the balance runs without a closing event.
NIGHT_HOURS = UA_BALANCE_MAX_WINDOW_S / 3600 + 0.5
NIGHT_COUNT = int(NIGHT_HOURS * 3600 / STEP_S) + 1


def _feed_night(
    learner,
    hours=NIGHT_HOURS,
    start_ts=NIGHT_START_TS,
    room_temp_c=20.0,
    outdoor_temp_c=0.0,
    power_w=312.5,
    room_temps=None,
    skip=(),
    room_sensor_fallback=False,
):
    count = int(hours * 3600 / STEP_S) + 1
    for index in range(count):
        if index in skip:
            continue
        learner.append_history(
            make_input(
                start_ts + index * STEP_S,
                room_temp_c=room_temps[index] if room_temps else room_temp_c,
                outdoor_temp_c=outdoor_temp_c,
                flow_temp_c=40.0,
                room_sensor_fallback=room_sensor_fallback,
            ),
            applied_heating_power_w=power_w,
        )
    return start_ts + (count - 1) * STEP_S


def _expected_factor(target_factor, current=1.0):
    return current + UA_BALANCE_WEIGHT * (target_factor - current)


def test_heat_balance_moves_ua_towards_the_power_that_held_the_room():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))
    assert learner.consume_persisted_learning_factors() is not None


def test_heat_balance_learns_a_low_target_above_the_lower_limit():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner, power_w=125.0)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.2))


def test_heat_balance_keeps_collecting_until_the_maximum_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner, hours=UA_BALANCE_MAX_WINDOW_S / 3600 - 0.5)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.COLLECTING
    assert loss_model.learned_ua_factor == 1.0


def test_closing_the_learning_window_balances_the_stretch():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=5)
    learner.set_learning_window_active(False, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))
    assert learner.get_ua_balance_info().last_window_s == pytest.approx(5 * 3600)


def test_heat_balance_needs_the_minimum_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=UA_BALANCE_MIN_WINDOW_S / 3600 - 0.5)
    learner.set_learning_window_active(False, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.TOO_SHORT
    assert loss_model.learned_ua_factor == 1.0


def test_closed_stretch_is_balanced_only_once():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=5)
    learner.set_learning_window_active(False, end_ts + 60)
    learner.run_learning_cycle()
    after_first = loss_model.learned_ua_factor
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.OUTSIDE_WINDOW
    assert loss_model.learned_ua_factor == after_first


def test_heat_balance_runs_once_per_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner)
    learner.run_learning_cycle()
    after_first = loss_model.learned_ua_factor

    learner.append_history(
        make_input(end_ts + STEP_S, room_temp_c=20.0, outdoor_temp_c=0.0, flow_temp_c=40.0),
        applied_heating_power_w=312.5,
    )
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == after_first


def test_heat_balance_accounts_for_heat_stored_in_the_room():
    learner, loss_model, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    rising = [20.0 + 0.2 * index / (count - 1) for index in range(count)]
    _feed_night(learner, room_temps=rising)
    learner.run_learning_cycle()

    # Part of the heat went into warming the room, so less of it was lost.
    assert loss_model.learned_ua_factor < _expected_factor(0.5)


def test_heat_balance_skips_windows_where_the_room_drifted():
    learner, loss_model, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    falling = [21.0 - 0.5 * index / (count - 1) for index in range(count)]
    _feed_night(learner, room_temps=falling)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_counts_hot_water_charges_as_no_heating():
    learner, loss_model, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    # Power during the charge is what the model would deliver at the spiked
    # flow; the radiators actually got nothing.
    for index in range(count):
        charging = 30 <= index < 36
        learner.append_history(
            make_input(
                NIGHT_START_TS + index * STEP_S,
                room_temp_c=20.0,
                outdoor_temp_c=0.0,
                flow_temp_c=55.0 if charging else 40.0,
            ),
            applied_heating_power_w=5000.0 if charging else 312.5,
        )
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor < _expected_factor(0.5)


def test_heat_balance_needs_a_meaningful_temperature_difference():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner, outdoor_temp_c=18.0, power_w=30.0)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_skips_windows_with_gaps():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner, skip=range(20, 26))
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_starts_when_the_learning_window_opens():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, NIGHT_START_TS)
    learner.set_learning_window_active(True, NIGHT_START_TS + 3600)

    _feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS + 3600


def test_heat_balance_learns_when_the_learning_window_opened_before():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, NIGHT_START_TS - 7200)
    learner.set_learning_window_active(True, NIGHT_START_TS - 3600)

    _feed_night(learner)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_inactive_learning_window_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, NIGHT_START_TS - 60)

    _feed_night(learner)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_paused_learning_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_paused(True, NIGHT_START_TS)

    _feed_night(learner)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.PAUSED
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_starts_after_the_resume():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_learning_paused(True, NIGHT_START_TS - 600)
    learner.set_learning_paused(False, NIGHT_START_TS + 3600)

    _feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS + 3600


def test_pause_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=5)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_stretch_ends_at_sunrise():
    learner, loss_model, _ = make_learner()
    learner.enable()
    sunrise_ts = NIGHT_START_TS + 6 * 3600
    learner.set_sun_condition(lambda ts: ts < sunrise_ts)

    _feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_learned_ts == sunrise_ts - STEP_S
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_stretch_starts_once_the_sun_condition_holds():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_sun_condition(lambda ts: ts >= NIGHT_START_TS + 3600)

    _feed_night(learner, hours=3)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().collecting_since_ts == NIGHT_START_TS + 3600


def test_ua_suppression_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # Suppression is measured in wall-clock time; the night ends just before.
    _feed_night(learner, start_ts=time.time() - 5 * 3600 - 60, hours=5)
    learner.suppress_for_interval(1800, 0)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_ua_suppression_starts_the_next_stretch_after_it():
    learner, _, _ = make_learner()
    learner.enable()
    learner.suppress_for_interval(1800, 0)
    blocked_until_ts = time.time() + 1800

    _feed_night(learner, start_ts=blocked_until_ts - 3600, hours=3)
    learner.run_learning_cycle()

    since_ts = learner.get_ua_balance_info().collecting_since_ts
    assert since_ts == pytest.approx(blocked_until_ts, abs=STEP_S)
    assert since_ts >= blocked_until_ts - 1


def test_short_heating_interruption_is_bridged_as_no_heating():
    learner, loss_model, _ = make_learner()
    learner.enable()
    off_ts = NIGHT_START_TS + 2 * 3600 + 5
    # A missed heat-source poll: off for three controller cycles and a second.
    learner.disable(now_ts=off_ts)
    learner.enable(now_ts=off_ts + 15 * 60 + 1)

    end_ts = _feed_night(learner, hours=6)
    learner.set_learning_window_active(False, end_ts + 60)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(6 * 3600)
    # The requested power during the interruption never reached the room.
    assert info.last_target_factor == pytest.approx(0.5 * (1 - 3 / 72))


def test_long_heating_interruption_ends_the_stretch():
    learner, _, _ = make_learner()
    learner.enable()

    _feed_night(learner, hours=1)
    learner.disable(now_ts=NIGHT_START_TS + 3600)
    enable_ts = NIGHT_START_TS + 3600 + UA_BALANCE_MAX_INTERRUPTION_S + 60
    _feed_night(learner, start_ts=NIGHT_START_TS + 3900, hours=0.5)
    learner.enable(now_ts=enable_ts)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.TOO_SHORT

    _feed_night(learner, start_ts=NIGHT_START_TS + 5700, hours=1)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().collecting_since_ts > NIGHT_START_TS + 3600


def test_stretch_before_heating_stayed_off_is_balanced_while_disabled():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=5)
    learner.disable(now_ts=end_ts + 1)
    # History keeps growing while disabled.
    _feed_night(learner, start_ts=end_ts + STEP_S, hours=0.5)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.DISABLED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))
    assert learner.consume_persisted_learning_factors() is not None


def test_recalibrate_discards_the_running_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner)
    learner.recalibrate(LearningFactors(ua_factor=1.2, capacity_factor=1.0))
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL
    assert loss_model.learned_ua_factor == 1.2


def test_balance_info_reports_the_learned_balance():
    learner, _, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_learned_ts == end_ts
    assert info.last_target_factor == pytest.approx(0.5)
    assert info.last_drift_c == pytest.approx(0.0)
    assert info.last_window_s == pytest.approx(NIGHT_HOURS * 3600)
    assert info.collecting_since_ts is None


def test_balance_info_reports_collecting_with_its_start():
    learner, _, _ = make_learner()
    learner.enable()

    _feed_night(learner, hours=2)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS
    assert info.last_learned_ts is None


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        (
            {"room_temps": [21.0 - 0.5 * i / (NIGHT_COUNT - 1) for i in range(NIGHT_COUNT)]},
            UaBalanceOutcome.DRIFT,
        ),
        ({"skip": range(20, 26)}, UaBalanceOutcome.GAP),
        (
            {"outdoor_temp_c": 18.0, "power_w": 30.0},
            UaBalanceOutcome.SMALL_TEMPERATURE_DIFFERENCE,
        ),
    ],
)
def test_balance_info_names_why_a_balance_was_skipped(kwargs, outcome):
    learner, _, _ = make_learner()
    learner.enable()

    _feed_night(learner, **kwargs)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == outcome


def test_balance_info_reports_an_inactive_learning_window():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, NIGHT_START_TS)

    _feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.OUTSIDE_WINDOW
    assert info.collecting_since_ts is None


@pytest.mark.parametrize(
    ("power_w", "target_factor"),
    [(0.0, 0.0), (62.5, 0.1), (1500.0, 2.4)],
)
def test_heat_balance_discards_an_implausible_target(power_w, target_factor):
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner, power_w=power_w)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.IMPLAUSIBLE_TARGET
    assert loss_model.learned_ua_factor == 1.0
    assert info.last_target_factor == pytest.approx(target_factor)
    assert info.last_drift_c == pytest.approx(0.0)
    assert info.last_learned_ts is None
    assert learner.consume_persisted_learning_factors() is None


def test_heat_balance_discards_a_stretch_dominated_by_stored_heat():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # 0.25 K rise stores 1.8 MJ, more than half of the 3.1 MJ requested.
    rising = [20.0 + 0.25 * index / (NIGHT_COUNT - 1) for index in range(NIGHT_COUNT)]
    _feed_night(learner, room_temps=rising, power_w=100.0)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.STORAGE_DOMINATED
    assert loss_model.learned_ua_factor == 1.0
    assert info.last_drift_c > 0.2
    assert info.last_target_factor is not None
    assert info.last_learned_ts is None


def test_discarded_stretch_is_not_balanced_again():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, power_w=0.0)
    learner.run_learning_cycle()
    learner.append_history(
        make_input(end_ts + STEP_S, room_temp_c=20.0, outdoor_temp_c=0.0, flow_temp_c=40.0),
        applied_heating_power_w=0.0,
    )
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.COLLECTING
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_skips_mild_nights():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # 7 K mean difference: below the mild-night limit even though the power
    # matches the modelled losses.
    _feed_night(learner, outdoor_temp_c=13.0, power_w=218.75)
    learner.run_learning_cycle()

    assert (
        learner.get_ua_balance_info().outcome
        == UaBalanceOutcome.SMALL_TEMPERATURE_DIFFERENCE
    )
    assert loss_model.learned_ua_factor == 1.0


def _replay_matching_window(learner):
    learner.set_prediction(
        LearnerPrediction(
            timestamp=START_TS, predicted_room_temperature_c=20.0, prediction_horizon_s=1800
        )
    )
    learner.run_learning_cycle()
    for index in range(6):
        learner.append_history(
            make_input(START_TS + index * 300, room_temp_c=18.0 + index * 0.0533),
            applied_heating_power_w=2000.0,
        )
    learner.run_learning_cycle()


def _paused(learner):
    learner.set_learning_paused(True, START_TS - 600)
    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)


def _suppressed(learner):
    learner.suppress_for_interval(3600, 3600)
    learner.run_learning_cycle()


def _outside_window(learner):
    learner.set_sun_condition(lambda ts: ts < START_TS + 900)
    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)


def _disturbed(learner):
    _evaluate_window(
        learner,
        [
            make_input(START_TS + index * 300, room_temp_c=room_temp_c, outdoor_temp_c=-5.0 - index)
            for index, room_temp_c in enumerate(RISING)
        ],
        power_w=2000.0,
    )


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [
        (lambda learner: learner.run_learning_cycle(), CapacityOutcome.WAITING),
        (
            lambda learner: _evaluate_window(learner, _inputs(RISING), power_w=2000.0),
            CapacityOutcome.LEARNED,
        ),
        (
            lambda learner: _evaluate_window(learner, _inputs((18.0,) * 6), power_w=2000.0),
            CapacityOutcome.STATIONARY,
        ),
        (_replay_matching_window, CapacityOutcome.SMALL_ERROR),
        (
            lambda learner: _evaluate_window(
                learner, _inputs((18.0, 18.1, 18.2, 18.3, 18.4, 18.5)), power_w=719.0
            ),
            CapacityOutcome.SMALL_CHANGE,
        ),
        (_disturbed, CapacityOutcome.DISTURBED),
        (_outside_window, CapacityOutcome.OUTSIDE_WINDOW),
        (_paused, CapacityOutcome.PAUSED),
        (_suppressed, CapacityOutcome.SUPPRESSED),
    ],
)
def test_capacity_outcome_names_the_branch(scenario, outcome):
    learner, _, _ = make_learner()
    learner.enable()

    scenario(learner)

    assert learner.get_capacity_outcome() == outcome


def test_capacity_outcome_reports_a_disabled_learner():
    learner, _, _ = make_learner()
    learner.enable()
    learner.disable()
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.DISABLED


def test_capacity_skips_windows_with_room_sensor_fallback():
    learner, _, capacity_model = make_learner()
    learner.enable()
    inputs = _inputs(RISING)
    inputs[2] = make_input(
        inputs[2].now_ts, room_temp_c=RISING[2], room_sensor_fallback=True
    )

    _evaluate_window(learner, inputs, power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.SUPPRESSED
    assert learner.get_capacity_outcome() == CapacityOutcome.ROOM_SENSOR_FALLBACK
    assert capacity_model.learned_capacity_factor == 1.0


def test_room_sensor_fallback_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=5)
    _feed_night(
        learner, hours=0.5, start_ts=end_ts + STEP_S, room_temp_c=19.0,
        room_sensor_fallback=True,
    )
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(5 * 3600)
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_room_sensor_fallback_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()

    _feed_night(learner, room_temp_c=19.0, room_sensor_fallback=True)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.ROOM_SENSOR_FALLBACK
    assert loss_model.learned_ua_factor == 1.0


def test_stretch_restarts_when_the_room_sensor_is_back():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=2, room_temp_c=19.0, room_sensor_fallback=True)
    back_ts = end_ts + STEP_S
    _feed_night(learner, hours=3, start_ts=back_ts)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == back_ts
    assert loss_model.learned_ua_factor == 1.0


def test_open_window_blocks_capacity_however_long_it_stays_open():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_window_open(True, START_TS - 3 * 3600)

    _evaluate_window(learner, _inputs(RISING), power_w=2000.0)

    assert learner.get_learning_state().status == LearningStatus.SUPPRESSED
    assert learner.get_capacity_outcome() == CapacityOutcome.SUPPRESSED
    assert capacity_model.learned_capacity_factor == 1.0


def test_opening_a_window_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = _feed_night(learner, hours=5)
    learner.set_window_open(True, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_open_window_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_window_open(True, NIGHT_START_TS - 60)

    _feed_night(learner)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.WINDOW_OPEN
    assert loss_model.learned_ua_factor == 1.0


def test_stretch_starts_after_a_long_open_window_closes():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_window_open(True, NIGHT_START_TS - 60)

    end_ts = _feed_night(learner, hours=6)
    closed_ts = end_ts + 60
    learner.set_window_open(False, closed_ts)
    _feed_night(learner, hours=3, start_ts=end_ts + STEP_S)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == end_ts + STEP_S
    assert loss_model.learned_ua_factor == 1.0

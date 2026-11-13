import time

import pytest
from heating_controller.const import (
    CapacityOutcome,
    Construction,
    LearningStatus,
    UaBalanceOutcome,
)
from heating_controller.controller.mpc import learner as learner_module
from heating_controller.controller.mpc.learner import (
    CAPACITY_JUMP_WEIGHT,
    UA_BALANCE_MAX_DRIFT_C,
    UA_BALANCE_MAX_INTERRUPTION_S,
    UA_BALANCE_MAX_WINDOW_S,
    UA_BALANCE_MIN_TARGET_FACTOR,
    UA_BALANCE_MIN_WINDOW_S,
    UA_BALANCE_WEIGHT,
    RoomMpcModelLearner,
)
from heating_controller.controller.mpc.models.capacity import (
    MIN_LEARNED_CAPACITY_FACTOR,
    ThermalCapacityModel,
)
from heating_controller.controller.mpc.models.gains import RoomGainModel
from heating_controller.controller.mpc.models.loss import RoomLossModel
from heating_controller.controller.mpc.types import LearningFactors, RoomThermalConfig
from learner_helpers import (
    NIGHT_HOURS,
    NIGHT_START_TS,
    STEP_S,
    feed_jump,
    feed_night,
    feed_samples,
    jump_samples,
    make_input,
)


def make_learner():
    # 90 m² very light construction: 7.2 MJ/K, the capacity the windows below
    # were worked out with.
    thermal_config = RoomThermalConfig(
        room_heat_load_w=1000, room_area_m2=90, construction=Construction.VERY_LIGHT
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    return RoomMpcModelLearner(loss_model, capacity_model), loss_model, capacity_model


def test_disabled_learner_is_noop():
    learner, _, _ = make_learner()
    assert learner.get_learning_state().status == LearningStatus.DISABLED
    learner.run_learning_cycle()
    assert learner.get_learning_state().status == LearningStatus.DISABLED


def test_enable_sets_waiting_interval():
    learner, _, _ = make_learner()
    learner.enable()
    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL


START_TS = 1_000_000.0


def test_recalibrate_sets_factors_directly():
    learner, loss_model, capacity_model = make_learner()
    learner.recalibrate(LearningFactors(ua_factor=1.3, capacity_factor=0.8))
    assert loss_model.learned_ua_factor == 1.3
    assert capacity_model.learned_capacity_factor == 0.8


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


# Samples in one feed_night of NIGHT_HOURS.
NIGHT_COUNT = int(NIGHT_HOURS * 3600 / STEP_S) + 1


def _expected_factor(target_factor, current=1.0):
    return current + UA_BALANCE_WEIGHT * (target_factor - current)


def test_heat_balance_moves_ua_towards_the_power_that_held_the_room():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))
    assert learner.consume_persisted_learning_factors() is not None


def test_heat_balance_learns_a_low_target_above_the_lower_limit():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, power_w=125.0)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.2))


def test_heat_balance_keeps_collecting_until_the_maximum_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, hours=UA_BALANCE_MAX_WINDOW_S / 3600 - 0.5)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.COLLECTING
    assert loss_model.learned_ua_factor == 1.0


def test_pausing_learning_balances_the_stretch():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))
    assert learner.get_ua_balance_info().last_window_s == pytest.approx(5 * 3600)


def test_heat_balance_needs_the_minimum_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=UA_BALANCE_MIN_WINDOW_S / 3600 - 0.5)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.TOO_SHORT
    assert loss_model.learned_ua_factor == 1.0


def test_closed_stretch_is_balanced_only_once():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()
    after_first = loss_model.learned_ua_factor
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.PAUSED
    assert loss_model.learned_ua_factor == after_first


def test_heat_balance_runs_once_per_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner)
    learner.run_learning_cycle()
    after_first = loss_model.learned_ua_factor

    learner.append_history(
        make_input(
            end_ts + STEP_S, room_temp_c=20.0, outdoor_temp_c=0.0, flow_temp_c=40.0
        ),
        applied_heating_power_w=312.5,
        radiator_power_w=312.5,
    )
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == after_first


def test_heat_balance_accounts_for_heat_stored_in_the_room():
    learner, loss_model, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    rising = [20.0 + 0.2 * index / (count - 1) for index in range(count)]
    feed_night(learner, room_temps=rising)
    learner.run_learning_cycle()

    # Part of the heat went into warming the room, so less of it was lost.
    assert loss_model.learned_ua_factor < _expected_factor(0.5)


def test_heat_balance_skips_windows_where_the_room_drifted():
    learner, loss_model, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    falling = [21.0 - 0.5 * index / (count - 1) for index in range(count)]
    feed_night(learner, room_temps=falling)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_counts_hot_water_charges_as_no_heating():
    learner, loss_model, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    # The observer's power lags into the charge; the radiators actually got
    # nothing.
    for index in range(count):
        charging = 30 <= index < 36
        learner.append_history(
            make_input(
                NIGHT_START_TS + index * STEP_S,
                room_temp_c=20.0,
                outdoor_temp_c=0.0,
                flow_temp_c=40.0,
                target_temp_c=20.0,
                hot_water_charge=charging,
            ),
            applied_heating_power_w=5000.0 if charging else 312.5,
            radiator_power_w=5000.0 if charging else 312.5,
        )
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor < _expected_factor(0.5)


def test_heat_balance_needs_a_meaningful_temperature_difference():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, outdoor_temp_c=18.0, power_w=30.0)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_skips_windows_with_gaps():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, skip=range(20, 26))
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == 1.0


def test_paused_learning_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_paused(True, NIGHT_START_TS)

    feed_night(learner)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.PAUSED
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_starts_after_the_resume():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_learning_paused(True, NIGHT_START_TS - 600)
    learner.set_learning_paused(False, NIGHT_START_TS + 3600)

    feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS + 3600


def test_pause_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_missing_outdoor_temperature_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    learner.set_outdoor_temperature_missing(True, end_ts + 60)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_heat_balance_starts_after_the_outdoor_temperature_is_back():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_outdoor_temperature_missing(True, NIGHT_START_TS - 600)
    learner.set_outdoor_temperature_missing(False, NIGHT_START_TS + 3600)

    feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS + 3600


def test_stretch_ends_at_sunrise():
    learner, loss_model, _ = make_learner()
    learner.enable()
    sunrise_ts = NIGHT_START_TS + 6 * 3600
    learner.set_sun_condition(lambda ts: ts < sunrise_ts)

    feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_learned_ts == sunrise_ts - STEP_S
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_stretch_starts_once_the_sun_condition_holds():
    learner, _, _ = make_learner()
    learner.enable()
    learner.set_sun_condition(lambda ts: ts >= NIGHT_START_TS + 3600)

    feed_night(learner, hours=3)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().collecting_since_ts == NIGHT_START_TS + 3600


def test_a_room_moving_after_sunrise_is_outside_the_window_not_coasting():
    # Dani and shower room, 31 October: balanced at sunrise, then warming
    # up in the morning while the night mode was still on.
    learner, _, _ = make_learner()
    learner.enable()
    sunrise_ts = NIGHT_START_TS + 6 * 3600
    learner.set_sun_condition(lambda ts: ts < sunrise_ts)

    end_ts = feed_night(
        learner,
        hours=6.5,
        room_temps=[20.0 + 0.05 * max(0, index - 72) for index in range(79)],
    )
    learner.run_learning_cycle()
    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED

    feed_night(
        learner,
        hours=1,
        start_ts=end_ts + STEP_S,
        room_temps=[20.35 + 0.05 * index for index in range(13)],
    )
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.OUTSIDE_WINDOW


def test_pausing_learning_after_sunrise_balances_nothing():
    learner, loss_model, _ = make_learner()
    learner.enable()
    sunrise_ts = NIGHT_START_TS + 6 * 3600
    learner.set_sun_condition(lambda ts: ts < sunrise_ts)

    end_ts = feed_night(learner, hours=7)
    learner.run_learning_cycle()
    learned_factor = loss_model.learned_ua_factor

    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.PAUSED
    assert loss_model.learned_ua_factor == learned_factor


def test_ua_suppression_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # Suppression is measured in wall-clock time; the night ends just before.
    feed_night(learner, start_ts=time.time() - 5 * 3600 - 60, hours=5)
    learner.suppress_for_interval(1800, 0)
    learner.run_learning_cycle()

    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_ua_suppression_starts_the_next_stretch_after_it():
    learner, _, _ = make_learner()
    learner.enable()
    learner.suppress_for_interval(1800, 0)
    blocked_until_ts = time.time() + 1800

    feed_night(learner, start_ts=blocked_until_ts - 3600, hours=3)
    learner.run_learning_cycle()

    since_ts = learner.get_ua_balance_info().collecting_since_ts
    assert since_ts == pytest.approx(blocked_until_ts, abs=STEP_S)
    assert since_ts >= blocked_until_ts - 1


def test_short_heating_interruption_is_bridged_as_no_heating():
    learner, _, _ = make_learner()
    learner.enable()
    off_ts = NIGHT_START_TS + 2 * 3600 + 5
    # A missed heat-source poll: off for three controller cycles and a second.
    learner.disable(now_ts=off_ts)
    learner.enable(now_ts=off_ts + 15 * 60 + 1)

    end_ts = feed_night(learner, hours=6)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(6 * 3600)
    # The requested power during the interruption never reached the room.
    assert info.last_target_factor == pytest.approx(0.5 * (1 - 3 / 72))


def test_long_heating_interruption_ends_the_stretch():
    learner, _, _ = make_learner()
    learner.enable()

    feed_night(learner, hours=1)
    learner.disable(now_ts=NIGHT_START_TS + 3600)
    enable_ts = NIGHT_START_TS + 3600 + UA_BALANCE_MAX_INTERRUPTION_S + 60
    feed_night(learner, start_ts=NIGHT_START_TS + 3900, hours=0.5)
    learner.enable(now_ts=enable_ts)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.TOO_SHORT

    feed_night(learner, start_ts=NIGHT_START_TS + 5700, hours=1)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().collecting_since_ts > NIGHT_START_TS + 3600


def test_stretch_before_heating_stayed_off_is_balanced_while_disabled():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    learner.disable(now_ts=end_ts + 1)
    # History keeps growing while disabled.
    feed_night(learner, start_ts=end_ts + STEP_S, hours=0.5)
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.DISABLED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))
    assert learner.consume_persisted_learning_factors() is not None


def test_recalibrate_discards_the_running_window():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner)
    learner.recalibrate(LearningFactors(ua_factor=1.2, capacity_factor=1.0))
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL
    assert loss_model.learned_ua_factor == 1.2


def test_balance_info_reports_the_learned_balance():
    learner, _, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_learned_ts == end_ts
    assert info.last_target_factor == pytest.approx(0.5)
    assert info.last_drift_c == pytest.approx(0.0)
    assert info.last_window_s == pytest.approx(NIGHT_HOURS * 3600)
    assert info.collecting_since_ts is None


def test_balance_reports_the_mean_outdoor_room_flow_and_heat():
    learner, _, _ = make_learner()
    learner.enable()

    feed_night(
        learner,
        room_temp_c=21.0,
        outdoor_temp_c=5.0,
        target_temp_c=21.0,
        power_w=300.0,
        flow_temp_c=35.0,
    )
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_mean_outdoor_c == pytest.approx(5)
    assert info.last_mean_room_c == pytest.approx(21)
    assert info.last_mean_flow_c == pytest.approx(35)
    assert info.last_mean_heating_w == pytest.approx(300)


def test_balance_reports_no_mean_flow_without_flow_samples():
    learner, _, _ = make_learner()
    learner.enable()

    feed_night(learner, flow_temp_c=None)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.last_mean_flow_c is None
    assert info.last_mean_room_c == pytest.approx(20)


def test_rejected_balance_still_reports_the_means():
    learner, _, _ = make_learner()
    learner.enable()

    count = NIGHT_COUNT
    falling = [21.0 - 0.5 * index / (count - 1) for index in range(count)]
    feed_night(
        learner,
        room_temps=falling,
        outdoor_temp_c=5.0,
        flow_temp_c=33.0,
        target_temp_c=21.0,
    )
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.DRIFT
    assert info.last_mean_outdoor_c == pytest.approx(5)
    assert info.last_mean_room_c == pytest.approx(20.75, abs=0.01)
    assert info.last_mean_flow_c == pytest.approx(33)
    assert info.last_mean_heating_w == pytest.approx(312.5)


def test_balance_info_reports_collecting_with_its_start():
    learner, _, _ = make_learner()
    learner.enable()

    feed_night(learner, hours=2)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS
    assert info.last_learned_ts is None


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        (
            {
                "room_temps": [
                    21.0 - 0.5 * i / (NIGHT_COUNT - 1) for i in range(NIGHT_COUNT)
                ],
                "target_temp_c": 21.0,
            },
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

    feed_night(learner, **kwargs)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == outcome


def test_heat_balance_discards_a_target_above_the_limit():
    power_w, target_factor = 1500.0, 2.4
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, power_w=power_w)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.IMPLAUSIBLE_TARGET
    assert loss_model.learned_ua_factor == 1.0
    assert info.last_target_factor == pytest.approx(target_factor)
    assert info.last_drift_c == pytest.approx(0.0)
    assert info.last_learned_ts is None
    assert learner.consume_persisted_learning_factors() is None


@pytest.mark.parametrize(
    ("power_w", "target_factor"),
    [(0.0, 0.0), (62.5, 0.1)],
)
def test_heat_balance_learns_a_low_target_only_down_to_the_limit(
    power_w, target_factor
):
    # A room that lives off its neighbours: the factor moves towards the
    # limit, the measured target stays visible.
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, power_w=power_w)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED_AT_LIMIT
    assert loss_model.learned_ua_factor == pytest.approx(
        _expected_factor(UA_BALANCE_MIN_TARGET_FACTOR)
    )
    assert info.last_target_factor == pytest.approx(target_factor)
    assert info.last_learned_ts is not None
    assert learner.consume_persisted_learning_factors() is not None


def test_drift_is_reported_without_a_target():
    learner, loss_model, _ = make_learner()
    learner.enable()

    falling = [21.0 - 0.5 * index / (NIGHT_COUNT - 1) for index in range(NIGHT_COUNT)]
    feed_night(learner, room_temps=falling, target_temp_c=21.0)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.DRIFT
    assert info.last_drift_c == pytest.approx(-0.47, abs=0.03)
    assert info.last_target_factor is None
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_discards_a_stretch_dominated_by_stored_heat():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # 0.25 K rise stores 1.8 MJ, more than half of the 3.1 MJ requested.
    rising = [20.0 + 0.25 * index / (NIGHT_COUNT - 1) for index in range(NIGHT_COUNT)]
    feed_night(learner, room_temps=rising, power_w=100.0)
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

    # A target of 2.4: above the plausible limit, discarded.
    end_ts = feed_night(learner, power_w=1500.0)
    learner.run_learning_cycle()
    learner.append_history(
        make_input(
            end_ts + STEP_S, room_temp_c=20.0, outdoor_temp_c=0.0, flow_temp_c=40.0
        ),
        applied_heating_power_w=1500.0,
        radiator_power_w=1500.0,
    )
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.COLLECTING
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_skips_mild_nights():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # 7 K mean difference: below the mild-night limit even though the power
    # matches the modelled losses.
    feed_night(learner, outdoor_temp_c=13.0, power_w=218.75)
    learner.run_learning_cycle()

    assert (
        learner.get_ua_balance_info().outcome
        == UaBalanceOutcome.SMALL_TEMPERATURE_DIFFERENCE
    )
    assert loss_model.learned_ua_factor == 1.0


def test_room_sensor_fallback_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    feed_night(
        learner,
        hours=0.5,
        start_ts=end_ts + STEP_S,
        room_temp_c=19.0,
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

    feed_night(learner, room_temp_c=19.0, room_sensor_fallback=True)
    learner.run_learning_cycle()

    assert (
        learner.get_ua_balance_info().outcome == UaBalanceOutcome.ROOM_SENSOR_FALLBACK
    )
    assert loss_model.learned_ua_factor == 1.0


def test_stretch_restarts_when_the_room_sensor_is_back():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=2, room_temp_c=19.0, room_sensor_fallback=True)
    back_ts = end_ts + STEP_S
    feed_night(learner, hours=3, start_ts=back_ts)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == back_ts
    assert loss_model.learned_ua_factor == 1.0


def test_opening_a_window_balances_the_stretch_before_it():
    learner, loss_model, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    learner.set_window_open(True, end_ts + 60)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_open_window_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_window_open(True, NIGHT_START_TS - 60)

    feed_night(learner)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.WINDOW_OPEN
    assert loss_model.learned_ua_factor == 1.0


def test_stretch_starts_after_a_long_open_window_closes():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_window_open(True, NIGHT_START_TS - 60)

    end_ts = feed_night(learner, hours=6)
    closed_ts = end_ts + 60
    learner.set_window_open(False, closed_ts)
    feed_night(learner, hours=3, start_ts=end_ts + STEP_S)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == end_ts + STEP_S
    assert loss_model.learned_ua_factor == 1.0


def _coasting_then_settled():
    # Starts 0.6 K above the 21 °C setpoint and coasts down over two hours,
    # then holds just above it.
    coast_steps = 24
    return [
        21.6 - 0.55 * min(index, coast_steps) / coast_steps
        for index in range(NIGHT_COUNT)
    ]


def test_heat_balance_starts_once_the_room_has_settled():
    learner, loss_model, _ = make_learner()
    learner.enable()

    temps = _coasting_then_settled()
    end_ts = feed_night(learner, hours=6, room_temps=temps, target_temp_c=21.0)
    learner.run_learning_cycle()

    settled_index = next(i for i, t in enumerate(temps) if t <= 21.1)
    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS + settled_index * STEP_S

    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    # Balanced from the start the drift would be about -0.55 K.
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert abs(info.last_drift_c) < 0.1
    assert loss_model.learned_ua_factor < 1.0


def test_heat_balance_reports_a_room_that_never_settles():
    learner, loss_model, _ = make_learner()
    learner.enable()

    # Coasting down 0.1 K an hour all night, always above setpoint + 0.1 K.
    coasting = [22.5 - 0.1 * index * STEP_S / 3600 for index in range(NIGHT_COUNT)]
    feed_night(learner, room_temps=coasting)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.COASTING
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_starts_once_a_room_above_setpoint_holds_still():
    # Kept above its setpoint by its neighbours, the room never comes down:
    # an hour without a trend is enough to start the stretch, and that hour
    # counts.
    learner, _, _ = make_learner()
    learner.enable()

    feed_night(learner, hours=3, room_temp_c=21.4, target_temp_c=21.0)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS


def test_a_still_room_counts_from_the_start_of_its_still_hour():
    # Coasting down from above its setpoint for an hour, then held above it
    # by its neighbours: the stretch starts where the room stopped moving.
    learner, _, _ = make_learner()
    learner.enable()

    coast_steps = 12
    temps = [21.8 - 0.4 * min(index, coast_steps) / coast_steps for index in range(37)]
    feed_night(learner, hours=3, room_temps=temps, target_temp_c=21.0)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    # The fitted hour already reads still with the last few coasting minutes
    # in it, a few hundredths of a kelvin.
    still_since_ts = NIGHT_START_TS + coast_steps * STEP_S
    assert info.collecting_since_ts == pytest.approx(still_since_ts, abs=3 * STEP_S)
    assert info.collecting_since_ts < NIGHT_START_TS + 2 * 3600


def test_a_later_cycle_finds_the_same_still_hour():
    learner, _, _ = make_learner()
    learner.enable()

    coast_steps = 12
    temps = [21.8 - 0.4 * min(index, coast_steps) / coast_steps for index in range(37)]
    feed_night(learner, hours=3, room_temps=temps, target_temp_c=21.0)
    learner.run_learning_cycle()
    first_since_ts = learner.get_ua_balance_info().collecting_since_ts
    learner.run_learning_cycle()

    assert first_since_ts is not None
    assert learner.get_ua_balance_info().collecting_since_ts == first_since_ts


def _pulsing_temps():
    # Heating pulses every 2.5 hours lift the room 0.5 K, then it coasts back;
    # the night ends on a peak. The edges differ by ~0.35 K, the trend is flat.
    period = 30
    temps = []
    for index in range(NIGHT_COUNT):
        phase = index % period
        temps.append(
            20.95 + (0.5 * phase / 6 if phase < 6 else 0.5 - 0.5 * (phase - 6) / 24)
        )
    return temps


@pytest.mark.parametrize(
    "temps",
    [
        pytest.param(_pulsing_temps(), id="heating pulses"),
        # A room sensor reporting once an hour: one low report in the last
        # half hour.
        pytest.param([21.0] * (NIGHT_COUNT - 6) + [20.67] * 6, id="hourly sensor"),
        # A hot-water dip in the first half hour.
        pytest.param([20.7] * 6 + [21.0] * (NIGHT_COUNT - 6), id="dip at the start"),
    ],
)
def test_drift_follows_the_trend_not_the_edges(temps):
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, room_temps=temps, target_temp_c=21.0)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome != UaBalanceOutcome.DRIFT
    assert abs(info.last_drift_c) < UA_BALANCE_MAX_DRIFT_C
    assert loss_model.learned_ua_factor != 1.0


def test_drift_weights_samples_by_the_time_they_stand_for():
    learner, _, _ = make_learner()
    learner.enable()

    # Flat at 21 °C, but a burst of samples a minute apart in the last half
    # hour reads 20.5 °C for only five minutes. Counted per sample it would
    # tilt the trend; weighted by time it hardly does.
    end_ts = feed_night(learner, hours=7.5, room_temp_c=21.0, target_temp_c=21.0)
    for minute in range(1, 6):
        learner.append_history(
            make_input(
                end_ts + minute * 60,
                room_temp_c=20.5,
                outdoor_temp_c=0.0,
                flow_temp_c=40.0,
            ),
            applied_heating_power_w=312.5,
            radiator_power_w=312.5,
        )
    feed_night(
        learner,
        hours=0.5,
        start_ts=end_ts + 6 * 60,
        room_temp_c=21.0,
        target_temp_c=21.0,
    )
    learner.run_learning_cycle()

    window = learner._history
    times = [entry.timestamp for entry in window]
    temps = [entry.room_temperature_c for entry in window]
    mean_t = sum(times) / len(times)
    mean_c = sum(temps) / len(temps)
    per_sample_drift = (
        sum((t - mean_t) * (c - mean_c) for t, c in zip(times, temps))
        / sum((t - mean_t) ** 2 for t in times)
        * (times[-1] - times[0])
    )
    weighted_drift = learner.get_ua_balance_info().last_drift_c
    assert abs(weighted_drift) < 0.03
    assert abs(weighted_drift) < abs(per_sample_drift) / 2


def test_missing_valve_position_balances_the_stretch_before_it():
    learner, _, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    feed_night(
        learner, hours=0.5, start_ts=end_ts + STEP_S, valve_position_missing=True
    )
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(5 * 3600)


def test_missing_valve_position_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, valve_position_missing=True)
    learner.run_learning_cycle()

    assert (
        learner.get_ua_balance_info().outcome == UaBalanceOutcome.VALVE_POSITION_MISSING
    )
    assert loss_model.learned_ua_factor == 1.0


def test_an_adapting_trv_ends_the_stretch_before_it():
    learner, _, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    feed_night(learner, hours=0.5, start_ts=end_ts + STEP_S, trv_adapting=True)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(5 * 3600)


def test_an_adapting_trv_blocks_the_heat_balance():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, trv_adapting=True)
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.TRV_ADAPTING
    assert loss_model.learned_ua_factor == 1.0


def test_heat_balance_starts_once_a_room_below_setpoint_has_recovered():
    # Back from a valve calibration that kept the radiator shut, the room
    # climbs back to its setpoint; the stretch starts once it is within the
    # margin below the target band, not while it still recovers.
    learner, _, _ = make_learner()
    learner.enable()

    temps = [min(20.4 + 0.05 * index, 21.0) for index in range(NIGHT_COUNT)]
    feed_night(learner, hours=6, room_temps=temps, target_temp_c=21.0)
    learner.run_learning_cycle()

    recovered_index = next(i for i, t in enumerate(temps) if t >= 20.9 - 1e-9)
    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.COLLECTING
    assert info.collecting_since_ts == NIGHT_START_TS + recovered_index * STEP_S


def test_a_radiator_not_heating_ends_the_stretch_before_it():
    learner, _, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=5)
    feed_night(learner, hours=0.5, start_ts=end_ts + STEP_S, radiator_not_heating=True)
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(5 * 3600)


def test_a_radiator_not_heating_ends_the_stretch_when_its_valve_opened():
    # Dining room, 31 October: valve open from 03:38, reported at 04:50; the
    # hour in between went into the balance with heat the radiator may not
    # have given off.
    learner, _, _ = make_learner()
    learner.enable()

    end_ts = feed_night(learner, hours=6)
    opened_ts = end_ts - 3600
    learner.append_history(
        make_input(
            end_ts + STEP_S,
            room_temp_c=20.0,
            target_temp_c=20.0,
            radiator_not_heating=True,
            radiator_not_heating_since_ts=opened_ts,
        ),
        applied_heating_power_w=312.5,
        radiator_power_w=312.5,
    )
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert info.last_window_s == pytest.approx(5 * 3600)


def test_a_radiator_not_heating_blocks_learning():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, radiator_not_heating=True)
    learner.run_learning_cycle()

    assert (
        learner.get_ua_balance_info().outcome == UaBalanceOutcome.RADIATOR_NOT_HEATING
    )
    assert loss_model.learned_ua_factor == 1.0


def test_a_night_with_fully_open_valves_below_the_band_is_not_learned():
    # Study, 28/29 October: valve open all night, room below its setpoint.
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(
        learner, room_temp_c=20.0, target_temp_c=20.5, valve_positions_pct=(100.0,)
    )
    learner.run_learning_cycle()

    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.VALVE_SATURATED
    assert info.last_target_factor == pytest.approx(0.5)
    assert loss_model.learned_ua_factor == 1.0


def test_fully_open_valves_at_the_setpoint_still_learn():
    learner, loss_model, _ = make_learner()
    learner.enable()

    feed_night(learner, valve_positions_pct=(100.0,))
    learner.run_learning_cycle()

    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED
    assert loss_model.learned_ua_factor == pytest.approx(_expected_factor(0.5))


def test_a_drifting_still_start_is_retried_from_where_the_room_came_near():
    # Living room, 29/30 October: flat at 21.7 for an hour after the evening
    # (only coasting slower than the sensor steps), then down to its
    # setpoint. Balanced from the still hour the drift is far too large; from
    # where it came near its setpoint the night still counts.
    learner, loss_model, _ = make_learner()
    learner.enable()

    flat_steps, fall_steps = 12, 12
    temps = [
        21.7
        if index < flat_steps
        else max(21.0, 21.7 - 0.7 * (index - flat_steps) / fall_steps)
        for index in range(NIGHT_COUNT)
    ]
    end_ts = feed_night(learner, room_temps=temps, target_temp_c=21.0)
    learner.set_learning_paused(True, end_ts + 60)
    learner.run_learning_cycle()

    near_index = next(i for i, t in enumerate(temps) if t <= 21.1 + 1e-9)
    info = learner.get_ua_balance_info()
    assert info.outcome == UaBalanceOutcome.LEARNED
    assert abs(info.last_drift_c) < 0.1
    assert info.last_window_s == pytest.approx(
        end_ts - (NIGHT_START_TS + near_index * STEP_S)
    )
    assert loss_model.learned_ua_factor < 1.0


def _make_learner_with_gains():
    from heating_controller.controller.mpc.models.gains import RoomGainModel

    thermal_config = RoomThermalConfig(
        room_heat_load_w=1000, room_area_m2=90, construction=Construction.VERY_LIGHT
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    gains = RoomGainModel(1000, loss_model.base_ua_w_per_k)
    learner = RoomMpcModelLearner(loss_model, capacity_model, gain_model=gains)
    return learner, loss_model, gains


def test_balanced_nights_feed_the_gain_regression():
    learner, loss_model, gains = _make_learner_with_gains()
    learner.enable()
    # Room 20 °C, UA 15 W/K and a steady gain of 50 W.
    for night, outdoor_c in enumerate((0.0, -5.0, 5.0)):
        delta_c = 20.0 - outdoor_c
        feed_night(
            learner,
            start_ts=NIGHT_START_TS + night * 86400,
            outdoor_temp_c=outdoor_c,
            power_w=15.0 * delta_c - 50.0,
        )
        learner.run_learning_cycle()
        assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.LEARNED

    assert gains.night_points == 3
    assert gains.learned_gain_w == pytest.approx(50.0, abs=5)
    assert gains.gain_determined
    # The single-night step of the factor is unchanged in this stage.
    factor = 1.0
    for outdoor_c in (0.0, -5.0, 5.0):
        delta_c = 20.0 - outdoor_c
        target = (15.0 * delta_c - 50.0) / delta_c / loss_model.base_ua_w_per_k
        factor = _expected_factor(target, factor)
    assert loss_model.learned_ua_factor == pytest.approx(factor)


def test_rejected_night_gives_no_gain_point():
    learner, _, gains = _make_learner_with_gains()
    learner.enable()
    feed_night(
        learner,
        room_temps=[20.0 + 0.6 * index / NIGHT_COUNT for index in range(NIGHT_COUNT)],
    )
    learner.run_learning_cycle()
    assert learner.get_ua_balance_info().outcome == UaBalanceOutcome.DRIFT
    assert gains.night_points == 0


# Jump stretches. A small room: 20 m² very light (1.6 MJ/K base), UA 15.625
# W/K. Its true capacity of 0.48 MJ/K is a factor of 0.3, as the rooms show.
JUMP_TS = 3_000_000.0
BASE_CAPACITY_J_PER_K = 20 * 80.0 * 1000
TRUE_CAPACITY_J_PER_K = 0.3 * BASE_CAPACITY_J_PER_K
SMALL_UA_W_PER_K = 500 / 32


def make_small_learner(gain_model_surfaces=None):
    thermal_config = RoomThermalConfig(
        room_heat_load_w=500, room_area_m2=20, construction=Construction.VERY_LIGHT
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    assert loss_model.effective_ua_w_per_k == pytest.approx(SMALL_UA_W_PER_K)
    assert capacity_model.base_capacity_j_per_k == pytest.approx(BASE_CAPACITY_J_PER_K)
    gain_model = (
        None
        if gain_model_surfaces is None
        else RoomGainModel(
            500, loss_model.base_ua_w_per_k, window_surfaces=gain_model_surfaces
        )
    )
    learner = RoomMpcModelLearner(loss_model, capacity_model, gain_model=gain_model)
    learner.enable()
    return learner, capacity_model


HEAT_UP_JUMP = {
    "true_capacity_j_per_k": TRUE_CAPACITY_J_PER_K,
    "ua_w_per_k": SMALL_UA_W_PER_K,
    "start_room_c": 18.0,
    "old_target_c": 18.0,
    "new_target_c": 20.0,
    "power_w": 600.0,
}
COOL_DOWN_JUMP = {
    **HEAT_UP_JUMP,
    "start_room_c": 20.0,
    "old_target_c": 20.0,
    "new_target_c": 18.0,
    "power_w": 0.0,
}


def test_a_heat_up_stretch_learns_the_capacity():
    learner, capacity_model = make_small_learner()
    feed_jump(learner, start_ts=JUMP_TS, **HEAT_UP_JUMP)
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.LEARNED
    assert info.kind == "heat_up"
    assert info.start_ts == JUMP_TS + 2 * STEP_S
    assert info.room_delta_c == pytest.approx(1.8, abs=0.11)
    assert info.estimate_factor == pytest.approx(0.3, rel=0.2)
    assert info.last_learned_ts == info.end_ts
    assert capacity_model.learned_capacity_factor == pytest.approx(
        1.0 + CAPACITY_JUMP_WEIGHT * (info.estimate_factor - 1.0)
    )
    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert learner.consume_persisted_learning_factors().capacity_factor == (
        capacity_model.learned_capacity_factor
    )
    assert learner.consume_persisted_learning_factors() is None


def test_repeated_stretches_converge_on_the_true_capacity():
    learner, capacity_model = make_small_learner()
    ts = JUMP_TS
    for _ in range(10):
        ts = feed_jump(learner, start_ts=ts, **HEAT_UP_JUMP)
        ts = feed_jump(learner, start_ts=ts, **COOL_DOWN_JUMP)
        learner.run_learning_cycle()

    assert capacity_model.learned_capacity_factor == pytest.approx(0.3, rel=0.15)


def test_a_cool_down_stretch_only_estimates():
    learner, capacity_model = make_small_learner()
    feed_jump(learner, start_ts=JUMP_TS, **COOL_DOWN_JUMP)
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.ESTIMATED
    assert info.kind == "cool_down"
    assert info.estimate_factor == pytest.approx(0.3, rel=0.2)
    assert info.last_learned_ts is None
    assert capacity_model.learned_capacity_factor == 1.0
    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL


@pytest.mark.parametrize("unknown", [False, True])
def test_an_open_or_unknown_window_at_the_start_blocks_the_stretch(unknown):
    learner, capacity_model = make_small_learner()
    if unknown:
        learner.set_window_unknown(True)
    else:
        learner.set_window_open(True, JUMP_TS)
    feed_jump(learner, start_ts=JUMP_TS, **HEAT_UP_JUMP)
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.WINDOW_OPEN
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_window_opening_mid_stretch_keeps_the_part_before():
    learner, capacity_model = make_small_learner()
    samples, _ = jump_samples(start_ts=JUMP_TS, **HEAT_UP_JUMP)
    feed_samples(learner, samples[:8])
    learner.set_window_open(True, samples[8][0])
    feed_samples(learner, samples[8:])
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.LEARNED
    assert info.end_ts == samples[7][0]
    assert info.estimate_factor == pytest.approx(0.3, rel=0.2)
    assert capacity_model.learned_capacity_factor < 1.0


def test_a_flag_at_the_start_blocks_the_stretch():
    learner, capacity_model = make_small_learner()
    feed_jump(learner, start_ts=JUMP_TS, radiator_not_heating=True, **HEAT_UP_JUMP)
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.RADIATOR_NOT_HEATING
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_constant_setpoint_leaves_the_capacity_waiting():
    learner, capacity_model = make_small_learner()
    feed_night(learner, start_ts=JUMP_TS, power_w=312.5)
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.WAITING
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_room_barely_moving_is_too_small():
    learner, capacity_model = make_small_learner()
    feed_jump(
        learner,
        start_ts=JUMP_TS,
        **{
            **HEAT_UP_JUMP,
            "start_room_c": 19.6,
            "old_target_c": 19.0,
            "power_w": 400.0,
        },
    )
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.TOO_SMALL
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_gap_in_the_stretch_is_not_learned():
    learner, capacity_model = make_small_learner()
    samples, _ = jump_samples(start_ts=JUMP_TS, **HEAT_UP_JUMP)
    # Five samples missing: 30 minutes without data.
    feed_samples(learner, samples[:4] + samples[9:])
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.GAP
    assert capacity_model.learned_capacity_factor == 1.0


def test_an_estimate_out_of_range_is_implausible():
    learner, capacity_model = make_small_learner()
    feed_jump(
        learner,
        start_ts=JUMP_TS,
        **{
            **HEAT_UP_JUMP,
            "true_capacity_j_per_k": 6.0 * BASE_CAPACITY_J_PER_K,
            "power_w": 2000.0,
        },
    )
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.IMPLAUSIBLE
    assert info.estimate_factor == pytest.approx(6.0, rel=0.2)
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_room_warming_without_heat_is_implausible():
    learner, capacity_model = make_small_learner()
    samples, _ = jump_samples(start_ts=JUMP_TS, **HEAT_UP_JUMP)
    feed_samples(
        learner, [(ts, room_c, target_c, 0.0) for ts, room_c, target_c, _ in samples]
    )
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.IMPLAUSIBLE
    assert info.estimate_factor is None
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_running_stretch_is_judged_once_it_ends():
    learner, capacity_model = make_small_learner()
    samples, _ = jump_samples(start_ts=JUMP_TS, **HEAT_UP_JUMP)
    feed_samples(learner, samples[:5])
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.RUNNING
    assert info.start_ts == JUMP_TS + 2 * STEP_S
    assert capacity_model.learned_capacity_factor == 1.0

    feed_samples(learner, samples[5:])
    learner.run_learning_cycle()
    assert learner.get_capacity_outcome() == CapacityOutcome.LEARNED
    learned_factor = capacity_model.learned_capacity_factor
    assert learned_factor < 1.0

    learner.run_learning_cycle()
    assert capacity_model.learned_capacity_factor == learned_factor


def test_a_disabled_learner_still_estimates_cool_down():
    learner, capacity_model = make_small_learner()
    learner.disable(JUMP_TS)
    feed_jump(learner, start_ts=JUMP_TS, **COOL_DOWN_JUMP)
    learner.run_learning_cycle()

    assert learner.get_capacity_outcome() == CapacityOutcome.ESTIMATED
    assert learner.get_learning_state().status == LearningStatus.DISABLED
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_suppression_delays_the_start_of_the_stretch(monkeypatch):
    learner, capacity_model = make_small_learner()
    jump_ts = JUMP_TS + 2 * STEP_S
    monkeypatch.setattr(learner_module.time, "time", lambda: jump_ts - 60)
    learner.suppress_for_interval(1800, 600)
    feed_jump(learner, start_ts=JUMP_TS, **HEAT_UP_JUMP)
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.start_ts == jump_ts + 2 * STEP_S
    assert info.outcome == CapacityOutcome.LEARNED
    assert capacity_model.learned_capacity_factor < 1.0


def test_an_estimate_below_the_lower_limit_is_implausible():
    learner, capacity_model = make_small_learner()
    feed_jump(
        learner,
        start_ts=JUMP_TS,
        step_s=120,
        **{
            **HEAT_UP_JUMP,
            "true_capacity_j_per_k": 0.05 * BASE_CAPACITY_J_PER_K,
            "power_w": 400.0,
        },
    )
    learner.run_learning_cycle()

    info = learner.get_capacity_info()
    assert info.outcome == CapacityOutcome.IMPLAUSIBLE
    assert info.estimate_factor == pytest.approx(0.05, rel=0.25)
    assert capacity_model.learned_capacity_factor == 1.0


def test_a_small_capacity_converges_on_the_lower_limit():
    learner, capacity_model = make_small_learner()
    ts = JUMP_TS
    for _ in range(15):
        ts = feed_jump(
            learner,
            start_ts=ts,
            step_s=120,
            **{**HEAT_UP_JUMP, "true_capacity_j_per_k": 0.12 * BASE_CAPACITY_J_PER_K},
        )
        ts = feed_jump(
            learner,
            start_ts=ts,
            step_s=120,
            **{
                **COOL_DOWN_JUMP,
                "true_capacity_j_per_k": 0.12 * BASE_CAPACITY_J_PER_K,
            },
        )
        learner.run_learning_cycle()

    assert learner.get_capacity_info().estimate_factor == pytest.approx(0.12, rel=0.2)
    assert capacity_model.learned_capacity_factor == MIN_LEARNED_CAPACITY_FACTOR


# Sun on the windows. 2026-11-02 12:00 UTC.
NOON_TS = 1_793_577_600.0 + 12 * 3600


class FixedSun:
    """The same irradiance on every surface, or a function of time."""

    def __init__(self, value):
        self.value = value

    def value_at(self, surface, ts):
        return self.value(ts) if callable(self.value) else self.value

    def filtered_at(self, surface, ts, tau_s):
        return self.value_at(surface, ts)


def _sun_learner(sun, surfaces=("south",), dark=True):
    learner, _ = make_small_learner(gain_model_surfaces=surfaces)
    learner.set_gain_context(lambda ts: 0, lambda ts: 0, lambda: sun)
    learner.set_sun_condition(lambda ts: dark)
    feed_jump(learner, start_ts=NOON_TS, **HEAT_UP_JUMP)
    learner.run_learning_cycle()
    return learner.get_capacity_outcome()


def test_a_stretch_without_sun_is_learned_at_midday():
    assert _sun_learner(FixedSun(0.0)) == CapacityOutcome.LEARNED


def test_sun_on_the_windows_blocks_the_stretch():
    assert _sun_learner(FixedSun(200.0)) == CapacityOutcome.OUTSIDE_WINDOW


def test_sun_shortly_before_the_stretch_blocks_it():
    jump_ts = NOON_TS + 2 * STEP_S
    sun = FixedSun(lambda ts: 200.0 if ts <= jump_ts - 90 * 60 else 0.0)
    assert _sun_learner(sun) == CapacityOutcome.OUTSIDE_WINDOW


def test_a_room_without_windows_ignores_the_sun():
    assert _sun_learner(FixedSun(200.0), surfaces=()) == CapacityOutcome.LEARNED


@pytest.mark.parametrize(
    ("dark", "outcome"),
    [(True, CapacityOutcome.LEARNED), (False, CapacityOutcome.OUTSIDE_WINDOW)],
)
def test_without_a_sun_source_the_sun_condition_decides(dark, outcome):
    assert _sun_learner(None, dark=dark) == outcome

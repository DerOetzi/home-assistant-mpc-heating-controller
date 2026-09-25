from heating_controller.const import LearningStatus, RoomTemperatureStrategy
from heating_controller.controller.mpc.learner import (
    LearnerPrediction,
    RoomMpcModelLearner,
)
from heating_controller.controller.mpc.models.capacity import ThermalCapacityModel
from heating_controller.controller.mpc.models.loss import RoomLossModel
from heating_controller.controller.mpc.results import RoomMpcInput
from heating_controller.controller.mpc.types import LearningFactors, RoomThermalConfig


def make_learner():
    thermal_config = RoomThermalConfig(room_heat_load_w=1000)
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    return RoomMpcModelLearner(loss_model, capacity_model), loss_model, capacity_model


def make_input(now_ts, room_temp_c=20.0, outdoor_temp_c=-5.0, flow_temp_c=45.0):
    return RoomMpcInput(
        now_ts=now_ts,
        target_temp_c=21.0,
        room_temp_c=room_temp_c,
        outdoor_temp_c=outdoor_temp_c,
        flow_temp_c=flow_temp_c,
        used_room_sensor_strategy=RoomTemperatureStrategy.ROOM_SENSOR,
        trv_temperatures=[room_temp_c],
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


def test_full_cycle_learns_ua_factor_when_room_cools_more_than_predicted():
    learner, loss_model, _ = make_learner()
    learner.enable()

    start_ts = 1_000_000.0
    for i in range(6):
        learner.append_history(
            make_input(start_ts + i * 300, room_temp_c=17.0), applied_heating_power_w=100.0
        )

    learner.set_prediction(
        LearnerPrediction(
            timestamp=start_ts,
            predicted_room_temperature_c=19.5,
            prediction_horizon_s=1800,
        )
    )
    learner.run_learning_cycle()
    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL

    for i in range(6, 12):
        learner.append_history(
            make_input(start_ts + i * 300, room_temp_c=17.0), applied_heating_power_w=100.0
        )

    initial_ua_factor = loss_model.learned_ua_factor
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor != initial_ua_factor


START_TS = 1_000_000.0


def _evaluate_window(learner, inputs, predicted_room_temperature_c, power_w=100.0):
    """Run the cycle that evaluates a prediction against the given window."""
    learner.set_prediction(
        LearnerPrediction(
            timestamp=START_TS,
            predicted_room_temperature_c=predicted_room_temperature_c,
            prediction_horizon_s=1800,
        )
    )
    learner.run_learning_cycle()

    for mpc_input in inputs:
        learner.append_history(mpc_input, applied_heating_power_w=power_w)

    learner.run_learning_cycle()


def test_cycle_reports_no_correction_when_the_room_matched_the_prediction():
    learner, loss_model, _ = make_learner()
    learner.enable()
    initial_ua_factor = loss_model.learned_ua_factor

    _evaluate_window(
        learner,
        [
            make_input(START_TS + index * 300, room_temp_c=20.05)
            for index in range(6)
        ],
        predicted_room_temperature_c=20.0,
    )

    assert learner.get_learning_state().status == LearningStatus.NO_CORRECTION
    assert loss_model.learned_ua_factor == initial_ua_factor


def test_cycle_reports_disturbed_when_the_outdoor_temperature_drifts():
    learner, loss_model, _ = make_learner()
    learner.enable()
    initial_ua_factor = loss_model.learned_ua_factor

    _evaluate_window(
        learner,
        [
            make_input(
                START_TS + index * 300,
                room_temp_c=18.0,
                outdoor_temp_c=-5.0 - index,
            )
            for index in range(6)
        ],
        predicted_room_temperature_c=20.0,
    )

    assert learner.get_learning_state().status == LearningStatus.DISTURBED
    assert loss_model.learned_ua_factor == initial_ua_factor


def test_cycle_reports_disturbed_when_the_flow_temperature_jumps():
    learner, loss_model, _ = make_learner()
    learner.enable()
    initial_ua_factor = loss_model.learned_ua_factor

    # A domestic hot water charge on the shared leaving-water sensor.
    flow_temperatures = [45.0, 45.0, 55.0, 55.0, 45.0, 45.0]
    _evaluate_window(
        learner,
        [
            make_input(START_TS + index * 300, room_temp_c=18.0, flow_temp_c=flow_temp_c)
            for index, flow_temp_c in enumerate(flow_temperatures)
        ],
        predicted_room_temperature_c=20.0,
    )

    assert learner.get_learning_state().status == LearningStatus.DISTURBED
    assert loss_model.learned_ua_factor == initial_ua_factor


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
    learner, loss_model, _ = make_learner()
    learner.enable()

    start_ts = 2_000_000.0
    for i in range(6):
        learner.append_history(
            make_input(start_ts + i * 300, room_temp_c=19.0), applied_heating_power_w=100.0
        )
    learner.set_prediction(
        LearnerPrediction(
            timestamp=start_ts, predicted_room_temperature_c=19.5, prediction_horizon_s=1800
        )
    )
    learner.run_learning_cycle()
    for i in range(6, 12):
        learner.append_history(
            make_input(start_ts + i * 300, room_temp_c=17.0), applied_heating_power_w=100.0
        )
    learner.run_learning_cycle()

    factors = learner.consume_persisted_learning_factors()
    assert factors is not None
    assert learner.consume_persisted_learning_factors() is None


def _stationary_inputs(room_temp_c=18.0):
    return [
        make_input(START_TS + index * 300, room_temp_c=room_temp_c)
        for index in range(6)
    ]


def _transient_inputs(temperatures=(18.0, 18.1, 18.2, 18.3, 18.4, 18.5)):
    return [
        make_input(START_TS + index * 300, room_temp_c=room_temp_c)
        for index, room_temp_c in enumerate(temperatures)
    ]


def test_stationary_window_learns_ua_even_with_high_heating_power():
    learner, loss_model, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(
        learner, _stationary_inputs(), predicted_room_temperature_c=20.0, power_w=400.0
    )

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor != 1.0
    assert capacity_model.learned_capacity_factor == 1.0


def test_transient_window_learns_capacity():
    learner, loss_model, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(learner, _transient_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor == 1.0
    assert capacity_model.learned_capacity_factor != 1.0


def test_range_counts_both_directions_even_when_the_window_returns():
    learner, loss_model, capacity_model = make_learner()
    learner.enable()

    _evaluate_window(
        learner,
        _transient_inputs((18.0, 17.8, 17.6, 17.8, 18.0, 18.0)),
        predicted_room_temperature_c=20.0,
    )

    assert loss_model.learned_ua_factor == 1.0
    assert capacity_model.learned_capacity_factor != 1.0


def test_configured_stationary_range_widens_the_ua_window():
    learner, loss_model, capacity_model = make_learner()
    learner.enable()
    learner.set_stationary_range_c(0.6)

    _evaluate_window(learner, _transient_inputs(), predicted_room_temperature_c=20.0)

    assert loss_model.learned_ua_factor != 1.0
    assert capacity_model.learned_capacity_factor == 1.0


def test_inactive_learning_window_blocks_ua():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, START_TS - 600)

    _evaluate_window(learner, _stationary_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.OUTSIDE_WINDOW
    assert loss_model.learned_ua_factor == 1.0


def test_inactive_learning_window_still_lets_capacity_learn():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, START_TS - 600)

    _evaluate_window(learner, _transient_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor != 1.0


def test_learning_window_must_cover_the_whole_window():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, START_TS - 600)
    learner.set_learning_window_active(True, START_TS + 300)

    _evaluate_window(learner, _stationary_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.OUTSIDE_WINDOW
    assert loss_model.learned_ua_factor == 1.0


def test_learning_window_active_before_the_window_allows_ua():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_learning_window_active(False, START_TS - 1200)
    learner.set_learning_window_active(True, START_TS - 600)

    _evaluate_window(learner, _stationary_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert loss_model.learned_ua_factor != 1.0


def test_sun_condition_blocks_both_factors():
    learner, loss_model, capacity_model = make_learner()
    learner.enable()
    learner.set_sun_condition(lambda _ts: False)

    _evaluate_window(learner, _stationary_inputs(), predicted_room_temperature_c=20.0)
    assert learner.get_learning_state().status == LearningStatus.OUTSIDE_WINDOW

    _evaluate_window(learner, _transient_inputs(), predicted_room_temperature_c=20.0)
    assert learner.get_learning_state().status == LearningStatus.OUTSIDE_WINDOW

    assert loss_model.learned_ua_factor == 1.0
    assert capacity_model.learned_capacity_factor == 1.0


def test_sun_condition_is_checked_at_both_window_edges():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_sun_condition(lambda ts: ts < START_TS + 900)

    _evaluate_window(learner, _stationary_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.OUTSIDE_WINDOW
    assert loss_model.learned_ua_factor == 1.0


def test_capacity_learns_once_its_shorter_suppression_ended():
    learner, _, capacity_model = make_learner()
    learner.enable()
    learner.suppress_for_interval(3600, 0)

    _evaluate_window(learner, _transient_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.LEARNED
    assert capacity_model.learned_capacity_factor != 1.0


def test_ua_stays_suppressed_while_capacity_is_free():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.suppress_for_interval(3600, 0)

    _evaluate_window(learner, _stationary_inputs(), predicted_room_temperature_c=20.0)

    assert learner.get_learning_state().status == LearningStatus.SUPPRESSED
    assert loss_model.learned_ua_factor == 1.0


def test_recalibrate_discards_the_running_window():
    learner, loss_model, _ = make_learner()
    learner.enable()
    learner.set_prediction(
        LearnerPrediction(
            timestamp=START_TS,
            predicted_room_temperature_c=20.0,
            prediction_horizon_s=1800,
        )
    )
    learner.run_learning_cycle()
    for mpc_input in _stationary_inputs():
        learner.append_history(mpc_input, applied_heating_power_w=100.0)

    learner.recalibrate(LearningFactors(ua_factor=1.2, capacity_factor=1.0))
    learner.run_learning_cycle()

    assert learner.get_learning_state().status == LearningStatus.WAITING_INTERVAL
    assert loss_model.learned_ua_factor == 1.2

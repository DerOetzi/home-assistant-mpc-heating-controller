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
            make_input(start_ts + i * 300, room_temp_c=19.0), applied_heating_power_w=100.0
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


def test_suppress_for_interval_marks_current_window_invalid():
    learner, _, _ = make_learner()
    learner.enable()
    learner.suppress_for_interval(3600)
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

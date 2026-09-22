from heating_controller.controller.mpc.controller import (
    FlowGateConfig,
    MpcRateLimitConfig,
    RoomMpcController,
)
from heating_controller.controller.mpc.results import RoomMpcErrorCode
from heating_controller.controller.mpc.types import (
    FlowGateState,
    RoomThermalConfig,
    TrvConfig,
)


def make_controller(flow_gate_config=None, **rate_limit_overrides):
    return RoomMpcController(
        thermal_config=RoomThermalConfig(room_heat_load_w=1200),
        trvs=[TrvConfig(name="trv1")],
        rate_limit_config=MpcRateLimitConfig(**rate_limit_overrides),
        max_sensor_age_s=1800,
        flow_gate_config=flow_gate_config,
    )


def make_gated_controller(**gate_overrides):
    """Controller whose surplus gate closes on the first cycle that qualifies."""
    overrides = {"hold_time_s": 0.0, **gate_overrides}
    return make_controller(flow_gate_config=FlowGateConfig(**overrides))


def feed_coasting_room(controller):
    """Room coasting well above target on stored heat; outdoor below target."""
    controller.set_room_sensor_temperature(27.5)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)


def test_compute_fails_without_sensor_data():
    controller = make_controller()
    result = controller.compute(target_temperature_c=21.0)
    assert result.valid is False
    assert result.error.code == RoomMpcErrorCode.MISSING_ROOM_TEMPERATURE


def test_compute_returns_higher_demand_for_colder_room():
    warm = make_controller()
    warm.set_room_sensor_temperature(20.0)
    warm.set_outdoor_temperature(5.0)
    warm.set_flow_temperature(45.0)
    warm_result = warm.compute(target_temperature_c=21.0)

    cold = make_controller()
    cold.set_room_sensor_temperature(15.0)
    cold.set_outdoor_temperature(-10.0)
    cold.set_flow_temperature(45.0)
    cold_result = cold.compute(target_temperature_c=21.0)

    assert warm_result.valid and cold_result.valid
    assert cold_result.result.demand_pct >= warm_result.result.demand_pct


def test_demand_rate_limiting_caps_step_change():
    controller = make_controller(max_demand_step_pct=10, demand_hysteresis_pct=0)
    controller.set_room_sensor_temperature(10.0)
    controller.set_outdoor_temperature(-15.0)
    controller.set_flow_temperature(45.0)

    first = controller.compute(target_temperature_c=25.0)
    second = controller.compute(target_temperature_c=25.0)

    assert first.valid and second.valid
    assert abs(second.result.demand_pct - first.result.demand_pct) <= 10


def test_hysteresis_suppresses_small_demand_changes():
    controller = make_controller(demand_hysteresis_pct=50, max_demand_step_pct=100)
    controller.set_room_sensor_temperature(19.0)
    controller.set_outdoor_temperature(-5.0)
    controller.set_flow_temperature(45.0)

    first = controller.compute(target_temperature_c=20.0)
    second = controller.compute(target_temperature_c=20.5)

    assert first.result.demand_pct == second.result.demand_pct


def test_compute_returns_zero_result_instead_of_error_when_no_heating_power_available():
    controller = make_controller()
    controller.set_room_sensor_temperature(26.9)
    controller.set_outdoor_temperature(26.2)
    controller.set_flow_temperature(27.0)

    result = controller.compute(target_temperature_c=8.0)

    assert result.valid is True
    assert result.result.demand_pct == 0
    assert result.result.requested_heating_power_w == 0
    assert result.result.recommended_flow_temperature_c == 0


def test_recommended_flow_floored_at_heat_loss_when_current_flow_too_cold():
    controller = make_controller()
    controller.set_room_sensor_temperature(19.0)
    controller.set_outdoor_temperature(-5.0)
    controller.set_flow_temperature(20.0)

    result = controller.compute(target_temperature_c=22.0)

    assert result.valid
    assert result.result.available_heating_power_w == 0
    assert result.result.recommended_flow_temperature_c > 22.0


def test_recommended_flow_is_the_hold_flow_independent_of_current_room_temp():
    colder = make_controller()
    colder.set_room_sensor_temperature(18.0)
    colder.set_outdoor_temperature(-5.0)
    colder.set_flow_temperature(20.0)
    colder_result = colder.compute(target_temperature_c=22.0)

    warmer = make_controller()
    warmer.set_room_sensor_temperature(21.0)
    warmer.set_outdoor_temperature(-5.0)
    warmer.set_flow_temperature(20.0)
    warmer_result = warmer.compute(target_temperature_c=22.0)

    assert colder_result.valid and warmer_result.valid
    assert (
        colder_result.result.recommended_flow_temperature_c
        == warmer_result.result.recommended_flow_temperature_c
    )


def test_recommended_flow_zero_when_ambient_alone_holds_target():
    controller = make_controller()
    controller.set_room_sensor_temperature(19.0)
    controller.set_outdoor_temperature(24.0)
    controller.set_flow_temperature(20.0)

    result = controller.compute(target_temperature_c=22.0)

    assert result.valid
    assert result.result.recommended_flow_temperature_c == 0


def test_recommended_flow_reported_while_room_coasts_above_target_and_outdoor():
    controller = make_controller()
    feed_coasting_room(controller)

    result = controller.compute(target_temperature_c=23.0)

    assert result.valid
    assert result.result.demand_pct == 0
    assert result.result.flow_gate_closed is False
    assert result.result.recommended_flow_temperature_c > 0


def test_recommended_flow_unchanged_when_room_drops_to_target():
    coasting = make_controller()
    coasting.set_room_sensor_temperature(27.5)
    coasting.set_outdoor_temperature(21.1)
    coasting.set_flow_temperature(26.0)
    coasting_result = coasting.compute(target_temperature_c=23.0)

    settled = make_controller()
    settled.set_room_sensor_temperature(23.0)
    settled.set_outdoor_temperature(21.1)
    settled.set_flow_temperature(26.0)
    settled_result = settled.compute(target_temperature_c=23.0)

    assert coasting_result.valid and settled_result.valid
    assert (
        coasting_result.result.recommended_flow_temperature_c
        == settled_result.result.recommended_flow_temperature_c
    )


def test_recommended_flow_combines_hold_and_requested_branches_independently():
    controller = make_controller()
    controller.set_room_sensor_temperature(5.0)
    controller.set_outdoor_temperature(18.0)
    controller.set_flow_temperature(45.0)

    result = controller.compute(target_temperature_c=22.0)

    assert result.valid
    assert result.result.requested_heating_power_w > 0
    requested_branch_flow_c = (
        controller._emitter_model.calculate_recommended_flow_temperature_c(
            result.result.requested_heating_power_w, result.result.input.room_temp_c
        )
    )
    assert result.result.recommended_flow_temperature_c > requested_branch_flow_c


def test_surplus_gate_zeroes_the_requirement_while_the_room_coasts():
    controller = make_gated_controller()
    feed_coasting_room(controller)

    result = controller.compute(target_temperature_c=23.0)

    assert result.valid
    assert result.result.demand_pct == 0
    assert result.result.flow_gate_closed is True
    assert result.result.recommended_flow_temperature_c == 0
    assert result.result.hold_flow_temperature_c > 0


def test_surplus_gate_stays_open_for_a_room_sitting_at_its_setpoint():
    controller = make_gated_controller()
    controller.set_room_sensor_temperature(23.0)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)

    result = controller.compute(target_temperature_c=23.0)

    assert result.valid
    assert result.result.demand_pct == 0
    assert result.result.flow_gate_closed is False
    assert result.result.recommended_flow_temperature_c > 0


def test_surplus_gate_does_not_close_within_the_deadband():
    controller = make_gated_controller(close_surplus_c=0.5)
    controller.set_room_sensor_temperature(23.4)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)

    result = controller.compute(target_temperature_c=23.0)

    assert result.result.flow_gate_closed is False
    assert result.result.recommended_flow_temperature_c > 0


def test_surplus_gate_reopens_immediately_once_the_surplus_is_used_up():
    controller = make_gated_controller(hold_time_s=0.0, open_surplus_c=0.2)
    feed_coasting_room(controller)
    closed = controller.compute(target_temperature_c=23.0)
    assert closed.result.flow_gate_closed is True

    controller.set_room_sensor_temperature(23.1)
    reopened = controller.compute(target_temperature_c=23.0)

    assert reopened.result.flow_gate_closed is False
    assert reopened.result.recommended_flow_temperature_c > 0


def test_surplus_gate_keeps_hysteresis_between_close_and_open_thresholds():
    controller = make_gated_controller(close_surplus_c=0.5, open_surplus_c=0.2)
    feed_coasting_room(controller)
    assert controller.compute(target_temperature_c=23.0).result.flow_gate_closed

    controller.set_room_sensor_temperature(23.3)
    assert controller.compute(target_temperature_c=23.0).result.flow_gate_closed


def test_surplus_gate_waits_out_the_hold_time_before_closing():
    controller = make_controller(flow_gate_config=FlowGateConfig(hold_time_s=900.0))
    feed_coasting_room(controller)

    assert controller.compute(target_temperature_c=23.0).result.flow_gate_closed is False
    assert controller.compute(target_temperature_c=23.0).result.flow_gate_closed is False


def test_surplus_gate_state_is_separate_per_compute_path():
    controller = make_gated_controller()
    controller.set_room_sensor_temperature(19.0)
    controller.set_outdoor_temperature(-5.0)
    controller.set_flow_temperature(35.0)

    frost = controller.compute(target_temperature_c=8.0)
    normal = controller.compute(target_temperature_c=21.0, apply_side_effects=False)

    assert frost.result.flow_gate_closed is True
    assert frost.result.recommended_flow_temperature_c == 0
    assert normal.result.flow_gate_closed is False
    assert normal.result.recommended_flow_temperature_c > 0


def test_hold_flow_temperature_is_reported_regardless_of_the_gate():
    coasting = make_gated_controller()
    feed_coasting_room(coasting)
    coasting_result = coasting.compute(target_temperature_c=23.0)

    settled = make_gated_controller()
    settled.set_room_sensor_temperature(23.0)
    settled.set_outdoor_temperature(21.1)
    settled.set_flow_temperature(26.0)
    settled_result = settled.compute(target_temperature_c=23.0)

    assert coasting_result.result.flow_gate_closed is True
    assert settled_result.result.flow_gate_closed is False
    assert (
        coasting_result.result.hold_flow_temperature_c
        == settled_result.result.hold_flow_temperature_c
    )


def test_enable_learning_and_run_learning_cycle_does_not_raise():
    controller = make_controller()
    controller.enable_learning()
    controller.set_room_sensor_temperature(19.0)
    controller.set_outdoor_temperature(-5.0)
    controller.set_flow_temperature(45.0)
    controller.compute(target_temperature_c=21.0)

    controller.run_learning_cycle()
    controller.disable_learning()


def test_restored_closed_gate_stays_closed_within_the_hysteresis_band():
    controller = make_controller(
        flow_gate_config=FlowGateConfig(
            close_surplus_c=0.5, open_surplus_c=0.2, hold_time_s=900.0
        )
    )
    controller.restore_flow_gate_state(
        FlowGateState(live_closed=True, preview_closed=True)
    )
    controller.set_room_sensor_temperature(23.3)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)

    live = controller.compute(target_temperature_c=23.0)
    preview = controller.compute(target_temperature_c=23.0, apply_side_effects=False)

    assert live.result.flow_gate_closed is True
    assert preview.result.flow_gate_closed is True
    assert controller.flow_gate_state == FlowGateState(
        live_closed=True, preview_closed=True
    )


def test_restored_closed_gate_reopens_once_the_surplus_is_gone():
    controller = make_gated_controller(open_surplus_c=0.2)
    controller.restore_flow_gate_state(FlowGateState(live_closed=True))
    controller.set_room_sensor_temperature(23.1)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)

    result = controller.compute(target_temperature_c=23.0)

    assert result.result.flow_gate_closed is False
    assert controller.flow_gate_state.live_closed is False

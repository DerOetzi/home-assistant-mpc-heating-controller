from dataclasses import replace

import pytest
from heating_controller.const import (
    Construction,
    DesignTemperatureSystem,
    PanelRadiatorType,
    RoomTemperatureStrategy,
    UaBalanceOutcome,
)
from heating_controller.controller.mpc.controller import (
    FlowGateConfig,
    RoomMpcController,
)
from heating_controller.controller.mpc.observer import ObserverEstimate
from heating_controller.controller.mpc.results import RoomMpcErrorCode, RoomMpcInput
from heating_controller.controller.mpc.types import (
    FlowGateState,
    LearningFactors,
    RoomThermalConfig,
    TrvConfig,
)
from learner_helpers import feed_night


def make_controller(flow_gate_config=None):
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=1200,
            room_area_m2=108,
            construction=Construction.VERY_LIGHT,
        ),
        trvs=[TrvConfig(name="trv1")],
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        flow_gate_config=flow_gate_config,
    )


def make_thermal_controller(thermal_config):
    return RoomMpcController(
        thermal_config=thermal_config,
        trvs=[TrvConfig(name="trv1")],
        max_sensor_age_s=1800,
        room_sensor_configured=False,
    )


def make_gated_controller(**gate_overrides):
    """Controller whose surplus gate closes on the first cycle that qualifies."""
    overrides = {"hold_time_s": 0.0, **gate_overrides}
    return make_controller(flow_gate_config=FlowGateConfig(**overrides))


def _valve_controller(trvs=None, flow_gate_config=None):
    """A 15 m² room whose TRVs report their valve openings."""
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=600, room_area_m2=15, construction=Construction.MEDIUM
        ),
        trvs=trvs
        or [TrvConfig(name="trv1", width_mm=1000, valve_position_configured=True)],
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        flow_gate_config=flow_gate_config,
    )


def feed_coasting_room(controller):
    """Room coasting well above target on stored heat; outdoor below target."""
    controller.set_room_sensor_temperature(27.5)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)


class _Clock:
    def __init__(self, monkeypatch, start_ts=1_000_000.0):
        self.now_ts = start_ts
        monkeypatch.setattr(
            "heating_controller.controller.mpc.sensors.time.time", lambda: self.now_ts
        )

    def advance(self, seconds):
        self.now_ts += seconds


def _feed(controller, room_temp_c, outdoor_temp_c=5.0, flow_temp_c=50.0, opening=None):
    controller.set_room_sensor_temperature(room_temp_c)
    controller.set_outdoor_temperature(outdoor_temp_c)
    controller.set_flow_temperature(flow_temp_c)
    if opening is not None:
        controller.set_valve_position(0, opening)


def test_compute_fails_without_sensor_data():
    controller = make_controller()
    result = controller.compute(target_temperature_c=21.0)
    assert result.valid is False
    assert result.error.code == RoomMpcErrorCode.MISSING_ROOM_TEMPERATURE


def test_measured_power_follows_the_highest_reported_opening():
    """A TRV without a report counts with the room's highest opening."""
    trvs = [
        TrvConfig(name="a", width_mm=1000, valve_position_configured=True),
        TrvConfig(name="b", width_mm=1000, valve_position_configured=True),
    ]
    controller = _valve_controller(trvs)
    _feed(controller, 20.0, flow_temp_c=40.0)
    controller.set_valve_position(0, 60.0)

    result = controller.compute(target_temperature_c=21.0).result
    emitter = controller._emitter_model

    assert result.measured_heating_power_w == pytest.approx(
        emitter.calculate_measured_heating_power_w(20.0, 40.0, [60.0, None], 60.0)
    )


def test_compute_returns_zero_result_instead_of_error_when_no_heating_power_available():
    controller = make_controller()
    controller.set_room_sensor_temperature(26.9)
    controller.set_outdoor_temperature(26.2)
    controller.set_flow_temperature(27.0)

    result = controller.compute(target_temperature_c=8.0)

    assert result.valid is True
    assert result.result.measured_heating_power_w == 0
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


def test_recommended_flow_rises_with_the_room_deficit():
    flows = []
    for room_temp_c in (21.0, 20.9, 20.7, 20.5, 20.0):
        controller = make_controller()
        controller.set_room_sensor_temperature(room_temp_c)
        controller.set_outdoor_temperature(10.0)
        controller.set_flow_temperature(35.0)
        result = controller.compute(target_temperature_c=21.0).result
        flows.append(result.recommended_flow_temperature_c)

    assert flows == sorted(flows)
    assert flows[-1] > flows[0]


def test_recommended_flow_is_the_hold_flow_within_the_recovery_tolerance():
    controller = make_controller()
    controller.set_room_sensor_temperature(20.95)
    controller.set_outdoor_temperature(10.0)
    controller.set_flow_temperature(35.0)

    result = controller.compute(target_temperature_c=21.0).result

    assert result.recovery_flow_temperature_c == 0
    assert result.recommended_flow_temperature_c == result.hold_flow_temperature_c


def _esszimmer_controller():
    controller = RoomMpcController(
        thermal_config=RoomThermalConfig(
            design_indoor_temperature_c=20.0,
            design_outdoor_temperature_c=-13.5,
            room_heat_load_w=853,
            room_area_m2=19,
            construction=Construction.HEAVY,
        ),
        trvs=[
            TrvConfig(
                name="trv1",
                radiator_type=PanelRadiatorType.TYPE_21,
                width_mm=1200,
                height_mm=600,
            )
        ],
        max_sensor_age_s=1800,
        room_sensor_configured=False,
    )
    controller.recalibrate_learning_factors(
        LearningFactors(ua_factor=0.558, capacity_factor=1.0)
    )
    controller.set_outdoor_temperature(8.8)
    controller.set_flow_temperature(35.0)
    return controller


def test_hold_flow_is_where_the_available_power_meets_the_loss():
    """Esszimmer 01.11.2026: 173 W loss at 8.8 °C outdoor. The hold flow
    comes from the same spread as the valve power the factors were learned
    with; a part-load spread asked for 31.5 °C, while the room held only
    20.7 °C at a 34-35 °C flow."""
    controller = _esszimmer_controller()
    controller.set_room_sensor_temperature(21.0)

    result = controller.compute(target_temperature_c=21.0).result
    emitter = controller._emitter_model
    loss_w = controller._loss_model.calculate_heat_loss_w(21.0, 8.8)

    assert result.hold_flow_temperature_c == 35.5
    assert emitter.calculate_available_heating_power_w(21.0, 35.5) >= loss_w
    assert emitter.calculate_available_heating_power_w(21.0, 35.0) < loss_w


def test_recovery_flow_needs_more_than_the_hold_flow():
    """Recovery simulates with the same spread: bringing the room back takes
    more than holding it, never less."""
    controller = _esszimmer_controller()
    controller.set_room_sensor_temperature(20.5)

    result = controller.compute(target_temperature_c=21.0).result

    assert result.recovery_flow_temperature_c > result.hold_flow_temperature_c
    assert result.recommended_flow_temperature_c == result.recovery_flow_temperature_c


def test_recommended_flow_is_independent_of_the_measured_flow_temperature():
    # 2026-09-22: a DHW charge on the shared leaving-water sensor (58 degC) and
    # a stale reading (None) drove the requirement to the design flow.
    results = []
    for flow_temp_c in (29.0, 58.0, None):
        controller = make_controller()
        controller.set_room_sensor_temperature(20.5)
        controller.set_outdoor_temperature(10.4)
        if flow_temp_c is not None:
            controller.set_flow_temperature(flow_temp_c)
        results.append(controller.compute(target_temperature_c=21.0).result)

    recommended = {r.recommended_flow_temperature_c for r in results}
    assert len(recommended) == 1
    assert results[0].recovery_flow_temperature_c > results[0].hold_flow_temperature_c
    assert results[0].recovery_flow_saturated is False


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


def test_recommended_flow_saturates_at_the_design_flow_temperature():
    controller = make_thermal_controller(
        RoomThermalConfig(
            room_heat_load_w=1200,
            design_temperature_system=DesignTemperatureSystem.SYSTEM_45_35,
            room_area_m2=108,
            construction=Construction.VERY_LIGHT,
        )
    )
    controller.set_room_sensor_temperature(15.0)
    controller.set_outdoor_temperature(-5.0)
    controller.set_flow_temperature(35.0)

    result = controller.compute(target_temperature_c=22.0).result

    assert result.recovery_flow_saturated is True
    assert result.recovery_flow_temperature_c == 45.0
    assert result.recommended_flow_temperature_c == 45.0


def test_flow_search_starts_below_the_flow_threshold():
    def hold_flow(flow_threshold_c):
        controller = make_thermal_controller(
            RoomThermalConfig(
                room_heat_load_w=1200,
                flow_threshold_c=flow_threshold_c,
                room_area_m2=108,
                construction=Construction.VERY_LIGHT,
            )
        )
        controller.set_room_sensor_temperature(21.0)
        controller.set_outdoor_temperature(20.0)
        controller.set_flow_temperature(35.0)
        result = controller.compute(target_temperature_c=21.0).result
        return result.hold_flow_temperature_c

    assert hold_flow(30.0) < 30.0
    assert hold_flow(40.0) >= 30.0


def test_no_recovery_flow_when_outdoor_is_above_target():
    controller = make_controller()
    controller.set_room_sensor_temperature(18.0)
    controller.set_outdoor_temperature(25.0)
    controller.set_flow_temperature(35.0)

    result = controller.compute(target_temperature_c=21.0).result

    assert result.recovery_flow_temperature_c == 0
    assert result.recommended_flow_temperature_c == 0


def test_surplus_gate_zeroes_the_requirement_while_the_room_coasts():
    controller = make_gated_controller()
    feed_coasting_room(controller)

    result = controller.compute(target_temperature_c=23.0)

    assert result.valid
    assert result.result.flow_gate_closed is True
    assert result.result.recommended_flow_temperature_c == 0
    assert result.result.hold_flow_temperature_c > 0


def test_surplus_gate_stays_open_while_a_trv_reports_an_open_valve():
    controller = _valve_controller(flow_gate_config=FlowGateConfig(hold_time_s=0.0))
    feed_coasting_room(controller)
    controller.set_valve_position(0, 5.0)

    result = controller.compute(target_temperature_c=23.0)

    assert result.result.flow_gate_closed is False

    controller.set_valve_position(0, 0.0)
    assert controller.compute(target_temperature_c=23.0).result.flow_gate_closed


def test_surplus_gate_stays_open_for_a_room_sitting_at_its_setpoint():
    controller = make_gated_controller()
    controller.set_room_sensor_temperature(23.0)
    controller.set_outdoor_temperature(21.1)
    controller.set_flow_temperature(26.0)

    result = controller.compute(target_temperature_c=23.0)

    assert result.valid
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

    assert (
        controller.compute(target_temperature_c=23.0).result.flow_gate_closed is False
    )
    assert (
        controller.compute(target_temperature_c=23.0).result.flow_gate_closed is False
    )


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


def test_gated_hold_flow_is_zero_while_the_room_coasts():
    controller = make_gated_controller()
    feed_coasting_room(controller)

    result = controller.compute(target_temperature_c=23.0).result

    assert result.flow_gate_closed is True
    assert result.hold_flow_temperature_c > 0
    assert result.gated_hold_flow_temperature_c == 0


def test_gated_hold_flow_ignores_the_recovery_part():
    controller = make_controller()
    controller.set_room_sensor_temperature(19.0)
    controller.set_outdoor_temperature(10.0)
    controller.set_flow_temperature(35.0)

    result = controller.compute(target_temperature_c=21.0).result

    assert result.flow_gate_closed is False
    assert result.gated_hold_flow_temperature_c == result.hold_flow_temperature_c
    assert result.recommended_flow_temperature_c > result.gated_hold_flow_temperature_c


def test_the_observer_follows_the_reported_valve(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    _feed(controller, 21.2, opening=90.0)
    first = controller.compute(target_temperature_c=21.0)
    clock.advance(1800)
    _feed(controller, 21.2, opening=90.0)
    later = controller.compute(target_temperature_c=21.0)

    # The TRV holds its valve 90 % open: the radiator gives off what the
    # valve lets through.
    assert first.result.measured_heating_power_w > 0
    assert later.result.radiator_power_w > 0.5 * first.result.measured_heating_power_w


def test_radiators_stay_cold_while_heat_is_unavailable(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    controller.set_heat_available(False)
    _feed(controller, 15.0, opening=100.0)
    controller.compute(target_temperature_c=21.0)
    clock.advance(600)
    _feed(controller, 15.0, opening=100.0)
    result = controller.compute(target_temperature_c=21.0)

    assert result.result.measured_heating_power_w > 0
    assert result.result.radiator_power_w == 0


def test_preview_compute_leaves_the_observer_alone(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    _feed(controller, 15.0, opening=100.0)
    controller.compute(target_temperature_c=21.0, apply_side_effects=False)
    clock.advance(600)
    _feed(controller, 15.0, opening=100.0)
    result = controller.compute(target_temperature_c=21.0)

    # Nothing was committed by the preview: no heat in the radiators yet.
    assert result.result.radiator_power_w == 0


def test_balance_does_not_move_anything_but_the_factor():
    controller = make_controller()
    controller.enable_learning()
    # Outdoor at room temperature and the radiators cold: the room stays put
    # and loses nothing whatever the factor, so only a hand-over of heat
    # could move the observer's estimate.
    state_input = RoomMpcInput(
        now_ts=5_000_000.0,
        target_temp_c=20.0,
        room_temp_c=20.0,
        outdoor_temp_c=20.0,
        flow_temp_c=40.0,
        used_room_sensor_strategy=RoomTemperatureStrategy.ROOM_SENSOR,
        trv_temperatures=[20.0],
    )
    controller._observer.commit(
        ObserverEstimate(
            timestamp=state_input.now_ts,
            room_temperature_c=20.0,
            radiator_power_w=0.0,
        ),
        state_input,
        target_power_w=0.0,
        demand_pct=0.0,
    )
    probe = replace(state_input, now_ts=state_input.now_ts + 600)
    before = controller._observer.estimate(probe)
    capacity_factor = controller.learned_capacity_factor

    feed_night(controller._learner)
    controller.run_learning_cycle()

    assert controller.get_ua_balance_info().outcome in (
        UaBalanceOutcome.LEARNED,
        UaBalanceOutcome.LEARNED_AT_LIMIT,
    )
    assert controller.learned_ua_factor != 1.0
    assert controller.learned_capacity_factor == capacity_factor
    after = controller._observer.estimate(probe)
    assert after.room_temperature_c == before.room_temperature_c
    assert after.radiator_power_w == before.radiator_power_w


def _charge(controller, clock, flows_c, room_temp_c=20.5):
    result = None
    for flow_c in flows_c:
        clock.advance(300)
        _feed(controller, room_temp_c, flow_temp_c=flow_c, opening=60.0)
        result = controller.compute(target_temperature_c=21.0)
    return result


def test_a_hot_water_charge_keeps_the_heating_flow(monkeypatch):
    # 31 October, 05:05: the flow jumped from 35 to 56 °C while the
    # radiators got no water.
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    charged = _charge(controller, clock, [35.0, 35.0, 35.0, 56.0])

    assert charged.result.input.hot_water_charge is True
    assert charged.result.input.flow_temp_c == 35.0


def test_radiators_get_no_heat_during_a_hot_water_charge(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    before = _charge(controller, clock, [35.0, 35.0, 35.0], room_temp_c=19.0)
    during = _charge(controller, clock, [56.0, 56.0, 56.0], room_temp_c=19.0)

    clock = _Clock(monkeypatch)
    heating = _valve_controller()
    steady = _charge(heating, clock, [35.0] * 6, room_temp_c=19.0)

    # The radiators cool down with their own lag.
    assert before.result.radiator_power_w > 0
    assert during.result.radiator_power_w < before.result.radiator_power_w
    assert during.result.radiator_power_w < 0.75 * steady.result.radiator_power_w


def test_missing_outdoor_temperature_pauses_learning():
    controller = make_controller()
    controller.enable_learning()
    controller.set_room_sensor_temperature(20.0)
    controller.set_flow_temperature(40.0)

    result = controller.compute(target_temperature_c=21.0)
    controller.run_learning_cycle()

    assert result.valid is False
    assert result.error.code == RoomMpcErrorCode.MISSING_OUTDOOR_TEMPERATURE
    assert controller.outdoor_temperature_missing is True
    assert controller.get_ua_balance_info().outcome == (
        UaBalanceOutcome.OUTDOOR_TEMPERATURE_MISSING
    )


def test_learning_resumes_once_the_outdoor_temperature_is_back():
    controller = make_controller()
    controller.enable_learning()
    controller.set_room_sensor_temperature(20.0)
    controller.set_flow_temperature(40.0)
    controller.compute(target_temperature_c=21.0)

    controller.set_outdoor_temperature(5.0)
    result = controller.compute(target_temperature_c=21.0)
    controller.run_learning_cycle()

    assert result.valid is True
    assert controller.outdoor_temperature_missing is False
    assert controller.get_ua_balance_info().outcome != (
        UaBalanceOutcome.OUTDOOR_TEMPERATURE_MISSING
    )


def test_a_preview_compute_leaves_the_outdoor_state_alone():
    controller = make_controller()
    controller.set_room_sensor_temperature(20.0)

    controller.compute(target_temperature_c=21.0, apply_side_effects=False)

    assert controller.outdoor_temperature_missing is False


def test_hold_flow_at_other_outdoor_values_follows_the_same_calculation():
    """The curve end points ask the same question at other outdoor values."""
    controller = _esszimmer_controller()
    controller.set_room_sensor_temperature(21.0)
    result = controller.compute(target_temperature_c=21.0).result

    assert (
        controller.calculate_hold_flow_temperature_c(21.0, 8.8)
        == result.hold_flow_temperature_c
    )
    cold_c = controller.calculate_hold_flow_temperature_c(21.0, -13.0)
    warm_c = controller.calculate_hold_flow_temperature_c(21.0, 18.0)
    assert warm_c < result.hold_flow_temperature_c < cold_c
    assert controller.calculate_hold_flow_temperature_c(21.0, 22.0) == 0.0


def _cold_radiator_hours(controller, clock, hours=2.5, mode_target_c=21.0):
    for _ in range(int(hours * 12) + 1):
        _feed(controller, 19.0, opening=100.0)
        controller.compute(
            target_temperature_c=mode_target_c,
            mode_target_temperature_c=mode_target_c,
        )
        clock.advance(300)


def test_a_cold_radiator_is_reported_after_two_hours(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    _cold_radiator_hours(controller, clock)
    assert controller.radiators_not_heating == [True]


def test_an_open_window_keeps_the_radiator_check_from_judging(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    controller.set_window_open(True, clock.now_ts)
    _cold_radiator_hours(controller, clock)
    assert controller.radiators_not_heating == [False]


def test_a_room_near_its_mode_setpoint_is_not_judged(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    _cold_radiator_hours(controller, clock, mode_target_c=19.3)
    assert controller.radiators_not_heating == [False]

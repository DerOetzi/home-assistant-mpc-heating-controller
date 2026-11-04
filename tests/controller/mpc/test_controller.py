from dataclasses import replace

import pytest
from heating_controller.const import (
    CapacityOutcome,
    Construction,
    DesignTemperatureSystem,
    PanelRadiatorType,
    RoomTemperatureStrategy,
    TrvSetpointMode,
    UaBalanceOutcome,
)
from heating_controller.controller.mpc.controller import (
    MPC_DEMAND_SEARCH_STEP_PCT,
    FlowGateConfig,
    MpcRateLimitConfig,
    RoomMpcController,
    _DemandPrediction,
)
from heating_controller.controller.mpc.observer import ObserverEstimate
from heating_controller.controller.mpc.results import RoomMpcErrorCode, RoomMpcInput
from heating_controller.controller.mpc.types import (
    FlowGateState,
    LearningFactors,
    RoomThermalConfig,
    TrvConfig,
)
from heating_controller.controller.mpc.valve_curve import ValveCurveStatus
from learner_helpers import feed_night
from test_valve_curve import DANFOSS_SEED, _Trv


def make_controller(flow_gate_config=None, **rate_limit_overrides):
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=1200,
            room_area_m2=108,
            construction=Construction.VERY_LIGHT,
        ),
        trvs=[TrvConfig(name="trv1")],
        rate_limit_config=MpcRateLimitConfig(**rate_limit_overrides),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=0.0,
        flow_gate_config=flow_gate_config,
    )


def make_thermal_controller(thermal_config):
    return RoomMpcController(
        thermal_config=thermal_config,
        trvs=[TrvConfig(name="trv1")],
        rate_limit_config=MpcRateLimitConfig(),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=0.0,
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


def test_demand_rate_limiting_caps_step_change(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = make_controller(max_demand_step_pct=10, demand_hysteresis_pct=0)
    controller.set_room_sensor_temperature(10.0)
    controller.set_outdoor_temperature(-15.0)
    controller.set_flow_temperature(45.0)

    first = controller.compute(target_temperature_c=25.0)
    clock.advance(300)
    second = controller.compute(target_temperature_c=25.0)

    assert first.valid and second.valid
    assert abs(second.result.demand_pct - first.result.demand_pct) <= 10


def _heating_room_controller(clock):
    # Default smoothing: 20 % per 5 minutes, 300 s hold, 40 % hold override.
    controller = make_controller()
    controller.set_room_sensor_temperature(18.0)
    controller.set_outdoor_temperature(-5.0)
    controller.set_flow_temperature(45.0)
    for _ in range(5):
        controller.compute(target_temperature_c=22.0)
        clock.advance(300)
    return controller


def test_smoothing_steps_the_demand_down_slowly(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _heating_room_controller(clock)
    heating_pct = controller.compute(target_temperature_c=22.0).result.demand_pct
    assert heating_pct > 50

    clock.advance(300)
    lowered = controller.compute(target_temperature_c=5.0).result

    assert lowered.demand_pct > 0


def test_skipped_smoothing_applies_the_demand_at_once(monkeypatch):
    controller = _heating_room_controller(_Clock(monkeypatch))

    frost = controller.compute(
        target_temperature_c=5.0, skip_demand_smoothing=True
    ).result

    assert frost.demand_pct == 0
    assert frost.trv_targets == [TrvConfig(name="trv1").min_target_temperature_c]


def test_skipped_smoothing_still_heats_a_room_below_frost_protection():
    controller = make_controller()
    controller.set_room_sensor_temperature(3.0)
    controller.set_outdoor_temperature(-15.0)
    controller.set_flow_temperature(45.0)

    frost = controller.compute(
        target_temperature_c=8.0, skip_demand_smoothing=True
    ).result

    assert frost.demand_pct > 20


def test_smoothing_resumes_from_the_forced_demand(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _heating_room_controller(clock)
    controller.compute(target_temperature_c=5.0, skip_demand_smoothing=True)
    clock.advance(300)

    resumed = controller.compute(target_temperature_c=22.0).result

    # Hold time and step limit start again from 0 %, not from the old demand.
    assert resumed.demand_pct <= 20


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
        rate_limit_config=MpcRateLimitConfig(),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=0.1,
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


class _Clock:
    def __init__(self, monkeypatch, start_ts=1_000_000.0):
        self.now_ts = start_ts
        monkeypatch.setattr(
            "heating_controller.controller.mpc.sensors.time.time", lambda: self.now_ts
        )

    def advance(self, seconds):
        self.now_ts += seconds


def _feed(controller, room_temp_c, outdoor_temp_c=5.0, flow_temp_c=50.0):
    controller.set_room_sensor_temperature(room_temp_c)
    controller.set_outdoor_temperature(outdoor_temp_c)
    controller.set_flow_temperature(flow_temp_c)


def _unsmoothed_controller():
    """A 15 m² room one radiator can heat, demand applied as computed."""
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=600, room_area_m2=15, construction=Construction.MEDIUM
        ),
        trvs=[TrvConfig(name="trv1")],
        rate_limit_config=MpcRateLimitConfig(
            demand_hysteresis_pct=0, hold_time_s=0, max_demand_step_pct=100
        ),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=0.0,
    )


def test_heat_still_in_the_radiator_lowers_the_demand(monkeypatch):
    clock = _Clock(monkeypatch)
    heated = _unsmoothed_controller()
    _feed(heated, 18.0)
    first = heated.compute(target_temperature_c=21.0)
    clock.advance(600)
    _feed(heated, 20.8)
    after_heating = heated.compute(target_temperature_c=21.0)

    fresh = _unsmoothed_controller()
    _feed(fresh, 20.8)
    cold_radiator = fresh.compute(target_temperature_c=21.0)

    assert first.result.demand_pct == 100
    assert after_heating.result.radiator_power_w > 0
    assert cold_radiator.result.radiator_power_w == 0
    assert after_heating.result.demand_pct < cold_radiator.result.demand_pct


def test_radiators_stay_cold_while_heat_is_unavailable(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _unsmoothed_controller()
    controller.set_heat_available(False)
    _feed(controller, 15.0)
    controller.compute(target_temperature_c=21.0)
    clock.advance(600)
    _feed(controller, 15.0)
    result = controller.compute(target_temperature_c=21.0)

    assert result.result.demand_pct > 0
    assert result.result.radiator_power_w == 0


def test_preview_compute_leaves_the_observer_alone(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _unsmoothed_controller()
    _feed(controller, 15.0)
    controller.compute(target_temperature_c=21.0, apply_side_effects=False)
    clock.advance(600)
    _feed(controller, 15.0)
    result = controller.compute(target_temperature_c=21.0)

    # Nothing was committed by the preview: no heat in the radiators yet.
    assert result.result.radiator_power_w == 0


def _heavy_room_controller(target_band_c):
    """A heavy 19 m² room whose radiator has little reserve at a low flow."""
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=340, room_area_m2=19, construction=Construction.HEAVY
        ),
        trvs=[TrvConfig(name="trv1")],
        rate_limit_config=MpcRateLimitConfig(
            demand_hysteresis_pct=0, hold_time_s=0, max_demand_step_pct=100
        ),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=target_band_c,
    )


def _heavy_room_demand(monkeypatch, target_band_c, room_temp_c):
    _Clock(monkeypatch)
    controller = _heavy_room_controller(target_band_c)
    _feed(controller, room_temp_c, outdoor_temp_c=9.0, flow_temp_c=38.0)
    return controller.compute(target_temperature_c=21.0).result.demand_pct


def test_one_sensor_step_below_setpoint_opens_the_valve_fully_without_band(
    monkeypatch,
):
    assert _heavy_room_demand(monkeypatch, 0.0, 20.9) == 100


def test_band_holds_one_sensor_step_below_setpoint(monkeypatch):
    without_band = _heavy_room_demand(monkeypatch, 0.0, 21.0)
    in_band = _heavy_room_demand(monkeypatch, 0.1, 20.9)

    # Near the hold demand instead of fully open.
    assert 0 < in_band < 100
    assert abs(in_band - without_band) <= 20


def test_band_does_not_shut_the_valve_at_the_setpoint(monkeypatch):
    assert _heavy_room_demand(monkeypatch, 0.1, 21.0) > 0


def test_band_still_heats_fully_below_it(monkeypatch):
    assert _heavy_room_demand(monkeypatch, 0.1, 20.5) == 100


def test_band_still_closes_above_the_setpoint(monkeypatch):
    assert _heavy_room_demand(monkeypatch, 0.1, 21.3) == 0


def _valve_controller(
    seed=None,
    *,
    room_heat_load_w=600,
    room_area_m2=15,
    width_mm=1000,
    target_band_c=0.0,
    trv_setpoint_mode=TrvSetpointMode.ABSOLUTE,
):
    """One TRV reporting its valve opening, demand applied as computed."""
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=room_heat_load_w,
            room_area_m2=room_area_m2,
            construction=Construction.MEDIUM,
        ),
        trvs=[
            TrvConfig(
                name="trv1",
                width_mm=width_mm,
                valve_position_configured=True,
                valve_characteristic_seed=seed,
            )
        ],
        rate_limit_config=MpcRateLimitConfig(
            demand_hysteresis_pct=0, hold_time_s=0, max_demand_step_pct=100
        ),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=target_band_c,
        trv_setpoint_mode=trv_setpoint_mode,
    )


# Hauptbad-like: 8 m², a big radiator; at a 35 °C flow, 22 °C and 8 °C outside
# the open valve gives 324 W against a 66 W loss.
HAUPTBAD = {
    "room_heat_load_w": 150,
    "room_area_m2": 8,
    "width_mm": 2000,
    "target_band_c": 0.1,
}


def test_the_observer_follows_the_reported_valve_not_the_demand(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    _feed(controller, 21.2)
    controller.set_valve_position(0, 90.0)
    first = controller.compute(target_temperature_c=21.0)
    clock.advance(1800)
    _feed(controller, 21.2)
    controller.set_valve_position(0, 90.0)
    later = controller.compute(target_temperature_c=21.0)

    # A small demand, yet the TRV holds its valve 90 % open: the radiator
    # gives off what the valve lets through.
    assert first.result.demand_pct <= 10
    assert (
        first.result.measured_heating_power_w
        > 5 * first.result.requested_heating_power_w
    )
    assert later.result.radiator_power_w > 0.5 * first.result.measured_heating_power_w


def _hold_at_setpoint(controller, clock):
    """Room at 21 °C, radiator already giving the loss at its hold opening.

    Returns the loss. With the radiator warm the search has no lag to make
    up and settles on the opening that holds the setpoint.
    """
    controller.recalibrate_learning_factors(
        LearningFactors(ua_factor=0.7, capacity_factor=1.0)
    )
    loss_w = controller._loss_model.calculate_heat_loss_w(21.0, 5.0)
    state_input = RoomMpcInput(
        now_ts=clock.now_ts - 60,
        target_temp_c=21.0,
        room_temp_c=21.0,
        outdoor_temp_c=5.0,
        flow_temp_c=40.0,
        used_room_sensor_strategy=RoomTemperatureStrategy.ROOM_SENSOR,
        trv_temperatures=[21.0],
    )
    controller._observer.commit(
        ObserverEstimate(
            timestamp=state_input.now_ts,
            room_temperature_c=21.0,
            radiator_power_w=loss_w,
        ),
        state_input,
        target_power_w=loss_w,
        demand_pct=controller._hold_demand_pct(state_input),
    )
    _feed(controller, 21.0, outdoor_temp_c=5.0, flow_temp_c=40.0)
    controller.set_trv_temperature(0, 20.3)
    return loss_w


def test_hold_demand_is_the_opening_that_gives_the_loss(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    loss_w = _hold_at_setpoint(controller, clock)
    emitter = controller._emitter_model

    result = controller.compute(target_temperature_c=21.0).result

    assert 0 < loss_w < emitter.calculate_available_heating_power_w(21.0, 40.0)
    # The opening, not the share of available power: little water already
    # gives most of the heat.
    hold_pct = controller._hold_demand_pct(result.input)
    assert emitter.planned_heating_power_w(21.0, 40.0, hold_pct) == pytest.approx(
        loss_w, rel=0.01
    )
    assert hold_pct < 100 * loss_w / emitter.calculate_available_heating_power_w(
        21.0, 40.0
    )
    # The search runs on a 5 % grid: the point next to the hold opening.
    assert abs(result.demand_pct - hold_pct) <= MPC_DEMAND_SEARCH_STEP_PCT
    assert emitter.planned_heating_power_w(
        21.0, 40.0, result.demand_pct
    ) == pytest.approx(loss_w, rel=0.05)


def test_planner_prefers_a_small_opening_when_a_small_one_gives_most_heat(
    monkeypatch,
):
    """Hauptbad-like: 8 m², a big radiator, a low flow, 0.2 K below setpoint.

    At a 35 °C flow and 22 °C the open valve gives 324 W, the loss at 8 °C
    outside is 66 W. On the valve curve 3.5 % opening already holds that,
    25 % gives 230 W (71 %). Planned as available x demand, the same search
    picked 60 % (194 W planned, about 290 W real) and the room overshot.
    """
    _Clock(monkeypatch)
    controller = _valve_controller(**HAUPTBAD)
    _feed(controller, 21.8, outdoor_temp_c=8.0, flow_temp_c=35.0)

    result = controller.compute(target_temperature_c=22.0).result

    assert 0 < result.demand_pct <= 30


def test_requested_power_is_the_planned_power(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_controller()
    _hold_at_setpoint(controller, clock)

    result = controller.compute(target_temperature_c=21.0).result

    assert 0 < result.demand_pct < 100
    assert result.requested_heating_power_w == pytest.approx(
        controller._emitter_model.planned_heating_power_w(
            21.0, 40.0, result.demand_pct
        ),
        abs=0.01,
    )


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
        _feed(controller, room_temp_c, flow_temp_c=flow_c)
        result = controller.compute(target_temperature_c=21.0)
    return result


def test_a_hot_water_charge_keeps_the_demand_of_the_heating_flow(monkeypatch):
    # 31 October, 05:05: the flow jumped from 35 to 56 °C while the
    # radiators got no water; the valves closed on the hot flow reading.
    clock = _Clock(monkeypatch)
    heating = _unsmoothed_controller()
    steady = _charge(heating, clock, [35.0, 35.0, 35.0, 35.0])

    clock = _Clock(monkeypatch)
    charging = _unsmoothed_controller()
    charged = _charge(charging, clock, [35.0, 35.0, 35.0, 56.0])

    assert charged.result.input.hot_water_charge is True
    assert charged.result.input.flow_temp_c == 35.0
    assert charged.result.demand_pct == steady.result.demand_pct


def test_radiators_get_no_heat_during_a_hot_water_charge(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _unsmoothed_controller()
    before = _charge(controller, clock, [35.0, 35.0, 35.0], room_temp_c=19.0)
    during = _charge(controller, clock, [56.0, 56.0, 56.0], room_temp_c=19.0)

    clock = _Clock(monkeypatch)
    heating = _unsmoothed_controller()
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
    assert controller.get_capacity_outcome() == (
        CapacityOutcome.OUTDOOR_TEMPERATURE_MISSING
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


def _cold_room_controller(**rate_limit_overrides):
    controller = make_controller(
        demand_hysteresis_pct=0,
        hold_time_s=0,
        max_demand_step_pct=20,
        **rate_limit_overrides,
    )
    controller.set_room_sensor_temperature(10.0)
    controller.set_outdoor_temperature(-15.0)
    controller.set_flow_temperature(45.0)
    return controller


def test_demand_step_scales_with_the_time_since_the_last_call(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _cold_room_controller()

    first = controller.compute(target_temperature_c=25.0)
    clock.advance(6)
    second = controller.compute(target_temperature_c=25.0)

    # The cold room asks for 100 %: the first call steps to 20 %, the second
    # 6 s later by 20 % * 6 / 300 more.
    assert first.result.demand_pct == 20
    assert second.result.demand_pct == pytest.approx(20 + 20 * 6 / 300)


def test_first_call_allows_the_full_step(monkeypatch):
    _Clock(monkeypatch)
    controller = _cold_room_controller()

    first = controller.compute(target_temperature_c=25.0)

    assert first.result.demand_pct == 20


def test_a_full_interval_allows_the_full_step(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _cold_room_controller()

    first = controller.compute(target_temperature_c=25.0)
    clock.advance(300)
    second = controller.compute(target_temperature_c=25.0)

    assert second.result.demand_pct - first.result.demand_pct == 20


def _valve_room(trvs=None):
    """A room whose TRVs report their valve opening (two by default)."""
    return RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=600, room_area_m2=15, construction=Construction.MEDIUM
        ),
        trvs=trvs
        or [
            TrvConfig(name="trv_a", valve_position_configured=True),
            TrvConfig(name="trv_b", valve_position_configured=True),
        ],
        rate_limit_config=MpcRateLimitConfig(),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=0.0,
    )


def _feed_trvs(controller, targets_c, openings_pct, report_targets=True):
    _feed(controller, 20.0)
    for index, (target_c, opening_pct) in enumerate(
        zip(targets_c, openings_pct, strict=True)
    ):
        controller.set_trv_temperature(index, 20.0)
        controller.set_valve_position(index, opening_pct)
        if report_targets:
            controller.set_trv_target(index, target_c)


def _valve_curve_cycles(
    controller, clock, offsets_c, openings_pct, times=3, report_targets=True, **compute
):
    """Per cycle: another setpoint, then offsets_c, read 700 s later.

    Returns every compute result, so two runs can be compared step by step.
    """
    results = []
    count = len(offsets_c)
    for _ in range(times):
        for targets_c, reported_pct, wait_s in (
            ([5.0] * count, [0.0] * count, 300),
            ([20.0 + offset for offset in offsets_c], [0.0] * count, 700),
            ([20.0 + offset for offset in offsets_c], openings_pct, 300),
        ):
            _feed_trvs(controller, targets_c, reported_pct, report_targets)
            results.append(controller.compute(target_temperature_c=21.0, **compute))
            clock.advance(wait_s)
    return results


def _all_bins(controller):
    return {
        name: learner.bins()
        for name, learner in controller.valve_curve_learners.items()
    }


def test_trv_without_valve_entity_has_no_learner():
    controller = make_controller()

    assert controller.valve_curve_learners == {}
    assert controller.valve_characteristics() == [None]


def test_two_trvs_learn_separately(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_room()

    _valve_curve_cycles(controller, clock, [0.7, 1.2], [30.0, 60.0])

    bins = _all_bins(controller)
    [first] = bins["trv_a"]
    [second] = bins["trv_b"]
    assert (first.offset_c, first.opening_pct, first.samples) == (
        pytest.approx(0.7),
        30.0,
        3,
    )
    assert (second.offset_c, second.opening_pct, second.samples) == (
        pytest.approx(1.2),
        60.0,
        3,
    )
    assert controller.consume_valve_curves_changed() is True
    assert controller.consume_valve_curves_changed() is False


def test_preview_compute_does_not_feed_the_learner(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_room()

    _valve_curve_cycles(
        controller, clock, [0.7, 1.2], [30.0, 60.0], apply_side_effects=False
    )

    assert _all_bins(controller) == {"trv_a": [], "trv_b": []}


@pytest.mark.parametrize(
    ("block", "compute"),
    [
        pytest.param(
            lambda controller, now_ts: controller.set_window_open(True, now_ts),
            {},
            id="window",
        ),
        pytest.param(
            lambda controller, now_ts: controller.set_trv_adapting(True),
            {},
            id="adapting",
        ),
        pytest.param(
            lambda controller, now_ts: None,
            {"skip_demand_smoothing": True},
            id="frost",
        ),
    ],
)
def test_a_blocked_moment_feeds_no_sample(monkeypatch, block, compute):
    clock = _Clock(monkeypatch)
    controller = _valve_room()
    block(controller, clock.now_ts)

    _valve_curve_cycles(controller, clock, [0.7, 1.2], [30.0, 60.0], **compute)

    assert _all_bins(controller) == {"trv_a": [], "trv_b": []}


def test_a_missing_valve_position_feeds_no_sample_for_that_trv(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_room()

    _valve_curve_cycles(controller, clock, [0.7, 1.2], [None, 60.0])

    bins = _all_bins(controller)
    assert bins["trv_a"] == []
    assert bins["trv_b"] != []


def test_learning_does_not_change_the_demand(monkeypatch):
    clock = _Clock(monkeypatch)
    learning = _valve_room()
    blind = _valve_room()

    start_ts = clock.now_ts
    learned = _valve_curve_cycles(learning, clock, [0.7, 2.2], [30.0, 85.0])
    clock.now_ts = start_ts
    unlearned = _valve_curve_cycles(
        blind, clock, [0.7, 2.2], [30.0, 85.0], report_targets=False
    )

    # Learning, not yet learned: absolute mode would ignore a curve anyway.
    assert _all_bins(learning)["trv_b"] != []
    assert _all_bins(blind) == {"trv_a": [], "trv_b": []}
    assert [r.result.demand_pct for r in learned] == [
        r.result.demand_pct for r in unlearned
    ]
    assert [r.result.trv_targets for r in learned] == [
        r.result.trv_targets for r in unlearned
    ]


def test_valve_curves_survive_export_and_restore(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_room()
    _valve_curve_cycles(controller, clock, [0.7, 1.2], [30.0, 60.0])

    restored = _valve_room()
    restored.restore_valve_curves(controller.export_valve_curves())

    assert _all_bins(restored) == _all_bins(controller)


def _learn_curve(controller, name="trv1"):
    """Three bins up to 2.2 K: the TRV's curve counts as learned."""
    trv = _Trv(controller.valve_curve_learners[name])
    for offset_c, opening_pct in ((0.7, 30), (1.2, 50), (2.2, 85)):
        trv.sample(offset_c, opening_pct, times=3)
    assert controller.valve_curve_learners[name].status is ValveCurveStatus.LEARNED


def test_a_seed_reaches_the_learner():
    controller = _valve_room(
        [
            TrvConfig(
                name="trv1",
                valve_position_configured=True,
                valve_characteristic_seed=DANFOSS_SEED,
            )
        ]
    )

    # The curve until learned; then it fills in beyond the learned span
    # (0.1 K below, 2.5 and 3.0 K above).
    assert controller.valve_characteristics() == [DANFOSS_SEED]
    _learn_curve(controller)
    [curve] = controller.valve_characteristics()
    assert curve.points[0] == (0.1, 5.0)
    assert curve.points[-2:] == ((2.5, 90.0), (3.0, 100.0))


def test_a_mixed_room_learns_only_where_the_valve_reports(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _valve_room(
        [
            TrvConfig(
                name="climate.with_valve",
                valve_position_configured=True,
                valve_characteristic_seed=DANFOSS_SEED,
            ),
            TrvConfig(
                name="climate.without_valve", valve_characteristic_seed=DANFOSS_SEED
            ),
        ]
    )
    _valve_curve_cycles(controller, clock, [0.7, 0.7], [30.0, None])

    assert list(controller.valve_curve_learners) == ["climate.with_valve"]
    learners = controller.valve_curve_learners
    assert learners["climate.with_valve"].status is ValveCurveStatus.LEARNING
    # Still learning: the seed; without a valve entity no curve at all.
    assert controller.valve_characteristics() == [DANFOSS_SEED, None]
    assert list(controller.export_valve_curves()) == ["climate.with_valve"]
    assert list(controller.valve_curve_report()) == ["climate.with_valve"]


def _relative_targets(controller, clock):
    _hold_at_setpoint(controller, clock)
    return controller.compute(target_temperature_c=21.0).result


def _scatter(controller):
    trv = _Trv(controller.valve_curve_learners["trv1"])
    for opening_pct in (10, 20, 50, 60, 80):
        trv.sample(0.7, opening_pct)
    assert controller.valve_curve_learners["trv1"].status is ValveCurveStatus.SCATTERED


@pytest.mark.parametrize(
    ("prepare", "uses_seed"),
    [
        pytest.param(lambda controller: None, True, id="seed"),
        pytest.param(_scatter, True, id="scattered"),
        pytest.param(_learn_curve, False, id="learned"),
    ],
)
def test_a_trv_with_a_curve_gets_the_offset_for_the_opening(
    monkeypatch, prepare, uses_seed
):
    clock = _Clock(monkeypatch)
    controller = _valve_controller(
        DANFOSS_SEED, trv_setpoint_mode=TrvSetpointMode.RELATIVE
    )
    prepare(controller)
    [curve] = controller.valve_characteristics()
    result = _relative_targets(controller, clock)

    assert (curve == DANFOSS_SEED) is uses_seed
    assert 0 < result.demand_pct < 100
    # TRV reading 20.3 °C: the nearest 0.5 K step to reading + offset, at
    # least the first step above reading + 0.1 K.
    offset_c = curve.offset_c(result.demand_pct)
    assert result.trv_targets[0] == max(20.5, round((20.3 + offset_c) / 0.5) * 0.5)


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda controller: None, id="unlearned"),
        pytest.param(_scatter, id="scattered"),
    ],
)
def test_a_trv_without_seed_or_learned_curve_keeps_the_weighted_mapping(
    monkeypatch, prepare
):
    """The last resort for a TRV model whose profile has no seed."""
    clock = _Clock(monkeypatch)
    controller = _valve_controller(trv_setpoint_mode=TrvSetpointMode.RELATIVE)
    prepare(controller)

    result = _relative_targets(controller, clock)

    assert controller.valve_characteristics() == [None]
    assert result.demand_pct > 0
    assert (
        result.trv_targets
        == controller._emitter_model.calculate_target_temperatures(
            result.demand_pct, TrvSetpointMode.RELATIVE, [20.3]
        )
    )


@pytest.mark.parametrize(
    ("current_target_c", "threshold_pct"),
    [pytest.param(5.0, 10, id="shut"), pytest.param(22.5, 5, id="open")],
)
def test_the_planner_skips_openings_below_the_threshold(
    monkeypatch, current_target_c, threshold_pct
):
    _Clock(monkeypatch)
    controller = _valve_controller(**HAUPTBAD)
    _feed(controller, 22.0, outdoor_temp_c=8.0, flow_temp_c=35.0)
    controller.set_current_trv_targets([current_target_c])

    result = controller.compute(target_temperature_c=22.0).result

    # At its setpoint the room holds with about 3.5 %. A shut valve opens
    # only from 10 %, an open one stays open down to 5 %: below that the TRV
    # would get its minimum target and none of the planned heat.
    assert controller._hold_demand_pct(result.input) < 5
    assert result.demand_pct == 0 or result.demand_pct >= threshold_pct
    shut = result.trv_targets == [5.0]
    assert shut == (result.demand_pct == 0)
    assert (result.requested_heating_power_w == 0) == shut


def _planner_asks(controller, monkeypatch, demands_pct):
    """Replace the search: each compute gets the next requested demand."""
    requests = iter(demands_pct)

    def fake_search(mpc_input, estimate, activation_pct):
        return _DemandPrediction(
            demand_pct=next(requests),
            predicted_temperature_c=mpc_input.target_temp_c,
            prediction_error=0.0,
        )

    monkeypatch.setattr(controller, "_find_optimal_demand_prediction", fake_search)


def _smoothed_outputs(monkeypatch, current_targets, demands_pct, interval_s=40):
    clock = _Clock(monkeypatch)
    # Default smoothing: 20 % per 5 minutes, 5 % hysteresis, 300 s hold.
    controller = make_controller()
    _feed(controller, 20.8, outdoor_temp_c=5.0, flow_temp_c=40.0)
    controller.set_current_trv_targets(current_targets)
    _planner_asks(controller, monkeypatch, demands_pct)
    outputs = []
    for _ in demands_pct:
        outputs.append(controller.compute(target_temperature_c=21.0).result.demand_pct)
        clock.advance(interval_s)
    return outputs


@pytest.mark.parametrize(
    ("current_target_c", "threshold_pct", "demands_pct", "final_pct"),
    [
        # Shut valve, ramp from 0: it passed 5.33 %, which the hysteresis
        # then held although the TRV got its minimum target for it.
        pytest.param(5.0, 10, [0] + [10] * 20, 10, id="shut_opens"),
        # Shut valve, from 15 % down to a request of 5 %: the ramp passed
        # about 9 %, below the 10 % a shut TRV needs.
        pytest.param(5.0, 10, [15] * 3 + [5] * 30, 0, id="shut_closes"),
        pytest.param(22.0, 5, [15] * 3 + [5] * 30, 5, id="open_stays_open"),
    ],
)
def test_smoothing_never_stops_below_the_threshold(
    monkeypatch, current_target_c, threshold_pct, demands_pct, final_pct
):
    outputs = _smoothed_outputs(monkeypatch, [current_target_c], demands_pct)

    assert not [pct for pct in outputs if 0 < pct < threshold_pct]
    assert outputs[-1] == pytest.approx(final_pct)


def _restarted_controller():
    """Cold room after a restart, its TRV reporting its valve opening."""
    controller = RoomMpcController(
        thermal_config=RoomThermalConfig(
            room_heat_load_w=1200,
            room_area_m2=108,
            construction=Construction.VERY_LIGHT,
        ),
        trvs=[TrvConfig(name="trv1", valve_position_configured=True)],
        rate_limit_config=MpcRateLimitConfig(
            demand_hysteresis_pct=0, hold_time_s=0, max_demand_step_pct=20
        ),
        max_sensor_age_s=1800,
        room_sensor_configured=False,
        target_band_c=0.0,
    )
    controller.set_room_sensor_temperature(10.0)
    controller.set_outdoor_temperature(-15.0)
    controller.set_flow_temperature(45.0)
    return controller


def test_ramp_starts_from_the_reported_valve_opening(monkeypatch):
    _Clock(monkeypatch)
    controller = _restarted_controller()
    controller.set_valve_position(0, 92.0)

    first = controller.compute(target_temperature_c=25.0)

    # Not 20 %: the TRV is still open from before the restart.
    assert first.result.demand_pct == 100


def test_ramp_steps_down_from_the_reported_opening(monkeypatch):
    _Clock(monkeypatch)
    controller = _restarted_controller()
    controller.set_room_sensor_temperature(30.0)
    controller.set_valve_position(0, 80.0)

    first = controller.compute(target_temperature_c=21.0)

    # Too warm: the planner wants 0 %, the ramp leaves 80 % only by one step.
    assert first.result.demand_pct == pytest.approx(60)


def test_a_late_valve_report_still_seeds_the_ramp(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _restarted_controller()

    first = controller.compute(target_temperature_c=25.0)
    clock.advance(30)
    controller.set_valve_position(0, 92.0)
    second = controller.compute(target_temperature_c=25.0)

    assert first.result.demand_pct == 20
    # Seeded at 92 %, then the step 30 s allow: 20 % * 30 / 300.
    assert second.result.demand_pct == pytest.approx(92 + 2)


def test_seed_only_once_after_setup(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _restarted_controller()
    controller.set_room_sensor_temperature(30.0)
    controller.set_valve_position(0, 80.0)
    controller.compute(target_temperature_c=21.0)

    clock.advance(60)
    controller.set_valve_position(0, 100.0)
    later = controller.compute(target_temperature_c=21.0)

    # The ramp goes on from 60 %, the new report does not reset it.
    assert later.result.demand_pct < 60


def test_without_a_valve_report_the_ramp_starts_at_zero(monkeypatch):
    clock = _Clock(monkeypatch)
    controller = _restarted_controller()

    first = controller.compute(target_temperature_c=25.0)
    clock.advance(11 * 60)
    controller.set_valve_position(0, 92.0)
    later = controller.compute(target_temperature_c=25.0)

    assert first.result.demand_pct == 20
    # The window has passed: the report no longer seeds, the ramp steps on.
    assert later.result.demand_pct == 40

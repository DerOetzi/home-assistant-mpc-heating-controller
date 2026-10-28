import pytest

from heating_controller.const import Construction, RoomTemperatureStrategy
from heating_controller.controller.mpc.models.capacity import ThermalCapacityModel
from heating_controller.controller.mpc.models.emitter import HeatEmitterModel
from heating_controller.controller.mpc.models.loss import RoomLossModel
from heating_controller.controller.mpc.observer import (
    OBSERVER_DISTURBANCE_TIME_CONSTANT_S,
    OBSERVER_MAX_GAP_S,
    RoomStateObserver,
    step_radiator_power_w,
)
from heating_controller.controller.mpc.results import RoomMpcInput
from heating_controller.controller.mpc.types import RoomThermalConfig, TrvConfig

START_TS = 1_000_000.0
STEP_S = 300


def make_observer(disturbance_limit_w=1000.0, target_band_c=0.0):
    # UA 1000 W / 32 K = 31.25 W/K, capacity 15 m² * 165 kJ/m²K = 2.475 MJ/K.
    thermal_config = RoomThermalConfig(
        room_heat_load_w=1000, room_area_m2=15, construction=Construction.MEDIUM
    )
    emitter_model = HeatEmitterModel(
        thermal_config, [TrvConfig(name="trv", width_mm=1000, height_mm=600)]
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    observer = RoomStateObserver(
        emitter_model,
        loss_model,
        capacity_model,
        disturbance_limit_w,
        target_band_c=target_band_c,
    )
    return observer, loss_model


def make_input(
    now_ts, room_temp_c=20.0, outdoor_temp_c=0.0, flow_temp_c=40.0, target_temp_c=20.0
):
    return RoomMpcInput(
        now_ts=now_ts,
        target_temp_c=target_temp_c,
        room_temp_c=room_temp_c,
        outdoor_temp_c=outdoor_temp_c,
        flow_temp_c=flow_temp_c,
        used_room_sensor_strategy=RoomTemperatureStrategy.ROOM_SENSOR,
        trv_temperatures=[room_temp_c],
    )


def run(
    observer,
    steps,
    power_w,
    demand_pct,
    allowed=True,
    room_temp_c=20.0,
    start_ts=START_TS,
    target_temp_c=20.0,
):
    estimate = None
    for index in range(steps):
        mpc_input = make_input(
            start_ts + index * STEP_S,
            room_temp_c=room_temp_c,
            target_temp_c=target_temp_c,
        )
        estimate = observer.estimate(mpc_input, disturbance_update_allowed=allowed)
        observer.commit(estimate, mpc_input, power_w, demand_pct)
    return estimate


def test_step_radiator_power_follows_a_first_order_lag():
    assert step_radiator_power_w(0.0, 1000.0, 600.0, 600.0) == pytest.approx(
        1000.0 * (1 - 1 / 2.718281828), rel=1e-6
    )
    assert step_radiator_power_w(0.0, 1000.0, 0.0, 10.0) == 1000.0


def test_first_estimate_starts_at_the_measurement():
    observer, _ = make_observer()

    estimate = observer.estimate(make_input(START_TS, 19.3), True)

    assert estimate.room_temperature_c == 19.3
    assert estimate.radiator_power_w == 0.0
    assert estimate.disturbance_w == 0.0


def test_radiator_keeps_heating_after_the_valve_closed():
    observer, _ = make_observer()
    run(observer, 24, power_w=625.0, demand_pct=50)

    # Valve closes: the radiator gives off its stored heat over a while.
    mpc_input = make_input(START_TS + 24 * STEP_S)
    estimate = observer.estimate(mpc_input, True)
    observer.commit(estimate, mpc_input, 0.0, 0.0)
    after_one_step = observer.estimate(make_input(START_TS + 25 * STEP_S), True)

    assert estimate.radiator_power_w == pytest.approx(625.0, abs=1.0)
    assert 300.0 < after_one_step.radiator_power_w < 625.0


def test_disturbance_takes_up_a_lasting_offset():
    observer, loss_model = make_observer()
    # Loss at 20 °C / 0 °C is 625 W; the room holds 20 °C on 425 W, so 200 W
    # come from somewhere else.
    # Several disturbance time constants: it settles over a few nights.
    estimate = run(observer, 60 * 12, power_w=425.0, demand_pct=40)

    assert loss_model.calculate_heat_loss_w(20.0, 0.0) == pytest.approx(625.0)
    assert estimate.disturbance_w == pytest.approx(200.0, abs=15.0)
    assert estimate.room_temperature_c == pytest.approx(20.0, abs=0.02)


def test_disturbance_holds_while_not_allowed():
    observer, _ = make_observer()
    observer.restore_disturbance(120.0)

    estimate = run(observer, 48, power_w=425.0, demand_pct=40, allowed=False)

    assert estimate.disturbance_w == 120.0
    # The temperature correction alone keeps the estimate near the room.
    assert estimate.room_temperature_c == pytest.approx(20.0, abs=0.1)


def test_disturbance_is_clamped():
    observer, _ = make_observer(disturbance_limit_w=100.0)

    # The valve is open but the room gets nothing: the model keeps expecting
    # it to cool.
    estimate = run(observer, 24 * 12, power_w=0.0, demand_pct=50)
    observer.restore_disturbance(5000.0)

    assert estimate.disturbance_w == 100.0
    assert observer.disturbance_w == 100.0


def test_estimate_does_not_store_anything():
    observer, _ = make_observer()
    run(observer, 4, power_w=625.0, demand_pct=50)

    later = make_input(START_TS + 10 * STEP_S, room_temp_c=21.0)
    first = observer.estimate(later, True)
    second = observer.estimate(later, True)

    assert first == second


def test_jump_resyncs_without_moving_the_disturbance():
    observer, _ = make_observer()
    run(observer, 4, power_w=625.0, demand_pct=50)

    estimate = observer.estimate(make_input(START_TS + 4 * STEP_S, room_temp_c=22.0), True)

    assert estimate.room_temperature_c == 22.0
    assert estimate.disturbance_w == observer.disturbance_w


def test_disturbance_rate_is_limited():
    observer, _ = make_observer(disturbance_limit_w=1000.0)
    run(observer, 1, power_w=0.0, demand_pct=50)

    # Just under the resync limit for a whole step.
    estimate = observer.estimate(make_input(START_TS + STEP_S, room_temp_c=20.9), True)

    assert (
        0
        < estimate.disturbance_w
        <= 1000.0 * STEP_S / OBSERVER_DISTURBANCE_TIME_CONSTANT_S + 1e-9
    )


def test_disturbance_does_not_rise_while_the_valve_is_shut():
    # A room coasting above its setpoint on stored heat: the model expects it
    # to cool, it does not, and the controller already requests nothing.
    observer, _ = make_observer()
    observer.restore_disturbance(50.0)

    estimate = run(observer, 24, power_w=0.0, demand_pct=0)

    assert estimate.disturbance_w == 50.0


def test_disturbance_may_fall_while_the_valve_is_shut():
    # Shut valve, but 800 W disturbance against 625 W loss lets the model
    # expect a rise the room does not show: a lower disturbance asks for heat
    # again, out of the limit.
    # The room sits below its 20.5 °C setpoint: the controller needs heat.
    observer, _ = make_observer()
    observer.restore_disturbance(800.0)

    estimate = run(observer, 24, power_w=0.0, demand_pct=0, target_temp_c=20.5)

    assert estimate.disturbance_w < 800.0


def test_disturbance_does_not_fall_while_the_valve_is_fully_open():
    # The radiators cannot lift the room: booking that as a lasting loss would
    # only ask for more of what is already at its maximum.
    # 900 W against 625 W loss: once the radiator is warm the model expects a
    # rise the room does not show. (While it still warms up the model expects
    # cooling, and a rise is allowed.)
    observer, _ = make_observer()
    observer.restore_disturbance(-50.0)

    disturbances_w = []
    for index in range(24):
        mpc_input = make_input(START_TS + index * STEP_S)
        estimate = observer.estimate(mpc_input, disturbance_update_allowed=True)
        observer.commit(estimate, mpc_input, 900.0, 100)
        disturbances_w.append(estimate.disturbance_w)

    assert min(disturbances_w) >= -50.0
    # Once warm, it holds: no further step in either direction.
    assert disturbances_w[-1] == disturbances_w[-2]


def test_disturbance_may_rise_while_the_valve_is_fully_open():
    # 300 W against 625 W loss, yet the room holds: a gain, which lowers the
    # demand out of the limit.
    # The room sits above its 19.5 °C setpoint: the controller needs less heat.
    observer, _ = make_observer()

    estimate = run(observer, 24, power_w=300.0, demand_pct=100, target_temp_c=19.5)

    assert estimate.disturbance_w > 0


def test_disturbance_does_not_ratchet_down_with_the_valve_shut_above_setpoint():
    # Room above its setpoint, valve shut, the 0.1 K sensor stepping between
    # 20.0 and 19.9: the model expects cooling, rises are held, and the steps
    # down must not walk the disturbance down one by one.
    observer, _ = make_observer(target_band_c=0.1)
    disturbances_w = []
    for index in range(48):
        mpc_input = make_input(
            START_TS + index * STEP_S,
            room_temp_c=20.0 if index % 3 else 19.9,
            target_temp_c=19.0,
        )
        estimate = observer.estimate(mpc_input, disturbance_update_allowed=True)
        observer.commit(estimate, mpc_input, 0.0, 0)
        disturbances_w.append(estimate.disturbance_w)

    assert min(disturbances_w) == 0.0


def test_disturbance_does_not_fall_with_the_valve_shut_inside_the_band():
    # Inside the band the controller does not want heat either.
    observer, _ = make_observer(target_band_c=0.1)
    observer.restore_disturbance(800.0)

    estimate = run(
        observer, 24, power_w=0.0, demand_pct=0, room_temp_c=20.45, target_temp_c=20.5
    )

    assert estimate.disturbance_w == 800.0


def test_long_gap_restarts_from_the_measurement():
    observer, _ = make_observer()
    run(observer, 4, power_w=625.0, demand_pct=50)

    estimate = observer.estimate(
        make_input(START_TS + 3 * STEP_S + OBSERVER_MAX_GAP_S + 60, room_temp_c=17.0),
        True,
    )

    assert estimate.room_temperature_c == 17.0

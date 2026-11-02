import pytest
from heating_controller.const import Construction, RoomTemperatureStrategy
from heating_controller.controller.mpc.models.capacity import ThermalCapacityModel
from heating_controller.controller.mpc.models.emitter import HeatEmitterModel
from heating_controller.controller.mpc.models.loss import RoomLossModel
from heating_controller.controller.mpc.observer import (
    OBSERVER_MAX_GAP_S,
    ObserverEstimate,
    RoomStateObserver,
    step_radiator_power_w,
)
from heating_controller.controller.mpc.results import RoomMpcInput
from heating_controller.controller.mpc.types import RoomThermalConfig, TrvConfig

START_TS = 1_000_000.0
STEP_S = 300


def make_observer():
    # UA 1000 W / 32 K = 31.25 W/K, capacity 15 m² * 165 kJ/m²K = 2.475 MJ/K.
    thermal_config = RoomThermalConfig(
        room_heat_load_w=1000, room_area_m2=15, construction=Construction.MEDIUM
    )
    emitter_model = HeatEmitterModel(
        thermal_config, [TrvConfig(name="trv", width_mm=1000, height_mm=600)]
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    observer = RoomStateObserver(emitter_model, loss_model, capacity_model)
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


def run(observer, steps, power_w, demand_pct, room_temp_c=20.0, start_ts=START_TS):
    estimate = None
    for index in range(steps):
        mpc_input = make_input(start_ts + index * STEP_S, room_temp_c=room_temp_c)
        estimate = observer.estimate(mpc_input)
        observer.commit(estimate, mpc_input, power_w, demand_pct)
    return estimate


def test_step_radiator_power_follows_a_first_order_lag():
    assert step_radiator_power_w(0.0, 1000.0, 600.0, 600.0) == pytest.approx(
        1000.0 * (1 - 1 / 2.718281828), rel=1e-6
    )
    assert step_radiator_power_w(0.0, 1000.0, 0.0, 10.0) == 1000.0


def test_first_estimate_starts_at_the_measurement():
    observer, _ = make_observer()

    estimate = observer.estimate(make_input(START_TS, 19.3))

    assert estimate.room_temperature_c == 19.3
    assert estimate.radiator_power_w == 0.0


def test_radiator_keeps_heating_after_the_valve_closed():
    observer, _ = make_observer()
    run(observer, 24, power_w=625.0, demand_pct=50)

    # Valve closes: the radiator gives off its stored heat over a while.
    mpc_input = make_input(START_TS + 24 * STEP_S)
    estimate = observer.estimate(mpc_input)
    observer.commit(estimate, mpc_input, 0.0, 0.0)
    after_one_step = observer.estimate(make_input(START_TS + 25 * STEP_S))

    assert estimate.radiator_power_w == pytest.approx(625.0, abs=1.0)
    assert 300.0 < after_one_step.radiator_power_w < 625.0


def test_observer_has_no_lasting_offset():
    # Loss at 20 °C / 0 °C is 625 W; 609.4 W hold the model at 19.5 °C, yet
    # the room stays at 20 °C for a whole day.
    observer, loss_model = make_observer()
    power_w = 625.0 - 0.5 * 31.25
    assert loss_model.calculate_heat_loss_w(19.5, 0.0) == pytest.approx(power_w)

    day_steps = 24 * 3600 // STEP_S
    after_a_day = run(observer, day_steps, power_w=power_w, demand_pct=50)
    after_two_days = run(
        observer,
        day_steps,
        power_w=power_w,
        demand_pct=50,
        start_ts=START_TS + day_steps * STEP_S,
    )

    # The estimate follows the measurement within the 5-minute correction
    # and nothing builds up: the second day ends where the first one did.
    assert after_a_day.room_temperature_c == pytest.approx(20.0, abs=0.01)
    assert after_two_days.room_temperature_c == pytest.approx(
        after_a_day.room_temperature_c, abs=1e-9
    )
    assert after_two_days.radiator_power_w == pytest.approx(
        after_a_day.radiator_power_w, abs=1e-6
    )
    after_a_day = after_two_days

    # Ahead it runs on the model and the radiator lag alone: a fresh observer
    # started from the same temperature and radiator output expects the same.
    fresh, _ = make_observer()
    last_input = make_input(after_a_day.timestamp)
    fresh.commit(
        ObserverEstimate(
            timestamp=after_a_day.timestamp,
            room_temperature_c=after_a_day.room_temperature_c,
            radiator_power_w=after_a_day.radiator_power_w,
        ),
        last_input,
        power_w,
        50,
    )
    next_input = make_input(after_a_day.timestamp + STEP_S)
    assert observer.estimate(next_input) == fresh.estimate(next_input)


def test_estimate_does_not_store_anything():
    observer, _ = make_observer()
    run(observer, 4, power_w=625.0, demand_pct=50)

    later = make_input(START_TS + 10 * STEP_S, room_temp_c=21.0)
    first = observer.estimate(later)
    second = observer.estimate(later)

    assert first == second


def test_jump_resyncs_to_the_measurement():
    observer, _ = make_observer()
    run(observer, 4, power_w=625.0, demand_pct=50)

    estimate = observer.estimate(make_input(START_TS + 4 * STEP_S, room_temp_c=22.0))

    assert estimate.room_temperature_c == 22.0
    # The radiators keep the heat they had.
    assert estimate.radiator_power_w > 0


def test_long_gap_restarts_from_the_measurement():
    observer, _ = make_observer()
    run(observer, 4, power_w=625.0, demand_pct=50)

    estimate = observer.estimate(
        make_input(START_TS + 3 * STEP_S + OBSERVER_MAX_GAP_S + 60, room_temp_c=17.0)
    )

    assert estimate.room_temperature_c == 17.0

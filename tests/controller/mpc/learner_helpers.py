"""Inputs and a heat-balance night for learner tests, shared by the MPC tests."""

from heating_controller.const import RoomTemperatureStrategy
from heating_controller.controller.mpc.learner import UA_BALANCE_MAX_WINDOW_S
from heating_controller.controller.mpc.results import RoomMpcInput

# Heat balance for UA. The test room loses 31.25 W/K * 20 K = 625 W at 20 °C
# room and 0 °C outside. Holding the room with 312.5 W means half the UA.
NIGHT_START_TS = 2_000_000.0
STEP_S = 300
# Longer than the maximum stretch, so the balance runs without a closing event.
NIGHT_HOURS = UA_BALANCE_MAX_WINDOW_S / 3600 + 0.5


def make_input(
    now_ts,
    room_temp_c=20.0,
    outdoor_temp_c=-5.0,
    flow_temp_c=45.0,
    room_sensor_fallback=False,
    valve_position_missing=False,
    target_temp_c=21.0,
    trv_adapting=False,
    radiator_not_heating=False,
    valve_positions_pct=(),
    radiator_not_heating_since_ts=None,
    hot_water_charge=False,
):
    return RoomMpcInput(
        now_ts=now_ts,
        target_temp_c=target_temp_c,
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
        valve_position_missing=valve_position_missing,
        trv_adapting=trv_adapting,
        radiator_not_heating=radiator_not_heating,
        radiator_not_heating_since_ts=radiator_not_heating_since_ts,
        hot_water_charge=hot_water_charge,
        valve_positions_pct=list(valve_positions_pct),
    )


def feed_night(
    learner,
    hours=NIGHT_HOURS,
    start_ts=NIGHT_START_TS,
    room_temp_c=20.0,
    outdoor_temp_c=0.0,
    power_w=312.5,
    room_temps=None,
    skip=(),
    room_sensor_fallback=False,
    valve_position_missing=False,
    # The balance nights hold the room at its setpoint.
    target_temp_c=20.0,
    trv_adapting=False,
    radiator_not_heating=False,
    valve_positions_pct=(),
    flow_temp_c=40.0,
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
                flow_temp_c=flow_temp_c,
                room_sensor_fallback=room_sensor_fallback,
                valve_position_missing=valve_position_missing,
                target_temp_c=target_temp_c,
                trv_adapting=trv_adapting,
                radiator_not_heating=radiator_not_heating,
                valve_positions_pct=valve_positions_pct,
            ),
            applied_heating_power_w=power_w,
            radiator_power_w=power_w,
        )
    return start_ts + (count - 1) * STEP_S


def jump_samples(
    *,
    true_capacity_j_per_k,
    ua_w_per_k,
    start_room_c,
    old_target_c,
    new_target_c,
    power_w,
    outdoor_temp_c=0.0,
    start_ts,
    step_s=STEP_S,
    max_steps=40,
):
    """A setpoint jump and a room of the given capacity following it.

    Two samples at the old setpoint, then the jump and one sample per step
    until the room is near its new setpoint (or max_steps), plus one more.
    Each is (timestamp, room reading rounded to 0.1 K like a real sensor,
    setpoint, power). Returns the samples and the timestamp after them.
    """
    heat_up = new_target_c > old_target_c
    samples = [
        (start_ts, round(start_room_c, 1), old_target_c, power_w),
        (start_ts + step_s, round(start_room_c, 1), old_target_c, power_w),
    ]
    ts = start_ts + 2 * step_s
    room_c = start_room_c
    for _ in range(max_steps):
        reading_c = round(room_c, 1)
        samples.append((ts, reading_c, new_target_c, power_w))
        ts += step_s
        if (heat_up and reading_c >= new_target_c - 0.2) or (
            not heat_up and reading_c <= new_target_c + 0.2
        ):
            break
        room_c += (
            (power_w - ua_w_per_k * (room_c - outdoor_temp_c))
            * step_s
            / true_capacity_j_per_k
        )
    samples.append((ts, round(room_c, 1), new_target_c, power_w))
    return samples, ts + step_s


def feed_samples(learner, samples, outdoor_temp_c=0.0, **flags):
    for ts, reading_c, target_c, power_w in samples:
        learner.append_history(
            make_input(
                ts,
                room_temp_c=reading_c,
                outdoor_temp_c=outdoor_temp_c,
                target_temp_c=target_c,
                **flags,
            ),
            applied_heating_power_w=power_w,
            radiator_power_w=power_w,
        )


def feed_jump(learner, *, outdoor_temp_c=0.0, **kwargs):
    """Feed a whole jump stretch (see jump_samples); flags go to make_input.

    Returns the timestamp after the stretch.
    """
    flags = {
        key: kwargs.pop(key)
        for key in (
            "room_sensor_fallback",
            "valve_position_missing",
            "trv_adapting",
            "radiator_not_heating",
            "valve_positions_pct",
        )
        if key in kwargs
    }
    samples, next_ts = jump_samples(outdoor_temp_c=outdoor_temp_c, **kwargs)
    feed_samples(learner, samples, outdoor_temp_c=outdoor_temp_c, **flags)
    return next_ts

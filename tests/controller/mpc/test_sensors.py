from heating_controller.const import RoomTemperatureStrategy
from heating_controller.controller.mpc.results import RoomMpcErrorCode
from heating_controller.controller.mpc.sensors import RoomMpcSensors
from heating_controller.controller.mpc.types import TrvConfig


def make_sensors(trv_count=1, max_sensor_age_s=1800):
    trvs = [TrvConfig(name=f"trv{i}") for i in range(trv_count)]
    return RoomMpcSensors(trvs, max_sensor_age_s)


def test_uses_room_sensor_when_fresh():
    sensors = make_sensors()
    sensors.set_room_sensor_temperature(21.0)
    sensors.set_trv_temperature(0, 19.0)

    result = sensors.get_room_temperature(now_ts=1000)
    assert result.valid is True
    assert result.temperature_c == 21.0
    assert result.used_strategy == RoomTemperatureStrategy.ROOM_SENSOR


def test_falls_back_to_trv_average_when_no_room_sensor():
    sensors = make_sensors(trv_count=2)
    sensors.set_trv_temperature(0, 20.0)
    sensors.set_trv_temperature(1, 22.0)

    result = sensors.get_room_temperature()
    assert result.valid is True
    assert result.temperature_c == 21.0
    assert result.used_strategy == RoomTemperatureStrategy.TRV_AVERAGE


def test_missing_room_temperature_when_nothing_configured():
    sensors = make_sensors()
    result = sensors.get_room_temperature()
    assert result.valid is False
    assert result.error.code == RoomMpcErrorCode.MISSING_ROOM_TEMPERATURE


def test_stale_value_outside_max_age_is_not_fresh():
    sensors = make_sensors()
    sensors.set_room_sensor_temperature(21.0)

    import time

    result = sensors.get_room_temperature(now_ts=time.time() + 999999)
    assert result.valid is False


def test_create_input_requires_outdoor_temperature():
    sensors = make_sensors()
    sensors.set_room_sensor_temperature(21.0)

    result = sensors.create_input(target_temp_c=22.0)
    assert result.valid is False
    assert result.error.code == "missing_outdoor_temperature"


def test_create_input_valid_with_all_sensors():
    sensors = make_sensors()
    sensors.set_room_sensor_temperature(21.0)
    sensors.set_outdoor_temperature(-3.0)
    sensors.set_flow_temperature(42.0)

    result = sensors.create_input(target_temp_c=22.0)
    assert result.valid is True
    assert result.input.room_temp_c == 21.0
    assert result.input.outdoor_temp_c == -3.0
    assert result.input.flow_temp_c == 42.0


def test_outlier_is_ignored_unless_it_repeats_as_drift():
    sensors = make_sensors()
    for value in (20.0, 20.1, 19.9):
        sensors.set_room_sensor_temperature(value)

    sensors.set_room_sensor_temperature(40.0)
    result = sensors.get_room_temperature()
    assert result.temperature_c == 19.9

    sensors.set_room_sensor_temperature(40.0)
    sensors.set_room_sensor_temperature(40.0)
    result = sensors.get_room_temperature()
    assert result.temperature_c == 40.0


def test_flow_temperature_follows_jumps_without_outlier_filter():
    # Leaving-water readings from the 2026-09-22 DHW charge, fed on the
    # coordinator's poll cadence: every jump must be taken as-is.
    sensors = make_sensors()
    sensors.set_room_sensor_temperature(20.5)
    sensors.set_outdoor_temperature(10.0)

    now_ts = 1_000_000.0
    for value in (27.0, 29.0, 29.0, 29.0, 29.0, 41.0, 58.0, 36.0, 35.0):
        sensors.set_flow_temperature(value, now_ts)
        result = sensors.create_input(target_temp_c=21.0, now_ts=now_ts)
        assert result.input.flow_temp_c == value
        now_ts += 300.5


def test_flow_temperature_still_expires_after_max_age():
    sensors = make_sensors(max_sensor_age_s=1800)
    sensors.set_room_sensor_temperature(20.5)
    sensors.set_outdoor_temperature(10.0)
    sensors.set_flow_temperature(35.0, 1_000_000.0)

    result = sensors.create_input(target_temp_c=21.0, now_ts=1_000_000.0 + 1801)
    assert result.input.flow_temp_c is None


def test_outlier_drift_is_confirmed_on_poll_cadence_with_jitter():
    # The poll re-feeds an unchanged state every 5 min; a drift window of the
    # same length let any jitter expire the candidate before confirmation.
    sensors = make_sensors(max_sensor_age_s=0)
    now_ts = 1_000_000.0
    for value in (20.0, 20.1, 19.9):
        sensors.set_room_sensor_temperature(value, now_ts)
        now_ts += 300.5

    for _ in range(2):
        sensors.set_room_sensor_temperature(27.0, now_ts)
        assert sensors.get_room_temperature(now_ts).temperature_c == 19.9
        now_ts += 300.5

    sensors.set_room_sensor_temperature(27.0, now_ts)
    assert sensors.get_room_temperature(now_ts).temperature_c == 27.0


def test_outlier_drift_candidate_expires_after_long_gap():
    sensors = make_sensors(max_sensor_age_s=0)
    now_ts = 1_000_000.0
    for value in (20.0, 20.1, 19.9):
        sensors.set_room_sensor_temperature(value, now_ts)

    for _ in range(3):
        sensors.set_room_sensor_temperature(27.0, now_ts)
        assert sensors.get_room_temperature(now_ts).temperature_c == 19.9
        now_ts += 601

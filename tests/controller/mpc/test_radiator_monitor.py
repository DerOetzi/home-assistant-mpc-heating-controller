from heating_controller.controller.mpc.radiator_monitor import (
    RADIATOR_CHECK_WINDOW_S,
    RadiatorDeliveryMonitor,
)

STEP_S = 300
START_TS = 1_000_000.0


def _feed(
    monitor,
    trv_temps,
    *,
    start_ts=START_TS,
    opening_pct=100.0,
    room_c=20.6,
    heat_available=True,
    hot_water_charge=False,
):
    for index, trv_c in enumerate(trv_temps):
        monitor.update(
            start_ts + index * STEP_S,
            opening_pct,
            trv_c,
            room_c,
            heat_available,
            hot_water_charge=hot_water_charge,
        )
    return start_ts + (len(trv_temps) - 1) * STEP_S


HOUR = RADIATOR_CHECK_WINDOW_S // STEP_S + 1


def test_a_radiator_full_of_air_is_reported_after_an_hour():
    # Bedroom, 28 October: valve 94-100 %, the TRV within half a kelvin of
    # the room for hours.
    monitor = RadiatorDeliveryMonitor()

    _feed(monitor, [21.1] * (HOUR - 1))
    assert monitor.not_heating is False

    _feed(monitor, [21.1], start_ts=START_TS + (HOUR - 1) * STEP_S)
    assert monitor.not_heating is True


def test_a_radiator_that_warms_its_trv_is_fine():
    monitor = RadiatorDeliveryMonitor()

    _feed(monitor, [20.9 + 0.05 * index for index in range(HOUR + 6)])

    assert monitor.not_heating is False


def test_a_fully_open_radiator_holding_its_output_is_fine():
    # Study: valve open all night, TRV steady two kelvin above the room.
    monitor = RadiatorDeliveryMonitor()

    _feed(monitor, [22.8] * (HOUR + 12))

    assert monitor.not_heating is False


def test_a_radiator_that_warmed_once_is_not_judged_on_a_steady_reading():
    monitor = RadiatorDeliveryMonitor()

    temps = [20.6 + 0.1 * min(index, 4) for index in range(HOUR + 12)]
    _feed(monitor, temps, room_c=20.8)

    assert monitor.not_heating is False


def test_the_verdict_holds_while_the_valve_is_shut_and_clears_once_it_heats():
    monitor = RadiatorDeliveryMonitor()
    end_ts = _feed(monitor, [21.1] * HOUR)
    assert monitor.not_heating is True

    end_ts = _feed(monitor, [21.0] * 3, start_ts=end_ts + STEP_S, opening_pct=0.0)
    assert monitor.not_heating is True

    _feed(monitor, [21.0, 21.2, 21.5], start_ts=end_ts + STEP_S)
    assert monitor.not_heating is False


def test_a_hot_water_charge_starts_the_hour_over():
    monitor = RadiatorDeliveryMonitor()

    end_ts = _feed(monitor, [21.1] * (HOUR - 4))
    end_ts = _feed(
        monitor, [21.1] * 4, start_ts=end_ts + STEP_S, hot_water_charge=True
    )
    _feed(monitor, [21.1] * 4, start_ts=end_ts + STEP_S)

    assert monitor.not_heating is False


def test_no_verdict_without_heating():
    monitor = RadiatorDeliveryMonitor()

    _feed(monitor, [21.1] * (HOUR + 6), heat_available=False)

    assert monitor.not_heating is False


def test_no_verdict_below_a_wide_open_valve():
    monitor = RadiatorDeliveryMonitor()

    _feed(monitor, [21.1] * (HOUR + 6), opening_pct=60.0)

    assert monitor.not_heating is False


def test_no_verdict_while_the_flow_is_too_cool_for_a_sound_radiator():
    # Rebekka, 30 October: heating curve lowered, flow down to 30 °C at a
    # 20.9 °C room; even a sound radiator stays barely warm.
    monitor = RadiatorDeliveryMonitor()

    for index in range(HOUR + 6):
        monitor.update(
            START_TS + index * STEP_S, 100.0, 21.1, 20.9, True, 0.18
        )

    assert monitor.not_heating is False


def test_a_cold_radiator_at_a_warm_flow_is_still_reported():
    monitor = RadiatorDeliveryMonitor()

    for index in range(HOUR):
        monitor.update(
            START_TS + index * STEP_S, 100.0, 21.1, 20.6, True, 0.42
        )

    assert monitor.not_heating is True


def test_the_rise_counts_from_just_before_the_valve_opened():
    # Dining room, 31 October: valve 90 % from 03:38, its TRV up from 21.01
    # to 21.36 by 03:53 and steady after, the room at 20.9. The first cycle
    # with the valve open already read 21.13.
    monitor = RadiatorDeliveryMonitor()
    for index, trv_c in enumerate([21.21, 21.11, 21.01]):
        monitor.update(START_TS + index * 100, 59.0, trv_c, 20.9, True, 0.35)

    _feed(
        monitor,
        [21.13, 21.25, 21.36] + [21.36] * HOUR,
        start_ts=START_TS + 300,
        room_c=20.9,
    )

    assert monitor.not_heating is False


def test_a_rising_room_counts_as_a_radiator_that_heats():
    # Bedroom, 30 October: valve open from 21:19, TRV only 21.08 -> 21.30
    # with the radiator fan blowing, the room 20.8 -> 21.2 in that hour.
    monitor = RadiatorDeliveryMonitor()

    for index in range(HOUR + 1):
        monitor.update(
            START_TS + index * STEP_S,
            100.0,
            21.08 + 0.22 * index / HOUR,
            20.8 + 0.4 * index / HOUR,
            True,
            0.35,
        )

    assert monitor.not_heating is False


def test_readings_long_before_the_opening_are_no_baseline():
    monitor = RadiatorDeliveryMonitor()
    monitor.update(START_TS, 0.0, 20.5, 20.6, True)

    _feed(monitor, [21.1] * HOUR, start_ts=START_TS + 3600)

    assert monitor.not_heating is True


def test_a_room_swinging_by_a_sensor_step_does_not_hide_a_cold_radiator():
    # Bedroom, 28 October, radiator full of air: the room 20.5 -> 20.7 within
    # an hour in 0.1 K steps, the TRV within half a kelvin of it.
    monitor = RadiatorDeliveryMonitor()

    for index in range(HOUR):
        monitor.update(
            START_TS + index * STEP_S,
            100.0,
            21.0,
            20.5 + 0.1 * (index // 6),
            True,
        )

    assert monitor.not_heating is True


def test_a_rising_room_only_holds_off_the_verdict_for_that_hour():
    monitor = RadiatorDeliveryMonitor()
    room_temps = [20.6 + 0.4 * index / HOUR for index in range(HOUR)]
    room_temps += [room_temps[-1]] * (HOUR + 1)

    for index, room_c in enumerate(room_temps):
        monitor.update(START_TS + index * STEP_S, 100.0, 21.1, room_c, True)

    assert monitor.not_heating is True


def test_the_verdict_names_when_the_valve_opened():
    monitor = RadiatorDeliveryMonitor()

    end_ts = _feed(monitor, [21.1] * HOUR)
    assert monitor.not_heating is True
    assert monitor.not_heating_since_ts == START_TS

    _feed(monitor, [21.0, 21.2, 21.5], start_ts=end_ts + STEP_S)
    assert monitor.not_heating_since_ts is None

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
    flow_c=38.0,
    heat_available=True,
):
    for index, trv_c in enumerate(trv_temps):
        monitor.update(
            start_ts + index * STEP_S,
            opening_pct,
            trv_c,
            room_c,
            flow_c,
            heat_available,
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
    end_ts = _feed(monitor, [21.1] * 4, start_ts=end_ts + STEP_S, flow_c=54.0)
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
            START_TS + index * STEP_S, 100.0, 21.1, 20.9, 30.0, True, 0.18
        )

    assert monitor.not_heating is False


def test_a_cold_radiator_at_a_warm_flow_is_still_reported():
    monitor = RadiatorDeliveryMonitor()

    for index in range(HOUR):
        monitor.update(
            START_TS + index * STEP_S, 100.0, 21.1, 20.6, 38.0, True, 0.42
        )

    assert monitor.not_heating is True

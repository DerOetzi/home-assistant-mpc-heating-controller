from heating_controller.controller.mpc.radiator_monitor import (
    RADIATOR_CHECK_WINDOW_S,
    RadiatorCheckSample,
    RadiatorDeliveryMonitor,
)

STEP_S = 300
START_TS = 1_000_000.0
WINDOW_STEPS = RADIATOR_CHECK_WINDOW_S // STEP_S


def _sample(ts, **overrides):
    values = {
        "now_ts": ts,
        "demand_pct": 100.0,
        "room_temperature_c": 19.5,
        "mode_target_c": 21.0,
        "heat_available": True,
        "full_output_fraction": 0.6,
        "surplus_share": 0.5,
        "excluded": False,
    }
    values.update(overrides)
    return RadiatorCheckSample(**values)


def _feed(monitor, steps, *, start_ts=START_TS, rooms=None, **overrides):
    """Feed steps + 1 samples, from start_ts on; returns the last timestamp."""
    for index in range(steps + 1):
        if rooms is not None:
            overrides["room_temperature_c"] = rooms[index]
        monitor.update(_sample(start_ts + index * STEP_S, **overrides))
    return start_ts + steps * STEP_S


def test_a_cold_radiator_is_reported_after_two_hours():
    monitor = RadiatorDeliveryMonitor()

    _feed(monitor, WINDOW_STEPS - 1)
    assert monitor.not_heating is False

    monitor.update(_sample(START_TS + WINDOW_STEPS * STEP_S))
    assert monitor.not_heating is True
    assert monitor.not_heating_since_ts == START_TS


def test_a_room_at_its_setpoint_is_not_judged():
    monitor = RadiatorDeliveryMonitor()
    _feed(monitor, 2 * WINDOW_STEPS, demand_pct=85.0, room_temperature_c=20.8)
    assert monitor.not_heating is False


def test_a_rising_room_is_not_judged():
    monitor = RadiatorDeliveryMonitor()
    rooms = [round(19.0 + 0.02 * index, 2) for index in range(2 * WINDOW_STEPS + 1)]
    _feed(monitor, 2 * WINDOW_STEPS, rooms=rooms)
    assert monitor.not_heating is False


def test_a_flow_too_low_for_the_radiator_is_not_judged():
    monitor = RadiatorDeliveryMonitor()
    _feed(monitor, 2 * WINDOW_STEPS, full_output_fraction=0.2)
    assert monitor.not_heating is False


def test_a_room_the_model_expects_to_hold_is_not_judged():
    monitor = RadiatorDeliveryMonitor()
    _feed(monitor, 2 * WINDOW_STEPS, surplus_share=0.1)
    assert monitor.not_heating is False


def test_a_low_demand_is_not_judged():
    monitor = RadiatorDeliveryMonitor()
    _feed(monitor, 2 * WINDOW_STEPS, demand_pct=70.0)
    assert monitor.not_heating is False


def test_no_heat_available_is_not_judged():
    monitor = RadiatorDeliveryMonitor()
    _feed(monitor, 2 * WINDOW_STEPS, heat_available=False)
    assert monitor.not_heating is False


def test_a_hot_water_charge_starts_the_window_over():
    monitor = RadiatorDeliveryMonitor()
    ts = _feed(monitor, WINDOW_STEPS - 2)
    monitor.update(_sample(ts + STEP_S, excluded=True))
    ts = _feed(monitor, WINDOW_STEPS - 1, start_ts=ts + 2 * STEP_S)
    assert monitor.not_heating is False

    monitor.update(_sample(ts + STEP_S))
    assert monitor.not_heating is True
    assert monitor.not_heating_since_ts == ts + STEP_S - RADIATOR_CHECK_WINDOW_S


def test_the_verdict_holds_while_nothing_can_be_tested():
    monitor = RadiatorDeliveryMonitor()
    ts = _feed(monitor, WINDOW_STEPS)
    assert monitor.not_heating is True

    _feed(monitor, 6, start_ts=ts + STEP_S, excluded=True, heat_available=False)
    assert monitor.not_heating is True


def test_the_verdict_clears_once_the_room_rises():
    monitor = RadiatorDeliveryMonitor()
    ts = _feed(monitor, WINDOW_STEPS)
    assert monitor.not_heating is True

    monitor.update(_sample(ts + STEP_S, room_temperature_c=19.7))
    assert monitor.not_heating is True
    monitor.update(_sample(ts + 2 * STEP_S, room_temperature_c=19.8))
    assert monitor.not_heating is False
    assert monitor.not_heating_since_ts is None


def test_the_verdict_clears_once_the_demand_falls():
    monitor = RadiatorDeliveryMonitor()
    ts = _feed(monitor, WINDOW_STEPS)

    monitor.update(_sample(ts + STEP_S, demand_pct=60.0))
    assert monitor.not_heating is True
    monitor.update(_sample(ts + 2 * STEP_S, demand_pct=40.0))
    assert monitor.not_heating is False


def test_after_clearing_a_new_window_is_needed():
    monitor = RadiatorDeliveryMonitor()
    ts = _feed(monitor, WINDOW_STEPS)
    monitor.update(_sample(ts + STEP_S, demand_pct=40.0))

    ts = _feed(monitor, WINDOW_STEPS - 1, start_ts=ts + 2 * STEP_S)
    assert monitor.not_heating is False
    monitor.update(_sample(ts + STEP_S))
    assert monitor.not_heating is True

from heating_controller.controller.mpc.hot_water import (
    HOT_WATER_REFERENCE_WINDOW_S,
    HotWaterChargeDetector,
)

STEP_S = 300
START_TS = 1_000_000.0


def _feed(detector, flows_c, start_ts=START_TS, heat_available=True):
    for index, flow_c in enumerate(flows_c):
        detector.update(start_ts + index * STEP_S, flow_c, heat_available)
    return start_ts + (len(flows_c) - 1) * STEP_S


def test_a_flow_spike_is_a_hot_water_charge():
    # 31 October, 05:05-05:35: flow 35 °C, then 44-56 °C during the charge.
    detector = HotWaterChargeDetector()

    _feed(detector, [35.0, 35.5, 35.0, 44.0, 56.0])

    assert detector.charging is True
    assert detector.heating_flow_c == 35.0


def test_the_charge_ends_once_the_flow_is_back():
    detector = HotWaterChargeDetector()

    _feed(detector, [35.0, 35.5, 56.0, 56.0, 36.0])

    assert detector.charging is False
    assert detector.heating_flow_c == 36.0


def test_normal_flow_swings_are_no_charge():
    detector = HotWaterChargeDetector()

    _feed(detector, [35.0, 36.0, 37.0, 39.0, 38.0])

    assert detector.charging is False


def test_a_lasting_rise_is_taken_as_the_new_heating_flow():
    detector = HotWaterChargeDetector()

    count = HOT_WATER_REFERENCE_WINDOW_S // STEP_S + 2
    _feed(detector, [35.0, 35.0] + [42.0] * count)

    assert detector.charging is False
    assert detector.heating_flow_c == 42.0


def test_a_heating_pause_starts_the_reference_over():
    detector = HotWaterChargeDetector()

    end_ts = _feed(detector, [22.0, 22.0], heat_available=False)
    _feed(detector, [35.0], start_ts=end_ts + STEP_S)

    assert detector.charging is False


def test_no_flow_reading_keeps_the_state():
    detector = HotWaterChargeDetector()

    end_ts = _feed(detector, [35.0, 56.0])
    detector.update(end_ts + STEP_S, None, True)

    assert detector.charging is True

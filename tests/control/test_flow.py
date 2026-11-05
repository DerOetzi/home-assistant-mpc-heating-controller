from heating_controller.control.flow import (
    ActuatorMode,
    FlowControlConfig,
    FlowController,
    FlowInputs,
    FlowSource,
    HeatingCurve,
)
from heating_controller.control.write_budget import WritePriority

# The house's Daikin curve: -13/54, 18/29, raise 2 K around 0 °C, width 4.
CURVE = HeatingCurve(-13.0, 18.0, 54.0, 29.0, bump_k=2.0, bump_width_k=4.0)
T0 = 1_800_000_000.0


def _config(**overrides) -> FlowControlConfig:
    values = {
        "mode": ActuatorMode.OFFSET,
        "minimum": -10.0,
        "maximum": 10.0,
        "step": 1.0,
        "fallback": 0.0,
        "margin_k": 1.0,
        "hysteresis_k": 0.5,
        "hold_s": 1800.0,
        "threshold_c": None,
    }
    values.update(overrides)
    return FlowControlConfig(**values)


def _inputs(**overrides) -> FlowInputs:
    values = {
        "outdoor_c": 8.0,
        "lead_min_flow_c": 33.0,
        "charge_flow_c": None,
        "actual": 0.0,
        "heating_on": True,
        "reachable": True,
    }
    values.update(overrides)
    return FlowInputs(**values)


def test_curve_matches_the_template() -> None:
    assert CURVE.flow_at(-13.0) == 54.0
    assert CURVE.flow_at(18.0) == 29.0
    assert CURVE.flow_at(25.0) == 29.0
    # 8 °C: 54 - 25/31 * 21 = 37.06
    assert CURVE.flow_at(8.0) == 37.1
    # Full raise at 0 °C, half at 1 °C.
    assert CURVE.flow_at(0.0) == round(54 - 25 / 31 * 13 + 2, 1)
    assert CURVE.flow_at(1.0) == round(54 - 25 / 31 * 14 + 1, 1)


def test_offset_brings_curve_to_lead_flow_plus_margin() -> None:
    controller = FlowController(_config(), T0 - 3600)
    decision = controller.decide(_inputs(), CURVE, T0)

    # 33 + 1 = 34 against curve 37.06 -> -3.06 -> -3
    assert decision.target_flow_c == 34.0
    assert decision.value == -3.0
    assert decision.write == -3.0
    assert decision.priority == WritePriority.NORMAL
    assert decision.source is FlowSource.LEAD


def test_setpoint_mode_writes_the_flow_itself() -> None:
    controller = FlowController(
        _config(
            mode=ActuatorMode.SETPOINT,
            minimum=25,
            maximum=55,
            step=0.5,
            threshold_c=29.0,
        ),
        T0 - 3600,
    )
    decision = controller.decide(_inputs(actual=40.0), CURVE, T0)
    assert decision.write == 34.0


def test_setpoint_never_below_threshold() -> None:
    controller = FlowController(
        _config(mode=ActuatorMode.SETPOINT, minimum=20, maximum=55, threshold_c=29.0),
        T0 - 3600,
    )
    decision = controller.decide(_inputs(lead_min_flow_c=0.0, actual=35.0), CURVE, T0)

    assert decision.source is FlowSource.THRESHOLD
    assert decision.target_flow_c == 29.0
    assert decision.value == 29.0


def test_offset_mode_has_no_threshold_floor() -> None:
    controller = FlowController(_config(threshold_c=None), T0 - 3600)
    decision = controller.decide(_inputs(lead_min_flow_c=0.0), CURVE, T0)

    # Margin 1 K against curve 37.06 -> -36 -> clamped to -10.
    assert decision.source is FlowSource.LEAD
    assert decision.value == -10.0


def test_hold_time_and_hysteresis_keep_writes_rare() -> None:
    controller = FlowController(_config(), T0 - 3600)
    controller.written(-3.0, 0.0, T0)

    # Cloud still shows the old value: no external change, no new write.
    controller.observe_actual(0.0, T0 + 60)
    assert controller.external_change_ts is None
    held = controller.decide(_inputs(lead_min_flow_c=40.0), CURVE, T0 + 600)
    assert held.write is None
    assert held.reason == "hold"

    controller.observe_actual(-3.0, T0 + 900)
    small = controller.decide(
        _inputs(lead_min_flow_c=33.3, actual=-3.0), CURVE, T0 + 4000
    )
    assert small.write is None

    later = controller.decide(
        _inputs(lead_min_flow_c=36.0, actual=-3.0), CURVE, T0 + 4000
    )
    assert later.write == 0.0


def test_external_change_restarts_hold() -> None:
    controller = FlowController(_config(), T0 - 3600)
    controller.written(-3.0, 0.0, T0)
    controller.observe_actual(-3.0, T0 + 600)
    controller.observe_actual(2.0, T0 + 1200)

    assert controller.external_change_ts == T0 + 1200
    decision = controller.decide(_inputs(actual=2.0), CURVE, T0 + 2000)
    assert decision.write is None
    assert decision.reason == "hold"


def test_fallback_when_lead_value_missing() -> None:
    controller = FlowController(_config(fallback=0.0), T0 - 3600)
    decision = controller.decide(_inputs(lead_min_flow_c=None, actual=-3.0), CURVE, T0)

    assert decision.source is FlowSource.FALLBACK
    assert decision.write == 0.0
    assert decision.priority == WritePriority.REQUIRED

    controller.written(0.0, -3.0, T0)
    again = controller.decide(
        _inputs(lead_min_flow_c=None, actual=-3.0), CURVE, T0 + 60
    )
    assert again.write is None


def test_fallback_when_outdoor_missing_in_offset_mode() -> None:
    controller = FlowController(_config(), T0 - 3600)
    decision = controller.decide(_inputs(outdoor_c=None, actual=2.0), CURVE, T0)
    assert decision.source is FlowSource.FALLBACK
    assert decision.write == 0.0


def test_leaving_fallback_is_not_held() -> None:
    controller = FlowController(_config(), T0 - 3600)
    controller.decide(_inputs(lead_min_flow_c=None, actual=-3.0), CURVE, T0)
    controller.written(0.0, -3.0, T0)
    decision = controller.decide(_inputs(actual=0.0), CURVE, T0 + 120)

    assert decision.write == -3.0
    assert decision.priority == WritePriority.REQUIRED


def test_charge_raise_goes_out_at_once() -> None:
    controller = FlowController(_config(), T0 - 3600)
    controller.written(-3.0, 0.0, T0)
    decision = controller.decide(
        _inputs(actual=-3.0, charge_flow_c=40.0), CURVE, T0 + 60
    )

    assert decision.source is FlowSource.CHARGE
    assert decision.write == 3.0
    assert decision.priority == WritePriority.REQUIRED


def test_nothing_while_heating_off_or_unreachable() -> None:
    controller = FlowController(_config(), T0 - 3600)
    assert controller.decide(_inputs(heating_on=False), CURVE, T0).write is None
    assert controller.decide(_inputs(reachable=False), CURVE, T0).write is None


def test_clamped_to_actuator_range() -> None:
    controller = FlowController(_config(), T0 - 3600)
    decision = controller.decide(_inputs(lead_min_flow_c=60.0), CURVE, T0)
    assert decision.value == 10.0

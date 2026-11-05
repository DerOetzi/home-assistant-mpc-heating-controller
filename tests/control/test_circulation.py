from datetime import datetime, timedelta

from heating_controller.control.circulation import (
    CirculationConfig,
    CirculationController,
    CirculationInputs,
    CirculationReason,
)

MORNING = datetime(2026, 11, 5, 7, 0)  # peak 1
NOON = datetime(2026, 11, 5, 12, 0)  # off-peak


def _inputs(now: datetime = MORNING, **overrides) -> CirculationInputs:
    values = {
        "now": now,
        "pump_on": False,
        "pump_minutes": 10.0,
        "return_c": 42.0,
        "outlet_c": 50.0,
        "continuous": False,
        "heater_ok": True,
        "people_gates_open": True,
        "draw": False,
    }
    values.update(overrides)
    return CirculationInputs(**values)


def test_starts_below_peak_threshold_and_stops_hot() -> None:
    ctrl = CirculationController(CirculationConfig())
    start = ctrl.decide(_inputs(return_c=39.0))
    assert start.pump_on is True
    assert start.reason is CirculationReason.COOLED
    # Off-peak threshold 35: 39 does not start.
    assert ctrl.decide(_inputs(NOON, return_c=39.0)).pump_on is None
    # Stop at outlet 50 - 4 = 46.
    stop = ctrl.decide(_inputs(pump_on=True, pump_minutes=1.0, return_c=46.0))
    assert stop.pump_on is False
    assert stop.reason is CirculationReason.HOT


def test_max_run_and_min_pause() -> None:
    ctrl = CirculationController(CirculationConfig())
    assert ctrl.decide(
        _inputs(pump_on=True, pump_minutes=3.0, return_c=40.0)
    ).reason is (CirculationReason.MAX_RUN)
    assert ctrl.decide(_inputs(pump_minutes=4.0, return_c=30.0)).pump_on is None


def test_draw_starts_pulse_off_peak() -> None:
    ctrl = CirculationController(CirculationConfig())
    decision = ctrl.decide(_inputs(NOON, return_c=39.0, draw=True))
    assert decision.pump_on is True
    assert decision.reason is CirculationReason.DRAW


def test_gates_and_blocked_heater() -> None:
    ctrl = CirculationController(CirculationConfig())
    assert ctrl.decide(_inputs(people_gates_open=False, return_c=30.0)).pump_on is False
    assert ctrl.decide(_inputs(heater_ok=False, return_c=30.0)).reason is (
        CirculationReason.BLOCKED
    )
    overrides = CirculationController(CirculationConfig(draw_overrides_gates=True))
    assert (
        overrides.decide(
            _inputs(people_gates_open=False, return_c=40.0, draw=True)
        ).pump_on
        is True
    )


def test_fallback_interval_without_return() -> None:
    ctrl = CirculationController(CirculationConfig())
    assert ctrl.decide(_inputs(return_c=None, pump_minutes=15.0)).pump_on is True
    assert (
        ctrl.decide(_inputs(return_c=None, pump_on=True, pump_minutes=2.0)).pump_on
        is False
    )


def test_continuous_run_and_release() -> None:
    ctrl = CirculationController(CirculationConfig(release_enabled=True))
    first = ctrl.decide(_inputs(continuous=True, return_c=58.5))
    assert first.pump_on is True
    assert not first.release
    later = ctrl.decide(
        _inputs(MORNING + timedelta(minutes=30), continuous=True, return_c=58.5)
    )
    assert later.release

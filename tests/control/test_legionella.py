from datetime import datetime, time, timedelta

from heating_controller.control.legionella import (
    LegionellaConfig,
    LegionellaController,
    LegionellaFailure,
    LegionellaInputs,
    LegionellaState,
)

TUESDAY = datetime(2026, 11, 10, 13, 0)


def _inputs(now: datetime, **overrides) -> LegionellaInputs:
    values = {
        "now": now,
        "outlet_c": 45.0,
        "return_c": 40.0,
        "heater_off": False,
        "forecast": 5.0,
        "target_c": 46.0,
    }
    values.update(overrides)
    return LegionellaInputs(**values)


def _controller(**overrides) -> LegionellaController:
    return LegionellaController(LegionellaConfig(**overrides))


def test_full_cycle() -> None:
    ctrl = _controller()
    ctrl.step(_inputs(TUESDAY))
    assert ctrl.active
    assert ctrl.cycle.state is LegionellaState.WAITING

    ctrl.step(_inputs(TUESDAY + timedelta(minutes=10), target_c=60.0))
    assert ctrl.cycle.state is LegionellaState.RUNNING
    assert ctrl.wants_circulation

    t = TUESDAY + timedelta(minutes=60)
    ctrl.step(_inputs(t, target_c=60.0, outlet_c=60.5, return_c=58.5))
    ctrl.step(
        _inputs(t + timedelta(minutes=29), target_c=60.0, outlet_c=60.5, return_c=58.5)
    )
    assert ctrl.cycle.state is LegionellaState.RUNNING
    ctrl.step(
        _inputs(t + timedelta(minutes=30), target_c=60.0, outlet_c=60.5, return_c=58.5)
    )
    assert ctrl.cycle.state is LegionellaState.DONE
    assert not ctrl.active

    # Same day: not again.
    ctrl.step(_inputs(TUESDAY.replace(hour=15)))
    assert ctrl.cycle.state is LegionellaState.DONE


def test_return_drop_restarts_hold() -> None:
    ctrl = _controller()
    ctrl.step(_inputs(TUESDAY))
    ctrl.step(_inputs(TUESDAY, target_c=60.0))
    ctrl.step(_inputs(TUESDAY + timedelta(minutes=10), target_c=60.0, return_c=58.5))
    ctrl.step(_inputs(TUESDAY + timedelta(minutes=20), target_c=60.0, return_c=57.0))
    ctrl.step(_inputs(TUESDAY + timedelta(minutes=45), target_c=60.0, return_c=58.5))
    assert not ctrl.cycle.return_ok


def test_forecast_gate_until_fallback() -> None:
    ctrl = _controller()
    ctrl.step(_inputs(TUESDAY, forecast=0.0))
    assert ctrl.cycle.state is LegionellaState.IDLE
    ctrl.step(_inputs(TUESDAY.replace(hour=15), forecast=0.0))
    assert ctrl.cycle.state is LegionellaState.WAITING


def test_not_on_other_days_or_when_hot() -> None:
    ctrl = _controller()
    ctrl.step(_inputs(TUESDAY + timedelta(days=1)))
    assert ctrl.cycle.state is LegionellaState.IDLE
    ctrl.step(_inputs(TUESDAY, outlet_c=58.0))
    assert ctrl.cycle.state is LegionellaState.IDLE


def test_fails_when_never_started() -> None:
    ctrl = _controller(fallback=time(15, 0), retry_window_min=120)
    ctrl.step(
        _inputs(
            TUESDAY.replace(hour=17, minute=6),
            forecast=0.0,
            heater_off=False,
            outlet_c=45.0,
        )
    )
    # 17:06 is past the fallback window (15:00 + 120 min + 5): too late to
    # start, so it counts as failed.
    assert ctrl.cycle.state is LegionellaState.FAILED
    assert ctrl.cycle.failure is LegionellaFailure.NOT_STARTED


def test_verification_timeout_fails() -> None:
    ctrl = _controller(verify_timeout_min=240)
    ctrl.step(_inputs(TUESDAY))
    ctrl.step(_inputs(TUESDAY, target_c=60.0))
    ctrl.step(_inputs(TUESDAY + timedelta(minutes=240), target_c=60.0, outlet_c=55.0))
    assert ctrl.failed
    assert ctrl.cycle.failure is LegionellaFailure.OUTLET


def test_restore_keeps_running_cycle() -> None:
    ctrl = _controller()
    ctrl.step(_inputs(TUESDAY))
    ctrl.step(_inputs(TUESDAY, target_c=60.0))
    restored = _controller()
    restored.restore(ctrl.export())
    assert restored.cycle.state is LegionellaState.RUNNING
    assert restored.cycle.started == TUESDAY


def test_start_by_hand() -> None:
    ctrl = _controller()
    ctrl.start_now()
    ctrl.step(_inputs(TUESDAY + timedelta(days=2), target_c=60.0))
    assert ctrl.cycle.state is LegionellaState.RUNNING

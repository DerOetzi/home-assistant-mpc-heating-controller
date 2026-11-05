from datetime import datetime, timedelta

from heating_controller.control.hot_water import (
    ChargeDetector,
    HotWaterConfig,
    HotWaterController,
    HotWaterInputs,
    HotWaterMode,
    HotWaterPhase,
    ManualMode,
)
from heating_controller.control.write_budget import WritePriority

DAY = datetime(2026, 11, 5, 11, 0)


def _inputs(now: datetime = DAY, **overrides) -> HotWaterInputs:
    values = {
        "now": now,
        "grid_export_w": 0.0,
        "battery_soc": 80.0,
        "battery_discharge_w": 0.0,
        "outdoor_c": 8.0,
        "tank_c": 45.0,
        "current_target_c": 46.0,
        "boost_on": False,
        "legionella_active": False,
    }
    values.update(overrides)
    return HotWaterInputs(**values)


def test_normal_target_without_surplus() -> None:
    decision = HotWaterController(HotWaterConfig()).decide(_inputs())
    assert decision.phase is HotWaterPhase.NORMAL
    assert decision.target_c == 46.0
    assert decision.write is None


def test_night_and_charge_window() -> None:
    controller = HotWaterController(HotWaterConfig())
    night = controller.decide(_inputs(DAY.replace(hour=22)))
    assert night.phase is HotWaterPhase.NIGHT
    assert night.write == 38.0
    assert night.priority == WritePriority.REQUIRED

    # 5 Nov 13:00: tank 43, target 46 -> forced to max(53, 43 + 5 + 1).
    window = controller.decide(
        _inputs(DAY.replace(hour=13), tank_c=43.0, current_target_c=46.0)
    )
    assert window.phase is HotWaterPhase.CHARGE_WINDOW
    assert window.write == 53.0
    # Tank 50: target at least 56 so the heater starts at once.
    hot = controller.decide(
        _inputs(DAY.replace(hour=13), tank_c=50.0, current_target_c=46.0)
    )
    assert hot.write == 56.0
    # Once at or above the phase's own target it stays, no climbing.
    kept = controller.decide(
        _inputs(DAY.replace(hour=14), tank_c=54.0, current_target_c=56.0)
    )
    assert kept.write is None


def test_summer_surplus_and_release() -> None:
    controller = HotWaterController(HotWaterConfig(min_hold_s=0))
    surplus = controller.decide(
        _inputs(grid_export_w=3500.0, outdoor_c=25.0, tank_c=45.0)
    )
    assert surplus.mode is HotWaterMode.SURPLUS
    assert surplus.target_c == 57.0
    assert surplus.priority == WritePriority.NORMAL

    # Own load lowers export to 1500: still above release, stays.
    held = controller.decide(
        _inputs(DAY + timedelta(minutes=1), grid_export_w=1500.0, outdoor_c=25.0)
    )
    assert held.mode is HotWaterMode.SURPLUS
    released = controller.decide(
        _inputs(DAY + timedelta(minutes=2), grid_export_w=500.0, outdoor_c=25.0)
    )
    assert released.mode is HotWaterMode.NORMAL


def test_shoulder_season_needs_stable_upper_export() -> None:
    controller = HotWaterController(HotWaterConfig(min_hold_s=0))
    first = controller.decide(_inputs(grid_export_w=6000.0, outdoor_c=15.0))
    assert first.mode is HotWaterMode.NORMAL
    later = controller.decide(
        _inputs(DAY + timedelta(minutes=5), grid_export_w=6000.0, outdoor_c=15.0)
    )
    assert later.mode is HotWaterMode.SURPLUS


def test_gates_block_surplus() -> None:
    cfg = HotWaterConfig(min_hold_s=0)
    hot = {"grid_export_w": 3500.0, "outdoor_c": 25.0}
    assert (
        HotWaterController(cfg).decide(_inputs(battery_soc=40.0, **hot)).mode
        is HotWaterMode.NORMAL
    )
    assert (
        HotWaterController(cfg).decide(_inputs(tank_c=39.0, **hot)).mode
        is HotWaterMode.NORMAL
    )
    assert (
        HotWaterController(cfg).decide(_inputs(battery_discharge_w=800.0, **hot)).mode
        is HotWaterMode.NORMAL
    )
    # Without the battery entities the gates are off.
    no_battery = HotWaterConfig(
        min_hold_s=0, battery_soc_min=None, discharge_max_w=None
    )
    assert (
        HotWaterController(no_battery)
        .decide(_inputs(battery_soc=None, battery_discharge_w=None, **hot))
        .mode
        is HotWaterMode.SURPLUS
    )


def test_hold_time_between_mode_changes() -> None:
    controller = HotWaterController(HotWaterConfig(min_hold_s=900))
    controller.mode_changed_ts = DAY.timestamp()
    decision = controller.decide(
        _inputs(DAY + timedelta(minutes=5), grid_export_w=3500.0, outdoor_c=25.0)
    )
    assert decision.mode is HotWaterMode.NORMAL


def test_manual_mode_holds_then_returns_to_auto() -> None:
    controller = HotWaterController(HotWaterConfig(manual_hold_s=3600))
    controller.set_manual(ManualMode.SURPLUS, DAY.timestamp())
    assert controller.decide(_inputs()).mode is HotWaterMode.SURPLUS
    controller.decide(_inputs(DAY + timedelta(minutes=61)))
    assert controller.manual is ManualMode.AUTO


def test_boost_and_legionella_win() -> None:
    controller = HotWaterController(HotWaterConfig())
    boost = controller.decide(_inputs(boost_on=True, tank_c=45.0))
    assert boost.phase is HotWaterPhase.BOOST
    assert boost.write == 60.0
    legionella = controller.decide(_inputs(boost_on=True, legionella_active=True))
    assert legionella.phase is HotWaterPhase.LEGIONELLA


def test_boost_expires() -> None:
    controller = HotWaterController(HotWaterConfig(boost_max_s=3600))
    assert not controller.boost_expired(True, 0.0)
    assert controller.boost_expired(True, 3600.0)
    assert not controller.boost_expired(False, 3700.0)


def test_charge_detector_announce_and_rise() -> None:
    detector = ChargeDetector(reheat_hysteresis_k=5.0)
    detector.update(43.0, 0.0)
    assert not detector.charging
    # Target 53 above 43 + 5: a charge follows.
    detector.announce(53.0, 43.0, 60.0)
    detector.update(43.0, 60.0)
    assert detector.charging
    # Target 46 at tank 43 is no reheat: no announcement.
    quiet = ChargeDetector(reheat_hysteresis_k=5.0)
    quiet.announce(46.0, 43.0, 0.0)
    quiet.update(43.0, 0.0)
    assert not quiet.charging

    detector.update(44.0, 600.0)
    detector.update(48.0, 1200.0)
    assert detector.charging
    detector.update(54.0, 2400.0)
    assert detector.charging
    detector.update(54.0, 2400.0 + 15 * 60)
    assert not detector.charging

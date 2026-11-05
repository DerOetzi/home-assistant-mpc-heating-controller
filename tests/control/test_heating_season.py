from heating_controller.control.heating_season import HeatingSeason

H = 3600.0
T0 = 1_800_000_000.0


def _run(season: HeatingSeason, start: float, hours: float, demand: bool) -> float:
    """Feed one value per minute; returns the end time."""
    ts = start
    for _ in range(int(hours * 60)):
        season.update(demand, ts, hold_threshold_h=2.0)
        ts += 60
    return ts


def test_sums_demand_of_the_last_24_hours() -> None:
    season = HeatingSeason()
    ts = _run(season, T0, 3, True)
    ts = _run(season, ts, 2, False)

    assert season.hours(ts) == 3.0
    # The demand ended 2 h before ts; 21 h later two of its hours are out.
    assert season.hours(ts + 21 * H) == 1.0


def test_unknown_demand_counts_as_none() -> None:
    season = HeatingSeason()
    ts = _run(season, T0, 1, True)
    season.update(None, ts, hold_threshold_h=2.0)

    assert season.hours(ts + H) == 1.0


def test_stamps_last_demand_while_above_hold_threshold() -> None:
    season = HeatingSeason()
    ts = _run(season, T0, 2, True)
    assert season.last_demand_ts is None

    ts = _run(season, ts, 1, True)
    assert season.last_demand_ts == ts - 60


def test_recommends_switching_on_only_while_heating_is_off() -> None:
    season = HeatingSeason()
    ts = _run(season, T0, 7, True)

    assert season.switch_on_recommended(ts, 6.0, heating_on=False)
    assert not season.switch_on_recommended(ts, 6.0, heating_on=True)
    assert not season.switch_on_recommended(ts, 8.0, heating_on=False)


def test_recommends_switching_off_after_quiet_period() -> None:
    season = HeatingSeason()
    ts = _run(season, T0, 3, True)
    ts = _run(season, ts, 1, False)
    # Still above the hold threshold over the last 24 h, so stamped on.
    last = season.last_demand_ts
    assert last == ts - 60

    assert not season.switch_off_recommended(last + 47 * H, 48.0, heating_on=True)
    assert season.switch_off_recommended(last + 48 * H, 48.0, heating_on=True)
    assert not season.switch_off_recommended(last + 48 * H, 48.0, heating_on=False)


def test_restart_closes_running_period_where_it_was_last_seen() -> None:
    season = HeatingSeason()
    ts = _run(season, T0, 2, True)

    restored = HeatingSeason()
    restored.restore(season.export())

    # Down for an hour, then the demand is on again.
    restored.update(True, ts + H, hold_threshold_h=2.0)
    assert round(restored.hours(ts + 2 * H), 2) == round(2 - 1 / 60 + 1, 2)

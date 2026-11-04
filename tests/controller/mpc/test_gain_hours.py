"""Hourly gain learning: day profile and sun from synthetic rooms."""

import math
import random

import pytest
from heating_controller.const import Construction
from heating_controller.controller.mpc.learner import RoomMpcModelLearner
from heating_controller.controller.mpc.models.capacity import ThermalCapacityModel
from heating_controller.controller.mpc.models.gains import (
    HourOutcome,
    RoomGainModel,
    profile_index,
)
from heating_controller.controller.mpc.models.loss import RoomLossModel
from heating_controller.controller.mpc.types import RoomThermalConfig
from learner_helpers import make_input

DAY_S = 86400
STEP_S = 300
# 2026-11-02 00:00 UTC, a Monday.
START_TS = 1_793_577_600.0
ROOM_C = 21.0
OUTDOOR_C = 5.0


class FakeSun:
    """South facade: a bell from 8 to 16 UTC, scaled per day by cloud."""

    def __init__(self, clouds: dict[int, float] | None = None) -> None:
        self.clouds = clouds or {}

    def clear_sky(self, ts: float) -> float:
        hour = (ts % DAY_S) / 3600
        if not 8 <= hour <= 16:
            return 0.0
        return 600.0 * math.sin(math.pi * (hour - 8) / 8)

    def value_at(self, surface, ts):
        if surface != "south":
            return None
        day = int((ts - START_TS) // DAY_S)
        return self.clear_sky(ts) * self.clouds.get(day, 1.0)

    def filtered_at(self, surface, ts, tau_s):
        return self.value_at(surface, ts)


def _setup(window_surfaces=("south",), load_w=1000.0):
    thermal_config = RoomThermalConfig(
        room_heat_load_w=load_w, room_area_m2=20, construction=Construction.MEDIUM
    )
    loss_model = RoomLossModel(thermal_config)
    capacity_model = ThermalCapacityModel(thermal_config)
    gains = RoomGainModel(
        load_w, loss_model.base_ua_w_per_k, window_surfaces=window_surfaces
    )
    learner = RoomMpcModelLearner(loss_model, capacity_model, gain_model=gains)
    learner.enable(START_TS)
    return learner, loss_model, gains


def _feed(learner, loss_model, days, gain_w, sun=None, seed=1, sensor_noise_c=0.05):
    """Hold the room: the radiators give off the loss minus the true gain."""
    rng = random.Random(seed)
    learner.set_gain_context(
        lambda ts: profile_index(int((ts % DAY_S) // 3600), True),
        lambda: sun,
    )
    loss_w = loss_model.effective_ua_w_per_k * (ROOM_C - OUTDOOR_C)
    for index in range(int(days * DAY_S / STEP_S)):
        ts = START_TS + index * STEP_S
        power_w = max(0.0, loss_w - gain_w(ts))
        room_c = round(ROOM_C + rng.gauss(0, sensor_noise_c), 1)
        learner.append_history(
            make_input(ts, room_temp_c=room_c, outdoor_temp_c=OUTDOOR_C),
            applied_heating_power_w=power_w,
            radiator_power_w=power_w,
        )


def test_learns_an_evening_block_and_the_south_sun():
    learner, loss_model, gains = _setup()
    rng = random.Random(7)
    clouds = {day: rng.uniform(0.0, 1.0) for day in range(14)}
    sun = FakeSun(clouds)

    def true_gain(ts):
        hour = (ts % DAY_S) / 3600
        evening = 100.0 if 18 <= hour < 21 else 0.0
        return evening + 400.0 * sun.value_at("south", ts) / 1000

    _feed(learner, loss_model, 14, true_gain, sun=sun)

    profile = gains.profile_w()
    assert profile[6] == pytest.approx(100, abs=20)
    for index in (0, 1, 7):
        assert profile[index] == pytest.approx(0, abs=20)
    coefficients = gains.sun_coefficients_w()
    sun_total = coefficients["window_south"]
    assert sun_total == pytest.approx(400, rel=0.25)
    assert gains.profile_determined(6)
    assert gains.last_hour_outcome == HourOutcome.LEARNED
    assert gains.hour_samples > 300


def test_clear_days_get_the_noon_total_right():
    learner, loss_model, gains = _setup()
    sun = FakeSun()

    def true_gain(ts):
        return 400.0 * sun.value_at("south", ts) / 1000

    _feed(learner, loss_model, 14, true_gain, sun=sun)

    noon_ts = START_TS + 14 * DAY_S + 12 * 3600
    breakdown_raw = gains.profile_w()[4] + sum(
        value * coefficient
        for value, coefficient in zip(
            gains.sun_regressors(sun, noon_ts),
            gains.sun_coefficients_w().values(),
            strict=True,
        )
    )
    assert breakdown_raw == pytest.approx(240, rel=0.15)


def test_bedroom_empty_by_day_learns_a_negative_day_profile():
    learner, loss_model, gains = _setup(window_surfaces=())
    # Night regression: 130 W of sleepers counted as steady gain.
    for delta_c in (10.0, 14.0, 18.0):
        gains.add_night_point(
            delta_c, loss_model.effective_ua_w_per_k * delta_c - 130, 6.0, 31.25
        )
    assert gains.steady_gain_w == pytest.approx(130, abs=10)

    def true_gain(ts):
        hour = (ts % DAY_S) / 3600
        return 130.0 if hour < 6 or hour >= 21 else 0.0

    _feed(learner, loss_model, 14, true_gain)

    profile = gains.profile_w()
    for index in (2, 3, 4, 5):
        assert profile[index] == pytest.approx(-130, abs=30)
    assert profile[0] == pytest.approx(0, abs=30)


def test_window_open_hour_is_not_learned():
    learner, loss_model, gains = _setup(window_surfaces=())
    learner.set_window_open(True, START_TS)
    _feed(learner, loss_model, 0.1, lambda ts: 0.0)
    assert gains.last_hour_outcome == HourOutcome.WINDOW_OPEN
    assert gains.hour_samples == 0


def test_daytime_hour_without_sun_is_not_learned():
    learner, loss_model, gains = _setup()
    learner.set_sun_condition(lambda ts: False)
    _feed(learner, loss_model, 0.1, lambda ts: 0.0, sun=None)
    assert gains.last_hour_outcome == HourOutcome.SUN_MISSING
    assert gains.hour_samples == 0


def test_dark_hour_without_sun_series_learns():
    learner, loss_model, gains = _setup()
    learner.set_sun_condition(lambda ts: True)
    _feed(learner, loss_model, 0.1, lambda ts: 0.0, sun=None)
    assert gains.last_hour_outcome == HourOutcome.LEARNED


def test_hour_started_mid_way_is_too_short():
    learner, _, gains = _setup(window_surfaces=())
    learner.set_gain_context(lambda ts: 0, lambda: None)
    for minute in range(30, 75, 5):
        ts = START_TS + minute * 60
        learner.append_history(
            make_input(ts, room_temp_c=ROOM_C, outdoor_temp_c=OUTDOOR_C),
            applied_heating_power_w=500,
            radiator_power_w=500,
        )
    assert gains.last_hour_outcome == HourOutcome.TOO_SHORT


def test_paused_learning_blocks_the_hour():
    learner, loss_model, gains = _setup(window_surfaces=())
    learner.set_learning_paused(True, START_TS)
    _feed(learner, loss_model, 0.1, lambda ts: 0.0)
    assert gains.last_hour_outcome == HourOutcome.PAUSED


def test_hourly_state_survives_restore_only_with_the_same_surfaces():
    learner, loss_model, gains = _setup()
    sun = FakeSun()
    _feed(learner, loss_model, 1, lambda ts: 50.0, sun=sun)
    saved = gains.export()

    same = RoomGainModel(1000, loss_model.base_ua_w_per_k, window_surfaces=("south",))
    same.restore(saved)
    assert same.hour_samples == gains.hour_samples

    other = RoomGainModel(1000, loss_model.base_ua_w_per_k, window_surfaces=("west",))
    other.restore(saved)
    assert other.hour_samples == 0

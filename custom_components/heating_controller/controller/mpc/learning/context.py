"""What the learner's evaluations share: samples, models and the sun."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from ..models.capacity import ThermalCapacityModel
from ..models.gains import RoomGainModel, SunSource, profile_index
from ..models.loss import RoomLossModel
from .history import LearnerHistoryEntry

# Sun on the room's windows during the stretch or shortly before it warms the
# room by itself; the heat stored in floor and furniture keeps coming out after
# the sun has gone.
JUMP_SUN_LIMIT_W_M2 = 50.0
JUMP_SUN_LOOKBACK_S = 2 * 3600
JUMP_SUN_SAMPLE_S = 1800


def default_profile_index(timestamp: float) -> int:
    # UTC and Monday to Friday; the coordinator supplies local time and the
    # workday entity.
    moment = time.gmtime(timestamp)
    return profile_index(moment.tm_hour, moment.tm_wday < 5)


def default_local_day(timestamp: float) -> int:
    # UTC; the coordinator supplies HA's time zone.
    return datetime.fromtimestamp(timestamp, UTC).toordinal()


class LearningContext:
    def __init__(
        self,
        loss_model: RoomLossModel,
        capacity_model: ThermalCapacityModel,
        gain_model: RoomGainModel | None,
    ) -> None:
        self.loss_model = loss_model
        self.capacity_model = capacity_model
        self.gain_model = gain_model
        self.history: list[LearnerHistoryEntry] = []
        self.profile_index_at: Callable[[float], int] = default_profile_index
        self.local_day_at: Callable[[float], int] = default_local_day
        self.sun_source: Callable[[], SunSource | None] = lambda: None
        self.sun_condition: Callable[[float], bool] = lambda _ts: True
        # No stretch starts before these: a window just closed, inputs just
        # came back, or the configuration changed.
        self.ua_blocked_until_ts = 0.0
        self.capacity_blocked_until_ts = 0.0
        # Short heating interruptions, booked as no heating.
        self.ua_balance_bridged: list[tuple[float, float]] = []

    def balance_heating_power_w(
        self, entry: LearnerHistoryEntry, next_entry: LearnerHistoryEntry
    ) -> float:
        # The observer's estimate at the next sample already followed the lag
        # over the interval, so the mean of both ends is close to what the
        # radiators gave off in between.
        return (
            self.balance_point_power_w(entry) + self.balance_point_power_w(next_entry)
        ) / 2

    def balance_point_power_w(self, entry: LearnerHistoryEntry) -> float:
        # During a domestic hot-water charge the radiators get no water at
        # all, however slowly the observer lets them cool.
        if entry.hot_water_charge:
            return 0.0
        # While heating was unavailable the TRVs were closed, whatever the
        # controller kept requesting.
        if any(
            start_ts <= entry.timestamp < end_ts
            for start_ts, end_ts in self.ua_balance_bridged
        ):
            return 0.0
        return entry.radiator_power_w

    def sun_free(self, start_ts: float, end_ts: float) -> bool:
        """No sun worth mentioning on the room's windows during the stretch and
        the time before it."""
        if self.gain_model is None or not self.gain_model.window_surfaces:
            return True
        sun = self.sun_source()

        def fallback() -> bool:
            return self.sun_condition(start_ts) and self.sun_condition(end_ts)

        if sun is None:
            return fallback()
        sample_ts = [start_ts - JUMP_SUN_LOOKBACK_S]
        while sample_ts[-1] + JUMP_SUN_SAMPLE_S < end_ts:
            sample_ts.append(sample_ts[-1] + JUMP_SUN_SAMPLE_S)
        sample_ts.append(end_ts)
        for surface in self.gain_model.window_surfaces:
            for ts in sample_ts:
                value = sun.value_at(surface, ts)
                if value is None:
                    return fallback()
                if value > JUMP_SUN_LIMIT_W_M2:
                    return False
        return True

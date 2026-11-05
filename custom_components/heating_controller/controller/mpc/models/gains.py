"""Heat the radiators do not supply: steady gain, daily profile, sun.

Night level: every usable heat-balance stretch is a point
(mean room-outdoor difference, net radiator heat). Over several nights with
different temperature differences a regression separates the heat-loss
factor from a steady gain G -- one night alone cannot, at an almost constant
difference both explain the same heat.

The heat-loss factor itself keeps learning from the single-night balance;
the regression only supplies G, once it is determined.

Hourly level: every usable hour gives the heat the model with UA and G does
not explain. A second regression splits it into a profile over the day
(8 blocks of 3 hours, workdays and other days apart) and the sun on the
room's surfaces: windows with the hour's irradiance, outer walls and roof
surfaces through a lag. The profile is relative to the night: night blocks
come out near zero, a bedroom empty by day below zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..rls import RecursiveLeastSquares

# About 20 nights of memory.
NIGHT_FORGETTING = 0.95
# |G| and every profile value stay within this share of the design heat load.
GAIN_LIMIT_SHARE = 0.5
# A gain counts once its standard deviation is below this share of the
# design heat load.
GAIN_DETERMINED_SHARE = 0.1
# A profile block counts only with hours from this many days: one evening
# with a hot shower must not make the block's gain look known (Duschbad
# 5 Nov: 191 W from one or two hours, the room fell below setpoint).
MIN_PROFILE_DAYS = 3
# Noise floor of the night regression, as share of the design heat load: a
# few nearly exact points must not make G look known.
NIGHT_NOISE_FLOOR_SHARE = 0.02
MIN_NIGHT_POINTS = 3
# The regression's heat-loss factor stays in the learner's limits.
MIN_UA_FACTOR = 0.15
MAX_UA_FACTOR = 2.0

GAINS_STORE_VERSION = 1

# About 10 days of memory (Thermocast).
HOURLY_FORGETTING = 0.996
BLOCK_HOURS = 3
BLOCKS_PER_DAY = 24 // BLOCK_HOURS
PROFILE_SIZE = 2 * BLOCKS_PER_DAY
# Walls and roof surfaces pass the sun on with this time constant.
OPAQUE_LAG_S = 3 * 3600
HALF_HOUR_S = 1800
# Irradiance enters in kW/m^2: a sun coefficient is the heat in W at
# 1000 W/m^2.
IRRADIANCE_SCALE_W_M2 = 1000.0
# A sun coefficient may reach this share of the design heat load.
SUN_LIMIT_SHARE = 2.0
# A sun coefficient counts once its standard deviation is below this share
# of its value.
SUN_DETERMINED_RELATIVE = 0.5
HOURLY_NOISE_FLOOR_SHARE = 0.02


def profile_index(local_hour: int, workday: bool) -> int:
    """Index of the profile value: 0-7 workday blocks, 8-15 other days."""
    return local_hour // BLOCK_HOURS + (0 if workday else BLOCKS_PER_DAY)


class SunSource(Protocol):
    def value_at(self, surface: str, ts: float) -> float | None: ...

    def filtered_at(self, surface: str, ts: float, tau_s: float) -> float | None: ...


@dataclass(frozen=True)
class GainBreakdown:
    """The gain the model counts at a moment, and its parts."""

    total_w: float
    steady_w: float
    profile_w: float
    sun_w: float
    # All terms, determined or not -- diagnostics only.
    ungated_w: float


class HourOutcome(StrEnum):
    LEARNED = "learned"
    WINDOW_OPEN = "window_open"
    ROOM_SENSOR_FALLBACK = "room_sensor_fallback"
    VALVE_POSITION_MISSING = "valve_position_missing"
    TRV_ADAPTING = "trv_adapting"
    RADIATOR_NOT_HEATING = "radiator_not_heating"
    VALVE_SATURATED = "valve_saturated"
    PAUSED = "paused"
    SUPPRESSED = "suppressed"
    GAP = "gap"
    TOO_SHORT = "too_short"
    SUN_MISSING = "sun_missing"


class RoomGainModel:
    def __init__(
        self,
        room_heat_load_w: float,
        base_ua_w_per_k: float,
        window_surfaces: Sequence[str] = (),
        opaque_surfaces: Sequence[str] = (),
    ) -> None:
        self._room_heat_load_w = max(1.0, room_heat_load_w)
        self._base_ua_w_per_k = max(0.1, base_ua_w_per_k)
        self.window_surfaces = tuple(window_surfaces)
        self.opaque_surfaces = tuple(opaque_surfaces)
        limit_w = GAIN_LIMIT_SHARE * self._room_heat_load_w
        self._night = RecursiveLeastSquares(
            theta0=[self._base_ua_w_per_k, 0.0],
            p0_diag=[self._base_ua_w_per_k**2, limit_w**2],
            forgetting=NIGHT_FORGETTING,
            lower_bounds=[MIN_UA_FACTOR * self._base_ua_w_per_k, -limit_w],
            upper_bounds=[MAX_UA_FACTOR * self._base_ua_w_per_k, limit_w],
            noise_floor_variance=(NIGHT_NOISE_FLOOR_SHARE * self._room_heat_load_w)
            ** 2,
        )
        self._night_started = False

        sun_size = len(self.window_surfaces) + len(self.opaque_surfaces)
        sun_limit_w = SUN_LIMIT_SHARE * self._room_heat_load_w
        p0 = [limit_w**2] * PROFILE_SIZE + [sun_limit_w**2] * sun_size
        self._hourly = RecursiveLeastSquares(
            theta0=[0.0] * (PROFILE_SIZE + sun_size),
            p0_diag=p0,
            forgetting=HOURLY_FORGETTING,
            lower_bounds=[-limit_w] * PROFILE_SIZE + [0.0] * sun_size,
            upper_bounds=[limit_w] * PROFILE_SIZE + [sun_limit_w] * sun_size,
            p_max_diag=p0,
            noise_floor_variance=(HOURLY_NOISE_FLOOR_SHARE * self._room_heat_load_w)
            ** 2,
        )
        # Distinct days (ordinals) with an hour in each profile block; the
        # most recent MIN_PROFILE_DAYS are enough.
        self._profile_days: list[list[int]] = [[] for _ in range(PROFILE_SIZE)]
        self.last_hour_outcome: HourOutcome | None = None
        self.last_hour_ts: float | None = None
        self._changed = False

    @property
    def sun_terms(self) -> list[str]:
        """Names of the sun parameters, in regressor order."""
        return [f"window_{surface}" for surface in self.window_surfaces] + [
            f"opaque_{surface}" for surface in self.opaque_surfaces
        ]

    @property
    def _signature(self) -> list[str]:
        return [f"profile_{index}" for index in range(PROFILE_SIZE)] + self.sun_terms

    # --- night level -------------------------------------------------------

    def add_night_point(
        self,
        mean_temperature_difference_c: float,
        net_heating_w: float,
        hours: float,
        current_ua_w_per_k: float,
    ) -> None:
        """One balanced stretch: net heat = UA * difference - G."""
        if not self._night_started:
            # Start the regression from the factor learned so far.
            self._night.theta[0] = current_ua_w_per_k
            self._night_started = True
        self._night.update(
            [mean_temperature_difference_c, -1.0], net_heating_w, weight=max(hours, 0.1)
        )
        self._changed = True

    @property
    def night_points(self) -> int:
        return self._night.samples

    @property
    def learned_gain_w(self) -> float | None:
        return self._night.theta[1] if self._night.samples else None

    @property
    def gain_sigma_w(self) -> float | None:
        return self._night.std(1) if self._night.samples else None

    @property
    def gain_determined(self) -> bool:
        return (
            self._night.samples >= MIN_NIGHT_POINTS
            and self._night.std(1) < GAIN_DETERMINED_SHARE * self._room_heat_load_w
        )

    @property
    def steady_gain_w(self) -> float:
        """G where determined, otherwise 0 -- what the model may use."""
        return self._night.theta[1] if self.gain_determined else 0.0

    @property
    def regression_ua_factor(self) -> float | None:
        if not self._night.samples:
            return None
        return self._night.theta[0] / self._base_ua_w_per_k

    # --- hourly level ------------------------------------------------------

    def sun_regressors(
        self, sun: SunSource | None, mid_ts: float
    ) -> list[float] | None:
        """Sun inputs of the hour around mid_ts; None if one is missing."""
        if not self.sun_terms:
            return []
        if sun is None:
            return None
        values: list[float] = []
        for surface in self.window_surfaces:
            # Mean over the hour: a separate term one hour later was almost
            # the same input (the sun changes slowly) and made the pair
            # unstable; the delayed share goes to the wall term.
            points = [
                sun.value_at(surface, mid_ts + offset_s)
                for offset_s in (-HALF_HOUR_S, 0.0, HALF_HOUR_S)
            ]
            if any(value is None for value in points):
                return None
            values.append(
                (points[0] + 2 * points[1] + points[2]) / 4 / IRRADIANCE_SCALE_W_M2
            )
        for surface in self.opaque_surfaces:
            value = sun.filtered_at(surface, mid_ts, OPAQUE_LAG_S)
            if value is None:
                return None
            values.append(value / IRRADIANCE_SCALE_W_M2)
        return values

    def add_hour(
        self, profile: int, sun_values: list[float], unexplained_w: float, day: int
    ) -> None:
        """One balanced hour; day is the local date's ordinal."""
        x = [0.0] * PROFILE_SIZE + list(sun_values)
        x[profile] = 1.0
        self._hourly.update(x, unexplained_w)
        days = self._profile_days[profile]
        if day not in days:
            days.append(day)
            del days[:-MIN_PROFILE_DAYS]
        self._changed = True

    def set_hour_outcome(self, outcome: HourOutcome, hour_ts: float) -> None:
        self.last_hour_outcome = outcome
        self.last_hour_ts = hour_ts

    @property
    def hour_samples(self) -> int:
        return self._hourly.samples

    @property
    def residual_rms_w(self) -> float | None:
        variance = self._hourly.noise_variance
        return variance**0.5 if variance is not None else None

    def profile_days(self, index: int) -> int:
        return len(self._profile_days[index])

    def profile_determined(self, index: int) -> bool:
        return (
            len(self._profile_days[index]) >= MIN_PROFILE_DAYS
            and self._hourly.std(index) < GAIN_DETERMINED_SHARE * self._room_heat_load_w
        )

    def sun_determined(self, term: int) -> bool:
        index = PROFILE_SIZE + term
        value = self._hourly.theta[index]
        return value > 0 and self._hourly.std(index) < SUN_DETERMINED_RELATIVE * value

    def profile_w(self) -> list[float]:
        return list(self._hourly.theta[:PROFILE_SIZE])

    def sun_coefficients_w(self) -> dict[str, float]:
        return dict(zip(self.sun_terms, self._hourly.theta[PROFILE_SIZE:], strict=True))

    def breakdown(self, profile: int, sun_values: list[float] | None) -> GainBreakdown:
        """Gain at a moment; sun_values None (sun missing) counts no sun."""
        steady_w = self.steady_gain_w
        raw_profile_w = self._hourly.theta[profile]
        profile_w = raw_profile_w if self.profile_determined(profile) else 0.0
        sun_w = 0.0
        raw_sun_w = 0.0
        for term, value in enumerate(sun_values or []):
            contribution = self._hourly.theta[PROFILE_SIZE + term] * value
            raw_sun_w += contribution
            if self.sun_determined(term):
                sun_w += contribution
        learned_gain_w = self.learned_gain_w or 0.0
        return GainBreakdown(
            total_w=steady_w + profile_w + sun_w,
            steady_w=steady_w,
            profile_w=profile_w,
            sun_w=sun_w,
            ungated_w=learned_gain_w + raw_profile_w + raw_sun_w,
        )

    # --- persistence -------------------------------------------------------

    def consume_changed(self) -> bool:
        changed = self._changed
        self._changed = False
        return changed

    def reset(self) -> None:
        self._night.reset()
        self._night_started = False
        self._hourly.reset()
        self._profile_days = [[] for _ in range(PROFILE_SIZE)]
        self.last_hour_outcome = None
        self.last_hour_ts = None
        self._changed = True

    def export(self) -> dict:
        return {
            "version": GAINS_STORE_VERSION,
            "night": self._night.to_dict() if self._night_started else None,
            "hourly": {"terms": self._signature, **self._hourly.to_dict()},
            "profile_days": [list(days) for days in self._profile_days],
        }

    def restore(self, data: dict) -> None:
        if data.get("version") != GAINS_STORE_VERSION:
            return
        night = data.get("night")
        if isinstance(night, dict) and self._night.restore(night):
            self._night_started = True
        hourly = data.get("hourly")
        # Other surfaces than when it was saved: the hourly level learns anew.
        if isinstance(hourly, dict) and hourly.get("terms") == self._signature:
            self._hourly.restore(hourly)
            # Stores from before the day count start empty: every block
            # waits for three days again.
            days = data.get("profile_days")
            if isinstance(days, list) and len(days) == PROFILE_SIZE:
                self._profile_days = [
                    [int(day) for day in block][-MIN_PROFILE_DAYS:] for block in days
                ]

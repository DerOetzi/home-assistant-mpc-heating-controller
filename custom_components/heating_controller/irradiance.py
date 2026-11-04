"""Sun on the building's surfaces from Open-Meteo.

Home Assistant's own Open-Meteo integration offers no irradiance, so the hub
asks the forecast API itself: global tilted irradiance per surface, the past
day and two days ahead, once an hour. Rooms use it to learn and plan their
sun gains; a room never calls the API.

Open-Meteo reports an hourly irradiance as the mean of the preceding hour.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import exp
from typing import Any

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import FACADES, RoofOrientation, Surface

_LOGGER = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FETCH_INTERVAL = timedelta(hours=1)
FETCH_TIMEOUT_S = 30
# A series older than this counts as missing: rooms neither learn nor plan
# with sun from it.
MAX_SERIES_AGE_S = 3 * 3600
HOUR_S = 3600

_FACADE_AZIMUTH_DEG = {
    Surface.NORTH: 0.0,
    Surface.EAST: 90.0,
    Surface.SOUTH: 180.0,
    Surface.WEST: 270.0,
}
_ROOF_SURFACES = {
    RoofOrientation.EAST_WEST: (Surface.ROOF_EAST, Surface.ROOF_WEST),
    RoofOrientation.NORTH_SOUTH: (Surface.ROOF_NORTH, Surface.ROOF_SOUTH),
    RoofOrientation.NONE: (),
}
_ROOF_FACADE = {
    Surface.ROOF_NORTH: Surface.NORTH,
    Surface.ROOF_EAST: Surface.EAST,
    Surface.ROOF_SOUTH: Surface.SOUTH,
    Surface.ROOF_WEST: Surface.WEST,
}


@dataclass(frozen=True)
class SurfaceGeometry:
    surface: Surface
    # Compass bearing the surface faces, 0 = north, 90 = east.
    azimuth_deg: float
    # 90 = vertical wall, 0 = flat.
    tilt_deg: float

    @property
    def open_meteo_azimuth_deg(self) -> float:
        """Open-Meteo counts 0 = south, -90 = east, 90 = west."""
        value = (self.azimuth_deg - 180.0) % 360.0
        return value - 360.0 if value > 180.0 else value


def building_surfaces(
    rotation_deg: float, roof: RoofOrientation, roof_tilt_deg: float
) -> list[SurfaceGeometry]:
    """The four facades and the roof surfaces of a rectangular house.

    rotation_deg turns the whole house: the "north" facade faces
    rotation_deg (e.g. -15 for a house turned 15 degrees to the west).
    """
    surfaces = [
        SurfaceGeometry(
            facade, (_FACADE_AZIMUTH_DEG[facade] + rotation_deg) % 360.0, 90.0
        )
        for facade in FACADES
    ]
    for roof_surface in _ROOF_SURFACES[roof]:
        facade = _ROOF_FACADE[roof_surface]
        surfaces.append(
            SurfaceGeometry(
                roof_surface,
                (_FACADE_AZIMUTH_DEG[facade] + rotation_deg) % 360.0,
                roof_tilt_deg,
            )
        )
    return surfaces


@dataclass(frozen=True)
class IrradianceInterval:
    start_ts: float
    end_ts: float
    value_w_m2: float


class SunSeries:
    """Hourly irradiance per surface: hour means, past and forecast."""

    def __init__(
        self,
        intervals: Mapping[Surface, list[IrradianceInterval]],
        fetched_ts: float | None,
    ) -> None:
        self._intervals = {
            surface: sorted(values, key=lambda interval: interval.start_ts)
            for surface, values in intervals.items()
        }
        self.fetched_ts = fetched_ts

    @property
    def surfaces(self) -> list[Surface]:
        return list(self._intervals)

    def fresh(self, now_ts: float) -> bool:
        return (
            self.fetched_ts is not None and now_ts - self.fetched_ts <= MAX_SERIES_AGE_S
        )

    def _covers(self, surface: Surface, ts: float) -> bool:
        intervals = self._intervals.get(surface)
        return bool(intervals) and intervals[0].start_ts <= ts <= intervals[-1].end_ts

    def value_at(self, surface: Surface, ts: float) -> float | None:
        """Irradiance at ts, linear between the hour midpoints."""
        if not self._covers(surface, ts):
            return None
        points = [
            ((interval.start_ts + interval.end_ts) / 2, interval.value_w_m2)
            for interval in self._intervals[surface]
        ]
        if ts <= points[0][0]:
            return points[0][1]
        for (t0, v0), (t1, v1) in pairwise(points):
            if t0 <= ts <= t1:
                return v0 + (v1 - v0) * (ts - t0) / (t1 - t0)
        return points[-1][1]

    def filtered_at(self, surface: Surface, ts: float, tau_s: float) -> float | None:
        """First-order lag of the hour means, what a wall passes on at ts.

        Starts from the first hour of the series (the past day reaches back
        far enough for a lag of a few hours to forget its start).
        """
        if not self._covers(surface, ts):
            return None
        intervals = self._intervals[surface]
        value = intervals[0].value_w_m2
        for interval in intervals:
            if interval.start_ts >= ts:
                break
            duration_s = min(interval.end_ts, ts) - interval.start_ts
            decay = exp(-duration_s / tau_s)
            value = value * decay + interval.value_w_m2 * (1 - decay)
        return value

    def forecast(
        self, surface: Surface, from_ts: float, hours: int
    ) -> list[tuple[float, float]]:
        """The hour running at from_ts and the ones after it, hours in all."""
        coming = [
            (interval.start_ts, interval.value_w_m2)
            for interval in self._intervals.get(surface, [])
            if interval.end_ts > from_ts
        ]
        return coming[:hours]


def parse_open_meteo(payload: Mapping[str, Any]) -> list[IrradianceInterval]:
    """Hour means from an Open-Meteo response (times in UTC)."""
    hourly = payload["hourly"]
    times = hourly["time"]
    values = hourly["global_tilted_irradiance"]
    intervals = []
    for time_text, value in zip(times, values, strict=True):
        if value is None:
            continue
        end = datetime.fromisoformat(time_text).replace(tzinfo=UTC)
        end_ts = end.timestamp()
        intervals.append(
            IrradianceInterval(end_ts - HOUR_S, end_ts, max(0.0, float(value)))
        )
    return intervals


def request_params(
    latitude: float, longitude: float, geometry: SurfaceGeometry
) -> dict[str, Any]:
    return {
        "latitude": round(latitude, 4),
        "longitude": round(longitude, 4),
        "hourly": "global_tilted_irradiance",
        "tilt": round(geometry.tilt_deg, 1),
        "azimuth": round(geometry.open_meteo_azimuth_deg, 1),
        "past_days": 1,
        "forecast_days": 2,
        "timezone": "UTC",
    }


class IrradianceProvider:
    """Fetches the series for the configured surfaces once an hour."""

    def __init__(
        self,
        hass: HomeAssistant,
        surfaces: list[SurfaceGeometry],
        on_update: Callable[[], None],
    ) -> None:
        self.hass = hass
        self._surfaces = surfaces
        self._on_update = on_update
        self.series = SunSeries({}, None)
        self.last_error: str | None = None
        self._unsub: list[CALLBACK_TYPE] = []

    @property
    def geometries(self) -> list[SurfaceGeometry]:
        return list(self._surfaces)

    @callback
    def async_start(self) -> None:
        # A random minute within the hour spreads the requests of many
        # installations; the first fetch runs shortly after start.
        self._unsub.append(
            async_call_later(self.hass, random.uniform(5, 60), self._async_scheduled)
        )
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_scheduled, FETCH_INTERVAL)
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []

    async def _async_scheduled(self, _now: Any) -> None:
        await self.async_refresh()

    async def async_refresh(self) -> bool:
        session = async_get_clientsession(self.hass)
        latitude = self.hass.config.latitude
        longitude = self.hass.config.longitude
        intervals: dict[Surface, list[IrradianceInterval]] = {}
        try:
            for geometry in self._surfaces:
                async with asyncio.timeout(FETCH_TIMEOUT_S):
                    response = await session.get(
                        OPEN_METEO_URL,
                        params=request_params(latitude, longitude, geometry),
                    )
                    response.raise_for_status()
                    payload = await response.json()
                intervals[geometry.surface] = parse_open_meteo(payload)
        except (TimeoutError, aiohttp.ClientError, KeyError, ValueError) as err:
            self.last_error = str(err) or type(err).__name__
            _LOGGER.warning("Open-Meteo irradiance fetch failed: %s", self.last_error)
            self._on_update()
            return False
        self.series = SunSeries(intervals, datetime.now(UTC).timestamp())
        self.last_error = None
        self._on_update()
        return True

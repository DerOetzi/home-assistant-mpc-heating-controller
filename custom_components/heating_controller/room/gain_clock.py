"""Calendar and sun for a room's gain model."""

from __future__ import annotations

import time
from datetime import datetime

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..controller.mpc.models.gains import profile_index
from ..irradiance import IrradianceProvider, SunSeries


def _local(timestamp: float) -> datetime:
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp))


def local_day_at(timestamp: float) -> int:
    """Ordinal of the date in HA's time zone."""
    return _local(timestamp).toordinal()


class GainClock:
    """Which hour profile and which sun series a moment of the room has."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # Set by the hub when some room has sun surfaces.
        self.sun_provider: IrradianceProvider | None = None
        self.workday_entity: str | None = None

    def set_sun_provider(
        self, provider: IrradianceProvider, workday_entity: str | None
    ) -> None:
        self.sun_provider = provider
        self.workday_entity = workday_entity

    def fresh_sun_series(self) -> SunSeries | None:
        """The hub's irradiance series, unless it is too old to use."""
        provider = self.sun_provider
        if provider is None or not provider.series.fresh(time.time()):
            return None
        return provider.series

    def is_workday(self, timestamp: float) -> bool:
        """The workday entity's state; without one Monday to Friday."""
        if self.workday_entity:
            state = self._hass.states.get(self.workday_entity)
            if state is not None and state.state in (STATE_ON, STATE_OFF):
                return state.state == STATE_ON
        return _local(timestamp).weekday() < 5

    def profile_index_at(self, timestamp: float) -> int:
        return profile_index(_local(timestamp).hour, self.is_workday(timestamp))

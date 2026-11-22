"""The night: from two hours after sunset until sunrise.

The heat-loss learner balances only inside it (no sun, no day use), the
flow feedback takes its night times inside it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

SUNSET_DELAY = timedelta(hours=2)
SUN_EVENT_CACHE_SIZE = 8


class NightWindow:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._sun_events: dict[tuple[str, date], datetime | None] = {}

    def is_night(self, timestamp: float) -> bool:
        moment = dt_util.utc_from_timestamp(timestamp)
        today = dt_util.as_local(moment).date()

        sunset_today = self._sun_event(SUN_EVENT_SUNSET, today)
        if sunset_today is not None and moment >= sunset_today + SUNSET_DELAY:
            return True

        sunrise_today = self._sun_event(SUN_EVENT_SUNRISE, today)
        sunset_yesterday = self._sun_event(SUN_EVENT_SUNSET, today - timedelta(days=1))
        if sunrise_today is None or sunset_yesterday is None:
            return False
        return sunset_yesterday + SUNSET_DELAY <= moment < sunrise_today

    def _sun_event(self, event: str, day: date) -> datetime | None:
        # The learner asks for every sample of its history on every cycle.
        key = (event, day)
        if key not in self._sun_events:
            if len(self._sun_events) >= SUN_EVENT_CACHE_SIZE:
                self._sun_events.clear()
            self._sun_events[key] = get_astral_event_date(self.hass, event, day)
        return self._sun_events[key]

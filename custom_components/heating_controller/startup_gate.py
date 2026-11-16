"""Hold the hub back after startup until its inputs are there.

After a restart Home Assistant runs before every integration has restored
its entities: TRVs, sensors and the heat source come in over a minute or
more. Computed or written in between, the hub works on half the house --
switches it cannot see stay as they were, a room without its sensor falls
back to the TRVs. The gate stays shut until every entity the hub needs has a
state and none came in for a while, or until a timeout; then it opens once
and everything starts together.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started

_LOGGER = logging.getLogger(__name__)

# No needed entity came in for this long: the rest is not on its way.
STARTUP_QUIET_S = 30
# Opens at the latest this long after Home Assistant started.
STARTUP_TIMEOUT_S = 3 * 60


def entity_ready(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


class StartupGate:
    """Opens once the needed entities are there and quiet, or on timeout."""

    def __init__(
        self, hass: HomeAssistant, on_open: Callable[[], Awaitable[None]]
    ) -> None:
        self._hass = hass
        self._on_open = on_open
        self._entity_ids: set[str] = set()
        self.is_open = False
        self._started_ts: float | None = None
        self._unsub: list[CALLBACK_TYPE] = []
        self._quiet_unsub: CALLBACK_TYPE | None = None

    def require(self, entity_ids: Iterable[str | None]) -> None:
        self._entity_ids.update(entity_id for entity_id in entity_ids if entity_id)

    @property
    def missing(self) -> list[str]:
        return sorted(
            entity_id
            for entity_id in self._entity_ids
            if not entity_ready(self._hass, entity_id)
        )

    @callback
    def async_start(self) -> None:
        # Already running (a reload): the entities are as complete as they get.
        if self._hass.state is CoreState.running and not self.missing:
            self._open()
            return
        self._unsub.append(async_at_started(self._hass, self._async_ha_started))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        self._cancel_quiet()

    @callback
    def _async_ha_started(self, _hass: HomeAssistant) -> None:
        self._started_ts = time.time()
        if self._entity_ids:
            self._unsub.append(
                async_track_state_change_event(
                    self._hass, list(self._entity_ids), self._async_entity_changed
                )
            )
        self._unsub.append(
            async_call_later(self._hass, STARTUP_TIMEOUT_S, self._async_timeout)
        )
        self._restart_quiet()

    @callback
    def _async_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        old_state = event.data["old_state"]
        arrived = old_state is None or old_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )
        if arrived or not entity_ready(self._hass, event.data["entity_id"]):
            self._restart_quiet()

    @callback
    def _restart_quiet(self) -> None:
        self._cancel_quiet()
        if self.is_open or self.missing:
            return
        self._quiet_unsub = async_call_later(
            self._hass, STARTUP_QUIET_S, self._async_quiet
        )

    @callback
    def _cancel_quiet(self) -> None:
        if self._quiet_unsub is not None:
            self._quiet_unsub()
            self._quiet_unsub = None

    @callback
    def _async_quiet(self, _now: Any) -> None:
        self._quiet_unsub = None
        if not self.missing:
            self._open()

    @callback
    def _async_timeout(self, _now: Any) -> None:
        if self.is_open:
            return
        _LOGGER.warning(
            "Starting after %s s without %s", STARTUP_TIMEOUT_S, ", ".join(self.missing)
        )
        self._open()

    @callback
    def _open(self) -> None:
        if self.is_open:
            return
        self.is_open = True
        self.async_stop()
        if self._started_ts is not None:
            _LOGGER.debug(
                "Inputs complete %.0f s after startup", time.time() - self._started_ts
            )
        self._hass.async_create_task(self._on_open())

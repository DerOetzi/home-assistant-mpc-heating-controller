"""The heating season: demand hours and the heat source's heat/off."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DEFAULT_SEASON_HOLD_THRESHOLD_H,
    DEFAULT_SEASON_OFF_AFTER_H,
    DEFAULT_SEASON_ON_THRESHOLD_H,
    HEAT_SOURCE_ACTIVE_STATE,
)
from .control.heating_season import HEAT_MODE, OFF_MODE, HeatingSeason, SeasonSwitcher
from .control.write_budget import WritePriority
from .state_reader import available_state

_LOGGER = logging.getLogger(__name__)

SEASON_THRESHOLDS = ("on_threshold_h", "hold_threshold_h", "off_after_h")


class SeasonControl:
    """Counts the lead rooms' demand hours and switches the heat source by
    them, until someone switches it by hand."""

    def __init__(
        self,
        hass: HomeAssistant,
        heat_source_entity: str | None,
        may_command: Callable[[], bool],
        request_write: Callable[[WritePriority], bool],
    ) -> None:
        """may_command says whether the heat source can take a command now;
        request_write counts the command against the write budget."""
        self._hass = hass
        # The heat source's climate entity, switched heat/off.
        self.heat_source_entity = heat_source_entity
        self._may_command = may_command
        self._request_write = request_write
        self.demand = HeatingSeason()
        self._switcher = SeasonSwitcher()
        # Switch the heat source by the season criterion; off after a hand switch.
        self.auto_on = True
        self.on_threshold_h = DEFAULT_SEASON_ON_THRESHOLD_H
        self.hold_threshold_h = DEFAULT_SEASON_HOLD_THRESHOLD_H
        self.off_after_h = DEFAULT_SEASON_OFF_AFTER_H

    @property
    def heating_on(self) -> bool | None:
        """Whether the heat source heats; None without or with an empty entity."""
        state = available_state(self._hass, self.heat_source_entity)
        if state is None:
            return None
        return state.state == HEAT_SOURCE_ACTIVE_STATE

    @property
    def demand_hours_24h(self) -> float:
        return round(self.demand.hours(time.time()), 2)

    @property
    def switch_on_recommended(self) -> bool:
        return self.demand.switch_on_recommended(
            time.time(), self.on_threshold_h, self.heating_on
        )

    @property
    def switch_off_recommended(self) -> bool:
        return self.demand.switch_off_recommended(
            time.time(), self.off_after_h, self.heating_on
        )

    def set_threshold(self, name: str, value: float) -> None:
        """Set one of the season numbers by its short name."""
        if name not in SEASON_THRESHOLDS:
            raise ValueError(f"Unknown heating season threshold {name}")
        setattr(self, name, float(value))

    def threshold(self, name: str) -> float:
        if name not in SEASON_THRESHOLDS:
            raise ValueError(f"Unknown heating season threshold {name}")
        value: float = getattr(self, name)
        return value

    def update(self, heating_demand: bool | None, now_ts: float) -> None:
        """Count the demand and switch the heat source if the season asks."""
        self.demand.update(heating_demand, now_ts, self.hold_threshold_h)
        self.switch(now_ts)

    def set_auto(self, value: bool) -> None:
        """Switch the automatic heat/off of the heat source."""
        self.auto_on = value
        if value:
            self.switch(time.time())

    def switch(self, now_ts: float) -> None:
        """Command the heat source to the mode the season criterion asks for."""
        if (
            not self.auto_on
            or not self.heat_source_entity
            or self.heating_on is None
            or not self._may_command()
        ):
            return
        mode = SeasonSwitcher.wanted(
            self.switch_on_recommended, self.switch_off_recommended
        )
        if mode is None or not self._switcher.due(mode, now_ts):
            return
        self._switcher.sent(mode, now_ts)
        self._request_write(WritePriority.REQUIRED)
        self._hass.async_create_task(self._async_command_heat_source(mode))

    async def _async_command_heat_source(self, mode: str) -> None:
        try:
            await self._hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.heat_source_entity, "hvac_mode": mode},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Switching %s to %s failed: %s", self.heat_source_entity, mode, err
            )

    @callback
    def heat_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """A change between heat and off not commanded here is by hand."""
        old = event.data["old_state"]
        new = event.data["new_state"]
        modes = (HEAT_MODE, OFF_MODE)
        if (
            old is None
            or new is None
            or old.state not in modes
            or new.state not in modes
            or old.state == new.state
        ):
            return
        if not self._switcher.take_change(new.state, time.time()):
            if self.auto_on:
                _LOGGER.info(
                    "%s switched to %s by hand; automatic heating season off",
                    self.heat_source_entity,
                    new.state,
                )
            self.auto_on = False

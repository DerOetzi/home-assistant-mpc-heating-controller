"""Whether a TRV runs an adaptation or is not calibrated."""

from __future__ import annotations

import logging

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant

from ..profiles import TrvBinding

_LOGGER = logging.getLogger(__name__)

# A write aborts a TRV's adaptation run, so nothing is written to a TRV while
# its profile reports one running -- but no longer than this, so a status that
# never clears cannot leave the room uncontrolled.
# A Danfoss Ally calibration took 1 h 46 to 1 h 49 min.
ADAPTATION_MAX_HOLD_S = 3 * 3600


class AdaptationWatch:
    """Tracks the adaptation runs of a room's TRVs."""

    def __init__(self, hass: HomeAssistant, trvs: list[TrvBinding]) -> None:
        self._hass = hass
        self._trvs = trvs
        self._since: dict[str, float] = {}

    def running(self, trv: TrvBinding, now_ts: float) -> bool:
        """Whether the TRV runs an adaptation that a write would abort."""
        if trv.adaptation_busy is None or trv.adaptation_status_entity is None:
            return False
        if not trv.adaptation_busy.matches(
            self._hass.states.get(trv.adaptation_status_entity)
        ):
            self._since.pop(trv.climate_entity, None)
            return False
        since = self._since.setdefault(trv.climate_entity, now_ts)
        if now_ts - since < ADAPTATION_MAX_HOLD_S:
            return True
        if since > 0:
            _LOGGER.warning(
                "%s still reports an adaptation run after %s s; writing again",
                trv.climate_entity,
                ADAPTATION_MAX_HOLD_S,
            )
            # Marks the run as warned about; it clears with the status.
            self._since[trv.climate_entity] = -ADAPTATION_MAX_HOLD_S
        return False

    def uncalibrated(self, trv: TrvBinding) -> bool:
        """Whether the TRV is mounting or its valve is not calibrated.

        Its valve then follows the mounting or the TRV's own search, not the
        setpoint, so the room learns nothing. Unlike a running adaptation
        this does not stop writes: a TRV left uncalibrated still has to heat.
        """
        if trv.mounted_mode_entity is not None:
            mounted = self._hass.states.get(trv.mounted_mode_entity)
            if mounted is not None and mounted.state == STATE_ON:
                return True
        if trv.adaptation_ready is None or trv.adaptation_status_entity is None:
            return False
        return not trv.adaptation_ready.matches(
            self._hass.states.get(trv.adaptation_status_entity)
        )

    def any_adapting(self, now_ts: float) -> bool:
        """Some TRV of the room moves its valve by itself."""
        return any(
            self.running(trv, now_ts) or self.uncalibrated(trv) for trv in self._trvs
        )

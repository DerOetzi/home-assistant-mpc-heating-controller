"""Whether the TRVs get hot water from the heat source."""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from ..const import HEAT_SOURCE_ACTIVE_STATE

# Heat stays available while the flow dips below the threshold for less than
# this: a defrost drops the flow by about 10 K for one 10-minute report.
FLOW_DIP_TOLERANCE_S = 20 * 60


class HeatAvailability:
    """Heat is there while the heat source heats and the flow reached the
    threshold, bridging short dips and drop-outs."""

    def __init__(self) -> None:
        # The heat source drops out for a few polls at a time while it keeps
        # running; its last known state bridges that.
        self._last_heat_source_state: str | None = None
        # Whether the flow has reached the threshold since the heat source
        # last stopped heating, and since when it has been below it.
        self._flow_heating = False
        self._flow_dip_since_ts: float | None = None

    def update(
        self,
        heat_source: State | None,
        flow_c: float | None,
        threshold_c: float,
        now_ts: float,
    ) -> bool:
        if heat_source is not None and heat_source.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._last_heat_source_state = heat_source.state
        if self._last_heat_source_state != HEAT_SOURCE_ACTIVE_STATE:
            return self._stop()
        if flow_c is not None and flow_c >= threshold_c:
            self._flow_heating = True
            self._flow_dip_since_ts = None
            return True
        if not self._flow_heating or flow_c is None:
            return self._stop()
        if self._flow_dip_since_ts is None:
            self._flow_dip_since_ts = now_ts
        if now_ts - self._flow_dip_since_ts < FLOW_DIP_TOLERANCE_S:
            return True
        return self._stop()

    def _stop(self) -> bool:
        self._flow_heating = False
        self._flow_dip_since_ts = None
        return False

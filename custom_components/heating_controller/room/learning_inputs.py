"""The flow reading and what pauses a room's learning."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State

from ..controller.mpc.controller import RoomMpcController
from ..state_reader import entity_is_on, state_float, state_is_on

# The heat pump reports its flow temperature about every 10 minutes and drops
# out for a poll now and then. Learning pauses only once no reading came for
# longer than this; the control keeps the last reading meanwhile.
FLOW_TEMPERATURE_MAX_GAP_S = 16 * 60


class LearningInputs:
    """Keeps the last flow reading and pauses learning while it is missing
    or the learning block entity is on."""

    def __init__(
        self,
        hass: HomeAssistant,
        mpc: RoomMpcController,
        learning_block_entity: str | None,
    ) -> None:
        self._hass = hass
        self._mpc = mpc
        self.learning_block_entity = learning_block_entity
        # The last flow reading; the flow temperature entity drops out for a
        # moment whenever its template is reloaded.
        self.flow_temperature_c: float | None = None
        # When the flow temperature last had a reading; set on startup.
        self._flow_seen_ts = 0.0
        self._learning_blocked = False
        self._learning_paused = False

    def start(self, now_ts: float) -> None:
        """A missing flow reading counts from startup on, like a gap."""
        self._flow_seen_ts = now_ts
        if self.learning_block_entity:
            self._learning_blocked = entity_is_on(
                self._hass, self.learning_block_entity
            )

    def update_flow(self, state: State | None, now_ts: float) -> None:
        value = state_float(state)
        if value is not None:
            self.flow_temperature_c = value
            self._flow_seen_ts = now_ts
        self._mpc.set_flow_temperature(self.flow_temperature_c)

    def flow_missing(self, now_ts: float) -> bool:
        """No flow reading for longer than the heat pump's usual poll gaps."""
        return now_ts - self._flow_seen_ts > FLOW_TEMPERATURE_MAX_GAP_S

    def update_learning_block(self, state: State | None) -> None:
        self._learning_blocked = state_is_on(state)

    def update_learning_paused(self, now_ts: float) -> None:
        paused = self._learning_blocked or self.flow_missing(now_ts)
        if paused != self._learning_paused:
            self._learning_paused = paused
            self._mpc.set_learning_paused(paused, now_ts)

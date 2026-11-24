"""What the room writes to its TRVs: setpoint, room temperature, heat switch."""

from __future__ import annotations

import time

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State

from ..controller.mpc.math_helper import clamp, round_to_step
from ..controller.trv_inputs import ExternalTemperatureSchedule, raw_value
from ..profiles import TrvBinding
from ..state_reader import attribute_float, entity_is_on, state_float
from .adaptation import AdaptationWatch

TARGET_TEMPERATURE_TOLERANCE_C = 0.01
# A Danfoss Ally reports its previous setpoint once more a few seconds after a
# write. Within this window the written target counts as the TRV's target, so
# the echo does not trigger a second write.
TARGET_WRITE_SETTLE_S = 120.0
# Without a target_temp_step attribute the TRV takes setpoints in this step.
DEFAULT_TARGET_TEMPERATURE_STEP_C = 0.5


async def async_set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id, "value": value},
        blocking=False,
    )


def trv_setpoint_c(state: State, target_c: float) -> float:
    """The target as the TRV's climate entity takes it."""
    attributes = state.attributes
    step_c = attributes.get("target_temp_step") or DEFAULT_TARGET_TEMPERATURE_STEP_C
    setpoint_c = round_to_step(target_c, step_c)
    min_c = attributes.get("min_temp")
    max_c = attributes.get("max_temp")
    if min_c is not None and max_c is not None:
        setpoint_c = clamp(setpoint_c, min_c, max_c)
    return setpoint_c


class TrvWriter:
    """Writes a room's TRVs, only what changed and nothing while one adapts."""

    def __init__(
        self,
        hass: HomeAssistant,
        trvs: list[TrvBinding],
        adaptation: AdaptationWatch,
    ) -> None:
        self._hass = hass
        self._trvs = trvs
        self._adaptation = adaptation
        # Target writes per TRV since setup: every write wakes the TRV.
        self.setpoint_writes: dict[str, int] = {}
        self._written_targets: dict[str, tuple[float, float]] = {}
        # External temperature writes per TRV since setup, and when each TRV
        # gets the room sensor's reading next.
        self.external_writes: dict[str, int] = {}
        self._external_schedules: dict[str, ExternalTemperatureSchedule] = {
            trv.external_temperature_entity: ExternalTemperatureSchedule()
            for trv in trvs
            if trv.external_temperature_entity
        }
        # TRVs handed back their own sensor since setup (room without sensor).
        self._external_off_sent: set[str] = set()

    # --- Setpoints ---------------------------------------------------------

    async def async_write_setpoints(self, target_c: float, now_ts: float) -> None:
        """Hand every TRV the room's setpoint; it regulates on its own."""
        for trv in self._trvs:
            entity_id = trv.climate_entity
            # Every write wakes the TRV and aborts a running adaptation run,
            # so only send a target the TRV does not already report, none to a
            # TRV that cannot take it, and none while it adapts.
            state = self._hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            if self._adaptation.running(trv, now_ts):
                continue
            setpoint_c = trv_setpoint_c(state, target_c)
            current_target_c = self.current_target(entity_id)
            if (
                current_target_c is not None
                and abs(current_target_c - setpoint_c) < TARGET_TEMPERATURE_TOLERANCE_C
            ):
                continue
            await self._hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": setpoint_c},
                blocking=False,
            )
            self.setpoint_writes[entity_id] = self.setpoint_writes.get(entity_id, 0) + 1
            self._written_targets[entity_id] = (setpoint_c, now_ts)

    def current_target(self, entity_id: str) -> float | None:
        """The TRV's target, or the one just written while its echo settles."""
        written = self._written_targets.get(entity_id)
        if written is not None and time.time() - written[1] < TARGET_WRITE_SETTLE_S:
            return written[0]
        return attribute_float(self._hass.states.get(entity_id), "temperature")

    # --- External temperature ----------------------------------------------

    async def async_write_external_temperatures(
        self, room_c: float | None, has_room_sensor: bool, now_ts: float
    ) -> None:
        """Hand the TRVs that take one the room sensor's reading.

        While the reading is unknown nothing is sent, and the TRV returns to
        its own sensor after its timeout. A room without a room sensor hands
        its TRVs back their own once.
        """
        for trv in self._trvs:
            entity_id = trv.external_temperature_entity
            if entity_id is None or self._adaptation.running(trv, now_ts):
                continue
            state = self._hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                continue
            if not has_room_sensor:
                await self._async_hand_back_own_sensor(trv, entity_id, state)
            elif room_c is not None:
                await self._async_send_room_temperature(trv, entity_id, room_c, now_ts)

    async def _async_hand_back_own_sensor(
        self, trv: TrvBinding, entity_id: str, state: State
    ) -> None:
        off_value = trv.external_off_value
        if (
            off_value is not None
            and entity_id not in self._external_off_sent
            and state_float(state) != off_value
        ):
            await async_set_number(self._hass, entity_id, off_value)
            self._count_external_write(entity_id)
        self._external_off_sent.add(entity_id)

    async def _async_send_room_temperature(
        self, trv: TrvBinding, entity_id: str, room_c: float, now_ts: float
    ) -> None:
        covered = entity_is_on(self._hass, trv.radiator_covered_entity)
        schedule = self._external_schedules[entity_id]
        if not schedule.due(room_c, covered, now_ts):
            return
        await async_set_number(
            self._hass, entity_id, raw_value(room_c, trv.external_scale)
        )
        schedule.sent(room_c, covered, now_ts)
        self._count_external_write(entity_id)

    def _count_external_write(self, entity_id: str) -> None:
        self.external_writes[entity_id] = self.external_writes.get(entity_id, 0) + 1

    def resend_external(self, entity_id: str) -> None:
        """The entity is back from an outage: it gets its value again."""
        schedule = self._external_schedules.get(entity_id)
        if schedule is not None:
            schedule.reset()
        self._external_off_sent.discard(entity_id)

    # --- Heat available ----------------------------------------------------

    async def async_apply_heat_available(self, active: bool, now_ts: float) -> None:
        """Switch the TRVs' heat-available switches to the room's state."""
        desired_state = STATE_ON if active else STATE_OFF
        switches_to_set = [
            trv.heat_available_entity
            for trv in self._trvs
            if trv.heat_available_entity
            and not self._adaptation.running(trv, now_ts)
            and (
                (state := self._hass.states.get(trv.heat_available_entity)) is None
                or state.state != desired_state
            )
        ]
        if not switches_to_set:
            return
        await self._hass.services.async_call(
            "switch",
            "turn_on" if active else "turn_off",
            {"entity_id": switches_to_set},
            blocking=False,
        )

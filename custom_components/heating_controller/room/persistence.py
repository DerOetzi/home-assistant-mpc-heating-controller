"""What a room keeps across restarts: learned factors, gains, flow gate."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from ..controller.mpc.controller import RoomMpcController
from ..controller.mpc.types import FlowGateState, LearningFactors
from ..store import (
    FlowGateStateStore,
    GainStore,
    LearningFactorsStore,
    async_remove_setpoint_correction_store,
    async_remove_valve_curve_store,
)


class RoomPersistence:
    """The room's stores, loaded into and saved from its controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        room_name: str,
        room_id: str,
        mpc: RoomMpcController,
    ) -> None:
        self._hass = hass
        self._room_name = room_name
        self._room_id = room_id
        self._mpc = mpc
        self.factors = LearningFactorsStore(hass, room_name, room_id)
        self.gains = GainStore(hass, room_name, room_id)
        self.flow_gate = FlowGateStateStore(hass, room_name, room_id)
        self._saved_flow_gate: FlowGateState | None = None

    async def async_restore(self) -> None:
        """Load what was saved into the controller; drop retired stores."""
        factors = await self.factors.async_load()
        if factors is not None:
            self._mpc.recalibrate_learning_factors(factors)
        gains = await self.gains.async_load()
        if gains is not None:
            self._mpc.restore_gains(gains)
        flow_gate_state = await self.flow_gate.async_load()
        if flow_gate_state is not None:
            self._mpc.restore_flow_gate_state(flow_gate_state)
            self._saved_flow_gate = flow_gate_state
        await async_remove_valve_curve_store(self._hass, self._room_name, self._room_id)
        await async_remove_setpoint_correction_store(
            self._hass, self._room_name, self._room_id
        )

    async def async_save_learning(self) -> None:
        """Save factors and gains the controller marked as changed."""
        factors = self._mpc.take_factors_to_save()
        if factors is not None:
            await self.factors.async_save(factors)
        await self.async_save_gains()

    async def async_save_gains(self) -> None:
        gains = self._mpc.take_gains_to_save()
        if gains is not None:
            await self.gains.async_save(gains)

    async def async_save_factors(self, factors: LearningFactors) -> None:
        await self.factors.async_save(factors)

    async def async_save_flow_gate(self) -> None:
        """Save the flow gate state once it changed."""
        flow_gate_state = self._mpc.flow_gate_state
        if flow_gate_state == self._saved_flow_gate:
            return
        await self.flow_gate.async_save(flow_gate_state)
        self._saved_flow_gate = flow_gate_state

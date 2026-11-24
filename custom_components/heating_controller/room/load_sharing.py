"""Load sharing between the TRVs of a room."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from ..controller.trv_inputs import load_room_mean
from ..profiles import LoadSharingEntities, TrvBinding
from ..state_reader import state_float
from .adaptation import AdaptationWatch
from .trv_writer import async_set_number


class LoadSharing:
    """Shares the load between a room's TRVs, if it has two or more.

    Each TRV takes part only while it gets the room's mean load, so a TRV
    that cannot get it, or the only one in its room, works alone.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        trvs: list[TrvBinding],
        adaptation: AdaptationWatch,
    ) -> None:
        self._hass = hass
        self._trvs = trvs
        self._adaptation = adaptation
        self._sharing: list[tuple[TrvBinding, LoadSharingEntities]] = [
            (trv, entities)
            for trv in trvs
            if (entities := trv.load_sharing) is not None
        ]
        # The room's mean load last handed to its TRVs; None if they work alone.
        self.room_mean: int | None = None

    @property
    def possible(self) -> bool:
        """Some TRV of the room can take part."""
        return bool(self._sharing)

    def owns(self, entity_id: str) -> bool:
        """The entity is one this writes to."""
        return any(
            entity_id in (trv.load_balancing_entity, trv.load_room_mean_entity)
            for trv in self._trvs
        )

    async def async_update(self, now_ts: float) -> None:
        balance = len(self._sharing) >= 2
        await self._async_switch_balancing(balance, now_ts)
        if not balance:
            self.room_mean = None
            return
        self.room_mean = load_room_mean(
            [
                (
                    state_float(state),
                    state.last_reported.timestamp() if state is not None else None,
                )
                for _, entities in self._sharing
                for state in (self._hass.states.get(entities.estimate),)
            ],
            now_ts,
        )
        if self.room_mean is not None:
            await self._async_write_room_mean(self.room_mean, now_ts)

    async def _async_switch_balancing(self, balance: bool, now_ts: float) -> None:
        for trv in self._trvs:
            entity_id = trv.load_balancing_entity
            if entity_id is None or self._adaptation.running(trv, now_ts):
                continue
            state = self._hass.states.get(entity_id)
            if state is None or state.state not in (STATE_ON, STATE_OFF):
                continue
            wanted = balance and trv.shares_load
            if (state.state == STATE_ON) != wanted:
                await self._hass.services.async_call(
                    "switch",
                    "turn_on" if wanted else "turn_off",
                    {"entity_id": entity_id},
                    blocking=False,
                )

    async def _async_write_room_mean(self, mean: int, now_ts: float) -> None:
        for trv, entities in self._sharing:
            if self._adaptation.running(trv, now_ts):
                continue
            state = self._hass.states.get(entities.room_mean)
            if state is None or state.state == STATE_UNAVAILABLE:
                continue
            await async_set_number(self._hass, entities.room_mean, mean)

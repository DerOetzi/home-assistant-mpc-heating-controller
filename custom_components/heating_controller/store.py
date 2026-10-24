from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .const import DOMAIN
from .controller.mpc.types import FlowGateState, LearningFactors

STORAGE_VERSION = 1


def _storage_key(room_name: str, entry_id: str) -> str:
    return f"{DOMAIN}_{slugify(room_name)}_{entry_id}_learning_factors"


def _flow_gate_storage_key(room_name: str, entry_id: str) -> str:
    return f"{DOMAIN}_{slugify(room_name)}_{entry_id}_flow_gate"


def _observer_storage_key(room_name: str, entry_id: str) -> str:
    return f"{DOMAIN}_{slugify(room_name)}_{entry_id}_observer"


class LearningFactorsStore:
    def __init__(self, hass: HomeAssistant, room_name: str, entry_id: str) -> None:
        self._room_name = room_name
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, _storage_key(room_name, entry_id)
        )

    async def async_load(self) -> LearningFactors | None:
        data = await self._store.async_load()
        if data is None:
            return None
        return LearningFactors(
            ua_factor=data["ua_factor"], capacity_factor=data["capacity_factor"]
        )

    async def async_save(self, factors: LearningFactors) -> None:
        await self._store.async_save(
            {
                "room_name": self._room_name,
                "ua_factor": factors.ua_factor,
                "capacity_factor": factors.capacity_factor,
            }
        )

    async def async_remove(self) -> None:
        await self._store.async_remove()


class FlowGateStateStore:
    def __init__(self, hass: HomeAssistant, room_name: str, entry_id: str) -> None:
        self._room_name = room_name
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, _flow_gate_storage_key(room_name, entry_id)
        )

    async def async_load(self) -> FlowGateState | None:
        data = await self._store.async_load()
        if data is None:
            return None
        return FlowGateState(
            live_closed=bool(data.get("live_closed", False)),
            preview_closed=bool(data.get("preview_closed", False)),
        )

    async def async_save(self, state: FlowGateState) -> None:
        await self._store.async_save(
            {
                "room_name": self._room_name,
                "live_closed": state.live_closed,
                "preview_closed": state.preview_closed,
            }
        )

    async def async_remove(self) -> None:
        await self._store.async_remove()


class ObserverStateStore:
    """The observer's disturbance estimate, so a restart keeps last night's."""

    def __init__(self, hass: HomeAssistant, room_name: str, entry_id: str) -> None:
        self._room_name = room_name
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, _observer_storage_key(room_name, entry_id)
        )

    async def async_load(self) -> float | None:
        data = await self._store.async_load()
        if data is None:
            return None
        return float(data["disturbance_w"])

    async def async_save(self, disturbance_w: float) -> None:
        await self._store.async_save(
            {"room_name": self._room_name, "disturbance_w": disturbance_w}
        )

    async def async_remove(self) -> None:
        await self._store.async_remove()

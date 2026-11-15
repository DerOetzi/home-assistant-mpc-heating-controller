from __future__ import annotations

from collections.abc import Callable

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


def _valve_curve_storage_key(room_name: str, entry_id: str) -> str:
    return f"{DOMAIN}_{slugify(room_name)}_{entry_id}_valve_curve"


async def async_remove_valve_curve_store(
    hass: HomeAssistant, room_name: str, entry_id: str
) -> None:
    """Delete the valve curve samples a former version kept."""
    await Store(
        hass, STORAGE_VERSION, _valve_curve_storage_key(room_name, entry_id)
    ).async_remove()


def _gains_storage_key(room_name: str, entry_id: str) -> str:
    return f"{DOMAIN}_{slugify(room_name)}_{entry_id}_gains"


class GainStore:
    """The state of the gain regressions (night and hourly)."""

    def __init__(self, hass: HomeAssistant, room_name: str, entry_id: str) -> None:
        self._room_name = room_name
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, _gains_storage_key(room_name, entry_id)
        )

    async def async_load(self) -> dict | None:
        data = await self._store.async_load()
        if data is None:
            return None
        return dict(data.get("gains", {}))

    async def async_save(self, gains: dict) -> None:
        await self._store.async_save({"room_name": self._room_name, "gains": gains})

    async def async_remove(self) -> None:
        await self._store.async_remove()


def _setpoint_correction_storage_key(room_name: str, entry_id: str) -> str:
    return f"{DOMAIN}_{slugify(room_name)}_{entry_id}_setpoint_correction"


class SetpointCorrectionStore:
    """The room's setpoint correction, so a restart keeps it."""

    def __init__(self, hass: HomeAssistant, room_name: str, entry_id: str) -> None:
        self._room_name = room_name
        self._store: Store[dict] = Store(
            hass,
            STORAGE_VERSION,
            _setpoint_correction_storage_key(room_name, entry_id),
        )

    async def async_load(self) -> float | None:
        data = await self._store.async_load()
        if data is None:
            return None
        return float(data.get("correction_c", 0.0))

    async def async_save(self, correction_c: float) -> None:
        await self._store.async_save(
            {"room_name": self._room_name, "correction_c": correction_c}
        )

    async def async_remove(self) -> None:
        await self._store.async_remove()


class HubStore:
    """The hub's own state: write budget and heating season."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_hub"
        )

    async def async_load(self) -> dict:
        return dict(await self._store.async_load() or {})

    async def async_save(self, data: dict) -> None:
        await self._store.async_save(data)

    def async_delay_save(self, data_func: Callable[[], dict], delay_s: float) -> None:
        self._store.async_delay_save(data_func, delay_s)

    async def async_remove(self) -> None:
        await self._store.async_remove()

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ...const import RoomTemperatureStrategy


class RoomMpcErrorCode(StrEnum):
    MISSING_ROOM_TEMPERATURE = "missing_room_temperature"
    MISSING_OUTDOOR_TEMPERATURE = "missing_outdoor_temperature"


@dataclass
class RoomMpcError:
    code: RoomMpcErrorCode
    message: str
    details: dict[str, object] | None = None


@dataclass
class RoomTemperatureResult:
    valid: bool
    temperature_c: float | None = None
    used_strategy: RoomTemperatureStrategy | None = None
    trv_temperatures: list[float | None] | None = None
    room_sensor_temp_c: float | None = None
    error: RoomMpcError | None = None


@dataclass
class RoomMpcInput:
    now_ts: float
    target_temp_c: float
    room_temp_c: float
    outdoor_temp_c: float
    used_room_sensor_strategy: RoomTemperatureStrategy
    trv_temperatures: list[float | None]
    flow_temp_c: float | None = None
    room_sensor_temp_c: float | None = None


@dataclass
class RoomMpcInputResult:
    valid: bool
    input: RoomMpcInput | None = None
    error: RoomMpcError | None = None


@dataclass
class RoomMpcResult:
    trv_targets: list[float]
    input: RoomMpcInput
    demand_pct: float
    requested_heating_power_w: float
    available_heating_power_w: float
    recommended_flow_temperature_c: float | None
    hold_flow_temperature_c: float | None = None
    flow_gate_closed: bool = False

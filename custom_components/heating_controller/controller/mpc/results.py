from __future__ import annotations

from dataclasses import dataclass, field
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
    room_sensor_fallback: bool = False
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
    # A room sensor is configured but stale or missing, so the TRVs stand in.
    room_sensor_fallback: bool = False
    # Valve opening per TRV in %, None where none is configured or reported.
    valve_positions_pct: list[float | None] = field(default_factory=list)
    # A valve position entity is configured but stale or unavailable.
    valve_position_missing: bool = False
    # A TRV calibrates its valve: it moves the valve by itself, whatever the
    # controller asks for.
    trv_adapting: bool = False
    # A radiator gives off no heat although its valve reports open (air in
    # it, a valve stuck shut).
    radiator_not_heating: bool = False
    # When the valve of that radiator opened: from then on its heat is in
    # doubt.
    radiator_not_heating_since_ts: float | None = None
    # A domestic hot-water charge runs: the radiators get no water, and
    # flow_temp_c is the heating flow from before it.
    hot_water_charge: bool = False


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
    gated_hold_flow_temperature_c: float | None = None
    recovery_flow_temperature_c: float | None = None
    recovery_flow_saturated: bool = False
    flow_gate_closed: bool = False
    # Heat at the reported valve openings, before the radiator lag.
    measured_heating_power_w: float | None = None
    # Observer state the demand was planned from.
    estimated_room_temperature_c: float | None = None
    radiator_power_w: float | None = None
    disturbance_w: float | None = None

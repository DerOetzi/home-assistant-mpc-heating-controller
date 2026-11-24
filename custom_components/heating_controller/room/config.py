"""Controller and state settings of a room, built from its configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..const import (
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_CONSTRUCTION,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FROST_PROTECTION_TEMPERATURE,
    CONF_OPAQUE_SURFACES,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOM_AREA,
    CONF_ROOM_HEAT_LOAD,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_WIDTH_MM,
    CONF_WINDOW_SURFACES,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    Construction,
    DesignTemperatureSystem,
    HeatEmitterType,
    PanelRadiatorType,
)
from ..controller.mpc.controller import (
    FlowGateConfig,
)
from ..controller.mpc.types import (
    RoomThermalConfig,
    TrvConfig,
)
from ..controller.state import HeatingStateConfig
from ..profiles import TrvBinding


def build_trv_configs(trvs: list[TrvBinding]) -> list[TrvConfig]:
    return [
        TrvConfig(
            name=trv.climate_entity,
            emitter_type=HeatEmitterType(trv.emitter[CONF_TRV_EMITTER_TYPE]),
            radiator_type=(
                PanelRadiatorType(trv.emitter[CONF_TRV_RADIATOR_TYPE])
                if CONF_TRV_RADIATOR_TYPE in trv.emitter
                else None
            ),
            width_mm=trv.emitter.get(CONF_TRV_WIDTH_MM),
            height_mm=trv.emitter.get(CONF_TRV_HEIGHT_MM),
            nominal_power_w=trv.emitter.get(CONF_TRV_NOMINAL_POWER_W),
            valve_position_configured=trv.valve_position_entity is not None,
        )
        for trv in trvs
    ]


def state_config(data: Mapping[str, Any]) -> HeatingStateConfig:
    return HeatingStateConfig(
        boost_temperature_offset_c=data[CONF_BOOST_TEMPERATURE_OFFSET],
        frost_protection_temperature_c=data[CONF_FROST_PROTECTION_TEMPERATURE],
        pv_boost_enabled=data[CONF_PV_BOOST_ENABLED],
        pv_boost_temperature_offset_c=data[CONF_PV_BOOST_TEMPERATURE_OFFSET],
    )


def thermal_config(
    data: Mapping[str, Any], flow_threshold_c: float
) -> RoomThermalConfig:
    return RoomThermalConfig(
        design_indoor_temperature_c=data[CONF_DESIGN_INDOOR_TEMPERATURE],
        design_outdoor_temperature_c=data[CONF_DESIGN_OUTDOOR_TEMPERATURE],
        design_temperature_system=DesignTemperatureSystem(
            data[CONF_DESIGN_TEMPERATURE_SYSTEM]
        ),
        room_heat_load_w=data[CONF_ROOM_HEAT_LOAD],
        flow_threshold_c=flow_threshold_c,
        room_area_m2=data[CONF_ROOM_AREA],
        construction=Construction(data[CONF_CONSTRUCTION]),
        window_surfaces=tuple(data.get(CONF_WINDOW_SURFACES) or ()),
        opaque_surfaces=tuple(data.get(CONF_OPAQUE_SURFACES) or ()),
    )


def flow_gate_config(data: Mapping[str, Any]) -> FlowGateConfig:
    return FlowGateConfig(
        close_surplus_c=data.get(
            CONF_FLOW_GATE_CLOSE_SURPLUS, DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C
        ),
        open_surplus_c=data.get(
            CONF_FLOW_GATE_OPEN_SURPLUS, DEFAULT_FLOW_GATE_OPEN_SURPLUS_C
        ),
        hold_time_s=data.get(CONF_FLOW_GATE_HOLD_TIME, DEFAULT_FLOW_GATE_HOLD_TIME_S),
    )

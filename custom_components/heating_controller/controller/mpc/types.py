from __future__ import annotations

from dataclasses import dataclass

from ...const import (
    DesignTemperatureSystem,
    HeatEmitterType,
    LearningStatus,
    PanelRadiatorType,
)


@dataclass
class TrvConfig:
    name: str
    emitter_type: HeatEmitterType = HeatEmitterType.PANEL

    min_target_temperature_c: float = 5.0
    max_target_temperature_c: float = 30.0
    target_temperature_step_c: float = 0.5

    radiator_type: PanelRadiatorType | None = PanelRadiatorType.TYPE_22
    width_mm: float | None = 1000
    height_mm: float | None = 600

    nominal_power_w: float | None = None


@dataclass
class RoomThermalConfig:
    design_indoor_temperature_c: float = 20.0
    design_outdoor_temperature_c: float = -12.0
    design_temperature_system: DesignTemperatureSystem = (
        DesignTemperatureSystem.SYSTEM_55_45
    )
    room_heat_load_w: float = 1000.0


@dataclass
class LearningFactors:
    ua_factor: float = 1.0
    capacity_factor: float = 1.0


@dataclass
class FlowGateState:
    """Open/closed state of both surplus gates, persisted across restarts.

    Only the closed flag is kept, not the pending close timer: a closed gate
    reopens immediately if the surplus is gone, so restoring it is safe, while
    a restored timer could close the gate on a surplus nobody observed.
    """

    live_closed: bool = False
    preview_closed: bool = False


@dataclass
class RoomModelLearningState:
    status: LearningStatus
    learned_factors: LearningFactors

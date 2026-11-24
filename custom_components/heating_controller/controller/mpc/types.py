from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

from ...const import (
    DEFAULT_FLOW_THRESHOLD_C,
    CapacityOutcome,
    Construction,
    DesignTemperatureSystem,
    HeatEmitterType,
    LearningStatus,
    PanelRadiatorType,
    UaBalanceOutcome,
)


@dataclass
class TrvConfig:
    name: str
    emitter_type: HeatEmitterType = HeatEmitterType.PANEL

    radiator_type: PanelRadiatorType | None = PanelRadiatorType.TYPE_22
    width_mm: float | None = 1000
    height_mm: float | None = 600

    nominal_power_w: float | None = None
    # The TRV reports its valve opening (0-100 %) through an entity.
    valve_position_configured: bool = False


@dataclass
class RoomThermalConfig:
    design_indoor_temperature_c: float = 20.0
    design_outdoor_temperature_c: float = -12.0
    design_temperature_system: DesignTemperatureSystem = (
        DesignTemperatureSystem.SYSTEM_55_45
    )
    room_heat_load_w: float = 1000.0
    flow_threshold_c: float = DEFAULT_FLOW_THRESHOLD_C
    _: KW_ONLY
    room_area_m2: float
    construction: Construction
    # Building surfaces whose sun reaches the room: through windows at once,
    # through walls and roof surfaces with a delay.
    window_surfaces: tuple[str, ...] = ()
    opaque_surfaces: tuple[str, ...] = ()


@dataclass
class LearningFactors:
    ua_factor: float = 1.0
    capacity_factor: float = 1.0
    # Airing capacity, observed only; None until learned.
    vent_capacity_factor: float | None = None


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


@dataclass
class UaBalanceInfo:
    """Where the heat-loss heat balance stands, for diagnostics."""

    outcome: UaBalanceOutcome | None = None
    collecting_since_ts: float | None = None
    last_learned_ts: float | None = None
    last_target_factor: float | None = None
    last_drift_c: float | None = None
    last_window_s: float | None = None
    # Time-weighted means of the last stretch that got as far as the balance,
    # as data basis for judging what the factor can and cannot explain.
    # Earlier rejections (too short, gap, ...) keep them, like the fields above.
    last_mean_outdoor_c: float | None = None
    last_mean_room_c: float | None = None
    last_mean_flow_c: float | None = None
    last_mean_heating_w: float | None = None


@dataclass
class CapacityStretchInfo:
    """The last jump stretch the capacity was judged on, for diagnostics."""

    outcome: CapacityOutcome = CapacityOutcome.WAITING
    # "heat_up" or "cool_down"
    kind: str | None = None
    start_ts: float | None = None
    end_ts: float | None = None
    room_delta_c: float | None = None
    estimate_factor: float | None = None
    saturated_share: float | None = None
    last_learned_ts: float | None = None


@dataclass(frozen=True)
class RoomContext:
    """What the house tells a room before each calculation."""

    now_ts: float
    # The lowest flow the heat source can usefully deliver.
    flow_threshold_c: float
    # Whether the TRVs get hot water; without it the radiators cool down and
    # nothing is learned.
    heat_available: bool = True
    # A TRV calibrates its valve: it moves the valve by itself.
    trv_adapting: bool = False
    # A charge seen outside the room: the house's one signal (decisive,
    # replaces the room's own detection on its flow) or the hot water
    # control's (adds to it).
    external_hot_water_charge: bool = False
    external_hot_water_decisive: bool = False

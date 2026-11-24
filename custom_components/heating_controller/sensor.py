from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_LEAD_FLOW,
    CurveEnd,
)
from .entity import (
    async_add_flow_entities,
    async_add_hot_water_entities,
    async_add_room_entities,
)
from .hub import HeatingHub
from .sensor_entities.flow import (
    FlowActuatorActualSensor,
    FlowActuatorTargetSensor,
    FlowCurveRecommendationSensor,
    FlowCurveSensor,
    FlowEffectiveCurveSensor,
    FlowFeedbackSensor,
    FlowHoldCurvesSensor,
    FlowTargetSensor,
    FlowThresholdSensor,
)
from .sensor_entities.hot_water import (
    CirculationReasonSensor,
    HotWaterPhaseSensor,
    HotWaterTargetSensor,
    LegionellaStatusSensor,
)
from .sensor_entities.hub import (
    HeatingDemandHoursSensor,
    HeatingSeasonLastDemandSensor,
    HoldFlowReserveSensor,
    IrradianceSensor,
    LeadHoldFlowTemperatureSensor,
    WriteBudgetSensor,
)
from .sensor_entities.room import (
    AdditionalHeatSensor,
    AvailableHeatingPowerSensor,
    BaseTemperatureSensor,
    BoostFlowTemperatureSensor,
    CapacityFactorSensor,
    CapacityLearningStatusSensor,
    CurveHoldFlowTemperatureSensor,
    GatedHoldFlowTemperatureSensor,
    HeatingDemandSensor,
    HoldFlowTemperatureSensor,
    MeasuredHeatingPowerSensor,
    RadiatorHeatingPowerSensor,
    RoomTemperatureSensor,
    SupplyCountSensor,
    SupplySensor,
    UaFactorSensor,
    UaLearningStatusSensor,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub: HeatingHub = entry.runtime_data
    hub_entities: list[SensorEntity] = [
        LeadHoldFlowTemperatureSensor(hub),
        HeatingDemandHoursSensor(hub),
        HeatingSeasonLastDemandSensor(hub),
        WriteBudgetSensor(hub),
    ]
    if hub.has_flow_curve:
        hub_entities.append(HoldFlowReserveSensor(hub))
    async_add_flow_entities(
        entry,
        async_add_entities,
        lambda flow: [
            FlowCurveSensor(flow),
            FlowActuatorActualSensor(flow),
            FlowEffectiveCurveSensor(flow),
            FlowTargetSensor(flow),
            FlowFeedbackSensor(flow),
            FlowActuatorTargetSensor(flow),
            FlowThresholdSensor(flow),
            FlowCurveRecommendationSensor(flow, CurveEnd.COLD),
            FlowCurveRecommendationSensor(flow, CurveEnd.WARM),
            FlowHoldCurvesSensor(flow),
        ],
    )
    async_add_hot_water_entities(
        entry,
        async_add_entities,
        lambda hot_water: (
            [HotWaterTargetSensor(hot_water), HotWaterPhaseSensor(hot_water)]
            + (
                [LegionellaStatusSensor(hot_water)]
                if hot_water.legionella_enabled
                else []
            )
            + (
                [CirculationReasonSensor(hot_water)]
                if hot_water.circulation_enabled
                else []
            )
        ),
    )
    if hub.irradiance is not None:
        hub_entities += [
            IrradianceSensor(hub, geometry.surface)
            for geometry in hub.irradiance.geometries
        ]
    async_add_entities(hub_entities)
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: [
            SupplySensor(coordinator),
            *(
                [SupplyCountSensor(coordinator, hub)]
                if coordinator.data.get(CONF_LEAD_FLOW)
                else []
            ),
            BoostFlowTemperatureSensor(coordinator),
            HoldFlowTemperatureSensor(coordinator),
            GatedHoldFlowTemperatureSensor(coordinator),
            CurveHoldFlowTemperatureSensor(coordinator, CurveEnd.COLD),
            CurveHoldFlowTemperatureSensor(coordinator, CurveEnd.WARM),
            HeatingDemandSensor(coordinator),
            AvailableHeatingPowerSensor(coordinator),
            RadiatorHeatingPowerSensor(coordinator),
            MeasuredHeatingPowerSensor(coordinator),
            RoomTemperatureSensor(coordinator),
            BaseTemperatureSensor(coordinator),
            UaLearningStatusSensor(coordinator),
            CapacityLearningStatusSensor(coordinator),
            UaFactorSensor(coordinator),
            CapacityFactorSensor(coordinator),
            AdditionalHeatSensor(coordinator),
        ],
    )

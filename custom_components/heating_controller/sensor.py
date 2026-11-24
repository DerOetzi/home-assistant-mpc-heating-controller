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
from .coordinator import HeatingRoomCoordinator
from .entity import (
    async_add_flow_entities,
    async_add_hot_water_entities,
    async_add_room_entities,
)
from .flow_device import FlowDevice
from .hot_water_device import HotWaterDevice
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
    async_add_flow_entities(entry, async_add_entities, _flow_sensors)
    async_add_hot_water_entities(entry, async_add_entities, _hot_water_sensors)
    async_add_entities(_hub_sensors(hub))
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: _room_sensors(coordinator, hub),
    )


def _hub_sensors(hub: HeatingHub) -> list[SensorEntity]:
    sensors: list[SensorEntity] = [
        LeadHoldFlowTemperatureSensor(hub),
        HeatingDemandHoursSensor(hub),
        HeatingSeasonLastDemandSensor(hub),
        WriteBudgetSensor(hub),
    ]
    if hub.has_flow_curve:
        sensors.append(HoldFlowReserveSensor(hub))
    if hub.irradiance is not None:
        sensors += [
            IrradianceSensor(hub, geometry.surface)
            for geometry in hub.irradiance.geometries
        ]
    return sensors


def _flow_sensors(flow: FlowDevice) -> list[SensorEntity]:
    return [
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
    ]


def _hot_water_sensors(hot_water: HotWaterDevice) -> list[SensorEntity]:
    sensors: list[SensorEntity] = [
        HotWaterTargetSensor(hot_water),
        HotWaterPhaseSensor(hot_water),
    ]
    if hot_water.legionella_enabled:
        sensors.append(LegionellaStatusSensor(hot_water))
    if hot_water.circulation_enabled:
        sensors.append(CirculationReasonSensor(hot_water))
    return sensors


def _room_sensors(
    coordinator: HeatingRoomCoordinator, hub: HeatingHub
) -> list[SensorEntity]:
    sensors: list[SensorEntity] = [SupplySensor(coordinator)]
    if coordinator.data.get(CONF_LEAD_FLOW):
        sensors.append(SupplyCountSensor(coordinator, hub))
    sensors += [
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
    ]
    return sensors

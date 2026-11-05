from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DEFAULT_COMFORT_TEMPERATURE_C,
    DEFAULT_ECO_TEMPERATURE_OFFSET_C,
    DEFAULT_SEASON_HOLD_THRESHOLD_H,
    DEFAULT_SEASON_OFF_AFTER_H,
    DEFAULT_SEASON_ON_THRESHOLD_H,
)
from .control.flow import ActuatorMode
from .coordinator import HeatingRoomCoordinator
from .entity import (
    FlowEntity,
    HeatingControllerEntity,
    HeatingHubEntity,
    HotWaterEntity,
    async_add_flow_entities,
    async_add_hot_water_entities,
    async_add_room_entities,
)
from .flow_device import FlowDevice
from .hot_water_device import HOT_WATER_NUMBER_DEFAULTS, HotWaterDevice
from .hub import HeatingHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub: HeatingHub = entry.runtime_data
    async_add_entities(
        [
            HeatingSeasonNumber(
                hub, "on_threshold_h", DEFAULT_SEASON_ON_THRESHOLD_H, 0.5, 24.0
            ),
            HeatingSeasonNumber(
                hub, "hold_threshold_h", DEFAULT_SEASON_HOLD_THRESHOLD_H, 0.5, 24.0
            ),
            HeatingSeasonNumber(
                hub, "off_after_h", DEFAULT_SEASON_OFF_AFTER_H, 12.0, 168.0
            ),
        ]
    )
    async_add_room_entities(
        entry,
        async_add_entities,
        lambda coordinator: [
            ComfortTemperatureNumber(coordinator),
            EcoOffsetNumber(coordinator),
        ],
    )
    async_add_flow_entities(
        entry,
        async_add_entities,
        lambda flow: [
            FlowNumber(flow, spec)
            for spec in FLOW_NUMBERS
            # In offset mode the threshold follows the curve and the offset.
            if spec.key != "threshold" or flow.mode is ActuatorMode.SETPOINT
        ],
    )

    async_add_hot_water_entities(
        entry,
        async_add_entities,
        lambda hot_water: [
            HotWaterTemperatureNumber(hot_water, key)
            for key in HOT_WATER_NUMBER_DEFAULTS
            if key != "legionella" or hot_water.legionella_enabled
        ],
    )


class HotWaterTemperatureNumber(HotWaterEntity, RestoreNumber):
    """A hot water target of one phase."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 30.0
    _attr_native_max_value = 70.0
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, hot_water: HotWaterDevice, key: str) -> None:
        super().__init__(hot_water, f"hot_water_{key}")
        self._key = key
        self._attr_translation_key = f"hot_water_{key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data and last_data.native_value is not None:
            self._hot_water.set_number(self._key, last_data.native_value)

    @property
    def native_value(self) -> float:
        return self._hot_water.numbers[self._key]

    async def async_set_native_value(self, value: float) -> None:
        self._hot_water.set_number(self._key, value)


@dataclass(frozen=True)
class FlowNumberSpec:
    key: str
    unit: str
    minimum: float
    maximum: float
    step: float
    temperature: bool = False


FLOW_NUMBERS = (
    FlowNumberSpec("curve_outdoor_cold", UnitOfTemperature.CELSIUS, -30, 10, 0.5, True),
    FlowNumberSpec("curve_outdoor_warm", UnitOfTemperature.CELSIUS, 0, 30, 0.5, True),
    FlowNumberSpec("curve_flow_cold", UnitOfTemperature.CELSIUS, 25, 75, 0.5, True),
    FlowNumberSpec("curve_flow_warm", UnitOfTemperature.CELSIUS, 15, 60, 0.5, True),
    FlowNumberSpec("curve_bump", UnitOfTemperature.KELVIN, -10, 10, 0.5),
    FlowNumberSpec("curve_bump_width", UnitOfTemperature.KELVIN, 0, 20, 0.5),
    FlowNumberSpec("margin", UnitOfTemperature.KELVIN, 0, 10, 0.5),
    FlowNumberSpec("hysteresis", UnitOfTemperature.KELVIN, 0, 5, 0.1),
    FlowNumberSpec("hold_time_min", UnitOfTime.MINUTES, 5, 240, 5),
    FlowNumberSpec("charge_raise_max", UnitOfTemperature.KELVIN, 0, 15, 0.5),
    FlowNumberSpec("threshold", UnitOfTemperature.CELSIUS, 15, 50, 0.5, True),
)


class FlowNumber(FlowEntity, RestoreNumber):
    """A setting of the flow device: heating curve, margins, timing."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, flow: FlowDevice, spec: FlowNumberSpec) -> None:
        super().__init__(flow, f"flow_{spec.key}")
        self._key = spec.key
        self._attr_translation_key = f"flow_{spec.key}"
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        if spec.temperature:
            self._attr_device_class = NumberDeviceClass.TEMPERATURE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data and last_data.native_value is not None:
            self._flow.set_number(self._key, last_data.native_value)

    @property
    def native_value(self) -> float:
        return self._flow.numbers[self._key]

    async def async_set_native_value(self, value: float) -> None:
        self._flow.set_number(self._key, value)


class HeatingSeasonNumber(HeatingHubEntity, RestoreNumber):
    """One of the heating season limits, in hours."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        hub: HeatingHub,
        name: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> None:
        super().__init__(hub, f"heating_season_{name}")
        self._name = name
        self._default = default
        self._attr_translation_key = f"heating_season_{name}"
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        value = (
            last_data.native_value
            if last_data and last_data.native_value is not None
            else self._default
        )
        self._hub.set_season_threshold(self._name, value)

    @property
    def native_value(self) -> float:
        return getattr(self._hub, f"season_{self._name}")

    async def async_set_native_value(self, value: float) -> None:
        self._hub.set_season_threshold(self._name, value)


class ComfortTemperatureNumber(HeatingControllerEntity, RestoreNumber):
    _attr_translation_key = "comfort_temperature"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 19.0
    _attr_native_max_value = 24.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "comfort_temperature")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        value = (
            last_data.native_value
            if last_data and last_data.native_value is not None
            else DEFAULT_COMFORT_TEMPERATURE_C
        )
        await self._coordinator.async_set_comfort_temperature(value)

    @property
    def native_value(self) -> float:
        return self._coordinator.state.comfort_temperature_c

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_comfort_temperature(value)


class EcoOffsetNumber(HeatingControllerEntity, RestoreNumber):
    _attr_translation_key = "eco_offset"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = -3.0
    _attr_native_max_value = 0.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: HeatingRoomCoordinator) -> None:
        super().__init__(coordinator, "eco_offset")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        value = (
            last_data.native_value
            if last_data and last_data.native_value is not None
            else DEFAULT_ECO_TEMPERATURE_OFFSET_C
        )
        await self._coordinator.async_set_eco_temperature_offset(value)

    @property
    def native_value(self) -> float:
        return self._coordinator.state.eco_temperature_offset_c

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_eco_temperature_offset(value)

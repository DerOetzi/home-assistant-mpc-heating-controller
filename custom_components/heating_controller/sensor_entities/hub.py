"""Sensors of the house: lead flow, demand, write budget, reserve, sun."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfIrradiance,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.util import dt as dt_util

from ..const import (
    Surface,
)
from ..entity import (
    HeatingHubEntity,
)
from ..hub import HeatingHub
from ..lead_rooms import LeadValue
from .format import iso, rounded


class _LeadFlowSensor(HeatingHubEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def _lead(self) -> LeadValue:
        raise NotImplementedError

    @property
    def native_value(self) -> float | None:
        return self._lead().value_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lead = self._lead()
        return {"room": lead.room, "rooms": lead.rooms}


class LeadHoldFlowTemperatureSensor(_LeadFlowSensor):
    """Highest gated hold flow among the heating-limit lead rooms."""

    _attr_translation_key = "lead_hold_flow_temperature"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "lead_hold_flow_temperature")

    def _lead(self) -> LeadValue:
        return self._hub.leads.hold_flow


class HeatingDemandHoursSensor(HeatingHubEntity, SensorEntity):
    """Hours of the last 24 h in which the lead rooms' heating demand was on."""

    _attr_translation_key = "heating_demand_hours_24h"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 1

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_demand_hours_24h")

    @property
    def native_value(self) -> float:
        return self._hub.season.demand_hours_24h


class HeatingSeasonLastDemandSensor(HeatingHubEntity, SensorEntity):
    """Last time the 24 h demand was above the hold threshold."""

    _attr_translation_key = "heating_season_last_demand"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "heating_season_last_demand")

    @property
    def native_value(self) -> datetime | None:
        last_ts = self._hub.season.demand.last_demand_ts
        return None if last_ts is None else dt_util.utc_from_timestamp(last_ts)


class WriteBudgetSensor(HeatingHubEntity, SensorEntity):
    """Calls left for writes to the heat source's cloud today.

    With a rate-limit entity its value, otherwise the integration's own daily
    maximum minus its writes.
    """

    _attr_translation_key = "write_budget"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-upload-outline"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "write_budget")

    @property
    def native_value(self) -> int | None:
        remaining = self._hub.rate_limit_remaining
        if remaining is not None:
            return remaining
        if self._hub.rate_limit_remaining_entity:
            return None
        used, _dropped = self._hub.write_budget.counts(time.time())
        return max(0, self._hub.write_budget.max_per_day - used)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        budget = self._hub.write_budget
        used, dropped = budget.counts(time.time())
        return {
            "own_writes_today": used,
            "dropped_today": dropped,
            "reserve": budget.reserve,
            "max_writes_per_day": budget.max_per_day,
            "source": self._hub.rate_limit_remaining_entity,
        }


class _FlowReserveSensor(HeatingHubEntity, SensorEntity):
    """The heating curve minus a lead flow; a temperature difference in K."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:thermometer-chevron-up"

    def _reserve(self) -> float | None:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return self._reserve() is not None

    @property
    def native_value(self) -> float | None:
        return self._reserve()


class HoldFlowReserveSensor(_FlowReserveSensor):
    _attr_translation_key = "hold_flow_reserve"

    def __init__(self, hub: HeatingHub) -> None:
        super().__init__(hub, "hold_flow_reserve")

    def _reserve(self) -> float | None:
        return self._hub.hold_flow_reserve


class IrradianceSensor(HeatingHubEntity, SensorEntity):
    """Sun on one surface of the building, from Open-Meteo."""

    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfIrradiance.WATTS_PER_SQUARE_METER
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The state records the sun; the forecast is for reading now.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, hub: HeatingHub, surface: Surface) -> None:
        super().__init__(hub, f"irradiance_{surface}")
        self._surface = surface
        self._attr_translation_key = f"irradiance_{surface}"

    @property
    def available(self) -> bool:
        provider = self._hub.irradiance
        return provider is not None and provider.series.fresh(time.time())

    @property
    def native_value(self) -> float | None:
        provider = self._hub.irradiance
        if provider is None:
            return None
        return rounded(provider.series.value_at(self._surface, time.time()), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        provider = self._hub.irradiance
        if provider is None:
            return {}
        now_ts = time.time()
        geometry = next(
            (g for g in provider.geometries if g.surface == self._surface), None
        )
        return {
            "azimuth_deg": geometry.azimuth_deg if geometry else None,
            "tilt_deg": geometry.tilt_deg if geometry else None,
            "last_update": iso(provider.series.fetched_ts),
            "last_error": provider.last_error,
            "forecast": [
                [iso(start_ts), round(value, 1)]
                for start_ts, value in provider.series.forecast(
                    self._surface, now_ts, 24
                )
            ],
        }

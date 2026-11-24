"""The hub: the house-wide part of the integration.

Holds the rooms of one installation and derives the figures of the lead
rooms from them -- the hold and boost flow temperatures, the heating
demand and the reserve of the heat pump's curve.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_BUILDING_ROTATION,
    CONF_FLOW_CURVE_ENTITY,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEAD_FLOW,
    CONF_LEAD_HEATING_LIMIT,
    CONF_MAX_FLOW_GAP_MIN,
    CONF_MAX_WRITES_PER_DAY,
    CONF_OPAQUE_SURFACES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_RATE_LIMIT_REMAINING_ENTITY,
    CONF_RATE_LIMIT_RESERVE,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_WINDOW_SURFACES,
    CONF_WORKDAY_ENTITY,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_MAX_FLOW_GAP_MIN,
    DEFAULT_MAX_WRITES_PER_DAY,
    DEFAULT_RATE_LIMIT_RESERVE,
    DEFAULT_ROOF_TILT_DEG,
    DEFAULT_SEASON_HOLD_THRESHOLD_H,
    DEFAULT_SEASON_OFF_AFTER_H,
    DEFAULT_SEASON_ON_THRESHOLD_H,
    HEAT_SOURCE_ACTIVE_STATE,
    CurveEnd,
    RoofOrientation,
)
from .control.flow_correction import FlowCorrection
from .control.flow_feedback import DemandSample, FlowFeedback
from .control.heating_season import (
    HEAT_MODE,
    OFF_MODE,
    HeatingSeason,
    SeasonSwitcher,
)
from .control.hot_water import FlowChargeDetector
from .control.write_budget import WriteBudget, WritePriority
from .coordinator import HeatingRoomCoordinator
from .flow_device import FlowDevice
from .hot_water_device import HotWaterDevice
from .irradiance import IrradianceProvider, SurfaceGeometry, building_surfaces
from .listeners import Observable
from .startup_gate import StartupGate
from .state_reader import available_state, entity_float
from .store import HubStore

_LOGGER = logging.getLogger(__name__)

HUB_TICK = timedelta(minutes=1)
HUB_SAVE_DELAY_S = 300
SEASON_THRESHOLDS = ("on_threshold_h", "hold_threshold_h", "off_after_h")

# Like the min/max helpers the lead figures replace.
LEAD_ROUND_DIGITS = 1


@dataclass(frozen=True)
class LeadValue:
    """The highest value among the lead rooms and the room it comes from."""

    value_c: float | None
    room: str | None
    rooms: list[str]


class HeatingHub(Observable):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        self.hass = hass
        self.entry = entry
        self.rooms: dict[str, HeatingRoomCoordinator] = {}
        # Room id to name of rooms left out because their device is disabled.
        self.disabled_rooms: dict[str, str] = {}
        self._flow_threshold_entity: str | None = entry.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        self.flow_curve_entity: str | None = entry.data.get(CONF_FLOW_CURVE_ENTITY)
        self._unsub: list[Callable[[], None]] = []
        self.workday_entity: str | None = entry.data.get(CONF_WORKDAY_ENTITY)
        self.irradiance: IrradianceProvider | None = None
        self.rate_limit_remaining_entity: str | None = entry.data.get(
            CONF_RATE_LIMIT_REMAINING_ENTITY
        )
        self.write_budget = WriteBudget(
            reserve=int(
                entry.data.get(CONF_RATE_LIMIT_RESERVE, DEFAULT_RATE_LIMIT_RESERVE)
            ),
            max_per_day=int(
                entry.data.get(CONF_MAX_WRITES_PER_DAY, DEFAULT_MAX_WRITES_PER_DAY)
            ),
        )
        self._flow_temperature_entity: str | None = entry.data.get(
            CONF_FLOW_TEMPERATURE_ENTITY
        )
        self._heat_source_entity: str | None = entry.data.get(
            CONF_HEAT_SOURCE_CLIMATE_ENTITY
        )
        self._max_flow_gap_s = (
            float(entry.data.get(CONF_MAX_FLOW_GAP_MIN, DEFAULT_MAX_FLOW_GAP_MIN)) * 60
        )
        # A start counts as a fresh value; the gap runs from here.
        self.flow_seen_ts = time.time()
        self.season = HeatingSeason()
        self.season_switcher = SeasonSwitcher()
        # Switch the heat source by the season criterion; off after a hand switch.
        self.season_auto_on = True
        self.flow_feedback = FlowFeedback()
        self.flow_correction = FlowCorrection()
        self.charge_detector = FlowChargeDetector()
        # Nothing computes or writes before the inputs are there.
        self.startup_gate = StartupGate(hass, self._async_start)
        self.season_on_threshold_h = DEFAULT_SEASON_ON_THRESHOLD_H
        self.season_hold_threshold_h = DEFAULT_SEASON_HOLD_THRESHOLD_H
        self.season_off_after_h = DEFAULT_SEASON_OFF_AFTER_H
        self._store = HubStore(hass, entry.entry_id)
        self._outdoor_entity: str | None = entry.data.get(
            CONF_OUTDOOR_TEMPERATURE_ENTITY
        )
        self.flow: FlowDevice | None = None
        self.hot_water: HotWaterDevice | None = None
        # The house-wide comfort switch; off e.g. on holiday.
        self.comfort_on = True
        # PV surplus; set by hand or by an automation.
        self.pv_boost_on = False

    def add_room(self, coordinator: HeatingRoomCoordinator) -> None:
        self.rooms[coordinator.room_id] = coordinator
        coordinator.house = self
        self._unsub.append(coordinator.async_add_listener(self._notify_listeners))

    def used_surfaces(self) -> list[SurfaceGeometry]:
        """The building's surfaces some room has windows or walls on."""
        used = {
            surface
            for room in self.rooms.values()
            for key in (CONF_WINDOW_SURFACES, CONF_OPAQUE_SURFACES)
            for surface in room.data.get(key) or []
        }
        return [
            geometry
            for geometry in building_surfaces(
                float(self.entry.data.get(CONF_BUILDING_ROTATION, 0.0)),
                RoofOrientation(
                    self.entry.data.get(CONF_ROOF_ORIENTATION, RoofOrientation.NONE)
                ),
                float(self.entry.data.get(CONF_ROOF_TILT, DEFAULT_ROOF_TILT_DEG)),
            )
            if geometry.surface in used
        ]

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        self.write_budget.restore(stored.get("write_budget", {}))
        self.season.restore(stored.get("heating_season", {}))
        self.flow_feedback.restore(stored.get("flow_feedback", {}))
        self.flow_correction.restore(stored.get("flow_correction", {}))
        surfaces = self.used_surfaces()
        if surfaces:
            self.irradiance = IrradianceProvider(
                self.hass, surfaces, self._notify_listeners
            )
            self.irradiance.async_start()
            for room in self.rooms.values():
                room.gain_clock.set_sun_provider(self.irradiance, self.workday_entity)
        tracked = [
            entity_id
            for entity_id in (
                self._flow_threshold_entity,
                self.flow_curve_entity,
                self._heat_source_entity,
                self.rate_limit_remaining_entity,
            )
            if entity_id
        ]
        if tracked:
            self._unsub.append(
                async_track_state_change_event(
                    self.hass, tracked, self._async_handle_state_change
                )
            )
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_tick, HUB_TICK)
        )
        gate = self.startup_gate
        for room in self.rooms.values():
            gate.require(room.required_entities)
        gate.require([self.flow_curve_entity, self._heat_source_entity])
        if self.flow is not None:
            gate.require([self.flow.actuator_entity])

    def async_start_gate(self) -> None:
        """Called once the platforms are set up: on a reload the gate opens at
        once, so the entities that restore setpoints and switches must be in."""
        self.startup_gate.async_start()

    @property
    def started(self) -> bool:
        return self.startup_gate.is_open

    async def _async_start(self) -> None:
        for room in self.rooms.values():
            await room.async_start()
        self._tick(time.time())
        self._notify_listeners()

    def async_unload(self) -> None:
        self.startup_gate.async_stop()
        if self.hot_water is not None:
            self.hot_water.async_unload()
        if self.irradiance is not None:
            self.irradiance.async_stop()
        for unsub in self._unsub:
            unsub()
        self._unsub = []

    async def async_flush(self) -> None:
        """Write the hub's state now, e.g. before an unload."""
        await self._store.async_save(self._export())
        if self.hot_water is not None:
            await self.hot_water.async_flush()

    def _export(self) -> dict[str, Any]:
        return {
            "write_budget": self.write_budget.export(),
            "heating_season": self.season.export(),
            "flow_feedback": self.flow_feedback.export(),
            "flow_correction": self.flow_correction.export(),
        }

    @callback
    def _async_tick(self, _now: Any) -> None:
        self._tick(time.time())
        self._notify_listeners()

    def _tick(self, now_ts: float) -> None:
        if not self.started:
            return
        if (
            self._flow_temperature_entity
            and entity_float(self.hass, self._flow_temperature_entity) is not None
        ):
            self.flow_seen_ts = now_ts
        self.update_charge()
        self.season.update(self.heating_demand, now_ts, self.season_hold_threshold_h)
        self._switch_season(now_ts)
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)
        if self.hot_water is not None:
            self.hass.async_create_task(self.hot_water.async_evaluate())
        if self.flow is not None:
            self.hass.async_create_task(self.flow.async_evaluate())

    # --- Flow control ------------------------------------------------------

    def set_flow(self, flow: FlowDevice) -> None:
        """Hand the flow device the actuator; rooms take its threshold."""
        self.flow = flow

    # --- What the rooms ask the house for ----------------------------------

    def flow_threshold_override_c(self) -> float | None:
        """The flow device's threshold; None without a flow device."""
        return None if self.flow is None else self.flow.threshold_c

    def curve_outdoor_ends_c(self) -> tuple[float, float] | None:
        """The heating curve's outdoor end points (cold, warm); None without."""
        if self.flow is None:
            return None
        return (
            self.flow.numbers["curve_outdoor_cold"],
            self.flow.numbers["curve_outdoor_warm"],
        )

    def rooms_catching_up(self) -> bool:
        """The rooms catch up after a pause of the room heating."""
        return self.flow_feedback.catching_up(time.time())

    def reset_flow_feedback(self) -> None:
        """Drop the valve feedback to zero, e.g. after a change of its rules."""
        self.flow_feedback.reset(time.time())
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)

    def set_hot_water(self, hot_water: HotWaterDevice) -> None:
        self.hot_water = hot_water

    def update_charge(self) -> None:
        """Take the flow, the heat source's aim and the tank detector."""
        self.charge_detector.update(
            self.measured_flow_c,
            self.effective_curve_c,
            self.hot_water is not None and self.hot_water.charging,
            time.time(),
        )

    def room_hot_water_charge(self) -> tuple[bool, bool]:
        """The charge as rooms take it, and whether it is the house's own.

        With the measured flow and the heat source's aim the house decides
        for all rooms and the feedback alike. Without, each room adds what
        the hot water device sees to its own detection on its flow.
        """
        if self.charge_detector.decisive:
            return self.charge_detector.charging, True
        return self.hot_water is not None and self.hot_water.charging, False

    @property
    def outdoor_temperature_c(self) -> float | None:
        return entity_float(self.hass, self._outdoor_entity)

    @property
    def charge_flow_c(self) -> float | None:
        """Flow the rooms need around a planned hot-water charge; not built yet."""
        return None

    # --- Writes to the heat source -----------------------------------------

    @property
    def rate_limit_remaining(self) -> int | None:
        """Calls the heat source's cloud still allows today, if known."""
        value = entity_float(self.hass, self.rate_limit_remaining_entity)
        return None if value is None else int(value)

    def request_write(self, priority: WritePriority) -> bool:
        """Whether a write to the heat source may go out now; counts it."""
        allowed = self.write_budget.request(
            priority, self.rate_limit_remaining, time.time()
        )
        self._store.async_delay_save(self._export, HUB_SAVE_DELAY_S)
        self._notify_listeners()
        return allowed

    # --- Heat source reachable ---------------------------------------------

    @property
    def heat_source_reachable(self) -> bool:
        """False once the measured flow has been missing longer than the gap."""
        return time.time() - self.flow_seen_ts <= self._max_flow_gap_s

    # --- Heating season ----------------------------------------------------

    @property
    def heat_source_entity(self) -> str | None:
        """The heat source's climate entity, switched heat/off by the hub."""
        return self._heat_source_entity

    async def async_set_season_auto(self, value: bool) -> None:
        """Switch the automatic heat/off of the heat source."""
        self.season_auto_on = value
        if value:
            self._switch_season(time.time())
        self._notify_listeners()

    def _switch_season(self, now_ts: float) -> None:
        """Command the heat source to the mode the season criterion asks for."""
        if (
            not self.season_auto_on
            or not self.started
            or not self._heat_source_entity
            or self.heating_on is None
            or not self.heat_source_reachable
        ):
            return
        mode = SeasonSwitcher.wanted(
            self.season_switch_on_recommended, self.season_switch_off_recommended
        )
        if mode is None or not self.season_switcher.due(mode, now_ts):
            return
        self.season_switcher.sent(mode, now_ts)
        self.request_write(WritePriority.REQUIRED)
        self.hass.async_create_task(self._async_command_heat_source(mode))

    async def _async_command_heat_source(self, mode: str) -> None:
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._heat_source_entity, "hvac_mode": mode},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Switching %s to %s failed: %s", self._heat_source_entity, mode, err
            )

    @callback
    def _heat_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """A change between heat and off not commanded by the hub is by hand."""
        old = event.data["old_state"]
        new = event.data["new_state"]
        modes = (HEAT_MODE, OFF_MODE)
        if (
            old is None
            or new is None
            or old.state not in modes
            or new.state not in modes
            or old.state == new.state
        ):
            return
        if not self.season_switcher.take_change(new.state, time.time()):
            if self.season_auto_on:
                _LOGGER.info(
                    "%s switched to %s by hand; automatic heating season off",
                    self._heat_source_entity,
                    new.state,
                )
            self.season_auto_on = False

    @property
    def heating_on(self) -> bool | None:
        """Whether the heat source heats; None without or with an empty entity."""
        state = available_state(self.hass, self._heat_source_entity)
        if state is None:
            return None
        return state.state == HEAT_SOURCE_ACTIVE_STATE

    @property
    def demand_hours_24h(self) -> float:
        return round(self.season.hours(time.time()), 2)

    @property
    def season_switch_on_recommended(self) -> bool:
        return self.season.switch_on_recommended(
            time.time(), self.season_on_threshold_h, self.heating_on
        )

    @property
    def season_switch_off_recommended(self) -> bool:
        return self.season.switch_off_recommended(
            time.time(), self.season_off_after_h, self.heating_on
        )

    async def async_set_comfort(self, value: bool) -> None:
        """Switch house-wide comfort; rooms that follow it recompute."""
        self.comfort_on = value
        for room in self.rooms.values():
            await room.async_set_house_comfort(value)
        self._notify_listeners()

    async def async_set_pv_boost(self, value: bool) -> None:
        """Switch the PV surplus; every room recomputes."""
        self.pv_boost_on = value
        for room in self.rooms.values():
            await room.async_set_pv_boost(value)
        self._notify_listeners()

    def set_season_threshold(self, name: str, value: float) -> None:
        """Set one of the season numbers by its short name."""
        if name not in SEASON_THRESHOLDS:
            raise ValueError(f"Unknown heating season threshold {name}")
        setattr(self, f"season_{name}", float(value))
        self._notify_listeners()

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] == self._heat_source_entity:
            self._heat_source_changed(event)
        self._notify_listeners()

    def _lead(
        self, flag: str, value: Callable[[HeatingRoomCoordinator], float | None]
    ) -> LeadValue:
        leads = [room for room in self.rooms.values() if room.data.get(flag)]
        best: tuple[float, str] | None = None
        for room in leads:
            room_value = value(room)
            if room_value is not None and (best is None or room_value > best[0]):
                best = (room_value, room.room_name)
        names = [room.room_name for room in leads]
        if best is None:
            return LeadValue(None, None, names)
        return LeadValue(round(best[0], LEAD_ROUND_DIGITS), best[1], names)

    @property
    def lead_hold_flow(self) -> LeadValue:
        """Flow the heating-limit lead rooms need to hold their setpoint."""
        return self._lead(
            CONF_LEAD_HEATING_LIMIT,
            lambda room: room.normal_gated_hold_flow_temperature_c,
        )

    @property
    def lead_feedforward_flow(self) -> LeadValue:
        """Hold flow of the flow lead rooms, ungated: the feedforward."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.normal_hold_flow_temperature_c
        )

    @property
    def lead_boost_flow(self) -> LeadValue:
        """Recovery flow of the flow lead rooms that recover after a mode change."""
        return self._lead(CONF_LEAD_FLOW, lambda room: room.flow_boost_temperature_c)

    @property
    def design_flow_c(self) -> float | None:
        """Highest design flow of the flow lead rooms' radiators."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.design_flow_temperature_c
        ).value_c

    def feedback_samples(self) -> list[DemandSample]:
        """Valve demand of the flow lead rooms the feedback may use."""
        return [
            sample
            for room in self.rooms.values()
            if room.data.get(CONF_LEAD_FLOW)
            and (sample := room.feedback_sample) is not None
        ]

    @property
    def measured_flow_c(self) -> float | None:
        return entity_float(self.hass, self._flow_temperature_entity)

    @property
    def hot_water_charging(self) -> bool:
        """A hot-water charge takes the flow.

        One signal from the flow against the heat source's aim; without it,
        as any room or the device sees it.
        """
        if self.charge_detector.decisive:
            return self.charge_detector.charging
        if self.hot_water is not None and self.hot_water.charging:
            return True
        return any(room.mpc.hot_water_charging for room in self.rooms.values())

    def lead_curve_hold_flow(self, end: CurveEnd) -> LeadValue:
        """Flow the flow lead rooms need at one end of the curve, at comfort."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.hold_flow_at_curve_end_c(end)
        )

    def hold_flow_curves(self, outdoor_values: list[float]) -> list[dict[str, Any]]:
        """Each room's comfort hold flow over the outdoor values, for the card.

        Not stepped up to the control's grid, so the curves come out smooth.
        """
        return [
            {
                "name": room.room_name,
                "lead": bool(room.data.get(CONF_LEAD_FLOW)),
                "flow_c": [
                    None
                    if (flow_c := room.comfort_hold_flow_c(value, round_up=False))
                    is None
                    else round(flow_c, 1)
                    for value in outdoor_values
                ],
            }
            for room in self.rooms.values()
        ]

    @property
    def flow_threshold_c(self) -> float | None:
        """The threshold the demand compares to; None while its entity is empty."""
        if self.flow is not None:
            return self.flow.threshold_c
        if self._flow_threshold_entity:
            return entity_float(self.hass, self._flow_threshold_entity)
        return self.entry.data.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C)

    @property
    def heating_demand(self) -> bool | None:
        """The lead rooms need more than the heat pump's lowest useful flow."""
        hold_c = self.lead_hold_flow.value_c
        threshold_c = self.flow_threshold_c
        if hold_c is None or threshold_c is None:
            return None
        return hold_c >= threshold_c

    def _reserve(self, lead: LeadValue) -> float | None:
        # A lead value of 0 means all lead rooms sit in their surplus buffer;
        # the curve minus 0 would say nothing.
        if lead.value_c is None or lead.value_c <= 0:
            return None
        curve_c = self.effective_curve_c
        if curve_c is None:
            return None
        return round(curve_c - lead.value_c, LEAD_ROUND_DIGITS)

    @property
    def has_flow_curve(self) -> bool:
        return self.flow is not None or bool(self.flow_curve_entity)

    @property
    def effective_curve_c(self) -> float | None:
        """The flow the heat source aims at, from the flow device or entity."""
        if self.flow is not None:
            return self.flow.effective_curve_c
        if not self.flow_curve_entity:
            return None
        return entity_float(self.hass, self.flow_curve_entity)

    @property
    def hold_flow_reserve(self) -> float | None:
        return self._reserve(self.lead_hold_flow)

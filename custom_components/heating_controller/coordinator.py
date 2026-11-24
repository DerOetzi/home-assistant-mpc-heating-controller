from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_MAX_SENSOR_AGE,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_ROOM_COMFORT_SWITCH,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    CONF_WINDOW_CONTACT_ENTITIES,
    DEFAULT_FLOW_THRESHOLD_C,
    LEARNING_CYCLE_INTERVAL_MINUTES,
    SENSOR_POLL_INTERVAL_MINUTES,
    CurveEnd,
    HeatMode,
    RoomSupply,
    SupplyReason,
)
from .control.flow_feedback import DemandSample, holds, wants_more
from .controller.mpc.controller import (
    RoomMpcController,
)
from .controller.mpc.models.gains import GainBreakdown
from .controller.mpc.results import RoomMpcError, RoomMpcResult
from .controller.mpc.sensors import VALVE_POSITION_MAX_AGE_S
from .controller.mpc.types import (
    LearningFactors,
    RoomContext,
)
from .controller.state import HeatingStateController
from .controller.trv_inputs import (
    LOAD_MEAN_INTERVAL_S,
)
from .listeners import Observable
from .night import NightWindow
from .profiles import TrvBinding
from .room.adaptation import AdaptationWatch
from .room.config import (
    build_trv_configs,
    flow_gate_config,
    state_config,
    thermal_config,
)
from .room.flow_boost import FlowBoost
from .room.gain_clock import GainClock, local_day_at
from .room.heat_availability import HeatAvailability
from .room.house import DetachedHouse, House
from .room.learning_inputs import LearningInputs
from .room.load_sharing import LoadSharing
from .room.persistence import RoomPersistence
from .room.trv_writer import TrvWriter
from .startup_gate import entity_ready
from .state_reader import (
    attribute_float,
    available_state,
    entity_float,
    entity_is_on,
    state_float,
    state_is_on,
)

_LOGGER = logging.getLogger(__name__)

RECOMPUTE_DEBOUNCE_S = 1.0

LEARNING_CYCLE_INTERVAL = timedelta(minutes=LEARNING_CYCLE_INTERVAL_MINUTES)

SENSOR_POLL_INTERVAL = timedelta(minutes=SENSOR_POLL_INTERVAL_MINUTES)

WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S = 30 * 60
WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S = 10 * 60


class HeatingRoomCoordinator(Observable):
    def __init__(
        self,
        hass: HomeAssistant,
        hub_entry: ConfigEntry,
        room_id: str,
        data: Mapping[str, Any],
        trvs: list[TrvBinding],
    ) -> None:
        """One room of the hub.

        room_id is the room's subentry id; stores, unique ids and the device
        hang on it. data holds the hub's settings merged with the room's.
        """
        super().__init__()
        self.hass = hass
        self.hub_entry = hub_entry
        self.room_id = room_id
        self.data = data
        self.room_name: str = self.data[CONF_ROOM_NAME]

        self._trvs = trvs
        self._trv_entity_ids: list[str] = [trv.climate_entity for trv in trvs]
        self._valve_position_entities: list[str | None] = [
            trv.valve_position_entity for trv in trvs
        ]

        self._heat_source_climate_entity: str | None = self.data.get(
            CONF_HEAT_SOURCE_CLIMATE_ENTITY
        )
        self._flow_temperature_entity: str = self.data[CONF_FLOW_TEMPERATURE_ENTITY]
        self._heat_availability = HeatAvailability()
        self._flow_threshold_entity: str | None = self.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        # The hub, once it took the room in.
        self.house: House = DetachedHouse()

        self.state = HeatingStateController(state_config(self.data))
        # Until the switches restore: house on, the room's own switch off.
        self.follows_house_comfort: bool = self.data.get(
            CONF_USE_HOUSE_COMFORT_CONDITIONS, True
        )
        self.has_room_comfort_switch: bool = self.data.get(
            CONF_ROOM_COMFORT_SWITCH, False
        )
        self._set_initial_comfort()
        self.gain_clock = GainClock(hass)
        self.mpc = self._build_mpc(trvs)
        self.learning_inputs = LearningInputs(
            hass, self.mpc, self.data.get(CONF_LEARNING_BLOCK_ENTITY)
        )
        self.persistence = RoomPersistence(hass, self.room_name, room_id, self.mpc)
        # Computes and writes only once the hub's startup gate opened.
        self.running = False

        self.blocked = False
        self.trv_active = True
        self.last_result: RoomMpcResult | None = None
        self.adaptation = AdaptationWatch(hass, trvs)
        self.trv_writer = TrvWriter(hass, trvs, self.adaptation)
        self.load_sharing = LoadSharing(hass, trvs, self.adaptation)
        self.normal_result: RoomMpcResult | None = None
        self.normal_heat_mode: HeatMode | None = None
        self.normal_target_temperature_c: float | None = None
        # The setpoint handed to the TRVs: the active mode's, or frost
        # protection for an open window.
        self.mode_target_temperature_c: float | None = None
        self.flow_boost = FlowBoost()
        self._started_ts = 0.0

        self._unsub: list[Callable[[], None]] = []
        # Entities report in bursts (a TRV its temperature and valve position,
        # the heat pump flow and outdoor temperature): one recompute per burst.
        self._pending_recompute: asyncio.Task[None] | None = None
        # A recompute awaits TRV writes; a second one in between would compute
        # and write on half-updated state.
        self._recompute_lock = asyncio.Lock()
        self._night = NightWindow(hass)
        self._state_handlers = self._build_state_handlers()

    def _set_initial_comfort(self) -> None:
        # None where the room has no such condition.
        if self.follows_house_comfort:
            self.state.set_house_comfort(True)
        if self.has_room_comfort_switch:
            self.state.set_room_comfort(False)

    def _build_mpc(self, trvs: list[TrvBinding]) -> RoomMpcController:
        mpc = RoomMpcController(
            thermal_config=thermal_config(self.data, self.flow_threshold_c),
            trvs=build_trv_configs(trvs),
            max_sensor_age_s=self.data[CONF_MAX_SENSOR_AGE],
            room_sensor_configured=bool(self.data.get(CONF_ROOM_SENSOR_ENTITY)),
            flow_gate_config=flow_gate_config(self.data),
        )
        mpc.set_sun_condition(self.is_sun_condition_met)
        mpc.set_gain_context(
            self.gain_clock.profile_index_at,
            local_day_at,
            self.gain_clock.fresh_sun_series,
        )
        return mpc

    def current_gain(self) -> GainBreakdown:
        now_ts = time.time()
        gains = self.mpc.gain_model
        return gains.breakdown(
            self.gain_clock.profile_index_at(now_ts),
            gains.sun_regressors(self.gain_clock.fresh_sun_series(), now_ts),
        )

    async def async_setup(self) -> None:
        self._started_ts = time.time()
        await self.persistence.async_restore()

        for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            self.state.update_window_state(
                entity_id, entity_is_on(self.hass, entity_id)
            )
        self.mpc.set_window_open(self.state.is_window_open, time.time())
        self._update_window_unknown()

        self.learning_inputs.start(self._started_ts)
        self._refresh_temperature_inputs()
        self.learning_inputs.update_learning_paused(time.time())

        self._unsub.append(
            async_track_state_change_event(
                self.hass, self._tracked_entities(), self._async_handle_state_change
            )
        )
        self._unsub.append(
            async_track_entity_registry_updated_event(
                self.hass,
                [entity_id for trv in self._trvs for entity_id in trv.entity_ids],
                self._async_handle_trv_entity_registry_update,
            )
        )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._async_handle_learning_cycle, LEARNING_CYCLE_INTERVAL
            )
        )
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._async_handle_sensor_poll, SENSOR_POLL_INTERVAL
            )
        )
        if self.load_sharing.possible:
            self._unsub.append(
                async_track_time_interval(
                    self.hass,
                    self._async_handle_load_sharing,
                    timedelta(seconds=LOAD_MEAN_INTERVAL_S),
                )
            )

    def _tracked_entities(self) -> list[str]:
        """Every entity whose change can change what the room computes."""
        tracked = [*self._state_handlers]
        for trv in self._trvs:
            tracked.extend(
                entity_id
                for entity_id in (
                    trv.adaptation_status_entity,
                    trv.mounted_mode_entity,
                    # A covered radiator wants the room sensor's reading more
                    # often.
                    trv.radiator_covered_entity
                    if trv.external_temperature_entity
                    else None,
                )
                if entity_id
            )
        tracked.extend(
            entity_id
            for entity_id in (
                self._heat_source_climate_entity,
                self._flow_threshold_entity,
            )
            if entity_id
        )
        # Written entities that come back after an outage get their value again.
        tracked.extend(self._rewrite_entities)
        return list(dict.fromkeys(tracked))

    @property
    def required_entities(self) -> list[str]:
        """What the room cannot start without: what it writes and reads to write."""
        entity_ids = [
            entity_id
            for trv in self._trvs
            for entity_id in (
                trv.climate_entity,
                trv.heat_available_entity,
                trv.adaptation_status_entity,
                trv.external_temperature_entity,
                trv.load_balancing_entity,
                trv.load_room_mean_entity,
            )
            if entity_id
        ]
        entity_ids.append(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        entity_ids.append(self._flow_temperature_entity)
        for entity_id in (
            self.data.get(CONF_ROOM_SENSOR_ENTITY),
            self._heat_source_climate_entity,
        ):
            if entity_id:
                entity_ids.append(entity_id)
        return entity_ids

    @property
    def _rewrite_entities(self) -> list[str]:
        return [
            entity_id
            for trv in self._trvs
            for entity_id in (
                trv.external_temperature_entity,
                trv.load_balancing_entity,
                trv.load_room_mean_entity,
            )
            if entity_id
        ]

    async def async_start(self) -> None:
        """The hub's inputs are there: compute and write from now on."""
        self.running = True
        self._refresh_temperature_inputs()
        await self._async_recompute()
        await self._async_update_load_sharing()

    def async_unload(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if self._pending_recompute is not None:
            self._pending_recompute.cancel()
            self._pending_recompute = None
        self.mpc.destroy()

    def _refresh_temperature_inputs(self) -> None:
        for index, entity_id in enumerate(self._trv_entity_ids):
            state = self.hass.states.get(entity_id)
            self.mpc.set_trv_temperature(
                index, attribute_float(state, "current_temperature")
            )
        for index in range(len(self._valve_position_entities)):
            self._update_valve_position(index)

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if room_sensor_entity:
            self._update_room_sensor(self.hass.states.get(room_sensor_entity))

        self.mpc.set_outdoor_temperature(
            entity_float(self.hass, self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        )
        self._update_flow_temperature(
            self.hass.states.get(self._flow_temperature_entity)
        )

    @callback
    def _async_handle_trv_entity_registry_update(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        """Bind the TRVs afresh once one of their entities is renamed or removed."""
        if event.data["action"] == "update" and "entity_id" not in event.data.get(
            "changes", {}
        ):
            return
        self.hass.config_entries.async_schedule_reload(self.hub_entry.entry_id)

    async def _async_handle_sensor_poll(self, _now: Any) -> None:
        self._refresh_temperature_inputs()
        await self._async_recompute()

    def _update_flow_temperature(self, state: State | None) -> None:
        self.learning_inputs.update_flow(state, time.time())

    def _trv_reported_ts(self, index: int) -> float | None:
        """When the TRV last reported its temperature or valve opening."""
        reported = [
            state.last_reported.timestamp()
            for entity_id in (
                self._trv_entity_ids[index],
                self._valve_position_entities[index],
            )
            if (state := available_state(self.hass, entity_id)) is not None
        ]
        return max(reported, default=None)

    def _update_room_sensor(self, state: State | None) -> None:
        # The poll hands in an unchanged state again: its age is that of the
        # sensor's last report, or a sensor that hangs would never go stale.
        self.mpc.set_room_sensor_temperature(
            state_float(state),
            state.last_reported.timestamp() if state is not None else None,
        )

    def _update_valve_position(self, index: int) -> None:
        entity_id = self._valve_position_entities[index]
        if entity_id:
            self.mpc.set_valve_position(
                index, entity_float(self.hass, entity_id), self._trv_reported_ts(index)
            )

    def _trv_demand_pct(self, index: int) -> float | None:
        """The TRV's valve opening; None if unknown or reported too long ago."""
        entity_id = self._valve_position_entities[index]
        reported_ts = self._trv_reported_ts(index)
        if (
            not entity_id
            or reported_ts is None
            or time.time() - reported_ts > VALVE_POSITION_MAX_AGE_S
        ):
            return None
        return entity_float(self.hass, entity_id)

    def _update_window_unknown(self) -> None:
        # A contact that is unavailable counts as closed for control, but a
        # jump stretch must not start on it: the window may well be open.
        self.mpc.set_window_unknown(
            any(
                (state := self.hass.states.get(entity_id)) is None
                or state.state not in (STATE_ON, STATE_OFF)
                for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, [])
            )
        )

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self._rewrite_entities:
            self._handle_rewrite_entity(entity_id, event.data["old_state"])
        self._apply_state_change(entity_id, event.data["new_state"])
        if self._pending_recompute is None:
            self._pending_recompute = self.hass.async_create_task(
                self._async_recompute_after_burst()
            )

    @callback
    def _handle_rewrite_entity(self, entity_id: str, old_state: State | None) -> None:
        """A written entity back from an outage gets its value at once."""
        if old_state is not None and old_state.state != STATE_UNAVAILABLE:
            return
        if not self.running or not entity_ready(self.hass, entity_id):
            return
        self.trv_writer.resend_external(entity_id)
        if self.load_sharing.owns(entity_id):
            self.hass.async_create_task(self._async_update_load_sharing())

    async def _async_recompute_after_burst(self) -> None:
        await asyncio.sleep(RECOMPUTE_DEBOUNCE_S)
        # Cleared first: a change that arrives while this one computes gets
        # its own recompute.
        self._pending_recompute = None
        await self._async_recompute()

    def _build_state_handlers(self) -> dict[str, Callable[[State | None], None]]:
        """Per tracked entity what a new state of it updates.

        Listed in precedence order: an entity in two roles takes the first.
        """
        handlers: dict[str, Callable[[State | None], None]] = {}
        for index, entity_id in enumerate(self._trv_entity_ids):
            handlers.setdefault(entity_id, partial(self._on_trv_state, index))
        for index, valve_entity in enumerate(self._valve_position_entities):
            if valve_entity:
                handlers.setdefault(valve_entity, partial(self._on_valve_state, index))
        if room_sensor := self.data.get(CONF_ROOM_SENSOR_ENTITY):
            handlers.setdefault(room_sensor, self._update_room_sensor)
        handlers.setdefault(
            self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY],
            lambda state: self.mpc.set_outdoor_temperature(state_float(state)),
        )
        handlers.setdefault(
            self._flow_temperature_entity, self._update_flow_temperature
        )
        for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            handlers.setdefault(entity_id, partial(self._on_window_state, entity_id))
        if block_entity := self.learning_inputs.learning_block_entity:
            handlers.setdefault(
                block_entity, self.learning_inputs.update_learning_block
            )
        return handlers

    def _apply_state_change(self, entity_id: str, new_state: State | None) -> None:
        handler = self._state_handlers.get(entity_id)
        if handler is not None:
            handler(new_state)

    def _on_trv_state(self, index: int, state: State | None) -> None:
        self.mpc.set_trv_temperature(
            index, attribute_float(state, "current_temperature")
        )
        # The TRV reported: its unchanged valve opening is still current.
        self._update_valve_position(index)

    def _on_valve_state(self, index: int, _state: State | None) -> None:
        self._update_valve_position(index)

    def _on_window_state(self, entity_id: str, state: State | None) -> None:
        was_open = self.state.is_window_open
        _, is_open = self.state.update_window_state(entity_id, state_is_on(state))
        self.mpc.set_window_open(is_open, time.time())
        self._update_window_unknown()
        if was_open and not is_open:
            self.mpc.suppress_learning_for_interval(
                WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S,
                WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S,
            )

    async def _async_handle_learning_cycle(self, _now: Any) -> None:
        if not self.running:
            return
        self.mpc.run_learning_cycle()
        await self.persistence.async_save_learning()
        self._notify_listeners()

    async def async_recalibrate(
        self,
        ua_factor: float | None,
        capacity_factor: float | None,
        reset_gains: bool = False,
    ) -> None:
        """Set the factors; one left out keeps its learned value."""
        if reset_gains:
            self.mpc.reset_gains()
            await self.persistence.async_save_gains()
            _LOGGER.info("Reset learned gains for room %s", self.room_name)
        if ua_factor is None and capacity_factor is None:
            self._notify_listeners()
            return
        factors = LearningFactors(
            ua_factor=self.mpc.learned_ua_factor if ua_factor is None else ua_factor,
            capacity_factor=(
                self.mpc.learned_capacity_factor
                if capacity_factor is None
                else capacity_factor
            ),
            # A new mass capacity starts the airing one afresh from it.
            vent_capacity_factor=(
                self.mpc.learning.learned_vent_capacity_factor
                if capacity_factor is None
                else None
            ),
        )
        self.mpc.recalibrate_learning_factors(factors)
        await self.persistence.async_save_factors(factors)
        _LOGGER.info(
            "Recalibrated learning factors for room %s: ua_factor=%s capacity_factor=%s",
            self.room_name,
            factors.ua_factor,
            factors.capacity_factor,
        )
        self._notify_listeners()

    def is_sun_condition_met(self, timestamp: float) -> bool:
        return self._night.is_night(timestamp)

    @property
    def flow_threshold_c(self) -> float:
        """The lowest flow temperature the heat source can usefully deliver.

        Read from the linked entity on every access so the limit can be tried
        out on the heat pump by changing a single helper. Without a linked
        entity, or while it reports nothing usable, the hub's fixed threshold
        keeps the room controllable instead of blocking it.
        """
        override_c = self.house.flow_threshold_override_c()
        if override_c is not None:
            return override_c
        fixed = self.data.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C)
        if not self._flow_threshold_entity:
            return fixed
        value = entity_float(self.hass, self._flow_threshold_entity)
        if value is None:
            return fixed
        return value

    async def _async_recompute(self) -> None:
        async with self._recompute_lock:
            await self._async_recompute_locked()

    async def _async_recompute_locked(self) -> None:
        if not self.running:
            return
        self.state.set_active_heat_mode(
            self.state.desired_automatic_heat_mode(self.blocked)
        )
        now_ts = time.time()
        self.learning_inputs.update_learning_paused(now_ts)
        self.trv_active = self._heat_available(now_ts)
        self.mpc.apply_context(self._room_context(now_ts))

        display_mode = self.state.resolve_display_mode(self.blocked)
        mode_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(display_mode)
        )
        self.mode_target_temperature_c = mode_target
        normal_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(self.state.current_heat_mode)
        )
        self.flow_boost.update(
            normal_target,
            self.normal_result.input.room_temp_c if self.normal_result else None,
            now_ts,
        )
        await self.trv_writer.async_write_setpoints(mode_target, now_ts)
        await self.trv_writer.async_write_external_temperatures(
            self._room_sensor_temperature_c(),
            bool(self.data.get(CONF_ROOM_SENSOR_ENTITY)),
            now_ts,
        )

        self.normal_heat_mode = self.state.current_heat_mode
        self.normal_target_temperature_c = normal_target
        normal = self.mpc.preview(normal_target)
        self.normal_result = normal if isinstance(normal, RoomMpcResult) else None

        # Frost protection for an open window is no mode change for the
        # learner.
        actual = self.mpc.compute(mode_target, mode_target_temperature_c=normal_target)
        if isinstance(actual, RoomMpcResult):
            self.last_result = actual
        else:
            _LOGGER.debug("MPC compute failed for room %s: %s", self.room_name, actual)

        await self.trv_writer.async_apply_heat_available(self.trv_active, time.time())
        await self.persistence.async_save_learning()
        await self.persistence.async_save_flow_gate()
        self._notify_listeners()

    def _heat_available(self, now_ts: float) -> bool:
        if not self._heat_source_climate_entity:
            # Nothing tells when the heat is gone; the TRVs find out themselves.
            return True
        return self._heat_availability.update(
            self.hass.states.get(self._heat_source_climate_entity),
            self.current_flow_temperature_c,
            self.flow_threshold_c,
            now_ts,
        )

    def _room_context(self, now_ts: float) -> RoomContext:
        charging, decisive = self.house.room_hot_water_charge()
        return RoomContext(
            now_ts=now_ts,
            flow_threshold_c=self.flow_threshold_c,
            heat_available=self.trv_active,
            trv_adapting=self.adaptation.any_adapting(now_ts),
            external_hot_water_charge=charging,
            external_hot_water_decisive=decisive,
        )

    @property
    def trv_target_temperatures(self) -> dict[str, float | None]:
        """Each TRV's target as last written or reported."""
        return {
            entity_id: self.trv_writer.current_target(entity_id)
            for entity_id in self._trv_entity_ids
        }

    @property
    def room_demand_pct(self) -> float | None:
        """The highest valve opening any TRV of the room reports."""
        openings = [
            opening
            for opening in self.trv_valve_positions.values()
            if opening is not None
        ]
        return max(openings) if openings else None

    @property
    def flow_boost_active(self) -> bool:
        """The room recovers after its mode's setpoint went up."""
        return self.flow_boost.active

    @property
    def flow_boost_temperature_c(self) -> float | None:
        """The recovery flow while the room recovers; None otherwise."""
        result = self.normal_result
        if not self.flow_boost_active or result is None:
            return None
        return result.recovery_flow_temperature_c

    @property
    def feedback_sample(self) -> DemandSample | None:
        """The room's valve demand for the flow feedback; None if not usable.

        Left out: an open window, frost protection, a radiator that does not
        heat, a running boost, and a TRV whose opening is unknown.
        """
        return self._demand_sample()[0]

    @property
    def supply(self) -> tuple[RoomSupply, SupplyReason | None, DemandSample | None]:
        """How the flow supplies the room, judged like the feedback does.

        Without heat at the valves (heat pump off, below the threshold, hot
        water) they stand open for nothing: not rated. Right after such a
        pause the room catches up on what cooled off: not rated either.
        """
        sample, reason = self._demand_sample()
        if reason is None and (not self.trv_active or self.mpc.hot_water_charging):
            reason = SupplyReason.NO_HEAT
        if reason is None and self.house.rooms_catching_up():
            reason = SupplyReason.CATCH_UP
        if sample is None or reason is not None:
            return RoomSupply.NOT_RATED, reason, sample
        if wants_more(sample):
            return RoomSupply.UNDERSUPPLIED, None, sample
        if holds(sample):
            return RoomSupply.SUPPLIED, None, sample
        return RoomSupply.OVERSUPPLIED, None, sample

    def _demand_sample(self) -> tuple[DemandSample | None, SupplyReason | None]:
        if self.state.is_window_open:
            return None, SupplyReason.WINDOW_OPEN
        if self.current_heat_mode is HeatMode.FROST_PROTECTION:
            return None, SupplyReason.FROST_PROTECTION
        if self.radiators_not_heating:
            return None, SupplyReason.RADIATOR_NOT_HEATING
        if self.flow_boost_active:
            return None, SupplyReason.BOOST
        demands = [
            self._trv_demand_pct(index)
            for index, entity_id in enumerate(self._valve_position_entities)
            if entity_id
        ]
        known = [demand for demand in demands if demand is not None]
        if not known or len(known) < len(demands):
            return None, SupplyReason.DEMAND_UNKNOWN
        # Only the room sensor says how far the room is short: the TRVs sit at
        # the radiator and read warm while it heats. Without a fresh reading
        # the deficit is unknown and the opening alone decides.
        target_c = self.flow_boost.settled_target_c
        room_c = self._room_sensor_temperature_c()
        deficit_c = (
            None if target_c is None or room_c is None else round(target_c - room_c, 2)
        )
        return DemandSample(self.room_name, max(known), deficit_c), None

    def _room_sensor_temperature_c(self) -> float | None:
        """The room sensor's reading; None without one or while it is stale."""
        room = self.mpc.get_room_temperature()
        if isinstance(room, RoomMpcError) or room.room_sensor_fallback:
            return None
        return room.room_sensor_temp_c

    async def _async_handle_load_sharing(self, _now: Any) -> None:
        await self._async_update_load_sharing()

    async def _async_update_load_sharing(self) -> None:
        if self.running:
            await self.load_sharing.async_update(time.time())

    async def async_set_manual_heat_mode(self, mode: HeatMode) -> None:
        self.state.set_active_heat_mode(mode)
        self.blocked = True
        await self._async_recompute()

    async def async_unblock(self) -> None:
        self.blocked = False
        await self._async_recompute()

    async def async_set_house_comfort(self, value: bool) -> None:
        if not self.follows_house_comfort:
            return
        self.state.set_house_comfort(value)
        await self._async_recompute()

    async def async_set_room_comfort(self, value: bool) -> None:
        self.state.set_room_comfort(value)
        await self._async_recompute()

    async def async_set_pv_boost(self, value: bool) -> None:
        self.state.set_pv_boost(value)
        await self._async_recompute()

    async def async_set_comfort_temperature(self, value: float) -> None:
        self.state.set_comfort_temperature(value)
        await self._async_recompute()

    async def async_set_eco_temperature_offset(self, value: float) -> None:
        self.state.set_eco_temperature_offset(value)
        await self._async_recompute()

    @property
    def trv_entity_ids(self) -> list[str]:
        return self._trv_entity_ids

    @property
    def trv_valve_positions(self) -> dict[str, float | None]:
        """The valve opening each TRV reports (pi_heating_demand), in percent.

        None for a TRV whose profile has no valve-position entity, whose
        entity has no number right now, or that reported nothing for
        VALVE_POSITION_MAX_AGE_S.
        """
        return {
            climate: self._trv_demand_pct(index)
            for index, climate in enumerate(self._trv_entity_ids)
        }

    @property
    def radiators_not_heating(self) -> list[str]:
        """TRVs whose radiator gives off no heat although the valve is open."""
        return [
            entity_id
            for entity_id, not_heating in zip(
                self._trv_entity_ids, self.mpc.radiators_not_heating, strict=True
            )
            if not_heating
        ]

    @property
    def current_heat_mode(self) -> HeatMode:
        return self.state.resolve_display_mode(self.blocked)

    @property
    def base_temperature_c(self) -> float:
        return self.state.determine_base_target_temperature(self.current_heat_mode)

    @property
    def is_automation_active(self) -> bool:
        return not self.blocked

    @property
    def current_flow_temperature_c(self) -> float | None:
        """The last valid flow temperature; None until one was reported."""
        return self.learning_inputs.flow_temperature_c

    @property
    def normal_hold_flow_temperature_c(self) -> float | None:
        """The ungated hold requirement -- what the room would need at setpoint.

        Unlike the gated hold this keeps following the weather while the
        surplus gate is closed, which is what makes the gate's effect (and the
        seasonal heating limit behind it) observable.
        """
        result = self.normal_result
        return result.hold_flow_temperature_c if result else None

    @property
    def comfort_temperature_c(self) -> float:
        return self.state.determine_base_target_temperature(HeatMode.COMFORT)

    def curve_outdoor_c(self, end: CurveEnd) -> float | None:
        """Outdoor value of one end of the heat source's curve; None without one."""
        ends = self.house.curve_outdoor_ends_c()
        if ends is None:
            return None
        cold_c, warm_c = ends
        return cold_c if end is CurveEnd.COLD else warm_c

    def hold_flow_at_curve_end_c(self, end: CurveEnd) -> float | None:
        """Flow that holds the comfort temperature at one end of the curve.

        Comfort, not the current mode: the curve has to carry the room when it
        is in use. Learned UA, no gains -- errs high, the safe side for a
        curve that also has to hold through the night.
        """
        outdoor_c = self.curve_outdoor_c(end)
        if outdoor_c is None:
            return None
        return self.comfort_hold_flow_c(outdoor_c)

    def comfort_hold_flow_c(
        self, outdoor_c: float, *, round_up: bool = True
    ) -> float | None:
        """Flow that holds the comfort temperature at an outdoor value."""
        return self.mpc.calculate_hold_flow_temperature_c(
            self.comfort_temperature_c, outdoor_c, round_up=round_up
        )

    @property
    def normal_gated_hold_flow_temperature_c(self) -> float | None:
        """The hold requirement after the surplus gate, without recovery.

        Depends on the weather and on whether the room coasts on stored heat,
        not on a deficit the room would recover from once heating runs -- the
        signal for deciding whether the heating season is on.
        """
        result = self.normal_result
        return result.gated_hold_flow_temperature_c if result else None

    @property
    def design_flow_temperature_c(self) -> float:
        return self.mpc.design_flow_temperature_c

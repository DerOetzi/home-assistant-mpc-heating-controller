from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET, STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util, slugify

from .const import (
    CONF_COMFORT_CONDITION_ENTITIES,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEARNING_WINDOW_ENTITY,
    CONF_MAX_SENSOR_AGE,
    CONF_MPC_DEMAND_HYSTERESIS_PCT,
    CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
    CONF_MPC_HOLD_TIME,
    CONF_MPC_MAX_DEMAND_STEP_PCT,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_ENTITY,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOM_COMFORT_CONDITION_ENTITIES,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_STATIONARY_RANGE,
    CONF_TRV_ACTIVE_SWITCH,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_ENTITY_ID,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_MAX_TARGET_TEMPERATURE,
    CONF_TRV_MIN_TARGET_TEMPERATURE,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_TARGET_TEMPERATURE_STEP,
    CONF_TRV_WIDTH_MM,
    CONF_TRVS,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_FROST_PROTECTION_TEMPERATURE,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_STATIONARY_RANGE_C,
    HEAT_SOURCE_ACTIVE_STATE,
    DesignTemperatureSystem,
    FlowSupplyStatus,
    HeatEmitterType,
    HeatMode,
    LEARNING_CYCLE_INTERVAL_MINUTES,
    SENSOR_POLL_INTERVAL_MINUTES,
    PanelRadiatorType,
)
from .controller.mpc.controller import (
    FlowGateConfig,
    MpcRateLimitConfig,
    RoomMpcController,
)
from .controller.mpc.results import RoomMpcResult
from .controller.mpc.types import (
    FlowGateState,
    LearningFactors,
    RoomThermalConfig,
    TrvConfig,
)
from .controller.state import HeatingStateConfig, HeatingStateController
from .store import FlowGateStateStore, LearningFactorsStore

_LOGGER = logging.getLogger(__name__)

LEARNING_CYCLE_INTERVAL = timedelta(minutes=LEARNING_CYCLE_INTERVAL_MINUTES)

SENSOR_POLL_INTERVAL = timedelta(minutes=SENSOR_POLL_INTERVAL_MINUTES)

WINDOW_OPEN_SUPPRESS_LEARNING_S = 60 * 60
WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S = 30 * 60
WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S = 10 * 60

SUNSET_LEARNING_DELAY = timedelta(hours=2)

# After startup a configured room sensor often reports a few seconds after the
# TRVs. Computing in between falls back to the TRV average, which can sit well
# below the room sensor and reopen a restored surplus gate. Wait this long for
# the room sensor before accepting the fallback.
ROOM_SENSOR_STARTUP_GRACE_S = 5 * 60


def _build_trv_configs(trv_entries: list[dict[str, Any]]) -> list[TrvConfig]:
    return [
        TrvConfig(
            name=entry[CONF_TRV_ENTITY_ID],
            emitter_type=HeatEmitterType(entry[CONF_TRV_EMITTER_TYPE]),
            min_target_temperature_c=entry[CONF_TRV_MIN_TARGET_TEMPERATURE],
            max_target_temperature_c=entry[CONF_TRV_MAX_TARGET_TEMPERATURE],
            target_temperature_step_c=entry[CONF_TRV_TARGET_TEMPERATURE_STEP],
            radiator_type=(
                PanelRadiatorType(entry[CONF_TRV_RADIATOR_TYPE])
                if CONF_TRV_RADIATOR_TYPE in entry
                else None
            ),
            width_mm=entry.get(CONF_TRV_WIDTH_MM),
            height_mm=entry.get(CONF_TRV_HEIGHT_MM),
            nominal_power_w=entry.get(CONF_TRV_NOMINAL_POWER_W),
        )
        for entry in trv_entries
    ]


class HeatingRoomCoordinator:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.data = entry.data
        self.room_name: str = self.data[CONF_ROOM_NAME]

        self._trv_entries: list[dict[str, Any]] = self.data[CONF_TRVS]
        self._trv_entity_ids: list[str] = [
            trv[CONF_TRV_ENTITY_ID] for trv in self._trv_entries
        ]
        self._trv_active_switches: list[str] = [
            trv[CONF_TRV_ACTIVE_SWITCH]
            for trv in self._trv_entries
            if trv.get(CONF_TRV_ACTIVE_SWITCH)
        ]
        self.room_comfort_condition_entities: list[str] = list(
            self.data.get(CONF_ROOM_COMFORT_CONDITION_ENTITIES, [])
        )
        self._comfort_condition_entities: list[str] = [
            *self.data.get(CONF_COMFORT_CONDITION_ENTITIES, []),
            *self.room_comfort_condition_entities,
        ]

        self._heat_source_climate_entity: str | None = self.data.get(
            CONF_HEAT_SOURCE_CLIMATE_ENTITY
        )
        self._flow_threshold_entity: str | None = self.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        self._learning_window_entity: str | None = self.data.get(
            CONF_LEARNING_WINDOW_ENTITY
        )

        self.state = HeatingStateController(
            HeatingStateConfig(
                boost_temperature_offset_c=self.data[CONF_BOOST_TEMPERATURE_OFFSET],
                frost_protection_temperature_c=self.data[
                    CONF_FROST_PROTECTION_TEMPERATURE
                ],
                pv_boost_enabled=self.data[CONF_PV_BOOST_ENABLED],
                pv_boost_temperature_offset_c=self.data[
                    CONF_PV_BOOST_TEMPERATURE_OFFSET
                ],
            )
        )

        thermal_config = RoomThermalConfig(
            design_indoor_temperature_c=self.data[CONF_DESIGN_INDOOR_TEMPERATURE],
            design_outdoor_temperature_c=self.data[CONF_DESIGN_OUTDOOR_TEMPERATURE],
            design_temperature_system=DesignTemperatureSystem(
                self.data[CONF_DESIGN_TEMPERATURE_SYSTEM]
            ),
            room_heat_load_w=self.data[CONF_ROOM_HEAT_LOAD],
            flow_threshold_c=self.flow_threshold_c,
        )
        rate_limit_config = MpcRateLimitConfig(
            demand_hysteresis_pct=self.data[CONF_MPC_DEMAND_HYSTERESIS_PCT],
            hold_time_s=self.data[CONF_MPC_HOLD_TIME],
            hold_override_demand_pct=self.data[CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT],
            max_demand_step_pct=self.data[CONF_MPC_MAX_DEMAND_STEP_PCT],
        )
        flow_gate_config = FlowGateConfig(
            close_surplus_c=self.data.get(
                CONF_FLOW_GATE_CLOSE_SURPLUS, DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C
            ),
            open_surplus_c=self.data.get(
                CONF_FLOW_GATE_OPEN_SURPLUS, DEFAULT_FLOW_GATE_OPEN_SURPLUS_C
            ),
            hold_time_s=self.data.get(
                CONF_FLOW_GATE_HOLD_TIME, DEFAULT_FLOW_GATE_HOLD_TIME_S
            ),
        )
        self.mpc = RoomMpcController(
            thermal_config=thermal_config,
            trvs=_build_trv_configs(self._trv_entries),
            rate_limit_config=rate_limit_config,
            max_sensor_age_s=self.data[CONF_MAX_SENSOR_AGE],
            flow_gate_config=flow_gate_config,
        )
        self.mpc.set_stationary_range_c(
            self.data.get(CONF_STATIONARY_RANGE, DEFAULT_STATIONARY_RANGE_C)
        )
        self.mpc.set_sun_condition(self.is_sun_condition_met)

        self.store = LearningFactorsStore(hass, self.room_name, entry.entry_id)
        self.flow_gate_store = FlowGateStateStore(hass, self.room_name, entry.entry_id)
        self._persisted_flow_gate_state: FlowGateState | None = None
        self._awaiting_room_sensor = bool(self.data.get(CONF_ROOM_SENSOR_ENTITY))

        self.blocked = False
        self.trv_active = True
        self.last_result: RoomMpcResult | None = None
        self.normal_result: RoomMpcResult | None = None
        self.normal_heat_mode: HeatMode | None = None
        self.normal_target_temperature_c: float | None = None

        self._unsub: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    def _notify_listeners(self) -> None:
        for update_callback in self._listeners:
            update_callback()

    async def async_setup(self) -> None:
        factors = await self.store.async_load()
        if factors is not None:
            self.mpc.recalibrate_learning_factors(factors)
        else:
            await self._async_import_legacy_learning_factors()

        flow_gate_state = await self.flow_gate_store.async_load()
        if flow_gate_state is not None:
            self.mpc.restore_flow_gate_state(flow_gate_state)
            self._persisted_flow_gate_state = flow_gate_state

        for entity_id in self._comfort_condition_entities:
            self.state.set_comfort_condition(entity_id, self._is_on(entity_id))

        for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            self.state.update_window_state(entity_id, self._is_on(entity_id))

        self.state.set_pv_boost(self._is_on(self.data[CONF_PV_BOOST_ENTITY]))

        if self._learning_window_entity:
            self.mpc.set_learning_window_active(
                self._is_on(self._learning_window_entity), time.time()
            )

        self._refresh_temperature_inputs()

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if self._awaiting_room_sensor:
            self._unsub.append(
                async_call_later(
                    self.hass,
                    ROOM_SENSOR_STARTUP_GRACE_S,
                    self._async_handle_room_sensor_grace_expired,
                )
            )

        tracked_entities = list(self._trv_entity_ids)
        tracked_entities.extend(self._comfort_condition_entities)
        tracked_entities.extend(self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []))
        tracked_entities.append(self.data[CONF_PV_BOOST_ENTITY])
        tracked_entities.append(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        if self._heat_source_climate_entity:
            tracked_entities.append(self._heat_source_climate_entity)
        if self._flow_threshold_entity:
            tracked_entities.append(self._flow_threshold_entity)
        if self._learning_window_entity:
            tracked_entities.append(self._learning_window_entity)
        if room_sensor_entity:
            tracked_entities.append(room_sensor_entity)

        self._unsub.append(
            async_track_state_change_event(
                self.hass, tracked_entities, self._async_handle_state_change
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

        await self._async_recompute()

    def async_unload(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        self.mpc.destroy()

    async def _async_import_legacy_learning_factors(self) -> None:
        slug = slugify(self.room_name)
        ua_state = self.hass.states.get(f"text.{slug}_ua_factor")
        capacity_state = self.hass.states.get(f"text.{slug}_capacity_factor")
        if ua_state is None or capacity_state is None:
            return

        try:
            factors = LearningFactors(
                ua_factor=float(ua_state.state),
                capacity_factor=float(capacity_state.state),
            )
        except ValueError:
            _LOGGER.warning(
                "Could not parse legacy learning factors for room %s", self.room_name
            )
            return

        self.mpc.recalibrate_learning_factors(factors)
        await self.store.async_save(factors)
        _LOGGER.info(
            "Imported legacy learning factors for room %s: ua_factor=%s capacity_factor=%s",
            self.room_name,
            factors.ua_factor,
            factors.capacity_factor,
        )

    def _refresh_temperature_inputs(self) -> None:
        for index, entity_id in enumerate(self._trv_entity_ids):
            self.mpc.set_trv_temperature(
                index,
                self._climate_temperature_from_state(self.hass.states.get(entity_id)),
            )

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if room_sensor_entity:
            room_sensor_temp_c = self._float_state(room_sensor_entity)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)
            if room_sensor_temp_c is not None:
                self._awaiting_room_sensor = False

        self.mpc.set_outdoor_temperature(
            self._float_state(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        )
        self.mpc.set_flow_temperature(self.current_flow_temperature_c)

    async def _async_handle_room_sensor_grace_expired(self, _now: Any) -> None:
        if not self._awaiting_room_sensor:
            return
        _LOGGER.warning(
            "Room sensor for room %s did not report within %s s after startup; "
            "falling back to TRV temperatures",
            self.room_name,
            ROOM_SENSOR_STARTUP_GRACE_S,
        )
        self._awaiting_room_sensor = False
        await self._async_recompute()

    async def _async_handle_sensor_poll(self, _now: Any) -> None:
        self._refresh_temperature_inputs()
        await self._async_recompute()

    def _compute_trv_active(self) -> bool:
        if not self._heat_source_climate_entity:
            return False
        climate_state = self.hass.states.get(self._heat_source_climate_entity)
        if climate_state is None or climate_state.state != HEAT_SOURCE_ACTIVE_STATE:
            return False
        flow_temp_c = self._climate_temperature_from_state(climate_state)
        return flow_temp_c is not None and flow_temp_c > self.flow_threshold_c

    async def _async_apply_trv_active_switches(self, active: bool) -> None:
        if not self._trv_active_switches:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if active else "turn_off",
            {"entity_id": self._trv_active_switches},
            blocking=False,
        )

    def _is_on(self, entity_id: str) -> bool:
        return self._bool_from_state(self.hass.states.get(entity_id))

    def _float_state(self, entity_id: str) -> float | None:
        return self._float_from_state(self.hass.states.get(entity_id))

    @staticmethod
    def _bool_from_state(state: State | None) -> bool:
        return state is not None and state.state == STATE_ON

    @staticmethod
    def _float_from_state(state: State | None) -> float | None:
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _climate_temperature_from_state(state: State | None) -> float | None:
        if state is None:
            return None
        try:
            return float(state.attributes.get("current_temperature"))
        except (TypeError, ValueError):
            return None

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        self.hass.async_create_task(
            self._async_process_state_change(entity_id, new_state)
        )

    async def _async_process_state_change(
        self, entity_id: str, new_state: State | None
    ) -> None:
        if entity_id in self._trv_entity_ids:
            index = self._trv_entity_ids.index(entity_id)
            self.mpc.set_trv_temperature(
                index, self._climate_temperature_from_state(new_state)
            )

        elif entity_id == self.data.get(CONF_ROOM_SENSOR_ENTITY):
            room_sensor_temp_c = self._float_from_state(new_state)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)
            if room_sensor_temp_c is not None:
                self._awaiting_room_sensor = False

        elif entity_id == self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY]:
            self.mpc.set_outdoor_temperature(self._float_from_state(new_state))

        elif entity_id == self._heat_source_climate_entity:
            self.mpc.set_flow_temperature(
                self._climate_temperature_from_state(new_state)
            )

        elif entity_id in self._comfort_condition_entities:
            self.state.set_comfort_condition(
                entity_id, self._bool_from_state(new_state)
            )

        elif entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            was_open = self.state.is_window_open
            _, is_open = self.state.update_window_state(
                entity_id, self._bool_from_state(new_state)
            )
            if is_open and not was_open:
                self.mpc.suppress_learning_for_interval(
                    WINDOW_OPEN_SUPPRESS_LEARNING_S, WINDOW_OPEN_SUPPRESS_LEARNING_S
                )
            elif was_open and not is_open:
                self.mpc.suppress_learning_for_interval(
                    WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S,
                    WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S,
                )

        elif entity_id == self.data[CONF_PV_BOOST_ENTITY]:
            self.state.set_pv_boost(self._bool_from_state(new_state))

        elif entity_id == self._learning_window_entity:
            self.mpc.set_learning_window_active(
                self._bool_from_state(new_state), time.time()
            )

        await self._async_recompute()

    async def _async_handle_learning_cycle(self, _now: Any) -> None:
        self.mpc.run_learning_cycle()
        await self._async_persist_learning_factors()
        self._notify_listeners()

    async def _async_persist_learning_factors(self) -> None:
        persisted_factors = self.mpc.consume_persisted_learning_factors()
        if persisted_factors is not None:
            await self.store.async_save(persisted_factors)

    async def async_recalibrate(
        self, ua_factor: float | None, capacity_factor: float | None
    ) -> None:
        factors = LearningFactors(
            ua_factor=self.mpc.learned_ua_factor if ua_factor is None else ua_factor,
            capacity_factor=(
                self.mpc.learned_capacity_factor
                if capacity_factor is None
                else capacity_factor
            ),
        )
        self.mpc.recalibrate_learning_factors(factors)
        await self.store.async_save(factors)
        _LOGGER.info(
            "Recalibrated learning factors for room %s: ua_factor=%s capacity_factor=%s",
            self.room_name,
            factors.ua_factor,
            factors.capacity_factor,
        )
        self._notify_listeners()

    def is_sun_condition_met(self, timestamp: float) -> bool:
        moment = dt_util.utc_from_timestamp(timestamp)
        today = dt_util.as_local(moment).date()

        sunset_today = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, today)
        if sunset_today is not None and moment >= sunset_today + SUNSET_LEARNING_DELAY:
            return True

        sunrise_today = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, today)
        sunset_yesterday = get_astral_event_date(
            self.hass, SUN_EVENT_SUNSET, today - timedelta(days=1)
        )
        if sunrise_today is None or sunset_yesterday is None:
            return False
        return sunset_yesterday + SUNSET_LEARNING_DELAY <= moment < sunrise_today

    @property
    def flow_threshold_c(self) -> float:
        """The lowest flow temperature the heat source can usefully deliver.

        Read from the linked entity on every access so the limit can be tried
        out on the heat pump by changing a single helper. Without a linked
        entity, or while it reports nothing usable, the built-in default keeps
        the room controllable instead of blocking it.
        """
        if not self._flow_threshold_entity:
            return DEFAULT_FLOW_THRESHOLD_C
        value = self._float_state(self._flow_threshold_entity)
        if value is None:
            return DEFAULT_FLOW_THRESHOLD_C
        return value

    async def _async_recompute(self) -> None:
        self.mpc.set_flow_threshold_c(self.flow_threshold_c)
        desired_mode = self.state.desired_automatic_heat_mode(self.blocked)
        self.state.set_active_heat_mode(desired_mode)

        self.trv_active = self._compute_trv_active()
        self.state.set_trv_active(self.trv_active)
        if self.trv_active:
            self.mpc.enable_learning()
        else:
            self.mpc.disable_learning()

        if self._awaiting_room_sensor:
            await self._async_apply_trv_active_switches(self.trv_active)
            self._notify_listeners()
            return

        self.normal_heat_mode = self.state.current_heat_mode
        normal_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(self.normal_heat_mode)
        )
        self.normal_target_temperature_c = normal_target
        normal_compute = self.mpc.compute(normal_target, apply_side_effects=False)
        self.normal_result = normal_compute.result if normal_compute.valid else None

        display_mode = self.state.resolve_display_mode(self.blocked)
        actual_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(display_mode)
        )
        actual_compute = self.mpc.compute(actual_target)
        if actual_compute.valid:
            self.last_result = actual_compute.result
            await self._async_apply_result(actual_compute.result)
        else:
            _LOGGER.debug(
                "MPC compute failed for room %s: %s",
                self.room_name,
                actual_compute.error,
            )

        await self._async_apply_trv_active_switches(self.trv_active)
        await self._async_persist_learning_factors()
        await self._async_persist_flow_gate_state()
        self._notify_listeners()

    async def _async_persist_flow_gate_state(self) -> None:
        flow_gate_state = self.mpc.flow_gate_state
        if flow_gate_state == self._persisted_flow_gate_state:
            return
        await self.flow_gate_store.async_save(flow_gate_state)
        self._persisted_flow_gate_state = flow_gate_state

    async def _async_apply_result(self, result: RoomMpcResult) -> None:
        for entity_id, target_temperature_c in zip(
            self._trv_entity_ids, result.trv_targets, strict=True
        ):
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": target_temperature_c},
                blocking=False,
            )

    async def async_set_manual_heat_mode(self, mode: HeatMode) -> None:
        self.state.set_active_heat_mode(mode)
        self.blocked = True
        await self._async_recompute()

    async def async_unblock(self) -> None:
        self.blocked = False
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
        if not self._heat_source_climate_entity:
            return None
        return self._climate_temperature_from_state(
            self.hass.states.get(self._heat_source_climate_entity)
        )

    @property
    def normal_min_flow_temperature_c(self) -> float | None:
        result = self.normal_result
        return result.recommended_flow_temperature_c if result else None

    @property
    def normal_hold_flow_temperature_c(self) -> float | None:
        """The ungated hold requirement -- what the room would need at setpoint.

        Unlike normal_min_flow_temperature_c this keeps following the weather
        while the surplus gate is closed, which is what makes the gate's effect
        (and the seasonal heating limit behind it) observable.
        """
        result = self.normal_result
        return result.hold_flow_temperature_c if result else None

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
    def normal_recovery_flow_temperature_c(self) -> float | None:
        result = self.normal_result
        return result.recovery_flow_temperature_c if result else None

    @property
    def normal_recovery_flow_saturated(self) -> bool | None:
        result = self.normal_result
        return result.recovery_flow_saturated if result else None

    @property
    def normal_flow_gate_closed(self) -> bool | None:
        result = self.normal_result
        return result.flow_gate_closed if result else None

    @property
    def flow_supply_status(self) -> FlowSupplyStatus:

        required = self.normal_min_flow_temperature_c
        if required is None or required <= 0:
            if self.normal_flow_gate_closed:
                return FlowSupplyStatus.SURPLUS
            return FlowSupplyStatus.NO_REQUIREMENT
        
        if required < self.flow_threshold_c:
            return FlowSupplyStatus.BELOW_THRESHOLD
            
        if not self.trv_active:
            return FlowSupplyStatus.SOURCE_INACTIVE

        current = self.current_flow_temperature_c
        if current is not None and current >= required:
            return FlowSupplyStatus.SUFFICIENT
        
        return FlowSupplyStatus.UNDERSUPPLIED

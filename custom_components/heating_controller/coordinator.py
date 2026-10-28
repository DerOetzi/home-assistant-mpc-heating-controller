from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util, slugify

from .const import (
    CONF_COMFORT_CONDITION_ENTITIES,
    CONF_CONSTRUCTION,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEARNING_ENABLED_ENTITY,
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
    CONF_ROOM_AREA,
    CONF_ROOM_COMFORT_CONDITION_ENTITIES,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_STATIONARY_RANGE,
    CONF_TARGET_BAND,
    CONF_TRV_SETPOINT_MODE,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_WIDTH_MM,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_FROST_PROTECTION_TEMPERATURE,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_FLOW_THRESHOLD_C,
    DEFAULT_STATIONARY_RANGE_C,
    HEAT_SOURCE_ACTIVE_STATE,
    Construction,
    DesignTemperatureSystem,
    FlowSupplyStatus,
    HeatEmitterType,
    HeatMode,
    LEARNING_CYCLE_INTERVAL_MINUTES,
    SENSOR_POLL_INTERVAL_MINUTES,
    PanelRadiatorType,
    TrvSetpointMode,
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
from .profiles import TrvBinding
from .store import FlowGateStateStore, LearningFactorsStore, ObserverStateStore

_LOGGER = logging.getLogger(__name__)

LEARNING_CYCLE_INTERVAL = timedelta(minutes=LEARNING_CYCLE_INTERVAL_MINUTES)

SENSOR_POLL_INTERVAL = timedelta(minutes=SENSOR_POLL_INTERVAL_MINUTES)

TARGET_TEMPERATURE_TOLERANCE_C = 0.01
# A Danfoss Ally reports its previous setpoint once more a few seconds after a
# write. Within this window the written target counts as the TRV's target, so
# the echo neither triggers a second write nor moves a relative target.
TARGET_WRITE_SETTLE_S = 120.0
# A TRV switches between shut (minimum target) and open at most this often,
# unless frost protection is forced: every switch wakes it twice.
TRV_SWITCH_MIN_INTERVAL_S = 15 * 60
# A write aborts a TRV's adaptation run, so nothing is written to a TRV while
# its profile reports one running -- but no longer than this, so a status that
# never clears cannot leave the room uncontrolled.
# A Danfoss Ally calibration took 1 h 46 to 1 h 49 min.
ADAPTATION_MAX_HOLD_S = 3 * 3600

WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S = 30 * 60
WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S = 10 * 60
# The disturbance moves slowly; saving every small step only wears the disk.
OBSERVER_PERSIST_MIN_CHANGE_W = 1.0

SUNSET_LEARNING_DELAY = timedelta(hours=2)

# After startup a configured room sensor often reports a few seconds after the
# TRVs. Computing in between falls back to the TRV average, which can sit well
# below the room sensor and reopen a restored surplus gate. Wait this long for
# the room sensor before accepting the fallback.
ROOM_SENSOR_STARTUP_GRACE_S = 5 * 60

# Added after rooms were already set up; an entry without them stops with a
# hint to fill them in through the options flow instead of guessing.
_REQUIRED_ENTRY_KEYS = (
    CONF_ROOM_AREA,
    CONF_CONSTRUCTION,
    CONF_FLOW_TEMPERATURE_ENTITY,
)


def _build_trv_configs(trvs: list[TrvBinding]) -> list[TrvConfig]:
    return [
        TrvConfig(
            name=trv.climate_entity,
            emitter_type=HeatEmitterType(trv.emitter[CONF_TRV_EMITTER_TYPE]),
            min_target_temperature_c=trv.min_target_temperature_c,
            max_target_temperature_c=trv.max_target_temperature_c,
            target_temperature_step_c=trv.target_temperature_step_c,
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


class HeatingRoomCoordinator:
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, trvs: list[TrvBinding]
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.data = entry.data
        self.room_name: str = self.data[CONF_ROOM_NAME]
        missing = [key for key in _REQUIRED_ENTRY_KEYS if not self.data.get(key)]
        if missing:
            raise ConfigEntryError(
                f"Room {self.room_name} is missing {', '.join(missing)}; "
                "fill them in through the options flow"
            )

        self._trvs = trvs
        self._trv_entity_ids: list[str] = [trv.climate_entity for trv in trvs]
        self._valve_position_entities: list[str | None] = [
            trv.valve_position_entity for trv in trvs
        ]
        self._adaptation_status_entities: list[str] = [
            trv.adaptation_status_entity for trv in trvs if trv.adaptation_status_entity
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
        self._flow_temperature_entity: str = self.data[CONF_FLOW_TEMPERATURE_ENTITY]
        # The heat source drops out for a few polls at a time while it keeps
        # running; its last known state bridges that.
        self._last_heat_source_state: str | None = None
        # Same for the flow temperature entity, which drops out for a moment
        # whenever its template is reloaded.
        self._flow_temperature_c: float | None = None
        self._flow_threshold_entity: str | None = self.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        self._learning_window_entity: str | None = self.data.get(
            CONF_LEARNING_WINDOW_ENTITY
        )
        self._learning_enabled_entity: str | None = self.data.get(
            CONF_LEARNING_ENABLED_ENTITY
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
            room_area_m2=self.data[CONF_ROOM_AREA],
            construction=Construction(self.data[CONF_CONSTRUCTION]),
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
            trvs=_build_trv_configs(trvs),
            rate_limit_config=rate_limit_config,
            max_sensor_age_s=self.data[CONF_MAX_SENSOR_AGE],
            room_sensor_configured=bool(self.data.get(CONF_ROOM_SENSOR_ENTITY)),
            target_band_c=self.data[CONF_TARGET_BAND],
            trv_setpoint_mode=TrvSetpointMode(self.data[CONF_TRV_SETPOINT_MODE]),
            flow_gate_config=flow_gate_config,
        )
        self.mpc.set_stationary_range_c(
            self.data.get(CONF_STATIONARY_RANGE, DEFAULT_STATIONARY_RANGE_C)
        )
        self.mpc.set_sun_condition(self.is_sun_condition_met)

        self.store = LearningFactorsStore(hass, self.room_name, entry.entry_id)
        self.flow_gate_store = FlowGateStateStore(hass, self.room_name, entry.entry_id)
        self._persisted_flow_gate_state: FlowGateState | None = None
        self.observer_store = ObserverStateStore(hass, self.room_name, entry.entry_id)
        self._persisted_disturbance_w: float | None = None
        self._awaiting_room_sensor = bool(self.data.get(CONF_ROOM_SENSOR_ENTITY))

        self.blocked = False
        self.trv_active = True
        self.last_result: RoomMpcResult | None = None
        # Target writes per TRV since setup: every write wakes the TRV.
        self.trv_setpoint_writes: dict[str, int] = {}
        self._written_targets: dict[str, tuple[float, float]] = {}
        self._trv_switched_at: dict[str, float] = {}
        self._adaptation_since: dict[str, float] = {}
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

        disturbance_w = await self.observer_store.async_load()
        if disturbance_w is not None:
            self.mpc.restore_disturbance(disturbance_w)
            self._persisted_disturbance_w = disturbance_w

        for entity_id in self._comfort_condition_entities:
            self.state.set_comfort_condition(entity_id, self._is_on(entity_id))

        for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            self.state.update_window_state(entity_id, self._is_on(entity_id))
        self.mpc.set_window_open(self.state.is_window_open, time.time())

        self.state.set_pv_boost(self._is_on(self.data[CONF_PV_BOOST_ENTITY]))

        if self._learning_window_entity:
            self.mpc.set_learning_window_active(
                self._is_on(self._learning_window_entity), time.time()
            )

        if self._learning_enabled_entity:
            self.mpc.set_learning_paused(
                not self._is_on(self._learning_enabled_entity), time.time()
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
        tracked_entities.extend(
            entity_id for entity_id in self._valve_position_entities if entity_id
        )
        tracked_entities.extend(self._adaptation_status_entities)
        tracked_entities.extend(self._comfort_condition_entities)
        tracked_entities.extend(self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []))
        tracked_entities.append(self.data[CONF_PV_BOOST_ENTITY])
        tracked_entities.append(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        tracked_entities.append(self._flow_temperature_entity)
        if self._heat_source_climate_entity:
            tracked_entities.append(self._heat_source_climate_entity)
        if self._flow_threshold_entity:
            tracked_entities.append(self._flow_threshold_entity)
        if self._learning_window_entity:
            tracked_entities.append(self._learning_window_entity)
        if self._learning_enabled_entity:
            tracked_entities.append(self._learning_enabled_entity)
        if room_sensor_entity:
            tracked_entities.append(room_sensor_entity)

        self._unsub.append(
            async_track_state_change_event(
                self.hass, tracked_entities, self._async_handle_state_change
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
        for index, entity_id in enumerate(self._valve_position_entities):
            if entity_id:
                self.mpc.set_valve_position(index, self._float_state(entity_id))

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if room_sensor_entity:
            room_sensor_temp_c = self._float_state(room_sensor_entity)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)
            if room_sensor_temp_c is not None:
                self._awaiting_room_sensor = False

        self.mpc.set_outdoor_temperature(
            self._float_state(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
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
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

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

    def _update_flow_temperature(self, state: State | None) -> None:
        value = self._float_from_state(state)
        if value is not None:
            self._flow_temperature_c = value
        self.mpc.set_flow_temperature(self._flow_temperature_c)

    def _compute_trv_active(self) -> bool:
        if not self._heat_source_climate_entity:
            return False
        climate_state = self.hass.states.get(self._heat_source_climate_entity)
        if climate_state is not None and climate_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._last_heat_source_state = climate_state.state
        if self._last_heat_source_state != HEAT_SOURCE_ACTIVE_STATE:
            return False
        flow_temp_c = self.current_flow_temperature_c
        return flow_temp_c is not None and flow_temp_c >= self.flow_threshold_c

    def _adaptation_running(self, trv: TrvBinding, now_ts: float) -> bool:
        """Whether the TRV runs an adaptation that a write would abort."""
        if trv.adaptation_busy is None or trv.adaptation_status_entity is None:
            return False
        if not trv.adaptation_busy.matches(
            self.hass.states.get(trv.adaptation_status_entity)
        ):
            self._adaptation_since.pop(trv.climate_entity, None)
            return False
        since = self._adaptation_since.setdefault(trv.climate_entity, now_ts)
        if now_ts - since < ADAPTATION_MAX_HOLD_S:
            return True
        if since > 0:
            _LOGGER.warning(
                "%s still reports an adaptation run after %s s; writing again",
                trv.climate_entity,
                ADAPTATION_MAX_HOLD_S,
            )
            # Marks the run as warned about; it clears with the status.
            self._adaptation_since[trv.climate_entity] = -ADAPTATION_MAX_HOLD_S
        return False

    async def _async_apply_trv_active_switches(self, active: bool) -> None:
        desired_state = STATE_ON if active else STATE_OFF
        now_ts = time.time()
        switches_to_set = [
            trv.heat_available_entity
            for trv in self._trvs
            if trv.heat_available_entity
            and not self._adaptation_running(trv, now_ts)
            and (
                (state := self.hass.states.get(trv.heat_available_entity)) is None
                or state.state != desired_state
            )
        ]
        if not switches_to_set:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if active else "turn_off",
            {"entity_id": switches_to_set},
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

    @staticmethod
    def _climate_target_from_state(state: State | None) -> float | None:
        if state is None:
            return None
        try:
            return float(state.attributes.get("temperature"))
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

        elif entity_id in self._valve_position_entities:
            self.mpc.set_valve_position(
                self._valve_position_entities.index(entity_id),
                self._float_from_state(new_state),
            )

        elif entity_id == self.data.get(CONF_ROOM_SENSOR_ENTITY):
            room_sensor_temp_c = self._float_from_state(new_state)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)
            if room_sensor_temp_c is not None:
                self._awaiting_room_sensor = False

        elif entity_id == self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY]:
            self.mpc.set_outdoor_temperature(self._float_from_state(new_state))

        elif entity_id == self._flow_temperature_entity:
            self._update_flow_temperature(new_state)

        elif entity_id in self._comfort_condition_entities:
            self.state.set_comfort_condition(
                entity_id, self._bool_from_state(new_state)
            )

        elif entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            was_open = self.state.is_window_open
            _, is_open = self.state.update_window_state(
                entity_id, self._bool_from_state(new_state)
            )
            self.mpc.set_window_open(is_open, time.time())
            if was_open and not is_open:
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

        elif entity_id == self._learning_enabled_entity:
            self.mpc.set_learning_paused(
                not self._bool_from_state(new_state), time.time()
            )

        await self._async_recompute()

    async def _async_handle_learning_cycle(self, _now: Any) -> None:
        self.mpc.run_learning_cycle()
        await self._async_persist_learning_factors()
        await self._async_persist_disturbance()
        self._notify_listeners()

    async def _async_persist_disturbance(self) -> None:
        disturbance_w = self.mpc.disturbance_w
        if (
            self._persisted_disturbance_w is not None
            and abs(disturbance_w - self._persisted_disturbance_w)
            < OBSERVER_PERSIST_MIN_CHANGE_W
        ):
            return
        await self.observer_store.async_save(disturbance_w)
        self._persisted_disturbance_w = disturbance_w

    async def _async_persist_learning_factors(self) -> None:
        persisted_factors = self.mpc.consume_persisted_learning_factors()
        if persisted_factors is not None:
            await self.store.async_save(persisted_factors)

    async def async_recalibrate(
        self,
        ua_factor: float | None,
        capacity_factor: float | None,
        disturbance_w: float | None = None,
    ) -> None:
        if disturbance_w is not None:
            self.mpc.restore_disturbance(disturbance_w)
            await self.observer_store.async_save(self.mpc.disturbance_w)
            self._persisted_disturbance_w = self.mpc.disturbance_w
            _LOGGER.info(
                "Recalibrated disturbance for room %s: %s W",
                self.room_name,
                self.mpc.disturbance_w,
            )
        # Setting the factors discards the running learning windows; a
        # disturbance-only call leaves them be.
        if ua_factor is not None or capacity_factor is not None:
            await self._async_recalibrate_factors(ua_factor, capacity_factor)
        self._notify_listeners()

    async def _async_recalibrate_factors(
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

        now_ts = time.time()
        self.mpc.set_trv_adapting(
            any(self._adaptation_running(trv, now_ts) for trv in self._trvs)
        )
        self.trv_active = self._compute_trv_active()
        self.state.set_trv_active(self.trv_active)
        self.mpc.set_heat_available(self.trv_active)
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
        self.mpc.set_current_trv_targets(
            [self._current_trv_target(entity_id) for entity_id in self._trv_entity_ids]
        )
        normal_compute = self.mpc.compute(normal_target, apply_side_effects=False)
        self.normal_result = normal_compute.result if normal_compute.valid else None

        display_mode = self.state.resolve_display_mode(self.blocked)
        actual_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(display_mode)
        )
        actual_compute = self.mpc.compute(
            actual_target,
            skip_demand_smoothing=self.state.should_force_frost_protection(
                self.blocked
            ),
        )
        if actual_compute.valid:
            self.last_result = actual_compute.result
            await self._async_apply_result(
                actual_compute.result,
                forced=self.state.should_force_frost_protection(self.blocked),
            )
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

    async def _async_apply_result(
        self, result: RoomMpcResult, forced: bool = False
    ) -> None:
        for trv, target_temperature_c in zip(
            self._trvs, result.trv_targets, strict=True
        ):
            entity_id = trv.climate_entity
            # Every write wakes the TRV and can abort a running adaptation run,
            # so only send a target the TRV does not already report, none to a
            # TRV that cannot take it, and none while it adapts unless frost
            # protection is forced.
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            now_ts = time.time()
            if not forced and self._adaptation_running(trv, now_ts):
                continue
            current_target_c = self._current_trv_target(entity_id)
            if (
                current_target_c is not None
                and abs(current_target_c - target_temperature_c)
                < TARGET_TEMPERATURE_TOLERANCE_C
            ):
                continue
            min_target_c = trv.min_target_temperature_c
            switches = (current_target_c is None or current_target_c <= min_target_c) != (
                target_temperature_c <= min_target_c
            )
            if (
                switches
                and not forced
                and now_ts - self._trv_switched_at.get(entity_id, 0.0)
                < TRV_SWITCH_MIN_INTERVAL_S
            ):
                continue
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": target_temperature_c},
                blocking=False,
            )
            self.trv_setpoint_writes[entity_id] = (
                self.trv_setpoint_writes.get(entity_id, 0) + 1
            )
            self._written_targets[entity_id] = (target_temperature_c, now_ts)
            if switches:
                self._trv_switched_at[entity_id] = now_ts

    def _current_trv_target(self, entity_id: str) -> float | None:
        """The TRV's target, or the one just written while its echo settles."""
        written = self._written_targets.get(entity_id)
        if written is not None and time.time() - written[1] < TARGET_WRITE_SETTLE_S:
            return written[0]
        return self._climate_target_from_state(self.hass.states.get(entity_id))

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
        """The last valid flow temperature; None until one was reported."""
        return self._flow_temperature_c

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

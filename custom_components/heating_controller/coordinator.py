from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
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
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_CONSTRUCTION,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_FROST_PROTECTION_TEMPERATURE,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_MAX_SENSOR_AGE,
    CONF_OPAQUE_SURFACES,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_ROOM_AREA,
    CONF_ROOM_COMFORT_SWITCH,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_NAME,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_TRV_EMITTER_TYPE,
    CONF_TRV_HEIGHT_MM,
    CONF_TRV_NOMINAL_POWER_W,
    CONF_TRV_RADIATOR_TYPE,
    CONF_TRV_WIDTH_MM,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_WINDOW_SURFACES,
    DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C,
    DEFAULT_FLOW_GATE_HOLD_TIME_S,
    DEFAULT_FLOW_GATE_OPEN_SURPLUS_C,
    DEFAULT_FLOW_THRESHOLD_C,
    HEAT_SOURCE_ACTIVE_STATE,
    LEARNING_CYCLE_INTERVAL_MINUTES,
    SENSOR_POLL_INTERVAL_MINUTES,
    Construction,
    CurveEnd,
    DesignTemperatureSystem,
    FlowSupplyStatus,
    HeatEmitterType,
    HeatMode,
    PanelRadiatorType,
)
from .control.flow_feedback import DemandSample
from .controller.mpc.controller import (
    FLOW_RECOVERY_HORIZON_S,
    FlowGateConfig,
    RoomMpcController,
)
from .controller.mpc.learner import JUMP_MIN_TARGET_STEP_C
from .controller.mpc.math_helper import clamp, round_to_step
from .controller.mpc.models.gains import GainBreakdown, profile_index
from .controller.mpc.results import RoomMpcResult
from .controller.mpc.sensors import VALVE_POSITION_MAX_AGE_S
from .controller.mpc.types import (
    FlowGateState,
    LearningFactors,
    RoomThermalConfig,
    TrvConfig,
)
from .controller.state import HeatingStateConfig, HeatingStateController
from .controller.trv_inputs import (
    LOAD_MEAN_INTERVAL_S,
    ExternalTemperatureSchedule,
    load_room_mean,
    raw_value,
)
from .irradiance import IrradianceProvider, SunSeries
from .profiles import TrvBinding
from .startup_gate import entity_ready
from .store import (
    FlowGateStateStore,
    GainStore,
    LearningFactorsStore,
    async_remove_setpoint_correction_store,
    async_remove_valve_curve_store,
)

_LOGGER = logging.getLogger(__name__)

RECOMPUTE_DEBOUNCE_S = 1.0
SUN_EVENT_CACHE_SIZE = 8

LEARNING_CYCLE_INTERVAL = timedelta(minutes=LEARNING_CYCLE_INTERVAL_MINUTES)

SENSOR_POLL_INTERVAL = timedelta(minutes=SENSOR_POLL_INTERVAL_MINUTES)

TARGET_TEMPERATURE_TOLERANCE_C = 0.01
# A Danfoss Ally reports its previous setpoint once more a few seconds after a
# write. Within this window the written target counts as the TRV's target, so
# the echo does not trigger a second write.
TARGET_WRITE_SETTLE_S = 120.0
# Without a target_temp_step attribute the TRV takes setpoints in this step.
DEFAULT_TARGET_TEMPERATURE_STEP_C = 0.5
# A room recovers with a raised flow (boost) only after its mode's setpoint
# went up by a jump, until it is this close to the new setpoint or the
# recovery horizon has passed.
FLOW_BOOST_DONE_BELOW_C = 0.2
# Heat stays available while the flow dips below the threshold for less than
# this: a defrost drops the flow by about 10 K for one 10-minute report.
FLOW_DIP_TOLERANCE_S = 20 * 60
# A write aborts a TRV's adaptation run, so nothing is written to a TRV while
# its profile reports one running -- but no longer than this, so a status that
# never clears cannot leave the room uncontrolled.
# A Danfoss Ally calibration took 1 h 46 to 1 h 49 min.
ADAPTATION_MAX_HOLD_S = 3 * 3600

WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S = 30 * 60
WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S = 10 * 60

SUNSET_LEARNING_DELAY = timedelta(hours=2)

# The heat pump reports its flow temperature about every 10 minutes and drops
# out for a poll now and then. Learning pauses only once no reading came for
# longer than this; the control keeps the last reading meanwhile.
FLOW_TEMPERATURE_MAX_GAP_S = 16 * 60


def _build_trv_configs(trvs: list[TrvBinding]) -> list[TrvConfig]:
    return [
        TrvConfig(
            name=trv.climate_entity,
            emitter_type=HeatEmitterType(trv.emitter[CONF_TRV_EMITTER_TYPE]),
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
        self._adaptation_status_entities: list[str] = [
            trv.adaptation_status_entity for trv in trvs if trv.adaptation_status_entity
        ]
        self._mounted_mode_entities: list[str] = [
            trv.mounted_mode_entity for trv in trvs if trv.mounted_mode_entity
        ]

        self._heat_source_climate_entity: str | None = self.data.get(
            CONF_HEAT_SOURCE_CLIMATE_ENTITY
        )
        self._flow_temperature_entity: str = self.data[CONF_FLOW_TEMPERATURE_ENTITY]
        # The heat source drops out for a few polls at a time while it keeps
        # running; its last known state bridges that.
        self._last_heat_source_state: str | None = None
        # Whether the flow has reached the threshold since the heat source
        # last stopped heating, and since when it has been below it.
        self._flow_heating = False
        self._flow_dip_since_ts: float | None = None
        # Same for the flow temperature entity, which drops out for a moment
        # whenever its template is reloaded.
        self._flow_temperature_c: float | None = None
        self._flow_threshold_entity: str | None = self.data.get(
            CONF_FLOW_THRESHOLD_ENTITY
        )
        # Set by the hub when a flow device owns the threshold.
        self.flow_threshold_provider: Callable[[], float] | None = None
        # Set by the hub when a hot water device sees the charges.
        self.hot_water_charge_provider: Callable[[], bool] | None = None
        # Set by the hub when a flow device knows the heating curve's outdoor
        # end points: (cold, warm).
        self.curve_outdoor_provider: Callable[[], tuple[float, float]] | None = None
        self._learning_block_entity: str | None = self.data.get(
            CONF_LEARNING_BLOCK_ENTITY
        )
        self._learning_blocked = False
        # When the flow temperature last had a reading; set on startup.
        self._flow_temperature_seen_ts = 0.0
        self._learning_paused = False

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
        # Until the switches restore: house on, the room's own switch off.
        self.follows_house_comfort: bool = self.data.get(
            CONF_USE_HOUSE_COMFORT_CONDITIONS, True
        )
        self.has_room_comfort_switch: bool = self.data.get(
            CONF_ROOM_COMFORT_SWITCH, False
        )
        if self.follows_house_comfort:
            self.state.set_house_comfort(True)
        if self.has_room_comfort_switch:
            self.state.set_room_comfort(False)

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
            window_surfaces=tuple(self.data.get(CONF_WINDOW_SURFACES) or ()),
            opaque_surfaces=tuple(self.data.get(CONF_OPAQUE_SURFACES) or ()),
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
            max_sensor_age_s=self.data[CONF_MAX_SENSOR_AGE],
            room_sensor_configured=bool(self.data.get(CONF_ROOM_SENSOR_ENTITY)),
            flow_gate_config=flow_gate_config,
        )
        self.mpc.set_sun_condition(self.is_sun_condition_met)
        self.mpc.set_gain_context(
            self.profile_index_at, self.local_day_at, self.fresh_sun_series
        )

        self.store = LearningFactorsStore(hass, self.room_name, room_id)
        self.flow_gate_store = FlowGateStateStore(hass, self.room_name, room_id)
        self._persisted_flow_gate_state: FlowGateState | None = None
        self.gain_store = GainStore(hass, self.room_name, room_id)
        # Set by the hub when some room has sun surfaces.
        self.sun_provider: IrradianceProvider | None = None
        self.workday_entity: str | None = None
        # Computes and writes only once the hub's startup gate opened.
        self.running = False

        self.blocked = False
        self.trv_active = True
        self.last_result: RoomMpcResult | None = None
        # Target writes per TRV since setup: every write wakes the TRV.
        self.trv_setpoint_writes: dict[str, int] = {}
        self._written_targets: dict[str, tuple[float, float]] = {}
        self._adaptation_since: dict[str, float] = {}
        self.normal_result: RoomMpcResult | None = None
        self.normal_heat_mode: HeatMode | None = None
        self.normal_target_temperature_c: float | None = None
        # The setpoint handed to the TRVs: the active mode's, or frost
        # protection for an open window.
        self.mode_target_temperature_c: float | None = None
        # The active mode's own setpoint; an open window does not change it.
        self._settled_target_c: float | None = None
        # Since when the room recovers after a raised mode setpoint.
        self._flow_boost_since_ts: float | None = None
        # External temperature writes per TRV since setup, and when each TRV
        # gets the room sensor's reading next.
        self.trv_external_writes: dict[str, int] = {}
        self._external_schedules: dict[str, ExternalTemperatureSchedule] = {
            trv.external_temperature_entity: ExternalTemperatureSchedule()
            for trv in trvs
            if trv.external_temperature_entity
        }
        # TRVs handed back their own sensor since setup (room without sensor).
        self._external_off_sent: set[str] = set()
        # The room's mean load last handed to its TRVs; None if they work alone.
        self.load_room_mean: int | None = None
        self._started_ts = 0.0

        self._unsub: list[Callable[[], None]] = []
        # Entities report in bursts (a TRV its temperature and valve position,
        # the heat pump flow and outdoor temperature): one recompute per burst.
        self._pending_recompute: asyncio.Task[None] | None = None
        # A recompute awaits TRV writes; a second one in between would compute
        # and write on half-updated state.
        self._recompute_lock = asyncio.Lock()
        self._sun_events: dict[tuple[str, date], datetime | None] = {}
        self._listeners: list[Callable[[], None]] = []

    def set_sun_provider(
        self, provider: IrradianceProvider, workday_entity: str | None
    ) -> None:
        self.sun_provider = provider
        self.workday_entity = workday_entity

    def fresh_sun_series(self) -> SunSeries | None:
        """The hub's irradiance series, unless it is too old to use."""
        provider = self.sun_provider
        if provider is None or not provider.series.fresh(time.time()):
            return None
        return provider.series

    def is_workday(self, timestamp: float) -> bool:
        """The workday entity's state; without one Monday to Friday."""
        if self.workday_entity:
            state = self.hass.states.get(self.workday_entity)
            if state is not None and state.state in (STATE_ON, STATE_OFF):
                return state.state == STATE_ON
        return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).weekday() < 5

    def profile_index_at(self, timestamp: float) -> int:
        local = dt_util.as_local(dt_util.utc_from_timestamp(timestamp))
        return profile_index(local.hour, self.is_workday(timestamp))

    @staticmethod
    def local_day_at(timestamp: float) -> int:
        """Ordinal of the date in HA's time zone."""
        return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).toordinal()

    def current_gain(self) -> GainBreakdown:
        now_ts = time.time()
        gains = self.mpc.gain_model
        return gains.breakdown(
            self.profile_index_at(now_ts),
            gains.sun_regressors(self.fresh_sun_series(), now_ts),
        )

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    def _notify_listeners(self) -> None:
        for update_callback in self._listeners:
            update_callback()

    async def async_setup(self) -> None:
        self._started_ts = time.time()
        factors = await self.store.async_load()
        if factors is not None:
            self.mpc.recalibrate_learning_factors(factors)

        gains = await self.gain_store.async_load()
        if gains is not None:
            self.mpc.gain_model.restore(gains)

        flow_gate_state = await self.flow_gate_store.async_load()
        if flow_gate_state is not None:
            self.mpc.restore_flow_gate_state(flow_gate_state)
            self._persisted_flow_gate_state = flow_gate_state

        await async_remove_valve_curve_store(self.hass, self.room_name, self.room_id)
        await async_remove_setpoint_correction_store(
            self.hass, self.room_name, self.room_id
        )

        for entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            self.state.update_window_state(entity_id, self._is_on(entity_id))
        self.mpc.set_window_open(self.state.is_window_open, time.time())
        self._update_window_unknown()

        if self._learning_block_entity:
            self._learning_blocked = self._is_on(self._learning_block_entity)

        # A missing flow reading counts from startup on, like a gap.
        self._flow_temperature_seen_ts = self._started_ts
        self._refresh_temperature_inputs()
        self._update_learning_paused(time.time())

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)

        trvs_with_external = [
            trv for trv in self._trvs if trv.external_temperature_entity
        ]
        tracked_entities = list(self._trv_entity_ids)
        tracked_entities.extend(
            entity_id for entity_id in self._valve_position_entities if entity_id
        )
        tracked_entities.extend(self._adaptation_status_entities)
        tracked_entities.extend(self._mounted_mode_entities)
        # A covered radiator wants the room sensor's reading more often.
        tracked_entities.extend(
            trv.radiator_covered_entity
            for trv in trvs_with_external
            if trv.radiator_covered_entity
        )
        tracked_entities.extend(self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []))
        tracked_entities.append(self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY])
        tracked_entities.append(self._flow_temperature_entity)
        if self._heat_source_climate_entity:
            tracked_entities.append(self._heat_source_climate_entity)
        if self._flow_threshold_entity:
            tracked_entities.append(self._flow_threshold_entity)
        if self._learning_block_entity:
            tracked_entities.append(self._learning_block_entity)
        if room_sensor_entity:
            tracked_entities.append(room_sensor_entity)
        # Written entities that come back after an outage get their value again.
        tracked_entities.extend(self._rewrite_entities)

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
        if any(trv.shares_load for trv in self._trvs):
            self._unsub.append(
                async_track_time_interval(
                    self.hass,
                    self._async_handle_load_sharing,
                    timedelta(seconds=LOAD_MEAN_INTERVAL_S),
                )
            )

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
        await self._async_update_load_sharing(time.time())

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
                index, self._climate_temperature_from_state(state)
            )
        for index in range(len(self._valve_position_entities)):
            self._update_valve_position(index)

        room_sensor_entity = self.data.get(CONF_ROOM_SENSOR_ENTITY)
        if room_sensor_entity:
            room_sensor_temp_c = self._float_state(room_sensor_entity)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)

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
        self.hass.config_entries.async_schedule_reload(self.hub_entry.entry_id)

    async def _async_handle_sensor_poll(self, _now: Any) -> None:
        self._refresh_temperature_inputs()
        await self._async_recompute()

    def _update_flow_temperature(self, state: State | None) -> None:
        value = self._float_from_state(state)
        if value is not None:
            self._flow_temperature_c = value
            self._flow_temperature_seen_ts = time.time()
        self.mpc.set_flow_temperature(self._flow_temperature_c)

    @property
    def flow_temperature_missing(self) -> bool:
        """No flow reading for longer than the heat pump's usual poll gaps."""
        return time.time() - self._flow_temperature_seen_ts > FLOW_TEMPERATURE_MAX_GAP_S

    def _update_learning_paused(self, now_ts: float) -> None:
        paused = self._learning_blocked or self.flow_temperature_missing
        if paused != self._learning_paused:
            self._learning_paused = paused
            self.mpc.set_learning_paused(paused, now_ts)

    def _compute_trv_active(self, now_ts: float) -> bool:
        if not self._heat_source_climate_entity:
            # Nothing tells when the heat is gone; the TRVs find out themselves.
            return True
        climate_state = self.hass.states.get(self._heat_source_climate_entity)
        if climate_state is not None and climate_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._last_heat_source_state = climate_state.state
        if self._last_heat_source_state != HEAT_SOURCE_ACTIVE_STATE:
            self._flow_heating = False
            self._flow_dip_since_ts = None
            return False
        flow_temp_c = self.current_flow_temperature_c
        if flow_temp_c is not None and flow_temp_c >= self.flow_threshold_c:
            self._flow_heating = True
            self._flow_dip_since_ts = None
            return True
        if not self._flow_heating or flow_temp_c is None:
            self._flow_heating = False
            return False
        if self._flow_dip_since_ts is None:
            self._flow_dip_since_ts = now_ts
        if now_ts - self._flow_dip_since_ts < FLOW_DIP_TOLERANCE_S:
            return True
        self._flow_heating = False
        self._flow_dip_since_ts = None
        return False

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

    def _trv_uncalibrated(self, trv: TrvBinding) -> bool:
        """Whether the TRV is mounting or its valve is not calibrated.

        Its valve then follows the mounting or the TRV's own search, not the
        setpoint, so the room learns nothing. Unlike a running adaptation
        this does not stop writes: a TRV left uncalibrated still has to heat.
        """
        if trv.mounted_mode_entity is not None:
            mounted = self.hass.states.get(trv.mounted_mode_entity)
            if mounted is not None and mounted.state == STATE_ON:
                return True
        if trv.adaptation_ready is None or trv.adaptation_status_entity is None:
            return False
        return not trv.adaptation_ready.matches(
            self.hass.states.get(trv.adaptation_status_entity)
        )

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

    def _trv_reported_ts(self, index: int) -> float | None:
        """When the TRV last reported its temperature or valve opening."""
        reported = [
            state.last_reported.timestamp()
            for entity_id in (
                self._trv_entity_ids[index],
                self._valve_position_entities[index],
            )
            if entity_id
            and (state := self.hass.states.get(entity_id)) is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ]
        return max(reported, default=None)

    def _update_valve_position(self, index: int) -> None:
        entity_id = self._valve_position_entities[index]
        if entity_id:
            self.mpc.set_valve_position(
                index, self._float_state(entity_id), self._trv_reported_ts(index)
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
        return self._float_state(entity_id)

    def _is_on(self, entity_id: str) -> bool:
        return self._bool_from_state(self.hass.states.get(entity_id))

    def _float_state(self, entity_id: str) -> float | None:
        return self._float_from_state(self.hass.states.get(entity_id))

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
        schedule = self._external_schedules.get(entity_id)
        if schedule is not None:
            schedule.reset()
        self._external_off_sent.discard(entity_id)
        if any(
            entity_id in (trv.load_balancing_entity, trv.load_room_mean_entity)
            for trv in self._trvs
        ):
            self.hass.async_create_task(self._async_update_load_sharing(time.time()))

    async def _async_recompute_after_burst(self) -> None:
        await asyncio.sleep(RECOMPUTE_DEBOUNCE_S)
        # Cleared first: a change that arrives while this one computes gets
        # its own recompute.
        self._pending_recompute = None
        await self._async_recompute()

    def _apply_state_change(self, entity_id: str, new_state: State | None) -> None:
        if entity_id in self._trv_entity_ids:
            index = self._trv_entity_ids.index(entity_id)
            self.mpc.set_trv_temperature(
                index, self._climate_temperature_from_state(new_state)
            )
            # The TRV reported: its unchanged valve opening is still current.
            self._update_valve_position(index)

        elif entity_id in self._valve_position_entities:
            self._update_valve_position(self._valve_position_entities.index(entity_id))

        elif entity_id == self.data.get(CONF_ROOM_SENSOR_ENTITY):
            room_sensor_temp_c = self._float_from_state(new_state)
            self.mpc.set_room_sensor_temperature(room_sensor_temp_c)

        elif entity_id == self.data[CONF_OUTDOOR_TEMPERATURE_ENTITY]:
            self.mpc.set_outdoor_temperature(self._float_from_state(new_state))

        elif entity_id == self._flow_temperature_entity:
            self._update_flow_temperature(new_state)

        elif entity_id in self.data.get(CONF_WINDOW_CONTACT_ENTITIES, []):
            was_open = self.state.is_window_open
            _, is_open = self.state.update_window_state(
                entity_id, self._bool_from_state(new_state)
            )
            self.mpc.set_window_open(is_open, time.time())
            self._update_window_unknown()
            if was_open and not is_open:
                self.mpc.suppress_learning_for_interval(
                    WINDOW_CLOSED_SUPPRESS_UA_LEARNING_S,
                    WINDOW_CLOSED_SUPPRESS_CAPACITY_LEARNING_S,
                )

        elif entity_id == self._learning_block_entity:
            self._learning_blocked = self._bool_from_state(new_state)

    async def _async_handle_learning_cycle(self, _now: Any) -> None:
        if not self.running:
            return
        self.mpc.run_learning_cycle()
        await self._async_persist_learning_factors()
        self._notify_listeners()

    async def _async_persist_learning_factors(self) -> None:
        persisted_factors = self.mpc.consume_persisted_learning_factors()
        if persisted_factors is not None:
            await self.store.async_save(persisted_factors)
        if self.mpc.gain_model.consume_changed():
            await self.gain_store.async_save(self.mpc.gain_model.export())

    async def async_recalibrate(
        self,
        ua_factor: float | None,
        capacity_factor: float | None,
        reset_gains: bool = False,
    ) -> None:
        """Set the factors; one left out keeps its learned value."""
        if reset_gains:
            self.mpc.gain_model.reset()
            await self.gain_store.async_save(self.mpc.gain_model.export())
            self.mpc.gain_model.consume_changed()
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
                self.mpc.learned_vent_capacity_factor
                if capacity_factor is None
                else None
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

        sunset_today = self._sun_event(SUN_EVENT_SUNSET, today)
        if sunset_today is not None and moment >= sunset_today + SUNSET_LEARNING_DELAY:
            return True

        sunrise_today = self._sun_event(SUN_EVENT_SUNRISE, today)
        sunset_yesterday = self._sun_event(SUN_EVENT_SUNSET, today - timedelta(days=1))
        if sunrise_today is None or sunset_yesterday is None:
            return False
        return sunset_yesterday + SUNSET_LEARNING_DELAY <= moment < sunrise_today

    def _sun_event(self, event: str, day: date) -> datetime | None:
        # The learner asks for every sample of its history on every cycle.
        key = (event, day)
        if key not in self._sun_events:
            if len(self._sun_events) >= SUN_EVENT_CACHE_SIZE:
                self._sun_events.clear()
            self._sun_events[key] = get_astral_event_date(self.hass, event, day)
        return self._sun_events[key]

    @property
    def flow_threshold_c(self) -> float:
        """The lowest flow temperature the heat source can usefully deliver.

        Read from the linked entity on every access so the limit can be tried
        out on the heat pump by changing a single helper. Without a linked
        entity, or while it reports nothing usable, the hub's fixed threshold
        keeps the room controllable instead of blocking it.
        """
        if self.flow_threshold_provider is not None:
            return self.flow_threshold_provider()
        fixed = self.data.get(CONF_FLOW_THRESHOLD, DEFAULT_FLOW_THRESHOLD_C)
        if not self._flow_threshold_entity:
            return fixed
        value = self._float_state(self._flow_threshold_entity)
        if value is None:
            return fixed
        return value

    async def _async_recompute(self) -> None:
        async with self._recompute_lock:
            await self._async_recompute_locked()

    async def _async_recompute_locked(self) -> None:
        if not self.running:
            return
        self.mpc.set_flow_threshold_c(self.flow_threshold_c)
        if self.hot_water_charge_provider is not None:
            self.mpc.set_external_hot_water_charge(self.hot_water_charge_provider())
        desired_mode = self.state.desired_automatic_heat_mode(self.blocked)
        self.state.set_active_heat_mode(desired_mode)

        now_ts = time.time()
        self._update_learning_paused(now_ts)
        self.mpc.set_trv_adapting(
            any(
                self._adaptation_running(trv, now_ts) or self._trv_uncalibrated(trv)
                for trv in self._trvs
            )
        )
        self.trv_active = self._compute_trv_active(now_ts)
        self.state.set_trv_active(self.trv_active)
        self.mpc.set_heat_available(self.trv_active)
        if self.trv_active:
            self.mpc.enable_learning()
        else:
            self.mpc.disable_learning()

        display_mode = self.state.resolve_display_mode(self.blocked)
        mode_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(display_mode)
        )
        self.mode_target_temperature_c = mode_target
        normal_target = self.state.effective_target_temperature(
            self.state.determine_base_target_temperature(self.state.current_heat_mode)
        )
        if normal_target != self._settled_target_c:
            previous_c = self._settled_target_c
            self._settled_target_c = normal_target
            if (
                previous_c is not None
                and normal_target - previous_c >= JUMP_MIN_TARGET_STEP_C
            ):
                self._flow_boost_since_ts = now_ts
            elif previous_c is not None and normal_target < previous_c:
                self._flow_boost_since_ts = None
        self._update_flow_boost_end(normal_target, now_ts)
        await self._async_write_setpoints(mode_target)
        await self._async_write_external_temperatures(now_ts)

        self.normal_heat_mode = self.state.current_heat_mode
        self.normal_target_temperature_c = normal_target
        normal_compute = self.mpc.compute(
            normal_target, apply_side_effects=False, recovery=self.flow_boost_active
        )
        self.normal_result = normal_compute.result if normal_compute.valid else None

        # Frost protection for an open window is no mode change for the
        # learner.
        actual_compute = self.mpc.compute(
            mode_target,
            mode_target_temperature_c=normal_target,
            recovery=self.flow_boost_active,
        )
        if actual_compute.valid:
            self.last_result = actual_compute.result
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

    @property
    def trv_target_temperatures(self) -> dict[str, float | None]:
        """Each TRV's target as last written or reported."""
        return {
            entity_id: self._current_trv_target(entity_id)
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

    def _update_flow_boost_end(self, target_c: float, now_ts: float) -> None:
        if self._flow_boost_since_ts is None:
            return
        room_c = (
            self.normal_result.input.room_temp_c
            if self.normal_result is not None
            else None
        )
        if now_ts - self._flow_boost_since_ts >= FLOW_RECOVERY_HORIZON_S or (
            room_c is not None and room_c >= target_c - FLOW_BOOST_DONE_BELOW_C
        ):
            self._flow_boost_since_ts = None

    @property
    def flow_boost_active(self) -> bool:
        """The room recovers after its mode's setpoint went up."""
        return self._flow_boost_since_ts is not None

    @property
    def flow_boost_temperature_c(self) -> float | None:
        """The recovery flow while the room recovers; None otherwise."""
        if not self.flow_boost_active:
            return None
        return self.normal_recovery_flow_temperature_c

    @property
    def feedback_sample(self) -> DemandSample | None:
        """The room's valve demand for the flow feedback; None if not usable.

        Left out: an open window, frost protection, a radiator that does not
        heat, a running boost, and a TRV whose opening is unknown.
        """
        if (
            self.state.is_window_open
            or self.current_heat_mode is HeatMode.FROST_PROTECTION
            or self.radiators_not_heating
            or self.flow_boost_active
        ):
            return None
        demands = [
            self._trv_demand_pct(index)
            for index, entity_id in enumerate(self._valve_position_entities)
            if entity_id
        ]
        if not demands or any(demand is None for demand in demands):
            return None
        target_c = self._settled_target_c
        room_c = self._room_sensor_temperature_c()
        if room_c is None and self.normal_result is not None:
            room_c = self.normal_result.input.room_temp_c
        deficit_c = (
            None if target_c is None or room_c is None else round(target_c - room_c, 2)
        )
        return DemandSample(self.room_name, max(demands), deficit_c)

    def _room_sensor_temperature_c(self) -> float | None:
        """The room sensor's reading; None without one or while it is stale."""
        room = self.mpc.get_room_temperature_result()
        if not room.valid or room.room_sensor_fallback:
            return None
        return room.room_sensor_temp_c

    async def _async_write_external_temperatures(self, now_ts: float) -> None:
        """Hand the TRVs that take one the room sensor's reading.

        While the reading is unknown nothing is sent, and the TRV returns to
        its own sensor after its timeout. A room without a room sensor hands
        its TRVs back their own once.
        """
        has_room_sensor = bool(self.data.get(CONF_ROOM_SENSOR_ENTITY))
        room_c = self._room_sensor_temperature_c()
        for trv in self._trvs:
            entity_id = trv.external_temperature_entity
            if entity_id is None or self._adaptation_running(trv, now_ts):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                continue
            if not has_room_sensor:
                off_value = trv.external_off_value
                if (
                    off_value is not None
                    and entity_id not in self._external_off_sent
                    and self._float_from_state(state) != off_value
                ):
                    await self._async_set_number(entity_id, off_value)
                    self._count_external_write(entity_id)
                self._external_off_sent.add(entity_id)
                continue
            if room_c is None:
                continue
            covered = trv.radiator_covered_entity is not None and self._is_on(
                trv.radiator_covered_entity
            )
            schedule = self._external_schedules[entity_id]
            if not schedule.due(room_c, covered, now_ts):
                continue
            await self._async_set_number(
                entity_id, raw_value(room_c, trv.external_scale)
            )
            schedule.sent(room_c, covered, now_ts)
            self._count_external_write(entity_id)

    def _count_external_write(self, entity_id: str) -> None:
        self.trv_external_writes[entity_id] = (
            self.trv_external_writes.get(entity_id, 0) + 1
        )

    async def _async_set_number(self, entity_id: str, value: float) -> None:
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=False,
        )

    async def _async_handle_load_sharing(self, _now: Any) -> None:
        await self._async_update_load_sharing(time.time())

    async def _async_update_load_sharing(self, now_ts: float) -> None:
        """Share the load between the room's TRVs, if it has two or more.

        Each TRV takes part only while it gets the room's mean load, so a
        TRV that cannot get it, or the only one in its room, works alone.
        """
        if not self.running:
            return
        sharing = [trv for trv in self._trvs if trv.shares_load]
        balance = len(sharing) >= 2
        for trv in self._trvs:
            entity_id = trv.load_balancing_entity
            if entity_id is None or self._adaptation_running(trv, now_ts):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state not in (STATE_ON, STATE_OFF):
                continue
            wanted = balance and trv.shares_load
            if (state.state == STATE_ON) != wanted:
                await self.hass.services.async_call(
                    "switch",
                    "turn_on" if wanted else "turn_off",
                    {"entity_id": entity_id},
                    blocking=False,
                )
        if not balance:
            self.load_room_mean = None
            return
        mean = load_room_mean(
            [
                (
                    self._float_from_state(state),
                    state.last_reported.timestamp() if state is not None else None,
                )
                for trv in sharing
                for state in (self.hass.states.get(trv.load_estimate_entity),)
            ],
            now_ts,
        )
        self.load_room_mean = mean
        if mean is None:
            return
        for trv in sharing:
            entity_id = trv.load_room_mean_entity
            if self._adaptation_running(trv, now_ts):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                continue
            await self._async_set_number(entity_id, mean)

    @staticmethod
    def _trv_setpoint_c(state: State, target_c: float) -> float:
        """The target as the TRV's climate entity takes it."""
        attributes = state.attributes
        step_c = attributes.get("target_temp_step") or DEFAULT_TARGET_TEMPERATURE_STEP_C
        setpoint_c = round_to_step(target_c, step_c)
        min_c = attributes.get("min_temp")
        max_c = attributes.get("max_temp")
        if min_c is not None and max_c is not None:
            setpoint_c = clamp(setpoint_c, min_c, max_c)
        return setpoint_c

    async def _async_write_setpoints(self, target_c: float) -> None:
        """Hand every TRV the room's setpoint; it regulates on its own."""
        now_ts = time.time()
        for trv in self._trvs:
            entity_id = trv.climate_entity
            # Every write wakes the TRV and aborts a running adaptation run,
            # so only send a target the TRV does not already report, none to a
            # TRV that cannot take it, and none while it adapts.
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            if self._adaptation_running(trv, now_ts):
                continue
            setpoint_c = self._trv_setpoint_c(state, target_c)
            current_target_c = self._current_trv_target(entity_id)
            if (
                current_target_c is not None
                and abs(current_target_c - setpoint_c) < TARGET_TEMPERATURE_TOLERANCE_C
            ):
                continue
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": setpoint_c},
                blocking=False,
            )
            self.trv_setpoint_writes[entity_id] = (
                self.trv_setpoint_writes.get(entity_id, 0) + 1
            )
            self._written_targets[entity_id] = (setpoint_c, now_ts)

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
                self._trv_entity_ids, self.mpc.radiators_not_heating
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
    def comfort_temperature_c(self) -> float:
        return self.state.determine_base_target_temperature(HeatMode.COMFORT)

    def curve_outdoor_c(self, end: CurveEnd) -> float | None:
        """Outdoor value of one end of the heat source's curve; None without one."""
        if self.curve_outdoor_provider is None:
            return None
        cold_c, warm_c = self.curve_outdoor_provider()
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

    def comfort_hold_flow_c(self, outdoor_c: float) -> float | None:
        """Flow that holds the comfort temperature at an outdoor value."""
        return self.mpc.calculate_hold_flow_temperature_c(
            self.comfort_temperature_c, outdoor_c
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
    def normal_recovery_flow_temperature_c(self) -> float | None:
        result = self.normal_result
        return result.recovery_flow_temperature_c if result else None

    @property
    def design_flow_temperature_c(self) -> float:
        return self.mpc.design_flow_temperature_c

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

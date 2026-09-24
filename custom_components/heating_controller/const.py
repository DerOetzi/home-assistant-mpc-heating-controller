from __future__ import annotations

from enum import StrEnum

DOMAIN = "heating_controller"

DEFAULT_COMFORT_TEMPERATURE_C = 22.0
DEFAULT_ECO_TEMPERATURE_OFFSET_C = -2.0
DEFAULT_FLOW_THRESHOLD_C = 30.0

DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C = 0.5
DEFAULT_FLOW_GATE_OPEN_SURPLUS_C = 0.2
DEFAULT_FLOW_GATE_HOLD_TIME_S = 900.0

MAX_TRV_COUNT = 3

HEAT_SOURCE_ACTIVE_STATE = "heat"

CONF_ROOM_NAME = "room_name"
CONF_TRV_COUNT = "trv_count"
CONF_TRVS = "trvs"
CONF_TRV_ENTITY_ID = "entity_id"
CONF_TRV_EMITTER_TYPE = "emitter_type"
CONF_TRV_MIN_TARGET_TEMPERATURE = "min_target_temperature_c"
CONF_TRV_MAX_TARGET_TEMPERATURE = "max_target_temperature_c"
CONF_TRV_TARGET_TEMPERATURE_STEP = "target_temperature_step_c"
CONF_TRV_RADIATOR_TYPE = "radiator_type"
CONF_TRV_WIDTH_MM = "width_mm"
CONF_TRV_HEIGHT_MM = "height_mm"
CONF_TRV_NOMINAL_POWER_W = "nominal_power_w"
CONF_TRV_ACTIVE_SWITCH = "trv_active_switch"

CONF_ROOM_SENSOR_ENTITY = "room_sensor_entity"
CONF_WINDOW_CONTACT_ENTITIES = "window_contact_entities"
CONF_COMFORT_CONDITION_ENTITIES = "comfort_condition_entities"
CONF_ROOM_COMFORT_CONDITION_ENTITIES = "room_comfort_condition_entities"
CONF_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
CONF_HEAT_SOURCE_CLIMATE_ENTITY = "heat_source_climate_entity"
CONF_FLOW_THRESHOLD = "flow_threshold_c"
CONF_FLOW_THRESHOLD_ENTITY = "flow_threshold_entity"
CONF_PV_BOOST_ENTITY = "pv_boost_entity"

CONF_BOOST_ENABLED = "boost_enabled"
CONF_BOOST_TEMPERATURE_OFFSET = "boost_temperature_offset_c"
CONF_FROST_PROTECTION_TEMPERATURE = "frost_protection_temperature_c"
CONF_PV_BOOST_ENABLED = "pv_boost_enabled"
CONF_PV_BOOST_TEMPERATURE_OFFSET = "pv_boost_temperature_offset_c"

CONF_DESIGN_INDOOR_TEMPERATURE = "design_indoor_temperature_c"
CONF_DESIGN_OUTDOOR_TEMPERATURE = "design_outdoor_temperature_c"
CONF_DESIGN_TEMPERATURE_SYSTEM = "design_temperature_system"
CONF_ROOM_HEAT_LOAD = "room_heat_load_w"
CONF_MPC_DEMAND_HYSTERESIS_PCT = "mpc_demand_hysteresis_pct"
CONF_MPC_HOLD_TIME = "mpc_hold_time_s"
CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT = "mpc_hold_override_demand_pct"
CONF_MPC_MAX_DEMAND_STEP_PCT = "mpc_max_demand_step_pct"
CONF_FLOW_GATE_CLOSE_SURPLUS = "flow_gate_close_surplus_c"
CONF_FLOW_GATE_OPEN_SURPLUS = "flow_gate_open_surplus_c"
CONF_FLOW_GATE_HOLD_TIME = "flow_gate_hold_time_s"
CONF_MAX_SENSOR_AGE = "max_sensor_age_s"

SERVICE_UNBLOCK = "unblock"

LEARNING_CYCLE_INTERVAL_MINUTES = 30
SENSOR_POLL_INTERVAL_MINUTES = 5


class HeatMode(StrEnum):
    COMFORT = "comfort"
    ECO = "eco"
    BOOST = "boost"
    FROST_PROTECTION = "frost_protection"


class FlowSupplyStatus(StrEnum):
    """The minimum-flow-temperature sensor's situation, in one value.

    Replaces comparing sufficiently_supplied/below_operating_threshold
    separately -- those alone cannot tell "no requirement" from "requirement
    exists but can never bind" from "source isn't even running".
    """

    NO_REQUIREMENT = "no_requirement"
    SURPLUS = "surplus"
    BELOW_THRESHOLD = "below_threshold"
    SOURCE_INACTIVE = "source_inactive"
    UNDERSUPPLIED = "undersupplied"
    SUFFICIENT = "sufficient"


class HeatEmitterType(StrEnum):
    PANEL = "panel"
    TOWEL = "towel"


class PanelRadiatorType(StrEnum):
    TYPE_10 = "10"
    TYPE_11 = "11"
    TYPE_21 = "21"
    TYPE_22 = "22"
    TYPE_33 = "33"


class DesignTemperatureSystem(StrEnum):
    SYSTEM_75_65 = "system_75_65"
    SYSTEM_70_55 = "system_70_55"
    SYSTEM_55_45 = "system_55_45"
    SYSTEM_45_35 = "system_45_35"
    SYSTEM_35_30 = "system_35_30"


class RoomTemperatureStrategy(StrEnum):
    ROOM_SENSOR = "room_sensor"
    TRV_AVERAGE = "trv_average"


class LearningStatus(StrEnum):
    LEARNED = "learned"
    DISABLED = "disabled"
    NO_CORRECTION = "no_correction"
    DISTURBED = "disturbed"
    SUPPRESSED = "suppressed"
    WAITING_INTERVAL = "waiting_interval"

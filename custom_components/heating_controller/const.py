from __future__ import annotations

from enum import StrEnum

DOMAIN = "heating_controller"

DEFAULT_COMFORT_TEMPERATURE_C = 22.0
DEFAULT_ECO_TEMPERATURE_OFFSET_C = -2.0
DEFAULT_FLOW_THRESHOLD_C = 30.0

DEFAULT_FLOW_GATE_CLOSE_SURPLUS_C = 0.5
DEFAULT_FLOW_GATE_OPEN_SURPLUS_C = 0.2
DEFAULT_FLOW_GATE_HOLD_TIME_S = 900.0

DEFAULT_STATIONARY_RANGE_C = 0.2
MIN_STATIONARY_RANGE_C = 0.1
MAX_STATIONARY_RANGE_C = 1.0
STATIONARY_RANGE_STEP_C = 0.05

# Band below the setpoint the demand counts as on target in: one step of the
# usual 0.1 K room sensor.
DEFAULT_TARGET_BAND_C = 0.1
MIN_TARGET_BAND_C = 0.0
MAX_TARGET_BAND_C = 0.5
TARGET_BAND_STEP_C = 0.05

MAX_TRV_COUNT = 3

HEAT_SOURCE_ACTIVE_STATE = "heat"

CONF_ROOM_NAME = "room_name"
CONF_TRV_COUNT = "trv_count"
CONF_TRVS = "trvs"
CONF_TRV_DEVICE_ID = "device_id"
CONF_TRV_EMITTER_TYPE = "emitter_type"
CONF_TRV_RADIATOR_TYPE = "radiator_type"
CONF_TRV_WIDTH_MM = "width_mm"
CONF_TRV_HEIGHT_MM = "height_mm"
CONF_TRV_NOMINAL_POWER_W = "nominal_power_w"

CONF_ROOM_SENSOR_ENTITY = "room_sensor_entity"
CONF_WINDOW_CONTACT_ENTITIES = "window_contact_entities"
CONF_COMFORT_CONDITION_ENTITIES = "comfort_condition_entities"
CONF_ROOM_COMFORT_CONDITION_ENTITIES = "room_comfort_condition_entities"
CONF_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
CONF_HEAT_SOURCE_CLIMATE_ENTITY = "heat_source_climate_entity"
CONF_FLOW_THRESHOLD = "flow_threshold_c"
CONF_FLOW_THRESHOLD_ENTITY = "flow_threshold_entity"
CONF_PV_BOOST_ENTITY = "pv_boost_entity"
CONF_FLOW_TEMPERATURE_ENTITY = "flow_temperature_entity"
# The heat pump's effective heating curve; the hub reports the reserve of the
# lead rooms against it.
CONF_FLOW_CURVE_ENTITY = "flow_curve_entity"
# Learning stops while this room entity is on (e.g. an extra heater runs).
CONF_LEARNING_BLOCK_ENTITY = "learning_block_entity"
CONF_USE_HOUSE_COMFORT_CONDITIONS = "use_house_comfort_conditions"
CONF_LEAD_HEATING_LIMIT = "lead_heating_limit"
CONF_LEAD_FLOW = "lead_flow"

SUBENTRY_TYPE_ROOM = "room"
HUB_TITLE = "Heizung"

CONF_BOOST_ENABLED = "boost_enabled"
CONF_BOOST_TEMPERATURE_OFFSET = "boost_temperature_offset_c"
CONF_FROST_PROTECTION_TEMPERATURE = "frost_protection_temperature_c"
CONF_PV_BOOST_ENABLED = "pv_boost_enabled"
CONF_PV_BOOST_TEMPERATURE_OFFSET = "pv_boost_temperature_offset_c"

CONF_DESIGN_INDOOR_TEMPERATURE = "design_indoor_temperature_c"
CONF_DESIGN_OUTDOOR_TEMPERATURE = "design_outdoor_temperature_c"
CONF_DESIGN_TEMPERATURE_SYSTEM = "design_temperature_system"
CONF_ROOM_HEAT_LOAD = "room_heat_load_w"
CONF_ROOM_AREA = "room_area_m2"
CONF_CONSTRUCTION = "construction"
CONF_MPC_DEMAND_HYSTERESIS_PCT = "mpc_demand_hysteresis_pct"
CONF_MPC_HOLD_TIME = "mpc_hold_time_s"
CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT = "mpc_hold_override_demand_pct"
CONF_MPC_MAX_DEMAND_STEP_PCT = "mpc_max_demand_step_pct"
CONF_FLOW_GATE_CLOSE_SURPLUS = "flow_gate_close_surplus_c"
CONF_FLOW_GATE_OPEN_SURPLUS = "flow_gate_open_surplus_c"
CONF_FLOW_GATE_HOLD_TIME = "flow_gate_hold_time_s"
CONF_MAX_SENSOR_AGE = "max_sensor_age_s"
CONF_STATIONARY_RANGE = "stationary_range_c"
CONF_TARGET_BAND = "target_band_c"
CONF_TRV_SETPOINT_MODE = "trv_setpoint_mode"
# Sun on the building: how the house is turned, its roof, and which day
# counts as a workday for the gain profile.
CONF_BUILDING_ROTATION = "building_rotation_deg"
CONF_ROOF_ORIENTATION = "roof_orientation"
CONF_ROOF_TILT = "roof_tilt_deg"
CONF_WORKDAY_ENTITY = "workday_entity"
# Per room: surfaces whose windows let the sun in at once, and outer walls or
# roof surfaces that pass it on with a delay.
CONF_WINDOW_SURFACES = "window_surfaces"
CONF_OPAQUE_SURFACES = "opaque_surfaces"
DEFAULT_ROOF_TILT_DEG = 45.0

# Writes to the heat source's cloud: an optional entity with the calls left
# today, the calls kept back for required writes and the integration's own
# daily maximum.
CONF_RATE_LIMIT_REMAINING_ENTITY = "rate_limit_remaining_entity"
CONF_RATE_LIMIT_RESERVE = "rate_limit_reserve"
CONF_MAX_WRITES_PER_DAY = "max_writes_per_day"
DEFAULT_RATE_LIMIT_RESERVE = 20
DEFAULT_MAX_WRITES_PER_DAY = 40
# The heat source counts as unreachable once its flow value is missing
# this long.
CONF_MAX_FLOW_GAP_MIN = "max_flow_gap_min"
DEFAULT_MAX_FLOW_GAP_MIN = 30

# Heating season recommendations (hub numbers).
DEFAULT_SEASON_ON_THRESHOLD_H = 6.0
DEFAULT_SEASON_HOLD_THRESHOLD_H = 2.0
DEFAULT_SEASON_OFF_AFTER_H = 48.0

# Settings of the whole house, held once by the hub entry.
HUB_KEYS = (
    CONF_RATE_LIMIT_REMAINING_ENTITY,
    CONF_RATE_LIMIT_RESERVE,
    CONF_MAX_WRITES_PER_DAY,
    CONF_MAX_FLOW_GAP_MIN,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_HEAT_SOURCE_CLIMATE_ENTITY,
    CONF_FLOW_TEMPERATURE_ENTITY,
    CONF_FLOW_THRESHOLD,
    CONF_FLOW_THRESHOLD_ENTITY,
    CONF_FLOW_CURVE_ENTITY,
    CONF_PV_BOOST_ENTITY,
    CONF_COMFORT_CONDITION_ENTITIES,
    CONF_FROST_PROTECTION_TEMPERATURE,
    CONF_DESIGN_OUTDOOR_TEMPERATURE,
    CONF_DESIGN_TEMPERATURE_SYSTEM,
    CONF_MPC_DEMAND_HYSTERESIS_PCT,
    CONF_MPC_HOLD_TIME,
    CONF_MPC_HOLD_OVERRIDE_DEMAND_PCT,
    CONF_MPC_MAX_DEMAND_STEP_PCT,
    CONF_FLOW_GATE_CLOSE_SURPLUS,
    CONF_FLOW_GATE_OPEN_SURPLUS,
    CONF_FLOW_GATE_HOLD_TIME,
    CONF_MAX_SENSOR_AGE,
    CONF_STATIONARY_RANGE,
    CONF_TARGET_BAND,
    CONF_TRV_SETPOINT_MODE,
    CONF_BUILDING_ROTATION,
    CONF_ROOF_ORIENTATION,
    CONF_ROOF_TILT,
    CONF_WORKDAY_ENTITY,
)

# Settings of one room, held by its subentry.
ROOM_KEYS = (
    CONF_ROOM_NAME,
    CONF_TRVS,
    CONF_ROOM_SENSOR_ENTITY,
    CONF_WINDOW_CONTACT_ENTITIES,
    CONF_ROOM_COMFORT_CONDITION_ENTITIES,
    CONF_USE_HOUSE_COMFORT_CONDITIONS,
    CONF_LEARNING_BLOCK_ENTITY,
    CONF_BOOST_ENABLED,
    CONF_BOOST_TEMPERATURE_OFFSET,
    CONF_PV_BOOST_ENABLED,
    CONF_PV_BOOST_TEMPERATURE_OFFSET,
    CONF_DESIGN_INDOOR_TEMPERATURE,
    CONF_ROOM_HEAT_LOAD,
    CONF_ROOM_AREA,
    CONF_CONSTRUCTION,
    CONF_LEAD_HEATING_LIMIT,
    CONF_LEAD_FLOW,
    CONF_WINDOW_SURFACES,
    CONF_OPAQUE_SURFACES,
)

SERVICE_UNBLOCK = "unblock"
SERVICE_RECALIBRATE = "recalibrate"
ATTR_UA_FACTOR = "ua_factor"
ATTR_CAPACITY_FACTOR = "capacity_factor"
ATTR_RESET_GAINS = "reset_gains"
ATTR_RESET_VALVE_CURVES = "reset_valve_curves"

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


class Construction(StrEnum):
    """Building construction class after ISO 13790, table 12."""

    VERY_LIGHT = "very_light"
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"
    VERY_HEAVY = "very_heavy"


# Effective internal heat capacity per floor area, ISO 13790 table 12.
CONSTRUCTION_CAPACITY_KJ_PER_M2K = {
    Construction.VERY_LIGHT: 80.0,
    Construction.LIGHT: 110.0,
    Construction.MEDIUM: 165.0,
    Construction.HEAVY: 260.0,
    Construction.VERY_HEAVY: 370.0,
}


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
    OUTSIDE_WINDOW = "outside_window"
    PAUSED = "paused"


class TrvSetpointMode(StrEnum):
    # Target spread over the TRV's range by demand (18 °C .. max).
    ABSOLUTE = "absolute"
    # Target a demand-dependent step above the TRV's own reading.
    RELATIVE = "relative"


DEFAULT_TRV_SETPOINT_MODE = TrvSetpointMode.RELATIVE


class UaBalanceOutcome(StrEnum):
    LEARNED = "learned"
    # The target lay below the lowest plausible factor; learned towards it.
    LEARNED_AT_LIMIT = "learned_at_limit"
    COLLECTING = "collecting"
    OUTSIDE_WINDOW = "outside_window"
    PAUSED = "paused"
    GAP = "gap"
    DRIFT = "drift"
    SMALL_TEMPERATURE_DIFFERENCE = "small_temperature_difference"
    TOO_SHORT = "too_short"
    IMPLAUSIBLE_TARGET = "implausible_target"
    STORAGE_DOMINATED = "storage_dominated"
    ROOM_SENSOR_FALLBACK = "room_sensor_fallback"
    WINDOW_OPEN = "window_open"
    # A configured valve position entity is stale or unavailable.
    VALVE_POSITION_MISSING = "valve_position_missing"
    # A TRV calibrates its valve and moves it by itself.
    TRV_ADAPTING = "trv_adapting"
    # A radiator gives off no heat although its valve reports open.
    RADIATOR_NOT_HEATING = "radiator_not_heating"
    # The outdoor temperature is missing: the TRVs hold the base setpoint on
    # their own, the radiator heat is unknown.
    OUTDOOR_TEMPERATURE_MISSING = "outdoor_temperature_missing"
    # The valves stood fully open with the room below its target band for
    # most of the stretch: the heat assumed for a full opening is the least
    # certain one (a flow-limited radiator gives off less).
    VALVE_SATURATED = "valve_saturated"
    # The room is not yet near its setpoint -- still coasting down from above
    # or recovering from below; the stretch starts once it has settled.
    COASTING = "coasting"


class CapacityOutcome(StrEnum):
    LEARNED = "learned"
    STATIONARY = "stationary"
    SMALL_ERROR = "small_error"
    SMALL_CHANGE = "small_change"
    OUTSIDE_WINDOW = "outside_window"
    DISTURBED = "disturbed"
    SUPPRESSED = "suppressed"
    ROOM_SENSOR_FALLBACK = "room_sensor_fallback"
    VALVE_POSITION_MISSING = "valve_position_missing"
    TRV_ADAPTING = "trv_adapting"
    RADIATOR_NOT_HEATING = "radiator_not_heating"
    OUTDOOR_TEMPERATURE_MISSING = "outdoor_temperature_missing"
    PAUSED = "paused"
    WAITING = "waiting"
    DISABLED = "disabled"


class RoofOrientation(StrEnum):
    """Which way the two roof surfaces of a gable roof face."""

    NONE = "none"
    EAST_WEST = "east_west"
    NORTH_SOUTH = "north_south"


class Surface(StrEnum):
    """An outer surface of the building the sun can shine on."""

    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"
    ROOF_NORTH = "roof_north"
    ROOF_EAST = "roof_east"
    ROOF_SOUTH = "roof_south"
    ROOF_WEST = "roof_west"


FACADES = (Surface.NORTH, Surface.EAST, Surface.SOUTH, Surface.WEST)

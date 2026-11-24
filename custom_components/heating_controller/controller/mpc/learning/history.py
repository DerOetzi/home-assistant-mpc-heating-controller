"""The learner's samples and what all of its evaluations read from them."""

from __future__ import annotations

from dataclasses import dataclass, field

from ....const import CapacityOutcome
from ..results import RoomMpcInput

MIN_HISTORY_SAMPLES = 5

# The learner judges the room against the setpoint of its mode, not against
# the setpoint written to the TRVs: frost protection for an open window is no
# mode change. The TRVs hold the room within a few tenths either way, plus a
# sensor step; a room within this below its mode's setpoint counts as near it.
LEARNER_SETPOINT_TOLERANCE_C = 0.4

# The heat assumed for a fully open valve is the least certain one: a radiator
# that is flow-limited gives off less (study, 28/29 October: target 0.54 with
# the valve open all night, 0.40-0.41 on nights it held its setpoint). A
# sample counts as saturated with every reported valve at least this open and
# the room more than LEARNER_SETPOINT_TOLERANCE_C below its setpoint.
VALVE_SATURATED_OPENING_PCT = 95.0
# Samples further apart than this leave a gap no stretch is balanced across.
UA_BALANCE_MAX_GAP_S = 20 * 60


@dataclass
class LearnerHistoryEntry:
    timestamp: float
    room_temperature_c: float
    outdoor_temperature_c: float
    applied_heating_power_w: float
    # What the observer estimates the radiators gave off at this time: the
    # requested power, lagged by the radiator mass.
    radiator_power_w: float
    target_temperature_c: float | None = None
    flow_temperature_c: float | None = None
    room_sensor_fallback: bool = False
    valve_position_missing: bool = False
    trv_adapting: bool = False
    radiator_not_heating: bool = False
    # A domestic hot-water charge ran: the radiators got no water.
    hot_water_charge: bool = False
    # All reported valves at least VALVE_SATURATED_OPENING_PCT open with the
    # room more than LEARNER_SETPOINT_TOLERANCE_C below its setpoint.
    valve_saturated: bool = False
    window_open: bool = False
    # A window contact reports neither open nor closed.
    window_unknown: bool = False
    # Learning paused, outdoor temperature missing or learner disabled.
    learning_paused: bool = False
    # Learning paused or outdoor temperature missing; unlike learning_paused
    # not set while heating is off, when the room still cools down by itself.
    inputs_paused: bool = False
    # Result of the stationary check over the full hour before this sample,
    # kept so a long history is not fitted again on every cycle.
    stationary_checked: bool = field(default=False, compare=False, repr=False)
    stationary_since_ts: float | None = field(default=None, compare=False, repr=False)


def mean_room_c(previous: LearnerHistoryEntry, current: LearnerHistoryEntry) -> float:
    return (previous.room_temperature_c + current.room_temperature_c) / 2


def mean_outdoor_c(
    previous: LearnerHistoryEntry, current: LearnerHistoryEntry
) -> float:
    return (previous.outdoor_temperature_c + current.outdoor_temperature_c) / 2


def mode_target_c(mpc_input: RoomMpcInput) -> float:
    if mpc_input.mode_target_temp_c is not None:
        return mpc_input.mode_target_temp_c
    return mpc_input.target_temp_c


def trend_drift_c(window: list[LearnerHistoryEntry]) -> float:
    """Room temperature change over the stretch from a fitted straight line.

    Samples are weighted by the time they stand for (half the interval to
    each neighbour): the history gains a sample on every sensor change, so
    a pulse that moves the sensor often would otherwise count more than a
    quiet hour.
    """
    times = [entry.timestamp for entry in window]
    weights = [
        (times[min(index + 1, len(times) - 1)] - times[max(index - 1, 0)]) / 2
        for index in range(len(times))
    ]
    total_weight = sum(weights)
    if total_weight <= 0:
        return 0.0
    temperatures = [entry.room_temperature_c for entry in window]
    mean_time = sum(w * t for w, t in zip(weights, times, strict=True)) / total_weight
    mean_temperature = (
        sum(w * c for w, c in zip(weights, temperatures, strict=True)) / total_weight
    )
    spread = sum(w * (t - mean_time) ** 2 for w, t in zip(weights, times, strict=True))
    if spread <= 0:
        return 0.0
    slope = (
        sum(
            w * (t - mean_time) * (c - mean_temperature)
            for w, t, c in zip(weights, times, temperatures, strict=True)
        )
        / spread
    )
    return slope * (times[-1] - times[0])


def invalid_entry_outcome(entry: LearnerHistoryEntry) -> CapacityOutcome | None:
    if entry.window_open or entry.window_unknown:
        return CapacityOutcome.WINDOW_OPEN
    if entry.inputs_paused:
        return CapacityOutcome.PAUSED
    if entry.room_sensor_fallback:
        return CapacityOutcome.ROOM_SENSOR_FALLBACK
    if entry.valve_position_missing:
        return CapacityOutcome.VALVE_POSITION_MISSING
    if entry.trv_adapting:
        return CapacityOutcome.TRV_ADAPTING
    if entry.radiator_not_heating:
        return CapacityOutcome.RADIATOR_NOT_HEATING
    return None

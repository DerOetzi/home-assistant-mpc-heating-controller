"""The room model learner: UA from the night's heat balance, and the
coordination of the capacity, airing and hourly gain evaluations."""

from __future__ import annotations

import time
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

from ...const import (
    SENSOR_POLL_INTERVAL_MINUTES,
    CapacityOutcome,
    LearningStatus,
    UaBalanceOutcome,
)
from .learning.airing import AiringLearner
from .learning.capacity_jump import JumpLearner
from .learning.context import LearningContext
from .learning.gain_hour import GainHourLearner
from .learning.history import (
    LEARNER_SETPOINT_TOLERANCE_C,
    MIN_HISTORY_SAMPLES,
    UA_BALANCE_MAX_GAP_S,
    VALVE_SATURATED_OPENING_PCT,
    LearnerHistoryEntry,
    mean_outdoor_c,
    mean_room_c,
    mode_target_c,
    trend_drift_c,
)
from .math_helper import clamp
from .models.capacity import (
    MAX_LEARNED_CAPACITY_FACTOR,
    MIN_LEARNED_CAPACITY_FACTOR,
    ThermalCapacityModel,
)
from .models.gains import RoomGainModel, SunSource
from .models.loss import RoomLossModel
from .results import RoomMpcInput
from .types import (
    CapacityStretchInfo,
    LearningFactors,
    RoomModelLearningState,
    UaBalanceInfo,
)

# UA is learned from a heat balance over a long window rather than from single
# prediction windows: in closed loop the room sits at the setpoint the
# prediction aims for, so a wrong UA hardly shows as a prediction error. Over
# the window, the heat the controller requested has to match the losses plus
# what the room stored; the UA that balances this is the learning target.
#
# The window is a stretch of usable time: it runs until learning is paused, a
# window contact opens, the configured room
# sensor drops out, heating stays off for longer than a short interruption, a
# hot-water charge starts once the stretch is long enough, or the sun
# condition ends, and is balanced
# once at that end. A night shortened by an interruption still counts as long as
# the remaining stretch is long enough; a stretch that reaches the maximum is
# balanced right away so a stretch that never ends still learns.
UA_BALANCE_MIN_WINDOW_S = 4 * 3600
UA_BALANCE_MAX_WINDOW_S = 8 * 3600
# Share of the way to the target a stretch of the maximum length moves the
# factor. Shorter stretches move it less, so every hour weighs the same however
# a night is split into stretches.
UA_BALANCE_WEIGHT = 0.3
# Heating briefly unavailable (a missed heat-source poll, the flow dipping
# under the threshold) does not end the stretch: up to three controller cycles
# are bridged as no heating, which is what the radiators got. The extra minute
# absorbs the jitter of the poll that ends the interruption.
UA_BALANCE_MAX_INTERRUPTION_S = 3 * SENSOR_POLL_INTERVAL_MINUTES * 60 + 60
HISTORY_RETENTION_S = UA_BALANCE_MAX_WINDOW_S + 3600
# The stored heat relies on the (uncertain) capacity, so only windows where the
# room ends close to where it started are used. The drift is the trend over
# the whole stretch, not the difference of its edges: heating pulses, a room
# sensor reporting once an hour or a hot-water dip at the start move a short
# edge by tenths of a kelvin without the room storing any heat.
UA_BALANCE_MAX_DRIFT_C = 0.3
# A room still coasting down from above its setpoint with the valve shut would
# end the night far below where it started, one recovering from below (after a
# long valve calibration, say) far above. The stretch starts at the first
# sample no more than this above the setpoint or no more than
# LEARNER_SETPOINT_TOLERANCE_C below it, or
# once the room has held its temperature for an hour, then from the start of
# that hour: a room kept above its setpoint by its neighbours never comes down
# and would otherwise never learn.
UA_BALANCE_SETTLED_MARGIN_C = 0.1
UA_BALANCE_STATIONARY_WINDOW_S = 3600
UA_BALANCE_STATIONARY_SLOPE_C_PER_H = 0.05
# In mild nights little heat is requested, and heat from neighbouring rooms or
# appliances weighs too much in the balance.
UA_BALANCE_MIN_MEAN_DELTA_C = 8.0
# Even within the drift limit the stored heat must stay a minor part of the
# balance; otherwise the result depends on the capacity more than on the
# losses.
UA_BALANCE_MAX_STORAGE_SHARE = 0.5
# Plausibility floor against implausible nights: a balance pointing below this
# factor is taken as a data error (sensor, valve or flow reading), not as a
# room that barely loses heat. The factor still moves, but only down to this
# limit, and the outcome is learned_at_limit.
UA_BALANCE_MIN_TARGET_FACTOR = 0.15
# Above this the stretch is a measurement artefact and is discarded.
UA_BALANCE_MAX_TARGET_FACTOR = 2.0

# A stretch that spent more than this share saturated (see
# VALVE_SATURATED_OPENING_PCT) is not learned from.
UA_BALANCE_MAX_SATURATED_SHARE = 0.5


def ua_balance_weight(window_s: float) -> float:
    """Share of the way to the target a balanced stretch of window_s moves."""
    return 1 - (1 - UA_BALANCE_WEIGHT) ** (window_s / UA_BALANCE_MAX_WINDOW_S)


@dataclass
class _StretchIntegral:
    """Sums over a heat-balance stretch, each weighted by its duration."""

    start_ts: float
    end_ts: float
    heat_j: float = 0.0
    saturated_s: float = 0.0
    outdoor_ks: float = 0.0
    room_ks: float = 0.0
    flow_ks: float = 0.0
    flow_s: float = 0.0

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts

    @property
    def temperature_difference_ks(self) -> float:
        return self.room_ks - self.outdoor_ks

    @property
    def mean_temperature_difference_c(self) -> float:
        return self.temperature_difference_ks / self.duration_s

    def record_means(self, info: UaBalanceInfo) -> None:
        """The stretch's means for diagnostics; a retry overwrites them."""
        info.last_mean_outdoor_c = self.outdoor_ks / self.duration_s
        info.last_mean_room_c = self.room_ks / self.duration_s
        info.last_mean_flow_c = self.flow_ks / self.flow_s if self.flow_s > 0 else None
        info.last_mean_heating_w = self.heat_j / self.duration_s


class LearningReport(Protocol):
    """What the learner tells about its learning, read only."""

    def get_learning_state(self) -> RoomModelLearningState: ...
    def get_ua_balance_info(self) -> UaBalanceInfo: ...
    def get_capacity_outcome(self) -> CapacityOutcome: ...
    def get_capacity_info(self) -> CapacityStretchInfo: ...
    def get_vent_info(self) -> CapacityStretchInfo: ...
    @property
    def vent_capacity_factor(self) -> float: ...
    @property
    def learned_vent_capacity_factor(self) -> float | None: ...


class RoomMpcModelLearner:
    def __init__(
        self,
        loss_model: RoomLossModel,
        capacity_model: ThermalCapacityModel,
        gain_model: RoomGainModel | None = None,
    ) -> None:
        self._ctx = LearningContext(loss_model, capacity_model, gain_model)
        self._jumps = JumpLearner(self._ctx)
        self._airings = AiringLearner(self._ctx)
        self._gain_hours = GainHourLearner(self._ctx)

        self._enabled = False
        self._ua_balance_from_ts = 0.0
        self._ua_balance_closed: tuple[float, float] | None = None
        self._disabled_since_ts: float | None = None
        self._ua_balance_info = UaBalanceInfo()
        self._window_opened_ts: float | None = None
        self._window_unknown = False
        self._learning_paused = False
        self._learning_resumed_ts = 0.0
        self._room_sensor_fallback = False
        self._valve_position_missing = False
        self._trv_adapting = False
        self._radiator_not_heating = False
        self._window_open = False
        self._outdoor_temperature_missing = False
        self._pending_persisted_factors: LearningFactors | None = None

        self._learning_state = self._create_learning_state(LearningStatus.DISABLED)

    def destroy(self) -> None:
        self.disable()

    def enable(self, now_ts: float | None = None) -> None:
        if self._enabled:
            return
        now_ts = time.time() if now_ts is None else now_ts
        self._pending_persisted_factors = None
        # History keeps growing while disabled, with power the TRVs never got:
        # a short interruption is bridged as no heating, a longer one ends the
        # heat-balance stretch.
        disabled_since_ts = self._disabled_since_ts
        self._disabled_since_ts = None
        if (
            disabled_since_ts is not None
            and now_ts - disabled_since_ts <= UA_BALANCE_MAX_INTERRUPTION_S
        ):
            self._ctx.ua_balance_bridged.append((disabled_since_ts, now_ts))
        else:
            if disabled_since_ts is not None:
                self._close_ua_balance(disabled_since_ts)
            if self._ctx.history:
                self._ua_balance_from_ts = max(
                    self._ua_balance_from_ts, self._ctx.history[-1].timestamp
                )
        self._enabled = True
        self._learning_state = self._create_learning_state(
            LearningStatus.PAUSED
            if self._learning_paused
            else LearningStatus.WAITING_INTERVAL
        )

    def disable(self, now_ts: float | None = None) -> None:
        if self._enabled:
            self._enabled = False
            self._pending_persisted_factors = None
            self._disabled_since_ts = time.time() if now_ts is None else now_ts
        self._learning_state = self._create_learning_state(LearningStatus.DISABLED)

    def suppress_for_interval(
        self, ua_duration_s: float, capacity_duration_s: float
    ) -> None:
        now = time.time()
        self._close_ua_balance(now)
        self._ctx.ua_blocked_until_ts = now + ua_duration_s
        self._ctx.capacity_blocked_until_ts = now + capacity_duration_s
        self._pending_persisted_factors = None
        self._learning_state = self._create_learning_state(LearningStatus.SUPPRESSED)

    def set_learning_paused(self, paused: bool, now_ts: float) -> None:
        # Blocks both factors: an external veto for when the inputs themselves
        # are not trustworthy (e.g. an estimated outdoor temperature). Windows
        # that started before the resume carry paused samples and are discarded.
        if self._learning_paused and not paused:
            self._learning_resumed_ts = now_ts
        if paused and not self._learning_paused:
            self._close_ua_balance(now_ts)
        self._learning_paused = paused
        if paused and self._enabled:
            self._learning_state = self._create_learning_state(LearningStatus.PAUSED)

    @property
    def window_open(self) -> bool:
        return self._window_open

    def set_window_open(self, window_open: bool, now_ts: float) -> None:
        # Blocks both factors for as long as the window is open, however long
        # that is: the stretch before it is still balanced, the next one starts
        # no earlier than the close (the caller adds its settling time).
        if window_open and not self._window_open:
            self._close_ua_balance(now_ts)
            self._pending_persisted_factors = None
            self._window_opened_ts = now_ts
        if self._window_open and not window_open:
            self._ctx.ua_blocked_until_ts = max(self._ctx.ua_blocked_until_ts, now_ts)
            if self._window_opened_ts is not None:
                self._airings.pending.append((self._window_opened_ts, now_ts))
            self._window_opened_ts = None
        self._window_open = window_open
        if window_open and self._enabled:
            self._learning_state = self._create_learning_state(
                LearningStatus.SUPPRESSED
            )

    def set_window_unknown(self, unknown: bool) -> None:
        """A window contact reports neither open nor closed: no jump stretch
        starts, as the window may well be open."""
        self._window_unknown = unknown

    def set_outdoor_temperature_missing(self, missing: bool, now_ts: float) -> None:
        # Without an outdoor temperature the controller computes nothing and the
        # TRVs hold a fixed setpoint on their own: no samples come in, and the
        # heat the radiators give off meanwhile is unknown. The stretch up to
        # the last sample is still balanced, the next one starts once the
        # value is back; a jump stretch across the outage ends in a gap.
        if missing and not self._outdoor_temperature_missing and self._ctx.history:
            self._close_ua_balance(self._ctx.history[-1].timestamp)
        if self._outdoor_temperature_missing and not missing:
            self._ctx.ua_blocked_until_ts = max(self._ctx.ua_blocked_until_ts, now_ts)
        self._outdoor_temperature_missing = missing

    def set_sun_condition(self, sun_condition: Callable[[float], bool]) -> None:
        self._ctx.sun_condition = sun_condition

    def append_history(
        self,
        mpc_input: RoomMpcInput,
        applied_heating_power_w: float,
        radiator_power_w: float,
    ) -> None:
        self._track_degraded_inputs(mpc_input)
        if (
            mpc_input.hot_water_charge
            and self._ctx.history
            and not self._ctx.history[-1].hot_water_charge
        ):
            self._close_ua_balance_at_charge(self._ctx.history[-1].timestamp)
        self._ctx.history.append(
            LearnerHistoryEntry(
                timestamp=mpc_input.now_ts,
                room_temperature_c=mpc_input.room_temp_c,
                outdoor_temperature_c=mpc_input.outdoor_temp_c,
                flow_temperature_c=mpc_input.flow_temp_c,
                applied_heating_power_w=applied_heating_power_w,
                radiator_power_w=radiator_power_w,
                target_temperature_c=mode_target_c(mpc_input),
                room_sensor_fallback=mpc_input.room_sensor_fallback,
                valve_position_missing=mpc_input.valve_position_missing,
                trv_adapting=mpc_input.trv_adapting,
                radiator_not_heating=mpc_input.radiator_not_heating,
                hot_water_charge=mpc_input.hot_water_charge,
                valve_saturated=self._valve_saturated(mpc_input),
                window_open=self._window_open,
                window_unknown=self._window_unknown,
                learning_paused=(
                    self._learning_paused
                    or self._outdoor_temperature_missing
                    or not self._enabled
                ),
                inputs_paused=(
                    self._learning_paused or self._outdoor_temperature_missing
                ),
            )
        )
        self._gain_hours.on_sample(mpc_input.now_ts)
        self._cleanup_history(mpc_input.now_ts)

    def set_gain_context(
        self,
        profile_index_at: Callable[[float], int],
        local_day_at: Callable[[float], int],
        sun_source: Callable[[], SunSource | None],
    ) -> None:
        """Where the hourly gain learning gets its day profile, its local day
        number and the sun from."""
        self._ctx.profile_index_at = profile_index_at
        self._ctx.local_day_at = local_day_at
        self._ctx.sun_source = sun_source

    def _track_degraded_inputs(self, mpc_input: RoomMpcInput) -> None:
        # The TRVs sit at the radiator and read up to a kelvin off the room
        # sensor, and without a reported valve opening the radiator heat is a
        # guess, and an adapting TRV moves its valve by itself, so samples
        # taken then are invalid, as with an open window: the stretch up to
        # the last good sample is still balanced, the next one starts once the
        # inputs are back.
        degraded = (
            mpc_input.room_sensor_fallback
            or mpc_input.valve_position_missing
            or mpc_input.trv_adapting
            or mpc_input.radiator_not_heating
        )
        was_degraded = (
            self._room_sensor_fallback
            or self._valve_position_missing
            or self._trv_adapting
            or self._radiator_not_heating
        )
        if degraded and not was_degraded and self._ctx.history:
            end_ts = self._ctx.history[-1].timestamp
            if (
                mpc_input.radiator_not_heating
                and mpc_input.radiator_not_heating_since_ts is not None
            ):
                # The radiator is judged two hours after its check window
                # started; that window's heat is in doubt as well.
                end_ts = min(end_ts, mpc_input.radiator_not_heating_since_ts)
            self._close_ua_balance(end_ts)
        if was_degraded and not degraded:
            self._ctx.ua_blocked_until_ts = max(
                self._ctx.ua_blocked_until_ts, mpc_input.now_ts
            )
        self._room_sensor_fallback = mpc_input.room_sensor_fallback
        self._valve_position_missing = mpc_input.valve_position_missing
        self._trv_adapting = mpc_input.trv_adapting
        self._radiator_not_heating = mpc_input.radiator_not_heating

    def _cleanup_history(self, now_ts: float) -> None:
        self._ctx.history = [
            entry
            for entry in self._ctx.history
            if now_ts - entry.timestamp <= HISTORY_RETENTION_S
        ]
        self._ctx.ua_balance_bridged = [
            (start_ts, end_ts)
            for start_ts, end_ts in self._ctx.ua_balance_bridged
            if now_ts - end_ts <= HISTORY_RETENTION_S
        ]

    def get_learning_state(self) -> RoomModelLearningState:
        return self._learning_state

    def get_ua_balance_info(self) -> UaBalanceInfo:
        return self._ua_balance_info

    def get_capacity_outcome(self) -> CapacityOutcome:
        return self._jumps.info.outcome

    def get_capacity_info(self) -> CapacityStretchInfo:
        return self._jumps.info

    @property
    def vent_capacity_factor(self) -> float:
        """Airing capacity factor; until learned a share of the mass one."""
        return self._airings.factor

    @property
    def learned_vent_capacity_factor(self) -> float | None:
        return self._airings.learned_factor

    def get_vent_info(self) -> CapacityStretchInfo:
        return self._airings.info

    def run_learning_cycle(self) -> None:
        # Jump stretches are judged with heating off as well: the room
        # cooling down in frost protection shows its capacity just the same.
        capacity_learned = self._jumps.evaluate()
        if self._airings.evaluate():
            # Observed only: kept, but no learned status.
            self._pending_persisted_factors = self._get_current_learning_factors()
        if not self._enabled:
            self._run_disabled_heat_balance()
            if capacity_learned:
                self._pending_persisted_factors = self._get_current_learning_factors()
            return

        ua_learned = self._learn_ua_from_heat_balance()
        if ua_learned or capacity_learned:
            status = LearningStatus.LEARNED
            self._pending_persisted_factors = self._get_current_learning_factors()
        elif self._learning_paused:
            status = LearningStatus.PAUSED
        elif self._window_open:
            status = LearningStatus.SUPPRESSED
        else:
            status = LearningStatus.WAITING_INTERVAL
        self._learning_state = self._create_learning_state(status)

    def _run_disabled_heat_balance(self) -> None:
        # Heating may stay off past the end of the night; the stretch before
        # it is still worth balancing.
        if (
            self._disabled_since_ts is not None
            and self._ctx.history
            and self._ctx.history[-1].timestamp - self._disabled_since_ts
            > UA_BALANCE_MAX_INTERRUPTION_S
        ):
            self._close_ua_balance(self._disabled_since_ts)
        if self._ua_balance_closed is None:
            return
        if self._learn_ua_from_heat_balance():
            self._pending_persisted_factors = self._get_current_learning_factors()
            self._learning_state = self._create_learning_state(LearningStatus.DISABLED)

    def _ua_balance_start_ts(self) -> float:
        return max(
            self._ua_balance_from_ts,
            self._learning_resumed_ts,
            self._ctx.ua_blocked_until_ts,
            self._ctx.history[0].timestamp if self._ctx.history else 0.0,
        )

    def _close_ua_balance(self, end_ts: float) -> None:
        """End the running heat-balance stretch; the next cycle balances it."""
        if self._ua_balance_closed is not None:
            # Heating went off before the stretch was closed, and only
            # turned out to stay off for too long afterwards.
            start_ts, closed_end_ts = self._ua_balance_closed
            if start_ts < end_ts < closed_end_ts:
                self._ua_balance_closed = (start_ts, end_ts)
            return
        if not self._ctx.history or self._ua_blocker() is not None:
            return
        start_ts = self._ua_balance_start_ts()
        if end_ts <= start_ts or not self._has_sun_free_sample(start_ts, end_ts):
            # Nothing to balance: the stretch lies after sunrise, say, when
            # the night mode ends later.
            return
        self._ua_balance_closed = (start_ts, end_ts)
        self._ua_balance_from_ts = max(self._ua_balance_from_ts, end_ts)

    def _close_ua_balance_at_charge(self, end_ts: float) -> None:
        """A hot-water charge ends a stretch that is long enough already.

        The morning charge comes with the household's morning and a defrost:
        all rooms cool at once, and the stretch would end in a drift. A
        shorter stretch keeps running and books the charge as no heating.
        """
        settled_ts = self._settled_since_ts(self._ua_balance_start_ts())
        if settled_ts is None or end_ts - settled_ts < UA_BALANCE_MIN_WINDOW_S:
            return
        self._close_ua_balance(end_ts)

    def _learn_ua_from_heat_balance(self) -> bool:
        outcome = self._evaluate_heat_balance()
        self._ua_balance_info.outcome = outcome
        return outcome in (
            UaBalanceOutcome.LEARNED,
            UaBalanceOutcome.LEARNED_AT_LIMIT,
        )

    def _evaluate_heat_balance(self) -> UaBalanceOutcome:
        info = self._ua_balance_info
        info.collecting_since_ts = None
        if self._ua_balance_closed is not None:
            start_ts, end_ts = self._ua_balance_closed
            self._ua_balance_closed = None
            return self._balance_stretch(start_ts, end_ts)
        blocker = self._ua_blocker()
        if blocker is not None:
            return blocker
        if not self._ctx.history:
            return UaBalanceOutcome.COLLECTING
        return self._evaluate_open_stretch()

    def _ua_blocker(self) -> UaBalanceOutcome | None:
        """Why the room cannot be balanced right now; None if it can."""
        for blocked, outcome in (
            (self._learning_paused, UaBalanceOutcome.PAUSED),
            (
                self._outdoor_temperature_missing,
                UaBalanceOutcome.OUTDOOR_TEMPERATURE_MISSING,
            ),
            (self._room_sensor_fallback, UaBalanceOutcome.ROOM_SENSOR_FALLBACK),
            (self._valve_position_missing, UaBalanceOutcome.VALVE_POSITION_MISSING),
            (self._trv_adapting, UaBalanceOutcome.TRV_ADAPTING),
            (self._radiator_not_heating, UaBalanceOutcome.RADIATOR_NOT_HEATING),
            (self._window_open, UaBalanceOutcome.WINDOW_OPEN),
        ):
            if blocked:
                return outcome
        return None

    def _evaluate_open_stretch(self) -> UaBalanceOutcome:
        """Balance the stretch since the last one once it is long enough."""
        info = self._ua_balance_info
        # Checked before the room has to settle: after sunrise a room warming
        # up is no coasting room.
        if not self._has_sun_free_sample(
            self._ua_balance_start_ts(), self._ctx.history[-1].timestamp
        ):
            return UaBalanceOutcome.OUTSIDE_WINDOW
        settled_ts = self._settled_since_ts(self._ua_balance_start_ts())
        if settled_ts is None:
            return UaBalanceOutcome.COASTING
        end_ts = self._ctx.history[-1].timestamp
        sun_free_ts = [
            entry.timestamp
            for entry in self._ctx.history
            if entry.timestamp >= settled_ts
            and self._ctx.sun_condition(entry.timestamp)
        ]
        if not sun_free_ts:
            return UaBalanceOutcome.OUTSIDE_WINDOW
        if not self._ctx.sun_condition(end_ts):
            # The stretch ran into sunrise.
            self._ua_balance_from_ts = end_ts
            return self._balance_stretch(sun_free_ts[0], sun_free_ts[-1])

        if end_ts - sun_free_ts[0] < UA_BALANCE_MAX_WINDOW_S:
            info.collecting_since_ts = sun_free_ts[0]
            return UaBalanceOutcome.COLLECTING
        self._ua_balance_from_ts = end_ts
        return self._balance_stretch(sun_free_ts[0], end_ts)

    def _balance_stretch(
        self, start_ts: float, end_ts: float, retried: bool = False
    ) -> UaBalanceOutcome:
        if self._ctx.loss_model.base_ua_w_per_k <= 0:
            return UaBalanceOutcome.COLLECTING
        settled_ts = self._settled_since_ts(start_ts)
        if settled_ts is None or settled_ts > end_ts:
            return UaBalanceOutcome.COASTING
        window = [
            entry
            for entry in self._ctx.history
            if settled_ts <= entry.timestamp <= end_ts
            and self._ctx.sun_condition(entry.timestamp)
        ]
        rejection = self._stretch_rejection(window)
        if rejection is not None:
            return rejection
        stretch = self._integrate_stretch(window)
        if stretch is None:
            return UaBalanceOutcome.GAP
        stretch.record_means(self._ua_balance_info)
        if stretch.mean_temperature_difference_c < UA_BALANCE_MIN_MEAN_DELTA_C:
            return UaBalanceOutcome.SMALL_TEMPERATURE_DIFFERENCE

        drift_c = trend_drift_c(window)
        if abs(drift_c) > UA_BALANCE_MAX_DRIFT_C:
            retry_ts = None if retried else self._retry_start_ts(window)
            if retry_ts is not None:
                return self._balance_stretch(retry_ts, stretch.end_ts, retried=True)
            # Kept for diagnostics, without a target: none was computed.
            self._ua_balance_info.last_target_factor = None
            self._ua_balance_info.last_drift_c = drift_c
            return UaBalanceOutcome.DRIFT
        return self._learn_from_stretch(stretch, drift_c)

    def _stretch_rejection(
        self, window: list[LearnerHistoryEntry]
    ) -> UaBalanceOutcome | None:
        """Why the stretch cannot be balanced; None if it can."""
        if (
            len(window) < MIN_HISTORY_SAMPLES
            or window[-1].timestamp - window[0].timestamp < UA_BALANCE_MIN_WINDOW_S
        ):
            return UaBalanceOutcome.TOO_SHORT
        for flag, outcome in (
            ("room_sensor_fallback", UaBalanceOutcome.ROOM_SENSOR_FALLBACK),
            ("valve_position_missing", UaBalanceOutcome.VALVE_POSITION_MISSING),
            ("trv_adapting", UaBalanceOutcome.TRV_ADAPTING),
            ("radiator_not_heating", UaBalanceOutcome.RADIATOR_NOT_HEATING),
            ("window_open", UaBalanceOutcome.WINDOW_OPEN),
        ):
            if any(getattr(entry, flag) for entry in window):
                return outcome
        return None

    def _integrate_stretch(
        self, window: list[LearnerHistoryEntry]
    ) -> _StretchIntegral | None:
        """Heat and temperatures summed over the stretch; None on a gap."""
        stretch = _StretchIntegral(
            start_ts=window[0].timestamp, end_ts=window[-1].timestamp
        )
        for previous, current in pairwise(window):
            duration_s = current.timestamp - previous.timestamp
            if duration_s > UA_BALANCE_MAX_GAP_S:
                return None
            if previous.valve_saturated:
                stretch.saturated_s += duration_s
            stretch.heat_j += (
                self._ctx.balance_heating_power_w(previous, current) * duration_s
            )
            stretch.outdoor_ks += mean_outdoor_c(previous, current) * duration_s
            stretch.room_ks += mean_room_c(previous, current) * duration_s
            if previous.flow_temperature_c is not None:
                stretch.flow_ks += previous.flow_temperature_c * duration_s
                stretch.flow_s += duration_s
        return stretch

    def _retry_start_ts(self, window: list[LearnerHistoryEntry]) -> float | None:
        """Where a drifting stretch may count from instead.

        A room found holding still above or below its setpoint was sometimes
        only coasting slower than its sensor steps and then moved on. The
        same night counts from where it came near its setpoint, if enough of
        it is left.
        """
        if self._near_setpoint(window[0]):
            return None
        near_ts = next(
            (entry.timestamp for entry in window if self._near_setpoint(entry)),
            None,
        )
        if near_ts is None or window[-1].timestamp - near_ts < UA_BALANCE_MIN_WINDOW_S:
            return None
        return near_ts

    def _learn_from_stretch(
        self, stretch: _StretchIntegral, drift_c: float
    ) -> UaBalanceOutcome:
        info = self._ua_balance_info
        # Over hours the room's whole mass takes up or gives off heat, not
        # only the air and surfaces a setpoint jump shows: the base capacity
        # from area and construction, without the learned factor.
        stored_heat_j = self._ctx.capacity_model.base_capacity_j_per_k * drift_c
        target_factor = (
            (stretch.heat_j - stored_heat_j)
            / stretch.temperature_difference_ks
            / self._ctx.loss_model.base_ua_w_per_k
        )
        info.last_target_factor = target_factor
        info.last_drift_c = drift_c
        self._ua_balance_from_ts = max(self._ua_balance_from_ts, stretch.end_ts)

        rejection = self._target_rejection(stretch, stored_heat_j, target_factor)
        if rejection is not None:
            # Kept for diagnostics; the factor and the last learned time stay.
            return rejection
        if self._ctx.gain_model is not None:
            # A point for the night regression of heat-loss factor and steady
            # gain; a target below the floor is a valid balance as well -- the
            # room living off its neighbours is exactly such a gain.
            self._ctx.gain_model.add_night_point(
                stretch.mean_temperature_difference_c,
                (stretch.heat_j - stored_heat_j) / stretch.duration_s,
                stretch.duration_s / 3600,
                self._ctx.loss_model.effective_ua_w_per_k,
            )
        learned_target_factor = max(target_factor, UA_BALANCE_MIN_TARGET_FACTOR)
        current_factor = self._ctx.loss_model.learned_ua_factor
        self._ctx.loss_model.learned_ua_factor = current_factor + ua_balance_weight(
            stretch.duration_s
        ) * (learned_target_factor - current_factor)
        info.last_learned_ts = stretch.end_ts
        info.last_window_s = stretch.duration_s
        if target_factor < UA_BALANCE_MIN_TARGET_FACTOR:
            return UaBalanceOutcome.LEARNED_AT_LIMIT
        return UaBalanceOutcome.LEARNED

    @staticmethod
    def _target_rejection(
        stretch: _StretchIntegral, stored_heat_j: float, target_factor: float
    ) -> UaBalanceOutcome | None:
        if stretch.saturated_s > UA_BALANCE_MAX_SATURATED_SHARE * stretch.duration_s:
            return UaBalanceOutcome.VALVE_SATURATED
        if abs(stored_heat_j) > UA_BALANCE_MAX_STORAGE_SHARE * stretch.heat_j:
            return UaBalanceOutcome.STORAGE_DOMINATED
        if target_factor > UA_BALANCE_MAX_TARGET_FACTOR:
            return UaBalanceOutcome.IMPLAUSIBLE_TARGET
        return None

    def _has_sun_free_sample(self, start_ts: float, end_ts: float) -> bool:
        return any(
            start_ts <= entry.timestamp <= end_ts
            and self._ctx.sun_condition(entry.timestamp)
            for entry in self._ctx.history
        )

    def _settled_since_ts(self, start_ts: float) -> float | None:
        """First sample from start_ts on where the room has settled.

        Settled is near the setpoint -- within the tolerance below and the
        margin above it -- or holding its temperature wherever it is. A
        room coasting down from above or recovering from below would book its
        stored heat as a drift. A room found holding still counts from the
        start of the hour it held still: that hour stored next to nothing.
        """
        for index, entry in enumerate(self._ctx.history):
            if entry.timestamp < start_ts:
                continue
            if self._near_setpoint(entry):
                return entry.timestamp
            stationary_since_ts = self._stationary_since_ts(index, start_ts)
            if stationary_since_ts is not None:
                return stationary_since_ts
        return None

    @staticmethod
    def _near_setpoint(entry: LearnerHistoryEntry) -> bool:
        """Within the tolerance below and the margin above the setpoint."""
        return entry.target_temperature_c is None or (
            entry.target_temperature_c - LEARNER_SETPOINT_TOLERANCE_C
            <= entry.room_temperature_c
            <= entry.target_temperature_c + UA_BALANCE_SETTLED_MARGIN_C
        )

    @staticmethod
    def _valve_saturated(mpc_input: RoomMpcInput) -> bool:
        openings_pct = [
            opening for opening in mpc_input.valve_positions_pct if opening is not None
        ]
        return (
            bool(openings_pct)
            and min(openings_pct) >= VALVE_SATURATED_OPENING_PCT
            and mpc_input.room_temp_c
            < mode_target_c(mpc_input) - LEARNER_SETPOINT_TOLERANCE_C
        )

    def _stationary_since_ts(self, index: int, start_ts: float) -> float | None:
        """Start of the hour up to a sample over which the room held still.

        Only samples from start_ts on count, so a stretch never reaches back
        before its own start. None while the room still moves.
        """
        entry = self._ctx.history[index]
        end_ts = entry.timestamp
        window_start_ts = end_ts - UA_BALANCE_STATIONARY_WINDOW_S
        # A full hour inside the history and the stretch reads the same on
        # every cycle; a cut one depends on where the stretch starts.
        full_window = window_start_ts >= max(start_ts, self._ctx.history[0].timestamp)
        if full_window and entry.stationary_checked:
            return entry.stationary_since_ts
        first = bisect_left(
            self._ctx.history,
            max(start_ts, window_start_ts),
            hi=index + 1,
            key=lambda sample: sample.timestamp,
        )
        stationary_since_ts = self._held_still_since_ts(
            self._ctx.history[first : index + 1]
        )
        if full_window:
            entry.stationary_checked = True
            entry.stationary_since_ts = stationary_since_ts
        return stationary_since_ts

    def _held_still_since_ts(self, window: list[LearnerHistoryEntry]) -> float | None:
        if len(window) < MIN_HISTORY_SAMPLES:
            return None
        duration_s = window[-1].timestamp - window[0].timestamp
        if duration_s < UA_BALANCE_STATIONARY_WINDOW_S * 0.9:
            return None
        slope_c_per_h = trend_drift_c(window) / duration_s * 3600
        if abs(slope_c_per_h) >= UA_BALANCE_STATIONARY_SLOPE_C_PER_H:
            return None
        return window[0].timestamp

    def recalibrate(self, factors: LearningFactors) -> None:
        self._pending_persisted_factors = None
        self._ua_balance_closed = None
        if self._ctx.history:
            self._ua_balance_from_ts = self._ctx.history[-1].timestamp
            self._jumps.done_ts = self._ctx.history[-1].timestamp
        self._airings.pending = []
        self._ctx.loss_model.learned_ua_factor = factors.ua_factor
        self._ctx.capacity_model.learned_capacity_factor = factors.capacity_factor
        self._airings.learned_factor = (
            None
            if factors.vent_capacity_factor is None
            else clamp(
                factors.vent_capacity_factor,
                MIN_LEARNED_CAPACITY_FACTOR,
                MAX_LEARNED_CAPACITY_FACTOR,
            )
        )
        self._learning_state = self._create_learning_state(self._learning_state.status)

    def take_factors_to_save(self) -> LearningFactors | None:
        factors = self._pending_persisted_factors
        self._pending_persisted_factors = None
        return factors

    def _create_learning_state(self, status: LearningStatus) -> RoomModelLearningState:
        return RoomModelLearningState(
            status=status, learned_factors=self._get_current_learning_factors()
        )

    def _get_current_learning_factors(self) -> LearningFactors:
        return LearningFactors(
            ua_factor=self._ctx.loss_model.learned_ua_factor,
            capacity_factor=self._ctx.capacity_model.learned_capacity_factor,
            vent_capacity_factor=self._airings.learned_factor,
        )

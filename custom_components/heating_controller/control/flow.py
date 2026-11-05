"""Flow temperature control: the heat source's curve and its actuator.

The heat source either follows its own weather-compensated curve with an
offset (Daikin "leaving water offset") or takes a fixed flow setpoint. In
both cases the target is the flow the flow lead rooms need, plus a margin,
raised around a planned hot-water charge, and never below the lowest flow
the heat source usefully delivers. The controller decides what to write
and when; the caller writes and reports back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .write_budget import WritePriority


class ActuatorMode(StrEnum):
    OFFSET = "offset"
    SETPOINT = "setpoint"


class FlowSource(StrEnum):
    LEAD = "lead"
    CHARGE = "charge"
    THRESHOLD = "threshold"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class HeatingCurve:
    """Two points and an optional bump around 0 °C (Daikin [4.A])."""

    outdoor_cold_c: float
    outdoor_warm_c: float
    flow_cold_c: float
    flow_warm_c: float
    bump_k: float = 0.0
    bump_width_k: float = 0.0

    def flow_at(self, outdoor_c: float) -> float | None:
        """The curve's flow for an outdoor temperature, None if undefined."""
        if self.outdoor_cold_c == self.outdoor_warm_c:
            return None
        clamped = max(self.outdoor_cold_c, min(self.outdoor_warm_c, outdoor_c))
        slope = (self.flow_warm_c - self.flow_cold_c) / (
            self.outdoor_warm_c - self.outdoor_cold_c
        )
        flow = self.flow_cold_c + slope * (clamped - self.outdoor_cold_c)
        if self.bump_width_k > 0:
            # Full bump at 0 °C, linearly down to nothing at ±width/2.
            flow += self.bump_k * max(0.0, 1 - abs(outdoor_c) / (self.bump_width_k / 2))
        return round(flow, 1)


@dataclass(frozen=True)
class FlowControlConfig:
    mode: ActuatorMode
    minimum: float
    maximum: float
    step: float
    fallback: float
    margin_k: float
    hysteresis_k: float
    hold_s: float
    # Lowest target in setpoint mode. In offset mode the threshold moves with
    # the offset (curve's warm end + offset), so it cannot bound the target.
    threshold_c: float | None


@dataclass(frozen=True)
class FlowInputs:
    outdoor_c: float | None
    lead_min_flow_c: float | None
    # Flow the rooms need around a planned hot-water charge; None outside.
    charge_flow_c: float | None
    # The actuator's current value as the heat source reports it.
    actual: float | None
    heating_on: bool | None
    reachable: bool


@dataclass(frozen=True)
class FlowDecision:
    target_flow_c: float | None
    value: float | None
    source: FlowSource | None
    # What to write now; None writes nothing.
    write: float | None = None
    priority: WritePriority = WritePriority.NORMAL
    reason: str = ""


class FlowController:
    def __init__(self, config: FlowControlConfig, now_ts: float) -> None:
        self.config = config
        # A start counts as a write: no change before the hold time, so a
        # restart does not hand the heat source a burst of new values.
        self.last_write_ts = now_ts
        self.last_written: float | None = None
        self.external_change_ts: float | None = None
        self._fallback_active = False
        self._charge_active = False
        self._before_write: float | None = None

    def _round(self, value: float) -> float:
        cfg = self.config
        stepped = round(value / cfg.step) * cfg.step
        return max(cfg.minimum, min(cfg.maximum, round(stepped, 3)))

    def observe_actual(self, actual: float | None, now_ts: float) -> None:
        """Take the actuator's value; a change not ours restarts the hold.

        A cloud reports a write only with its next poll; until then the old
        value is not a change by someone else.
        """
        if actual is None or self.last_written is None:
            return
        if math.isclose(actual, self.last_written):
            self._before_write = None
            return
        if self._before_write is not None and math.isclose(actual, self._before_write):
            return
        self.external_change_ts = now_ts
        self.last_write_ts = now_ts
        self.last_written = actual
        self._before_write = None

    def written(self, value: float, actual: float | None, now_ts: float) -> None:
        self._before_write = actual
        self.last_written = value
        self.last_write_ts = now_ts

    def _held(self, now_ts: float) -> bool:
        return now_ts - self.last_write_ts < self.config.hold_s

    def decide(
        self, inputs: FlowInputs, curve: HeatingCurve, now_ts: float
    ) -> FlowDecision:
        cfg = self.config
        curve_flow = (
            curve.flow_at(inputs.outdoor_c) if inputs.outdoor_c is not None else None
        )

        if inputs.heating_on is False:
            return FlowDecision(None, None, None, reason="heating_off")
        if not inputs.reachable:
            return FlowDecision(None, None, None, reason="unreachable")

        needs_curve = cfg.mode is ActuatorMode.OFFSET
        if inputs.lead_min_flow_c is None or (needs_curve and curve_flow is None):
            self._charge_active = False
            target = (
                None
                if curve_flow is None
                else curve_flow + (cfg.fallback if needs_curve else 0.0)
            )
            value = self._round(cfg.fallback)
            # Once written, sent again only after the hold time: the cloud
            # shows the new value with its next poll.
            already = self.last_written is not None and math.isclose(
                self.last_written, value
            )
            write = (
                None
                if _same(inputs.actual, value)
                or (self._fallback_active and already and self._held(now_ts))
                else value
            )
            self._fallback_active = True
            return FlowDecision(
                target,
                value,
                FlowSource.FALLBACK,
                write=write,
                priority=WritePriority.REQUIRED,
                reason="fallback",
            )

        lead = inputs.lead_min_flow_c + cfg.margin_k
        target, source = lead, FlowSource.LEAD
        if inputs.charge_flow_c is not None and inputs.charge_flow_c > target:
            target, source = inputs.charge_flow_c, FlowSource.CHARGE
        if cfg.threshold_c is not None and target < cfg.threshold_c:
            target, source = cfg.threshold_c, FlowSource.THRESHOLD

        raw = target - curve_flow if needs_curve else target
        value = self._round(raw)

        charge_now = source is FlowSource.CHARGE
        # Entering or leaving a charge window, or leaving the fallback, is
        # not held back: the raise has to be there before the pause.
        urgent = charge_now != self._charge_active or self._fallback_active
        self._charge_active = charge_now
        self._fallback_active = False

        decision = FlowDecision(round(target, 1), value, source)
        if _same(inputs.actual, value):
            return decision
        if inputs.actual is not None and not urgent:
            if abs(value - inputs.actual) < cfg.hysteresis_k:
                return FlowDecision(
                    decision.target_flow_c, value, source, reason="hysteresis"
                )
            if self._held(now_ts):
                return FlowDecision(
                    decision.target_flow_c, value, source, reason="hold"
                )
        return FlowDecision(
            decision.target_flow_c,
            value,
            source,
            write=value,
            priority=WritePriority.REQUIRED if urgent else WritePriority.NORMAL,
            reason="charge" if urgent and charge_now else "",
        )


def _same(actual: float | None, value: float) -> bool:
    return actual is not None and math.isclose(actual, value)

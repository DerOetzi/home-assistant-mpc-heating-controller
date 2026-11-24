"""The flow control device: reads the hub, decides, writes the actuator."""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_ACTUATOR_ENTITY,
    CONF_ACTUATOR_FALLBACK,
    CONF_ACTUATOR_MAX,
    CONF_ACTUATOR_MIN,
    CONF_ACTUATOR_MODE,
    CONF_ACTUATOR_STEP,
)
from .control.flow import (
    ActuatorMode,
    FlowControlConfig,
    FlowController,
    FlowDecision,
    FlowInputs,
    HeatingCurve,
)
from .control.flow_correction import SETTLE_S, CorrectionFreeze
from .control.flow_feedback import FeedbackFreeze, TargetLimit
from .listeners import Observable
from .night import NightWindow
from .state_reader import attribute_float, available_state, state_float

if TYPE_CHECKING:
    from .hub import HeatingHub

_LOGGER = logging.getLogger(__name__)

# Starting values of the flow device's numbers. The curve points are those
# of a typical air-to-water heat pump; set them to the heat source's own.
FLOW_NUMBER_DEFAULTS: dict[str, float] = {
    "curve_outdoor_cold": -13.0,
    "curve_outdoor_warm": 18.0,
    "curve_flow_cold": 54.0,
    "curve_flow_warm": 29.0,
    "curve_bump": 0.0,
    "curve_bump_width": 0.0,
    "hysteresis": 0.5,
    "hold_time_min": 30.0,
    "charge_raise_max": 4.0,
    "threshold": 29.0,
}


class FlowDevice(Observable):
    def __init__(
        self, hass: HomeAssistant, hub: HeatingHub, subentry: ConfigSubentry
    ) -> None:
        super().__init__()
        self.hass = hass
        self.hub = hub
        self.subentry_id = subentry.subentry_id
        self.title = subentry.title
        data = subentry.data
        self.mode = ActuatorMode(data[CONF_ACTUATOR_MODE])
        self.actuator_entity: str = data[CONF_ACTUATOR_ENTITY]
        self._minimum = float(data[CONF_ACTUATOR_MIN])
        self._maximum = float(data[CONF_ACTUATOR_MAX])
        self._step = float(data[CONF_ACTUATOR_STEP])
        self._fallback = float(data[CONF_ACTUATOR_FALLBACK])
        self.numbers: dict[str, float] = dict(FLOW_NUMBER_DEFAULTS)
        self.write_enabled = False
        self.controller = FlowController(self._config(), time.time())
        self.decision: FlowDecision | None = None
        self.last_error: str | None = None
        # The last decision wanted a new value that did not go out.
        self._unwritten = False
        self._context = Context()
        self._night = NightWindow(hass)

    # --- Settings ----------------------------------------------------------

    def _config(self) -> FlowControlConfig:
        n = self.numbers
        return FlowControlConfig(
            mode=self.mode,
            minimum=self._minimum,
            maximum=self._maximum,
            step=self._step,
            fallback=self._fallback,
            hysteresis_k=n["hysteresis"],
            hold_s=n["hold_time_min"] * 60,
            threshold_c=self.threshold_c,
        )

    def set_number(self, key: str, value: float) -> None:
        if key not in FLOW_NUMBER_DEFAULTS:
            raise ValueError(f"Unknown flow number {key}")
        self.numbers[key] = float(value)
        self.controller.config = self._config()
        self._notify_listeners()

    def set_write_enabled(self, enabled: bool) -> None:
        self.write_enabled = enabled
        self._notify_listeners()

    def reset_feedback(self) -> None:
        self.hub.reset_flow_feedback()
        self._notify_listeners()

    @property
    def curve(self) -> HeatingCurve:
        n = self.numbers
        return HeatingCurve(
            outdoor_cold_c=n["curve_outdoor_cold"],
            outdoor_warm_c=n["curve_outdoor_warm"],
            flow_cold_c=n["curve_flow_cold"],
            flow_warm_c=n["curve_flow_warm"],
            bump_k=n["curve_bump"],
            bump_width_k=n["curve_bump_width"],
        )

    @property
    def threshold_c(self) -> float:
        """The lowest flow the heat source usefully delivers.

        Offset mode: the curve's warm end without offset. The offset is the
        control's own output; a threshold moving with it would follow the
        rooms' demand and could no longer say whether they need heat.
        Setpoint mode: the threshold number.
        """
        if self.mode is ActuatorMode.SETPOINT:
            return self.numbers["threshold"]
        return self.numbers["curve_flow_warm"]

    # --- Readings ----------------------------------------------------------

    @property
    def actual(self) -> float | None:
        """The actuator's value: a climate's target, else the entity state."""
        state = available_state(self.hass, self.actuator_entity)
        if state is None:
            return None
        if self.actuator_entity.startswith("climate."):
            return attribute_float(state, "temperature")
        return state_float(state)

    @property
    def curve_flow_c(self) -> float | None:
        """The heat source's curve without offset at the current outdoor value."""
        outdoor = self.hub.outdoor_temperature_c
        return None if outdoor is None else self.curve.flow_at(outdoor)

    @property
    def effective_curve_c(self) -> float | None:
        """The flow the heat source aims at: curve plus offset, or setpoint."""
        actual = self.actual
        if self.mode is ActuatorMode.SETPOINT:
            return actual
        curve = self.curve_flow_c
        if curve is None or actual is None:
            return None
        return round(curve + actual, 1)

    # --- Control -----------------------------------------------------------

    async def async_evaluate(self) -> None:
        if not self.hub.started:
            return
        now_ts = time.time()
        actual = self.actual
        self.controller.observe_actual(actual, now_ts)
        hub = self.hub
        feedback = hub.flow_feedback
        correction = hub.flow_correction
        inputs = FlowInputs(
            outdoor_c=hub.outdoor_temperature_c,
            feedforward_flow_c=hub.lead_feedforward_flow.value_c,
            charge_flow_c=hub.charge_flow_c,
            actual=actual,
            heating_on=hub.heating_on,
            reachable=hub.heat_source_reachable,
            feedback_k=feedback.feedback_k,
            boost_flow_c=hub.lead_boost_flow.value_c,
            design_flow_c=hub.design_flow_c,
            correction_k=correction.correction_k
            if self.mode is ActuatorMode.OFFSET
            else 0.0,
        )
        bounds = self.controller.bounds(inputs, self.curve)
        limit: TargetLimit | None = None
        if bounds is not None and inputs.feedforward_flow_c is not None:
            lifted = inputs.feedforward_flow_c + inputs.feedback_k
            if lifted <= bounds.lower_c:
                limit = TargetLimit.LOWER
            elif lifted >= bounds.upper_c:
                limit = TargetLimit.UPPER
        freeze = self._freeze_reason(inputs, bounds is None)
        feedback.update(
            hub.feedback_samples(),
            self._step,
            freeze,
            now_ts,
            limit=limit,
            night=self._night.is_night(now_ts),
        )
        if self.mode is ActuatorMode.OFFSET:
            correction.update(
                hub.measured_flow_c,
                self.effective_curve_c,
                self._correction_freeze(freeze, now_ts),
                now_ts,
            )
            inputs = dataclasses.replace(inputs, correction_k=correction.correction_k)
        decision = self.controller.decide(
            dataclasses.replace(inputs, feedback_k=feedback.feedback_k),
            self.curve,
            now_ts,
        )
        self.decision = decision
        written = False
        if (
            decision.write is not None
            and self.write_enabled
            and hub.request_write(decision.priority)
        ):
            written = await self._async_write(decision.write, actual, now_ts)
        self._unwritten = decision.reason == "hold" or (
            decision.write is not None and not written
        )
        self._notify_listeners()

    def _freeze_reason(
        self, inputs: FlowInputs, no_feedforward: bool
    ) -> FeedbackFreeze | None:
        """Why the feedback must not move now; None if it may.

        The valves only say something about the flow when the decided value
        reaches the heat source and the heat source heats the radiators.
        """
        if not self.write_enabled:
            return FeedbackFreeze.WRITE_OFF
        if inputs.heating_on is False:
            return FeedbackFreeze.HEATING_OFF
        if not inputs.reachable:
            return FeedbackFreeze.UNREACHABLE
        if self.hub.hot_water_charging:
            return FeedbackFreeze.HOT_WATER
        measured = self.hub.measured_flow_c
        if measured is not None and measured < self.threshold_c:
            return FeedbackFreeze.DEFROST
        if no_feedforward:
            return FeedbackFreeze.FALLBACK
        if inputs.boost_flow_c is not None:
            return FeedbackFreeze.BOOST
        if self._unwritten:
            return FeedbackFreeze.NOT_WRITTEN
        return None

    def _correction_freeze(
        self, freeze: FeedbackFreeze | None, now_ts: float
    ) -> CorrectionFreeze | None:
        """Why the measured correction must not learn now; None if it may.

        Only in calm operation: whenever the feedback may move, the rooms do
        not catch up after a pause, and the heat source had time to reach
        the last value written.
        """
        if freeze is not None:
            return CorrectionFreeze.FEEDBACK_FROZEN
        if self.hub.flow_feedback.catching_up(now_ts):
            return CorrectionFreeze.CATCH_UP
        if now_ts - self.controller.last_write_ts < SETTLE_S:
            return CorrectionFreeze.SETTLING
        return None

    async def _async_write(
        self, value: float, actual: float | None, now_ts: float
    ) -> bool:
        domain = self.actuator_entity.split(".", 1)[0]
        if domain == "climate":
            service, data = "set_temperature", {"temperature": value}
        else:
            service, data = "set_value", {"value": value}
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {ATTR_ENTITY_ID: self.actuator_entity, **data},
                blocking=True,
                context=self._context,
            )
        except HomeAssistantError as err:
            self.last_error = str(err)
            _LOGGER.warning(
                "Writing %s to %s failed: %s", value, self.actuator_entity, err
            )
            return False
        self.last_error = None
        self.controller.written(value, actual, now_ts)
        _LOGGER.info("Flow actuator %s set to %s", self.actuator_entity, value)
        return True

    # --- Listeners ---------------------------------------------------------

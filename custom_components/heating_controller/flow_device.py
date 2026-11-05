"""The flow control device: reads the hub, decides, writes the actuator."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant, callback
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
    "margin": 1.0,
    "hysteresis": 0.5,
    "hold_time_min": 30.0,
    "charge_raise_max": 4.0,
    "threshold": 29.0,
}


class FlowDevice:
    def __init__(
        self, hass: HomeAssistant, hub: HeatingHub, subentry: ConfigSubentry
    ) -> None:
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
        self._last_actual: float | None = None
        self._listeners: list[Callable[[], None]] = []
        self._context = Context()

    # --- Settings ----------------------------------------------------------

    def _config(self) -> FlowControlConfig:
        n = self.numbers
        return FlowControlConfig(
            mode=self.mode,
            minimum=self._minimum,
            maximum=self._maximum,
            step=self._step,
            fallback=self._fallback,
            margin_k=n["margin"],
            hysteresis_k=n["hysteresis"],
            hold_s=n["hold_time_min"] * 60,
            threshold_c=None if self.mode is ActuatorMode.OFFSET else n["threshold"],
        )

    def set_number(self, key: str, value: float) -> None:
        if key not in FLOW_NUMBER_DEFAULTS:
            raise ValueError(f"Unknown flow number {key}")
        self.numbers[key] = float(value)
        self.controller.config = self._config()
        self._notify()

    def set_write_enabled(self, enabled: bool) -> None:
        self.write_enabled = enabled
        self._notify()

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

        Offset mode: the curve's warm end plus the current offset, as the heat
        source sees it (the last reported offset during a cloud gap, the
        fallback before any). Setpoint mode: the threshold number.
        """
        if self.mode is ActuatorMode.SETPOINT:
            return self.numbers["threshold"]
        actual = self.actual
        if actual is not None:
            self._last_actual = actual
        offset = self._last_actual if self._last_actual is not None else self._fallback
        return round(self.numbers["curve_flow_warm"] + offset, 1)

    @property
    def charge_raise_max_k(self) -> float:
        return self.numbers["charge_raise_max"]

    # --- Readings ----------------------------------------------------------

    @property
    def actual(self) -> float | None:
        """The actuator's value: a climate's target, else the entity state."""
        state = self.hass.states.get(self.actuator_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        raw = (
            state.attributes.get("temperature")
            if self.actuator_entity.startswith("climate.")
            else state.state
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

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
        now_ts = time.time()
        actual = self.actual
        self.controller.observe_actual(actual, now_ts)
        lead = self.hub.lead_min_flow
        decision = self.controller.decide(
            FlowInputs(
                outdoor_c=self.hub.outdoor_temperature_c,
                lead_min_flow_c=lead.value_c,
                charge_flow_c=self.hub.charge_flow_c,
                actual=actual,
                heating_on=self.hub.heating_on,
                reachable=self.hub.heat_source_reachable,
            ),
            self.curve,
            now_ts,
        )
        self.decision = decision
        if (
            decision.write is not None
            and self.write_enabled
            and self.hub.request_write(decision.priority)
        ):
            await self._async_write(decision.write, actual, now_ts)
        self._notify()

    async def _async_write(
        self, value: float, actual: float | None, now_ts: float
    ) -> None:
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
            return
        self.last_error = None
        self.controller.written(value, actual, now_ts)
        _LOGGER.info("Flow actuator %s set to %s", self.actuator_entity, value)

    # --- Listeners ---------------------------------------------------------

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

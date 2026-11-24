"""House-wide figures taken from the lead rooms."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .const import CONF_LEAD_FLOW, CONF_LEAD_HEATING_LIMIT, CurveEnd
from .control.flow_feedback import DemandSample
from .coordinator import HeatingRoomCoordinator

# Like the min/max helpers the lead figures replace.
LEAD_ROUND_DIGITS = 1


@dataclass(frozen=True)
class LeadValue:
    """The highest value among the lead rooms and the room it comes from."""

    value_c: float | None
    room: str | None
    rooms: list[str]


class LeadRooms:
    """The heating-limit lead rooms decide the season, the flow lead rooms
    the flow."""

    def __init__(self, rooms: dict[str, HeatingRoomCoordinator]) -> None:
        # The hub's own mapping: rooms added later count as well.
        self._rooms = rooms

    def _lead(
        self, flag: str, value: Callable[[HeatingRoomCoordinator], float | None]
    ) -> LeadValue:
        leads = [room for room in self._rooms.values() if room.data.get(flag)]
        best: tuple[float, str] | None = None
        for room in leads:
            room_value = value(room)
            if room_value is not None and (best is None or room_value > best[0]):
                best = (room_value, room.room_name)
        names = [room.room_name for room in leads]
        if best is None:
            return LeadValue(None, None, names)
        return LeadValue(round(best[0], LEAD_ROUND_DIGITS), best[1], names)

    @property
    def hold_flow(self) -> LeadValue:
        """Flow the heating-limit lead rooms need to hold their setpoint."""
        return self._lead(
            CONF_LEAD_HEATING_LIMIT,
            lambda room: room.normal_gated_hold_flow_temperature_c,
        )

    @property
    def feedforward_flow(self) -> LeadValue:
        """Hold flow of the flow lead rooms, ungated: the feedforward."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.normal_hold_flow_temperature_c
        )

    @property
    def boost_flow(self) -> LeadValue:
        """Recovery flow of the flow lead rooms that recover after a mode change."""
        return self._lead(CONF_LEAD_FLOW, lambda room: room.flow_boost_temperature_c)

    @property
    def design_flow_c(self) -> float | None:
        """Highest design flow of the flow lead rooms' radiators."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.design_flow_temperature_c
        ).value_c

    def curve_hold_flow(self, end: CurveEnd) -> LeadValue:
        """Flow the flow lead rooms need at one end of the curve, at comfort."""
        return self._lead(
            CONF_LEAD_FLOW, lambda room: room.hold_flow_at_curve_end_c(end)
        )

    def feedback_samples(self) -> list[DemandSample]:
        """Valve demand of the flow lead rooms the feedback may use."""
        return [
            sample
            for room in self._rooms.values()
            if room.data.get(CONF_LEAD_FLOW)
            and (sample := room.feedback_sample) is not None
        ]

    def hold_flow_curves(self, outdoor_values: list[float]) -> list[dict[str, Any]]:
        """Each room's comfort hold flow over the outdoor values, for the card.

        Not stepped up to the control's grid, so the curves come out smooth.
        """
        return [
            {
                "name": room.room_name,
                "lead": bool(room.data.get(CONF_LEAD_FLOW)),
                "flow_c": [
                    None
                    if (flow_c := room.comfort_hold_flow_c(value, round_up=False))
                    is None
                    else round(flow_c, 1)
                    for value in outdoor_values
                ],
            }
            for room in self._rooms.values()
        ]

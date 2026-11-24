"""The raised flow a room gets after its setpoint went up."""

from __future__ import annotations

from ..controller.mpc.controller import FLOW_RECOVERY_HORIZON_S
from ..controller.mpc.learning.capacity_jump import JUMP_MIN_TARGET_STEP_C

# A room recovers with a raised flow (boost) only after its mode's setpoint
# went up by a jump, until it is this close to the new setpoint or the
# recovery horizon has passed.
FLOW_BOOST_DONE_BELOW_C = 0.2


class FlowBoost:
    """Starts on a raised setpoint, ends once the room got there."""

    def __init__(self) -> None:
        # The active mode's own setpoint; an open window does not change it.
        self.settled_target_c: float | None = None
        self._since_ts: float | None = None

    @property
    def active(self) -> bool:
        return self._since_ts is not None

    def update(self, target_c: float, room_c: float | None, now_ts: float) -> None:
        previous_c = self.settled_target_c
        self.settled_target_c = target_c
        if previous_c is not None and target_c != previous_c:
            if target_c - previous_c >= JUMP_MIN_TARGET_STEP_C:
                self._since_ts = now_ts
            elif target_c < previous_c:
                self._since_ts = None
        if self._since_ts is None:
            return
        if now_ts - self._since_ts >= FLOW_RECOVERY_HORIZON_S or (
            room_c is not None and room_c >= target_c - FLOW_BOOST_DONE_BELOW_C
        ):
            self._since_ts = None

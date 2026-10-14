from __future__ import annotations

from ..math_helper import clamp
from ..types import RoomThermalConfig

# Room heat loads come from a design calculation (worst case: neighbours unheated,
# full air change). In operation the room may lose a fraction of that.
MIN_LEARNED_UA_FACTOR = 0.1
MAX_LEARNED_UA_FACTOR = 2.0


class RoomLossModel:
    def __init__(self, config: RoomThermalConfig) -> None:
        self._learned_ua_factor = 1.0

        design_temperature_delta_c = (
            config.design_indoor_temperature_c - config.design_outdoor_temperature_c
        )
        self._base_ua_w_per_k = config.room_heat_load_w / max(
            0.1, design_temperature_delta_c
        )

    def calculate_heat_loss_w(
        self, temperature_c: float, outdoor_temperature_c: float
    ) -> float:
        return self.effective_ua_w_per_k * (temperature_c - outdoor_temperature_c)

    @property
    def learned_ua_factor(self) -> float:
        return self._learned_ua_factor

    @learned_ua_factor.setter
    def learned_ua_factor(self, value: float) -> None:
        self._learned_ua_factor = clamp(
            value, MIN_LEARNED_UA_FACTOR, MAX_LEARNED_UA_FACTOR
        )

    @property
    def base_ua_w_per_k(self) -> float:
        return self._base_ua_w_per_k

    @property
    def effective_ua_w_per_k(self) -> float:
        return self._base_ua_w_per_k * self._learned_ua_factor

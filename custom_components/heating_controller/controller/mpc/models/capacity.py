from __future__ import annotations

from ....const import CONSTRUCTION_CAPACITY_KJ_PER_M2K
from ..math_helper import clamp
from ..types import RoomThermalConfig

MIN_LEARNED_CAPACITY_FACTOR = 0.5
MAX_LEARNED_CAPACITY_FACTOR = 3.0


class ThermalCapacityModel:
    def __init__(self, config: RoomThermalConfig) -> None:
        self._learned_capacity_factor = 1.0
        # Effective heat capacity from floor area and construction class
        # (ISO 13790). The learned factor covers furniture, open doors and
        # what neighbouring rooms exchange.
        self._base_capacity_j_per_k = (
            config.room_area_m2
            * CONSTRUCTION_CAPACITY_KJ_PER_M2K[config.construction]
            * 1000
        )

    def predict_temperature_change_c(
        self, net_heating_power_w: float, duration_seconds: float
    ) -> float:
        return (net_heating_power_w * duration_seconds) / self.effective_capacity_j_per_k

    @property
    def learned_capacity_factor(self) -> float:
        return self._learned_capacity_factor

    @learned_capacity_factor.setter
    def learned_capacity_factor(self, value: float) -> None:
        self._learned_capacity_factor = clamp(
            value, MIN_LEARNED_CAPACITY_FACTOR, MAX_LEARNED_CAPACITY_FACTOR
        )

    @property
    def effective_capacity_j_per_k(self) -> float:
        return self._base_capacity_j_per_k * self._learned_capacity_factor

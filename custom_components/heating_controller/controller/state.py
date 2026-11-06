from __future__ import annotations

from dataclasses import dataclass

from ..const import (
    DEFAULT_COMFORT_TEMPERATURE_C,
    DEFAULT_ECO_TEMPERATURE_OFFSET_C,
    HeatMode,
)


@dataclass
class HeatingStateConfig:
    boost_temperature_offset_c: float = 5.0
    frost_protection_temperature_c: float = 8.0
    pv_boost_enabled: bool = False
    pv_boost_temperature_offset_c: float = 1.0


class HeatingStateController:
    def __init__(self, config: HeatingStateConfig) -> None:
        self._config = config
        # The comfort switches this room follows; None = not followed.
        # Comfort needs every followed switch on, and at least one of them.
        self._house_comfort: bool | None = None
        self._room_comfort: bool | None = None
        self._window_states: dict[str, bool] = {}
        self._comfort_temperature_c = DEFAULT_COMFORT_TEMPERATURE_C
        self._eco_temperature_offset_c = DEFAULT_ECO_TEMPERATURE_OFFSET_C
        self._pv_boost = False
        self._trv_active = True
        self._window_open = False
        self._active_heat_mode = HeatMode.ECO

    @property
    def house_comfort(self) -> bool | None:
        return self._house_comfort

    def set_house_comfort(self, value: bool | None) -> None:
        self._house_comfort = value

    @property
    def room_comfort(self) -> bool | None:
        return self._room_comfort

    def set_room_comfort(self, value: bool | None) -> None:
        self._room_comfort = value

    @property
    def comfort_temperature_c(self) -> float:
        return self._comfort_temperature_c

    def set_comfort_temperature(self, value: float) -> None:
        self._comfort_temperature_c = value

    @property
    def eco_temperature_offset_c(self) -> float:
        return self._eco_temperature_offset_c

    def set_eco_temperature_offset(self, value: float) -> None:
        self._eco_temperature_offset_c = value

    def set_pv_boost(self, value: bool) -> None:
        self._pv_boost = value

    def set_trv_active(self, value: bool) -> None:
        self._trv_active = value

    def set_active_heat_mode(self, heat_mode: HeatMode) -> None:
        self._active_heat_mode = heat_mode

    def update_window_state(self, key: str, is_open: bool) -> tuple[bool, bool]:
        previous = self._window_open
        self._window_states[key] = is_open
        self._window_open = any(self._window_states.values())
        return previous, self._window_open

    def is_comfort(self) -> bool:
        followed = [
            value
            for value in (self._house_comfort, self._room_comfort)
            if value is not None
        ]
        return bool(followed) and all(followed)

    def automatic_mode_selection_allowed(self, blocked: bool) -> bool:
        return not blocked and not self._window_open

    def desired_automatic_heat_mode(self, blocked: bool) -> HeatMode:
        if not self.automatic_mode_selection_allowed(blocked):
            return self._active_heat_mode
        return HeatMode.COMFORT if self.is_comfort() else HeatMode.ECO

    def should_force_frost_protection(self, blocked: bool) -> bool:
        if blocked:
            return False
        return self._window_open or not self._trv_active

    def resolve_display_mode(self, blocked: bool) -> HeatMode:
        if self.should_force_frost_protection(blocked):
            return HeatMode.FROST_PROTECTION
        return self._active_heat_mode

    def determine_base_target_temperature(
        self, heat_mode: HeatMode | None = None
    ) -> float:
        mode = heat_mode if heat_mode is not None else self._active_heat_mode
        if mode is HeatMode.COMFORT:
            return self._comfort_temperature_c
        if mode is HeatMode.ECO:
            return self._comfort_temperature_c + self._eco_temperature_offset_c
        if mode is HeatMode.BOOST:
            return self._comfort_temperature_c + self._config.boost_temperature_offset_c
        return self._config.frost_protection_temperature_c

    def effective_target_temperature(self, base_target_temperature_c: float) -> float:
        if self._config.pv_boost_enabled and self._pv_boost:
            return (
                base_target_temperature_c + self._config.pv_boost_temperature_offset_c
            )
        return base_target_temperature_c

    @property
    def is_window_open(self) -> bool:
        return self._window_open

    @property
    def is_trv_active(self) -> bool:
        return self._trv_active

    @property
    def is_pv_boost_active(self) -> bool:
        return self._pv_boost

    @property
    def current_heat_mode(self) -> HeatMode:
        return self._active_heat_mode

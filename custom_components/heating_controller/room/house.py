"""What a room asks the house (the hub) for."""

from __future__ import annotations

from typing import Protocol


class House(Protocol):
    def flow_threshold_override_c(self) -> float | None:
        """The flow device's threshold; None without a flow device."""

    def curve_outdoor_ends_c(self) -> tuple[float, float] | None:
        """The heating curve's outdoor end points (cold, warm); None without."""

    def room_hot_water_charge(self) -> tuple[bool, bool]:
        """Whether a hot water charge runs, and whether that signal decides."""

    def rooms_catching_up(self) -> bool:
        """The rooms catch up after a pause of the room heating."""


class DetachedHouse:
    """A room on its own: no flow device, no hot water signal."""

    def flow_threshold_override_c(self) -> float | None:
        return None

    def curve_outdoor_ends_c(self) -> tuple[float, float] | None:
        return None

    def room_hot_water_charge(self) -> tuple[bool, bool]:
        return False, False

    def rooms_catching_up(self) -> bool:
        return False

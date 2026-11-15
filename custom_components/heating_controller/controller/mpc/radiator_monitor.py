from __future__ import annotations

from dataclasses import dataclass

# Every condition below has to hold this long without a break before a
# radiator counts as not heating. Long enough that a sound radiator, even a
# slow one at a low flow, lifts its room by a sensor step or more.
RADIATOR_CHECK_WINDOW_S = 2 * 3600
# The TRV asks for nearly all the heat its radiator can give.
RADIATOR_CHECK_MIN_DEMAND_PCT = 80.0
# A verdict clears once the TRV no longer asks for much heat: the room is
# near its setpoint or got its heat elsewhere.
RADIATOR_CHECK_CLEAR_DEMAND_PCT = 50.0
# The room stays clearly below its mode setpoint, beyond what the setpoint
# correction leaves and a sensor step.
RADIATOR_CHECK_MIN_SHORTFALL_C = 0.5
# A room rising this much gets heat from somewhere; room sensors step in
# 0.1 K and swing by a step or two on their own.
RADIATOR_CHECK_MIN_ROOM_RISE_C = 0.3
# Only judged while the flow lets a fully open radiator give off at least this
# share of its design output. Below it even a sound radiator stays barely warm.
RADIATOR_CHECK_MIN_OUTPUT_FRACTION = 0.3
# The model has to expect the room to rise clearly: the room's full output
# exceeds its loss by at least this share of its design heat load. Otherwise a
# room that stays put is what the model expects, not a radiator that fails.
RADIATOR_CHECK_MIN_SURPLUS_SHARE = 0.2


@dataclass
class RadiatorCheckSample:
    """One controller cycle as the radiator check sees it, for one TRV."""

    now_ts: float
    demand_pct: float | None
    room_temperature_c: float
    mode_target_c: float
    heat_available: bool
    # Output of the fully open radiator as a share of its design output.
    full_output_fraction: float | None
    # Room output with the valves fully open minus its loss, as a share of
    # its design heat load.
    surplus_share: float | None
    # Hot-water charge, open window, adapting TRV or mounting mode: nothing
    # can be told, the window starts over.
    excluded: bool = False


class RadiatorDeliveryMonitor:
    """Watches whether a radiator gives off heat while its TRV asks for it.

    Air in the radiator or a valve stuck shut looks the same from here: the
    TRV asks for nearly full heat, the room stays well below its setpoint,
    the model expects it to rise with the heat available, and it does not.
    The verdict holds while there is nothing to test and clears once the room
    rises or the TRV no longer asks for much heat.
    """

    def __init__(self) -> None:
        self._since_ts: float | None = None
        self._lowest_room_c: float | None = None
        self.not_heating = False
        # Start of the window that led to the verdict.
        self.not_heating_since_ts: float | None = None

    def update(self, sample: RadiatorCheckSample) -> None:
        if self.not_heating:
            self._check_clear(sample)
            return
        if sample.excluded or not self._conditions_hold(sample):
            self._restart()
            return
        room_c = sample.room_temperature_c
        if self._since_ts is None or self._lowest_room_c is None:
            self._since_ts = sample.now_ts
            self._lowest_room_c = room_c
            return
        if room_c - self._lowest_room_c >= RADIATOR_CHECK_MIN_ROOM_RISE_C:
            # The room rises: the radiator heats, the window starts over.
            self._since_ts = sample.now_ts
            self._lowest_room_c = room_c
            return
        self._lowest_room_c = min(self._lowest_room_c, room_c)
        if sample.now_ts - self._since_ts >= RADIATOR_CHECK_WINDOW_S:
            self.not_heating = True
            self.not_heating_since_ts = self._since_ts
            self._lowest_room_c = room_c

    @staticmethod
    def _conditions_hold(sample: RadiatorCheckSample) -> bool:
        return (
            sample.heat_available
            and sample.demand_pct is not None
            and sample.demand_pct >= RADIATOR_CHECK_MIN_DEMAND_PCT
            and sample.room_temperature_c
            <= sample.mode_target_c - RADIATOR_CHECK_MIN_SHORTFALL_C
            and sample.full_output_fraction is not None
            and sample.full_output_fraction >= RADIATOR_CHECK_MIN_OUTPUT_FRACTION
            and sample.surplus_share is not None
            and sample.surplus_share >= RADIATOR_CHECK_MIN_SURPLUS_SHARE
        )

    def _check_clear(self, sample: RadiatorCheckSample) -> None:
        if (
            sample.demand_pct is not None
            and sample.demand_pct < RADIATOR_CHECK_CLEAR_DEMAND_PCT
        ):
            self._clear()
            return
        assert self._lowest_room_c is not None
        if (
            sample.room_temperature_c - self._lowest_room_c
            >= RADIATOR_CHECK_MIN_ROOM_RISE_C
        ):
            self._clear()
            return
        self._lowest_room_c = min(self._lowest_room_c, sample.room_temperature_c)

    def _clear(self) -> None:
        self.not_heating = False
        self.not_heating_since_ts = None
        self._restart()

    def _restart(self) -> None:
        self._since_ts = None
        self._lowest_room_c = None

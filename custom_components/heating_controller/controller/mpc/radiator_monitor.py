from __future__ import annotations

# A TRV reports its valve wide open for this long before the radiator is
# judged. A radiator that gets water warms its TRV within minutes.
RADIATOR_CHECK_WINDOW_S = 3600
RADIATOR_CHECK_MIN_OPENING_PCT = 90.0
# Over that hour the TRV's reading must have risen this much, or sit this far
# above the room. A Danfoss Ally next to a radiator that heats reads 1-2 K
# above the room; next to one full of air (bedroom, 28 and 29 October) it
# stayed within 0.5 K for hours at 94-100 % valve.
RADIATOR_CHECK_MIN_RISE_C = 0.3
RADIATOR_CHECK_MIN_LEAD_C = 1.0
# The rise counts from the lowest reading over this time before the valve
# opened: the controller runs every five minutes, and a radiator that gets
# water warms its TRV within those (dining room, 31 October: 21.01 at the
# opening, already 21.13 at the next cycle, 21.36 a quarter of an hour on).
RADIATOR_CHECK_BASELINE_S = 15 * 60
# A room rising this much over the hour gets heat from its radiator. Where a
# radiator fan blows room air past the TRV it reads close to the room even
# next to a warm radiator (bedroom, 30 October: TRV +0.22 K, room +0.4 K).
# Room sensors step in 0.1 K: next to the radiator full of air (bedroom,
# 28 October) the room still swung by 0.2 K within an hour. A rising room
# only holds off the verdict for that hour; unlike a warm TRV it does not
# clear the opening, sun or a neighbour may lift a room too.
RADIATOR_CHECK_MIN_ROOM_RISE_C = 0.3
# Only judged while the flow lets a fully open radiator give off at least this
# share of its design output. Below it even a sound radiator stays barely warm
# and its TRV hardly notices (study, 30 October: 30 °C flow at a 20.9 °C room,
# about a fifth of design output, after the heating curve was lowered).
RADIATOR_CHECK_MIN_OUTPUT_FRACTION = 0.3


class RadiatorDeliveryMonitor:
    """Watches whether a radiator gives off heat while its valve is open.

    Air in the radiator or a valve stuck shut looks the same from here: the
    TRV reports its valve open, yet its own reading stays near the room's
    and the room does not rise. Only judged while the flow is warm enough
    for a sound radiator to warm its TRV. The verdict holds while the valve
    is shut, heating is off or the flow too low -- there is nothing to test
    then -- and clears as soon as an open valve warms the TRV again.
    """

    def __init__(self) -> None:
        self._open_since_ts: float | None = None
        # (time, TRV reading, room temperature) while the valve is open, and
        # over the last minutes before it opened.
        self._readings: list[tuple[float, float, float]] = []
        self._before_opening: list[tuple[float, float, float]] = []
        # Once the radiator warmed during an opening, a steady reading later
        # on is a radiator that holds its output, not one that fails.
        self._heated_this_opening = False
        self.not_heating = False
        # When the valve opened for the hour that led to the verdict.
        self.not_heating_since_ts: float | None = None

    def update(
        self,
        now_ts: float,
        opening_pct: float | None,
        trv_temperature_c: float | None,
        room_temperature_c: float,
        heat_available: bool,
        full_output_fraction: float | None = None,
        hot_water_charge: bool = False,
    ) -> None:
        if (
            not heat_available
            or opening_pct is None
            or trv_temperature_c is None
            or opening_pct < RADIATOR_CHECK_MIN_OPENING_PCT
            or (
                full_output_fraction is not None
                and full_output_fraction < RADIATOR_CHECK_MIN_OUTPUT_FRACTION
            )
        ):
            self._restart()
            if trv_temperature_c is not None:
                self._before_opening = [
                    reading
                    for reading in self._before_opening
                    if now_ts - reading[0] <= RADIATOR_CHECK_BASELINE_S
                ]
                self._before_opening.append(
                    (now_ts, trv_temperature_c, room_temperature_c)
                )
            return
        if hot_water_charge:
            # The radiators get no water: the hour starts over once it is
            # done.
            self._open_since_ts = None
            self._readings = []
            return

        if self._open_since_ts is None:
            self._open_since_ts = now_ts
            baseline = [
                reading
                for reading in self._before_opening
                if now_ts - reading[0] <= RADIATOR_CHECK_BASELINE_S
            ]
            if baseline:
                # Stamped with the opening, so it stays in the hour judged.
                self._readings = [
                    (
                        now_ts,
                        min(trv_c for _, trv_c, _ in baseline),
                        min(room_c for _, _, room_c in baseline),
                    )
                ]
        self._before_opening = []
        self._readings = [
            reading
            for reading in self._readings
            if now_ts - reading[0] <= RADIATOR_CHECK_WINDOW_S
        ]
        self._readings.append((now_ts, trv_temperature_c, room_temperature_c))

        rise_c = trv_temperature_c - min(trv_c for _, trv_c, _ in self._readings)
        room_rise_c = room_temperature_c - min(
            room_c for _, _, room_c in self._readings
        )
        lead_c = trv_temperature_c - room_temperature_c
        heating = (
            rise_c >= RADIATOR_CHECK_MIN_RISE_C or lead_c >= RADIATOR_CHECK_MIN_LEAD_C
        )
        if heating:
            self._heated_this_opening = True
            self._clear_verdict()
        elif room_rise_c >= RADIATOR_CHECK_MIN_ROOM_RISE_C:
            self._clear_verdict()
        elif (
            not self._heated_this_opening
            and now_ts - self._open_since_ts >= RADIATOR_CHECK_WINDOW_S
        ):
            if not self.not_heating:
                self.not_heating_since_ts = self._open_since_ts
            self.not_heating = True

    def _clear_verdict(self) -> None:
        self.not_heating = False
        self.not_heating_since_ts = None

    def _restart(self) -> None:
        self._open_since_ts = None
        self._readings = []
        self._heated_this_opening = False

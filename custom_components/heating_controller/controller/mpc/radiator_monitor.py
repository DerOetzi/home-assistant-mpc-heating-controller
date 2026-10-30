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
# Only judged while the flow lets a fully open radiator give off at least this
# share of its design output. Below it even a sound radiator stays barely warm
# and its TRV hardly notices (study, 30 October: 30 °C flow at a 20.9 °C room,
# about a fifth of design output, after the heating curve was lowered).
RADIATOR_CHECK_MIN_OUTPUT_FRACTION = 0.3
# A hot-water charge shows as a flow spike on a shared leaving-water sensor
# while the radiators get no water; such samples do not count.
RADIATOR_CHECK_HOT_WATER_FLOW_DELTA_C = 5.0


class RadiatorDeliveryMonitor:
    """Watches whether a radiator gives off heat while its valve is open.

    Air in the radiator or a valve stuck shut looks the same from here: the
    TRV reports its valve open, yet its own reading stays near the room's.
    Only judged while the flow is warm enough for a sound radiator to warm
    its TRV. The verdict holds while the valve is shut, heating is off or
    the flow too low -- there is nothing to test then -- and clears as soon
    as an open valve warms the TRV again.
    """

    def __init__(self) -> None:
        self._open_since_ts: float | None = None
        self._readings: list[tuple[float, float]] = []
        self._lowest_flow_c: float | None = None
        # Once the radiator warmed during an opening, a steady reading later
        # on is a radiator that holds its output, not one that fails.
        self._heated_this_opening = False
        self.not_heating = False

    def update(
        self,
        now_ts: float,
        opening_pct: float | None,
        trv_temperature_c: float | None,
        room_temperature_c: float,
        flow_temperature_c: float | None,
        heat_available: bool,
        full_output_fraction: float | None = None,
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
            return
        if flow_temperature_c is not None:
            if (
                self._lowest_flow_c is not None
                and flow_temperature_c
                > self._lowest_flow_c + RADIATOR_CHECK_HOT_WATER_FLOW_DELTA_C
            ):
                # Hot-water charge: the hour starts over once it is done.
                self._open_since_ts = None
                self._readings = []
                return
            self._lowest_flow_c = (
                flow_temperature_c
                if self._lowest_flow_c is None
                else min(self._lowest_flow_c, flow_temperature_c)
            )

        if self._open_since_ts is None:
            self._open_since_ts = now_ts
        self._readings = [
            (ts, temperature_c)
            for ts, temperature_c in self._readings
            if now_ts - ts <= RADIATOR_CHECK_WINDOW_S
        ]
        self._readings.append((now_ts, trv_temperature_c))

        rise_c = trv_temperature_c - min(temperature_c for _, temperature_c in self._readings)
        lead_c = trv_temperature_c - room_temperature_c
        heating = (
            rise_c >= RADIATOR_CHECK_MIN_RISE_C or lead_c >= RADIATOR_CHECK_MIN_LEAD_C
        )
        if heating:
            self._heated_this_opening = True
            self.not_heating = False
        elif (
            not self._heated_this_opening
            and now_ts - self._open_since_ts >= RADIATOR_CHECK_WINDOW_S
        ):
            self.not_heating = True

    def _restart(self) -> None:
        self._open_since_ts = None
        self._readings = []
        self._lowest_flow_c = None
        self._heated_this_opening = False

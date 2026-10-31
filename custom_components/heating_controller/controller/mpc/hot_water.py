from __future__ import annotations

# A domestic hot-water charge shows as a flow spike on a shared leaving-water
# sensor while the radiators get no water at all (31 October, 05:05-05:35:
# 35 °C, then 44-56 °C). A flow this far above the lowest one of the last
# hour is taken as such a charge.
HOT_WATER_FLOW_DELTA_C = 5.0
# A charge lasts about half an hour. A flow raised for longer than this is the
# new heating flow (a raised heating curve, say) and no charge any more.
HOT_WATER_REFERENCE_WINDOW_S = 3600


class HotWaterChargeDetector:
    """Tells a hot-water charge from heating on the measured flow.

    While heating is unavailable the flow says nothing about the heating
    circuit; the reference starts over once it is back. A heat pump ramping
    up after such a pause may still read as a charge for a while.
    """

    def __init__(self) -> None:
        self._flows: list[tuple[float, float]] = []
        self.charging = False
        # The last flow measured while no charge ran: what the radiators get
        # once the charge is done.
        self.heating_flow_c: float | None = None

    def update(
        self, now_ts: float, flow_temperature_c: float | None, heat_available: bool
    ) -> None:
        if not heat_available:
            self._flows = []
            self.charging = False
            return
        if flow_temperature_c is None:
            return
        self._flows = [
            (ts, flow_c)
            for ts, flow_c in self._flows
            if now_ts - ts <= HOT_WATER_REFERENCE_WINDOW_S
        ]
        self.charging = bool(self._flows) and flow_temperature_c > (
            min(flow_c for _, flow_c in self._flows) + HOT_WATER_FLOW_DELTA_C
        )
        self._flows.append((now_ts, flow_temperature_c))
        if not self.charging:
            self.heating_flow_c = flow_temperature_c

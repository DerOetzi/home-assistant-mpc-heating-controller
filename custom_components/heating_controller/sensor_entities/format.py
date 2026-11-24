"""Value formatting shared by the sensors."""

from __future__ import annotations

from homeassistant.util import dt as dt_util


def rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).isoformat()

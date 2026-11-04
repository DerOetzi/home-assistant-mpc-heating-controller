import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heating_controller.const import RoofOrientation, Surface
from heating_controller.irradiance import (
    HOUR_S,
    MAX_SERIES_AGE_S,
    OPEN_METEO_URL,
    IrradianceInterval,
    IrradianceProvider,
    SunSeries,
    building_surfaces,
    parse_open_meteo,
    request_params,
)
from homeassistant.core import HomeAssistant

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "open_meteo_gti.json").read_text()
)


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def test_surfaces_of_a_house_turned_fifteen_degrees_west():
    surfaces = {
        g.surface: g for g in building_surfaces(-15, RoofOrientation.EAST_WEST, 43)
    }
    assert surfaces[Surface.NORTH].azimuth_deg == 345
    assert surfaces[Surface.EAST].azimuth_deg == 75
    assert surfaces[Surface.SOUTH].azimuth_deg == 165
    assert surfaces[Surface.WEST].azimuth_deg == 255
    assert surfaces[Surface.ROOF_EAST].azimuth_deg == 75
    assert surfaces[Surface.ROOF_EAST].tilt_deg == 43
    assert surfaces[Surface.WEST].tilt_deg == 90
    assert Surface.ROOF_NORTH not in surfaces


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        (Surface.SOUTH, -15),
        (Surface.EAST, -105),
        (Surface.NORTH, 165),
        (Surface.WEST, 75),
    ],
)
def test_open_meteo_azimuth(surface, expected):
    geometry = next(
        g
        for g in building_surfaces(-15, RoofOrientation.NONE, 45)
        if g.surface == surface
    )
    assert geometry.open_meteo_azimuth_deg == pytest.approx(expected)
    params = request_params(49.46, 11.36, geometry)
    assert params["azimuth"] == pytest.approx(expected)
    assert params["tilt"] == 90
    assert params["hourly"] == "global_tilted_irradiance"


def test_parse_takes_the_preceding_hour_and_skips_gaps():
    intervals = parse_open_meteo(FIXTURE)
    assert len(intervals) == 72 - 1
    noon = next(i for i in intervals if i.end_ts == _ts("2026-11-04T12:00"))
    assert noon.start_ts == _ts("2026-11-04T11:00")
    assert noon.value_w_m2 == 600


def _step_series(start_value: float, step_value: float, step_ts: float) -> SunSeries:
    intervals = [
        IrradianceInterval(
            step_ts + k * HOUR_S,
            step_ts + (k + 1) * HOUR_S,
            start_value if k < 0 else step_value,
        )
        for k in range(-24, 24)
    ]
    return SunSeries({Surface.SOUTH: intervals}, step_ts)


def test_filtered_value_after_a_step():
    series = _step_series(0.0, 500.0, 1_000_000.0)
    value = series.filtered_at(Surface.SOUTH, 1_000_000.0 + 3 * HOUR_S, 3 * HOUR_S)
    assert value == pytest.approx(316.06, abs=1)


def test_value_interpolates_between_hour_midpoints():
    series = _step_series(0.0, 500.0, 1_000_000.0)
    # Midpoints at -30 min (0) and +30 min (500).
    assert series.value_at(Surface.SOUTH, 1_000_000.0) == pytest.approx(250)
    assert series.value_at(Surface.SOUTH, 1_000_000.0 + 2 * HOUR_S) == 500


def test_outside_the_series_is_missing():
    series = _step_series(0.0, 500.0, 1_000_000.0)
    assert series.value_at(Surface.SOUTH, 1_000_000.0 + 30 * HOUR_S) is None
    assert series.value_at(Surface.WEST, 1_000_000.0) is None
    assert series.filtered_at(Surface.WEST, 1_000_000.0, HOUR_S) is None


def test_series_goes_stale():
    series = _step_series(0.0, 500.0, 1_000_000.0)
    assert series.fresh(1_000_000.0 + MAX_SERIES_AGE_S)
    assert not series.fresh(1_000_000.0 + MAX_SERIES_AGE_S + 1)


def test_forecast_lists_coming_hours():
    series = _step_series(0.0, 500.0, 1_000_000.0)
    forecast = series.forecast(Surface.SOUTH, 1_000_000.0 + 10, 3)
    assert [value for _, value in forecast] == [500.0, 500.0, 500.0]


async def test_provider_fetches_each_surface(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.get(OPEN_METEO_URL, json=FIXTURE)
    updates = []
    provider = IrradianceProvider(
        hass,
        [
            g
            for g in building_surfaces(-15, RoofOrientation.EAST_WEST, 43)
            if g.surface in (Surface.SOUTH, Surface.ROOF_WEST)
        ],
        lambda: updates.append(True),
    )
    assert await provider.async_refresh()
    assert aioclient_mock.call_count == 2
    assert set(provider.series.surfaces) == {Surface.SOUTH, Surface.ROOF_WEST}
    assert provider.series.value_at(Surface.SOUTH, _ts("2026-11-04T11:30")) == 600
    assert provider.last_error is None
    assert updates


async def test_provider_keeps_the_last_series_on_error(
    hass: HomeAssistant, aioclient_mock
):
    aioclient_mock.get(OPEN_METEO_URL, json=FIXTURE)
    geometry = building_surfaces(0, RoofOrientation.NONE, 45)[:1]
    provider = IrradianceProvider(hass, geometry, lambda: None)
    assert await provider.async_refresh()
    fetched = provider.series.fetched_ts

    aioclient_mock.clear_requests()
    aioclient_mock.get(OPEN_METEO_URL, status=500)
    assert not await provider.async_refresh()
    assert provider.series.fetched_ts == fetched
    assert provider.last_error


async def test_hub_with_room_surfaces_adds_irradiance_sensors(
    hass: HomeAssistant, aioclient_mock
):
    from heating_controller.const import DOMAIN
    from homeassistant.core import ServiceCall
    from test_coordinator import _hub_entry, _room_data, _seed_entities

    aioclient_mock.get(OPEN_METEO_URL, json=FIXTURE)
    _seed_entities(hass)

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)

    entry = _hub_entry(
        _room_data(
            hass,
            building_rotation_deg=-15,
            roof_orientation="east_west",
            roof_tilt_deg=43,
            window_surfaces=["south"],
            opaque_surfaces=["roof_west"],
        )
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hub = entry.runtime_data
    assert hub.irradiance is not None
    assert {g.surface for g in hub.irradiance.geometries} == {
        Surface.SOUTH,
        Surface.ROOF_WEST,
    }
    assert hass.data[DOMAIN][entry.entry_id].sun_provider is hub.irradiance
    sensors = [
        state.entity_id
        for state in hass.states.async_all("sensor")
        if "irradiance" in state.entity_id or "einstrahlung" in state.entity_id
    ]
    assert len(sensors) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hub_without_room_surfaces_fetches_nothing(
    hass: HomeAssistant, aioclient_mock
):
    from homeassistant.core import ServiceCall
    from test_coordinator import _hub_entry, _room_data, _seed_entities

    _seed_entities(hass)

    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)
    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.irradiance is None
    assert aioclient_mock.call_count == 0
    assert await hass.config_entries.async_unload(entry.entry_id)

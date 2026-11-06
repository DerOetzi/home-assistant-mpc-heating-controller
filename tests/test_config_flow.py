import pytest
from heating_controller.const import DOMAIN, HUB_TITLE, SUBENTRY_TYPE_ROOM
from heating_controller.migration import HUB_VERSION
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_coordinator import (
    ENTRY_DATA,
    TRV_EMITTER,
    _hub_entry,
    register_trv_device,
)

HUB_SOURCES = {
    "outdoor_temperature_entity": "sensor.outdoor_temperature",
    "heat_source_climate_entity": "climate.heat_source",
    "flow_temperature_entity": "sensor.flow_temperature",
    "flow_threshold_c": 28.0,
    "flow_threshold_entity": "input_number.flow_threshold",
    "flow_curve_entity": "sensor.flow_curve",
    "frost_protection_temperature_c": 6.0,
}
HUB_SUN = {
    "building_rotation_deg": -15,
    "roof_orientation": "east_west",
    "roof_tilt_deg": 43,
    "workday_entity": "binary_sensor.workday_sensor",
}
HUB_CONTROL = {
    "design_outdoor_temperature_c": -12.0,
    "design_temperature_system": "system_55_45",
    "mpc_demand_hysteresis_pct": 5.0,
    "mpc_hold_time_s": 300.0,
    "mpc_hold_override_demand_pct": 40.0,
    "mpc_max_demand_step_pct": 20.0,
    "max_sensor_age_s": 1800.0,
    "stationary_range_c": 0.3,
    "target_band_c": 0.15,
}
ROOM_SIGNALS = {
    "room_sensor_entity": "sensor.wohnzimmer_temperatur",
    "window_contact_entities": [],
    "use_house_comfort_conditions": False,
    "room_comfort_switch": True,
    "learning_block_entity": "binary_sensor.extra_heater",
}
ROOM_SETTINGS = {
    "boost_enabled": True,
    "boost_temperature_offset_c": 4.0,
    "pv_boost_enabled": True,
    "pv_boost_temperature_offset_c": 2.0,
    "lead_heating_limit": True,
    "lead_flow": False,
}
ROOM_DESIGN = {
    "design_indoor_temperature_c": 20.0,
    "room_heat_load_w": 1000.0,
    "room_area_m2": 21.0,
    "construction": "medium",
}


async def test_hub_flow_creates_the_hub(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "sources"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], HUB_SOURCES
    )
    assert result["step_id"] == "control"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], HUB_CONTROL
    )
    assert result["step_id"] == "sun"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], HUB_SUN)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == HUB_TITLE
    assert result["data"] == {
        **HUB_SOURCES,
        **HUB_CONTROL,
        **HUB_SUN,
        "max_flow_gap_min": 30,
        "rate_limit_reserve": 20,
        "max_writes_per_day": 40,
        "flow_gate_close_surplus_c": 0.5,
        "flow_gate_open_surplus_c": 0.2,
        "flow_gate_hold_time_s": 900.0,
        "trv_setpoint_mode": "relative",
    }


async def test_hub_sun_step_defaults(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], HUB_SOURCES
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], HUB_CONTROL
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["building_rotation_deg"] == 0
    assert result["data"]["roof_orientation"] == "none"
    assert result["data"]["roof_tilt_deg"] == 45
    assert "workday_entity" not in result["data"]
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.version == HUB_VERSION


async def test_only_one_hub(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, version=HUB_VERSION, unique_id=DOMAIN).add_to_hass(
        hass
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_hub_reconfigure_clears_an_optional_entity(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=HUB_VERSION,
        unique_id=DOMAIN,
        data={**HUB_SOURCES, **HUB_CONTROL},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "sources"
    sources = {
        key: value for key, value in HUB_SOURCES.items() if key != "flow_curve_entity"
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**sources, "flow_threshold_c": 32.0}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], HUB_CONTROL
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], HUB_SUN)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert "flow_curve_entity" not in entry.data
    assert entry.data["flow_threshold_c"] == 32.0


def _empty_hub(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=HUB_VERSION,
        unique_id=DOMAIN,
        data={**HUB_SOURCES, **HUB_CONTROL},
    )
    entry.add_to_hass(hass)
    return entry


async def _start_room_flow(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": config_entries.SOURCE_USER},
    )


async def test_room_flow_adds_a_room(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass)
    entry = _empty_hub(hass)
    result = await _start_room_flow(hass, entry)
    assert result["step_id"] == "user"

    configure = hass.config_entries.subentries.async_configure
    result = await configure(
        result["flow_id"], {"room_name": "Wohnzimmer", "trv_count": 1}
    )
    assert result["step_id"] == "trv"
    result = await configure(
        result["flow_id"], {"device_id": device_id, "emitter_type": "panel"}
    )
    assert result["step_id"] == "trv_details"
    result = await configure(
        result["flow_id"],
        {"radiator_type": "22", "width_mm": 1000.0, "height_mm": 600.0},
    )
    assert result["step_id"] == "signals"
    result = await configure(result["flow_id"], ROOM_SIGNALS)
    assert result["step_id"] == "settings"
    result = await configure(result["flow_id"], ROOM_SETTINGS)
    assert result["step_id"] == "design"
    result = await configure(result["flow_id"], ROOM_DESIGN)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Wohnzimmer"
    assert subentry.unique_id == "wohnzimmer"
    assert dict(subentry.data) == {
        "room_name": "Wohnzimmer",
        "trvs": [{"device_id": device_id, **TRV_EMITTER}],
        "window_surfaces": [],
        "opaque_surfaces": [],
        **ROOM_SIGNALS,
        **ROOM_SETTINGS,
        **ROOM_DESIGN,
    }


async def test_room_flow_offers_the_roof_surfaces_of_the_hub(
    hass: HomeAssistant,
) -> None:
    device_id = register_trv_device(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=HUB_VERSION,
        unique_id=DOMAIN,
        data={**HUB_SOURCES, **HUB_CONTROL, **HUB_SUN},
    )
    entry.add_to_hass(hass)
    configure = hass.config_entries.subentries.async_configure
    result = await _start_room_flow(hass, entry)
    result = await configure(
        result["flow_id"], {"room_name": "Schlafzimmer", "trv_count": 1}
    )
    result = await configure(
        result["flow_id"], {"device_id": device_id, "emitter_type": "panel"}
    )
    result = await configure(
        result["flow_id"],
        {"radiator_type": "22", "width_mm": 1000.0, "height_mm": 600.0},
    )
    result = await configure(result["flow_id"], ROOM_SIGNALS)
    result = await configure(result["flow_id"], ROOM_SETTINGS)
    assert result["step_id"] == "design"

    result = await configure(
        result["flow_id"],
        {
            **ROOM_DESIGN,
            "window_surfaces": ["south"],
            "opaque_surfaces": ["south", "roof_east", "roof_west"],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.data["window_surfaces"] == ["south"]
    assert subentry.data["opaque_surfaces"] == ["south", "roof_east", "roof_west"]


async def test_room_flow_without_roof_rejects_a_roof_surface(
    hass: HomeAssistant,
) -> None:
    device_id = register_trv_device(hass)
    entry = _empty_hub(hass)
    configure = hass.config_entries.subentries.async_configure
    result = await _start_room_flow(hass, entry)
    result = await configure(
        result["flow_id"], {"room_name": "Schlafzimmer", "trv_count": 1}
    )
    result = await configure(
        result["flow_id"], {"device_id": device_id, "emitter_type": "panel"}
    )
    result = await configure(
        result["flow_id"],
        {"radiator_type": "22", "width_mm": 1000.0, "height_mm": 600.0},
    )
    result = await configure(result["flow_id"], ROOM_SIGNALS)
    result = await configure(result["flow_id"], ROOM_SETTINGS)

    with pytest.raises(InvalidData):
        await configure(
            result["flow_id"], {**ROOM_DESIGN, "opaque_surfaces": ["roof_east"]}
        )


async def test_room_flow_rejects_a_device_without_profile(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass, model_id="unknown-model")
    entry = _empty_hub(hass)
    result = await _start_room_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"room_name": "Wohnzimmer", "trv_count": 1}
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id, "emitter_type": "panel"}
    )

    assert result["step_id"] == "trv"
    assert result["errors"] == {"device_id": "unsupported_device"}


async def test_room_flow_rejects_a_taken_name(hass: HomeAssistant) -> None:
    entry = _hub_entry(ENTRY_DATA)
    entry.add_to_hass(hass)
    result = await _start_room_flow(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"room_name": "wohnzimmer", "trv_count": 1}
    )

    assert result["step_id"] == "user"
    assert result["errors"] == {"room_name": "already_configured"}


async def test_room_area_and_construction_have_no_default(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass)
    entry = _empty_hub(hass)
    result = await _start_room_flow(hass, entry)
    configure = hass.config_entries.subentries.async_configure
    result = await configure(
        result["flow_id"], {"room_name": "Wohnzimmer", "trv_count": 1}
    )
    result = await configure(
        result["flow_id"], {"device_id": device_id, "emitter_type": "panel"}
    )
    result = await configure(
        result["flow_id"],
        {"radiator_type": "22", "width_mm": 1000.0, "height_mm": 600.0},
    )
    result = await configure(result["flow_id"], ROOM_SIGNALS)
    result = await configure(result["flow_id"], ROOM_SETTINGS)

    with pytest.raises(InvalidData):
        await configure(
            result["flow_id"],
            {"design_indoor_temperature_c": 20.0, "room_heat_load_w": 1000.0},
        )


async def test_room_reconfigure_changes_the_room(hass: HomeAssistant) -> None:
    device_id = register_trv_device(hass)
    entry = _hub_entry(
        {
            **ENTRY_DATA,
            "trvs": [{"device_id": device_id, **TRV_EMITTER}],
            "lead_heating_limit": False,
            "lead_flow": False,
        }
    )
    entry.add_to_hass(hass)

    result = await entry.start_subentry_reconfigure_flow(hass, entry.entry_id)
    assert result["step_id"] == "trv"
    configure = hass.config_entries.subentries.async_configure
    result = await configure(
        result["flow_id"], {"device_id": device_id, "emitter_type": "panel"}
    )
    result = await configure(
        result["flow_id"],
        {"radiator_type": "33", "width_mm": 1200.0, "height_mm": 600.0},
    )
    assert result["step_id"] == "signals"
    signals = {
        key: value for key, value in ROOM_SIGNALS.items() if key != "room_sensor_entity"
    }
    result = await configure(result["flow_id"], signals)
    result = await configure(result["flow_id"], ROOM_SETTINGS)
    result = await configure(result["flow_id"], ROOM_DESIGN)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    data = entry.subentries[entry.entry_id].data
    assert data["room_name"] == "Wohnzimmer"
    assert data["trvs"] == [
        {
            "device_id": device_id,
            "emitter_type": "panel",
            "radiator_type": "33",
            "width_mm": 1200.0,
            "height_mm": 600.0,
        }
    ]
    assert "room_sensor_entity" not in data
    assert data["lead_heating_limit"] is True
    assert data["learning_block_entity"] == "binary_sensor.extra_heater"

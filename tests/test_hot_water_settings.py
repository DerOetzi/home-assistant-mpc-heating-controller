"""The hot water subentry's keys match its form fields and texts."""

import json
from pathlib import Path

from heating_controller.config_flows.hot_water import (
    _hw_circulation_schema,
    _hw_legionella_schema,
    _hw_modes_schema,
    _hw_sources_schema,
)
from heating_controller.hot_water_settings import (
    HOT_WATER_DEFAULTS,
    HotWaterKey,
    HotWaterNumber,
    HotWaterSettings,
)

STRINGS = (
    Path(__file__).parents[1] / "custom_components/heating_controller/strings.json"
)


def test_every_key_is_a_form_field_with_a_text() -> None:
    steps = json.loads(STRINGS.read_text())["config_subentries"]["hot_water"]["step"]
    texts = {key for step in steps.values() for key in step["data"]}
    defaults = {**HOT_WATER_DEFAULTS, HotWaterKey.WATER_HEATER_ENTITY: "x"}
    fields = {
        str(marker.schema)
        for build in (
            _hw_sources_schema,
            _hw_modes_schema,
            _hw_legionella_schema,
            _hw_circulation_schema,
        )
        for marker in build(defaults).schema
    }

    assert fields == set(HotWaterKey)
    assert texts == set(HotWaterKey)


def test_thresholds_apply_only_with_their_entity() -> None:
    without = HotWaterSettings.from_data({HotWaterKey.WATER_HEATER_ENTITY: "x"})
    with_return = HotWaterSettings.from_data(
        {
            HotWaterKey.WATER_HEATER_ENTITY: "x",
            HotWaterKey.RETURN_ENTITY: "sensor.return",
            HotWaterKey.BATTERY_SOC_ENTITY: "sensor.soc",
        }
    )

    assert without.legionella_config().return_threshold_c is None
    assert with_return.legionella_config().return_threshold_c == 58.0
    numbers = dict.fromkeys(HotWaterNumber, 50.0)
    assert without.hot_water_config(numbers).battery_soc_min is None
    assert with_return.hot_water_config(numbers).battery_soc_min == 50.0

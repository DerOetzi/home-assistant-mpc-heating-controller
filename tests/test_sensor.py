"""The hold-flow sensor is the ungated counterpart to the min-flow sensor."""

import pytest
from heating_controller.const import FlowSupplyStatus
from homeassistant.core import HomeAssistant, ServiceCall
from test_coordinator import _hub_entry, _room_data, _seed_entities


def _register_stub_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)


async def _setup_coasting_room(hass: HomeAssistant, data: dict) -> None:
    """Room above target on stored heat, outdoor below target."""
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "12.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "25.0")
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "heat", {"current_temperature": 25.0}
    )
    _register_stub_services(hass)
    entry = _hub_entry(data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_hold_flow_sensor_keeps_reporting_while_the_gate_is_closed(
    hass: HomeAssistant,
) -> None:
    await _setup_coasting_room(hass, {**_room_data(hass), "flow_gate_hold_time_s": 0.0})

    gated = hass.states.get("sensor.wohnzimmer_minimum_flow_temperature")
    hold = hass.states.get("sensor.wohnzimmer_hold_flow_temperature")
    assert gated is not None and hold is not None

    assert float(gated.state) == 0
    assert gated.attributes["supply_status"] == FlowSupplyStatus.SURPLUS
    assert gated.attributes["flow_gate_closed"] is True

    assert float(hold.state) > 0
    assert hold.attributes["flow_gate_closed"] is True
    assert hold.attributes["gated_min_flow_temperature_c"] == 0
    assert hold.attributes["room_surplus_c"] > 0

    gated_hold = hass.states.get("sensor.wohnzimmer_gated_hold_flow_temperature")
    assert gated_hold is not None
    assert float(gated_hold.state) == 0
    assert gated_hold.attributes["flow_gate_closed"] is True
    assert gated_hold.attributes["hold_flow_temperature_c"] == float(hold.state)


async def test_hold_flow_sensor_matches_min_flow_while_the_gate_is_open(
    hass: HomeAssistant,
) -> None:
    await _setup_coasting_room(hass, _room_data(hass))

    gated = hass.states.get("sensor.wohnzimmer_minimum_flow_temperature")
    hold = hass.states.get("sensor.wohnzimmer_hold_flow_temperature")
    assert gated is not None and hold is not None

    assert gated.attributes["flow_gate_closed"] is False
    assert float(gated.state) > 0
    assert float(hold.state) == float(gated.state)


async def test_min_flow_sensor_reports_the_recovery_flow_for_a_cold_room(
    hass: HomeAssistant,
) -> None:
    _seed_entities(hass)
    hass.states.async_set("sensor.outdoor_temperature", "5.0")
    hass.states.async_set("sensor.wohnzimmer_temperatur", "15.0")
    hass.states.async_set(
        "climate.heizung_wohnzimmer", "off", {"current_temperature": 58.0}
    )
    _register_stub_services(hass)
    entry = _hub_entry(_room_data(hass))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    gated = hass.states.get("sensor.wohnzimmer_minimum_flow_temperature")
    assert gated is not None

    recovery_c = gated.attributes["recovery_flow_temperature_c"]
    assert recovery_c > gated.attributes["hold_flow_temperature_c"]
    assert float(gated.state) == recovery_c

    gated_hold = hass.states.get("sensor.wohnzimmer_gated_hold_flow_temperature")
    assert gated_hold is not None
    assert float(gated_hold.state) == gated.attributes["hold_flow_temperature_c"]
    assert float(gated_hold.state) < recovery_c
    assert gated.attributes["recovery_flow_saturated"] in (True, False)


async def test_ua_learning_status_sensor_exposes_the_heat_balance(
    hass: HomeAssistant,
) -> None:
    await _setup_coasting_room(hass, _room_data(hass))

    status = hass.states.get("sensor.wohnzimmer_ua_learning_status")
    assert status is not None
    for key in (
        "collecting_since",
        "due",
        "last_learned",
        "last_target_factor",
        "last_drift_c",
        "last_window_h",
        "last_mean_outdoor_c",
        "last_mean_room_c",
        "last_mean_flow_c",
        "last_mean_heating_w",
    ):
        assert key in status.attributes
    assert status.attributes["last_learned"] is None
    assert status.attributes["last_mean_outdoor_c"] is None
    assert status.attributes["last_mean_heating_w"] is None
    assert hass.states.get("sensor.wohnzimmer_mpc_learning_status") is None


def test_ua_learning_status_sensor_rounds_the_means() -> None:
    from types import SimpleNamespace

    from heating_controller.controller.mpc.types import UaBalanceInfo
    from heating_controller.sensor import UaLearningStatusSensor

    info = UaBalanceInfo(
        last_mean_outdoor_c=5.04567,
        last_mean_room_c=20.98765,
        last_mean_flow_c=None,
        last_mean_heating_w=312.5678,
    )
    sensor = object.__new__(UaLearningStatusSensor)
    from heating_controller.controller.mpc.models.gains import RoomGainModel

    gains = RoomGainModel(1000, 31.25)
    for delta_c in (10.0, 14.0, 18.0):
        gains.add_night_point(delta_c, 20.0 * delta_c - 50.0, 6.0, 31.25)
    sensor._coordinator = SimpleNamespace(
        mpc=SimpleNamespace(get_ua_balance_info=lambda: info, gain_model=gains)
    )

    attributes = sensor.extra_state_attributes

    assert attributes["last_mean_outdoor_c"] == 5.05
    assert attributes["last_mean_room_c"] == 20.99
    assert attributes["last_mean_flow_c"] is None
    assert attributes["last_mean_heating_w"] == 312.6
    assert attributes["learned_gain_w"] == pytest.approx(50.0, abs=5)
    assert attributes["gain_fit_points"] == 3
    assert attributes["gain_determined"] is True
    assert attributes["regression_ua_factor"] == pytest.approx(0.64, abs=0.02)


async def test_capacity_learning_status_sensor_reports_the_replay(
    hass: HomeAssistant,
) -> None:
    await _setup_coasting_room(hass, _room_data(hass))

    status = hass.states.get("sensor.wohnzimmer_capacity_learning_status")
    assert status is not None
    assert status.state == "waiting"
    # 20 m² medium construction, learned factor 1.0.
    assert status.attributes["capacity_j_per_k"] == 20 * 165 * 1000


def test_valve_curve_sensor_shows_status_and_rounded_bins() -> None:
    from types import SimpleNamespace

    from heating_controller.controller.mpc.valve_curve import ValveCurveLearner
    from heating_controller.sensor import ValveCurveSensor

    learner = ValveCurveLearner(None)
    learner.restore({"samples": [[0.7123, 30.0], [0.7123, 100 / 3], [0.7123, 40.0]]})
    sensor = object.__new__(ValveCurveSensor)
    # Only TRVs with a valve entity are in the report; a second TRV without
    # one stays out of the attributes.
    sensor._coordinator = SimpleNamespace(
        mpc=SimpleNamespace(
            valve_curve_report=lambda: {"climate.a": (learner.status, learner.bins())}
        ),
    )

    assert sensor.native_value == 0
    assert sensor.extra_state_attributes == {
        "climate.a": {"status": "learning", "bins": [[0.71, 33.3, 3, 8.0]]}
    }


async def test_valve_curve_sensor_is_set_up(hass: HomeAssistant) -> None:
    await _setup_coasting_room(hass, _room_data(hass))

    curve = hass.states.get("sensor.wohnzimmer_valve_curve")
    assert curve is not None
    # The room's TRV reports no valve opening, so it learns nothing.
    assert curve.state == "0"
    assert "climate.heizung_wohnzimmer" not in curve.attributes


async def test_no_disturbance_sensor_is_set_up(hass: HomeAssistant) -> None:
    await _setup_coasting_room(hass, _room_data(hass))

    assert hass.states.get("sensor.wohnzimmer_valve_curve") is not None
    assert hass.states.get("sensor.wohnzimmer_disturbance_power") is None

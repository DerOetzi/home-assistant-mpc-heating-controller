from heating_controller.const import HeatMode
from heating_controller.controller.state import (
    HeatingStateConfig,
    HeatingStateController,
)


def make_controller(**overrides) -> HeatingStateController:
    return HeatingStateController(HeatingStateConfig(**overrides))


def test_is_comfort_false_when_no_conditions_recorded():
    controller = make_controller()
    assert controller.is_comfort() is False


def test_is_comfort_true_when_all_conditions_true():
    controller = make_controller()
    controller.set_comfort_condition("global_release", True)
    controller.set_comfort_condition("guest_present", True)
    assert controller.is_comfort() is True


def test_is_comfort_false_when_any_condition_false():
    controller = make_controller()
    controller.set_comfort_condition("global_release", True)
    controller.set_comfort_condition("guest_present", False)
    assert controller.is_comfort() is False


def test_room_without_comfort_conditions_stays_in_eco():
    """A room configured with no comfort condition must never rise to comfort
    on its own -- only a manual override may lift it. Guards against a
    simplification to all(()), which is True."""
    controller = make_controller()

    assert controller.is_comfort() is False
    assert controller.desired_automatic_heat_mode(blocked=False) == HeatMode.ECO


def test_desired_automatic_heat_mode_follows_comfort_condition():
    controller = make_controller()
    controller.set_comfort_condition("global_release", True)
    assert controller.desired_automatic_heat_mode(blocked=False) == HeatMode.COMFORT

    controller.set_comfort_condition("global_release", False)
    assert controller.desired_automatic_heat_mode(blocked=False) == HeatMode.ECO


def test_desired_automatic_heat_mode_keeps_current_when_not_allowed():
    controller = make_controller()
    controller.set_active_heat_mode(HeatMode.BOOST)
    controller.set_comfort_condition("global_release", True)

    controller.update_window_state("a", True)
    assert controller.desired_automatic_heat_mode(blocked=False) == HeatMode.BOOST

    controller.update_window_state("a", False)
    assert controller.desired_automatic_heat_mode(blocked=True) == HeatMode.BOOST


def test_window_open_blocks_automatic_mode_selection():
    controller = make_controller()
    controller.update_window_state("kitchen", True)
    assert controller.automatic_mode_selection_allowed(blocked=False) is False


def test_update_window_state_is_or_combined_across_topics():
    controller = make_controller()
    previous, current = controller.update_window_state("a", True)
    assert (previous, current) == (False, True)

    previous, current = controller.update_window_state("b", False)
    assert (previous, current) == (True, True)

    previous, current = controller.update_window_state("a", False)
    assert (previous, current) == (True, False)


def test_should_force_frost_protection_on_open_window_or_no_heating():
    controller = make_controller()
    assert controller.should_force_frost_protection(blocked=False) is False

    controller.update_window_state("a", True)
    assert controller.should_force_frost_protection(blocked=False) is True

    controller.update_window_state("a", False)
    controller.set_trv_active(False)
    assert controller.should_force_frost_protection(blocked=False) is True


def test_resolve_display_mode_shows_frost_protection_when_forced():
    controller = make_controller()
    controller.set_active_heat_mode(HeatMode.COMFORT)
    controller.update_window_state("a", True)
    assert controller.resolve_display_mode(blocked=False) == HeatMode.FROST_PROTECTION


def test_blocked_bypasses_forced_frost_protection_regardless_of_chosen_mode():
    controller = make_controller()
    controller.update_window_state("a", True)
    controller.set_trv_active(False)

    assert controller.should_force_frost_protection(blocked=False) is True
    assert controller.should_force_frost_protection(blocked=True) is False

    controller.set_active_heat_mode(HeatMode.COMFORT)
    assert controller.resolve_display_mode(blocked=True) == HeatMode.COMFORT

    controller.set_active_heat_mode(HeatMode.FROST_PROTECTION)
    assert controller.resolve_display_mode(blocked=True) == HeatMode.FROST_PROTECTION


def test_unblocking_reasserts_forced_frost_protection_if_still_applicable():
    controller = make_controller()
    controller.set_trv_active(False)
    controller.set_active_heat_mode(HeatMode.COMFORT)

    assert controller.resolve_display_mode(blocked=True) == HeatMode.COMFORT
    assert controller.resolve_display_mode(blocked=False) == HeatMode.FROST_PROTECTION


def test_determine_base_target_temperature_per_mode():
    controller = make_controller(
        boost_temperature_offset_c=5.0, frost_protection_temperature_c=8.0
    )
    controller.set_comfort_temperature(22.0)
    controller.set_eco_temperature_offset(-2.0)

    assert controller.determine_base_target_temperature(HeatMode.COMFORT) == 22.0
    assert controller.determine_base_target_temperature(HeatMode.ECO) == 20.0
    assert controller.determine_base_target_temperature(HeatMode.BOOST) == 27.0
    assert (
        controller.determine_base_target_temperature(HeatMode.FROST_PROTECTION) == 8.0
    )


def test_effective_target_temperature_adds_pv_boost_offset_when_enabled():
    controller = make_controller(pv_boost_enabled=True, pv_boost_temperature_offset_c=1.5)
    controller.set_pv_boost(True)
    assert controller.effective_target_temperature(20.0) == 21.5

    controller.set_pv_boost(False)
    assert controller.effective_target_temperature(20.0) == 20.0


def test_effective_target_temperature_ignores_pv_boost_when_disabled():
    controller = make_controller(pv_boost_enabled=False)
    controller.set_pv_boost(True)
    assert controller.effective_target_temperature(20.0) == 20.0


def test_initial_state_defaults():
    controller = make_controller()
    assert controller.is_comfort() is False
    assert controller.is_window_open is False
    assert controller.is_trv_active is True
    assert controller.is_pv_boost_active is False
    assert controller.current_heat_mode == HeatMode.ECO

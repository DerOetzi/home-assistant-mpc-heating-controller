"""Make the repo's custom_components importable and discoverable by HA's loader."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

REPO_ROOT = Path(__file__).parent.parent

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "custom_components"))

pytest_plugins = "pytest_homeassistant_custom_component"

import custom_components

if str(REPO_ROOT / "custom_components") not in custom_components.__path__:
    custom_components.__path__.append(str(REPO_ROOT / "custom_components"))


@pytest.fixture(autouse=True)
def no_recompute_debounce(monkeypatch: pytest.MonkeyPatch):
    # A real delay would stall every test that changes a state, and never end
    # under a frozen clock. Tests import the package directly, HA loads it as
    # custom_components.heating_controller: two module objects.
    for package in ("heating_controller", "custom_components.heating_controller"):
        monkeypatch.setattr(f"{package}.coordinator.RECOMPUTE_DEBOUNCE_S", 0)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    hass: HomeAssistant, enable_custom_integrations: None
):
    yield

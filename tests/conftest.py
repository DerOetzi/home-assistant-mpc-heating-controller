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

import custom_components  # noqa: E402

if str(REPO_ROOT / "custom_components") not in custom_components.__path__:
    custom_components.__path__.append(str(REPO_ROOT / "custom_components"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    hass: HomeAssistant, enable_custom_integrations: None
):
    yield

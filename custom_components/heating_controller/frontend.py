"""Serve the Lovelace card that ships with this integration.

The card lives in the integration repo rather than a separate HACS plugin
repo so that card and integration can never drift apart in version. See
docs/card-design.md.

The files are served with HA's long cache headers -- revalidating on every
load races Lovelace's first render. To still pick up a changed card, the URL
carries a hash of all card files as a path segment: the entry module and
every module it imports relatively ("./entities.js") move to a new URL
together, so the browser can never mix a cached module with a new one.
"""

from __future__ import annotations

import hashlib
import os

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

CARD_FILENAME = "heating-controller-card.js"
STATIC_URL_PATH = f"/{DOMAIN}_frontend"

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
_CARD_PATH = os.path.join(_FRONTEND_DIR, CARD_FILENAME)
_REGISTERED_KEY = f"{DOMAIN}_frontend_registered"
_HASH_LENGTH = 12


def frontend_hash(directory: str = _FRONTEND_DIR) -> str:
    """Hash over the names and contents of all card files."""
    digest = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not name.endswith(".js") or not os.path.isfile(path):
            continue
        digest.update(name.encode())
        with open(path, "rb") as file:
            digest.update(file.read())
    return digest.hexdigest()[:_HASH_LENGTH]


def card_url(version: str) -> str:
    return f"{STATIC_URL_PATH}/{version}/{CARD_FILENAME}"


async def async_setup(hass: HomeAssistant) -> None:
    """Register the card once, no matter how many rooms are configured."""
    if hass.data.get(_REGISTERED_KEY):
        return
    hass.data[_REGISTERED_KEY] = True

    version = await hass.async_add_executor_job(frontend_hash)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"{STATIC_URL_PATH}/{version}", _FRONTEND_DIR)]
    )
    add_extra_js_url(hass, card_url(version))

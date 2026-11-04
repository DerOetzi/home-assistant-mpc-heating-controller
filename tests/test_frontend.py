"""The card is served by the integration, so setup must register it."""

import os

from heating_controller import frontend
from heating_controller.const import DOMAIN
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_coordinator import _hub_entry, _room_data, _seed_entities


def _register_stub_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        pass

    hass.services.async_register("climate", "set_temperature", handler)
    hass.services.async_register("switch", "turn_on", handler)
    hass.services.async_register("switch", "turn_off", handler)


async def _setup_entry(hass: HomeAssistant, room_name: str) -> MockConfigEntry:
    entry = _hub_entry(
        {**_room_data(hass), "room_name": room_name}, entry_id=f"hub-{room_name}"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def test_card_file_exists() -> None:
    """The module URL is useless if the file it points at is not shipped."""
    assert os.path.isfile(frontend._CARD_PATH)


async def test_setup_registers_card_module(hass: HomeAssistant) -> None:
    _seed_entities(hass)
    _register_stub_services(hass)
    await _setup_entry(hass, "Wohnzimmer")

    urls = [url for url in hass.data[DATA_EXTRA_MODULE_URL].urls if DOMAIN in url]
    assert urls == [frontend.card_url(frontend.frontend_hash())]


async def test_card_registered_once_for_multiple_rooms(hass: HomeAssistant) -> None:
    """One card registration must serve every configured room."""
    _seed_entities(hass)
    _register_stub_services(hass)
    await _setup_entry(hass, "Wohnzimmer")
    await _setup_entry(hass, "Esszimmer")

    urls = [url for url in hass.data[DATA_EXTRA_MODULE_URL].urls if DOMAIN in url]
    assert len(urls) == 1


async def test_card_is_served_without_auth_and_cached(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Revalidating on every load races Lovelace's first render.

    Serving from cache is what keeps the module defined before the dashboard
    renders; the cost is that a changed card needs a hard reload.
    """
    _seed_entities(hass)
    _register_stub_services(hass)
    await _setup_entry(hass, "Wohnzimmer")

    client = await hass_client_no_auth()
    response = await client.get(frontend.card_url(frontend.frontend_hash()))

    assert response.status == 200
    cache_control = response.headers["Cache-Control"]
    assert "no-cache" not in cache_control
    assert "max-age" in cache_control
    assert "customElements.define" in await response.text()


async def test_imported_modules_share_the_versioned_path(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A relative import ("./entities.js") must resolve under the same hash."""
    _seed_entities(hass)
    _register_stub_services(hass)
    await _setup_entry(hass, "Wohnzimmer")

    client = await hass_client_no_auth()
    version = frontend.frontend_hash()
    response = await client.get(f"{frontend.STATIC_URL_PATH}/{version}/entities.js")

    assert response.status == 200
    assert "export const managedDetailRows" in await response.text()


def test_hash_changes_with_any_module(tmp_path) -> None:
    """Changing a module the entry imports must move the whole card's URL."""
    (tmp_path / "heating-controller-card.js").write_text("import './entities.js';")
    (tmp_path / "entities.js").write_text("export const a = 1;")
    before = frontend.frontend_hash(str(tmp_path))

    (tmp_path / "entities.js").write_text("export const a = 2;")

    assert frontend.frontend_hash(str(tmp_path)) != before

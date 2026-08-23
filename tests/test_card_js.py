"""Logic tests for the shipped Lovelace card, executed in a real JS engine.

The card is plain ES2020 split across sibling modules, with no build tooling.
QuickJS has no module resolver, so the harness flattens the modules the same
way a bundler would: strip the import lines, drop the `export` keywords,
concatenate in dependency order. That only works because the modules are
side-effect free apart from the entry point, which is not loaded here.

Only DOM-free helpers are exercised — enough to pin the config round-trip,
where a silent data-loss bug lived, and the seeded detail rows.
"""

import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

from heating_controller import frontend

_FRONTEND_DIR = Path(frontend._FRONTEND_DIR)

_MODULES = [
    "translations.js",
    "const.js",
    "format.js",
    "config.js",
    "entities.js",
    "card-styles.js",
    "editor-styles.js",
    "controls.js",
    "card.js",
    "editor.js",
]

_IMPORT_RE = re.compile(
    r"^import\s+(?:(?P<names>\{[^}]*\})\s+from\s+)?[\"'](?P<source>[^\"']+)[\"'];\s*$",
    re.MULTILINE | re.DOTALL,
)

_DOM_STUBS = """
var window = globalThis;
var console = { info: function(){}, warn: function(){}, error: function(){} };
var customElements = { define: function(){}, get: function(){} };
var document = { createElement: function(){ return {}; } };
var HTMLElement = function(){};
HTMLElement.prototype = {};
var CustomEvent = function(){};
var setTimeout = function(){};
var clearTimeout = function(){};
"""


def _flatten(source: str) -> str:
    """Turn one ES module into plain script text."""
    without_imports = _IMPORT_RE.sub("", source)
    return re.sub(r"^export\s+", "", without_imports, flags=re.MULTILINE)


@pytest.fixture(scope="module")
def ctx() -> "quickjs.Context":
    context = quickjs.Context()
    context.eval(_DOM_STUBS)
    for name in _MODULES:
        context.eval(_flatten((_FRONTEND_DIR / name).read_text(encoding="utf-8")))
    return context


def _json(ctx, expression: str):
    import json

    return json.loads(ctx.eval(f"JSON.stringify({expression})"))


def _imports_of(path: Path) -> list[tuple[str, list[str]]]:
    """[(source file, [imported names]), ...] for one module."""
    found = []
    for match in _IMPORT_RE.finditer(path.read_text(encoding="utf-8")):
        names = match.group("names") or ""
        found.append(
            (
                match.group("source"),
                [n.strip() for n in names.strip("{} \n").split(",") if n.strip()],
            )
        )
    return found


def _exports_of(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8")
    return set(re.findall(r"^export\s+(?:const|class|function)\s+(\w+)", source, re.M))


def test_every_import_resolves_to_an_exported_symbol() -> None:
    """The browser fails loudly on a bad import; the QuickJS harness does not.

    It strips imports before evaluating, so a typo'd or removed export would
    still pass every other test in this file and only break in production.
    """
    problems = []
    for path in sorted(_FRONTEND_DIR.glob("*.js")):
        for source, names in _imports_of(path):
            target = _FRONTEND_DIR / source.removeprefix("./")
            if not target.is_file():
                problems.append(f"{path.name}: imports missing file {source}")
                continue
            missing = set(names) - _exports_of(target)
            if missing:
                problems.append(
                    f"{path.name}: {source} does not export {sorted(missing)}"
                )
    assert not problems, "\n".join(problems)


def test_flatten_order_covers_every_module() -> None:
    """A new module that nothing lists here would silently not be tested."""
    on_disk = {p.name for p in _FRONTEND_DIR.glob("*.js")}
    assert on_disk == set(_MODULES) | {frontend.CARD_FILENAME}


def test_translations_cover_the_same_keys(ctx) -> None:
    """A key present in one language and missing in another falls back
    silently to English, which reads as a bug rather than a gap."""
    english = set(_json(ctx, "Object.keys(TRANSLATIONS.en)"))
    german = set(_json(ctx, "Object.keys(TRANSLATIONS.de)"))
    assert english == german


def test_translation_falls_back_and_substitutes(ctx) -> None:
    assert ctx.eval('t({locale: {language: "de"}}, "section_details")') == "Details"
    assert (
        ctx.eval('t({locale: {language: "fr"}}, "status_blocked")') == "Blocked"
    ), "unknown language must fall back to English, not to the key"
    assert ctx.eval('t(null, "status_blocked")') == "Blocked"
    assert ctx.eval('t({}, "totally_unknown_key")') == "totally_unknown_key"
    assert (
        ctx.eval(
            't({locale: {language: "en"}}, "error_missing_entities", {missing: "mode"})'
        )
        == "This device is missing entities (mode)."
    )


def test_normalize_accepts_both_forms(ctx) -> None:
    result = _json(
        ctx,
        'normalizeItems(["sensor.a", {entity: "sensor.b", name: "B"}])',
    )
    assert result == [{"entity": "sensor.a"}, {"entity": "sensor.b", "name": "B"}]


def test_normalize_drops_broken_entries(ctx) -> None:
    """Configs damaged by an earlier version must heal, not render undefined."""
    result = _json(ctx, 'normalizeItems(["sensor.a", null, {}, undefined])')
    assert result == [{"entity": "sensor.a"}]


def test_compact_form_survives_a_second_round_trip(ctx) -> None:
    """The editor writes entries without name/icon back as bare strings.

    Mapping over that result again used to read .entity off a string, which is
    undefined — so every plain entry vanished on the next edit.
    """
    ctx.eval(
        """
        var compact = function (items) {
          return normalizeItems(items).map(function (item) {
            return !item.name && !item.icon ? item.entity : item;
          });
        };
        var once = compact(["sensor.a", {entity: "sensor.b", name: "B"}]);
        var twice = compact(once);
        """
    )
    assert _json(ctx, "once") == ["sensor.a", {"entity": "sensor.b", "name": "B"}]
    assert _json(ctx, "twice") == _json(ctx, "once")


def test_legacy_extra_entities_are_split_by_position(ctx) -> None:
    result = _json(
        ctx,
        """migrateLegacyConfig({
             device: "abc",
             extra_entities: [
               "sensor.detail",
               {entity: "sensor.head", position: "header"}
             ]
           })""",
    )
    assert result == {
        "device": "abc",
        "detail_entities": [{"entity": "sensor.detail"}],
        "header_entities": [{"entity": "sensor.head"}],
    }


def test_normalize_detail_keeps_key_and_entity_entries(ctx) -> None:
    result = _json(
        ctx,
        'normalizeDetail(["sensor.x", {key: "room_sensor"}, {entity: "sensor.y", name: "Y"}, {}, null])',
    )
    assert result == [
        {"entity": "sensor.x"},
        {"key": "room_sensor"},
        {"entity": "sensor.y", "name": "Y"},
    ]


_HASS_FIXTURE = """
var hass = {
  locale: { language: "en" },
  entities: {
    "sensor.katharina_raumtemperatur": {
      device_id: "dev", platform: "heating_controller",
      translation_key: "room_temperature", entity_id: "sensor.katharina_raumtemperatur",
    },
    "binary_sensor.katharina_heizautomatik": {
      device_id: "dev", platform: "heating_controller",
      translation_key: "heating_automation",
      entity_id: "binary_sensor.katharina_heizautomatik",
    },
    "sensor.katharina_heating_demand": {
      device_id: "dev", platform: "heating_controller",
      translation_key: "heating_demand", entity_id: "sensor.katharina_heating_demand",
    },
  },
  states: {
    "sensor.katharina_raumtemperatur": {
      entity_id: "sensor.katharina_raumtemperatur",
      state: "21.4",
      attributes: {
        used_strategy: "room_sensor", room_sensor_temp_c: 21.4,
        trv_temperatures: { "climate.gross": 21.1, "climate.klein": 21.2 },
      },
    },
    "binary_sensor.katharina_heizautomatik": {
      entity_id: "binary_sensor.katharina_heizautomatik",
      state: "on",
      attributes: {
        window_contact_entities: ["binary_sensor.fenster_a"],
      },
    },
    "sensor.katharina_heating_demand": {
      state: "0",
      attributes: { trv_target_temps: { "climate.gross": 5, "climate.klein": 5 } },
    },
    "climate.gross": { state: "heat", attributes: { friendly_name: "Heizung groß" } },
    "climate.klein": { state: "heat", attributes: { friendly_name: "Heizung klein" } },
    "binary_sensor.fenster_a": { state: "off", attributes: { friendly_name: "Fenster A" } },
  },
};
var roles = resolveRoles(hass, "dev");
"""


def test_resolve_roles_maps_by_translation_key(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    assert ctx.eval("roles.roomTemp.entity_id") == "sensor.katharina_raumtemperatur"
    assert ctx.eval("roles.automation.entity_id") == "binary_sensor.katharina_heizautomatik"


def test_managed_rows_seed_sensor_trvs_and_windows(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    keys = _json(ctx, "managedDetailRows(hass, roles).map(function(r){return r.key;})")
    assert keys == [
        "room_sensor",
        "trv:climate.gross",
        "trv:climate.klein",
        "window:binary_sensor.fenster_a",
    ]


def test_managed_row_defaults(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    rows = _json(ctx, "managedDetailRows(hass, roles)")
    by_key = {r["key"]: r for r in rows}
    assert by_key["room_sensor"]["value"] == "21.4 °C"
    assert by_key["room_sensor"]["name"] == "Sensor"
    assert by_key["trv:climate.gross"]["name"] == "Heizung groß"
    assert by_key["trv:climate.gross"]["value"] == "21.1 °C → 5 °C"
    assert by_key["window:binary_sensor.fenster_a"]["value"] == "Closed"


def test_managed_rows_follow_the_language(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval('hass.locale.language = "de";')
    rows = _json(ctx, "managedDetailRows(hass, roles)")
    by_key = {r["key"]: r for r in rows}
    assert by_key["window:binary_sensor.fenster_a"]["value"] == "Geschlossen"
    ctx.eval('hass.locale.language = "en";')


def test_managed_header_rows_seed_the_room_temperature(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    rows = _json(ctx, "managedHeaderRows(hass, roles)")
    assert rows == [
        {
            "key": "room_temp",
            "entity": "sensor.katharina_raumtemperatur",
            "label": "Room temperature",
            "icon": "mdi:thermometer",
        }
    ]


def test_managed_header_rows_empty_without_a_room_temp_role(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval("var noRoomTemp = {};")
    assert _json(ctx, "managedHeaderRows(hass, noRoomTemp)") == []


def test_managed_header_rows_follow_the_language(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval('hass.locale.language = "de";')
    rows = _json(ctx, "managedHeaderRows(hass, roles)")
    assert rows[0]["label"] == "Raumtemperatur"
    ctx.eval('hass.locale.language = "en";')


@pytest.mark.parametrize(
    ("status", "value", "verdict"),
    [
        ("no_requirement", "No requirement", ""),
        ("below_threshold", "Below operating threshold", "idle"),
        ("source_inactive", "Source inactive", "idle"),
        ("undersupplied", "34.5 °C ⚠", "low"),
        ("sufficient", "34.5 °C", "ok"),
    ],
)
def test_supply_presentation(ctx, status: str, value: str, verdict: str) -> None:
    """Three of the five states must read as words: a degree figure there would
    claim the source has to hit that temperature, which is not true of them."""
    result = _json(
        ctx,
        'supplyPresentation({locale: {language: "en"}}, '
        f'{{state: "34.5", attributes: {{supply_status: "{status}"}}}})',
    )
    assert result["value"] == value
    assert result["verdict"] == verdict
    assert result["hint"]


def test_supply_presentation_survives_an_unknown_status(ctx) -> None:
    """A future backend state must degrade to the bare number, not to nothing."""
    result = _json(
        ctx,
        'supplyPresentation({locale: {language: "en"}}, '
        '{state: "34.5", attributes: {supply_status: "brand_new"}})',
    )
    assert result["value"] == "34.5 °C"
    assert result["verdict"] == ""
    assert "hint" not in result


def test_number_formatting_drops_trailing_zeros(ctx) -> None:
    """`digits` is a maximum, not a fixed width -- "21 °C", not "21.0 °C"."""
    assert ctx.eval("num(21.0)") == "21"
    assert ctx.eval("num(21.45)") == "21.5"
    assert ctx.eval('num("-1.0")') == "-1"
    assert ctx.eval("num(640, 0)") == "640"


def test_number_formatting_survives_bad_input(ctx) -> None:
    assert ctx.eval('num("unavailable")') == "–"
    assert ctx.eval("num(undefined)") == "–"


_BUTTON_STUB = """
var makeButtonStub = function () {
  var el = { disabled: false, innerHTML: "", _class: "", _listeners: {} };
  Object.defineProperty(el, "className", {
    get: function () { return this._class; },
    set: function (v) { this._class = v; },
  });
  el.addEventListener = function (type, fn) { this._listeners[type] = fn; };
  return el;
};
"""


def test_boost_button_selects_boost_when_inactive(ctx) -> None:
    ctx.eval(_BUTTON_STUB)
    ctx.eval(
        """
        var calls = [];
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeButtonStub(); };
        var btn = boostButton({}, false, { selectMode: function (m) { calls.push(m); } });
        document.createElement = realCreateElement;
        btn._listeners.click({ stopPropagation: function () {} });
        """
    )
    assert ctx.eval("btn.disabled") is False
    assert _json(ctx, "calls") == ["boost"]


_ELEMENT_STUB = """
var makeElementStub = function () {
  var el = { disabled: false, textContent: "", innerHTML: "", _class: "", children: [],
             style: { setProperty: function () {} }, _listeners: {} };
  Object.defineProperty(el, "className", {
    get: function () { return this._class; },
    set: function (v) { this._class = v; },
  });
  el.addEventListener = function (type, fn) { this._listeners[type] = fn; };
  el.appendChild = function (child) { this.children.push(child); return child; };
  el.append = function () {
    for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]);
  };
  return el;
};
"""


def test_comfort_condition_toggle_missing_entity_returns_null(ctx) -> None:
    ctx.eval(_ELEMENT_STUB)
    ctx.eval(
        """
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeElementStub(); };
        var result = comfortConditionToggle(
          { states: {} }, "switch.missing", { setBoolean: function () {}, moreInfo: function () {} }
        );
        document.createElement = realCreateElement;
        """
    )
    assert ctx.eval("result") is None


def test_comfort_condition_toggle_writable_sets_explicit_state(ctx) -> None:
    """A segmented An/Aus control must set the state its label promises, not
    toggle -- clicking An while already on must still be a no-op-safe 'on'."""
    ctx.eval(_ELEMENT_STUB)
    ctx.eval(
        """
        var calls = [];
        var hass = {
          locale: { language: "en" },
          states: { "switch.desk": { state: "on", attributes: { friendly_name: "Desk plug" } } },
        };
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeElementStub(); };
        var row = comfortConditionToggle(hass, "switch.desk", {
          setBoolean: function (id, on) { calls.push([id, on]); },
          moreInfo: function () {},
        });
        document.createElement = realCreateElement;
        var onButton = row.children[0];
        var offButton = row.children[1];
        """
    )
    assert ctx.eval("onButton._class") == "segment active"
    assert ctx.eval("offButton._class") == "segment"
    ctx.eval('onButton._listeners.click({ stopPropagation: function () {} });')
    ctx.eval('offButton._listeners.click({ stopPropagation: function () {} });')
    assert _json(ctx, "calls") == [["switch.desk", True], ["switch.desk", False]]


def test_comfort_condition_toggle_uses_editor_overrides(ctx) -> None:
    """The editor lets a user override the generic An/Aus label and icon per
    condition -- those overrides must win over the translated default."""
    ctx.eval(_ELEMENT_STUB)
    ctx.eval(
        """
        var hass = {
          locale: { language: "en" },
          states: { "switch.desk": { state: "off", attributes: {} } },
        };
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeElementStub(); };
        var row = comfortConditionToggle(
          hass,
          {
            entity: "switch.desk", icon_on: "mdi:briefcase", icon_off: "mdi:home",
            label_on: "Homeoffice", label_off: "Zuhause",
          },
          { setBoolean: function () {}, moreInfo: function () {} }
        );
        document.createElement = realCreateElement;
        """
    )
    assert ctx.eval("row.children[0].innerHTML") == '<ha-icon icon="mdi:briefcase"></ha-icon>Homeoffice'
    assert ctx.eval("row.children[1].innerHTML") == '<ha-icon icon="mdi:home"></ha-icon>Zuhause'
    assert ctx.eval("row.children[1]._class") == "segment active"


def test_comfort_condition_toggle_read_only_shows_single_active_state(ctx) -> None:
    """A non-writable condition (e.g. a schedule or template binary_sensor)
    offers no choice, so it must not render both options -- only whichever
    state currently applies. Styled as a header-value tile, not the
    segmented/boost pill shape, so it never reads as something you can tap
    to change."""
    ctx.eval(_ELEMENT_STUB)
    ctx.eval(
        """
        var seen = [];
        var hass = {
          locale: { language: "en" },
          states: { "binary_sensor.homeoffice": { state: "on", attributes: {} } },
        };
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeElementStub(); };
        var el = comfortConditionToggle(hass, "binary_sensor.homeoffice", {
          setBoolean: function () { throw new Error("must not be called"); },
          moreInfo: function (id) { seen.push(id); },
        });
        document.createElement = realCreateElement;
        """
    )
    assert ctx.eval("el._class") == "value condition-indicator"
    assert ctx.eval("el.children.length") == 0
    assert ctx.eval("el.innerHTML") == '<span class="num">On</span>'
    ctx.eval('el._listeners.click({ stopPropagation: function () {} });')
    assert _json(ctx, "seen") == ["binary_sensor.homeoffice"]


def test_comfort_condition_toggle_read_only_reflects_off_state(ctx) -> None:
    ctx.eval(_ELEMENT_STUB)
    ctx.eval(
        """
        var hass = {
          locale: { language: "en" },
          states: { "binary_sensor.homeoffice": { state: "off", attributes: {} } },
        };
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeElementStub(); };
        var el = comfortConditionToggle(
          hass,
          { entity: "binary_sensor.homeoffice", label_on: "Homeoffice", label_off: "Zuhause",
            icon_on: "mdi:briefcase", icon_off: "mdi:home" },
          { setBoolean: function () {}, moreInfo: function () {} }
        );
        document.createElement = realCreateElement;
        """
    )
    assert ctx.eval("el.innerHTML") == '<ha-icon icon="mdi:home"></ha-icon><span class="num">Zuhause</span>'


def test_boost_button_has_no_click_handler_when_active(ctx) -> None:
    """Active boost has exactly one way out (Entblocken) -- the button itself
    must not offer a second, competing toggle-off."""
    ctx.eval(_BUTTON_STUB)
    ctx.eval(
        """
        var calls = [];
        var realCreateElement = document.createElement;
        document.createElement = function () { return makeButtonStub(); };
        var btn = boostButton({}, true, { selectMode: function (m) { calls.push(m); } });
        document.createElement = realCreateElement;
        """
    )
    assert ctx.eval("btn.disabled") is True
    assert ctx.eval('typeof btn._listeners.click') == "undefined"

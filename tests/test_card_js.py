"""Logic tests for the shipped Lovelace card, executed in a real JS engine.

The card is plain ES2020 split across sibling modules, with no build tooling.
QuickJS has no module resolver, so the harness flattens the modules the same
way a bundler would: strip the import lines, drop the `export` keywords,
concatenate in dependency order. That only works because the modules are
side-effect free apart from the entry point, which is not loaded here.

Only DOM-free helpers are exercised — enough to pin the config round-trip,
where a silent data-loss bug lived, and the seeded detail rows.
"""

import json
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
    "curve.js",
    "curve-card.js",
    "rooms.js",
    "rooms-card.js",
    "flow.js",
    "flow-card.js",
    "season.js",
    "season-card.js",
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
def ctx() -> quickjs.Context:
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
    return set(
        re.findall(r"^export\s+(?:const|class|function)\s+(\w+)", source, re.MULTILINE)
    )


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
    assert ctx.eval('t({locale: {language: "fr"}}, "status_blocked")') == "Blocked", (
        "unknown language must fall back to English, not to the key"
    )
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
    assert (
        ctx.eval("roles.automation.entity_id")
        == "binary_sensor.katharina_heizautomatik"
    )


def test_radiator_alert_is_quiet_without_a_cold_radiator(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    assert ctx.eval("radiatorAlert(hass, roles)") is None


def test_radiator_alert_names_the_cold_trvs(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval(
        """
hass.entities["binary_sensor.katharina_heizkorper_heizt_nicht"] = {
  entity_id: "binary_sensor.katharina_heizkorper_heizt_nicht",
  device_id: "dev", platform: "heating_controller",
  translation_key: "radiator_not_heating",
};
hass.states["binary_sensor.katharina_heizkorper_heizt_nicht"] = {
  entity_id: "binary_sensor.katharina_heizkorper_heizt_nicht",
  state: "on",
  attributes: { trv_entities: ["climate.klein"] },
};
var alertRoles = resolveRoles(hass, "dev");
var alert = radiatorAlert(hass, alertRoles);
"""
    )
    assert (
        ctx.eval("alert.entityId") == "binary_sensor.katharina_heizkorper_heizt_nicht"
    )
    assert ctx.eval("alert.text") == (
        "Radiator not heating – check or bleed it: Heizung klein"
    )


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


def test_trv_rows_show_the_valve_opening(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval(
        'hass.states["sensor.katharina_heating_demand"].attributes.trv_valve_positions'
        ' = { "climate.gross": 34.4, "climate.klein": null };'
    )
    rows = _json(ctx, "managedDetailRows(hass, roles)")
    by_key = {r["key"]: r for r in rows}
    assert by_key["trv:climate.gross"]["value"] == "21.1 °C → 5 °C · 34 %"
    assert by_key["trv:climate.klein"]["value"] == "21.2 °C → 5 °C"
    ctx.eval(
        'delete hass.states["sensor.katharina_heating_demand"].attributes.trv_valve_positions;'
    )


_ADDITIONAL_HEAT = """
hass.entities["sensor.katharina_zusatzwarme"] = {
  entity_id: "sensor.katharina_zusatzwarme",
  device_id: "dev", platform: "heating_controller",
  translation_key: "additional_heat",
};
hass.states["sensor.katharina_zusatzwarme"] = {
  entity_id: "sensor.katharina_zusatzwarme",
  state: "%s",
  attributes: { ungated_w: %s },
};
var heatRoles = resolveRoles(hass, "dev");
var heatRow = managedDetailRows(hass, heatRoles).filter(
  function(r){ return r.key === "additional_heat"; }
)[0] || null;
"""


def test_additional_heat_row_shows_the_used_gain(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval(_ADDITIONAL_HEAT % ("120.4", "120.4"))
    row = _json(ctx, "heatRow")
    assert row["entity"] == "sensor.katharina_zusatzwarme"
    assert row["name"] == "Additional heat"
    assert row["value"] == "120 W"


def test_additional_heat_row_names_what_is_still_learning(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval('hass.locale.language = "de";')
    ctx.eval(_ADDITIONAL_HEAT % ("0", "85.2"))
    row = _json(ctx, "heatRow")
    assert row["name"] == "Zusatzwärme"
    assert row["value"] == "0 W (lernt: 85 W)"
    ctx.eval('hass.locale.language = "en";')


def test_additional_heat_row_hidden_while_unknown(ctx) -> None:
    ctx.eval(_HASS_FIXTURE)
    ctx.eval(_ADDITIONAL_HEAT % ("unknown", "null"))
    assert ctx.eval("heatRow") is None


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
    ("status", "attributes", "value", "verdict"),
    [
        ("undersupplied", "{demand_pct: 100}", "Short · 100 % ⚠", "low"),
        ("supplied", "{demand_pct: 85}", "At limit · 85 %", "ok"),
        ("oversupplied", "{demand_pct: 30}", "Headroom · 30 %", "idle"),
        (
            "not_rated",
            '{reason: "window_open", demand_pct: null}',
            "Not rated · window open",
            "",
        ),
    ],
)
def test_supply_presentation(
    ctx, status: str, attributes: str, value: str, verdict: str
) -> None:
    """Rated: state and valve opening; not rated: state and why."""
    result = _json(
        ctx,
        'supplyPresentation({locale: {language: "en"}}, '
        f'{{state: "{status}", attributes: {attributes}}})',
    )
    assert result["value"] == value
    assert result["verdict"] == verdict
    assert result["hint"]


def test_supply_presentation_survives_an_unknown_status(ctx) -> None:
    """A future backend state must degrade to the bare state, not to nothing."""
    result = _json(
        ctx,
        'supplyPresentation({locale: {language: "en"}}, '
        '{state: "brand_new", attributes: {}})',
    )
    assert result["value"] == "brand_new"
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


def test_entity_value_rounds_numbers_with_their_unit(ctx) -> None:
    assert (
        ctx.eval(
            'entityValue({}, {state: "39", attributes: {unit_of_measurement: "%"}})'
        )
        == "39 %"
    )
    assert (
        ctx.eval(
            'entityValue({}, {state: "11.27", attributes: {unit_of_measurement: "°C"}})'
        )
        == "11.3 °C"
    )


def test_entity_value_shows_text_states_instead_of_a_dash(ctx) -> None:
    """An enum sensor (Danfoss adaptation status) used to render as "–"."""
    assert ctx.eval('entityValue({}, {state: "found", attributes: {}})') == "found"
    assert (
        ctx.eval(
            """entityValue(
                 {formatEntityState: function (s) { return "HA:" + s.state; }},
                 {state: "found", attributes: {}}
               )"""
        )
        == "HA:found"
    )


def test_entity_value_relabels_through_state_labels(ctx) -> None:
    labels = '{found: "Gefunden", none: "Keine"}'
    assert (
        ctx.eval(f'entityValue({{}}, {{state: "found", attributes: {{}}}}, {labels})')
        == "Gefunden"
    )
    # A state the map doesn't cover falls back to the plain text.
    assert (
        ctx.eval(f'entityValue({{}}, {{state: "lost", attributes: {{}}}}, {labels})')
        == "lost"
    )


def test_entity_value_keeps_a_dash_for_unavailable(ctx) -> None:
    assert (
        ctx.eval(
            'entityValue({}, {state: "unavailable", attributes: {unit_of_measurement: "%"}})'
        )
        == "– %"
    )


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
    ctx.eval("onButton._listeners.click({ stopPropagation: function () {} });")
    ctx.eval("offButton._listeners.click({ stopPropagation: function () {} });")
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
    assert (
        ctx.eval("row.children[0].innerHTML")
        == '<ha-icon icon="mdi:briefcase"></ha-icon>Homeoffice'
    )
    assert (
        ctx.eval("row.children[1].innerHTML")
        == '<ha-icon icon="mdi:home"></ha-icon>Zuhause'
    )
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
    ctx.eval("el._listeners.click({ stopPropagation: function () {} });")
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
    assert (
        ctx.eval("el.innerHTML")
        == '<ha-icon icon="mdi:home"></ha-icon><span class="num">Zuhause</span>'
    )


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
    assert ctx.eval("typeof btn._listeners.click") == "undefined"


_CURVE = (
    "{outdoorCold: -13, outdoorWarm: 18, flowCold: 50, flowWarm: 29,"
    " bump: 2, bumpWidth: 4}"
)


def test_curve_card_follows_the_python_heating_curve(ctx) -> None:
    """The card draws the heat source's curve itself; it must agree with
    HeatingCurve.flow_at, flat ends and bump included."""
    from heating_controller.control.flow import HeatingCurve

    curve = HeatingCurve(
        outdoor_cold_c=-13,
        outdoor_warm_c=18,
        flow_cold_c=50,
        flow_warm_c=29,
        bump_k=2,
        bump_width_k=4,
    )
    for outdoor in (-20, -13, -5, -1.5, 0, 1, 2, 7, 18, 25):
        assert ctx.eval(f"curveFlowAt({_CURVE}, {outdoor})") == curve.flow_at(outdoor)


def test_curve_card_recommendation_takes_the_highest_lead_room(ctx) -> None:
    rooms = (
        "[{name: 'A', lead: true, flow_c: [40, 30]},"
        " {name: 'B', lead: true, flow_c: [42, null]},"
        " {name: 'C', lead: false, flow_c: [50, 35]}]"
    )
    assert _json(ctx, f"recommendationSeries({rooms}, 1)") == [43, 31]
    assert (
        _json(ctx, "recommendationSeries([{name: 'C', lead: false, flow_c: [1]}], 1)")
        == []
    )
    # The envelope over any room choice, lead or not, for the trial curve's ends.
    assert _json(ctx, f"envelopeSeries({rooms}, 1)") == [51, 36]
    assert _json(ctx, "envelopeSeries([], 1)") == []


def test_curve_card_tightest_point_stays_inside_the_curve(ctx) -> None:
    result = _json(
        ctx,
        "tightestPoint([-14, -13, 0, 18], [50, 50, 43, 29], [60, 45, 44, 30],"
        f" {_CURVE})",
    )
    # -14 lies beyond the curve's cold end and does not count.
    assert result == {"reserve": -1, "outdoor": 0}


def test_curve_card_covering_ends_leave_no_deficit(ctx) -> None:
    # A recommendation that bulges up to 4.8 K above the straight line
    # between its values at the curve's ends (47 at -13, 30 at 18).
    ctx.eval(
        "var COVER_X = []; var COVER_REC = [];"
        " for (var x = -15; x <= 20; x++) { COVER_X.push(x);"
        " COVER_REC.push(Math.round((47 - 17 * (x + 13) / 31"
        " + 0.02 * (x + 13) * (18 - x)) * 10) / 10); }"
    )
    ends = _json(ctx, f"coveringEnds({_CURVE}, COVER_X, COVER_REC)")
    # The ends alone would give 47 and 30; the bulge lifts both.
    assert ends == {"flowCold": 52, "flowWarm": 35}
    tight = _json(
        ctx,
        f"tightestPoint(COVER_X, curveSeries(Object.assign({{}}, {_CURVE}, "
        f"{json.dumps(ends)}), COVER_X), COVER_REC, {_CURVE})",
    )
    assert tight["reserve"] >= 0
    assert _json(ctx, f"coveringEnds({_CURVE}, [0, 1], [40, 39])") is None


def test_curve_card_escapes_room_names(ctx) -> None:
    assert ctx.eval("escapeHtml('<b>\"A&B\"</b>')") == (
        "&lt;b&gt;&quot;A&amp;B&quot;&lt;/b&gt;"
    )


_ROOMS_HASS = """({
  floors: {
    og: {name: 'Obergeschoss', level: 1},
    eg: {name: 'Erdgeschoss', level: 0},
  },
  areas: {a1: {floor_id: 'eg'}, a2: {floor_id: 'eg'}, a3: {floor_id: 'og'}, a4: {}},
  devices: {
    d1: {name: 'Wohnzimmer', area_id: 'a1'},
    d2: {name: 'Esszimmer', name_by_user: 'Essen', area_id: 'a2'},
    d3: {name: 'Bad', area_id: 'a3'},
    d4: {name: 'Werkstatt', area_id: 'a4'},
    d5: {name: 'Fremd'},
  },
  entities: {
    'select.a': {platform: 'heating_controller', translation_key: 'heating_mode', device_id: 'd1'},
    'select.b': {platform: 'heating_controller', translation_key: 'heating_mode', device_id: 'd2'},
    'select.c': {platform: 'heating_controller', translation_key: 'heating_mode', device_id: 'd3'},
    'select.d': {platform: 'heating_controller', translation_key: 'heating_mode', device_id: 'd4'},
    'select.e': {platform: 'other', translation_key: 'heating_mode', device_id: 'd5'},
    'sensor.f': {platform: 'heating_controller', translation_key: 'supply', device_id: 'd1'},
  },
})"""


def test_rooms_card_finds_rooms_by_their_mode_entity(ctx) -> None:
    assert _json(ctx, f"roomDeviceIds({_ROOMS_HASS})") == ["d1", "d2", "d3", "d4"]


def test_rooms_card_groups_by_floor_level_then_configured_order(ctx) -> None:
    groups = _json(
        ctx,
        f"groupRooms({_ROOMS_HASS}, ['d3', 'd4', 'd1', 'd2'], null,"
        " ['Wohnzimmer', 'Essen'])",
    )
    assert [(g["name"], [r["name"] for r in g["rooms"]]) for g in groups] == [
        ("Erdgeschoss", ["Wohnzimmer", "Essen"]),
        ("Obergeschoss", ["Bad"]),
        ("", ["Werkstatt"]),
    ]
    # The configured order (by name or device id) wins over the integration's.
    groups = _json(
        ctx,
        f"groupRooms({_ROOMS_HASS}, ['d1', 'd2'], ['d2'], ['Wohnzimmer', 'Essen'])",
    )
    assert [r["id"] for r in groups[0]["rooms"]] == ["d2", "d1"]


def test_rooms_card_shortens_trv_names_to_what_differs(ctx) -> None:
    assert _json(
        ctx,
        "trvLabels({a: 'Heizung_Wohnzimmergross', b: 'Heizung_Wohnzimmerklein'})",
    ) == {"a": "Gross", "b": "Klein"}
    assert _json(ctx, "trvLabels({a: 'Heizung_Bad'})") == {"a": ""}


def test_rooms_card_judges_learning_states(ctx) -> None:
    assert ctx.eval("learningJudgement('ua', 'learned')") == "ok"
    assert ctx.eval("learningJudgement('ua', 'drift')") == "attention"
    assert ctx.eval("learningJudgement('capacity', 'disturbed')") == "attention"
    assert ctx.eval("learningJudgement('capacity', 'collecting')") == ""


_ROOMS_RENDER_SETUP = """
HTMLElement.prototype.attachShadow = function () {
  this.shadowRoot = { innerHTML: '', querySelectorAll: function () { return []; } };
  return this.shadowRoot;
};
var E = {}, S = {}, DEV = {flow: {name: 'Vorlauf'}};
function add(device, key, id, state, attributes) {
  E[id] = {entity_id: id, platform: 'heating_controller', translation_key: key, device_id: device};
  S[id] = {entity_id: id, state: String(state), attributes: attributes || {}};
}
function room(d, name, area, o) {
  DEV[d] = {name: name, area_id: area};
  add(d, 'heating_mode', 'select.' + d, o.mode);
  add(d, 'heating_automation', 'switch.' + d, 'on', {window_contact_entities: ['binary_sensor.' + d + '_win']});
  S['binary_sensor.' + d + '_win'] = {entity_id: 'binary_sensor.' + d + '_win', state: o.win ? 'on' : 'off', attributes: {}};
  add(d, 'room_temperature', 'sensor.' + d + '_t', o.t);
  add(d, 'supply', 'sensor.' + d + '_supply', o.supply, {demand_pct: o.dem, reason: o.reason});
  var valves = {}, targets = {};
  o.valves.forEach(function (v, i) {
    var id = 'climate.' + d + i;
    valves[id] = v; targets[id] = o.soll;
    S[id] = {entity_id: id, state: 'heat', attributes: {friendly_name: 'Heizung_' + name + ['gross', 'klein'][i]}};
  });
  add(d, 'heating_demand', 'sensor.' + d + '_dem', o.dem, {trv_valve_positions: valves, trv_target_temps: targets});
  add(d, 'gated_hold_flow_temperature', 'sensor.' + d + '_gh', o.gh, {flow_gate_closed: !!o.gate, calculation_target_temperature_c: o.soll});
  add(d, 'hold_flow_temperature', 'sensor.' + d + '_h', o.h);
  add(d, 'boost_flow_temperature', 'sensor.' + d + '_b', o.b, {boost_active: false});
  add(d, 'radiator_not_heating', 'binary_sensor.' + d + '_rnh', o.rnh ? 'on' : 'off');
  add(d, 'ua_learning_status', 'sensor.' + d + '_uas', o.uas, {learned_gain_w: 120.4, gain_sigma_w: 31, gain_determined: true});
  add(d, 'ua_factor', 'sensor.' + d + '_uaf', 0.523);
  add(d, 'capacity_learning_status', 'sensor.' + d + '_cs', 'collecting');
  add(d, 'capacity_factor', 'sensor.' + d + '_cf', 1.0412);
  add(d, 'additional_heat', 'sensor.' + d + '_ah', 85,
      {profile_determined: [true, false], sun_determined: {s: true}, residual_rms_w: 44});
}
room('wz', 'Wohnzimmer', 'a1', {mode: 'comfort', t: 21.2, soll: 21.5, supply: 'undersupplied', dem: 92, gh: 41.3, h: 41.3, b: 0, valves: [95, 88], uas: 'learned'});
room('ka', 'Katharina', 'a2', {mode: 'eco', t: 20.8, soll: 21, supply: 'not_rated', reason: 'window_open', dem: 0, gh: 30, h: 33.4, b: 44.2, valves: [20], uas: 'drift', gate: true, win: true, rnh: true});
add('flow', 'hold_flow_curves', 'sensor.curves', 'ok', {rooms: [{name: 'Wohnzimmer', lead: true}, {name: 'Katharina', lead: false}]});
add('flow', 'flow_target', 'sensor.soll', 41.3, {source: 'lead', room: 'Wohnzimmer'});
add('flow', 'flow_feedback', 'sensor.fb', 3, {lead_room: 'Katharina'});
var ROOMS_HASS = {
  entities: E, states: S, devices: DEV,
  areas: {a1: {floor_id: 'eg'}, a2: {floor_id: 'og'}},
  floors: {eg: {name: 'Erdgeschoss', level: 0}, og: {name: 'Obergeschoss', level: 1}},
  locale: {language: 'de'},
  formatEntityState: function (s) { return s.state; },
};
function renderRooms(view) {
  var card = new HeatingControllerRoomsCard();
  card.setConfig({views: [view]});
  card.hass = ROOMS_HASS;
  return card.shadowRoot.innerHTML;
}
"""


@pytest.mark.parametrize("view", ["supply", "radiators", "learning"])
def test_rooms_card_renders_every_view(ctx, view) -> None:
    ctx.eval(_ROOMS_RENDER_SETUP)
    html = ctx.eval(f"renderRooms('{view}')")
    assert "Erdgeschoss" in html and "Obergeschoss" in html
    assert html.index("Wohnzimmer") < html.index("Katharina")
    for broken in ("undefined", "NaN", "null"):
        assert broken not in html
    # Narrow cards stack each room into a tile captioned by the column names.
    assert f'<table class="view-{view} floor-tabbed">' in html
    # Floor tabs for the stacked layout; the first floor is picked, the other hidden there.
    assert 'data-floor="eg" aria-pressed="true">Erdgeschoss<' in html
    assert 'data-floor="og" aria-pressed="false">Obergeschoss<' in html
    assert html.count('class="other-floor"') == 1
    assert f"table.view-{view} tr.other-floor" in html
    assert f"table.view-{view} thead {{ display: none; }}" in html
    assert f"table.view-{view} td:empty {{ display: none; }}" in html
    assert 'data-label="Raum"' in html
    # The feedback marker sits behind the room name, not in a value column.
    feedback = html.index("mdi:arrow-up-down")
    assert html.index("Katharina") < feedback < html.index("</td>", feedback)
    assert ">Rückkopplung<" in html
    # The lead room that sets the flow gets its own marker behind the name.
    lead = html.index("mdi:thermometer-chevron-up")
    assert html.index("Wohnzimmer") < lead < html.index("</td>", lead)
    assert ">Leitraum<" in html
    if view == "supply":
        assert 'class="chip undersupplied">Zu knapp 92 %' in html
        assert 'class="flow-room"' in html and 'class="muted"' in html
        assert ">44.2\u00a0°C<" in html or ">44,2\u00a0°C<" in html
    elif view == "radiators":
        assert ">Gross<" in html and ">Klein<" in html
        # Two TRVs: one tile each, captioned by their labels; one TRV: "Ventil".
        assert 'class="multi-valve"' in html
        assert 'data-label="Ventil"' in html
        assert "mdi:window-open-variant" in html and "mdi:radiator-off" in html
    else:
        assert "✓ learned" in html and "⚠ drift" in html
        assert "\u00a0W<" in html
        assert "✓ / 1/2 / 1/1" in html


_FLOW_RENDER_SETUP = """
HTMLElement.prototype.attachShadow = function () {
  this.shadowRoot = { innerHTML: '', activeElement: null,
    querySelectorAll: function () { return []; }, querySelector: function () { return null; } };
  return this.shadowRoot;
};
var FE = {}, FS = {};
function fadd(id, key, state, attributes) {
  FE[id] = {entity_id: id, platform: 'heating_controller', translation_key: key, device_id: 'flow'};
  FS[id] = {entity_id: id, state: String(state), attributes: attributes || {}};
}
fadd('sensor.soll', 'flow_target', 41.5, {source: 'lead', room: 'Rebekka', feedforward_flow_c: 38.5,
  feedback_k: 3.0, threshold_c: 31, measured_correction_k: 0, measured_correction_frozen_reason: 'settling',
  boost_flow_c: null, reason: ''});
fadd('sensor.fb', 'flow_feedback', 3.0, {flow_timer_min: -22, step_up_at_min: -30, step_down_at_min: 60,
  lead_room: 'Rebekka', lead_demand_pct: 100, lead_deficit_c: 0.29, locked_direction: 1, night: false,
  frozen: false, last_change: '2026-11-22T10:41:11+00:00', room_variant_up: 59, room_variant_down: 8});
fadd('sensor.kurve', 'flow_curve', 39.1);
fadd('sensor.ist', 'flow_actuator_actual', 2);
fadd('sensor.stell', 'flow_actuator_target', 3, {last_error: null});
fadd('sensor.curves', 'hold_flow_curves', 'ok', {outdoor_temperature_c: null});
fadd('switch.write', 'flow_write', 'on');
fadd('button.reset', 'flow_feedback_reset', 'unknown');
fadd('binary_sensor.reach', 'heat_source_reachable', 'off');
var FLOW_HASS = {entities: FE, states: FS, locale: {language: 'de'},
  formatEntityState: function (s) { return s.state; }};
function renderFlow() {
  var card = new HeatingControllerFlowCard();
  card.setConfig({});
  card.hass = FLOW_HASS;
  return card.shadowRoot.innerHTML;
}
"""


def test_flow_card_renders(ctx) -> None:
    ctx.eval(_FLOW_RENDER_SETUP)
    html = ctx.eval("renderFlow()")
    for broken in ("undefined", "NaN", "null", "flow_"):
        assert broken not in html.replace("heating-controller-flow", "")
    assert "Wärmeerzeuger nicht erreichbar" in html and "Außentemperatur fehlt" in html
    assert '<span>Leitraum</span><span class="rooms">Rebekka</span>' in html
    FS = "FLOW_HASS.states['sensor.curves'].attributes"
    ctx.eval(
        FS
        + ".rooms = [{name: 'Wohnzimmer', lead: true}, {name: 'Esszimmer', lead: false},"
        " {name: 'Rebekka', lead: true}]"
    )
    assert (
        '<span>Leiträume</span><span class="rooms">Wohnzimmer, <b>Rebekka</b></span>'
        in ctx.eval("renderFlow()")
    )
    ctx.eval("delete " + FS + ".rooms")
    assert "Korrektur pausiert: Wärmeerzeuger stellt ein" in html
    assert "0.29 K unter Soll" in html
    # The lock up hatches the right side and replaces its step text.
    assert (
        '<span class="lock" style="left:' + str(60 / 90 * 100) + '%;right:0">' in html
    )
    assert '<ha-icon icon="mdi:lock"></ha-icon> gesperrt</span>' in html
    assert "Schritt hoch bei" not in html and "mdi:weather-night" not in html
    # -22 min on a bar from +60 (left) to -30 (right).
    assert "left:" + str(82 / 90 * 100) + "%" in html
    # The count and the steps without sign: the bar's side shows the direction.
    assert "<span>22 min</span>" in html and "Schritt runter bei 60 min" in html
    FB = "FLOW_HASS.states['sensor.fb'].attributes"
    ctx.eval(
        FB
        + ".locked_direction = -1; "
        + FB
        + ".night = true; "
        + FB
        + ".locked_until = new Date(Date.now() + 25 * 60000 - 1000).toISOString()"
    )
    html_down = ctx.eval("renderFlow()")
    assert '<span class="lock" style="left:0;right:' in html_down
    assert (
        "gesperrt noch 25 min" in html_down and "Schritt hoch bei 30 min" in html_down
    )
    assert "Schritt runter bei" not in html_down
    assert 'icon="mdi:weather-night" title="Nachtzeiten"' in html_down
    ctx.eval(
        FB
        + ".locked_direction = 1; "
        + FB
        + ".night = false; delete "
        + FB
        + ".locked_until"
    )
    assert "Leitraum Esszimmer" not in html and "Rückkopplung</h3>" in html
    # The feedback block sits between the two chains.
    assert (
        html.index("Rückkopplung</h3>")
        < html.index("Steuerung</h3>")
        < html.index(">Kurve<")
    )
    # Control: curve + correction = base; target − base = need → offset.
    assert (
        ">Basis</span><span>39.1\u00a0°C<" in html
        and ">Bedarf</span><span>+2.4\u00a0K<" in html
    )
    assert (
        'class="term pending" data-entity="sensor.stell"' in html
        and ">Offset · Ist +2.0\u00a0K</span><span>+3.0\u00a0K<" in html
    )
    assert "Effektiv" not in html and "Reserve" not in html
    ctx.eval("FLOW_HASS.states['sensor.ist'].state = '3'")
    html_ok = ctx.eval("renderFlow()")
    assert (
        'class="term ok" data-entity="sensor.stell"' in html_ok
        and "Offset · Ist" not in html_ok
    )
    ctx.eval("FLOW_HASS.states['sensor.ist'].state = '2'")
    assert "Berechnung" in html and "Schwelle" not in html
    # Switches, heating season and settings are not on this card.
    assert "data-toggle" not in html and "data-number" not in html
    assert "Schalter" not in html and "Heizsaison" not in html


def test_flow_card_counter_position(ctx) -> None:
    assert ctx.eval("counterPosition(0, -30, 60)") == pytest.approx(200 / 3)
    assert ctx.eval("counterPosition(-99, -30, 60)") == 100
    assert ctx.eval("counterPosition(99, -30, 90)") == 0
    assert ctx.eval("counterPosition(null, -30, 60)") is None
    assert ctx.eval("counterPosition('x', -30, 60)") is None


_SEASON_RENDER_SETUP = """
var SE = {}, SS = {};
function sadd(id, key, state, attributes, extra) {
  SE[id] = {entity_id: id, platform: 'heating_controller', translation_key: key, device_id: 'hub'};
  SS[id] = Object.assign({entity_id: id, state: String(state), attributes: attributes || {}}, extra || {});
}
var SEASON_ATTRS = {demand_hours_24h: 4.5, on_threshold_h: 6, hold_threshold_h: 2, off_after_h: 48,
  last_demand: new Date(Date.now() - 3 * 3600000).toISOString(), heating_on: true,
  heat_source_entity: 'climate.wp'};
sadd('binary_sensor.ein', 'heating_season_switch_on', 'off', SEASON_ATTRS);
sadd('binary_sensor.aus', 'heating_season_switch_off', 'off', SEASON_ATTRS);
sadd('binary_sensor.anf', 'lead_heating_demand', 'on',
  {hold_flow_temperature_c: 41, flow_threshold_c: 30, rooms: ['Wohnzimmer', 'Rebekka']},
  {last_changed: new Date(Date.now() - 2 * 3600000).toISOString()});
sadd('sensor.stunden', 'heating_demand_hours_24h', 4.5);
sadd('sensor.letzte', 'heating_season_last_demand', 'x');
sadd('number.ein', 'heating_season_on_threshold_h', 6);
sadd('number.halt', 'heating_season_hold_threshold_h', 2);
sadd('sensor.halte', 'lead_hold_flow_temperature', 41, {room: 'Rebekka', rooms: ['Wohnzimmer', 'Rebekka']});
sadd('sensor.reserve', 'hold_flow_reserve', -1.5);
SS['climate.wp'] = {entity_id: 'climate.wp', state: 'heat', attributes: {}};
var SEASON_HASS = {entities: SE, states: SS, locale: {language: 'de'}};
function renderSeason() {
  var card = new HeatingControllerSeasonCard();
  card.setConfig({});
  card.hass = SEASON_HASS;
  return card.shadowRoot.innerHTML;
}
"""


def test_season_card_renders(ctx) -> None:
    ctx.eval(_FLOW_RENDER_SETUP)
    ctx.eval(_SEASON_RENDER_SETUP)
    html = ctx.eval("renderSeason()")
    for broken in ("undefined", "NaN", "null", "season_"):
        assert broken not in html
    assert "Heizsaison" in html and "Heizgrenze</h3>" in html
    # Heat | Off: the current mode is marked, the other asks before switching.
    assert 'class="heat active" data-entity="climate.wp"' in html
    assert 'data-mode="off" data-target="climate.wp"' in html
    # Hold flow against the threshold decides the demand.
    assert ">Haltevorlauf</span><span>41.0\u00a0°C<" in html
    assert '<span class="op">≥</span>' in html and ">Ja<" in html
    assert "Leiträume: Wohnzimmer, <b>Rebekka</b>" in html
    assert "Reserve zur Kurve</span> −1.5\u00a0K" in html
    assert "Seit</span> vor 2 h" in html
    # 4.5 h on the 24 h bar with ticks at the hold and switch-on thresholds.
    assert "width:" + str(4.5 / 24 * 100) + "%" in html
    assert "Halten 2.0\u00a0h" in html and "Ein 6.0\u00a0h" in html
    assert "Letzte Anforderung</span> vor 3 h" in html
    assert "Ausschalten empfohlen</span> in 45 h" in html
    assert "empfohlen<" not in html.split("Heizgrenze")[0]


def test_season_card_shows_the_automatic_switch(ctx) -> None:
    ctx.eval(_FLOW_RENDER_SETUP)
    ctx.eval(_SEASON_RENDER_SETUP)
    ctx.eval("sadd('switch.auto', 'heating_season_auto', 'on');")
    ctx.eval(
        "sadd('binary_sensor.aus', 'heating_season_switch_off', 'on', SEASON_ATTRS);"
    )
    html = ctx.eval("renderSeason()")
    for broken in ("undefined", "NaN", "null", "season_"):
        assert broken not in html
    assert 'class="auto on" data-auto-toggle="switch.auto"' in html
    assert ">Automatik</button>" in html
    # With the automatic on the hub switches instead of recommending.
    assert "Wird ausgeschaltet" in html
    assert "Schaltet aus</span> in 45 h" in html
    # A hand switch knows it turns the automatic off.
    assert 'data-target="climate.wp" data-auto="switch.auto"' in html

    ctx.eval("sadd('switch.auto', 'heating_season_auto', 'off');")
    html = ctx.eval("renderSeason()")
    assert 'class="auto" data-auto-toggle="switch.auto"' in html
    assert "Ausschalten empfohlen<" in html
    ctx.eval("SS['binary_sensor.ein'].state = 'on'; SS['climate.wp'].state = 'off'")
    ctx.eval("SEASON_ATTRS.heating_on = false")
    html_on = ctx.eval("renderSeason()")
    assert "Einschalten empfohlen" in html_on
    assert 'class="off active"' in html_on and 'data-mode="heat"' in html_on
    assert "Ausschalten empfohlen" not in html_on


def test_season_hours_helpers(ctx) -> None:
    assert ctx.eval("hoursPosition(12)") == 50
    assert ctx.eval("hoursPosition(30)") == 100
    assert ctx.eval("hoursPosition(null)") is None
    assert ctx.eval(
        "hoursUntilOff('2026-11-20T00:00:00Z', 48, Date.parse('2026-11-21T00:00:00Z'))"
    ) == pytest.approx(24)
    assert ctx.eval("hoursUntilOff(null, 48, 0)") is None

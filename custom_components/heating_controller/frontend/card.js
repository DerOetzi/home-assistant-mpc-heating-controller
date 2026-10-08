/*
 * Heating Controller card.
 *
 * Takes one heating_controller device and renders the whole room from it.
 * Entities are resolved by translation_key, never by entity_id patterns —
 * room slugs differ from area names and users rename entities.
 */

import {
  CONDITION_KEY,
  DETAIL_KEY,
  DEVICE_CLASS_ICONS,
  DEVICE_CLASS_UNITS,
  EDITOR_TAG,
  HEADER_KEY,
  MODE_COLORS,
  REQUIRED_ROLES,
} from "./const.js";
import { num, localeOf, isOn, entityValue } from "./format.js";
import { resolveRoles, managedDetailRows, managedHeaderRows } from "./entities.js";
import { migrateLegacyConfig, normalizeItems, normalizeDetail } from "./config.js";
import { CARD_STYLES, NOTICE_STYLES } from "./card-styles.js";
import {
  boostButton,
  comfortConditionToggle,
  comfortSlider,
  ecoStepper,
  modeSegments,
  supplyPresentation,
} from "./controls.js";
import { t } from "./translations.js";

export class HeatingControllerCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(EDITOR_TAG);
  }

  static getStubConfig() {
    return { device: "" };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._expanded = false;
    this._editMode = false;
    this._rendered = false;
  }

  // Lovelace sets this while the dashboard is being edited. Expanding then
  // is what the expander-card this replaces does, and without it half the
  // card's content is invisible exactly when you are laying it out.
  set editMode(editMode) {
    this._editMode = Boolean(editMode);
    this._applyExpanded();
  }

  _isExpanded() {
    return this._expanded || this._editMode;
  }

  setConfig(config) {
    // Deliberately tolerant of a missing device: the visual editor starts out
    // with an empty one, and throwing here would leave the editor showing a
    // broken preview until the very first pick.
    this._config = migrateLegacyConfig(config ?? {});
    this._rendered = false;
  }

  getCardSize() {
    return this._isExpanded() ? 8 : 2;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // ---------- entity resolution ----------

  _resolveEntities() {
    return resolveRoles(this._hass, this._config.device);
  }

  _missingRoles(roles) {
    return REQUIRED_ROLES.filter((role) => !roles[role]);
  }

  _deviceName() {
    if (this._config.title) return this._config.title;
    const device = this._hass.devices?.[this._config.device];
    return (
      device?.name_by_user || device?.name || t(this._hass, "default_room_name")
    );
  }

  // ---------- derived state ----------

  _status(roles) {
    const mode = roles.mode.state;
    const automation = isOn(roles.automation);
    const options = roles.mode.attributes.options ?? [];
    const labels = this._modeLabels(roles.mode, options);

    if (mode === "frost_protection" && automation) {
      return { text: t(this._hass, "status_forced_frost"), forcedFrost: true };
    }
    const prefix = t(this._hass, automation ? "status_automatic" : "status_blocked");
    return { text: `${prefix} – ${labels[mode] ?? mode}`, forcedFrost: false };
  }

  // Labels come from the translated select options, so the card never
  // duplicates the integration's mode naming.
  _modeLabels(modeState, options) {
    const labels = {};
    for (const option of options) {
      labels[option] = this._hass.formatEntityState
        ? this._hass.formatEntityState(modeState, option)
        : option;
    }
    return labels;
  }

  _openWindows(roles) {
    const contacts = roles.automation.attributes.window_contact_entities ?? [];
    return contacts.filter((entityId) => isOn(this._hass.states[entityId]));
  }

  // ---------- actions ----------

  _callService(domain, service, data) {
    this._hass.callService(domain, service, data);
  }

  _selectMode(roles, option) {
    this._callService("select", "select_option", {
      entity_id: roles.mode.entity_id,
      option,
    });
  }

  _toggle(entityId) {
    const domain = entityId.split(".")[0];
    this._callService(domain, "toggle", { entity_id: entityId });
  }

  // Explicit on/off rather than toggle: a segmented An/Aus control must set
  // the state its label promises, even if clicked while already in it.
  _setBoolean(entityId, on) {
    const domain = entityId.split(".")[0];
    this._callService(domain, on ? "turn_on" : "turn_off", { entity_id: entityId });
  }

  _moreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      })
    );
  }

  // The context handed to the control builders in controls.js. Bundled here so
  // the wiring lives in one place instead of at every call site.
  _controlContext() {
    return {
      hass: this._hass,
      effectiveValue: (stateObj) => this._effectiveValue(stateObj),
      setNumber: (stateObj, value) => this._setNumber(stateObj, value),
      setDragging: (dragging) => {
        this._dragging = dragging;
      },
      isDragging: () => Boolean(this._dragging),
      toggle: (entityId) => this._toggle(entityId),
      setBoolean: (entityId, on) => this._setBoolean(entityId, on),
      moreInfo: (entityId) => this._moreInfo(entityId),
    };
  }

  // ---------- rendering ----------

  _render() {
    if (!this._hass || !this._config) return;

    if (!this._config.device) {
      this._renderHint(t(this._hass, "error_pick_device"));
      return;
    }

    const roles = this._resolveEntities();
    const missing = this._missingRoles(roles);
    if (missing.length) {
      this._renderError(
        `${t(this._hass, "error_missing_entities", { missing: missing.join(", ") })} ` +
          t(this._hass, "error_wrong_device")
      );
      return;
    }

    if (!this._rendered) {
      this._buildSkeleton();
      this._rendered = true;
      // Fresh DOM, so no block matches its cached signature any more.
      this._signatures = {};
      this._applyExpanded();
    }
    this._update(roles);
  }

  _renderError(message) {
    this._renderNotice(message, "error");
  }

  _renderHint(message) {
    this._renderNotice(message, "hint");
  }

  _renderNotice(message, kind) {
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="${kind}">${message}</div>
      </ha-card>
      <style>${NOTICE_STYLES}</style>`;
    this._rendered = false;
  }

  _buildSkeleton() {
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="header" id="header">
          <ha-icon id="statusIcon" class="status-icon"></ha-icon>
          <div class="titles">
            <div class="title" id="title"></div>
            <div class="subtitle" id="subtitle"></div>
          </div>
          <button id="unblock" class="unblock" hidden>
            <ha-icon icon="mdi:auto-mode"></ha-icon>${t(this._hass, "unblock")}
          </button>
          <ha-icon id="windowIcon" class="window-icon" icon="mdi:window-open-variant"></ha-icon>
          <ha-icon id="chevron" class="chevron" icon="mdi:chevron-down"></ha-icon>
        </div>
        <div class="values" id="headerValues"></div>
        <div class="conditions" id="conditions"></div>
        <div class="boost-row" id="boostRow"></div>
        <div class="details" id="details" hidden>
          <div class="section" id="sectionDetails">
            <div class="divider">${t(this._hass, "section_details")}</div>
            <div class="rows" id="detailRows"></div>
          </div>
          <div class="section">
            <div class="divider">${t(this._hass, "section_controls")}</div>
            <div class="controls" id="controls"></div>
          </div>
        </div>
      </ha-card>
      <style>${CARD_STYLES}</style>`;

    this.shadowRoot
      .getElementById("header")
      .addEventListener("click", () => this._toggleExpanded());

    // Sits inside the header, which toggles the card -- so it has to keep the
    // click to itself.
    this.shadowRoot.getElementById("unblock").addEventListener("click", (ev) => {
      ev.stopPropagation();
      const roles = this._resolveEntities();
      if (roles.unblock) {
        this._callService("button", "press", { entity_id: roles.unblock.entity_id });
      }
    });
  }

  _toggleExpanded() {
    this._expanded = !this._expanded;
    this._applyExpanded();
  }

  _applyExpanded() {
    const details = this.shadowRoot?.getElementById("details");
    if (!details) return;
    const expanded = this._isExpanded();
    details.hidden = !expanded;
    this.shadowRoot.getElementById("chevron").classList.toggle("open", expanded);
  }

  _update(roles) {
    const root = this.shadowRoot;
    const status = this._status(roles);
    const openWindows = this._openWindows(roles);

    root.getElementById("title").textContent = this._deviceName();
    root.getElementById("subtitle").textContent = status.text;
    root.getElementById("windowIcon").hidden = openWindows.length === 0;

    // Only while the automation is blocked -- an always-visible button would
    // suggest there is something to do when there isn't.
    const unblock = root.getElementById("unblock");
    unblock.hidden = isOn(roles.automation) || !roles.unblock;

    const icon = root.getElementById("statusIcon");
    if (status.forcedFrost) icon.icon = "mdi:snowflake-alert";
    else if (isOn(roles.automation)) icon.icon = "mdi:auto-mode";
    else icon.icon = "mdi:stop-circle-outline";
    // The icon shape says what the controller is doing, the mode colour tints
    // both the glyph and the disc behind it — same split as the old card.
    icon.style.setProperty("--hc-mode-color", MODE_COLORS[roles.mode.state] ?? "");

    // hass is reassigned on every state change anywhere in Home Assistant, many
    // times a minute. Rebuilding a block then throws away the element the mouse
    // is over (the hover flickers) and the element a finger is dragging (the
    // slider dies mid-gesture). So each block is only rebuilt when what it
    // renders actually changed.
    this._rebuildIf("headerValues", this._headerSignature(roles), () =>
      this._updateHeaderValues(roles)
    );
    this._rebuildIf("conditions", this._conditionsSignature(roles), () =>
      this._updateConditions(roles)
    );
    this._rebuildIf(
      "boost",
      `${roles.mode.state}|${(roles.mode.attributes.options ?? []).join(",")}`,
      () => this._updateBoost(roles)
    );
    this._rebuildIf("details", this._detailSignature(roles), () =>
      this._updateDetails(roles)
    );
    if (!this._dragging) {
      this._rebuildIf("controls", this._controlSignature(roles), () =>
        this._updateControls(roles)
      );
    }
  }

  _valueEl({ entityId, value, unit, label, icon }) {
    const el = document.createElement("div");
    el.className = "value";
    // The label is a caption above the reading (matching the old card's
    // "Raumtemp." / "Luftfeuchtigkeit" headers) rather than trailing text
    // after the unit -- it names which room/sensor the value is for, not a
    // unit-like qualifier of the value itself.
    if (label) {
      el.innerHTML = `<span class="label">${label}</span>`;
    }
    el.innerHTML += `<div class="value-main">${
      icon ? `<ha-icon icon="${icon}"></ha-icon>` : ""
    }<span class="num">${value}</span><span class="unit">${unit ?? ""}</span></div>`;
    if (entityId) {
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._moreInfo(entityId);
      });
    }
    return el;
  }

  // A block is rebuilt only when its signature changed. The signature has to
  // cover everything the block renders -- a value left out of it would freeze
  // on screen.
  _rebuildIf(key, signature, build) {
    this._signatures = this._signatures ?? {};
    if (this._signatures[key] === signature) return;
    this._signatures[key] = signature;
    build();
  }

  // The room-temperature entity is a managed header row (see
  // managedHeaderRows): present and unlabeled by default, but sortable,
  // hideable and nameable through the same list as any hand-added header
  // sensor. Config order and hidden flags win, same as detail rows and
  // comfort conditions; a managed row the config hasn't mentioned yet defaults
  // to the front, because that's where the primary reading has always been.
  _headerEntries(roles) {
    const managed = new Map(
      managedHeaderRows(this._hass, roles).map((row) => [row.key, row])
    );
    const configured = normalizeDetail(this._config[HEADER_KEY]);
    const used = new Set();
    const entries = [];

    for (const entry of configured) {
      if (!entry.key) {
        entries.push(entry);
        continue;
      }
      const base = managed.get(entry.key);
      if (!base) continue;
      used.add(entry.key);
      if (entry.hidden) continue;
      entries.push({ entity: base.entity, name: entry.name, icon: entry.icon ?? base.icon });
    }
    for (const [key, base] of [...managed].reverse()) {
      if (used.has(key)) continue;
      entries.unshift({ entity: base.entity, icon: base.icon });
    }
    return entries;
  }

  _headerSignature(roles) {
    return this._headerEntries(roles)
      .map(
        (e) =>
          `${e.entity}=${e.name}=${e.icon}=${this._hass.states[e.entity]?.state}`
      )
      .join("|");
  }

  _detailSignature(roles) {
    return this._detailRows(roles)
      .map((row) => `${row.key ?? row.entity}=${row.name}=${row.icon}=${row.value}`)
      .join("|");
  }

  _controlSignature(roles) {
    const flow = roles.minFlow?.attributes ?? {};
    return [
      roles.mode.state,
      (roles.mode.attributes.options ?? []).join(","),
      roles.comfort && this._effectiveValue(roles.comfort),
      roles.eco && this._effectiveValue(roles.eco),
      roles.minFlow?.state,
      flow.supply_status,
    ].join("|");
  }

  // Room-specific comfort conditions, shown in the header (see
  // _updateConditions) rather than inside the collapsed controls section --
  // they need to be visible without expanding the card, the same as boost.
  //
  // Reconciled with the config the same way detail rows are: config order and
  // hidden flags win, entries no longer reported by the device drop out,
  // newly reported ones append at the end.
  _conditionEntries(roles) {
    const actualIds = roles.automation.attributes.comfort_condition_entities ?? [];
    const configured = normalizeItems(this._config[CONDITION_KEY]);
    const used = new Set();
    const entries = [];

    for (const entry of configured) {
      if (!actualIds.includes(entry.entity)) continue;
      used.add(entry.entity);
      if (entry.hidden) continue;
      entries.push(entry);
    }
    for (const id of actualIds) {
      if (!used.has(id)) entries.push({ entity: id });
    }
    return entries;
  }

  _conditionsSignature(roles) {
    return this._conditionEntries(roles)
      .map(
        (e) =>
          `${e.entity}=${e.icon_on}=${e.icon_off}=${e.label_on}=${e.label_off}=` +
          this._hass.states[e.entity]?.state
      )
      .join("|");
  }

  _updateHeaderValues(roles) {
    const container = this.shadowRoot.getElementById("headerValues");
    container.textContent = "";

    for (const item of this._headerEntries(roles)) {
      const stateObj = this._hass.states[item.entity];
      if (!stateObj) continue;
      const deviceClass = stateObj.attributes.device_class;
      container.appendChild(
        this._valueEl({
          entityId: item.entity,
          value: num(
            stateObj.state,
            deviceClass === "temperature" ? 1 : 0,
            localeOf(this._hass)
          ),
          unit:
            stateObj.attributes.unit_of_measurement ??
            DEVICE_CLASS_UNITS[deviceClass] ??
            "",
          label: item.name,
          icon: item.icon ?? DEVICE_CLASS_ICONS[deviceClass],
        })
      );
    }
  }

  _updateConditions(roles) {
    const container = this.shadowRoot.getElementById("conditions");
    container.textContent = "";
    const context = this._controlContext();
    for (const entry of this._conditionEntries(roles)) {
      const el = comfortConditionToggle(this._hass, entry, context);
      if (el) container.appendChild(el);
    }
  }

  _updateBoost(roles) {
    const row = this.shadowRoot.getElementById("boostRow");
    row.textContent = "";
    if (!(roles.mode.attributes.options ?? []).includes("boost")) return;
    row.appendChild(
      boostButton(this._hass, roles.mode.state === "boost", {
        selectMode: (option) => this._selectMode(roles, option),
      })
    );
  }

  _row(name, value, icon, entityId) {
    const el = document.createElement("div");
    el.className = "row";
    const iconHtml = icon ? `<ha-icon icon="${icon}"></ha-icon>` : "";
    // The label sits in its own span so a long name can truncate with an
    // ellipsis instead of colliding with the value when both are long.
    el.innerHTML =
      `<span class="k">${iconHtml}<span class="label">${name}</span></span>` +
      `<span class="v">${value}</span>`;
    if (entityId) {
      el.classList.add("clickable");
      el.addEventListener("click", () => this._moreInfo(entityId));
    }
    return el;
  }

  // Reconciles the seeded rows with the config: config order wins, hidden and
  // vanished rows drop out, and TRVs/windows added later append at the end.
  _detailRows(roles) {
    const managed = new Map(
      managedDetailRows(this._hass, roles).map((row) => [row.key, row])
    );
    const rows = [];
    const used = new Set();

    for (const entry of normalizeDetail(this._config[DETAIL_KEY])) {
      if (entry.key) {
        used.add(entry.key);
        const base = managed.get(entry.key);
        if (!base || entry.hidden) continue;
        rows.push({
          ...base,
          name: entry.name ?? base.name,
          icon: entry.icon ?? base.icon,
        });
      } else {
        const stateObj = this._hass.states[entry.entity];
        if (!stateObj) continue;
        rows.push({
          entity: entry.entity,
          name: entry.name ?? stateObj.attributes.friendly_name ?? entry.entity,
          icon: entry.icon ?? DEVICE_CLASS_ICONS[stateObj.attributes.device_class],
          value: entityValue(this._hass, stateObj, entry.state_labels),
        });
      }
    }
    for (const row of managed.values()) {
      if (!used.has(row.key)) rows.push(row);
    }
    return rows;
  }

  _updateDetails(roles) {
    const rows = this.shadowRoot.getElementById("detailRows");
    rows.textContent = "";
    for (const row of this._detailRows(roles)) {
      rows.appendChild(
        this._row(row.name, row.value, row.icon, row.clickEntity ?? row.entity)
      );
    }
    this.shadowRoot.getElementById("sectionDetails").hidden =
      rows.childElementCount === 0;
  }

  _updateControls(roles) {
    const container = this.shadowRoot.getElementById("controls");
    container.textContent = "";
    const context = this._controlContext();

    container.appendChild(
      modeSegments(roles, this._modeLabels(roles.mode, roles.mode.attributes.options ?? []), {
        selectMode: (option) => this._selectMode(roles, option),
      })
    );

    const setpoints = document.createElement("div");
    setpoints.className = "setpoints";
    if (roles.comfort) setpoints.appendChild(comfortSlider(roles.comfort, context));
    if (roles.eco) setpoints.appendChild(ecoStepper(roles.eco, context));
    container.appendChild(setpoints);

    // Minimum flow temperature — same row styling as a detail sensor (icon,
    // spelled-out value, 44 px tall, clickable), not a special one-off widget.
    if (roles.minFlow) {
      const { value, verdict, hint } = supplyPresentation(this._hass, roles.minFlow);
      const flow = this._row(
        t(this._hass, "row_min_flow"),
        `<span class="${verdict}">${value}</span>`,
        "mdi:water-thermometer",
        roles.minFlow.entity_id
      );
      if (hint) flow.title = hint;
      container.appendChild(flow);
    }
  }

  // Setting a number runs in three steps, deliberately separated:
  //
  //   1. the shown value changes at once, so the control follows the finger
  //   2. the service call goes out 500 ms later, so a correction while still
  //      adjusting replaces the previous call instead of queueing behind it
  //   3. 2 s after that the local value is dropped
  //
  // Step 3 is the round-trip check: if the write landed, the state already
  // equals the local value and dropping it changes nothing on screen. If it did
  // not, the control snaps back to what the server actually has, rather than
  // showing a value that was never set.
  _setNumber(stateObj, value) {
    const { min, max, step } = stateObj.attributes;
    const snapped = Math.min(max, Math.max(min, Math.round(value / step) * step));
    const entityId = stateObj.entity_id;

    this._pending = this._pending ?? {};
    const entry = (this._pending[entityId] = this._pending[entityId] ?? {});
    clearTimeout(entry.writeTimer);
    clearTimeout(entry.verifyTimer);
    entry.value = snapped;

    entry.writeTimer = setTimeout(() => {
      this._callService("number", "set_value", {
        entity_id: entityId,
        value: snapped,
      });
      entry.verifyTimer = setTimeout(() => {
        delete this._pending[entityId];
        this._refreshControls();
      }, 2000);
    }, 500);

    this._refreshControls();
  }

  // Through the same guard as the regular update, so the cached signature stays
  // in sync with what is on screen -- otherwise the next real change from Home
  // Assistant would be skipped as "unchanged".
  _refreshControls() {
    if (!this._hass || !this._rendered) return;
    const roles = this._resolveEntities();
    if (!roles.mode || !roles.automation) return;
    this._rebuildIf("controls", this._controlSignature(roles), () =>
      this._updateControls(roles)
    );
  }

  _effectiveValue(stateObj) {
    const entry = this._pending?.[stateObj.entity_id];
    return entry ? entry.value : Number.parseFloat(stateObj.state);
  }

  disconnectedCallback() {
    for (const entry of Object.values(this._pending ?? {})) {
      clearTimeout(entry.writeTimer);
      clearTimeout(entry.verifyTimer);
    }
    this._pending = {};
  }
}

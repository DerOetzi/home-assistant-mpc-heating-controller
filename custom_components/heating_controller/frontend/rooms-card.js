/*
 * Rooms card.
 *
 * One table over every room of the integration, grouped by floor, with three
 * views: supply (room vs. target, valve judgement, hold and boost flow),
 * radiators (valve opening per TRV, windows, cold radiator) and learning
 * (UA and capacity, additional heat). Rooms are found by their devices; no
 * entity is listed by hand. Tapping a value opens its more-info dialog.
 */

import { MODE_ICONS, MODE_COLORS } from "./const.js";
import { num, withUnit, localeOf, isOn } from "./format.js";
import { resolveRoles } from "./entities.js";
import { escapeHtml } from "./curve.js";
import {
  ROOM_TABLE_ROLES,
  ROOM_VIEWS,
  groupRooms,
  integrationEntity,
  learningJudgement,
  roomDeviceIds,
  trvLabels,
} from "./rooms.js";
import { t } from "./translations.js";

export const ROOMS_CARD_TAG = "heating-controller-rooms-card";

const ROOMS_STYLES = `
  :host {
    --hc-comfort: var(--color-comfort, #e5533d);
    --hc-eco: var(--color-eco, #43a047);
    --hc-boost: var(--color-boost, #ff6d00);
    --hc-frost: var(--color-frost, #33ccff);
  }
  ha-card { padding: 12px 16px 12px; container-type: inline-size; }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .title { font-size: 1.2em; font-weight: 500; flex: 1 1 auto; }
  .tabs { display: flex; gap: 4px; }
  .tab {
    font: inherit; font-size: 0.85em; cursor: pointer; padding: 3px 10px;
    border-radius: 14px; border: 1px solid var(--divider-color);
    background: none; color: var(--secondary-text-color);
  }
  .floor-tabs { display: none; gap: 4px; flex-wrap: wrap; margin-bottom: 4px; }
  .tab[aria-pressed="true"] { color: var(--primary-text-color); border-color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 14%, transparent); }
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th { font-size: 0.75em; font-weight: 500; color: var(--secondary-text-color);
    text-align: right; padding: 2px 6px; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; padding-left: 0; }
  td { padding: 5px 6px; text-align: right; white-space: nowrap;
    border-top: 1px solid var(--divider-color); }
  tr.floor td { border-top: none; padding: 10px 0 2px; font-weight: 500;
    color: var(--secondary-text-color); font-size: 0.85em; text-align: left; }
  [data-entity] { cursor: pointer; }
  .room { display: inline-flex; align-items: center; gap: 6px; }
  .room ha-icon { --mdc-icon-size: 18px; }
  .lead-dot { color: var(--secondary-text-color); }
  .marker { display: inline-flex; align-items: center; gap: 2px; color: var(--secondary-text-color);
    font-size: 0.85em; }
  .marker ha-icon { --mdc-icon-size: 16px; }
  .marker .label { display: none; }
  @container (min-width: 900px) { .marker .label { display: inline; } }
  .muted { color: var(--secondary-text-color); opacity: 0.7; }
  .flow-room { font-weight: 600; color: var(--primary-color); }
  .chip { display: block; text-align: center; padding: 1px 8px; white-space: nowrap;
    border-radius: 10px; font-size: 0.85em; color: var(--text-primary-color, #fff); }
  .chip.undersupplied { background: var(--error-color, #db4437); }
  .chip.supplied { background: var(--success-color, #43a047); }
  .chip.oversupplied { background: var(--info-color, #039be5); }
  .chip.not_rated { background: var(--disabled-color, #9e9e9e); }
  .valves { display: inline-flex; gap: 10px; justify-content: flex-end; }
  .valve { display: inline-grid; grid-template-columns: minmax(0, 1fr); gap: 2px; width: 4.2em;
    text-align: left; }
  .valve .label { font-size: 0.75em; color: var(--secondary-text-color); overflow: hidden;
    text-overflow: ellipsis; }
  .bar { height: 4px; border-radius: 2px; background: var(--divider-color); overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--state-climate-heat-color, #ff8100); }
  td ha-icon { --mdc-icon-size: 18px; }
  .open { color: var(--info-color, #039be5); }
  .alert { color: var(--warning-color, #ffa600); margin-right: 6px; vertical-align: top; }
  .ok { color: var(--success-color, #43a047); }
  .attention { color: var(--warning-color, #ffa600); }
  .empty { color: var(--secondary-text-color); }
`;

// Below this card width the table turns into one tile per room: the columns
// stack as label and value, as many side by side as fit. One width for every
// view, so switching tabs never flips the layout; the widest view sets it.
const STACK_BELOW_PX = 780;

const stackStyles = (view) => {
  const table = `table.view-${view}`;
  return `@container (max-width: ${STACK_BELOW_PX}px) {
    ${table} thead { display: none; }
    ${table}, ${table} tbody { display: block; }
    ${table} tr { display: grid; grid-template-columns: repeat(auto-fill, minmax(88px, 1fr));
      gap: 6px 12px; padding: 8px 0; border-top: 1px solid var(--divider-color); }
    ${table} tr.floor { display: block; border-top: none; padding: 0; }
    ${table} td { display: block; border-top: none; padding: 0; text-align: left; white-space: normal; }
    ${table} tr.floor td { padding: 10px 0 2px; }
    ${table} td:first-child { grid-column: 1 / -1; }
    ${table} td.multi-valve, ${table} td.multi-valve .valves { display: contents; }
    ${table} td.multi-valve .valve { justify-self: start; }
    ${table} td:first-child { font-weight: 500; }
    ${table} td:empty { display: none; }
    ${table} td[data-label]:not(:first-child):not(.multi-valve)::before { content: attr(data-label); display: block;
      font-size: 0.75em; color: var(--secondary-text-color); }
    ${table} .valves { justify-content: flex-start; flex-wrap: wrap; }
    ${table} .chip { display: inline-block; }
    ${table} tr.other-floor, ${table}.floor-tabbed tr.floor { display: none; }
    .floor-tabs.view-${view} { display: flex; }
    ${table} .marker .label { display: inline; }
  }`;
};

const ROOM_COLUMNS = {
  supply: ["col_room", "col_actual", "col_target", "col_supply", "col_hold_gated", "col_hold", "col_boost"],
  radiators: ["col_room", "col_actual", "col_target", "col_valves", "col_window"],
  learning: [
    "col_room",
    "col_ua",
    "col_ua_factor",
    "col_capacity",
    "col_capacity_factor",
    "col_additional_heat",
    "col_gain",
    "col_determined",
    "col_rms",
  ],
};

export class HeatingControllerRoomsCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = null;
    this._html = null;
  }

  setConfig(config) {
    const views = (config?.views ?? ROOM_VIEWS).filter((view) => ROOM_VIEWS.includes(view));
    this._config = { ...(config ?? {}), views: views.length ? views : ROOM_VIEWS };
    this._view = null;
    this._html = null;
    if (this._hass) this._render();
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // ---------- view ----------

  _storageKey() {
    return `${ROOMS_CARD_TAG}:${this._config.title ?? ""}`;
  }

  _currentView() {
    if (this._view) return this._view;
    let stored = null;
    try {
      stored = localStorage.getItem(this._storageKey());
    } catch (err) {
      stored = null;
    }
    const views = this._config.views;
    this._view = [stored, this._config.view, views[0]].find((view) => views.includes(view));
    return this._view;
  }

  _currentFloor(groups) {
    const ids = groups.map((group) => group.id);
    if (this._floor == null) {
      try {
        this._floor = localStorage.getItem(`${this._storageKey()}:floor`);
      } catch (err) {
        this._floor = null;
      }
    }
    return ids.includes(this._floor) ? this._floor : ids[0];
  }

  _setFloor(floor) {
    this._floor = floor;
    try {
      localStorage.setItem(`${this._storageKey()}:floor`, floor);
    } catch (err) {
      // Private window or blocked storage: the card works without it.
    }
    this._render();
  }

  _setView(view) {
    this._view = view;
    try {
      localStorage.setItem(this._storageKey(), view);
    } catch (err) {
      // Private window or blocked storage: the card works without it.
    }
    this._render();
  }

  // ---------- render ----------

  _render() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const view = this._currentView();
    const curves = integrationEntity(hass, "hold_flow_curves");
    const target = integrationEntity(hass, "flow_target");
    const feedback = integrationEntity(hass, "flow_feedback");
    const integrationOrder = (curves?.attributes?.rooms ?? []).map((room) => room.name);
    const leads = new Set(
      (curves?.attributes?.rooms ?? []).filter((room) => room.lead).map((room) => room.name)
    );
    const context = {
      leads,
      flowRoom: target?.attributes?.source === "lead" ? target.attributes.room : null,
      feedbackRoom: feedback?.attributes?.lead_room ?? null,
    };
    const groups = groupRooms(hass, roomDeviceIds(hass), this._config.rooms, integrationOrder);
    const columns = ROOM_COLUMNS[view];

    let body = "";
    const showFloors = groups.length > 1 || groups[0]?.name;
    const floorName = (group) => escapeHtml(group.name || t(hass, "rooms_no_floor"));
    // Narrow cards show one floor at a time, picked by a tab; wide ones all.
    const floorTabbed = groups.length > 1;
    const floor = this._currentFloor(groups);
    for (const group of groups) {
      const other = floorTabbed && group.id !== floor ? ' class="other-floor"' : "";
      if (showFloors) {
        body += `<tr class="floor${other ? " other-floor" : ""}"><td colspan="${
          columns.length
        }">${floorName(group)}</td></tr>`;
      }
      for (const room of group.rooms) {
        const roles = resolveRoles(hass, room.id, ROOM_TABLE_ROLES);
        // Each cell carries its column's caption for the stacked layout,
        // unless it brings its own.
        const cells = this._cells(view, room, roles, context).map((cell, index) =>
          /^<td[^>]*data-label=/.test(cell)
            ? cell
            : cell.replace(/^<td/, `<td data-label="${escapeHtml(t(hass, columns[index]))}"`)
        );
        body += `<tr${other}>${cells.join("")}</tr>`;
      }
    }
    if (!groups.length) {
      body = `<tr><td class="empty" colspan="${columns.length}">${t(hass, "rooms_empty")}</td></tr>`;
    }

    const title = escapeHtml(this._config.title ?? t(hass, "rooms_title"));
    const tabs =
      this._config.views.length > 1
        ? `<div class="tabs">${this._config.views
            .map(
              (item) =>
                `<button type="button" class="tab" data-view="${item}" aria-pressed="${
                  item === view
                }">${t(hass, `rooms_view_${item}`)}</button>`
            )
            .join("")}</div>`
        : "";
    const floorTabs = floorTabbed
      ? `<div class="floor-tabs view-${view}">${groups
          .map(
            (group) =>
              `<button type="button" class="tab floor-tab" data-floor="${escapeHtml(
                group.id
              )}" aria-pressed="${group.id === floor}">${floorName(group)}</button>`
          )
          .join("")}</div>`
      : "";
    const html = `<style>${ROOMS_STYLES}${stackStyles(view)}</style>
      <ha-card>
        <div class="head"><div class="title">${title}</div>${tabs}</div>
        ${floorTabs}
        <div class="scroll"><table class="view-${view}${floorTabbed ? " floor-tabbed" : ""}">
          <thead><tr>${columns.map((key) => `<th>${t(hass, key)}</th>`).join("")}</tr></thead>
          <tbody>${body}</tbody>
        </table></div>
      </ha-card>`;
    // Most state updates touch nothing the table shows.
    if (html === this._html) return;
    this._html = html;
    this.shadowRoot.innerHTML = html;
    this.shadowRoot.querySelectorAll(".tab[data-view]").forEach((button) =>
      button.addEventListener("click", () => this._setView(button.dataset.view))
    );
    this.shadowRoot.querySelectorAll(".floor-tab").forEach((button) =>
      button.addEventListener("click", () => this._setFloor(button.dataset.floor))
    );
    this.shadowRoot.querySelectorAll("[data-entity]").forEach((element) =>
      element.addEventListener("click", () => this._moreInfo(element.dataset.entity))
    );
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

  // ---------- cells ----------

  _cell(stateObj, content, className = "", title = "") {
    const attrs = [
      stateObj ? `data-entity="${escapeHtml(stateObj.entity_id)}"` : "",
      className ? `class="${className}"` : "",
      title ? `title="${escapeHtml(title)}"` : "",
    ].join(" ");
    return `<td ${attrs}>${content}</td>`;
  }

  _value(stateObj, digits = 1, unit = "") {
    return withUnit(num(stateObj?.state, digits, localeOf(this._hass)), unit);
  }

  _nameCell(room, roles, context) {
    const hass = this._hass;
    const boosting = Boolean(roles.boost?.attributes?.boost_active);
    const mode = boosting ? "boost" : roles.mode?.state;
    const icon = MODE_ICONS[mode] ?? "mdi:radiator";
    const color = MODE_COLORS[mode] ?? "var(--state-icon-color)";
    const lead = context.leads.has(room.name);
    const feedback = context.feedbackRoom === room.name;
    const flowRoom = context.flowRoom === room.name;
    const marker = (icon, label) =>
      `<span class="marker"><ha-icon icon="${icon}"></ha-icon><span class="label">${t(
        hass,
        label
      )}</span></span>`;
    const modeText = roles.mode && hass.formatEntityState ? hass.formatEntityState(roles.mode) : "";
    return this._cell(
      roles.mode,
      `<span class="room"><ha-icon icon="${icon}" style="color:${color}"></ha-icon>${escapeHtml(
        room.name
      )}${lead ? '<span class="lead-dot">·</span>' : ""}${
        flowRoom ? marker("mdi:thermometer-chevron-up", "rooms_flow_room_short") : ""
      }${feedback ? marker("mdi:arrow-up-down", "rooms_feedback_short") : ""}</span>`,
      "",
      [
        modeText,
        lead ? t(hass, "curve_lead_room") : "",
        flowRoom ? t(hass, "rooms_flow_room") : "",
        feedback ? t(hass, "rooms_feedback_room") : "",
      ]
        .filter(Boolean)
        .join(" · ")
    );
  }

  _targetC(roles) {
    return roles.gatedHold?.attributes?.calculation_target_temperature_c;
  }

  _cells(view, room, roles, context) {
    const name = this._nameCell(room, roles, context);
    if (view === "radiators") return [name, ...this._radiatorCells(roles)];
    if (view === "learning") return [name, ...this._learningCells(roles)];
    return [name, ...this._supplyCells(room, roles, context)];
  }

  _supplyCells(room, roles, context) {
    const hass = this._hass;
    const locale = localeOf(hass);
    const supply = roles.supply;
    const state = supply?.state;
    const known = ["undersupplied", "supplied", "oversupplied", "not_rated"].includes(state);
    const reason = supply?.attributes?.reason;
    const demand = supply?.attributes?.demand_pct;
    let chip = "–";
    let title = "";
    if (known) {
      const percent = Number.isFinite(Number(demand)) && demand !== null ? ` ${num(demand, 0, locale)} %` : "";
      const label = state === "not_rated" ? "–" : `${t(hass, `supply_${state}`)}${percent}`;
      chip = `<span class="chip ${state}">${label}</span>`;
      title =
        state === "not_rated" && reason
          ? `${t(hass, "supply_not_rated")}: ${t(hass, `supply_reason_${reason}`)}`
          : t(hass, `supply_hint_${state}`);
    }
    const gateClosed = Boolean(roles.gatedHold?.attributes?.flow_gate_closed);
    const isFlowRoom = context.flowRoom === room.name;
    const boost = Number.parseFloat(roles.boost?.state);
    return [
      this._cell(roles.roomTemp, this._value(roles.roomTemp, 1, "°C")),
      this._cell(roles.gatedHold, withUnit(num(this._targetC(roles), 1, locale), "°C")),
      this._cell(supply, chip, "", title),
      this._cell(
        roles.gatedHold,
        this._value(roles.gatedHold, 1, "°C"),
        gateClosed ? "muted" : isFlowRoom ? "flow-room" : "",
        gateClosed ? t(hass, "rooms_gate_closed") : isFlowRoom ? t(hass, "rooms_flow_room") : ""
      ),
      this._cell(roles.hold, this._value(roles.hold, 1, "°C")),
      this._cell(roles.boost, boost > 0 ? withUnit(num(boost, 1, locale), "°C") : "–"),
    ];
  }

  _radiatorCells(roles) {
    const hass = this._hass;
    const locale = localeOf(hass);
    const valves = roles.demand?.attributes?.trv_valve_positions ?? {};
    const targets = roles.demand?.attributes?.trv_target_temps ?? {};
    const trvs = Object.keys({ ...targets, ...valves });
    const labels = trvLabels(
      Object.fromEntries(
        trvs.map((entityId) => [entityId, hass.states[entityId]?.attributes?.friendly_name ?? entityId])
      )
    );
    const valveHtml = trvs
      .map((entityId) => {
        const value = Number.parseFloat(valves[entityId]);
        const width = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
        const label = labels[entityId]
          ? `<span class="label" title="${escapeHtml(labels[entityId])}">${escapeHtml(labels[entityId])}</span>`
          : "";
        return `<span class="valve" data-entity="${escapeHtml(entityId)}">${label}<span>${
          Number.isFinite(value) ? `${num(value, 0, locale)} %` : "–"
        }</span><span class="bar"><span style="width:${width}%"></span></span></span>`;
      })
      .join("");
    const trvTarget = trvs.map((entityId) => targets[entityId]).find((value) => value != null);
    const contacts = roles.automation?.attributes?.window_contact_entities ?? [];
    const open = contacts.filter((entityId) => isOn(hass.states[entityId]));
    const windowIcon = contacts.length
      ? `<ha-icon icon="${open.length ? "mdi:window-open-variant" : "mdi:window-closed-variant"}" class="${
          open.length ? "open" : "muted"
        }"></ha-icon>`
      : "";
    const alert = isOn(roles.radiatorAlert)
      ? `<ha-icon icon="mdi:radiator-off" class="alert" title="${escapeHtml(
          t(hass, "radiator_not_heating")
        )}"></ha-icon>`
      : "";
    return [
      this._cell(roles.roomTemp, this._value(roles.roomTemp, 1, "°C")),
      this._cell(roles.demand, withUnit(num(trvTarget ?? this._targetC(roles), 1, locale), "°C")),
      // Several TRVs stack as one tile each, captioned by their own labels.
      `<td${
        trvs.length > 1
          ? ' class="multi-valve"'
          : ` data-label="${escapeHtml(t(hass, "col_valve"))}"`
      }>${alert}<span class="valves">${valveHtml || "–"}</span></td>`,
      this._cell(open[0] ? hass.states[open[0]] : contacts[0] ? hass.states[contacts[0]] : null, windowIcon),
    ];
  }

  _learningCells(roles) {
    const hass = this._hass;
    const locale = localeOf(hass);
    const status = (stateObj, kind) => {
      const state = stateObj?.state;
      if (!stateObj || state === "unknown" || state === "unavailable") return this._cell(stateObj, "–");
      const judgement = learningJudgement(kind, state);
      const mark = judgement === "ok" ? "✓ " : judgement === "attention" ? "⚠ " : "";
      const text = hass.formatEntityState ? hass.formatEntityState(stateObj) : state;
      return this._cell(stateObj, `${mark}${escapeHtml(text)}`, judgement);
    };
    const ua = roles.uaStatus?.attributes ?? {};
    const heat = roles.additionalHeat?.attributes ?? {};
    const gain = Number.isFinite(ua.learned_gain_w)
      ? `${withUnit(num(ua.learned_gain_w, 0, locale), "W")} (${
          Number.isFinite(ua.gain_sigma_w) ? withUnit(num(ua.gain_sigma_w, 0, locale), "W") : "–"
        })`
      : "–";
    const profile = Array.isArray(heat.profile_determined) ? heat.profile_determined : [];
    const sun = Object.values(heat.sun_determined ?? {});
    const count = (list) => `${list.filter(Boolean).length}/${list.length}`;
    const determined = `${ua.gain_determined ? "✓" : "–"} / ${count(profile)} / ${count(sun)}`;
    return [
      status(roles.uaStatus, "ua"),
      this._cell(roles.uaFactor, this._value(roles.uaFactor, 2)),
      status(roles.capStatus, "capacity"),
      this._cell(roles.capFactor, this._value(roles.capFactor, 3)),
      this._cell(roles.additionalHeat, this._value(roles.additionalHeat, 0, "W")),
      this._cell(roles.uaStatus, gain),
      this._cell(roles.additionalHeat, determined),
      this._cell(
        roles.additionalHeat,
        Number.isFinite(heat.residual_rms_w) ? withUnit(num(heat.residual_rms_w, 0, locale), "W") : "–"
      ),
    ];
  }
}

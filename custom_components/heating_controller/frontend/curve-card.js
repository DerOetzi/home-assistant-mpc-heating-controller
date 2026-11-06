/*
 * Heating curve card.
 *
 * Takes the flow device's "hold flow curves" sensor and draws the heat
 * source's curve, each room's comfort hold flow (flow lead rooms shown,
 * the others one tap away), the recommendation from the lead rooms and an
 * experiment curve to try a better curve before setting it on the heat
 * source. The experiment lives only in this browser; nothing is written.
 */

import { DOMAIN } from "./const.js";
import { num, localeOf } from "./format.js";
import {
  CURVE_NUMBER_KEYS,
  HOLD_CURVES_KEY,
  curveSeries,
  envelopeSeries,
  escapeHtml,
  recommendationSeries,
  tightestPoint,
  valueAt,
  valueRange,
  ticks,
} from "./curve.js";
import { t } from "./translations.js";

export const CURVE_CARD_TAG = "heating-controller-curve-card";

// HA's named colours where the theme has them, fixed hues otherwise.
const ROOM_COLORS = [
  "var(--blue-color, #2196f3)",
  "var(--green-color, #4caf50)",
  "var(--purple-color, #9c27b0)",
  "var(--amber-color, #ffc107)",
  "var(--pink-color, #e91e63)",
  "var(--cyan-color, #00bcd4)",
  "var(--indigo-color, #3f51b5)",
  "var(--lime-color, #cddc39)",
  "var(--brown-color, #795548)",
  "var(--teal-color, #009688)",
];

const EXPERIMENT_FIELDS = [
  { key: "flowCold", label: "curve_flow_cold", step: 1 },
  { key: "flowWarm", label: "curve_flow_warm", step: 1 },
  { key: "bump", label: "curve_bump", step: 1 },
  { key: "bumpWidth", label: "curve_bump_width", step: 1 },
];

const W = 600;
const H = 320;
const PAD = { left: 38, right: 10, top: 10, bottom: 30 };

const STYLES = `
  ha-card { padding: 12px 16px 16px; }
  .title { font-size: 1.2em; font-weight: 500; margin: 0 0 8px; }
  svg { display: block; width: 100%; height: auto; touch-action: pan-y; }
  svg text { fill: var(--secondary-text-color); font-size: 11px; }
  .grid { stroke: var(--divider-color); stroke-width: 1; }
  .now { stroke: var(--secondary-text-color); stroke-dasharray: 2 3; }
  .cursor { stroke: var(--primary-text-color); stroke-opacity: 0.35; }
  .rec { stroke: var(--secondary-text-color); stroke-width: 1.5; fill: none; }
  .source { stroke: var(--state-climate-heat-color, #ff8100); stroke-width: 3; fill: none; }
  .experiment {
    stroke: var(--primary-color); stroke-width: 2.5; fill: none;
    stroke-dasharray: 7 4;
  }
  .room { fill: none; stroke-width: 1.6; }
  .room.other { stroke-dasharray: 3 3; }
  .readout {
    display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 6px 0 10px;
    font-variant-numeric: tabular-nums; font-size: 0.9em;
  }
  .readout b { font-weight: 500; }
  .key { display: inline-block; width: 14px; height: 3px; border-radius: 2px;
    vertical-align: middle; margin-right: 5px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    border: 1px solid var(--divider-color); border-radius: 16px;
    padding: 3px 10px; font: inherit; font-size: 0.85em;
    background: none; color: var(--secondary-text-color);
  }
  .chip[aria-pressed="true"] { color: var(--primary-text-color);
    border-color: var(--primary-text-color); }
  .chip .dot { width: 10px; height: 10px; border-radius: 50%; }
  .chip[aria-pressed="false"] .dot { opacity: 0.35; }
  .experiment-box {
    border-top: 1px solid var(--divider-color); padding-top: 10px;
    display: grid; gap: 8px;
  }
  .experiment-head { font-weight: 500; }
  .fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
  .fields label { display: grid; gap: 2px; font-size: 0.85em; color: var(--secondary-text-color); }
  .fields input {
    font: inherit; font-size: 1rem; padding: 4px 6px; min-width: 0;
    color: var(--primary-text-color); background: var(--card-background-color);
    border: 1px solid var(--divider-color); border-radius: 4px;
  }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .actions-label { font-size: 0.85em; color: var(--secondary-text-color); }
  .actions button {
    font: inherit; font-size: 0.85em; cursor: pointer; padding: 4px 10px;
    border-radius: 4px; border: 1px solid var(--divider-color);
    background: none; color: var(--primary-color);
  }
  .summary { font-size: 0.9em; font-variant-numeric: tabular-nums; }
  .short { color: var(--error-color, #db4437); }
  .error { color: var(--error-color, #db4437); }
`;

export class HeatingControllerCurveCard extends HTMLElement {
  static getStubConfig(hass) {
    const entry = Object.values(hass?.entities ?? {}).find(
      (item) => item.platform === DOMAIN && item.translation_key === HOLD_CURVES_KEY
    );
    return { entity: entry?.entity_id ?? "" };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._visible = null;
    this._experiment = null;
    this._selected = null;
    this._signature = null;
  }

  setConfig(config) {
    this._config = { ...(config ?? {}) };
    this._signature = null;
  }

  getCardSize() {
    return 9;
  }

  set hass(hass) {
    this._hass = hass;
    const data = this._data();
    // Only redraw when something the card shows changed, so typing in the
    // experiment fields is never interrupted by an unrelated state update.
    const signature = data
      ? JSON.stringify([data.sensor.last_updated, data.curve])
      : "missing";
    if (signature === this._signature) return;
    this._signature = signature;
    this._render(data);
  }

  // ---------- data ----------

  _data() {
    const hass = this._hass;
    const sensor = hass?.states?.[this._config?.entity];
    const rooms = sensor?.attributes?.rooms;
    const outdoorValues = sensor?.attributes?.outdoor_c;
    if (!sensor || !Array.isArray(rooms) || !Array.isArray(outdoorValues)) return null;
    const deviceId = hass.entities?.[sensor.entity_id]?.device_id;
    const curve = {};
    for (const entry of Object.values(hass.entities ?? {})) {
      if (entry.device_id !== deviceId || entry.platform !== DOMAIN) continue;
      const field = CURVE_NUMBER_KEYS[entry.translation_key];
      const value = Number.parseFloat(hass.states[entry.entity_id]?.state);
      if (field && Number.isFinite(value)) curve[field] = value;
    }
    if (Object.keys(curve).length !== Object.keys(CURVE_NUMBER_KEYS).length) return null;
    return {
      sensor,
      rooms,
      outdoorValues,
      curve,
      margin: Number(sensor.attributes.margin_k ?? 0),
      outdoorNow: sensor.attributes.outdoor_temperature_c,
    };
  }

  _storageKey() {
    return `${CURVE_CARD_TAG}:${this._config.entity}`;
  }

  _loadState(data) {
    let stored = {};
    try {
      stored = JSON.parse(localStorage.getItem(this._storageKey()) ?? "{}") ?? {};
    } catch (err) {
      stored = {};
    }
    if (!this._visible) {
      const names = data.rooms.map((room) => room.name);
      this._visible = new Set(
        Array.isArray(stored.visible)
          ? stored.visible.filter((name) => names.includes(name))
          : data.rooms.filter((room) => room.lead).map((room) => room.name)
      );
    }
    if (!this._experiment) {
      this._experiment =
        stored.experiment && typeof stored.experiment === "object"
          ? { ...data.curve, ...stored.experiment }
          : { ...data.curve };
    }
  }

  _saveState() {
    try {
      localStorage.setItem(
        this._storageKey(),
        JSON.stringify({ visible: [...this._visible], experiment: this._experiment })
      );
    } catch (err) {
      // Private window or blocked storage: the card works without it.
    }
  }

  // ---------- render ----------

  _render(data) {
    const root = this.shadowRoot;
    if (!data) {
      root.innerHTML = `<style>${STYLES}</style><ha-card><div class="error">${t(
        this._hass,
        "curve_error_entity"
      )}</div></ha-card>`;
      return;
    }
    this._loadState(data);
    this._dataCache = data;
    if (this._selected === null) {
      this._selected = Number.isFinite(data.outdoorNow)
        ? Math.round(data.outdoorNow)
        : data.curve.outdoorCold;
    }
    const title = escapeHtml(this._config.title ?? t(this._hass, "curve_title"));
    root.innerHTML = `<style>${STYLES}</style>
      <ha-card>
        <div class="title">${title}</div>
        <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${title}"></svg>
        <div class="readout"></div>
        <div class="chips"></div>
        <div class="experiment-box">
          <div class="experiment-head">${t(this._hass, "curve_experiment")}</div>
          <div class="fields"></div>
          <div class="actions">
            <button type="button" data-action="source">${t(this._hass, "curve_copy_source")}</button>
          </div>
          <div class="actions">
            <span class="actions-label">${t(this._hass, "curve_copy_recommendation")}</span>
            <button type="button" data-action="all">${t(this._hass, "curve_rooms_all")}</button>
            <button type="button" data-action="lead">${t(this._hass, "curve_rooms_lead")}</button>
            <button type="button" data-action="shown">${t(this._hass, "curve_rooms_shown")}</button>
          </div>
          <div class="summary"></div>
        </div>
      </ha-card>`;
    this._renderChips(data);
    this._renderFields();
    root.querySelectorAll(".actions button").forEach((button) =>
      button.addEventListener("click", () => this._copy(button.dataset.action))
    );
    const svg = root.querySelector("svg");
    const pick = (event) => this._pick(svg, event);
    svg.addEventListener("pointermove", pick);
    svg.addEventListener("pointerdown", pick);
    this._update();
  }

  _renderChips(data) {
    const box = this.shadowRoot.querySelector(".chips");
    box.innerHTML = "";
    data.rooms.forEach((room, index) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.setAttribute("aria-pressed", String(this._visible.has(room.name)));
      chip.title = room.lead ? t(this._hass, "curve_lead_room") : "";
      chip.innerHTML = `<span class="dot" style="background:${
        ROOM_COLORS[index % ROOM_COLORS.length]
      }"></span>${escapeHtml(room.name)}${room.lead ? " ·" : ""}`;
      chip.addEventListener("click", () => {
        if (this._visible.has(room.name)) this._visible.delete(room.name);
        else this._visible.add(room.name);
        chip.setAttribute("aria-pressed", String(this._visible.has(room.name)));
        this._saveState();
        this._update();
      });
      box.appendChild(chip);
    });
  }

  _renderFields() {
    const box = this.shadowRoot.querySelector(".fields");
    box.innerHTML = "";
    for (const field of EXPERIMENT_FIELDS) {
      const label = document.createElement("label");
      label.textContent = t(this._hass, field.label);
      const input = document.createElement("input");
      input.type = "number";
      input.step = String(field.step);
      input.inputMode = "decimal";
      input.dataset.key = field.key;
      input.value = String(this._experiment[field.key]);
      input.addEventListener("input", () => {
        const value = Number.parseFloat(input.value);
        if (!Number.isFinite(value)) return;
        this._experiment = { ...this._experiment, [field.key]: value };
        this._saveState();
        this._update();
      });
      label.appendChild(input);
      box.appendChild(label);
    }
  }

  _syncFields() {
    this.shadowRoot.querySelectorAll(".fields input").forEach((input) => {
      input.value = String(this._experiment[input.dataset.key]);
    });
  }

  _copy(action) {
    const data = this._dataCache;
    if (action === "source") {
      this._experiment = { ...data.curve };
    } else {
      // The envelope of the chosen rooms plus margin at the curve's two ends;
      // bump and width stay.
      const chosen = data.rooms.filter((room) =>
        action === "lead" ? room.lead : action === "shown" ? this._visible.has(room.name) : true
      );
      const recommendation = envelopeSeries(chosen, data.margin);
      const at = (outdoor) => valueAt(data.outdoorValues, recommendation, outdoor);
      const cold = at(data.curve.outdoorCold);
      const warm = at(data.curve.outdoorWarm);
      if (!Number.isFinite(cold) || !Number.isFinite(warm)) return;
      this._experiment = {
        ...this._experiment,
        flowCold: Math.ceil(cold),
        flowWarm: Math.ceil(warm),
      };
    }
    this._syncFields();
    this._saveState();
    this._update();
  }

  _pick(svg, event) {
    const data = this._dataCache;
    const box = svg.getBoundingClientRect();
    if (!box.width) return;
    const x = ((event.clientX - box.left) / box.width) * W;
    const xs = data.outdoorValues;
    const outdoor = Math.round(
      xs[0] + ((x - PAD.left) / (W - PAD.left - PAD.right)) * (xs[xs.length - 1] - xs[0])
    );
    const clamped = Math.max(xs[0], Math.min(xs[xs.length - 1], outdoor));
    if (clamped === this._selected) return;
    this._selected = clamped;
    this._update();
  }

  _update() {
    const data = this._dataCache;
    const hass = this._hass;
    const locale = localeOf(hass);
    const xs = data.outdoorValues;
    const source = curveSeries(data.curve, xs);
    const experiment = curveSeries(this._experiment, xs);
    const recommendation = recommendationSeries(data.rooms, data.margin);
    // Rooms are drawn with the margin, like the recommendation, so every line
    // reads directly as "the curve needs to be at least here".
    const shown = data.rooms
      .map((room, index) => ({
        ...room,
        flow_c: envelopeSeries([room], data.margin),
        color: ROOM_COLORS[index % ROOM_COLORS.length],
      }))
      .filter((room) => this._visible.has(room.name));

    const [low, high] = valueRange([
      source,
      experiment,
      recommendation,
      ...shown.map((room) => room.flow_c),
    ]);
    const x = (value) =>
      PAD.left + ((value - xs[0]) / (xs[xs.length - 1] - xs[0])) * (W - PAD.left - PAD.right);
    const y = (value) =>
      PAD.top + ((high - value) / (high - low)) * (H - PAD.top - PAD.bottom);
    const path = (series) => {
      let d = "";
      let open = false;
      series.forEach((value, index) => {
        if (!Number.isFinite(value)) {
          open = false;
          return;
        }
        d += `${open ? "L" : "M"}${x(xs[index]).toFixed(1)} ${y(value).toFixed(1)}`;
        open = true;
      });
      return d;
    };

    let svg = "";
    for (const value of ticks(low, high, 5)) {
      svg += `<line class="grid" x1="${PAD.left}" x2="${W - PAD.right}" y1="${y(value)}" y2="${y(value)}"/>`;
      svg += `<text x="${PAD.left - 5}" y="${y(value) + 4}" text-anchor="end">${value}</text>`;
    }
    for (const value of ticks(xs[0], xs[xs.length - 1], 5)) {
      svg += `<line class="grid" y1="${PAD.top}" y2="${H - PAD.bottom}" x1="${x(value)}" x2="${x(value)}"/>`;
      svg += `<text x="${x(value)}" y="${H - PAD.bottom + 15}" text-anchor="middle">${value}</text>`;
    }
    svg += `<text x="${W - PAD.right}" y="${H - 3}" text-anchor="end">${t(hass, "curve_axis_outdoor")}</text>`;

    svg += `<path class="rec" d="${path(recommendation)}"/>`;
    for (const room of shown) {
      svg += `<path class="room${room.lead ? "" : " other"}" style="stroke:${room.color}" d="${path(room.flow_c)}"/>`;
    }
    svg += `<path class="source" d="${path(source)}"/>`;
    svg += `<path class="experiment" d="${path(experiment)}"/>`;
    if (Number.isFinite(data.outdoorNow) && data.outdoorNow >= xs[0] && data.outdoorNow <= xs[xs.length - 1]) {
      svg += `<line class="now" y1="${PAD.top}" y2="${H - PAD.bottom}" x1="${x(data.outdoorNow)}" x2="${x(data.outdoorNow)}"/>`;
    }
    svg += `<line class="cursor" y1="${PAD.top}" y2="${H - PAD.bottom}" x1="${x(this._selected)}" x2="${x(this._selected)}"/>`;
    this.shadowRoot.querySelector("svg").innerHTML = svg;

    // Readout at the selected outdoor value.
    const index = xs.indexOf(this._selected);
    const value = (series) => num(series[index], 1, locale);
    let readout = `<span><b>${t(hass, "curve_at_outdoor", { value: num(this._selected, 0, locale) })}</b></span>`;
    readout += `<span><span class="key" style="background:var(--state-climate-heat-color, #ff8100)"></span>${t(hass, "curve_source")} ${value(source)}</span>`;
    readout += `<span><span class="key" style="background:var(--primary-color)"></span>${t(hass, "curve_experiment")} ${value(experiment)}</span>`;
    readout += `<span><span class="key" style="background:var(--secondary-text-color)"></span>${t(hass, "curve_recommendation")} ${value(recommendation)}</span>`;
    for (const room of shown) {
      readout += `<span><span class="key" style="background:${room.color}"></span>${escapeHtml(room.name)} ${num(room.flow_c[index], 1, locale)}</span>`;
    }
    this.shadowRoot.querySelector(".readout").innerHTML = readout;

    // How the experiment compares to the recommendation.
    const tight = tightestPoint(xs, experiment, recommendation, this._experiment);
    const sourceTight = tightestPoint(xs, source, recommendation, data.curve);
    const line = (label, point) =>
      point
        ? `<div class="${point.reserve < 0 ? "short" : ""}">${t(hass, "curve_tightest", {
            curve: label,
            reserve: `${point.reserve > 0 ? "+" : ""}${num(point.reserve, 1, locale)}`,
            outdoor: num(point.outdoor, 0, locale),
          })}</div>`
        : "";
    this.shadowRoot.querySelector(".summary").innerHTML =
      line(t(hass, "curve_experiment"), tight) + line(t(hass, "curve_source"), sourceTight);
  }
}

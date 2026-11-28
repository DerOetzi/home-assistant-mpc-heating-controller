/*
 * Flow card.
 *
 * Everything the flow device and the hub decide about the shared flow, on
 * one card: warnings, how the flow target comes about (feedforward plus
 * feedback, curve plus correction to the offset) and the feedback count.
 * Entities are found by translation key; the card needs no configuration.
 */

import { DOMAIN } from "./const.js";
import { num, fixed, localeOf, isOn, withUnit } from "./format.js";
import { escapeHtml } from "./curve.js";
import {
  FLOW_CARD_ROLES,
  counterPosition,
  minutesSince,
  minutesUntil,
  resolveIntegration,
} from "./flow.js";
import { t } from "./translations.js";

export const FLOW_CARD_TAG = "heating-controller-flow-card";

const FLOW_STYLES = `
  ha-card { padding: 12px 16px; }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .title { font-size: 1.2em; font-weight: 500; flex: 1 1 auto; }
  .warn { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
    border-radius: 12px; font-size: 0.85em; cursor: pointer;
    background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
    color: var(--error-color, #db4437); }
  .warn ha-icon { --mdc-icon-size: 16px; }
  .big { display: flex; align-items: center; gap: 12px; margin: 8px 0 4px; }
  .big .value { font-size: 2em; font-weight: 400; cursor: pointer; white-space: nowrap; }
  .big .source { display: grid; gap: 2px; min-width: 0; color: var(--secondary-text-color); }
  .big .rooms { font-size: 0.85em; }
  .big .rooms b { font-weight: 500; color: var(--primary-text-color); }
  .section { margin-top: 12px; }
  .section h3 { display: flex; align-items: center; gap: 4px; font-size: 0.8em; font-weight: 500; margin: 0 0 4px;
    color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
  .chain + .chain { margin-top: 6px; }
  .chain { display: flex; align-items: center; gap: 4px 8px;
    font-variant-numeric: tabular-nums; }
  .term { display: grid; flex: 1 1 0; min-width: 0; text-align: center; cursor: pointer; padding: 2px 6px;
    border-radius: 6px; background: var(--secondary-background-color, rgba(127,127,127,0.1)); }
  .term.ok { background: color-mix(in srgb, var(--success-color, #43a047) 30%, transparent); }
  .term.pending { background: color-mix(in srgb, var(--warning-color, #ffa600) 30%, transparent); }
  .term .label { font-size: 0.7em; color: var(--secondary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .term > span:last-child { white-space: nowrap; }
  .op { flex: none; color: var(--secondary-text-color); font-size: 1.5em; line-height: 1; }
  .note { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 4px; }
  .bar { position: relative; height: 8px; border-radius: 4px; margin: 18px 0 4px;
    background: linear-gradient(to right,
      color-mix(in srgb, var(--info-color, #039be5) 35%, transparent),
      var(--divider-color) var(--zero),
      color-mix(in srgb, var(--error-color, #db4437) 35%, transparent)); }
  .bar .zero { position: absolute; top: -3px; bottom: -3px; width: 1px; left: var(--zero);
    background: var(--secondary-text-color); }
  .bar .mark { position: absolute; top: -4px; width: 4px; height: 16px; border-radius: 2px;
    margin-left: -2px; background: var(--primary-text-color); }
  .bar .mark span { position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%);
    font-size: 0.75em; white-space: nowrap; }
  .bar .lock { position: absolute; top: 0; bottom: 0; border-radius: 4px;
    background: repeating-linear-gradient(135deg,
      color-mix(in srgb, var(--card-background-color, #fff) 70%, transparent) 0 3px,
      transparent 3px 6px); }
  .scale { display: flex; justify-content: space-between; font-size: 0.75em;
    color: var(--secondary-text-color); }
  .scale ha-icon { --mdc-icon-size: 14px; vertical-align: -2px; }
  /* The heading is all capitals: centre on their height, not the line box. */
  .section h3 ha-icon { --mdc-icon-size: 14px; display: inline-flex; transform: translateY(-2px); }
  .facts { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.9em; }
  .facts > * { cursor: pointer; }
  .facts .k { color: var(--secondary-text-color); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .reset { font: inherit; font-size: 0.85em; cursor: pointer; padding: 2px 10px; border-radius: 12px;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  .empty { color: var(--secondary-text-color); }
`;

export class HeatingControllerFlowCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._html = null;
  }

  setConfig(config) {
    this._config = { ...(config ?? {}) };
    this._html = null;
    if (this._hass) this._render();
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // ---------- helpers ----------

  _n(value, digits = 1) {
    return num(value, digits, localeOf(this._hass));
  }

  _signed(value, digits = 1) {
    const number = Number(value);
    if (value === null || value === undefined || !Number.isFinite(number)) return "–";
    return `${number > 0 ? "+" : number < 0 ? "−" : "±"}${this._n(Math.abs(number), digits)}`;
  }

  // Box values: one decimal with unit, °C for flows and K for differences.
  _c(value) {
    return withUnit(fixed(value, 1, localeOf(this._hass)), "°C");
  }

  _k(value) {
    const number = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(number)) return "–";
    const sign = number > 0.04 ? "+" : number < -0.04 ? "−" : "±";
    return withUnit(`${sign}${fixed(Math.abs(number), 1, localeOf(this._hass))}`, "K");
  }

  _term(stateObj, label, value, state = "") {
    const entity = stateObj ? ` data-entity="${escapeHtml(stateObj.entity_id)}"` : "";
    return `<span class="term${state ? ` ${state}` : ""}"${entity}><span class="label">${escapeHtml(label)}</span><span>${value}</span></span>`;
  }

  _fact(stateObj, label, value) {
    const entity = stateObj ? ` data-entity="${escapeHtml(stateObj.entity_id)}"` : "";
    return `<span${entity}><span class="k">${escapeHtml(label)}</span> ${value}</span>`;
  }

  _ago(iso) {
    const minutes = minutesSince(iso, Date.now());
    if (minutes === null) return "–";
    const hass = this._hass;
    if (minutes < 60) return t(hass, "flow_ago_min", { value: minutes });
    if (minutes < 48 * 60) return t(hass, "flow_ago_h", { value: Math.round(minutes / 60) });
    return t(hass, "flow_ago_d", { value: Math.round(minutes / 1440) });
  }

  _state(stateObj) {
    if (!stateObj) return "–";
    return escapeHtml(this._hass.formatEntityState ? this._hass.formatEntityState(stateObj) : stateObj.state);
  }

  // ---------- render ----------

  _render() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const roles = resolveIntegration(hass, FLOW_CARD_ROLES);
    const title = escapeHtml(this._config.title ?? t(hass, "flow_title"));
    let content;
    if (!roles.target) {
      content = `<div class="empty">${t(hass, "flow_empty")}</div>`;
    } else {
      content = [
        this._summary(roles),
        this._derivation(roles),
      ].join("");
    }
    const html = `<style>${FLOW_STYLES}</style>
      <ha-card>
        <div class="head"><div class="title">${title}</div>${this._warnings(roles)}</div>
        ${content}
      </ha-card>`;
    if (html === this._html) return;
    this._html = html;
    this.shadowRoot.innerHTML = html;
    this._bind();
  }

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-entity]").forEach((element) =>
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        this._moreInfo(element.dataset.entity);
      })
    );
    root.querySelectorAll("[data-press]").forEach((element) =>
      element.addEventListener("click", () => {
        if (!window.confirm(t(this._hass, "flow_reset_confirm"))) return;
        this._hass.callService("button", "press", { entity_id: element.dataset.press });
      })
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

  // ---------- sections ----------

  _warnings(roles) {
    const hass = this._hass;
    const chips = [];
    const chip = (entityId, icon, text) =>
      chips.push(
        `<span class="warn"${entityId ? ` data-entity="${escapeHtml(entityId)}"` : ""}><ha-icon icon="${icon}"></ha-icon>${escapeHtml(text)}</span>`
      );
    if (roles.reachable && roles.reachable.state === "off") {
      chip(roles.reachable.entity_id, "mdi:lan-disconnect", t(hass, "flow_warn_unreachable"));
    }
    if (roles.curves && roles.curves.attributes?.outdoor_temperature_c == null) {
      chip(roles.curves.entity_id, "mdi:thermometer-off", t(hass, "flow_warn_outdoor"));
    }
    const cold = Object.values(hass.entities ?? {}).filter(
      (entry) =>
        entry.platform === DOMAIN &&
        entry.translation_key === "radiator_not_heating" &&
        isOn(hass.states[entry.entity_id])
    );
    if (cold.length) {
      chip(
        cold[0].entity_id,
        "mdi:radiator-off",
        t(hass, "flow_warn_radiator", { count: cold.length })
      );
    }
    return chips.join("");
  }

  _summary(roles) {
    const hass = this._hass;
    const attrs = roles.target.attributes ?? {};
    const source = attrs.source;
    // Line one: what sets the flow and why it holds; line two: the rooms behind it,
    // the one that sets it in bold.
    let rooms = [];
    if (source === "lead") {
      // The flow lead rooms, from the hold flow curves (each room carries its lead flag).
      const curveRooms = roles.curves?.attributes?.rooms;
      rooms = Array.isArray(curveRooms)
        ? curveRooms.filter((entry) => entry.lead).map((entry) => entry.name)
        : [attrs.room];
    } else if (source === "boost") {
      rooms = [attrs.boost_room];
    }
    rooms = rooms.filter(Boolean);
    const setter = source === "boost" ? attrs.boost_room : attrs.room;
    const sourceKey =
      source === "lead" && rooms.length === 1 ? "flow_source_lead_one" : `flow_source_${source}`;
    let sourceText = source ? t(hass, sourceKey) : "";
    if (attrs.reason) sourceText += ` · ${t(hass, `flow_reason_${attrs.reason}`)}`;
    const roomsText = rooms
      .map((name) => (name === setter && rooms.length > 1 ? `<b>${escapeHtml(name)}</b>` : escapeHtml(name)))
      .join(", ");
    const value = Number.isFinite(Number.parseFloat(roles.target.state))
      ? `${this._n(roles.target.state)} °C`
      : this._state(roles.target);
    return `<div class="big"><span class="value" data-entity="${escapeHtml(
      roles.target.entity_id
    )}">${value}</span><span class="source"><span>${sourceText}</span>${
      roomsText ? `<span class="rooms">${roomsText}</span>` : ""
    }</span></div>`;
  }

  _derivation(roles) {
    const hass = this._hass;
    const attrs = roles.target.attributes ?? {};
    const parts = [];
    // Target: feedforward plus feedback, beside a boost.
    parts.push(
      `<div class="chain">${this._term(roles.target, t(hass, "flow_feedforward"), this._c(attrs.feedforward_flow_c))}<span class="op">+</span>${this._term(
        roles.feedback,
        t(hass, "flow_feedback"),
        this._k(attrs.feedback_k)
      )}<span class="op">=</span>${this._term(
        roles.target,
        t(hass, "flow_lead_target"),
        this._c(Number(attrs.feedforward_flow_c) + Number(attrs.feedback_k))
      )}${
        attrs.boost_flow_c != null
          ? `<span class="op">·</span>${this._term(
              roles.target,
              t(hass, "flow_boost", { room: attrs.boost_room ?? "" }),
              this._c(attrs.boost_flow_c)
            )}`
          : ""
      }</div>`
    );
    // The valve feedback right under the term it explains.
    parts.push(this._feedbackSection(roles));
    parts.push(this._control(roles));
    const frozen = attrs.measured_correction_frozen_reason;
    const notes = [];
    if (frozen) notes.push(t(hass, "flow_correction_frozen", { reason: t(hass, `flow_correction_${frozen}`) }));
    const write = roles.actuatorTarget?.attributes ?? {};
    if (write.last_error) notes.push(`${t(hass, "flow_write_error")}: ${escapeHtml(write.last_error)}`);
    if (write.external_change) notes.push(t(hass, "flow_external_change"));
    return `<div class="section"><h3>${t(hass, "flow_section_derivation")}</h3>${parts.join("")}${
      notes.length ? `<div class="note">${notes.join(" · ")}</div>` : ""
    }</div>`;
  }

  // Control: curve plus measured correction is what the heat source gives
  // without offset; the target minus that is the need, rounded up to the offset.
  _control(roles) {
    const hass = this._hass;
    const attrs = roles.target.attributes ?? {};
    const target = Number.parseFloat(roles.target.state);
    const curve = Number.parseFloat(roles.curve?.state);
    const correction = Number(attrs.measured_correction_k ?? 0);
    const op = (sign) => `<span class="op">${sign}</span>`;
    const rows = [];
    let need = null;
    if (Number.isFinite(curve) && Number.isFinite(target)) {
      const base = curve + correction;
      need = target - base;
      rows.push(
        `<div class="chain">${this._term(roles.curve, t(hass, "flow_curve"), this._c(curve))}${op("+")}${this._term(
          roles.target,
          t(hass, "flow_correction"),
          this._k(correction)
        )}${op("=")}${this._term(roles.target, t(hass, "flow_base"), this._c(base))}</div>`
      );
    }
    const wanted = roles.actuatorTarget?.state;
    const actual = roles.actuatorActual?.state;
    const same =
      Number.isFinite(Number(wanted)) &&
      Number.isFinite(Number(actual)) &&
      Math.abs(Number(wanted) - Number(actual)) < 1e-6;
    // The actual offset goes into the label while it differs, so the box keeps its width.
    const offsetLabel =
      same || !roles.actuatorActual
        ? t(hass, "flow_offset")
        : `${t(hass, "flow_offset")} · ${t(hass, "flow_actual")} ${this._k(actual)}`;
    const offset = this._term(
      roles.actuatorTarget,
      offsetLabel,
      this._k(wanted),
      same ? "ok" : "pending"
    );
    rows.push(
      `<div class="chain">${
        need === null
          ? this._term(roles.target, t(hass, "flow_lead_target"), this._c(target))
          : `${this._term(roles.target, t(hass, "flow_target_short"), this._c(target))}${op("−")}${this._term(
              roles.target,
              t(hass, "flow_base"),
              this._c(curve + correction)
            )}${op("=")}${this._term(roles.target, t(hass, "flow_need"), this._k(need))}`
      }${op("→")}${offset}</div>`
    );
    return `<div class="section"><h3>${t(hass, "flow_section_control")}</h3>${rows.join("")}</div>`;
  }

  _feedbackSection(roles) {
    const hass = this._hass;
    const feedback = roles.feedback;
    if (!feedback) return "";
    const attrs = feedback.attributes ?? {};
    const upAt = attrs.step_up_at_min ?? -30;
    const downAt = attrs.step_down_at_min ?? (attrs.night ? 90 : 60);
    const position = counterPosition(attrs.flow_timer_min, upAt, downAt);
    const zero = counterPosition(0, upAt, downAt);
    // A step's lock closes its own side of the bar: hatched, with a lock and
    // the time left instead of where the count would step.
    const locked = Number(attrs.locked_direction) || 0;
    const lockLeft = minutesUntil(attrs.locked_until, Date.now());
    const lockText = `<ha-icon icon="mdi:lock"></ha-icon> ${
      lockLeft === null ? t(hass, "flow_locked") : t(hass, "flow_locked_left", { value: lockLeft })
    }`;
    const lockBand =
      locked > 0
        ? `<span class="lock" style="left:${zero}%;right:0"></span>`
        : locked < 0
          ? `<span class="lock" style="left:0;right:${100 - zero}%"></span>`
          : "";
    const bar =
      position === null
        ? ""
        : `<div class="bar" style="--zero:${zero}%">${lockBand}<span class="zero"></span><span class="mark" style="left:${position}%"><span>${this._n(
            Math.abs(attrs.flow_timer_min),
            0
          )} min</span></span></div>
        <div class="scale"><span>${
          locked < 0 ? lockText : t(hass, "flow_step_down", { value: Math.abs(downAt) })
        }</span><span>${locked > 0 ? lockText : t(hass, "flow_step_up", { value: Math.abs(upAt) })}</span></div>`;
    const facts = [];
    if (attrs.lead_room) {
      // The room with the widest open valve, which the count follows.
      facts.push(
        `<span data-entity="${escapeHtml(feedback.entity_id)}">${escapeHtml(attrs.lead_room)} ${this._n(
          attrs.lead_demand_pct,
          0
        )} %${
          attrs.lead_deficit_c == null
            ? ""
            : ` · ${t(hass, attrs.lead_deficit_c > 0 ? "flow_deficit_short" : "flow_deficit_over", {
                value: this._n(Math.abs(attrs.lead_deficit_c), 2),
              })}`
        }</span>`
      );
    }
    if (attrs.frozen) {
      facts.push(
        this._fact(feedback, t(hass, "flow_frozen"), t(hass, `flow_freeze_${attrs.frozen_reason}`))
      );
    }
    if (attrs.catch_up_until) {
      facts.push(
        this._fact(
          feedback,
          t(hass, "flow_catch_up"),
          `${t(hass, `flow_freeze_${attrs.catch_up_reason}`)}`
        )
      );
    }
    facts.push(this._fact(feedback, t(hass, "flow_last_change"), this._ago(attrs.last_change)));
    // Time without a room against it towards the next step back to zero.
    if (attrs.leak_at_min != null && Number(feedback.state) !== 0) {
      facts.push(
        this._fact(
          feedback,
          t(hass, "flow_leak"),
          t(hass, "flow_leak_value", {
            value: this._n(attrs.leak_min ?? 0, 0),
            at: this._n(attrs.leak_at_min, 0),
          })
        )
      );
    }
    if (attrs.room_variant_up != null) {
      facts.push(
        this._fact(
          feedback,
          t(hass, "flow_room_variant"),
          `↑${attrs.room_variant_up} ↓${attrs.room_variant_down ?? 0}`
        )
      );
    }
    const reset = roles.reset
      ? `<button type="button" class="reset" data-press="${escapeHtml(roles.reset.entity_id)}">${t(
          hass,
          "flow_reset"
        )}</button>`
      : "";
    const night = attrs.night
      ? ` <ha-icon icon="mdi:weather-night" title="${escapeHtml(t(hass, "flow_night"))}"></ha-icon>`
      : "";
    return `<div class="section"><h3>${t(hass, "flow_section_feedback")}${night}</h3>${bar}<div class="facts" style="margin-top:6px">${facts.join("")}</div>${
      reset ? `<div class="row" style="margin-top:6px">${reset}</div>` : ""
    }</div>`;
  }
}

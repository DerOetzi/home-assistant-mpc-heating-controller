/*
 * Flow card.
 *
 * Everything the flow device and the hub decide about the shared flow, on
 * one card: warnings, how the flow target comes about (feedforward plus
 * feedback, curve plus correction to the offset), the feedback count, the
 * switches, the heating season and, folded away, the settings. Entities are
 * found by translation key; the card needs no configuration.
 */

import { DOMAIN } from "./const.js";
import { num, localeOf, isOn } from "./format.js";
import { escapeHtml } from "./curve.js";
import {
  FLOW_CARD_ROLES,
  FLOW_SETTING_KEYS,
  counterPosition,
  minutesSince,
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
  .big { display: flex; align-items: baseline; gap: 10px; margin: 8px 0 4px; flex-wrap: wrap; }
  .big .value { font-size: 2em; font-weight: 400; cursor: pointer; }
  .big .source { color: var(--secondary-text-color); }
  .section { margin-top: 12px; }
  .section h3 { font-size: 0.8em; font-weight: 500; margin: 0 0 4px;
    color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
  .chain { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 6px;
    font-variant-numeric: tabular-nums; }
  .term { display: inline-grid; text-align: center; cursor: pointer; padding: 2px 6px;
    border-radius: 6px; background: var(--secondary-background-color, rgba(127,127,127,0.1)); }
  .term .label { font-size: 0.7em; color: var(--secondary-text-color); }
  .op { color: var(--secondary-text-color); }
  .note { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 4px; }
  .bar { position: relative; height: 8px; border-radius: 4px; margin: 18px 0 4px;
    background: linear-gradient(to right,
      color-mix(in srgb, var(--error-color, #db4437) 35%, transparent),
      var(--divider-color) var(--zero),
      color-mix(in srgb, var(--info-color, #039be5) 35%, transparent)); }
  .bar .zero { position: absolute; top: -3px; bottom: -3px; width: 1px; left: var(--zero);
    background: var(--secondary-text-color); }
  .bar .mark { position: absolute; top: -4px; width: 4px; height: 16px; border-radius: 2px;
    margin-left: -2px; background: var(--primary-text-color); }
  .bar .mark span { position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%);
    font-size: 0.75em; white-space: nowrap; }
  .scale { display: flex; justify-content: space-between; font-size: 0.75em;
    color: var(--secondary-text-color); }
  .facts { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.9em; }
  .facts > * { cursor: pointer; }
  .facts .k { color: var(--secondary-text-color); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .toggle { font: inherit; display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    padding: 4px 12px; border-radius: 16px; border: 1px solid var(--divider-color);
    background: none; color: var(--secondary-text-color); }
  .toggle ha-icon { --mdc-icon-size: 18px; }
  .toggle.on { color: var(--primary-text-color); border-color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 16%, transparent); }
  .reset { font: inherit; font-size: 0.85em; cursor: pointer; padding: 2px 10px; border-radius: 12px;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  details summary { cursor: pointer; font-size: 0.8em; font-weight: 500;
    color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
  .settings { display: grid; grid-template-columns: 1fr auto; gap: 6px 12px; align-items: center;
    margin-top: 6px; }
  .settings label { cursor: pointer; }
  .settings input { font: inherit; width: 6em; text-align: right; padding: 2px 4px;
    color: var(--primary-text-color); background: var(--card-background-color, transparent);
    border: 1px solid var(--divider-color); border-radius: 4px; }
  .settings .unit { color: var(--secondary-text-color); font-size: 0.85em; margin-left: 4px; }
  .empty { color: var(--secondary-text-color); }
`;

const SETTING_ROLES = Object.fromEntries(FLOW_SETTING_KEYS.map((key) => [key, key]));

export class HeatingControllerFlowCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._html = null;
    this._settingsOpen = false;
  }

  setConfig(config) {
    this._config = { ...(config ?? {}) };
    this._html = null;
    if (this._hass) this._render();
  }

  getCardSize() {
    return 9;
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

  _term(stateObj, label, value) {
    const entity = stateObj ? ` data-entity="${escapeHtml(stateObj.entity_id)}"` : "";
    return `<span class="term"${entity}><span class="label">${escapeHtml(label)}</span><span>${value}</span></span>`;
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
    // Do not throw away a value being typed.
    const active = this.shadowRoot.activeElement;
    if (active && active.tagName === "INPUT") return;
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
        this._feedbackSection(roles),
        this._switches(roles),
        this._season(roles),
        this._settings(),
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
    root.querySelectorAll("[data-toggle]").forEach((element) =>
      element.addEventListener("click", () =>
        this._hass.callService("switch", "toggle", { entity_id: element.dataset.toggle })
      )
    );
    root.querySelectorAll("[data-press]").forEach((element) =>
      element.addEventListener("click", () => {
        if (!window.confirm(t(this._hass, "flow_reset_confirm"))) return;
        this._hass.callService("button", "press", { entity_id: element.dataset.press });
      })
    );
    root.querySelectorAll("input[data-number]").forEach((element) =>
      element.addEventListener("change", () => {
        const value = Number(element.value);
        if (!Number.isFinite(value)) return;
        this._hass.callService("number", "set_value", {
          entity_id: element.dataset.number,
          value,
        });
        element.blur();
      })
    );
    const details = root.querySelector("details");
    if (details) {
      details.addEventListener("toggle", () => {
        this._settingsOpen = details.open;
      });
    }
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
    let sourceText = source ? t(hass, `flow_source_${source}`) : "";
    const room = source === "boost" ? attrs.boost_room : attrs.room;
    if (room && (source === "lead" || source === "boost")) sourceText += ` · ${escapeHtml(room)}`;
    if (attrs.reason) sourceText += ` · ${t(hass, `flow_reason_${attrs.reason}`)}`;
    const value = Number.isFinite(Number.parseFloat(roles.target.state))
      ? `${this._n(roles.target.state)} °C`
      : this._state(roles.target);
    return `<div class="big"><span class="value" data-entity="${escapeHtml(
      roles.target.entity_id
    )}">${value}</span><span class="source">${sourceText}</span></div>`;
  }

  _derivation(roles) {
    const hass = this._hass;
    const attrs = roles.target.attributes ?? {};
    const parts = [];
    // Target: feedforward plus feedback (or the boost / threshold that won).
    parts.push(
      `<div class="chain">${this._term(roles.target, t(hass, "flow_feedforward"), this._n(attrs.feedforward_flow_c))}<span class="op">+</span>${this._term(
        roles.feedback,
        t(hass, "flow_feedback"),
        this._signed(attrs.feedback_k)
      )}<span class="op">=</span>${this._term(
        roles.target,
        t(hass, "flow_lead_target"),
        this._n(Number(attrs.feedforward_flow_c) + Number(attrs.feedback_k))
      )}${
        attrs.boost_flow_c != null
          ? `<span class="op">·</span>${this._term(
              roles.target,
              t(hass, "flow_boost", { room: attrs.boost_room ?? "" }),
              this._n(attrs.boost_flow_c)
            )}`
          : ""
      }<span class="op">·</span>${this._term(
        roles.threshold ?? roles.target,
        t(hass, "flow_threshold"),
        this._n(attrs.threshold_c)
      )}</div>`
    );
    // Offset: target against curve plus correction.
    const correction = attrs.measured_correction_k;
    const frozen = attrs.measured_correction_frozen_reason;
    parts.push(
      `<div class="chain" style="margin-top:6px">${this._term(roles.curve, t(hass, "flow_curve"), this._n(roles.curve?.state))}<span class="op">+</span>${this._term(
        roles.target,
        t(hass, "flow_correction"),
        this._signed(correction)
      )}<span class="op">→</span>${this._term(
        roles.actuatorTarget,
        t(hass, "flow_offset"),
        this._signed(roles.actuatorTarget?.state, 0)
      )}${
        roles.actuatorActual
          ? `<span class="op">/</span>${this._term(
              roles.actuatorActual,
              t(hass, "flow_offset_actual"),
              this._signed(roles.actuatorActual.state, 0)
            )}`
          : ""
      }<span class="op">→</span>${this._term(
        roles.effectiveCurve,
        t(hass, "flow_effective"),
        this._n(roles.effectiveCurve?.state)
      )}${
        roles.reserve
          ? `<span class="op">·</span>${this._term(
              roles.reserve,
              t(hass, "flow_reserve"),
              `${this._signed(roles.reserve.state)} K`
            )}`
          : ""
      }</div>`
    );
    const notes = [];
    if (frozen) notes.push(t(hass, "flow_correction_frozen", { reason: t(hass, `flow_correction_${frozen}`) }));
    const write = roles.actuatorTarget?.attributes ?? {};
    if (write.last_error) notes.push(`${t(hass, "flow_write_error")}: ${escapeHtml(write.last_error)}`);
    if (write.external_change) notes.push(t(hass, "flow_external_change"));
    return `<div class="section"><h3>${t(hass, "flow_section_derivation")}</h3>${parts.join("")}${
      notes.length ? `<div class="note">${notes.join(" · ")}</div>` : ""
    }</div>`;
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
    const bar =
      position === null
        ? ""
        : `<div class="bar" style="--zero:${zero}%"><span class="zero"></span><span class="mark" style="left:${position}%"><span>${this._signed(
            attrs.flow_timer_min,
            0
          )} min</span></span></div>
        <div class="scale"><span>${t(hass, "flow_step_up", { value: Math.abs(upAt) })}</span><span>${t(
          hass,
          "flow_step_down",
          { value: downAt }
        )}</span></div>`;
    const facts = [];
    if (attrs.lead_room) {
      facts.push(
        this._fact(
          feedback,
          t(hass, "flow_lead_room"),
          `${escapeHtml(attrs.lead_room)} ${this._n(attrs.lead_demand_pct, 0)} %${
            attrs.lead_deficit_c == null
              ? ""
              : ` · ${t(hass, attrs.lead_deficit_c > 0 ? "flow_deficit_short" : "flow_deficit_over", {
                  value: this._n(Math.abs(attrs.lead_deficit_c), 2),
                })}`
          }`
        )
      );
    }
    if (attrs.locked_direction) {
      facts.push(
        this._fact(
          feedback,
          t(hass, "flow_locked"),
          t(hass, attrs.locked_direction > 0 ? "flow_locked_up" : "flow_locked_down")
        )
      );
    }
    if (attrs.night) facts.push(this._fact(feedback, t(hass, "flow_night"), "✓"));
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
    return `<div class="section"><h3>${t(hass, "flow_section_feedback")} ${this._signed(
      feedback.state
    )} K</h3>${bar}<div class="facts" style="margin-top:6px">${facts.join("")}</div>${
      reset ? `<div class="row" style="margin-top:6px">${reset}</div>` : ""
    }</div>`;
  }

  _switches(roles) {
    const hass = this._hass;
    const toggle = (stateObj, icon, key) => {
      if (!stateObj) return "";
      const on = isOn(stateObj);
      return `<button type="button" class="toggle${on ? " on" : ""}" data-toggle="${escapeHtml(
        stateObj.entity_id
      )}" aria-pressed="${on}"><ha-icon icon="${icon}"></ha-icon>${t(hass, key)}</button>`;
    };
    const budget = roles.budget;
    const budgetAttrs = budget?.attributes ?? {};
    const budgetText = budget
      ? this._fact(
          budget,
          t(hass, "flow_budget"),
          `${this._state(budget)}${
            budgetAttrs.max_writes_per_day != null
              ? ` · ${t(hass, "flow_budget_own", {
                  own: budgetAttrs.own_writes_today ?? 0,
                  max: budgetAttrs.max_writes_per_day,
                })}`
              : ""
          }`
        )
      : "";
    return `<div class="section"><h3>${t(hass, "flow_section_switches")}</h3><div class="row">${toggle(
      roles.write,
      "mdi:pencil",
      "flow_write"
    )}${toggle(roles.comfort, "mdi:sofa", "flow_comfort")}${toggle(
      roles.pvBoost,
      "mdi:solar-power",
      "flow_pv_boost"
    )}</div>${budgetText ? `<div class="facts" style="margin-top:6px">${budgetText}</div>` : ""}</div>`;
  }

  _season(roles) {
    const hass = this._hass;
    const facts = [];
    const due = (stateObj, key) => {
      if (stateObj && isOn(stateObj)) facts.push(this._fact(stateObj, t(hass, key), "✓"));
    };
    if (roles.leadDemand) {
      facts.push(
        this._fact(
          roles.leadDemand,
          t(hass, "flow_lead_demand"),
          t(hass, isOn(roles.leadDemand) ? "flow_yes" : "flow_no")
        )
      );
    }
    due(roles.seasonOn, "flow_season_on_due");
    due(roles.seasonOff, "flow_season_off_due");
    if (roles.lastDemand) {
      facts.push(
        this._fact(roles.lastDemand, t(hass, "flow_last_demand"), this._ago(roles.lastDemand.state))
      );
    }
    if (roles.demandHours) {
      facts.push(
        this._fact(roles.demandHours, t(hass, "flow_demand_hours"), `${this._n(roles.demandHours.state)} h`)
      );
    }
    if (!facts.length) return "";
    return `<div class="section"><h3>${t(hass, "flow_section_season")}</h3><div class="facts">${facts.join(
      ""
    )}</div></div>`;
  }

  _settings() {
    const hass = this._hass;
    const numbers = resolveIntegration(hass, SETTING_ROLES);
    const rows = FLOW_SETTING_KEYS.filter((key) => numbers[key])
      .map((key) => {
        const stateObj = numbers[key];
        const attrs = stateObj.attributes ?? {};
        const name = t(hass, `flow_setting_${key.replace(/^\w+:/, "")}`);
        const unit = attrs.unit_of_measurement ? `<span class="unit">${escapeHtml(attrs.unit_of_measurement)}</span>` : "";
        const limits = ["min", "max", "step"]
          .filter((limit) => attrs[limit] != null)
          .map((limit) => ` ${limit}="${attrs[limit]}"`)
          .join("");
        return `<label data-entity="${escapeHtml(stateObj.entity_id)}">${name}</label><span><input type="number" data-number="${escapeHtml(
          stateObj.entity_id
        )}" value="${escapeHtml(stateObj.state)}"${limits}>${unit}</span>`;
      })
      .join("");
    if (!rows) return "";
    return `<div class="section"><details${this._settingsOpen ? " open" : ""}><summary>${t(
      hass,
      "flow_section_settings"
    )}</summary><div class="settings">${rows}</div></details></div>`;
  }
}

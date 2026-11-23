/*
 * Heating season card.
 *
 * Whether the lead rooms ask for heat, how long over the last 24 h, when the
 * hub switches the heat source on or off, the hub's automatic switch and a
 * hand switch for the heat source (heat/off). A hand switch turns the
 * automatic off. Entities are found by translation key; the card needs no
 * configuration.
 */

import { fixed, localeOf, withUnit, num } from "./format.js";
import { escapeHtml } from "./curve.js";
import { minutesSince, resolveIntegration } from "./flow.js";
import { SEASON_CARD_ROLES, hoursPosition, hoursUntilOff } from "./season.js";
import { t } from "./translations.js";

export const SEASON_CARD_TAG = "heating-controller-season-card";

const SEASON_STYLES = `
  ha-card { padding: 12px 16px; }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .title { font-size: 1.2em; font-weight: 500; flex: 1 1 auto; }
  .chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
    border-radius: 12px; font-size: 0.85em; cursor: pointer;
    background: color-mix(in srgb, var(--warning-color, #ffa600) 25%, transparent); }
  .chip ha-icon { --mdc-icon-size: 16px; }
  .toggle { display: inline-flex; border: 1px solid var(--divider-color); border-radius: 16px;
    overflow: hidden; }
  .toggle button { font: inherit; font-size: 0.9em; padding: 4px 12px; border: none;
    background: none; color: var(--primary-text-color); cursor: pointer; }
  .toggle button.active { cursor: default; color: var(--text-primary-color, #fff); }
  .toggle button.active.heat { background: var(--state-climate-heat-color, #ff8100); }
  .toggle button.active.off { background: var(--disabled-color, #9e9e9e); }
  .auto { display: inline-flex; align-items: center; gap: 4px; font: inherit; font-size: 0.9em;
    padding: 4px 10px; border: 1px solid var(--divider-color); border-radius: 16px;
    background: none; color: var(--secondary-text-color); cursor: pointer; }
  .auto ha-icon { --mdc-icon-size: 16px; }
  .auto.on { color: var(--text-primary-color, #fff); border-color: transparent;
    background: var(--primary-color, #03a9f4); }
  .section { margin-top: 12px; }
  .section h3 { font-size: 0.8em; font-weight: 500; margin: 0 0 4px;
    color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.04em; }
  .chain { display: flex; align-items: center; gap: 4px 8px; font-variant-numeric: tabular-nums; }
  .term { display: grid; flex: 1 1 0; min-width: 0; text-align: center; cursor: pointer; padding: 2px 6px;
    border-radius: 6px; background: var(--secondary-background-color, rgba(127,127,127,0.1)); }
  .term.ok { background: color-mix(in srgb, var(--success-color, #43a047) 30%, transparent); }
  .term.pending { background: color-mix(in srgb, var(--warning-color, #ffa600) 30%, transparent); }
  .term .label { font-size: 0.7em; color: var(--secondary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .term > span:last-child { white-space: nowrap; }
  .op { flex: none; color: var(--secondary-text-color); font-size: 1.5em; line-height: 1; }
  .rooms { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 4px; }
  .rooms b { font-weight: 500; color: var(--primary-text-color); }
  .bar { position: relative; height: 8px; border-radius: 4px; margin: 18px 0 4px; cursor: pointer;
    background: var(--divider-color); }
  .bar .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 4px;
    background: color-mix(in srgb, var(--state-climate-heat-color, #ff8100) 60%, transparent); }
  .bar .tick { position: absolute; top: -3px; bottom: -3px; width: 2px; margin-left: -1px;
    background: var(--secondary-text-color); }
  .bar .tick span { position: absolute; top: 14px; left: 50%; transform: translateX(-50%);
    font-size: 0.75em; white-space: nowrap; color: var(--secondary-text-color); }
  .bar .mark { position: absolute; top: -4px; width: 4px; height: 16px; border-radius: 2px;
    margin-left: -2px; background: var(--primary-text-color); }
  .bar .mark span { position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%);
    font-size: 0.75em; white-space: nowrap; }
  .facts { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.9em; margin-top: 22px; }
  .facts > * { cursor: pointer; }
  .facts .k { color: var(--secondary-text-color); }
  .empty { color: var(--secondary-text-color); }
`;

export class HeatingControllerSeasonCard extends HTMLElement {
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
    return 5;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // ---------- helpers ----------

  _c(value) {
    return withUnit(fixed(value, 1, localeOf(this._hass)), "°C");
  }

  _h(value) {
    return withUnit(fixed(value, 1, localeOf(this._hass)), "h");
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

  // "3 h ago" for a past time, by minutes, hours or days.
  _ago(iso) {
    const minutes = minutesSince(iso, Date.now());
    if (minutes === null) return "–";
    return this._span(minutes, "season_ago");
  }

  _span(minutes, prefix) {
    const hass = this._hass;
    if (minutes < 60) return t(hass, `${prefix}_min`, { value: minutes });
    if (minutes < 48 * 60) return t(hass, `${prefix}_h`, { value: Math.round(minutes / 60) });
    return t(hass, `${prefix}_d`, { value: num(minutes / 1440, 1, localeOf(hass)) });
  }

  // ---------- render ----------

  _render() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const roles = resolveIntegration(hass, SEASON_CARD_ROLES);
    const title = escapeHtml(this._config.title ?? t(hass, "season_title"));
    const season = roles.switchOn ?? roles.switchOff;
    const heatSourceId = season?.attributes?.heat_source_entity;
    const heatSource = heatSourceId ? hass.states[heatSourceId] : undefined;
    let content;
    if (!season) {
      content = `<div class="empty">${t(hass, "season_empty")}</div>`;
    } else {
      content = [this._limit(roles), this._demand(roles, season)].join("");
    }
    const html = `<style>${SEASON_STYLES}</style>
      <ha-card>
        <div class="head"><div class="title">${title}</div>${this._recommendation(roles)}${this._auto(
          roles.auto
        )}${this._toggle(heatSource, roles.auto)}</div>
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
    root.querySelectorAll("[data-mode]").forEach((element) =>
      element.addEventListener("click", () => {
        const mode = element.dataset.mode;
        const auto = this._hass.states[element.dataset.auto ?? ""]?.state === "on";
        const question = t(this._hass, `season_confirm_${mode}`);
        if (!window.confirm(auto ? `${question} ${t(this._hass, "season_confirm_auto_off")}` : question)) return;
        this._hass.callService("climate", "set_hvac_mode", {
          entity_id: element.dataset.target,
          hvac_mode: mode,
        });
      })
    );
    root.querySelectorAll("[data-auto-toggle]").forEach((element) =>
      element.addEventListener("click", () => {
        this._hass.callService("switch", "toggle", { entity_id: element.dataset.autoToggle });
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

  _recommendation(roles) {
    const hass = this._hass;
    const chip = (stateObj, icon, key) =>
      `<span class="chip" data-entity="${escapeHtml(stateObj.entity_id)}"><ha-icon icon="${icon}"></ha-icon>${escapeHtml(
        t(hass, key)
      )}</span>`;
    const auto = roles.auto?.state === "on";
    if (roles.switchOn?.state === "on")
      return chip(roles.switchOn, "mdi:radiator", auto ? "season_switching_on" : "season_recommend_on");
    if (roles.switchOff?.state === "on")
      return chip(roles.switchOff, "mdi:radiator-off", auto ? "season_switching_off" : "season_recommend_off");
    return "";
  }

  // The hub's automatic switch; a click toggles it.
  _auto(auto) {
    if (!auto) return "";
    const on = auto.state === "on";
    return `<button type="button" class="auto${on ? " on" : ""}" data-auto-toggle="${escapeHtml(
      auto.entity_id
    )}" title="${escapeHtml(t(this._hass, on ? "season_auto_on" : "season_auto_off"))}"><ha-icon icon="mdi:thermostat-auto"></ha-icon>${escapeHtml(
      t(this._hass, "season_auto")
    )}</button>`;
  }

  // Heat | Off for the heat source; the current mode is marked, the other asks first.
  _toggle(heatSource, auto) {
    if (!heatSource) return "";
    const hass = this._hass;
    const id = escapeHtml(heatSource.entity_id);
    const button = (mode) => {
      const active = heatSource.state === mode;
      return `<button type="button" class="${mode}${active ? " active" : ""}"${
        active
          ? ` data-entity="${id}"`
          : ` data-mode="${mode}" data-target="${id}"${auto ? ` data-auto="${escapeHtml(auto.entity_id)}"` : ""}`
      }>${escapeHtml(t(hass, `season_mode_${mode}`))}</button>`;
    };
    return `<span class="toggle">${button("heat")}${button("off")}</span>`;
  }

  // Heating limit: the lead rooms' hold flow against the threshold decides the demand.
  _limit(roles) {
    const hass = this._hass;
    const demand = roles.demand;
    const attrs = demand?.attributes ?? {};
    const hold = roles.leadHold ?? demand;
    const on = demand?.state === "on";
    const op = (sign) => `<span class="op">${sign}</span>`;
    const holdValue = roles.leadHold ? roles.leadHold.state : attrs.hold_flow_temperature_c;
    const chain = `<div class="chain">${this._term(hold, t(hass, "season_hold_flow"), this._c(holdValue))}${op(
      on ? "≥" : "<"
    )}${this._term(demand, t(hass, "season_threshold"), this._c(attrs.flow_threshold_c))}${op("→")}${this._term(
      demand,
      t(hass, "season_demand"),
      escapeHtml(t(hass, on ? "season_demand_on" : "season_demand_off")),
      on ? "ok" : ""
    )}</div>`;
    const rooms = roles.leadHold?.attributes?.rooms ?? attrs.rooms ?? [];
    const setter = roles.leadHold?.attributes?.room;
    const names = Array.isArray(rooms) ? rooms.filter(Boolean) : [];
    const roomsText = names
      .map((name) => (name === setter && names.length > 1 ? `<b>${escapeHtml(name)}</b>` : escapeHtml(name)))
      .join(", ");
    const label = t(hass, names.length === 1 ? "season_lead_one" : "season_lead");
    const facts = [];
    if (roles.reserve) {
      facts.push(this._fact(roles.reserve, t(hass, "season_reserve"), this._k(roles.reserve.state)));
    }
    if (demand?.last_changed) {
      facts.push(this._fact(demand, t(hass, "season_since"), this._ago(demand.last_changed)));
    }
    return `<div class="section"><h3>${t(hass, "season_section_limit")}</h3>${chain}${
      roomsText ? `<div class="rooms">${escapeHtml(label)}: ${roomsText}</div>` : ""
    }${facts.length ? `<div class="facts" style="margin-top:6px">${facts.join("")}</div>` : ""}</div>`;
  }

  // Demand hours of the last 24 h against the hold and switch-on thresholds,
  // and when switching off would be recommended.
  _demand(roles, season) {
    const hass = this._hass;
    const attrs = season.attributes ?? {};
    const hours = roles.hours ? roles.hours.state : attrs.demand_hours_24h;
    const position = hoursPosition(hours);
    const tick = (stateObj, value, key) => {
      const at = hoursPosition(value);
      return at === null
        ? ""
        : `<span class="tick" style="left:${at}%"${
            stateObj ? ` data-entity="${escapeHtml(stateObj.entity_id)}"` : ""
          }><span>${escapeHtml(t(hass, key, { value: this._h(value) }))}</span></span>`;
    };
    const bar =
      position === null
        ? ""
        : `<div class="bar"${roles.hours ? ` data-entity="${escapeHtml(roles.hours.entity_id)}"` : ""}><span class="fill" style="width:${position}%"></span>${tick(
            roles.holdThreshold,
            attrs.hold_threshold_h,
            "season_tick_hold"
          )}${tick(roles.onThreshold, attrs.on_threshold_h, "season_tick_on")}<span class="mark" style="left:${position}%"><span>${this._h(
            hours
          )}</span></span></div>`;
    const facts = [];
    facts.push(
      this._fact(roles.lastDemand ?? season, t(hass, "season_last_demand"), this._ago(attrs.last_demand))
    );
    if (attrs.heating_on) {
      const until = hoursUntilOff(attrs.last_demand, attrs.off_after_h, Date.now());
      if (until !== null) {
        facts.push(
          this._fact(
            roles.offAfter ?? season,
            t(hass, roles.auto?.state === "on" ? "season_off_auto_at" : "season_off_at"),
            until <= 0 ? t(hass, "season_off_due") : this._span(Math.round(until * 60), "season_in")
          )
        );
      }
    }
    return `<div class="section"><h3>${t(hass, "season_section_demand")}</h3>${bar}<div class="facts">${facts.join(
      ""
    )}</div></div>`;
  }
}

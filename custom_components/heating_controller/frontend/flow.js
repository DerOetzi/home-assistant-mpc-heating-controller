// DOM-free logic of the flow card: which entities it shows and how the
// feedback count maps onto its bar.

import { DOMAIN } from "./const.js";

// translation_key -> role, for the flow and hub devices (one each). A key
// two platforms share takes its domain in front: "sensor:flow_threshold".
export const FLOW_CARD_ROLES = {
  flow_target: "target",
  flow_feedback: "feedback",
  flow_curve: "curve",
  flow_effective_curve: "effectiveCurve",
  flow_actuator_target: "actuatorTarget",
  flow_actuator_actual: "actuatorActual",
  "sensor:flow_threshold": "threshold",
  hold_flow_curves: "curves",
  hold_flow_reserve: "reserve",
  flow_write: "write",
  flow_feedback_reset: "reset",
  house_comfort: "comfort",
  pv_boost: "pvBoost",
  write_budget: "budget",
  heat_source_reachable: "reachable",
  lead_heating_demand: "leadDemand",
  heating_season_switch_on: "seasonOn",
  heating_season_switch_off: "seasonOff",
  heating_season_last_demand: "lastDemand",
  heating_demand_hours_24h: "demandHours",
};

// The numbers under "Settings", in the order shown.
export const FLOW_SETTING_KEYS = [
  "flow_hysteresis",
  "flow_hold_time_min",
  "flow_charge_raise_max",
  "number:flow_threshold",
  "heating_season_on_threshold_h",
  "heating_season_hold_threshold_h",
  "heating_season_off_after_h",
];

// Where the count sits on a bar from the step up (left, negative) to the
// step down (right): 0..100, with zero at the share the up side takes.
export const counterPosition = (timerMin, upAtMin, downAtMin) => {
  const up = Math.abs(Number(upAtMin));
  const down = Math.abs(Number(downAtMin));
  const value = Number(timerMin);
  if (timerMin == null || !(up > 0) || !(down > 0) || !Number.isFinite(value)) return null;
  const clamped = Math.max(-up, Math.min(down, value));
  return ((clamped + up) / (up + down)) * 100;
};

// Minutes since an ISO time, or null.
export const minutesSince = (iso, nowMs) => {
  const then = Date.parse(iso ?? "");
  if (!Number.isFinite(then)) return null;
  return Math.max(0, Math.round((nowMs - then) / 60000));
};

// The states of the integration's single-instance entities, by role.
export const resolveIntegration = (hass, roleMap) => {
  const found = {};
  for (const entry of Object.values(hass?.entities ?? {})) {
    if (entry.platform !== DOMAIN || !entry.translation_key) continue;
    const domain = entry.entity_id.split(".")[0];
    const role =
      roleMap[`${domain}:${entry.translation_key}`] ?? roleMap[entry.translation_key];
    const stateObj = hass.states?.[entry.entity_id];
    if (role && stateObj && !found[role]) found[role] = stateObj;
  }
  return found;
};

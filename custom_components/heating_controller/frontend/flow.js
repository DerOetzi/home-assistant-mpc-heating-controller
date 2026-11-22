// DOM-free logic of the flow card: which entities it shows and how the
// feedback count maps onto its bar.

import { DOMAIN } from "./const.js";

// translation_key -> role, for the flow and hub devices (one each). A key
// two platforms share takes its domain in front, e.g. "sensor:flow_threshold".
export const FLOW_CARD_ROLES = {
  flow_target: "target",
  flow_feedback: "feedback",
  flow_curve: "curve",
  flow_actuator_target: "actuatorTarget",
  flow_actuator_actual: "actuatorActual",
  hold_flow_curves: "curves",
  flow_feedback_reset: "reset",
  heat_source_reachable: "reachable",
};

// Where the count sits on a bar from the step down (left, positive) to the
// step up (right, negative): 0..100.
export const counterPosition = (timerMin, upAtMin, downAtMin) => {
  const up = Math.abs(Number(upAtMin));
  const down = Math.abs(Number(downAtMin));
  const value = Number(timerMin);
  if (timerMin == null || !(up > 0) || !(down > 0) || !Number.isFinite(value)) return null;
  const clamped = Math.max(-up, Math.min(down, value));
  return ((down - clamped) / (up + down)) * 100;
};

// Minutes since an ISO time, or null.
export const minutesSince = (iso, nowMs) => {
  const then = Date.parse(iso ?? "");
  if (!Number.isFinite(then)) return null;
  return Math.max(0, Math.round((nowMs - then) / 60000));
};

// Minutes until an ISO time (at least 1 while it is ahead), or null.
export const minutesUntil = (iso, nowMs) => {
  const then = Date.parse(iso ?? "");
  if (!Number.isFinite(then)) return null;
  return Math.max(1, Math.ceil((then - nowMs) / 60000));
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

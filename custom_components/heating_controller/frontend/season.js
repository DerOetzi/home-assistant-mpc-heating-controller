// DOM-free logic of the heating season card: which entities it shows and the
// times it works out from the season attributes.

// translation_key -> role, for the hub device (one each).
export const SEASON_CARD_ROLES = {
  heating_season_switch_on: "switchOn",
  heating_season_switch_off: "switchOff",
  lead_heating_demand: "demand",
  heating_demand_hours_24h: "hours",
  heating_season_last_demand: "lastDemand",
  heating_season_on_threshold_h: "onThreshold",
  heating_season_hold_threshold_h: "holdThreshold",
  heating_season_off_after_h: "offAfter",
  lead_hold_flow_temperature: "leadHold",
  hold_flow_reserve: "reserve",
};

// Where a value of hours sits on the 0..24 h bar: 0..100, or null.
export const hoursPosition = (hours) => {
  const value = Number.parseFloat(hours);
  if (hours == null || !Number.isFinite(value)) return null;
  return (Math.max(0, Math.min(24, value)) / 24) * 100;
};

// Hours from now until the switch-off recommendation comes: last demand plus
// the switch-off time. Negative once it is due, null without a last demand.
export const hoursUntilOff = (lastDemandIso, offAfterH, nowMs) => {
  const last = Date.parse(lastDemandIso ?? "");
  const after = Number(offAfterH);
  if (!Number.isFinite(last) || !Number.isFinite(after)) return null;
  return (last + after * 3600000 - nowMs) / 3600000;
};

// Value formatting and small state helpers.

// Formats for the viewer, not for the machine: a German user gets "21,5" and a
// bare "21" instead of "21.5"/"21.0". `digits` is the maximum, not a fixed
// width, so trailing zeros disappear. Falls back to the plain number if the
// runtime has no Intl (which is the case in the QuickJS test harness).
export const num = (value, digits = 1, locale) => {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed)) return "–";
  const factor = 10 ** digits;
  const rounded = Math.round(parsed * factor) / factor;
  try {
    return rounded.toLocaleString(locale, { maximumFractionDigits: digits });
  } catch (err) {
    return String(rounded);
  }
};

export const localeOf = (hass) => hass?.locale?.language;

// The value of a hand-added row. A number is rounded and gets its unit; any
// other state (an enum like "found") is shown as text, relabelled through the
// row's optional `state_labels` map, else in HA's own wording. unavailable/
// unknown stay "–" like a broken number always has.
export const entityValue = (hass, stateObj, stateLabels) => {
  const state = stateObj.state;
  if (stateLabels && Object.prototype.hasOwnProperty.call(stateLabels, state)) {
    return String(stateLabels[state]);
  }
  const unit = stateObj.attributes.unit_of_measurement ?? "";
  const isNumber = Number.isFinite(Number.parseFloat(state));
  if (!isNumber && state !== "unavailable" && state !== "unknown") {
    return hass.formatEntityState ? hass.formatEntityState(stateObj) : state;
  }
  return `${num(state, unit === "%" || unit === "ppm" ? 0 : 1, localeOf(hass))} ${unit}`;
};

export const isOn = (stateObj) => stateObj?.state === "on";

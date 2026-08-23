// The interactive parts of the card: mode selection, comfort setpoint, eco
// offset, boost.
//
// These are plain functions rather than card methods so their dependencies are
// visible in the signature instead of hiding behind `this`. Every one of them
// takes a context object:
//
//   hass           the current Home Assistant object (for locale and states)
//   callService    (domain, service, data) => void
//   effectiveValue (stateObj) => number, the value to show right now, which is
//                  the pending local value while a write is in flight
//   setNumber      (stateObj, value) => void, the show/write/verify pipeline
//   setDragging    (bool) => void, suppresses rebuilds during a gesture
//   isDragging     () => bool
//
// Individual builders take only the slice they need.

import { MODE_COLORS, MODE_ICONS } from "./const.js";
import { num, localeOf, isOn } from "./format.js";
import { t } from "./translations.js";

// The modes are one choice, not three switches, so they are rendered as a
// single segmented control rather than separate chips. Unblocking lives in the
// header: it is not a mode.
export const modeSegments = (roles, labels, { selectMode }) => {
  const row = document.createElement("div");
  row.className = "segmented";

  for (const option of roles.mode.attributes.options ?? []) {
    if (option === "boost") continue; // has its own row
    const button = document.createElement("button");
    button.className = `segment${roles.mode.state === option ? " active" : ""}`;
    // Each segment carries its own mode colour, used for the active gradient
    // and for the hover tint -- so the colour always names the mode, never
    // just "selected".
    button.style.setProperty("--seg", MODE_COLORS[option] ?? "var(--primary-color)");
    button.innerHTML = `<ha-icon icon="${MODE_ICONS[option] ?? ""}"></ha-icon>${
      labels[option] ?? option
    }`;
    button.addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectMode(option);
    });
    row.appendChild(button);
  }
  return row;
};

// While boost is active there is deliberately no click-to-toggle-off: the only
// way back is Entblocken, so the button is a plain state indicator at that
// point, not a second, competing exit.
export const boostButton = (hass, active, { selectMode }) => {
  const button = document.createElement("button");
  button.className = `boost${active ? " active" : ""}`;
  button.disabled = active;
  button.innerHTML = `<ha-icon icon="${MODE_ICONS.boost}"></ha-icon>${t(
    hass,
    "mode_boost"
  )}`;
  if (!active) {
    button.addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectMode("boost");
    });
  }
  return button;
};

// A room-specific comfort condition (e.g. a desk-plug Homeoffice signal)
// lives in the header, not tucked inside the controls section -- it needs to
// be visible without expanding the card, the same as boost. Just the
// switch/indicator, no heading above it: the editor is where the entity gets
// identified, the card only shows what it currently is.
//
// `entry` is either a bare entity id or an editor-configured object
// { entity, icon_on, icon_off, label_on, label_off } -- icons/labels fall back
// to a generic An/Aus when not overridden.
//
// Writable conditions (switch/input_boolean) get the full An/Aus segmented
// control: the user can set either state directly, so both options are shown.
// Everything else (binary_sensor, schedule, ...) can only be observed, not
// set, so showing both options would offer a choice that doesn't exist --
// instead it renders as a single state pill in the same style as the boost
// button, showing only whichever state currently applies.
export const comfortConditionToggle = (hass, entry, { setBoolean, moreInfo }) => {
  const entityId = typeof entry === "string" ? entry : entry.entity;
  const stateObj = hass.states[entityId];
  if (!stateObj) return null;

  const domain = entityId.split(".")[0];
  const writable = domain === "input_boolean" || domain === "switch";
  const on = isOn(stateObj);
  const overrides = typeof entry === "string" ? {} : entry;
  const labelOn = overrides.label_on || t(hass, "condition_on");
  const labelOff = overrides.label_off || t(hass, "condition_off");

  if (!writable) {
    // Deliberately NOT the bold pill/segment shape those use: that shape
    // means "tap here to change something", which isn't true of a
    // binary_sensor. Reuses the header value tile instead -- the card's
    // existing language for "read-only, tap for details" -- tinted to still
    // carry the comfort/eco signal without implying it is a control.
    const icon = on ? overrides.icon_on : overrides.icon_off;
    const label = on ? labelOn : labelOff;
    const el = document.createElement("div");
    el.className = "value condition-indicator";
    el.style.setProperty("--seg", on ? "var(--hc-comfort)" : "var(--hc-eco)");
    el.innerHTML =
      (icon ? `<ha-icon icon="${icon}"></ha-icon>` : "") +
      `<span class="num">${label}</span>`;
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      moreInfo(entityId);
    });
    return el;
  }

  const row = document.createElement("div");
  row.className = "segmented condition";

  const segment = (wantsOn, active, seg, icon, label) => {
    const button = document.createElement("button");
    button.className = `segment${active ? " active" : ""}`;
    button.style.setProperty("--seg", seg);
    button.innerHTML = `${icon ? `<ha-icon icon="${icon}"></ha-icon>` : ""}${label}`;
    button.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setBoolean(entityId, wantsOn);
    });
    return button;
  };

  row.append(
    segment(true, on, "var(--hc-comfort)", overrides.icon_on, labelOn),
    segment(false, !on, "var(--hc-eco)", overrides.icon_off, labelOff)
  );
  return row;
};

export const comfortSlider = (
  stateObj,
  { hass, effectiveValue, setNumber, setDragging, isDragging }
) => {
  const { min, max, step } = stateObj.attributes;
  const value = effectiveValue(stateObj);
  // The left cap holds icon and value and is always filled, so the readout
  // never sits on empty track. Only the part right of it is draggable.
  const CAP = 92;

  const track = document.createElement("div");
  track.className = "slider";
  track.title = `${num(min, 1, localeOf(hass))}–${num(max, 1, localeOf(hass))} °C`;

  const fill = document.createElement("div");
  fill.className = "slider-fill";

  const label = document.createElement("div");
  label.className = "slider-label";
  label.innerHTML = `<ha-icon icon="${MODE_ICONS.comfort}"></ha-icon><span></span>`;
  const readout = label.querySelector("span");

  // Shows what the finger is on, not what the server has -- snapped to step,
  // because a bar that lands between two settable values lies about where it
  // will end up.
  const show = (v) => {
    const snapped = Math.min(max, Math.max(min, Math.round(v / step) * step));
    const fraction = max > min ? (snapped - min) / (max - min) : 0;
    fill.style.width = `calc(${CAP}px + (100% - ${CAP}px) * ${fraction})`;
    readout.textContent = `${num(snapped, 1, localeOf(hass))} °C`;
  };
  show(value);

  const valueAt = (clientX) => {
    const rect = track.getBoundingClientRect();
    const span = rect.width - CAP;
    if (span <= 0) return value;
    const fraction = (clientX - rect.left - CAP) / span;
    return min + Math.min(1, Math.max(0, fraction)) * (max - min);
  };

  track.addEventListener("click", (ev) => ev.stopPropagation());
  track.addEventListener("pointerdown", (ev) => {
    ev.stopPropagation();
    // Card-wide, not local: a rebuild triggered by any other entity would
    // replace this element and the gesture would end mid-drag.
    setDragging(true);
    track.setPointerCapture(ev.pointerId);
    show(valueAt(ev.clientX));
  });
  track.addEventListener("pointermove", (ev) => {
    if (!isDragging()) return;
    show(valueAt(ev.clientX));
  });
  const finish = (ev) => {
    if (!isDragging()) return;
    setDragging(false);
    setNumber(stateObj, valueAt(ev.clientX));
  };
  track.addEventListener("pointerup", finish);
  track.addEventListener("pointercancel", finish);

  track.append(fill, label);
  return track;
};

export const ecoStepper = (stateObj, { hass, effectiveValue, setNumber }) => {
  const { min, max, step } = stateObj.attributes;
  const value = effectiveValue(stateObj);

  const box = document.createElement("div");
  box.className = "stepper";
  box.addEventListener("click", (ev) => ev.stopPropagation());

  const button = (glyph, delta, disabled) => {
    const el = document.createElement("button");
    el.textContent = glyph;
    el.disabled = disabled;
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setNumber(stateObj, value + delta);
    });
    return el;
  };

  const readout = document.createElement("span");
  readout.innerHTML = `<ha-icon icon="${MODE_ICONS.eco}"></ha-icon>${num(
    value,
    1,
    localeOf(hass)
  )} °C`;

  box.append(
    button("−", -step, value - step < min),
    readout,
    button("+", step, value + step > max)
  );
  return box;
};

// Maps supply_status straight to what the row shows. Four of the six states
// can never mean "this is the temperature the source must hit" -- no
// requirement exists, the room is coasting on stored heat, it could never
// bind, or there is no source running to judge against -- so those read as
// words rather than a misleading number.
const SUPPLY_PRESENTATION = {
  no_requirement: { textKey: "supply_no_requirement", verdict: "" },
  surplus: { textKey: "supply_surplus", verdict: "idle" },
  below_threshold: { textKey: "supply_below_threshold", verdict: "idle" },
  source_inactive: { textKey: "supply_source_inactive", verdict: "idle" },
  undersupplied: { verdict: "low" },
  sufficient: { verdict: "ok" },
};

const SUPPLY_HINT_KEYS = {
  no_requirement: "supply_hint_no_requirement",
  surplus: "supply_hint_surplus",
  below_threshold: "supply_hint_below_threshold",
  source_inactive: "supply_hint_source_inactive",
  undersupplied: "supply_hint_undersupplied",
  sufficient: "supply_hint_sufficient",
};

// Returns { value, verdict, hint } for the minimum-flow row; the caller renders
// it with the same row builder the detail sensors use.
export const supplyPresentation = (hass, stateObj) => {
  const status = stateObj.attributes.supply_status;
  const presentation = SUPPLY_PRESENTATION[status] ?? { verdict: "" };
  const hintKey = SUPPLY_HINT_KEYS[status];
  const value = presentation.textKey
    ? t(hass, presentation.textKey)
    : `${num(stateObj.state, 1, localeOf(hass))} °C` +
      (presentation.verdict === "low" ? " ⚠" : "");
  return {
    value,
    verdict: presentation.verdict,
    hint: hintKey ? t(hass, hintKey) : undefined,
  };
};

// DOM-free logic of the heating curve card: the heat source's curve, the
// recommendation from the flow lead rooms and the chart's scales.

// translation_key of the flow device's curve numbers -> curve field.
export const CURVE_NUMBER_KEYS = {
  flow_curve_outdoor_cold: "outdoorCold",
  flow_curve_outdoor_warm: "outdoorWarm",
  flow_curve_flow_cold: "flowCold",
  flow_curve_flow_warm: "flowWarm",
  flow_curve_bump: "bump",
  flow_curve_bump_width: "bumpWidth",
};

export const HOLD_CURVES_KEY = "hold_flow_curves";

// Room names and the title end up in markup.
export const escapeHtml = (text) =>
  String(text).replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
  );

// Same as HeatingCurve.flow_at in control/flow.py: two points, flat beyond
// them, plus a bump that is full at 0 °C and gone at ±width/2.
export const curveFlowAt = (curve, outdoor) => {
  if (curve.outdoorCold === curve.outdoorWarm) return null;
  const clamped = Math.max(curve.outdoorCold, Math.min(curve.outdoorWarm, outdoor));
  const slope =
    (curve.flowWarm - curve.flowCold) / (curve.outdoorWarm - curve.outdoorCold);
  let flow = curve.flowCold + slope * (clamped - curve.outdoorCold);
  if (curve.bumpWidth > 0) {
    flow += curve.bump * Math.max(0, 1 - Math.abs(outdoor) / (curve.bumpWidth / 2));
  }
  return Math.round(flow * 10) / 10;
};

export const curveSeries = (curve, outdoorValues) =>
  outdoorValues.map((outdoor) => curveFlowAt(curve, outdoor));

// The highest hold flow among the given rooms plus the margin, per outdoor value.
export const envelopeSeries = (rooms, margin) => {
  const chosen = rooms ?? [];
  if (!chosen.length) return [];
  return chosen[0].flow_c.map((_, index) => {
    const values = chosen
      .map((room) => room.flow_c[index])
      .filter((value) => Number.isFinite(value));
    return values.length ? Math.round((Math.max(...values) + margin) * 10) / 10 : null;
  });
};

// What the flow device recommends: the envelope of the flow lead rooms.
export const recommendationSeries = (rooms, margin) =>
  envelopeSeries((rooms ?? []).filter((room) => room.lead), margin);

// Linear interpolation on the outdoor grid; null outside it.
export const valueAt = (outdoorValues, series, outdoor) => {
  for (let index = 0; index < outdoorValues.length - 1; index += 1) {
    const x0 = outdoorValues[index];
    const x1 = outdoorValues[index + 1];
    if (outdoor < x0 || outdoor > x1) continue;
    const y0 = series[index];
    const y1 = series[index + 1];
    if (!Number.isFinite(y0) || !Number.isFinite(y1)) return null;
    return y0 + ((y1 - y0) * (outdoor - x0)) / (x1 - x0);
  }
  return null;
};

// The tightest point of a curve against the recommendation within the
// curve's own range: smallest reserve and where it is.
export const tightestPoint = (outdoorValues, series, recommendation, curve) => {
  let best = null;
  outdoorValues.forEach((outdoor, index) => {
    if (outdoor < curve.outdoorCold || outdoor > curve.outdoorWarm) return;
    const value = series[index];
    const needed = recommendation[index];
    if (!Number.isFinite(value) || !Number.isFinite(needed)) return;
    const reserve = Math.round((value - needed) * 10) / 10;
    if (best === null || reserve < best.reserve) best = { reserve, outdoor };
  });
  return best;
};

// Value axis bounds in whole steps around everything that is drawn.
export const valueRange = (seriesList, step = 5) => {
  const values = seriesList.flat().filter((value) => Number.isFinite(value));
  if (!values.length) return [20, 60];
  const low = Math.floor(Math.min(...values) / step) * step;
  const high = Math.ceil(Math.max(...values) / step) * step;
  return high > low ? [low, high] : [low, low + step];
};

export const ticks = (low, high, step) => {
  const found = [];
  for (let value = Math.ceil(low / step) * step; value <= high; value += step) {
    found.push(value);
  }
  return found;
};

// DOM-free logic of the rooms card: which devices are rooms, how they are
// grouped by floor and ordered, and the small judgements the table shows.

import { DOMAIN } from "./const.js";

// translation_key -> role, for every value the table shows.
export const ROOM_TABLE_ROLES = {
  heating_mode: "mode",
  heating_automation: "automation",
  room_temperature: "roomTemp",
  supply: "supply",
  heating_demand: "demand",
  gated_hold_flow_temperature: "gatedHold",
  hold_flow_temperature: "hold",
  boost_flow_temperature: "boost",
  radiator_not_heating: "radiatorAlert",
  ua_learning_status: "uaStatus",
  ua_factor: "uaFactor",
  capacity_learning_status: "capStatus",
  capacity_factor: "capFactor",
  additional_heat: "additionalHeat",
};

export const ROOM_VIEWS = ["supply", "radiators", "learning"];

// Learning states worth a second look, as in the MPC guide.
const UA_ATTENTION = [
  "drift",
  "implausible_target",
  "storage_dominated",
  "room_sensor_fallback",
  "valve_position_missing",
  "trv_adapting",
  "radiator_not_heating",
  "outdoor_temperature_missing",
  "valve_saturated",
  "learned_at_limit",
];
const CAPACITY_ATTENTION = [
  "disturbed",
  "implausible",
  "room_sensor_fallback",
  "valve_position_missing",
  "trv_adapting",
  "radiator_not_heating",
  "outdoor_temperature_missing",
  "disabled",
];

// "ok" for learned, "attention" for a state that hints at a problem, "" else.
export const learningJudgement = (kind, state) => {
  if (state === "learned") return "ok";
  const attention = kind === "ua" ? UA_ATTENTION : CAPACITY_ATTENTION;
  return attention.includes(state) ? "attention" : "";
};

// Every device of the integration that is a room: it has a heating mode.
export const roomDeviceIds = (hass) => {
  const ids = [];
  for (const entry of Object.values(hass?.entities ?? {})) {
    if (entry.platform !== DOMAIN || entry.translation_key !== "heating_mode") continue;
    if (entry.device_id && !ids.includes(entry.device_id)) ids.push(entry.device_id);
  }
  return ids;
};

// The state of the integration's one entity with this translation key (the
// flow and hub devices exist once per installation).
export const integrationEntity = (hass, key) => {
  const entry = Object.values(hass?.entities ?? {}).find(
    (item) => item.platform === DOMAIN && item.translation_key === key
  );
  return entry ? hass.states[entry.entity_id] : undefined;
};

export const deviceName = (hass, deviceId) => {
  const device = hass?.devices?.[deviceId];
  return device?.name_by_user || device?.name || deviceId;
};

// Rooms grouped by floor (lowest level first, rooms without a floor last).
// Inside a floor the configured order wins (device id or name), then the
// order the integration lists its rooms in, then the name.
export const groupRooms = (hass, deviceIds, configured, integrationOrder) => {
  const position = (list, id, name) => {
    const index = (list ?? []).findIndex((item) => item === id || item === name);
    return index < 0 ? Number.MAX_SAFE_INTEGER : index;
  };
  const floors = new Map();
  for (const id of deviceIds) {
    const name = deviceName(hass, id);
    const areaId = hass?.devices?.[id]?.area_id;
    const floorId = areaId ? hass?.areas?.[areaId]?.floor_id ?? null : null;
    const floor = floorId ? hass?.floors?.[floorId] : null;
    const key = floor ? floorId : "";
    if (!floors.has(key)) {
      floors.set(key, {
        id: key,
        name: floor?.name ?? "",
        level: floor ? floor.level ?? 0 : Number.MAX_SAFE_INTEGER,
        rooms: [],
      });
    }
    floors.get(key).rooms.push({
      id,
      name,
      configured: position(configured, id, name),
      integration: position(integrationOrder, id, name),
    });
  }
  const groups = [...floors.values()].sort((a, b) => a.level - b.level);
  for (const group of groups) {
    group.rooms.sort(
      (a, b) =>
        a.configured - b.configured ||
        a.integration - b.integration ||
        a.name.localeCompare(b.name)
    );
    for (const room of group.rooms) {
      delete room.configured;
      delete room.integration;
    }
  }
  return groups;
};

// Short labels for a room's TRVs: what is left of each name after the part
// all of them share ("Heating_Livingroombig" -> "Big"). A single TRV needs
// no label.
export const trvLabels = (names) => {
  const keys = Object.keys(names);
  if (keys.length < 2) return Object.fromEntries(keys.map((key) => [key, ""]));
  const values = keys.map((key) => String(names[key] ?? key));
  let prefix = values[0];
  for (const value of values.slice(1)) {
    let i = 0;
    while (i < prefix.length && i < value.length && prefix[i] === value[i]) i += 1;
    prefix = prefix.slice(0, i);
  }
  const labels = {};
  keys.forEach((key, index) => {
    const rest = values[index].slice(prefix.length).replace(/^[\s_\-]+/, "");
    const label = rest || values[index];
    labels[key] = label.charAt(0).toUpperCase() + label.slice(1);
  });
  return labels;
};

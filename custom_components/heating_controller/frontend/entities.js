// Resolving a heating_controller device into the entities the card renders.

import { DOMAIN, ROLES, ROOM_SENSOR_KEY, ROOM_TEMP_KEY, DEVICE_CLASS_ICONS } from "./const.js";
import { num, localeOf } from "./format.js";
import { t } from "./translations.js";

// The room-temperature entity as a managed header row. `label` is the
// editor-list caption ("Room temperature") -- it is never shown on the card
// itself; the card only shows a caption when the user has set one via `name`.
export const managedHeaderRows = (hass, roles) => {
  if (!roles.roomTemp) return [];
  return [
    {
      key: ROOM_TEMP_KEY,
      entity: roles.roomTemp.entity_id,
      label: t(hass, "row_room_temp"),
      icon: DEVICE_CLASS_ICONS.temperature,
    },
  ];
};

// Shared by card and editor so both resolve the controller's entities the
// same way — by translation_key, never by entity_id pattern.
export const resolveRoles = (hass, deviceId) => {
  const roles = {};
  if (!hass || !deviceId) return roles;
  const byTranslationKey = {};
  for (const entry of Object.values(hass.entities ?? {})) {
    if (entry.device_id !== deviceId || entry.platform !== DOMAIN) continue;
    if (entry.translation_key) byTranslationKey[entry.translation_key] = entry.entity_id;
  }
  for (const [key, role] of Object.entries(ROLES)) {
    const entityId = byTranslationKey[key];
    if (entityId && hass.states[entityId]) roles[role] = hass.states[entityId];
  }
  return roles;
};

// The header warning while a radiator stays cold although its valve is open
// (air in it, a valve stuck shut): null while all is well, otherwise the
// sensor to open and a tooltip naming the TRVs.
export const radiatorAlert = (hass, roles) => {
  const sensor = roles.radiatorAlert;
  if (!sensor || sensor.state !== "on") return null;
  const trvNames = (sensor.attributes?.trv_entities ?? []).map(
    (entityId) => hass.states[entityId]?.attributes?.friendly_name || entityId
  );
  const text = t(hass, "radiator_not_heating");
  return {
    entityId: sensor.entity_id,
    text: trvNames.length ? `${text}: ${trvNames.join(", ")}` : text,
  };
};

// The rows the card seeds itself: the raw room sensor, one per TRV, one per
// window contact. Each carries a stable `key` so the config can pin order,
// name and icon without hard-coding entity ids.
export const managedDetailRows = (hass, roles) => {
  const rows = [];
  const roomAttrs = roles.roomTemp?.attributes ?? {};

  if (
    roomAttrs.used_strategy === "room_sensor" &&
    roomAttrs.room_sensor_temp_c != null
  ) {
    rows.push({
      key: ROOM_SENSOR_KEY,
      name: t(hass, "row_room_sensor"),
      icon: "mdi:thermometer",
      value: `${num(roomAttrs.room_sensor_temp_c, 1, localeOf(hass))} °C`,
      // The raw sensor is an attribute, not a standalone entity; link the row
      // to the room-temperature entity whose more-info shows that value.
      clickEntity: roles.roomTemp?.entity_id,
    });
  }

  const trvTemps = roomAttrs.trv_temperatures ?? {};
  const trvTargets = roles.demand?.attributes?.trv_target_temps ?? {};
  for (const [entityId, temp] of Object.entries(trvTemps)) {
    const stateObj = hass.states[entityId];
    const target = trvTargets[entityId];
    rows.push({
      key: `trv:${entityId}`,
      entity: entityId,
      name: stateObj?.attributes?.friendly_name || entityId,
      icon: "mdi:radiator",
      value:
        target != null
          ? `${num(temp, 1, localeOf(hass))} °C → ${num(target, 1, localeOf(hass))} °C`
          : `${num(temp, 1, localeOf(hass))} °C`,
    });
  }

  const contacts = roles.automation?.attributes?.window_contact_entities ?? [];
  for (const entityId of contacts) {
    const stateObj = hass.states[entityId];
    if (!stateObj) continue;
    const open = stateObj.state === "on";
    rows.push({
      key: `window:${entityId}`,
      entity: entityId,
      name: stateObj.attributes.friendly_name || entityId,
      icon: open ? "mdi:window-open-variant" : "mdi:window-closed-variant",
      value: t(hass, open ? "window_open" : "window_closed"),
    });
  }
  return rows;
};

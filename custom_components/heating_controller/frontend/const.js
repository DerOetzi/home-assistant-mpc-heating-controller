// Shared constants for the card and its editor.

export const CARD_TAG = "heating-controller-card";
export const EDITOR_TAG = "heating-controller-card-editor";
export const DOMAIN = "heating_controller";

// translation_key -> role. See docs/card-design.md.
export const ROLES = {
  heating_mode: "mode",
  heating_automation: "automation",
  heating_unblock: "unblock",
  comfort_temperature: "comfort",
  eco_offset: "eco",
  room_temperature: "roomTemp",
  min_flow_temperature: "minFlow",
  heating_demand: "demand",
  radiator_not_heating: "radiatorAlert",
};

export const REQUIRED_ROLES = ["mode", "automation"];

export const MODE_ICONS = {
  comfort: "mdi:sun-thermometer",
  eco: "mdi:sprout",
  boost: "mdi:fire",
  frost_protection: "mdi:snowflake-thermometer",
};

// Resolved against the card's own variables, which in turn fall back to a
// house palette if one defines --color-comfort and friends.
export const MODE_COLORS = {
  comfort: "var(--hc-comfort)",
  eco: "var(--hc-eco)",
  boost: "var(--hc-boost)",
  frost_protection: "var(--hc-frost)",
};

export const DEVICE_CLASS_UNITS = {
  humidity: "%",
  carbon_dioxide: "ppm",
  pm25: "µg/m³",
  temperature: "°C",
};

export const DEVICE_CLASS_ICONS = {
  humidity: "mdi:water-percent",
  carbon_dioxide: "mdi:molecule-co2",
  pm25: "mdi:air-filter",
  temperature: "mdi:thermometer",
};

export const HEADER_KEY = "header_entities";
export const DETAIL_KEY = "detail_entities";
export const CONDITION_KEY = "comfort_conditions";

// Identifies the auto-populated detail rows. TRVs and windows append their
// entity id; the room sensor is a singleton.
export const ROOM_SENSOR_KEY = "room_sensor";

// Identifies the room-temperature entity as a managed header row -- present
// by default (leading, unlabeled), but sortable/hideable/nameable through the
// same header_entities list as any other header sensor, the same way
// ROOM_SENSOR_KEY is a managed row in detail_entities.
export const ROOM_TEMP_KEY = "room_temp";

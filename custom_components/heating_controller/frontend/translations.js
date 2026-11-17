// UI strings for the card and its editor.
//
// English is the source of truth and the fallback: a key missing from a
// translation falls back to English rather than disappearing, and a key
// missing everywhere renders as the key itself, which is loud enough to be
// noticed in review but never blanks out the interface.
export const TRANSLATIONS = {
  en: {
    // Card — header and status
    default_room_name: "Heating",
    status_forced_frost: "Frost protection enforced",
    status_automatic: "Automatic",
    status_blocked: "Blocked",
    unblock: "Unblock",
    radiator_not_heating: "Radiator not heating – check or bleed it",

    // Card — sections
    section_details: "Details",
    section_controls: "Heating control",

    // Card — detail rows
    row_room_sensor: "Sensor",
    row_room_temp: "Room temperature",
    window_open: "Open",
    window_closed: "Closed",
    row_supply: "Supply",
    row_additional_heat: "Additional heat",
    row_additional_heat_learning: "{value} (learning: {ungated})",

    // Card — supply, judged on the valves
    supply_undersupplied: "Short",
    supply_supplied: "At limit",
    supply_oversupplied: "Headroom",
    supply_not_rated: "Not rated",
    supply_reason_window_open: "window open",
    supply_reason_frost_protection: "frost protection",
    supply_reason_no_heat: "no heat",
    supply_reason_radiator_not_heating: "radiator not heating",
    supply_reason_boost: "boost",
    supply_reason_demand_unknown: "valve opening unknown",
    supply_hint_undersupplied:
      "Valves fully open and the room below target – the flow is too low",
    supply_hint_supplied: "Valves open enough – this room needs the current flow",
    supply_hint_oversupplied:
      "Valves well closed or the room above target – this room would get by on less flow",
    supply_hint_not_rated: "The valves say nothing about the flow right now",

    // Card — modes
    mode_boost: "Boost",

    // Card — room-specific comfort conditions
    condition_on: "On",
    condition_off: "Off",

    // Card — error states
    error_pick_device: "Please select a Heating Controller device.",
    error_missing_entities: "This device is missing entities ({missing}).",
    error_wrong_device: "Is it a Heating Controller device?",

    // Editor — fields
    editor_device: "Room (Heating Controller device)",
    editor_title: "Title (optional, defaults to the device name)",
    editor_entity: "Entity",
    editor_name: "Name",
    editor_icon: "Icon",

    // Editor — sections
    editor_header_heading: "Header",
    editor_header_hint:
      "Values shown next to the room temperature, visible even when the card is collapsed.",
    editor_detail_hint:
      "Seeded rows (room sensor, radiators, windows) and your own sensors. " +
      "Sortable and renameable; seeded rows can be hidden instead of deleted.",
    editor_add_entity: "Add entity",
    editor_add_custom_sensor: "Add your own sensor",
    editor_seeded: "Seeded",
    editor_conditions_heading: "Comfort conditions",
    editor_conditions_hint:
      "Room-specific conditions that decide comfort/eco. Sortable and " +
      "hideable, with an editable icon and label for each state.",
    editor_label_on: "Label (on)",
    editor_icon_on: "Icon (on)",
    editor_label_off: "Label (off)",
    editor_icon_off: "Icon (off)",

    // Card registration
    card_name: "Heating Controller",
    card_description:
      "Room control for the Heating Controller — mode, setpoints and supply " +
      "from a single device.",

    // Heating curve card
    curve_card_name: "Heating curve",
    curve_card_description:
      "The heat source's curve, each room's hold flow and a curve to try out.",
    curve_title: "Heating curve",
    curve_source: "Heating curve",
    curve_experiment: "Trial curve",
    curve_recommendation: "Recommendation",
    curve_lead_room: "Flow lead room",
    curve_at_outdoor: "At {value} °C",
    curve_axis_outdoor: "Outdoor °C",
    curve_flow_cold: "Flow cold",
    curve_flow_warm: "Flow warm",
    curve_bump: "Raise around 0 °C",
    curve_bump_width: "Raise width",
    curve_copy_source: "Copy heating curve",
    curve_copy_recommendation: "Ends from rooms:",
    curve_rooms_all: "All",
    curve_rooms_lead: "Lead rooms",
    curve_rooms_shown: "Shown",
    curve_tightest: "{curve}: tightest point {reserve} K at {outdoor} °C",
    curve_feedback: "Valve feedback {feedback} K · lead demand {demand} % ({room})",
    curve_error_entity:
      "Please pick the flow device's \"Hold flow curves\" sensor.",
  },

  de: {
    default_room_name: "Heizung",
    status_forced_frost: "Frostschutz erzwungen",
    status_automatic: "Automatik",
    status_blocked: "Blockiert",
    unblock: "Entblocken",
    radiator_not_heating: "Heizkörper heizt nicht – prüfen bzw. entlüften",

    section_details: "Details",
    section_controls: "Heizungssteuerung",

    row_room_sensor: "Sensor",
    row_room_temp: "Raumtemperatur",
    window_open: "Offen",
    window_closed: "Geschlossen",
    row_supply: "Versorgung",
    row_additional_heat: "Zusatzwärme",
    row_additional_heat_learning: "{value} (lernt: {ungated})",

    supply_undersupplied: "Zu knapp",
    supply_supplied: "Am Limit",
    supply_oversupplied: "Reserve",
    supply_not_rated: "Nicht bewertet",
    supply_reason_window_open: "Fenster offen",
    supply_reason_frost_protection: "Frostschutz",
    supply_reason_no_heat: "keine Wärme",
    supply_reason_radiator_not_heating: "Heizkörper heizt nicht",
    supply_reason_boost: "Boost",
    supply_reason_demand_unknown: "Ventilöffnung unbekannt",
    supply_hint_undersupplied:
      "Ventile voll offen und Raum unter Soll – Vorlauf zu niedrig",
    supply_hint_supplied: "Ventile offen genug – Raum braucht den jetzigen Vorlauf",
    supply_hint_oversupplied:
      "Ventile weit zu oder Raum über Soll – Raum käme mit weniger Vorlauf aus",
    supply_hint_not_rated: "Ventile sagen gerade nichts über den Vorlauf",

    mode_boost: "Boost",

    condition_on: "An",
    condition_off: "Aus",

    error_pick_device: "Bitte ein Heating-Controller-Gerät auswählen.",
    error_missing_entities: "Diesem Gerät fehlen Entitäten ({missing}).",
    error_wrong_device: "Ist es ein Heating-Controller-Gerät?",

    editor_device: "Raum (Heating-Controller-Gerät)",
    editor_title: "Titel (optional, sonst Gerätename)",
    editor_entity: "Entität",
    editor_name: "Name",
    editor_icon: "Symbol",

    editor_header_heading: "Kopfzeile",
    editor_header_hint:
      "Werte neben der Raumtemperatur, auch bei zugeklappter Karte sichtbar.",
    editor_detail_hint:
      "Vorbelegte Zeilen (Raumsensor, Heizkörper, Fenster) und eigene Sensoren. " +
      "Sortierbar und umbenennbar; Vorbelegte lassen sich aus-/einblenden statt löschen.",
    editor_add_entity: "Entität hinzufügen",
    editor_add_custom_sensor: "Eigenen Sensor hinzufügen",
    editor_seeded: "Vorbelegt",
    editor_conditions_heading: "Komfort-Bedingungen",
    editor_conditions_hint:
      "Raumspezifische Bedingungen, die über Komfort/Absenk entscheiden. " +
      "Sortierbar und ausblendbar, mit editierbarem Icon und Label je Zustand.",
    editor_label_on: "Label (an)",
    editor_icon_on: "Icon (an)",
    editor_label_off: "Label (aus)",
    editor_icon_off: "Icon (aus)",

    curve_card_name: "Heizkurve",
    curve_card_description:
      "Heizkurve der Wärmequelle, Haltevorlauf je Raum und eine Kurve zum Ausprobieren.",
    curve_title: "Heizkurve",
    curve_source: "Heizkurve",
    curve_experiment: "Versuchskurve",
    curve_recommendation: "Empfehlung",
    curve_lead_room: "Vorlauf-Leitraum",
    curve_at_outdoor: "Bei {value} °C",
    curve_axis_outdoor: "Außentemperatur °C",
    curve_flow_cold: "Vorlauf kalt",
    curve_flow_warm: "Vorlauf warm",
    curve_bump: "Erhöhung um 0 °C",
    curve_bump_width: "Erhöhung Breite",
    curve_copy_source: "Von Heizkurve übernehmen",
    curve_copy_recommendation: "Endpunkte aus Räumen:",
    curve_rooms_all: "Alle",
    curve_rooms_lead: "Leiträume",
    curve_rooms_shown: "Ausgewählte",
    curve_tightest: "{curve}: knappste Stelle {reserve} K bei {outdoor} °C",
    curve_feedback: "Ventil-Rückkopplung {feedback} K · Leit-Demand {demand} % ({room})",
    curve_error_entity:
      "Bitte den Sensor „Haltevorlauf-Kurven“ des Vorlauf-Geräts wählen.",

    card_name: "Heating Controller",
    card_description:
      "Raumsteuerung für den Heating Controller — Modus, Solltemperaturen und " +
      "Versorgung aus einem Gerät.",
  },
};

// `hass` may legitimately be absent while the editor boots, so this must never
// throw on a missing locale — it degrades to English instead.
export const t = (hass, key, placeholders) => {
  const language = (hass?.locale?.language || hass?.language || "en").split("-")[0];
  const dictionary = TRANSLATIONS[language] ?? TRANSLATIONS.en;
  let text = dictionary[key] ?? TRANSLATIONS.en[key] ?? key;
  for (const [name, value] of Object.entries(placeholders ?? {})) {
    text = text.replace(`{${name}}`, value);
  }
  return text;
};

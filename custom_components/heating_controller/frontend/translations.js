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

    // Card — sections
    section_details: "Details",
    section_controls: "Heating control",

    // Card — detail rows
    row_room_sensor: "Sensor",
    row_room_temp: "Room temperature",
    window_open: "Open",
    window_closed: "Closed",
    row_min_flow: "Minimum flow temperature",

    // Card — minimum flow supply status
    supply_no_requirement: "No requirement",
    supply_surplus: "Coasting on stored heat",
    supply_below_threshold: "Below operating threshold",
    supply_source_inactive: "Source inactive",
    supply_hint_no_requirement:
      "Ambient temperature alone holds the target, no flow needed",
    supply_hint_surplus:
      "Room is above target on stored heat – the requirement returns once that is used up",
    supply_hint_below_threshold:
      "Requirement is below the heat pump's operating threshold – can never become binding",
    supply_hint_source_inactive: "Heat pump is not running – cannot be judged",
    supply_hint_undersupplied: "Flow temperature too low for this room",
    supply_hint_sufficient: "Flow temperature is sufficient for this room",

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
      "Room control for the Heating Controller — mode, setpoints and minimum " +
      "flow temperature from a single device.",
  },

  de: {
    default_room_name: "Heizung",
    status_forced_frost: "Frostschutz erzwungen",
    status_automatic: "Automatik",
    status_blocked: "Blockiert",
    unblock: "Entblocken",

    section_details: "Details",
    section_controls: "Heizungssteuerung",

    row_room_sensor: "Sensor",
    row_room_temp: "Raumtemperatur",
    window_open: "Offen",
    window_closed: "Geschlossen",
    row_min_flow: "Mindestvorlauftemperatur",

    supply_no_requirement: "Keine Anforderung",
    supply_surplus: "Zehrt von gespeicherter Wärme",
    supply_below_threshold: "Unter Betriebsschwelle",
    supply_source_inactive: "Quelle inaktiv",
    supply_hint_no_requirement:
      "Außentemperatur allein hält das Ziel, kein Vorlauf nötig",
    supply_hint_surplus:
      "Raum liegt über Soll und zehrt vom Wärmepuffer – die Anforderung kommt zurück, sobald der aufgebraucht ist",
    supply_hint_below_threshold:
      "Anforderung liegt unter der Betriebsschwelle der Wärmepumpe – kann nie bestimmend werden",
    supply_hint_source_inactive: "Wärmepumpe läuft gerade nicht – nicht bewertbar",
    supply_hint_undersupplied: "Vorlauf zu niedrig für diesen Raum",
    supply_hint_sufficient: "Vorlauf reicht für diesen Raum",

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

    card_name: "Heating Controller",
    card_description:
      "Raumsteuerung für den Heating Controller — Modus, Solltemperaturen und " +
      "Mindestvorlauf aus einem Gerät.",
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

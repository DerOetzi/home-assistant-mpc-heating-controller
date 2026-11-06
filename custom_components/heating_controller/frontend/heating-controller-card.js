/*
 * Entry point for the Heating Controller Lovelace card.
 *
 * Registers the custom elements and announces the card to the card picker.
 * Everything else lives in the sibling modules — vanilla ES modules on
 * purpose, since this repo has no JS build tooling.
 */

import { CARD_TAG, EDITOR_TAG } from "./const.js";
import { HeatingControllerCard } from "./card.js";
import { HeatingControllerCardEditor } from "./editor.js";
import { CURVE_CARD_TAG, HeatingControllerCurveCard } from "./curve-card.js";
import { TRANSLATIONS } from "./translations.js";

customElements.define(CARD_TAG, HeatingControllerCard);
customElements.define(EDITOR_TAG, HeatingControllerCardEditor);
customElements.define(CURVE_CARD_TAG, HeatingControllerCurveCard);

window.customCards = window.customCards ?? [];
window.customCards.push({
  type: CARD_TAG,
  // The picker reads this once at load time, before any hass is available, so
  // it cannot be localised per user — English is the neutral choice.
  name: TRANSLATIONS.en.card_name,
  description: TRANSLATIONS.en.card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mcp-heating-controller",
});
window.customCards.push({
  type: CURVE_CARD_TAG,
  name: TRANSLATIONS.en.curve_card_name,
  description: TRANSLATIONS.en.curve_card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mcp-heating-controller",
});

console.info(`%c ${CARD_TAG} %c loaded`, "color: white; background: #d35400", "");

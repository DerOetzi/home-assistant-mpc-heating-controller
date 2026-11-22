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
import { ROOMS_CARD_TAG, HeatingControllerRoomsCard } from "./rooms-card.js";
import { FLOW_CARD_TAG, HeatingControllerFlowCard } from "./flow-card.js";
import { SEASON_CARD_TAG, HeatingControllerSeasonCard } from "./season-card.js";
import { TRANSLATIONS } from "./translations.js";

customElements.define(CARD_TAG, HeatingControllerCard);
customElements.define(EDITOR_TAG, HeatingControllerCardEditor);
customElements.define(CURVE_CARD_TAG, HeatingControllerCurveCard);
customElements.define(ROOMS_CARD_TAG, HeatingControllerRoomsCard);
customElements.define(FLOW_CARD_TAG, HeatingControllerFlowCard);
customElements.define(SEASON_CARD_TAG, HeatingControllerSeasonCard);

window.customCards = window.customCards ?? [];
window.customCards.push({
  type: CARD_TAG,
  // The picker reads this once at load time, before any hass is available, so
  // it cannot be localised per user — English is the neutral choice.
  name: TRANSLATIONS.en.card_name,
  description: TRANSLATIONS.en.card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mpc-heating-controller",
});
window.customCards.push({
  type: CURVE_CARD_TAG,
  name: TRANSLATIONS.en.curve_card_name,
  description: TRANSLATIONS.en.curve_card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mpc-heating-controller",
});
window.customCards.push({
  type: ROOMS_CARD_TAG,
  name: TRANSLATIONS.en.rooms_card_name,
  description: TRANSLATIONS.en.rooms_card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mpc-heating-controller",
});
window.customCards.push({
  type: FLOW_CARD_TAG,
  name: TRANSLATIONS.en.flow_card_name,
  description: TRANSLATIONS.en.flow_card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mpc-heating-controller",
});

window.customCards.push({
  type: SEASON_CARD_TAG,
  name: TRANSLATIONS.en.season_card_name,
  description: TRANSLATIONS.en.season_card_description,
  preview: true,
  documentationURL:
    "https://github.com/DerOetzi/home-assistant-mpc-heating-controller",
});

console.info(`%c ${CARD_TAG} %c loaded`, "color: white; background: #d35400", "");

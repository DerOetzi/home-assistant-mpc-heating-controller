# Vorlauf- und Warmwassersteuerung — Umsetzungsplan

Spec: `docs/superpowers/specs/2026-11-05-vorlauf-warmwasser-design.md`.
Umsetzung nativ, kein Agent. Tests nach jedem Task: `PYTHONDONTWRITEBYTECODE=1 ~/.venvs/heating-controller/bin/python -m pytest -q -p no:cacheprovider`, am Ende ruff. Nach jeder Phase: Change-Set zeigen, Deploy (Neustart) nur nach Nutzer-Okay.

## Festlegungen (Builder)

- Subentry-Typen `flow` und `hot_water` neben `room`. `async_get_supported_subentry_types` liefert alle drei. Der Flow bricht ab (`already_configured`), wenn der Typ schon existiert.
- Gerät je Subentry: Identifier `(DOMAIN, subentry_id)`, Unique-IDs `f"{subentry_id}_{suffix}"`. `suggested_object_id` deutsch passend zu den alten Helfern, damit die Umbenennung entfällt, wo die alte ID vorher gelöscht ist.
- Logik in reinen Python-Klassen ohne HA (testbar wie `controller/mpc`):
  - `control/write_budget.py`
  - `control/flow.py` (Kurve, Stellgrößen-Regel)
  - `control/hot_water.py` (Moduswahl)
  - `control/legionella.py`
  - `control/circulation.py`
  - `control/charge_plan.py` (Abkühl-/Laderate, Planliste)
  - `control/heating_season.py`

  HA-Anbindung in `flow_device.py`, `hot_water_device.py` (Listener, Schreiben, Store).
- Gespeicherter Zustand je Subentry in eigenem Store (`store.py` erweitern): Budgetzähler, letzter eigener Schreibvorgang, Modus-Zeitstempel, Raten, Legionellenschutz-Zustand, Heizanforderungs-Ringpuffer.
- Schreiben mit eigenem `Context`, damit fremde Änderungen erkennbar sind.

## Phase 1 — Hub: Budget, Erreichbarkeit, Heizsaison

1. **WriteBudget** (`control/write_budget.py`): Prioritäten, Reserve, Tagesmaximum, UTC-Tageswechsel, Zähler verworfen. Tests: Grenzfälle Reserve, Entity fehlt, Tageswechsel.
2. **Hub-Konfig**: `rate_limit_remaining_entity`, `rate_limit_reserve`, `max_writes_per_day`, `max_flow_gap` in Hub-Flow und Reconfigure. Sensor `schreibbudget`, `binary_sensor.waermeerzeuger_erreichbar`.
3. **Heizsaison** (`control/heating_season.py`): Ringpuffer 5 min über 24 h (persistiert), Stunden über Schwelle, letzte Anforderung, Empfehlungen ein/aus, `number`s für Schwellen und 48 h. Tests mit synthetischem Verlauf, Neustart mitten im Fenster.

## Phase 2 — Subentry `flow`

4. **Kurve** (`control/flow.py`): Formel 1:1 aus Template (Begrenzung auf Kurvenpunkte, Dreieck um 0 °C). Tests gegen Ist-Werte (−13/54, 18/29, 2/4).
5. **Regel**: Soll aus `lead_min_flow` + Marge, `charge_flow`, Schwelle. Stellgröße je Modus, Hysterese, Haltezeit, Rückfall, WP aus, Erreichbarkeit, fremde Änderung. Tests je Zweig.
6. **Subentry-Flow + Gerät**: Config/Reconfigure (`actuator_mode`, Entity, Grenzen aus Attributen vorbelegt, Rückfall), `number`s, `switch.vorlauf_schreiben`, Sensoren. Hub liest Schwelle und Kurve intern, `CONF_FLOW_THRESHOLD_ENTITY`/`CONF_FLOW_CURVE_ENTITY` entfallen, wenn `flow` existiert.
7. **Deploy-Halt**: Change-Set, Nutzer-Okay, Neustart, Migration Schritte 1–5 für Vorlauf/Heizsaison, Schattenbetrieb.

## Phase 3 — Subentry `hot_water`

8. **Prüfpunkt Onecta**: Attribute von `water_heater.heizung` und Gerätesensoren während einer Ladung lesen. Gibt es ein Signal für Warmwasserbetrieb aktiv, ist es das Haupt-Ladesignal.
9. **Moduswahl** (`control/hot_water.py`): Blueprint 1:1, also Gates, Stabilisierungszeiten, Release, Haltezeit, Fenster, Zwangsladung, Boost-Maximum, Manuell mit Rückfall. Tests aus Blueprint-Szenarien (Sommer, Winter-Überschuss, Batterie entlädt, Nacht, Zwang bei Speicher 50).
10. **Legionellenschutz** (`control/legionella.py`): Termin, Rückfallzeit, PV-Bedingung, einmal am Tag, Prüfung Austritt/Rücklauf/Haltezeit, Wiederholung, Fehlschlag-Zustand.
11. **Zirkulation** (`control/circulation.py`): Regeln 1–5, Spitzenfenster, Zapfen, Rückfall-Takt, Freigabe Dauerlauf.
12. **Subentry-Flow + Gerät**: Abschnitte Eingänge, Sollwerte, Fenster, Legionellenschutz, Zirkulation (optional). Entities laut Spec. Schreiben über Budget.
13. **Ladeerkennung**: Speicheranstieg + Vorlauf-Heuristik (bzw. Signal aus Task 8). `HotWaterChargeDetector` bekommt optionale externe Ladeinfo vom Hub.
14. **Deploy-Halt**: Change-Set, Okay, Neustart, Automationen aus, Subentry anlegen, Helfer migrieren.

## Phase 4 — Ladeplan im MPC

15. **Ladeplan** (`control/charge_plan.py`): Abkühlrate und Laderate lernen (persistiert), Planliste `night_end`, `charge_window`, `legionella`, `reheat`. Sensor `warmwasser_naechste_ladung`. Tests mit synthetischem Speicherverlauf inkl. Zapfung.
16. **Controller**: `ChargeWindow` im `RoomMpcInput`, `_calculate_charge_preheat` (Zuschlag, Vorlaufzeit, Deckel, `charge_at_risk`), `charge_min_flow_temperature_c`. Tests: massearmer Raum (Duschbad-Parameter) gefährdet, schwerer Raum nicht, Deckel greift.
17. **Hub**: Fenster an Räume, `charge_flow` mit Deckel in `flow`-Regel, `required`-Schreiben bei Anhebung/Rücknahme. Learner: UA-Strecke endet bei Vorheizbeginn.
18. **Deploy-Halt**: Change-Set, Okay, Neustart, eine Zwangsladung beobachten (Duschbad/Rebekka gegen 05.11.).

## Phase 5 — Doku

19. `strings.json`, `translations/{en,de}.json`, README, `docs/mpc-guide.md`, Skill (Regel 7, `entscheidungen.md`, `diagnose.md`, `tagesauswertung.md`), Hausdoku `klima.md`, helper-collection README-Hinweis. Doku je Phase mitziehen, hier nur Abschluss-Durchsicht.

## Deploy-Schritte je Phase (nicht Teil der Umsetzung)

Siehe Spec, Abschnitt Migration. Meldungs-Automationen nach jedem Deploy anpassen, bevor die alten gelöscht werden.

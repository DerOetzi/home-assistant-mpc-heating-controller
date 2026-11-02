# Planer mit echtem Stellglied, zeitbasierte Glättung, Fremdwärme entfernen, UA-Wetterabhängigkeit messen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Der MPC plant mit der Leistung, die eine Ventilöffnung wirklich bringt (wie Beobachter und Lerner schon rechnen), die Bedarfsglättung wirkt pro Zeit statt pro Aufruf, der Fremdwärme-Schätzer entfällt (UA-Bilanz ist einziger Dauerzustands-Lerner), und die UA-Bilanz legt die Größen offen, mit denen sich ihre Wetterabhängigkeit prüfen lässt.

**Architecture:** `demand_pct` bedeutet künftig *Ziel-Ventilöffnung* (Mittel über die TRVs). Der Planer rechnet Öffnung → Leistung mit derselben Funktion wie die gemessene Ventilleistung (`calculate_measured_heating_power_w`), der Haltebedarf ist deren Umkehrung. Öffnung → TRV-Sollwertabstand kommt aus einer Kennlinie im Geräteprofil (YAML); ohne Kennlinie bleibt die heutige Abbildung. Glättung skaliert den Max-Schritt mit der seit dem letzten Aufruf vergangenen Zeit.

**Tech Stack:** Python 3.13, Home Assistant Custom Component, voluptuous, pytest.

**Spec:** Befunde 1, 5, 2/3 aus der Code-Analyse vom 2026-11-02 (Gespräch). Kurz:
- Befund 1: Planer rechnet `Leistung = verfügbar × Bedarf` linear ([controller.py:621](../../../custom_components/heating_controller/controller/mpc/controller.py)), echte Kette ist stark konkav. Beleg Hauptbad 02.11. 01:04–02:49: Bedarf 15 % → Ventile 50 %, 130–190 W statt geplant ~32 W; Bedarf 55–60 % → Ventile 90 %, 210 W statt ~118 W. Folge: Takten im Stundenrhythmus.
- Befund 5: `compute()` läuft bei jedem Zustandswechsel; Max-Schritt/Hysterese gelten pro Aufruf (Hauptbad 85 → 45 → 5 % in 6 s).
- Befund 2/3: UA-Faktor nimmt feste Gewinne/Erdreich und vorlaufabhängigen Fehler des Heizkörpermodells auf, hängt damit vermutlich am Außen-ΔT. Erst messen, Entscheidung beim Nutzer.
- Befund 6 (Nutzerentscheidung 02.11.: Fremdwärme komplett entfernen): UA-Bilanz und Fremdwärme-Beobachter schätzen beide den Dauerzustand der Nacht; bei fast konstantem ΔT je Nacht sind `UA·ΔT` und konstante Watt nicht trennbar. Folge: Übergabe (g) plus Anti-Windup erzeugen Ratsche ins Negative (Dani 60 → −73 W, Katharina 96 → −35 W seit 29.10.). Fremdwärme wirkt tagsüber nicht (eingefroren), nachts mit τ 12 h langsamer als eine Nacht; ihre ursprünglichen Aufgaben (Heizkörperwärme auffangen, Stellglied-Fehler) erledigen Phase 2 (`pi_heating_demand`) bzw. Task 5. Feste Gewinne trennt später G aus Regression über Nächte (Task 7).

## Global Constraints

- Tests: `cd /home/oetzi/Projekte/home-assistant-config/repos/home-assistant-mpc-heating-controller && PYTHONDONTWRITEBYTECODE=1 /home/oetzi/.venvs/heating-controller/bin/python -m pytest -q -p no:cacheprovider` (~90 s). Repo-`.venv` nicht nutzen. Kein `sed -i`/`perl -pi` auf dem Mount, Edit-Tool.
- **Kein `git add`/`git commit`.** Jede Task endet mit Change-Set-Auflistung (Dateien + Änderung + Teststatus); Nutzer stagt selbst. Commit nur auf Zuruf.
- HA-Daten nur per MCP (`ha_get_history`, `ha_get_state`, `ha_eval_template`); Attribut-Verläufe mit `significant_changes_only=false`. Nie sqlite.
- Alle 10 Räume gleichzeitig; kein Pilotraum, kein Einzelraum-Schalter.
- Keine Rückwärtskompatibilität der Konfig nötig, aber Hardware offen: Kennlinie im Profil **optional**, ohne sie gilt die heutige Abbildung.
- Nicht anfassen: Halte-/Mindestvorlauf-Formel (Auslegungsspreizung entschieden), Nachbarraum-Kopplung (Phase 4), Offset/Heizkurve. G im Haltevorlauf (ersetzt Phase-4-Punkt „Haltevorlauf mit Fremdwärme") nur nach Nutzerentscheidung in Task 7.
- **Deploy:** Task 4 und Task 5 gemeinsam in einem Neustart, alle Räume. Task 1 darf vorher allein deployt werden.
- Nach Code-Änderung: `docs/mpc-guide.md`, `README.md`, `strings.json`, `translations/en.json`, `translations/de.json` mitziehen.
- `.py`/YAML-Änderungen brauchen HA-Neustart; vor Neustart Nutzer fragen.
- Code-Stil wie Umgebung: englische Kommentare, die das *Warum* mit Hausbeleg nennen, Konstanten mit Kommentar oben im Modul.

## Review Focus

1. **TRV ohne `valve_position`-Entity** (Profil ohne Eintrag, z. B. künftiges Fremdmodell): Planer muss linear rechnen (wie Beobachter-Rückfall), kein Absturz, keine Division durch 0. → Test in Task 2.
2. **Raum mit zwei TRVs, einer mit, einer ohne Kennlinie/Ventil-Entity**: jeder TRV bekommt seinen eigenen Abstand, Leistung mischt korrekt. → Test in Task 2.
3. **Vorlauf unter Raumtemperatur / verfügbare Leistung 0**: Haltebedarf-Umkehrung liefert 100 % (heutiges Verhalten), Suche bleibt bei niedrigstem Bedarf. → Test in Task 2.
4. **Frostschutz erzwungen (`skip_demand_smoothing`)** nach Task 3: Bedarf springt sofort, zeitbasierte Glättung darf das nicht bremsen. → bestehender Test `test_skipped_smoothing_applies_the_demand_at_once` muss grün bleiben.
5. **Erster Aufruf nach Start** (kein vorheriger Zeitstempel): Glättung erlaubt vollen Schritt, nicht 0. → Test in Task 3.
6. **Kapazitäts-Replay ohne Fremdwärme** (Task 4): dauerhafte Gewinne landen nicht im Kapazitätsfaktor; das Replay vergleicht Temperatur*änderung*, ein konstanter Leistungsfehler bei UA-Faktor aus derselben Nacht ist klein. → Test in Task 4.
7. **Alte Store-Datei / alter Service-Aufruf mit `disturbance_w`** nach Update: Start ohne Fehler, Service lehnt Schlüssel ab (voluptuous), keine Exception im Log. → Test in Task 4.

---

### Task 1: UA-Bilanz legt Mittelwerte offen (Messgrundlage Befund 2/3)

Zuerst, damit bis zur Auswertung (Task 6) Nächte zusammenkommen.

**Files:**
- Modify: `custom_components/heating_controller/controller/mpc/types.py` (`UaBalanceInfo`)
- Modify: `custom_components/heating_controller/controller/mpc/learner.py` (`_balance_stretch`)
- Modify: `custom_components/heating_controller/sensor.py` (`UaLearningStatusSensor.extra_state_attributes`, ~Zeile 352)
- Modify: `strings.json`, `translations/{en,de}.json` (falls Attribute dort übersetzt sind — prüfen, sonst nichts), `docs/mpc-guide.md` Attribut-Tabelle
- Test: `tests/controller/mpc/test_learner.py`, `tests/test_sensor.py`

**Interfaces:**
- Produces: `UaBalanceInfo.last_mean_outdoor_c: float | None`, `last_mean_room_c: float | None`, `last_mean_flow_c: float | None`, `last_mean_heating_w: float | None`. Sensor-Attribute gleichen Namens (gerundet: °C auf 2, W auf 1 Stelle).

- [ ] **Step 1: Failing tests schreiben**
  - `test_balance_reports_the_mean_outdoor_room_flow_and_heat`: Strecke mit konstant Raum 21, Außen 5, Vorlauf 35, Heizkörperleistung 300 W → nach `LEARNED`: `last_mean_outdoor_c == pytest.approx(5)`, `last_mean_room_c == pytest.approx(21)`, `last_mean_flow_c == pytest.approx(35)`, `last_mean_heating_w == pytest.approx(300)`.
  - `test_rejected_balance_still_reports_the_means`: Strecke mit Drift > 0.3 K → Outcome `DRIFT`, Mittelwerte trotzdem gesetzt.
  - Sensor-Test: Attribute erscheinen gerundet, `None` wenn nie bilanziert.
  Vorhandene Helfer in `test_learner.py` für Strecken nutzen.
- [ ] **Step 2:** Tests laufen lassen, FAIL erwarten (AttributeError).
- [ ] **Step 3: Implementieren.** Zeitgewichtete Mittel in der vorhandenen Schleife in `_balance_stretch` mitsummieren (gleiches `duration_s`-Gewicht wie `temperature_difference_ks`; Vorlauf nur über Samples mit `flow_temperature_c is not None`, sonst `None`). Setzen für jeden Ausgang nach der Schleife (`SMALL_TEMPERATURE_DIFFERENCE`, `DRIFT`, Ablehnungen, `LEARNED*`). Beim Retry (k) gelten die Werte der zweiten Strecke.
- [ ] **Step 4:** Ganze Suite, PASS.
- [ ] **Step 5:** Doku-Tabelle „Attribute" in `docs/mpc-guide.md` ergänzen. Change-Set zeigen.

---

### Task 2: Kennlinie Abstand → Öffnung im Geräteprofil

**Files:**
- Create: `custom_components/heating_controller/controller/mpc/models/valve.py`
- Modify: `custom_components/heating_controller/profiles.py` (`PROFILE_SCHEMA`, `DeviceProfile`, `TrvBinding`, `_profile_from_config`, `resolve_trv`)
- Modify: `custom_components/heating_controller/controller/mpc/types.py` (`TrvConfig`)
- Modify: `custom_components/heating_controller/coordinator.py` (`_build_trv_configs`)
- Modify: `custom_components/heating_controller/device_profiles/danfoss_ally_zigbee2mqtt.yaml`, `danfoss_ally_zha.yaml`
- Test: `tests/controller/mpc/models/test_valve.py` (neu), `tests/test_profiles.py`

**Interfaces:**
- Produces:
  - `class ValveCharacteristic` (frozen dataclass) in `models/valve.py`, Feld `points: tuple[tuple[float, float], ...]` (Abstand °C, Öffnung %), aufsteigend in beiden Spalten.
  - `ValveCharacteristic.opening_pct(offset_c: float) -> float` — stückweise linear, unter erstem Punkt 0, über letztem Punkt letzte Öffnung.
  - `ValveCharacteristic.offset_c(opening_pct: float) -> float` — Umkehrung der stückweise linearen Kurve; Ziel ≤ erste Öffnung → erster Abstand, Ziel ≥ letzte Öffnung → letzter Abstand, flache Abschnitte → deren Anfang.
  - `TrvConfig.valve_characteristic: ValveCharacteristic | None = None`
  - `DeviceProfile.valve_characteristic` / `TrvBinding.valve_characteristic` gleichen Typs.
  - YAML-Schlüssel auf Profilebene: `valve_characteristic: [[offset_c, opening_pct], ...]`, optional, ≥ 2 Punkte, Schema prüft Monotonie.

- [ ] **Step 1: Kennlinie messen (Daten, kein Code).** Nur Daten **ab 02.11.2026 ~12:30** verwenden: seitdem `algorithm_scale_factor` 3 und Lastausgleich aus auf allen TRVs, ältere Daten haben anderes TRV-Verhalten. Mindestens 2 Heiznächte abwarten. Per MCP für alle 13 Danfoss-TRVs: Verlauf `climate.heizung_*` (Attribute `temperature`, `current_temperature`, `significant_changes_only=false`) und `sensor.heizung_*_pi_heating_demand`. Je Sollwert-Schreibvorgang: Abstand = Soll − `current_temperature` zum Schreibzeitpunkt; Öffnung = `pi_heating_demand` 10–15 min später, nur wenn bis dahin kein neuer Sollwert. Abstände auf 0,5 K klassieren, Median je Klasse. Nächte mit Adaption/Neustart auslassen. Ergebnis als Tabelle dem Nutzer zeigen. Startwerte aus Diagnose-Notizen zum Gegenprüfen: 0,6 K → 40–55 %, 1,1–1,5 K → ~55 %, ≥ 2,4 K → ~90 %. **Stopp, wenn die Streuung je Klasse > 30 Prozentpunkte** (Danfoss-PI integriert, dann taugt eine statische Kennlinie nicht) — Befund dem Nutzer melden, Task 2/5 neu planen (Alternative: innerer Regelkreis auf gemessene Öffnung).
- [ ] **Step 2: Failing tests** `test_valve.py`:
  - `test_opening_interpolates_between_points`: Punkte `((0.1, 0), (0.6, 45), (1.5, 60), (2.5, 90), (3.0, 100))` → `opening_pct(1.05) == pytest.approx(52.5)`.
  - `test_opening_below_first_point_is_zero`, `test_opening_above_last_point_is_last_opening`.
  - `test_offset_is_the_smallest_reaching_the_opening`: `offset_c(52.5) == pytest.approx(1.05)`; `offset_c(0) == 0.1`; `offset_c(120) == 3.0`.
  - `test_offset_round_trips_the_opening` (parametrisiert über 0…100 in 10er-Schritten: `opening_pct(offset_c(o)) >= o - 1e-9` bis Maximum).
  `test_profiles.py`:
  - `test_profile_reads_the_valve_characteristic` (beide mitgelieferten Danfoss-Profile haben Kennlinie, Punkte als Tupel).
  - `test_profile_without_characteristic_is_valid` (`valve_characteristic is None`).
  - `test_profile_rejects_a_falling_characteristic` (`vol.Invalid`).
- [ ] **Step 3:** FAIL prüfen.
- [ ] **Step 4: Implementieren.** `ValveCharacteristic` mit `__post_init__`-freier Validierung im Schema (`vol.All([[vol.Coerce(float)]], vol.Length(min=2), _ascending)`). Durchreichen Profil → `TrvBinding` → `TrvConfig` in `_build_trv_configs`. Gemessene Punkte aus Step 1 in beide Danfoss-YAMLs mit Kommentar (Datum, Datengrundlage).
- [ ] **Step 5:** Ganze Suite PASS. Change-Set zeigen.

---

### Task 3: Glättung pro Zeit statt pro Aufruf (Befund 5)

**Files:**
- Modify: `custom_components/heating_controller/controller/mpc/controller.py` (`_apply_demand_rate_limiting`, `_reset_demand_rate_limiting`, `__init__`)
- Modify: `custom_components/heating_controller/const.py` / `strings.json` / `translations/*` (Text „Max demand step per cycle" → „per 5 minutes"), `docs/mpc-guide.md` Abschnitt „Demand smoothing"
- Test: `tests/controller/mpc/test_controller.py`

**Interfaces:**
- Produces: Konstante `DEMAND_STEP_REFERENCE_S = SENSOR_POLL_INTERVAL_MINUTES * 60` in `controller.py`; `MpcRateLimitConfig.max_demand_step_pct` bedeutet „pro 300 s".

- [ ] **Step 1: Failing tests** (mit vorhandenem `_Clock(monkeypatch)` aus `test_controller.py`; er patcht `time.time` modulweit, gilt also auch für `controller.py`):
  - `test_demand_step_scales_with_the_time_since_the_last_call`: Hysterese 0, Haltezeit 0, Max-Schritt 20; kalter Raum; erster Aufruf bei t0, zweiter bei t0 + 6 s → Differenz ≤ `20 * 6 / 300 + 1e-9`.
  - `test_first_call_allows_the_full_step`: frischer Controller, ein Aufruf, kalter Raum → `demand_pct == 20` (von 0 aus).
  - `test_a_full_interval_allows_the_full_step`: zweiter Aufruf bei t0 + 300 s → Differenz bis 20.
- [ ] **Step 2:** FAIL prüfen.
- [ ] **Step 3: Implementieren.** Neues Feld `_last_rate_limit_ts: float | None`. Erlaubter Schritt = `max_demand_step_pct * clamp((now - last) / DEMAND_STEP_REFERENCE_S, 0, 1)`, bei `None` voller Schritt. Zeitstempel bei jedem Aufruf setzen (auch wenn Ausgabe gehalten wird). `_reset_demand_rate_limiting` setzt ihn ebenfalls. Hysterese und Haltezeit unverändert.
- [ ] **Step 4:** Ganze Suite. Bestehende Tests, die mehrere `compute()` direkt hintereinander erwarten, dass der Bedarf steigt (`_heating_room_controller`, `test_smoothing_steps_the_demand_down_slowly`, …), auf `_Clock` mit +300 s je Aufruf umstellen — Aussage der Tests bleibt gleich.
- [ ] **Step 5:** Doku + Übersetzungen. Change-Set zeigen.

---

### Task 4: Fremdwärme entfernen (Befund 6)

Beobachter behält Temperatur-Zustand, Heizkörper-Verzögerung (radiator lag) und Resync; nur der Störgrößen-Integrator fällt weg. Bis G gelernt ist (Task 7), trägt der UA-Faktor allein den ganzen Netto-Verlust beim ΔT der Nacht — Planer und Vorlauf sehen dieselbe Zahl.

**Files:**
- Modify: `controller/mpc/observer.py` (Integrator, Anti-Windup `_held_at_valve_limit`, `restore_disturbance`, `disturbance_limit_w`, `disturbance_update_allowed`-Parameter, Konstanten `OBSERVER_DISTURBANCE_TIME_CONSTANT_S`, `VALVE_SHUT_DEMAND_PCT`, `VALVE_FULL_DEMAND_PCT` falls nur dafür genutzt; `ObserverEstimate.disturbance_w`)
- Modify: `controller/mpc/learner.py` (`disturbance_update_allowed`, `LearnerHistoryEntry.disturbance_w`, `append_history(disturbance_w=…)`, Kapazitäts-Replay `_simulate_room_temperatures_c` ohne `previous.disturbance_w`, Übergabe (g) ~Zeile 733–747, `_pending_disturbance_transfer_w`, `consume_disturbance_transfer_w`; Kommentar Zeile ~75 prüfen)
- Modify: `controller/mpc/controller.py` (`disturbance_w`-Property, `restore_disturbance`, Observer-Aufruf mit Sperren ~Zeile 336–350, `_hold_demand_pct` ohne `− disturbance_w`, `_simulate_room_temperature_c`-Parameter `disturbance_w`, `run_learning_cycle` ohne Übergabe, Docstring ~Zeile 468)
- Modify: `controller/mpc/results.py` (`disturbance_w`)
- Modify: `coordinator.py` (`observer_store`, `_persisted_disturbance_w`, `_async_persist_disturbance`, `OBSERVER_PERSIST_MIN_CHANGE_W`, `async_recalibrate(disturbance_w=…)`)
- Modify: `store.py` (Observer-Store-Klasse entfernen)
- Modify: `__init__.py`, `const.py` (`ATTR_DISTURBANCE_W`, `MAX_RECALIBRATE_DISTURBANCE_W`, Schema `has_at_least_one_key(ATTR_UA_FACTOR, ATTR_CAPACITY_FACTOR)`), `services.yaml`
- Modify: `sensor.py` (`DisturbancePowerSensor` entfernen), `strings.json`, `translations/{en,de}.json` (`disturbance_power`, Service-Feld)
- Modify: `frontend/` nur falls die Karte `disturbance_power` liest (grep; heute kein Treffer)
- Modify: `docs/mpc-guide.md` (Stellen ~107, 136, 141, 185–190, 253, 273, 291, 312–335, 395, 419), `README.md` Zeile ~21
- Modify: `/home/oetzi/Projekte/home-assistant-config/.claude/skills/heating-controller/references/entscheidungen.md`, `references/diagnose.md` (Fremdwärme-Einträge)
- Test: `tests/controller/mpc/test_observer.py`, `test_learner.py`, `test_controller.py`, `tests/test_init.py`, `tests/test_sensor.py`

**Interfaces:**
- Produces: `RoomStateObserver.estimate(mpc_input)` ohne `disturbance_update_allowed`; `ObserverEstimate` ohne `disturbance_w`; `RoomMpcController` ohne `disturbance_w`/`restore_disturbance`; `async_recalibrate(ua_factor, capacity_factor)`; Service `heating_controller.recalibrate` nur `ua_factor`/`capacity_factor`.
- Task 5 rechnet Haltebedarf aus Verlust allein.

- [ ] **Step 1: Tests anpassen/schreiben (failing)**
  - Fremdwärme-Tests in `test_observer.py` (Integrator, Anti-Windup, Grenze, Sperren) und `test_learner.py` (Übergabe (g)) löschen — nicht umschreiben.
  - `test_observer_has_no_lasting_offset`: Raum konstant 0,5 K wärmer als Modell über 24 h → Schätzung folgt Messung (τ 5 min), keine Größe wächst; Vorhersage über Horizont nutzt nur Modell + Heizkörper-Verzögerung.
  - `test_balance_does_not_move_anything_but_the_factor`: gelernte UA-Bilanz ändert nur `learned_ua_factor`, Controller hat keine Übergabe mehr (Attribut existiert nicht).
  - `test_capacity_replay_without_disturbance` (Review Focus 6): Strecke mit konstantem Gewinn 50 W und dazu passend gelerntem UA-Faktor → Kapazitätsfaktor nach dem Replay innerhalb `1 ± CAPACITY_LEARNING_RATE` des Startwerts (höchstens ein Lernschritt, kein Weglaufen).
  - `test_hold_demand_is_the_loss_at_the_setpoint`: Haltebedarf = Verlust bei Soll / verfügbare Leistung (heutige lineare Abbildung, Task 5 ändert die Abbildung).
  - `test_init.py`: `test_recalibrate_rejects_disturbance` (Review Focus 7, `vol.Invalid`/`ServiceValidationError` je nach heutigem Muster), bestehende Recalibrate-Tests ohne `disturbance_w`.
  - `test_sensor.py`: kein `disturbance_power`-Sensor mehr angelegt.
- [ ] **Step 2:** FAIL prüfen.
- [ ] **Step 3: Implementieren.** Alles oben Gelistete entfernen, keine Kompatibilitäts-Pfade. Alte Observer-Store-Datei: beim Setup einmal `Store(hass, <alte Version>, <alter Key>).async_remove()` mit Kommentar „drop after season 2026/27" — so bleibt keine Leiche in `.storage`. Konstanten, die nach dem Entfernen ungenutzt sind, mit entfernen (grep).
- [ ] **Step 4:** Ganze Suite PASS. `grep -rni disturbance custom_components tests` → nur Store-Aufräumzeile und Wortverwendungen ohne Fremdwärme-Bezug (z. B. Kommentar Zeile 37 learner.py „shortened by a disturbance" umformulieren auf „interruption").
- [ ] **Step 5: Doku.** `mpc-guide.md`: Beobachter-Abschnitt (~312–335) neu: Zustand + Verzögerung + Resync, kein Dauer-Offset; Begründung Befund 6 in einem Satz. Stellen „disturbance is held" streichen. README-Satz anpassen. `entscheidungen.md`: Einträge „Fremdwärme-Beobachter" und „(g)" ersetzen durch „**Keine Fremdwärme** (02.11.): zweiter Schätzer für denselben Dauerzustand wie UA, nicht trennbar bei konstantem ΔT, Ratsche durch (g)+Anti-Windup. Feste Gewinne später als G (Task 7)."; Offen-Punkt (g) negativ streichen; Phase-4-Punkt „Haltevorlauf mit Fremdwärme" → „G im Haltevorlauf". `diagnose.md`: Fremdwärme-Fehlbilder entfernen.
- [ ] **Step 6:** Change-Set zeigen. Nach Neustart (gemeinsam mit Task 5, Nutzer fragt): verwaiste Entities `sensor.<raum>_*disturbance*`/Fremdwärme-Sensor je Raum per MCP finden (`ha_search`) und nach Nutzer-Okay mit `ha_remove_entity` entfernen. Dashboards/Karten auf Referenzen prüfen.

---

### Task 5: Planer rechnet in Ventilöffnung (Befund 1)

**Files:**
- Modify: `custom_components/heating_controller/controller/mpc/models/emitter.py`
- Modify: `custom_components/heating_controller/controller/mpc/controller.py` (`_simulate_room_temperature_c`, `_hold_demand_pct`, `compute`)
- Modify: `docs/mpc-guide.md` (Abschnitte „TRV target mode", „Target band", „Valve position"), `README.md`, `strings.json` + `translations/*` (Name/Beschreibung `heating_demand` → „Ziel-Ventilöffnung"; Entity-ID bleibt)
- Test: `tests/controller/mpc/models/test_emitter.py`, `tests/controller/mpc/test_controller.py`

**Interfaces:**
- Consumes: `TrvConfig.valve_characteristic`, `ValveCharacteristic.offset_c` (Task 2); zeitbasierte Glättung (Task 3); Controller ohne Fremdwärme (Task 4).
- Produces:
  - `HeatEmitterModel.planned_heating_power_w(room_temperature_c: float, flow_temperature_c: float | None, opening_pct: float) -> float` — ruft `calculate_measured_heating_power_w(room, flow, [opening_pct if trv.valve_position_configured else None for trv], opening_pct)` auf. Damit rechnen Planer, Beobachter und Lerner dieselbe Kurve; TRV ohne Ventil-Entity linear.
  - `HeatEmitterModel.opening_for_power_pct(room_temperature_c: float, flow_temperature_c: float | None, power_w: float) -> float` — Bisektion 0…100 % auf `planned_heating_power_w`, Genauigkeit 0,5 %; `power_w <= 0` → 0; voll offen reicht nicht oder verfügbare Leistung 0 → 100.
  - `calculate_target_temperatures(...)` Signatur unverändert; im Modus RELATIVE mit Kennlinie: jeder TRV bekommt dieselbe Ziel-Öffnung (keine `distribution_weight`), Abstand = `valve_characteristic.offset_c(demand_pct)`.

- [ ] **Step 1: Failing tests Emitter:**
  - `test_planned_power_follows_the_valve_curve`: ein Panel-TRV mit `valve_position_configured=True`, Vorlauf 40, Raum 21 → `planned_heating_power_w(..., 10) == pytest.approx(0.44 * available, rel=0.05)` (Zahl aus Docstring `power_fraction_at_opening`), `…(50) ≈ 0.87 * available`.
  - `test_planned_power_is_linear_without_a_valve_entity` (Review Focus 1): `valve_position_configured=False` → `planned(…, 30) == pytest.approx(0.3 * available)`.
  - `test_planned_power_mixes_trvs_with_and_without_valve_entity` (Review Focus 2).
  - `test_opening_for_power_inverts_the_planned_power`: für Öffnungen 10/30/70 → `opening_for_power_pct(planned(o)) == pytest.approx(o, abs=0.5)`.
  - `test_opening_for_power_is_full_when_the_flow_is_too_cold` (Review Focus 3): Vorlauf 20, Raum 21 → 100.
  - `test_relative_target_uses_the_valve_characteristic`: Kennlinie wie Task 2, Ablesung 20,3, Step 0,5, Bedarf 52,5 % → Abstand 1,05 → 21,35 → Ziel `21.5` (Rundungsregel unten); Bedarf 10 % → Abstand 0,1 + 0,5·(10/45)… laut Kennlinie ≈ 0,21 → 20,51 → Rasterpunkt 20,5, aber Mindestregel `ceil(20,4/0,5)·0,5 = 20,5` → Ziel `20.5`.
  - `test_two_trvs_get_the_same_opening_with_a_characteristic`: zwei TRVs, Bedarf 100 → beide Abstand 3,0 (statt heute ~1,8).
  - `test_relative_target_without_characteristic_is_unchanged`: bestehende Abbildung, bestehende Tests bleiben grün.
- [ ] **Step 2:** FAIL prüfen.
- [ ] **Step 3: Implementieren Emitter.** Rundung mit Kennlinie: Ziel = größerer Wert aus `ceil((local + RELATIVE_MIN_OFFSET_C) / step) * step` und `round((local + offset) / step) * step` — nächster Rasterpunkt statt immer aufrunden (Aufrunden verschob kleine Abstände um bis zu 0,5 K, ein Danfoss öffnet dabei schon ~45 %). Regel „aktuellen Sollwert behalten, solange über Ablesung und ≤ 1 Schritt entfernt" bleibt. Ein/Aus-Schwellen `OPEN_ACTIVE_DEMAND`/`MIN_ACTIVE_DEMAND` bleiben, gelten jetzt in Öffnung.
- [ ] **Step 4: Failing tests Controller:**
  - `test_planner_prefers_a_small_opening_when_a_small_one_gives_most_heat`: Hauptbad-ähnlich (Vorlauf 35, Raum 0,2 K unter Soll, Außen 8, `valve_position_configured=True`) → gewählter Bedarf ≤ 30 % (heute ≥ 55 %). Zahl vorher mit dem neuen Modell nachrechnen und im Test kommentieren.
  - `test_hold_demand_is_the_opening_that_gives_the_loss` (ersetzt `test_hold_demand_is_the_loss_at_the_setpoint` aus Task 4): Raum am Soll → `planned_heating_power_w(result.demand_pct)` ≈ Verlust bei Soll (±5 %, Suchraster 5 %).
  - `test_requested_power_is_the_planned_power`: `result.requested_heating_power_w == pytest.approx(planned_heating_power_w(room, flow, result.demand_pct))`.
- [ ] **Step 5:** FAIL prüfen.
- [ ] **Step 6: Implementieren Controller.** In `_simulate_room_temperature_c` `target_power_w = planned_heating_power_w(simulated_room, flow, demand_pct)`. `_hold_demand_pct` = `opening_for_power_pct(target, flow, max(0, Verlust))`. `requested_heating_power_w` = `planned_heating_power_w(room, flow, stabilized_demand_pct)`. Erholungsvorlauf (Bedarf 100) bleibt rechnerisch gleich. Beobachter und Lerner nicht ändern.
- [ ] **Step 7:** Ganze Suite. Bestehende Tests mit festen Bedarfszahlen prüfen: anpassen nur, wo die Zahl aus der linearen Annahme folgte; im Testkommentar sagen warum.
- [ ] **Step 8:** Doku + Übersetzungen (Bedarf = Ziel-Ventilöffnung, Kennlinie im Profil, README-Abschnitt Geräteprofil: neues optionales Feld). Change-Set zeigen. Neustart (gemeinsam mit Task 4) erst nach Nutzer-Okay.

---

### Task 6: Nach Neustart beobachten und UA-Wetterabhängigkeit auswerten (Befund 1 + 6 prüfen, Befund 2/3 messen)

Keine Code-Änderung. Ergebnis an Nutzer, Entscheidungen bei ihm.

- [ ] **Step 1: Wirkung Befund 1 (erste 2 Nächte nach Neustart),** alle 10 Räume: Stundenmittel `sensor.<raum>_heizleistungsanforderung` und Raumtemperatur; Zahl der Ventil-Zu/Auf-Wechsel pro Nacht je TRV (`pi_heating_demand` < 5 ↔ > 30) vorher (Nächte nach 02.11. mit neuen TRV-Einstellungen, aber altem Planer — Nacht 01./02.11. hatte noch Scale-Factor 1 und Lastausgleich, taugt nur als zweite Referenz) vs. nachher; `trv_setpoint_writes`-Zuwachs pro Nacht. Erwartung: weniger Wechsel, Raum ruhiger im Band, Schreibzahl nicht höher. Nächte mit Neustart, Daikin-Lücke, WW-Ladung, Adaption kennzeichnen.
  **Befund 6 (ohne Fremdwärme):** je Raum Stunden mit Raum dauerhaft unter Band (Soll − 0,1 K) bei Ventil < 95 % zählen — Zeichen für bleibenden Fehler ohne Integrator. Besonders Flur EG (hatte 126 W Fremdwärme), Dani, Katharina. UA-Faktoren der ersten 2 Nächte gegen vorher: Flur EG soll steigen bzw. Netto-Verlust aufnehmen. Bleibender Fehler > 0,1 K über ≥ 2 Nächte in einem Raum → Nutzer melden, nicht selbst Integrator nachrüsten.
- [ ] **Step 2: Befund 2/3 (frühestens nach 7 Lernnächten seit Task 1),** je Raum Verlauf `sensor.<raum>_ua_lernstatus` mit `significant_changes_only=false`: je Nacht `last_target_factor`, `last_mean_outdoor_c`, `last_mean_room_c`, `last_mean_flow_c`, `last_mean_heating_w`, Outcome. Nur `learned`/`learned_at_limit`.
- [ ] **Step 3: Auswertung.** Je Raum lineare Regression `mean_heating_w = UA · (room − outdoor) − G` (zwei Parameter). Kriterium „wetterabhängig bestätigt": in ≥ 6 von 10 Räumen G > 0 mit G größer als seine Standardabweichung **und** Spanne Außen-Mittel über die Nächte ≥ 4 K. Zusätzlich Zielfaktor gegen `mean_flow_c` auftragen (Befund 3). Unter 4 K Spanne: Ergebnis „noch nicht entscheidbar", weiter sammeln.
- [ ] **Step 4:** Tabelle + Empfehlung an Nutzer (G je Raum nach Task 7 vs. Phase-4-Kopplung, G im Haltevorlauf ja/nein). Nicht umsetzen. Entscheidung danach in `.claude/skills/heating-controller/references/entscheidungen.md` eintragen, Diagnose-Erkenntnisse in `references/diagnose.md`.

---

### Task 7: UA und fester Gewinn G aus Regression über Nächte (nur nach Freigabe aus Task 6)

Startet nur, wenn Task 6 Step 3 „wetterabhängig bestätigt" liefert **und** Nutzer freigibt. Vorher Detailplan (Tests mit Zahlen, Dateien) als eigene Plan-Datei schreiben; hier nur die festgelegten Entscheidungen.

**Modell:** Verlust = `UA · (Raum − Außen) − G`. `G` in W je Raum, darf negativ sein (Erdreich/Bodenplatte zieht konstant ab). Grenze `|G| ≤ 0,5 · room_heat_load_w`.

**Lernen:**
- Jede gültige Bilanz-Strecke (Outcome `learned*`, Mittelwerte aus Task 1) als Punkt (`mean_ΔT`, `mean_heating_w` + Speicherterm) in Ringpuffer, letzte 14 Nächte, persistent im Lern-Store.
- Fit nur, wenn ≥ 5 Punkte **und** Spanne `mean_ΔT` ≥ 4 K: gewichtete Kleinste-Quadrate für UA und G, Schritt 30 % wie heute.
- Sonst: G gehalten, UA aus Einzelnacht wie heute, aber mit Gegenwert `(P + G) / ΔT`. Ohne gelerntes G (Start) gilt G = 0 — heutiges Verhalten.
- Keine Übergabe, kein zweiter Schätzer: G ist einziger Ort für ΔT-unabhängige Wärme.

**Verwendung:** Planer und Haltebedarf mit `UA·ΔT − G`. Halte-/Mindestvorlauf: G nur nach Nutzerentscheidung aus Task 6 Step 4 (ersetzt Phase-4-Punkt).

**Sichtbar:** Attribut `learned_gain_w` + `gain_fit_points` am UA-Lernstatus-Sensor; `recalibrate` bekommt optional `gain_w`.

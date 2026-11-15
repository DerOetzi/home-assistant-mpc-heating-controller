# Offener Umsetzungsplan (Stand 2026-11-15)

Nur was noch zu tun ist, mit den Angaben für die Umsetzung. Erledigtes steht im Code, in `mpc-guide.md` und in der Git-Historie. Getroffene Entscheidungen stehen im Skill `heating-controller` (`references/entscheidungen.md`).

## Arbeitsweise (gilt für jeden Schritt)

Pfade unten sind relativ zum Repo. Abkürzung `CC/` = `custom_components/heating_controller/`, `MPC/` = `CC/controller/mpc/`.

1. Skill `heating-controller` laden, `references/entscheidungen.md` lesen.
2. Nur einen Unterabschnitt (z. B. 2a) je Durchgang. Vorher die dort genannten Dateien ganz lesen.
3. **Stopp und Nutzer fragen**, wenn:
   - eine hier genannte Funktion, Konstante oder Datei nicht existiert oder anders aussieht als beschrieben;
   - ein bestehender Test rot wird, der laut Abschnitt nicht angepasst werden soll;
   - eine Entscheidung nötig ist, die hier nicht steht.
   Nicht raten, nicht umbauen was nicht im Abschnitt steht.
4. Dateien nur mit Edit/Write ändern. Kein `sed -i`, kein `perl -pi` (Netzlaufwerk).
5. Tests: `PYTHONDONTWRITEBYTECODE=1 ~/.venvs/heating-controller/bin/python -m pytest -q -p no:cacheprovider` (ca. 90 s), danach `~/.venvs/heating-controller/bin/ruff check .` und `ruff format --check .`.
6. Doku im selben Durchgang mitziehen: `docs/mpc-guide.md`, `README.md`, `CC/strings.json`, `CC/translations/en.json`, `CC/translations/de.json`, Skill-Dateien `references/diagnose.md` und `references/tagesauswertung.md`. Je Abschnitt steht unten, was genau.
7. Kommentare im Code englisch, wie Bestand. Keine Daten, keine „entschieden weil"-Sätze im Code.
8. Alle Räume gleichzeitig. Kein Pilot-Raum, keine Raum-Sonderfälle im Code.
9. Am Ende: Change-Set auflisten (Dateien, was geändert, Teststatus), dann stoppen. Nicht stagen, nicht committen, HA nicht neu starten. Neustart nur nach Nutzer-Okay.
10. Erledigten Abschnitt aus diesem Plan streichen. Neue Entscheidungen knapp in `references/entscheidungen.md`.
11. HA-Daten nur über MCP-Werkzeuge. Nie Datenbank direkt lesen, kein `curl` auf HA. Vorlauf-Offset nie selbst setzen.

## Zielbild

Raum regelt der TRV selbst (eigener PI, Sollwert über `climate.set_temperature` = Typ 1). Integration setzt je Raum nur Sollwert des Modus (plus langsame Raumfehler-Korrektur), `heat_available` und bei offenem Fenster Frostschutz-Soll (Fenster-Flag am TRV bleibt bei Automationen), beobachtet `pi_heating_demand`, lernt daraus UA, Kapazität (Masse; Lüften zunächst nur beobachtet), G und Sonne und regelt damit den gemeinsamen Vorlauf: Vorsteuerung aus Modell (Haltevorlauf), asymmetrische Rückkopplung auf höchsten Demand der Vorlauf-Leiträume, Boost nur nach Moduswechsel aus Kapazität. Kein Planer, keine Ventilkennlinie.

## Reihenfolge

| Schritt | Was | Bedingung für Start |
|---|---|---|
| 1 | 1d Kapazität beobachten | läuft seit Neustart 15.11., bis Neustart A |
| 2 | 2a–2g Umbau TRV-Ansteuerung, Neustart A | sofort |
| 3 | 3 Vorlauf-Rückkopplung, Neustart B | nach 2, ein paar Tage Betrieb ohne Auffälligkeit (Takten, Raumfehler) |
| 4 | 4a Gewinne auswerten | parallel, kein Code |
| 5 | Boiler-Modus TRV (5, ~01.12.) | Z2M 2.15.0 da, kein anderer Neustart in den 3 Tagen davor |
| 6 | 4b Gewinne im Lerner, 4c Vorsteuerung mit Gewinnen + Boost, Neustart C | Nutzer hat 4a freigegeben, 3 läuft stabil |
| 7 | 6 Ladeplan | nur wenn Nutzer nach 6a Bedarf bestätigt |
| 8 | 7 Simulationen | nach 4c und Nutzer-Okay |

## 1. Kapazität

1a–1c (Sprungstrecken, Untergrenze 0,15, Sonnensperre über Einstrahlung) seit Neustart 15.11. aktiv.

### 1d. Auswirkungen prüfen (bis Neustart A, kein Code)

Wenige Tage, Moduswechsel selten (nur Dani, Urlaub) — Ergebnis eher Plausibilität als Bewertung. In Tagesauswertung je Raum:
- `sensor.<raum>_kapazitatsfaktor` Verlauf; Zustände und `last_stretch_*` am Kapazitäts-Lernstatus.
- Überschwingen über Soll nach Aufheizen (> 0,3 K) melden.
- Abkühl-Schätzung (`estimated`) gegen Aufheiz-Schätzung je Raum. Liegt Abkühlen dauerhaft > Faktor 2 über Aufheizen: an Nutzer melden.
- Daten ab Neustart A nicht mit davor mischen.

## 2. Umbau TRV-Ansteuerung (Neustart A)

Ein Neustart für 2a–2g zusammen. 2a (Sollwert, Fenster, Wärmeangebot, Korrektur), 2b (Planer, Kennlinie), 2c (Lerner: Modus-Soll, Toleranz, Lüften-Kapazität), 2d (Heizkörper-Check), 2e (Geräteprofil), 2f (Demand-Eingang) und 2g (Doku) umgesetzt, warten auf Neustart A.

Nach Neustart A in Tagesauswertung: Raumfehler je Raum, Korrekturverlauf, Ventiltakten je TRV (Faktor 1), Schreibzahl, Fenster-Ablauf.

## 3. Vorlauf-Rückkopplung (Neustart B)

**Ist-Stand:**
- `CC/hub.py:347–376`: `_lead`; `lead_hold_flow` = Max über **Heizgrenzen**-Leiträume, Wert **nach** Überschuss-Gate (Heizanforderung, Reserve); `lead_min_flow` = Max über Vorlauf-Leiträume von max(Halten, Recovery) (`controller.py:495`).
- Vorlaufregler nutzt nur `lead_min_flow` + `margin_k` (1 K) (`CC/flow_device.py:174`, `CC/control/flow.py:199`). `FlowController.decide` mit Quellen, Rundung, Hysterese, Schreibbudget.
- Recovery springt bei jedem Raumdefizit > 0,1 K an (`controller.py:608`).

**Neu:**
- **Lead-Wert** `lead_feedforward_flow` = Max über Vorlauf-Leiträume des ungegateten Haltevorlaufs (`_calculate_hold_flow_temperature_c`). Heizgrenze/`lead_hold_flow` bleibt unverändert für Heizanforderung.
- **Recovery** nur noch nach Moduswechsel nach oben (Sprung Modus-Soll ≥ `JUMP_MIN_TARGET_STEP_C`), bis Raum Soll − 0,2 K erreicht oder Horizont abgelaufen. Nicht nach Fenster-zu (Lüften nur beobachtet), nicht bei Korrekturschritten. Wert `lead_boost_flow` = Max über Räume mit laufendem Boost.
- **Rückkopplung** `feedback_k` (Hub-Integrator, in `HubStore._export`):
  - `lead_demand_pct` = Max der Raum-Demands (2f) über Vorlauf-Leiträume, ohne Räume mit Fenster offen, Frostschutz, Heizkörper-Urteil, Boost, unbekanntem Demand.
  - Hoch: Demand > 90 % und dieser Raum ≥ 0,1 K unter Modus-Soll + Korrektur-Ziel, oder Demand ≥ 98 % → +1 Schritt, frühestens 30 min nach letztem Schritt.
  - Runter: Demand < 80 % seit ≥ 2 h am Stück → −1 Schritt, danach wieder 2 h.
  - Kein Doppelschritt bei Sättigung.
  - Klemmen `feedback_k` auf ±5 K (Konstante).
  - Einfrieren (kein Schritt, Zeitgeber laufen nicht) bei: Schalter „Vorlauf schreiben" aus, letzter Stellwert nicht geschrieben (Budget, Haltezeit), Heizbetrieb aus/WP nicht erreichbar, WW-Ladung, Abtauung (Vorlauf unter Schwelle), laufender Boost.
- **Vorlauf-Ziel** = clamp(`lead_feedforward_flow` + `feedback_k`, Vorlaufschwelle, min(`lead_feedforward_flow` + 6 K, Kurve + max. Offset, Auslegungsvorlauf)); bei Boost max(dieses, `lead_boost_flow`). `margin_k` entfällt (Konstante, Zahl-Entity, Übersetzungen).
- Überschuss-Gate unverändert, Eingang gemessener Demand.
- Attribute am Vorlauf-Gerät: `lead_demand_pct`, Leitraum, `feedback_k`, `feedforward_flow`, eingefroren ja/nein mit Grund, letzte Änderung.

Tests (`tests/control/test_flow.py`, `tests/test_hub.py`): 95 % + Raum unter Soll → hoch; 95 % + Raum auf Soll → nichts; 99 % → hoch; 60 % über 2 h → runter, 60 % über 1 h → nichts; Mindestabstand; Klemmen ±5 K; Obergrenze Vorsteuerung + 6 K; Einfrieren je Grund; Raum mit Fenster offen zählt nicht; Boost hebt Ziel und friert ein.

Doku: `mpc-guide.md` „Flow control", Karte (Leit-Demand, `feedback_k`), Skill-Dateien (Offset-Empfehlungsregeln in `tagesauswertung.md` an Rückkopplung anpassen).

## 4. Gewinne und Sonne

Stand: Nacht-Regression `[UA, G]`, Stunden-Profil, Sonne je Fläche (Open-Meteo GTI) lernen und sind sichtbar (`sensor.<raum>_zusatzwarme`, UA-Lernstatus-Attribute, Karte). Wirken noch nicht.

Modell (je Raum, W): `C · dT/dt = P_hk − UA · (T − T_a) + G + g(Block, Tagesart) + Σ s_f · Ī_f + Σ w_o · Ĩ_o`. Ein Term wirkt nur, wenn bestimmt, sonst 0.

### 4a. Auswertung (kein Code)

Eigener Abschnitt der Tagesauswertung, alle 10 Räume, per MCP (`ha_get_state` auf `sensor.<raum>_ua_lernstatus`, `sensor.<raum>_zusatzwarme`, Attribut-Verläufe mit `significant_changes_only=false`):

| Ebene | Lesen | Plausibel wenn |
|---|---|---|
| Nacht | `learned_gain_w`, `gain_sigma_w`, `gain_fit_points`, `gain_determined`, `regression_ua_factor` | Schlafzimmer G ≈ 100–150 W; `regression_ua_factor` nahe `ua_factor` |
| Stunde | Profil Werktag/sonst, Sonnenkoeffizienten, `last_hour_outcome` | Süd-/Westräume `s > 0`, Nord ≈ 0, Schlafzimmer tagsüber negativ, Esszimmer Abendblock positiv |
| Güte | `residual_rms_w`; Anteil gesperrter Stunden | RMS deutlich unter 10 % Norm-Heizlast |

Ergebnis: Tabelle je Raum (bestimmt ja/nein je Term, Wert, auffällig) + Empfehlung „4b/4c freigeben / warten". Daten vor/nach Neustart A getrennt.

### 4b. Gewinne im Lerner

`MPC/learner.py`:
- UA-Ziel in `_balance_stretch` nach `stored_heat_j`: `gain_j = steady_gain_w · window_duration_s` (0 ohne Gewinn-Modell), `target_ua = (heat_j − stored_heat_j + gain_j) / temperature_difference_ks`. `STORAGE_DOMINATED`-Prüfung und `add_night_point(...)` weiter **ohne** Gewinn. Grenzen 0,15/2 bleiben.
- Sprung- und Lüften-Strecken: `_gain_w_at(ts)` (`breakdown(...).total_w` mit Lerner-eigenem `_profile_index_at`, `_sun_source`), in Wärmebilanz `+ gain · dt`.
- Abkühlstrecken lernen wie Aufheizstrecken (Ausgang `learned`) **nur wenn `gain_determined`**; sonst weiter `estimated`.
- `_jump_sun_free`: sind alle Sonnen-Terme bestimmt und `sun` nicht `None` → `True`.

Tests (`test_learner.py`): UA-Ziel mit G = 50 W, Bilanz 300 W, ΔT 15 K, Drift 0 → `last_target_factor == (350/15)/base_ua`; Abkühlstrecke mit Gewinn 100 W bestimmt → C ±20 %, `learned`; Gewinn unbestimmt → `estimated`; Sonnen-Terme bestimmt + 200 W/m² → Strecke zugelassen.

### 4c. Vorsteuerung mit Gewinnen und Boost (Neustart C)

`MPC/controller.py`:
- `gain_w(ts)` und `_gain_schedule_w(now_ts)` (7 Stundenwerte: jetzt, dann Mitte der nächsten 6 vollen Stunden), einmal je `compute()`.
- `_calculate_hold_flow_temperature_c(mpc_input, gain_w)`: `hold_power_w = max(0, loss(Soll, außen) − gain_w)` → `emitter.calculate_recommended_flow_temperature_c`. Öffentliche `calculate_hold_flow_temperature_c` (Kurven-Endpunkte) ohne Gewinn.
- Prognose: Haltevorlauf zusätzlich für +1 h aus Außenprognose (Open-Meteo-Abruf in `CC/irradiance.py` um Temperatur erweitern) und Gewinn-Fahrplan; `lead_feedforward_flow` = max(jetzt, +1 h).
- Boost (`_calculate_recovery_flow`): `_simulate_room_temperature_c` bekommt `gains_w`; Kapazität Masse. Lüften-Kapazität erst nach Nutzer-Freigabe (eigener Schritt, nicht hier).
- Sonnen-Prognose fehlt → Sonne 0, G/Profil bleiben (Test).
- Nach Umstellung springt `lead_feedforward_flow`; `feedback_k` einmalig um Differenz alt/neu korrigieren, damit Vorlauf-Ziel stetig bleibt.

Tests: Haltevorlauf sinkt bei G = 200 W; Boost mit Gewinnen niedriger; Sonnenquelle `None` → wie ohne Sonne; öffentliche Kurvenfunktion unverändert; Ziel stetig bei Umstellung.

Doku: `mpc-guide.md` „Gains and sun", „Flow control"; Offset-Empfehlung neu bewerten.

## 5. Beobachten (laufend, Tagesauswertung)

Sofort durch Nutzer:
- Raumsensor Rebekka (`sensor.sensor_rebekka_temperature`) meldet nur ~stündlich — Reporting-Intervall am Gerät prüfen (Korrektur braucht ≥ 2 Werte je Stunde).
- Arbeitszimmer unter Soll bei 100 % Ventil: vermutlich Voreinstellung — prüfen.
- `algorithm_scale_factor` aller TRVs auf 1 vor Neustart A.

Mit Termin:
- Boiler-Modus TRV: nach Z2M 2.15.0 (~01.12., Schreib-Fix `heating_system_mode` UINT8) alle 13 `select.heizung_*_heating_system_mode` auf `default_heating`. Nur nach Nutzer-Okay. Danach Takten und Raumfehler je TRV gegen Tage davor.

Laufend:
- Ventiltakten je TRV seit Neustart A; bei Takten Faktor-Empfehlung an Nutzer.
- Typ 1: Demand-Verlauf nach jedem Korrekturschritt nach unten (Sprung auf ~10 % erwartet). Erholung > 30 min → Nutzer melden (dann Schritte nach unten seltener/erst bei größerem Fehler).
- Korrektur je Raum: am Deckel ±2 K → Ursache melden (Sensorlage, TRV-Lage, Unterversorgung).
- Vorlauf: `feedback_k` am Anschlag ±5 K oder Pendeln (Hoch/Runter im Wechsel < 4 h) → melden.
- Lüften-Kapazität: Werte je Raum sammeln, nach einigen Wochen Nutzer fragen, ob sie Boost bekommt.
- Ladungs-Dips zählen (6a).
- UA-Einschwingen nach Neustart A; Werte vor/nach nicht mischen.
- Strecken ab Sonne 2 h unter: Starts vor 22:00 auf Drift prüfen.

## 6. Ladeplan Warmwasser (zurückgestellt bis Bedarf belegt)

Ziel: Ladungen vorhersagen, Vorlauf vorher und in der Erholung anheben, gefährdete Räume dosiert vorheizen.

Voraussetzung: 2 und 3 eingeschwungen.

### 6a. Bedarf zählen (kein Code, Tagesauswertung)

- Ladungen über Warmwasser-Steuerung (`sensor.warmwasser_*`, per `ha_search`) und Speichertemperatur finden.
- Je Ladung: Start, Dauer, Art (Nachtende, Ladefenster, Legionellen, Nachladen, PV, nach Dusche), je Raum größter Abstand unter Soll von Ladungsbeginn bis 60 min nach Ende.
- Mindestens 3 Wochen, darin 2 Legionellen-Zyklen. Ergebnis: wie oft Dip > 0,2 K, welche Räume, welche Art. Nutzer entscheidet: kein Bedarf (Abschnitt streichen, Stub `charge_flow_c` bleibt) / nur Vorlauf (6b + 6d mit festem Fenster 30 min vor bis 60 min nach, Anhebung `charge_raise_max`) / voll (6b–6d).

**Ist-Stand (vor 6b prüfen):** `CC/hub.py` `charge_flow_c` Stub; `CC/control/flow.py` `FlowInputs.charge_flow_c`, `FlowSource.CHARGE`, Pflicht-Schreiben fertig; `CC/flow_device.py` Zahl `charge_raise_max_k`; `CC/control/hot_water.py` `HotWaterConfig`, `HotWaterController.decide`, `ChargeDetector`; `CC/control/legionella.py` `LegionellaController._windows`; `CC/hot_water_device.py` Store, `tank_c`, `charging`.

### 6b. `CC/control/charge_plan.py` (reines Python)

`planned_charges(now, tank_c, config, legionella_windows, rates, horizon_s=6 h) -> list[PlannedCharge(start_ts, duration_s, kind, certain)]`, `ChargeKind` = `night_end`, `charge_window`, `legionella`, `reheat` (letzteres `certain=False`). Speicher-Prognose linear mit `cooling_k_per_h` (Start 0,5), Dauer `(Ziel − Prognose) / charge_k_per_min · 60 + 300` s (Start 0,25). `ChargeRateLearner`: Abkühlrate Median der letzten 48 Stunden ohne Ladung/Zapfung (Zapfung = > 1 K in 15 min), Laderate Mittel der letzten 10 Ladungen; Raten im Store des `hot_water`-Subentrys. Sensor `sensor.warmwasser_naechste_ladung`. PV-/Nach-Dusch-Ladungen nicht planbar.

Tests `tests/control/test_charge_plan.py`: Zapf-Stunde ignoriert; Nachtende 39 °C → Ladung, 45 °C → keine; Ladefenster; Legionellen immer; Reheat unsicher; `tank_c=None` → leer; Dauer-Formel.

### 6c. Vorheizen je Raum

Kurzzeitige Anhebung des geschriebenen Sollwerts um `lift` (0,5-K-Schritte, Deckel 1 K) vor sicheren Ladungen ≤ 90 min voraus. `lift` aus Simulation (Masse-Kapazität, Gewinne): Raum bei Ladungsende ≥ Soll − 0,2 K. Anhebung läuft wie Korrektur am Modus-Soll vorbei (Lerner sieht keinen Sprung); UA-Strecke endet bei Vorheizbeginn, nächste ab Ende + 1 h. Attribute `charge_preheat_lift`, `charge_preheat_from`, `charge_at_risk`.

### 6d. Hub, Deploy, Doku

`charge_flow_c` = min(max Ladevorlauf aller Räume, Vorlauf-Ziel + `charge_raise_max_k`), `None` außerhalb Fenster. Rückkopplung friert in Ladung ohnehin ein (3). Tests `test_hub.py`. Doku „Hot-water charges", „Flow control". Nach Neustart eine Zwangsladung beobachten.

## 7. Simulationen (`tools/`, offline, nicht in Integration)

Zwei Fragen, ein Zwilling: (a) Absenkung bei Abwesenheit — Strom-Ersparnis vs. Aufholen je Startzeit/Dauer/Tiefe/Tagestyp; (b) gezieltes Überheizen +X K und Auskühlen bis −Y K (Pendeln, PV-gekoppelt) vs. Halten. Ergebnis je HTML-Heatmap. Ändert nichts in HA.

**Vor Beginn Nutzer fragen:** Zwilling ähnelt abgelehntem „Offline-Prüfstand" — gilt Ablehnung auch hier?

Eckpunkte (Details bei Beginn gegen dann aktuellen Code neu festlegen):
- Ablage `tools/absenk_sim/`, Tests `tests/tools/`, kein `homeassistant`-Import, nutzt `MPC/models/` (keine Kopie der Physik).
- Raum = 1R1C mit gelernten Parametern (UA, Kapazität Masse, Gewinne), TRV als PI-Nachbildung auf Typ-1-Soll (Danfoss-Verhalten aus Verläufen Demand vs. Abstand fitten), gemeinsamer Vorlauf wie Integration (Vorsteuerung + Rückkopplung).
- Eingaben per MCP in `rooms.json`; Wetter Open-Meteo-Archiv 3 Winter, Offset zum Daikin-Fühler, Tagestypen nach Temperatur × Sonne; COP aus Datenblatt, Skalierung gegen Daikin-Strom (2-h-Summen, ohne WW/Abtauen/Ausfall).
- Validierung zuerst: 10 vergangene Tage nachspielen, RMSE je Raum, getrennt lange Abkühlstrecken. **Halt** nach Validierung und nach COP-Fit (Restfehler an Nutzer; Ersparnis kleiner als Restfehler = nicht unterscheidbar).
- Sensitivität Kapazität × 0,5/1/2. Takten/Mindestmodulation markieren, nicht modellieren.
- Ergebnis berührt „Keine Tag/Nacht-Absenkung" bzw. „WP-Heizbetrieb schaltet Nutzer" — Bewertung trifft Nutzer.

## Später / offen (eigenes Design, nicht ungefragt)

- Lüften-Kapazität mit Wirkung (Boost nach Lüften, Bäder) — nach Beobachtung, Nutzer entscheidet.
- Experimente im Betrieb: Überheizen +1–2 K mit Auskühlen, Absenkung bei Abwesenheit — nach 7 bzw. auf Zuruf.
- Ladefenster verschieben (PV, Raumlast).
- Sonnenschutz Daikin-Außenfühler fürs Frühjahr — Nutzer regelt selbst.
- Karte: Boost und Ladeplan anzeigen — nach Betrieb entscheiden.
- Heizkörperlüfter-Faktor, Flur-EG-Sonderfall, Nachbarraum-Kopplung.

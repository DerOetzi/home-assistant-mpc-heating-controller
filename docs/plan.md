# Offener Umsetzungsplan (Stand 2026-11-13)

Nur was noch zu tun ist, mit den Angaben für die Umsetzung. Erledigtes steht im Code, in `mpc-guide.md` und in der Git-Historie. Getroffene Entscheidungen stehen im Skill `heating-controller` (`references/entscheidungen.md`).

## Arbeitsweise (gilt für jeden Schritt)

Pfade unten sind relativ zum Repo. Abkürzung `CC/` = `custom_components/heating_controller/`, `MPC/` = `CC/controller/mpc/`.

1. Skill `heating-controller` laden, `references/entscheidungen.md` lesen.
2. Nur einen Unterabschnitt (z. B. 1a) je Durchgang. Vorher die dort genannten Dateien ganz lesen.
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

## Reihenfolge

Abschnitte sind in Arbeitsreihenfolge nummeriert. Kapazität zuerst: gelernter Wert liegt bis Faktor 5 neben Seed, und Gewinne-Replay, Absenk-Simulation, Ladeplan und Hysterese rechnen alle damit.

| Schritt | Was | Bedingung für Start |
|---|---|---|
| 1 | Nutzer-Prüfungen aus 6. Auswertung Gewinne 2a. Ladungs-Dips zählen 4a. | sofort, kein Code |
| 2 | Neustart für 1a–1c (Code fertig) | Nutzer gibt frei |
| 3 | 1d beobachten, ca. 1 Woche | nach Neustart aus Schritt 2 |
| 4 | 2b Gewinne in Regelung, Neustart B | Nutzer hat 2a freigegeben, Schritt 3 ohne Auffälligkeit |
| 5 | Boiler-Modus + Kennlinien-Reset (6, ~01.12.) | Z2M 2.15.0 da. Kein anderer Neustart in den 3 Tagen davor. |
| 6 | 2c Halte-/Mindestvorlauf mit Gewinnen, Neustart C | nach Schritt 5 |
| 7 | 3 Schritte 1–3 (offline) | ab Schritt 3 möglich, parallel |
| 8 | 4b–4e Ladeplan | nur wenn Nutzer nach 4a Bedarf bestätigt |
| 9 | 3 Schritte 4–6, danach 5 | nach Schritt 6 und Halt-Freigaben in 3e |

## 1. Kapazität lernbar machen

1a–1c (Sprungstrecken, Untergrenze 0,15, Sonnensperre über Einstrahlung) umgesetzt, Neustart steht aus.

### 1d. Auswirkungen prüfen (nach Neustart, ca. 1 Woche, kein Code)

In Tagesauswertung je Raum:
- `sensor.<raum>_kapazitatsfaktor` Verlauf; Zustände und `last_stretch_*` am Kapazitäts-Lernstatus.
- Recovery-Vorlauf und Aufholdauer gegen Woche davor. Überschwingen über Soll nach Aufheizen (> 0,3 K) melden.
- UA-Faktoren vor/nach nicht mischen.
- Abkühl-Schätzung (`estimated`) gegen Aufheiz-Schätzung je Raum vergleichen. Liegt Abkühlen dauerhaft > Faktor 2 über Aufheizen: an Nutzer melden (Hinweis auf zwei Zeitskalen).
- Offene Frage: passen Aufheiz-C und Nacht-Verhalten (Drift über Stunden) zu einem C? Prüfung über Zwilling-Validierung in 3. Falls nicht vereinbar: Zwei-Knoten-Modell → „Später/offen"; berührt abgelehntes 2R2C, Nutzer entscheidet.

## 2. Gewinne und Sonne

Stand: Nacht-Regression `[UA, G]`, Stunden-Profil, Sonne je Fläche (Open-Meteo GTI) lernen und sind sichtbar (`sensor.<raum>_zusatzwarme`, UA-Lernstatus-Attribute, Karte). Sie wirken **noch nicht** in Regelung.

Modell (je Raum, W): `C · dT/dt = P_hk − UA · (T − T_a) + G + g(Block, Tagesart) + Σ s_f · Ī_f + Σ w_o · Ĩ_o`. Ein Term wirkt nur, wenn bestimmt (Schwellen siehe `entscheidungen.md`), sonst 0.

### 2a. Auswertung (keine Code-Änderung)

Ab 14.11. als eigener Abschnitt der Tagesauswertung, alle 10 Räume, Daten per MCP (`ha_get_state` auf `sensor.<raum>_ua_lernstatus` und `sensor.<raum>_zusatzwarme`, Attribut-Verläufe mit `significant_changes_only=false`):

| Ebene | Lesen | Plausibel wenn |
|---|---|---|
| Nacht | `learned_gain_w`, `gain_sigma_w`, `gain_fit_points`, `gain_determined`, `regression_ua_factor` | Schlafzimmer G ≈ 100–150 W; `regression_ua_factor` nahe `ua_factor` |
| Stunde | Profil Werktag/sonst, Sonnenkoeffizienten, `last_hour_outcome` | Süd-/Westräume `s > 0`, Nord ≈ 0, Schlafzimmer tagsüber negativ, Esszimmer Abendblock positiv |
| Güte | `residual_rms_w`; Anteil gesperrter Stunden aus `last_hour_outcome`-Verlauf | RMS deutlich unter 10 % Norm-Heizlast |

Ergebnis als Tabelle je Raum (bestimmt ja/nein je Term, Wert, auffällig ja/nein) + Empfehlung „2b freigeben / noch warten" an Nutzer. Nutzer gibt 2b frei.

### 2b. Gewinne in Regelung (Neustart B)

Voraussetzung: 1 ist deployt. Nutzer hat 2a freigegeben.

**Ist-Stand im Code:**
- `MPC/models/gains.py`: `RoomGainModel.breakdown(profile, sun_values) -> GainBreakdown`, Feld `total_w` enthält nur bestimmte Terme. `sun_regressors(sun, mid_ts)` liefert Sonnenwerte oder `None`. `steady_gain_w` = G wenn bestimmt, sonst 0.
- `MPC/controller.py`: `set_gain_context(profile_index_at, local_day_at, sun_source)` reicht nur an Lerner weiter.
- `CC/coordinator.py`: `current_gain()` macht dieselbe Rechnung für den Sensor.

**A. `MPC/controller.py` — Gewinn-Zugriff**
- In `__init__`: `self._profile_index_at`, `self._sun_source` mit Vorgaben wie im Lerner (`_default_profile_index` aus `learner.py` importieren, `lambda: None`). `set_gain_context` speichert beide zusätzlich lokal.
- Neue Methode:

```python
def gain_w(self, ts: float) -> float:
    """Determined gains at ts; undetermined terms count as zero."""
    gains = self._gain_model
    return gains.breakdown(
        self._profile_index_at(ts),
        gains.sun_regressors(self._sun_source(), ts),
    ).total_w
```

- Neue Methode `_gain_schedule_w(self, now_ts: float) -> list[float]`: 7 Werte. Index 0 = `gain_w(now_ts)`. Index k = 1…6 = `gain_w(Mitte der k-ten vollen Stunde nach der laufenden)`, also `(now_ts // 3600 + k) * 3600 + 1800`. Wird **einmal je `compute()`** gebaut (nach `create_input`), nicht je Simulationsschritt (Rechenzeit).
- `CC/coordinator.py`: `current_gain()` kann bleiben.

**B. `MPC/controller.py` — Simulation und Haltebedarf**
- `_simulate_room_temperature_c(...)`: neuer Parameter `gains_w: Sequence[float] | None = None`. Je Schritt `n` (0-basiert): `elapsed_s = n * MPC_SIMULATION_STEP_S`; Gewinn = `0.0` wenn `gains_w is None`, sonst `gains_w[min(int(elapsed_s // 3600), len(gains_w) − 1)]`. Vereinfachung gewollt: Index 0 gilt für die erste Stunde ab jetzt. `net_heating_power_w = heating_power_w − heat_loss_w + gain`.
- `_predict_room_temperature_c`: bekommt `gains_w` und reicht durch. `_find_optimal_demand_prediction`: bekommt `gains_w`, reicht an Vorhersage und an `_hold_demand_pct`.
- `_hold_demand_pct(mpc_input, gain_w: float = 0.0)`: `hold_power_w = max(0.0, loss − gain_w)`; Aufruf mit `gains_w[0]`.
- `_calculate_recovery_flow` und `_calculate_hold_flow_temperature_c`: **unverändert** (kommen in 2c). Recovery ruft Simulation weiter ohne `gains_w`.
- `calculate_hold_flow_temperature_c(target, outdoor)` (öffentlich, für Kurven-Endpunkte): nie mit Gewinnen.

**C. `MPC/observer.py`**
- `_CommittedState`: Feld `gain_w: float = 0.0`. `commit(..., gain_w: float = 0.0)` speichert es. `_propagate`: `net_heating_power_w` um `+ state.gain_w` ergänzen.
- `controller.py` `compute()`: beim `self._observer.commit(...)` `gain_w=gains_w[0]` mitgeben.

**D. `MPC/learner.py`**
- UA-Ziel in `_balance_stretch`: nach Berechnung von `stored_heat_j`:
  `gain_j = self._gain_model.steady_gain_w * window_duration_s if self._gain_model is not None else 0.0`
  `target_ua_w_per_k = (heat_j − stored_heat_j + gain_j) / temperature_difference_ks`.
  Prüfung `STORAGE_DOMINATED` und `add_night_point(...)` weiter mit `heat_j − stored_heat_j` **ohne** Gewinn (Regression trennt UA und G selbst). `UA_BALANCE_WEIGHT`, Grenzen 0,15/2 bleiben.
- Sprungstrecken (aus 1a): neue Methode `_gain_w_at(self, ts) -> float` (wie `gain_w` oben, mit Lerner-eigenen `_profile_index_at`, `_sun_source`; 0 ohne `_gain_model`). In Wärmebilanz D6: `+ self._gain_w_at((prev.timestamp + cur.timestamp) / 2) · dt`.
- Abkühlstrecken ändern ab jetzt den Faktor wie Aufheizstrecken (Ausgang `learned`). Zustand `estimated` aus `CapacityOutcome`, Übersetzungen, Doku entfernen.
- `_jump_sun_free` Endfassung: vor Schritt 3 neue Regel — sind alle Sonnen-Terme des Raums bestimmt (`all(self._gain_model.sun_determined(i) for i in range(len(self._gain_model.sun_terms)))`) und `sun` nicht `None` → `True` (Sonne steckt dann im Replay). Sonst wie bisher.

**E. Sensor**
- `sensor.<raum>_zusatzwarme`: Attribut `ungated_w` behalten. Nichts Neues.

**F. Tests**
- `tests/controller/mpc/test_controller.py`:
  1. Gewinn-Modell so füttern, dass G = 200 W bestimmt ist (Vorbild in `tests/controller/mpc/models/test_gains.py`), sonst gleiche Eingaben wie ein bestehender Test mit Bedarf > 0 → `demand_pct` kleiner als ohne Gewinn.
  2. Gewinn > Verlust bei Soll → `_hold_demand_pct` gibt 0.
  3. Unbestimmte Terme: **alle bestehenden Tests bleiben ohne Anpassung grün**. Wird einer rot, Fehler im Umbau suchen, nicht Test ändern.
  4. `recovery_flow_temperature_c` und `hold_flow_temperature_c` gleich mit und ohne Gewinn (kommt erst 2c).
- `tests/controller/mpc/test_observer.py`: konstanter Gewinn 100 W über 2 h, Raum folgt Modell mit Gewinn → Fehler Schätzung < 0,05 K.
- `tests/controller/mpc/test_learner.py`: UA-Ziel mit G = 50 W bestimmt, Bilanz 300 W bei ΔT 15 K, Drift 0 → `last_target_factor == (350/15) / base_ua`. Abkühlstrecke mit Gewinn 100 W und wahrem C → Schätzung ±20 %, Ausgang `learned`. Sonnen-Terme bestimmt + 200 W/m² → Strecke zugelassen.

**G. Doku**
- `mpc-guide.md` „Gains and sun": Satz „not yet used by the controller" ersetzen durch was jetzt wirkt (Planer, Haltebedarf, Beobachter, UA-Ziel, Kapazität) und was nicht (Vorlauf — folgt). „When the model learns": UA-Ziel-Formel, Abkühlstrecken.
- README, falls dort „Gewinne wirken noch nicht" steht.
- Skill `diagnose.md`/`tagesauswertung.md`: `estimated` raus; Hinweis UA-Faktoren vor/nach 2b nicht mischen.
- `entscheidungen.md`: Eintrag „Keine Fremdwärme-Schätzung … wirken erst wenn bestimmt" passt weiter, nicht ändern.

Nach Neustart B in Tagesauswertung: UA-Faktoren schwingen neu ein (höher erwartet, da G jetzt getrennt), Ventilöffnungen tagsüber, Unter-/Überschwingen in Räumen mit bestimmten Gewinnen.

### 2c. Halte- und Mindestvorlauf mit Gewinnen (Neustart C, eigener Neustart)

Erst nach Boiler-Modus-Umstellung und Kennlinien-Reset (6).

`MPC/controller.py`:
- `_calculate_hold_flow_temperature_c(mpc_input, gain_w: float = 0.0)`: nicht mehr an öffentliche Methode delegieren, sondern `hold_power_w = max(0.0, loss(Soll, außen) − gain_w)` und `self._emitter_model.calculate_recommended_flow_temperature_c(hold_power_w, target)`. Aufruf in `compute()` mit `gains_w[0]`. Öffentliche `calculate_hold_flow_temperature_c` bleibt ohne Gewinn.
- `_calculate_recovery_flow(mpc_input, estimate, gains_w)`: `reaches_target` ruft Simulation mit `gains_w` (7 Stundenwerte decken `FLOW_RECOVERY_HORIZON_S` = 6 h). Früher Ausstieg `hold_power_w <= 0` bleibt wie er ist (ohne Gewinn gerechnet).
- Sonnen-Prognose fehlt → `sun_regressors` gibt `None` → `breakdown` zählt Sonne 0, G und Profil bleiben. Kein zusätzlicher Code nötig; per Test absichern.
- Hub-Leitwerte (`CC/hub.py` `lead_hold_flow`, `lead_min_flow`) und Reserve folgen automatisch aus `RoomMpcResult`. Surplus-Gate unverändert.

Tests (`test_controller.py`): `hold_flow_temperature_c` sinkt bei G = 200 W bestimmt; Recovery mit Sonne ab Stunde 2 (Attrappe `SunSource`, Sonnen-Term bestimmt) → niedrigerer `recovery_flow_temperature_c` als ohne; Sonnenquelle `None` → gleich wie ohne Sonne; `calculate_hold_flow_temperature_c` unverändert.

Doku: `mpc-guide.md` „How the minimum flow temperature is computed" und „Gains and sun". Kommentar bei `hold_flow_at_curve_end_c` im Koordinator („no gains") bleibt richtig.

Danach Offset-Empfehlung neu bewerten; Reserve vor/nach nicht vergleichen.

## 3. Absenk-Simulation (`tools/`, offline, nicht in Integration)

Ziel: Strom-Ersparnis vs. Mehrbedarf beim Aufholen, wenn alle Räume (außer Flur EG, Dani — bleiben Basis) für X h um Y K abgesenkt werden, je Startzeit und Tagestyp. Ergebnis: HTML-Heatmap-Seite. Wiederholbar nach jeder Saison mit neu gelernten Parametern. Ändert nichts in HA, Absenklogik in Integration bleibt eigenes Thema.

Voraussetzung: 1 eingeschwungen (Kapazitätsfaktoren gelernt). **Vor Beginn Nutzer fragen:** Zwilling ähnelt abgelehntem „Offline-Prüfstand" — gilt Ablehnung nur für Reglertests oder auch hier? Ohne Ja nicht anfangen.

Vor Beginn diesen Abschnitt gegen aktuellen Code prüfen (Signaturen von `RoomMpcController`, `RoomThermalConfig`, `TrvConfig` in `MPC/types.py` ändern sich durch 1 und 2).

Ablage: `tools/absenk_sim/` mit `rooms.json`, `weather.py`, `twin.py`, `cop.py`, `scenarios.py`, `report.py`, `results.json`, `report.html`; Tests in `tests/tools/`. Läuft mit `~/.venvs/heating-controller`. Kein Import von `homeassistant` in `tools/` (nur `MPC/`-Module; bricht ein Import wegen HA-Abhängigkeit, Stopp).

### 3a. Eingaben

- **Raumparameter** → `tools/absenk_sim/rooms.json`. Per MCP je Raum lesen (kein Recorder-Zugriff aus Skript): UA-Faktor, Kapazitätsfaktor, `capacity_j_per_k`, Fläche, Bauweise, Norm-Heizlast, Heizkörper je TRV (Typ, Maße bzw. Nennleistung), Ventilkennlinie (`sensor.<raum>_ventilkennlinie`-Attribute), Gewinne soweit bestimmt, Komfort-Soll, Leitraum-Flaggen. Konfig-Werte aus Config-Entry über `ha_get_integration`. Datei von Hand prüfbar halten (ein Objekt je Raum-Slug).
- **Wetter**: Open-Meteo-Archiv (`archive-api.open-meteo.com`) für Wohnort, vergangene 3 Winter (Okt–Apr) stündlich: Temperatur, GTI je Fassade mit denselben Neigungen/Azimuten wie `CC/irradiance.py` (`building_surfaces`, `request_params` als Vorlage). Antwort in `tools/absenk_sim/cache/` ablegen, nicht bei jedem Lauf neu holen. Offset zum Daikin-Fühler aus Überlappung Okt/Nov fitten (ein konstanter Wert, Median der Differenz nachts). Tage klassieren: Tagesmittel < −5, −5…0, 0…5, 5…10, > 10 °C × klar/trüb (klar = Tagessumme Süd-GTI über Median der Klasse). Je Klasse Mediantag (stundenweise Median) + Streuung.
- **COP**: Form COP(T_außen, T_vorlauf) aus Datenblatt (Modell per `ha_get_device`), als Tabelle + bilineare Interpolation. Kalibrierung gegen Daikin-Strom (nur 2-h-Summen, keine Wärme): Zwilling auf vergangene Tage mit gemessenem Vorlauf + Ventilöffnungen → Q je 2-h-Fenster, ein Skalierfaktor (optional zweiter für Abtau-Bereich −5…+5 °C) so, dass Q/COP Strom trifft. Fenster mit WW-Ladung, Abtauen, Neustart/Ausfall raus (prüfen ob Onecta Heizen/WW trennt). Restfehler (relativer RMS je 2-h-Fenster und über Tagessummen) = Validierungsmaß.

### 3b. Zwilling

- `twin.py` baut je Raum `HeatEmitterModel`, `RoomLossModel`, `ThermalCapacityModel` aus `MPC/models/` und nutzt Planer aus `MPC/controller.py` (keine Kopie der Physik). Zeit kommt aus Simulation: `time.time` in `controller.py` muss dafür ersetzbar sein — per `unittest.mock.patch` im Tool, Integration nicht umbauen.
- Alle 10 Räume gleichzeitig, Schritt 150 s (`MPC_SIMULATION_STEP_S`). Gemeinsamer Vorlauf = max. `recommended_flow_temperature_c` der Vorlauf-Leiträume + Marge, begrenzt durch Kurve + Offset-Bereich und Auslegungsvorlauf; darunter Schwelle. WP-Maximalleistung je T_außen als Deckel (alle Räume anteilig kürzen).
- „Wahrer" Raum = dasselbe 1R1C mit gelernten Parametern + Gewinnen (ein-/ausschaltbar).
- Validierung vor Auswertung: 10 vergangene Tage nachspielen (gemessene Außentemperatur, gemessener Vorlauf), RMSE Raumtemperatur je Raum ausweisen. Getrennt für lange Abkühlstrecken (Dani und Flur EG in Absenkung, Frostschutz-Phasen): trifft gelerntes C Auskühlung über Stunden? Antwort auf Frage aus 1d.
- Grenzen markieren statt modellieren: Zellen mit Vorlauf unter Schwelle/Mindestmodulation (Takten) kennzeichnen.

### 3c. Szenarien

- Raster: Startstunde 0–23 × Dauer 1–24 h (+ 48 h) × Y = 1/2/3/4 K × Tagestyp.
- Benannt (Marker + Tabelle): Arbeitstag 7–17, Vormittag 8–13, Nachmittag 13–18, Abend 17–23, Nacht 22–6, Nacht lang 21–7, Ausflug 6–22, 24 h, Wochenende 48 h.
- Fenster 3 Tage: Einschwingtag, Absenkung, Nachlauftag mit vollem Aufholen. Ersparnis = Differenz zum Basisfall (immer Komfort) über ganzes Fenster.
- Aufholstrategien: (a) vorheizen mit bekannter Rückkehrzeit, Soll bei Rückkehr erreicht (Obergrenze, MPC kennt Rückkehr heute nicht); (b) Aufheizen erst bei Rückkehr.
- Sensitivität Kapazität × 0,5 / 1 / 2 um gelernten Wert (1R1C bildet träge Wandmasse nicht ab).

### 3d. Ausgaben

- Je Zelle: Strom kWh Basis/Szenario, Ersparnis %, Aufholdauer, Komfortdefizit Kh nach Rückkehr, Spitzenvorlauf, Takt-Markierung → `results.json`.
- HTML-Seite (eine Datei, Daten eingebettet, kein Server): Heatmap X = Startstunde, Y = Dauer, divergierende Farbe (Ersparnis/Mehrverbrauch). Auswahl Tagestyp, Y, Strategie, Kapazitäts-Sensitivität. Zweite Heatmap Aufholdauer/Komfortdefizit. Klick auf Zelle: Raumtemperaturen, Vorlauf, COP, Strom Basis vs. Szenario, Anteil je Raum. Restfehler COP-Fit sichtbar; Zellen mit Ersparnis kleiner als Restfehler als „nicht unterscheidbar" kennzeichnen.
- Vorab-Kennzahl: τ = C/UA je Raum (Stunden).
- Später optional: €-Skala mit PV-Deckung aus SolarEdge typischer Tage.

### 3e. Reihenfolge

1. `rooms.json` + Tabelle τ je Raum an Nutzer.
2. Wetter-Archiv + Klassierung; Tabelle Tage je Klasse an Nutzer.
3. Zwilling + Validierung gegen vergangene Tage.

**Halt nach Schritt 3:** Validierung an Nutzer. Trifft Zwilling lange Abkühlstrecken nicht (RMSE dort deutlich über Rest), erst Frage aus 1d klären — Raster mit falscher Auskühlung überschätzt Ersparnis.

4. COP-Fit gegen 2-h-Strom.

**Halt nach Schritt 4:** Restfehler an Nutzer. Absenk-Ersparnis bei Wärmepumpe meist wenige Prozent; liegt Restfehler darüber, entscheidet Nutzer ob Raster lohnt.

5. Szenario-Raster. 6. HTML.

Schritte 1–3 ab eingeschwungenem 1 möglich. Ab Schritt 4 sinnvoll nach 2c, sonst Gewinne getrennt ein-/ausschaltbar.

Ergebnis an Nutzer; berührt Entscheidung „Keine Tag/Nacht-Absenkung“ — Bewertung trifft Nutzer.

## 4. Ladeplan Warmwasser im MPC (zurückgestellt bis Bedarf belegt)

Anlass 05.11. 13:05–13:45: Zwangsladung, Heizkreis 40 min ohne Umlauf, Duschbad 20,8 → 20,5 °C, Rebekka lau; erst Offset +3 hielt. Ziel: Ladungen vorhersagen, gefährdete Räume heizen dosiert vor, Vorlauf steigt vorher und in der Erholung. Kein Vorheizen „auf Verdacht" in allen Räumen.

Voraussetzung: 1 eingeschwungen. Vorheiz-Simulation mit Seed-Kapazität sagt für massearme Räume keinen Dip voraus, Vorheizen griffe nie.

### 4a. Bedarf zählen (kein Code, Tagesauswertung)

- Ladungen finden: `binary_sensor`/Attribut der Warmwasser-Steuerung für laufende Ladung bzw. Phase (`sensor.warmwasser_*`, per `ha_search` suchen) und Speichertemperatur, Verlauf per `ha_get_history`.
- Je Ladung notieren: Start, Dauer, Art (Nachtende, Ladefenster, Legionellen, Nachladen, PV, nach Dusche — aus Phase und Uhrzeit), je Raum größter Abstand Raumtemperatur unter Soll von Ladungsbeginn bis 60 min nach Ende.
- Laufende Tabelle in Tagesauswertung, mindestens 3 Wochen, darin mindestens 2 Legionellen-Zyklen.
- Ergebnis an Nutzer: wie oft Dip > 0,2 K, welche Räume, welche Ladungsart. Nutzer entscheidet Umfang:
  - kein Bedarf → Abschnitt 4 streichen, Stub `charge_flow_c` im Hub kann bleiben;
  - nur Vorlauf: 4b + 4d-Vorlaufanhebung mit festem Fenster (30 min vor Start bis 60 min nach Ende, Anhebung = `charge_raise_max`), ohne 4c;
  - voll: 4b–4e.

### Ist-Stand im Code (vor 4b prüfen)

- `CC/hub.py`: Property `charge_flow_c` existiert als Stub (`return None`).
- `CC/control/flow.py`: `FlowInputs.charge_flow_c`, `FlowSource.CHARGE`, Pflicht-Schreiben beim Ein-/Austritt sind fertig. `CC/flow_device.py` übergibt `hub.charge_flow_c`, Zahl `charge_raise_max` (K) existiert (`charge_raise_max_k`).
- `CC/control/hot_water.py`: `HotWaterConfig` (Normal 46, Nacht 38 von `night_start` bis `night_end`, Ladefenster `window_start`–`window_end` auf `window_c`, `reheat_hysteresis_k`), `HotWaterController.decide`, `ChargeDetector` (`charging`, `started_ts`).
- `CC/control/legionella.py`: `LegionellaController` mit `_windows(now)`.
- `CC/hot_water_device.py`: `HotWaterDevice` mit Store (`_export`), `tank_c`, `charging`.
- Räume bekommen laufende Ladung über `hot_water_charge_provider` → `RoomMpcInput.hot_water_charge`.

### 4b. `CC/control/charge_plan.py` (reines Python, kein HA-Import)

```python
class ChargeKind(StrEnum): NIGHT_END, CHARGE_WINDOW, LEGIONELLA, REHEAT

@dataclass(frozen=True)
class PlannedCharge:
    start_ts: float
    duration_s: float
    kind: ChargeKind
    certain: bool

def planned_charges(now: datetime, tank_c: float | None, config: HotWaterConfig,
                    legionella_windows: list[tuple[datetime, datetime]],
                    rates: ChargeRates, horizon_s: float = 6 * 3600) -> list[PlannedCharge]
```

| Art | Auslöser | Vorhersage |
|---|---|---|
| `night_end` | `config.night_end` (Soll `night_c` → `normal_c`) | Ladung, wenn Speicher-Prognose zum Zeitpunkt < `normal_c − reheat_hysteresis_k` |
| `charge_window` | `config.window_start` | Ladung, wenn Prognose < `window_c` (Zwangsladung) bzw. < `window_c − reheat_hysteresis_k` ohne `forced_charge` |
| `legionella` | Beginn nächstes Legionellen-Fenster | immer, Ziel `legionella_c` |
| `reheat` | Abkühlung ohne Zapfung | Zeitpunkt, an dem Prognose < aktuelles Soll − Hysterese, wenn innerhalb Horizont; `certain = False` |

- Speicher-Prognose(t) = `tank_c − cooling_k_per_h · (t − now)`. `tank_c is None` → leere Liste.
- Dauer = `(Ziel − Prognose) / charge_k_per_min · 60 + 300` s (Daikin startet ~5 min nach Sollwechsel).
- `ChargeRates` (Dataclass, mit `to_dict`/`from_dict`): `cooling_k_per_h` Start 0,5; `charge_k_per_min` Start 0,25. Lernen in eigener Klasse `ChargeRateLearner.update(tank_c, charging, now_ts)`:
  - Abkühlrate: je volle Stunde ohne Ladung und ohne Zapfung (Zapfung = Abfall > 1 K in 15 min) eine Probe; Rate = Median der letzten 48 Proben.
  - Laderate: je erkannter Ladung (Speicheranstieg / Dauer); Rate = Mittel der letzten 10.
- Raten im Store des `hot_water`-Subentrys (`HotWaterDevice._export` / Laden in `async_setup`).
- Sensor `sensor.warmwasser_naechste_ladung` (Zeitstempel, Attribute `kind`, `duration_min`, `certain`, `cooling_k_per_h`, `charge_k_per_min`), angelegt in `CC/sensor.py` beim Warmwasser-Gerät; Übersetzungen.
- PV-Überschuss- und Nach-Dusch-Ladungen nicht planbar: wirken wie heute.

Tests `tests/control/test_charge_plan.py`: synthetischer Speicherverlauf mit Zapfung → Abkühlrate ignoriert Zapf-Stunde; Nachtende mit Speicher 39 °C → Ladung geplant, mit 45 °C → keine; Ladefenster; Legionellen immer; Reheat `certain=False`; `tank_c=None` → leer; Dauer-Formel.

### 4c. Controller: Vorheizen je Raum

- `MPC/results.py`: `@dataclass ChargeWindow(start_ts, end_ts)`; `RoomMpcInput.charge_window: ChargeWindow | None = None`.
- Hub gibt jedem Raum die nächste Ladung mit `certain = True`, wenn sie innerhalb `PREHEAT_WINDOW_MAX_S = 90 * 60` beginnt oder läuft. `reheat` erst ab 30 min vorher. Weg wie `hot_water_charge_provider`: neuer `charge_window_provider` am Koordinator, in `_async_recompute_locked` an `self.mpc.set_charge_window(...)`.

`MPC/controller.py`, neu `_calculate_charge_preheat(mpc_input, estimate, window, gains_w) -> _ChargePreheat(lift_c, from_ts, at_risk)` mit Konstanten `CHARGE_ALLOWED_DIP_C = 0.2`, `CHARGE_PREHEAT_MAX_LIFT_C = 0.5`, `CHARGE_PREHEAT_STEP_C = 0.05`, `CHARGE_RECOVERY_S = 3600`:
1. Simulation: von jetzt bis `start` mit Haltebedarf, `start`–`end` ohne Heizleistung (Heizkörper kühlen mit eigener Zeitkonstante aus) → Raumtemperatur bei `end`. Dafür Simulation in Abschnitte zerlegen (bestehende `_simulate_room_temperature_c` um Start-Zustand als Parameter erweitern statt kopieren).
2. ≥ Soll − `CHARGE_ALLOWED_DIP_C`: `lift = 0`.
3. Sonst kleinster `lift` (Schritte 0,05 bis Deckel), mit dem Start bei `Soll + lift` zum Zeitpunkt `start` der Raum bei `end` ≥ Soll − Dip liegt.
4. `lift` gilt ab `start − Vorlaufzeit` bis `start`. Vorlaufzeit = simulierte Zeit bei 100 % Öffnung für `lift` + 15 min, höchstens 90 min.
5. Deckel reicht nicht: `lift = Deckel`, `at_risk = True`.

Ziel für Planer in `compute()` = `target + lift`, solange `from_ts ≤ now < start`. 30-min-Horizont bleibt. Zielband unverändert.

Attribute am Bedarfssensor: `charge_preheat_lift`, `charge_preheat_from`, `charge_at_risk`.

`RoomMpcResult.charge_min_flow_temperature_c`: kleinster Vorlauf, mit dem der Raum (mit Vorheizen, Ventil bis 100 %) bei `end` ≥ Soll − Dip liegt und `CHARGE_RECOVERY_S` nach `end` wieder ≥ Soll − `FLOW_RECOVERY_TOLERANCE_C`. Bisektion wie `_calculate_recovery_flow`, gleiche Rundung. Außerhalb Fenster `None`.

Tests (`test_controller.py`): massearmer Raum (Fläche/Bauweise/Faktor so, dass C ≈ 0,3 MJ/K) gefährdet → `lift > 0`; schwerer Raum → `lift == 0`; Deckel greift → `at_risk`; ohne `charge_window` alles wie vorher (bestehende Tests unverändert grün).

### 4d. Hub und Lerner

- `CC/hub.py` `charge_flow_c`: `min(max(charge_min_flow aller Räume, nicht nur Leiträume), lead_min_flow + Marge + flow.charge_raise_max_k)`; `None` außerhalb Fenster (Vorheizbeginn frühester Raum bis `end + CHARGE_RECOVERY_S`) oder wenn kein Raum einen Wert liefert. Rest (Quelle `charge`, Pflicht-Schreiben) macht `control/flow.py` schon.
- Lerner: UA-Strecke endet bei Vorheizbeginn (`_close_ua_balance(from_ts)`), nächste beginnt nach `end + CHARGE_RECOVERY_S` (gewollter Anstieg verletzt sonst Drift-Test). Sprungstrecken: Soll-Anhebung < 0,5 K löst keine aus; `JUMP_TARGET_CHANGE_C` würde laufende beenden — gewollt.
- Während Ladung bleibt heutige Sperre. Kennlinien-Probe läuft weiter.

Tests: `tests/test_hub.py` (Maximum über alle Räume, Deckel, `None` außerhalb), `test_learner.py` (UA-Strecke endet bei Vorheizbeginn).

### 4e. Deploy und Doku

`mpc-guide.md` „Hot-water charges", „Flow control", „Hot water"; README; Übersetzungen; Skill-Dateien. Neustart nach Okay, eine Zwangsladung beobachten (Duschbad/Rebekka gegen 05.11.), Nachtende ~05:00 in Tagesauswertung.

## 5. Hysterese-Simulation (`tools/`, baut auf 3)

Ziel: Pendelbetrieb statt Halten — Räume auf Soll + X K heizen, dann bis Soll − Y K auskühlen lassen, wiederholt. Frage: Strom, WP-Takten und Komfort gegenüber heutigem Halten im Zielband. Gleicher Zwilling, gleiche Eingaben (Räume, Wetter-Tagestypen, COP-Fit) wie 3; Szenario-Modul `tools/absenk_sim/hysteresis.py` daneben, kein eigener Zwilling.

Niedrigste Priorität: Startverluste und Takten nicht messbar, Ergebnis bleibt Modellschätzung. Nur beginnen, wenn 3 brauchbaren COP-Fit geliefert hat und Nutzer es will.

### 5a. Betriebsarten

- (a) **Nur Räume pendeln, WP läuft durch**: TRVs je Raum mit Hysterese, gemeinsamer Vorlauf wie heute aus Leiträumen. Räume laufen phasenversetzt. Kein Gewinn auf WP-Seite erwartbar, kostet TRV-Schreiblast — optional, zuletzt.
- (b) **Synchron, WP im Intervall**: alle Räume heizen gemeinsam bis +X, dann WP aus (Heizanforderung weg) bis erster Leitraum −Y erreicht. Heizphase mit Vorlauf für Anstieg in Zeit T_heiz (Parameter 1/2/4 h), Abkühlphase ohne Heizleistung.
- (c) **PV-gekoppelt**: wie (b), Heizphase bevorzugt bei Überschuss (SolarEdge-Tagesprofile per MCP, als Datei im Tool), +X als thermischer Speicher.
- Vergleichsbasis: heutiges Halten (Zielband 0,1 K).

### 5b. WP-Modell (Zusatz zu 3)

- Mindestmodulation (Datenblatt) und Takt-Mindestzeiten als Grenze: unter Mindestleistung zählt Halten als Takten (Starts/h aus Leistungsbedarf geschätzt). Ohne Wärmezähler nur Modellschätzung, so ausweisen.
- COP je Phase aus T_außen und Vorlauf der Phase; Startverluste je Start als Parameter (Datenblatt/Literatur, Sensitivität 0 / typisch / hoch).
- Abtauen: COP-Abschlag im Bereich ca. −5…+5 °C wie in 3.

### 5c. Raster

- X = 0…1,5 K, Y = 0…1,5 K (Schritt 0,25) × Betriebsart × Tagestyp, Rechenfenster 2 Tage nach Einschwingtag.
- Mitteltemperatur verschiebt sich bei X ≠ Y: Ergebnis zweifach ausweisen — wie gerechnet und bezogen auf gleiche mittlere Raumtemperatur (UA-Korrektur), sonst vergleicht man Komfortniveaus statt Betriebsweisen.
- Sensitivität Kapazität × 0,5 / 1 / 2 (Zykluslänge hängt direkt an C).

### 5d. Ausgaben

- Je Zelle: Strom kWh vs. Basis (%), WP-Starts/Tag, mittlere Lauf- und Pausendauer, Zyklusdauer je Raum, Komfort (Kh unter Soll − 0,5, max. Abweichung), Spitzenvorlauf, TRV-Schreibzahl (Akku, Zigbee).
- HTML-Seite wie 3: Heatmap X × Y, Auswahl Betriebsart/Tagestyp/Bezug; Klick auf Zelle: Verlauf Räume, Vorlauf, WP an/aus, Strom.
- Gemeinsame Seite mit 3 möglich (Reiter Absenkung / Hysterese).

### 5e. Reihenfolge

Nach 3 Schritt 4 (Zwilling + COP-Fit validiert). 1. Basis und (b) mit WP-Intervall, 2. (c) PV, 3. HTML, 4. optional (a).

Ergebnis an Nutzer; berührt Entscheidungen „Zielband 0,1 K“ und „WP-Heizbetrieb schaltet Nutzer“ — Bewertung trifft Nutzer.

## 6. Beobachten (laufend, Tagesauswertung)

Sofort durch Nutzer:
- Raumsensor Rebekka (`sensor.sensor_rebekka_temperature`) meldet nur ~stündlich (22 Werte/Tag), langsamster Sensor, Rebekka bestimmt oft Vorlauf — Reporting-Intervall am Gerät prüfen.
- Arbeitszimmer unter Soll bei 100 % Ventil: vermutlich Voreinstellung — prüfen.
- e1/e2: Stufen bzw. Mindestabstand beim Nachführen der TRV-Sollwerte — klären, ob noch gewünscht.

Mit Termin:
- Boiler-Modus TRV: nach Z2M 2.15.0 (~01.12., Schreib-Fix `heating_system_mode` UINT8) alle 13 `select.heizung_*_heating_system_mode` auf `default_heating`, direkt danach `heating_controller.recalibrate` mit `reset_valve_curves` alle Räume. Beides nur nach Nutzer-Okay. Dann prüfen: Streuung weg? Ventil fällt bei Typ-1-Absenken weiter auf ~10 %? Bis dahin Kennlinien (Reset 10.11.) im Auto-Modus gelernt, nicht mit danach mischen.

Laufend:
- Ladungs-Dips zählen (4a).
- Ventilkennlinien seit Rampen-Probe 05.11.: Tabelle je TRV (Status, Klassen, Abstand zu Seed). `scattered` bei mehreren TRVs → innerer Regelkreis auf gemessene Öffnung erwägen. Weicht gelernte Kurve bei den meisten TRVs deutlich vom Seed ab → Seed im Profil nachziehen vorschlagen.
- Planer in Öffnung: Zu/Auf-Wechsel je TRV pro Nacht und `trv_setpoint_writes` gegen Nächte vor 03.11.
- Wirkung (h), (i), (k) und Heizkörper-Check-Regeln seit 31.10.
- UA-Einschwingen nach Daikin-Außenwert (01.11.) und Pumpenwechsel (04.11.).
- Strecken ab Sonne 2 h unter: Starts vor 22:00 (Abendnutzung) auf Drift prüfen.
- Reserve/Offset nach Spreizungs-Fix Tag/Nacht getrennt bewerten; vor 01.11. nicht vergleichbar.
- Nach 1: siehe 1d.

## Später / offen (eigenes Design, nicht ungefragt)

- Zwei-Knoten-Modell (Luft/Oberfläche schnell, Wandmasse langsam), falls 1d/3 zeigen, dass ein C nicht reicht.
- Ladefenster verschieben (PV, Raumlast).
- Sonnenschutz Daikin-Außenfühler fürs Frühjahr (Sonnenspitzen bis +14 K morgens) — Nutzer regelt selbst.
- Karte: Vorheizen und Ladeplan anzeigen — nach Betrieb entscheiden.
- Phase 4: G im Haltevorlauf über 2c hinaus, Heizkörperlüfter-Faktor, Flur-EG-Sonderfall, Nachbarraum-Kopplung.

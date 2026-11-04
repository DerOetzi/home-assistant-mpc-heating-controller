# Gewinne und Sonne — Umsetzungsplan

Spec: `docs/superpowers/specs/2026-11-03-gewinne-sonne-design.md`. Ersetzt Task 7 und Task 8 aus `2026-11-02-planer-stellglied.md`.
Umsetzung nativ in einer Sitzung je Task-Gruppe, kein Agent. Tests nach jedem Task: `cd <repo> && PYTHONDONTWRITEBYTECODE=1 /home/oetzi/.venvs/heating-controller/bin/python -m pytest -q -p no:cacheprovider`, am Ende ruff. Kein `sed -i` auf dem Mount.

## Festlegungen (Builder)

- **Ein RLS-Baustein** `controller/mpc/rls.py` für beide Ebenen. Regressoren skaliert, damit alle Parameter in W stehen: Indikatoren 1, Einstrahlung `I / 1000 W/m²` (Parameter `s`, `w` = W bei 1000 W/m²), ΔT in K (Parameter UA in W/K).
- **Gewinne als eigenes Modell** `controller/mpc/models/gains.py` (`RoomGainModel`): hält gelernte Parameter + Bestimmt-Flags, liefert `gain_w(ts, sun)`. `RoomLossModel` bleibt unverändert; Planer/Beobachter rechnen `netto = Heizung − Verlust + Gewinn`.
- **Sonne kommt vom Hub**: `IrradianceProvider` (`irradiance.py`) holt Open-Meteo stündlich, Räume bekommen eine `SunSeries` (Stundenwerte je Fläche, 24 h zurück, 48 h voraus) über `RoomMpcInput.sun`. Die Räume rufen Open-Meteo nicht selbst ab.
- **Flächen generisch** statt freier Liste **(Claude)**: Hub-Felder `building_rotation_deg` (Abweichung der Nordfassade von Nord, hier −15 → Nord 345°), `roof_orientation` (`east_west` | `north_south` | `none`), `roof_tilt_deg` (43). Daraus 4 Fassaden (`north`, `east`, `south`, `west`, Neigung 90°) + 0/2 Dachflächen (`roof_east`/`roof_west` bzw. `roof_north`/`roof_south`). Raum: zwei Mehrfachauswahlen `window_surfaces` (nur Fassaden), `opaque_surfaces` (Fassaden + Dach). Für andere Häuser reicht das für Rechteck-Grundrisse mit Satteldach.
- **Tagesart**: Hub-Feld `workday_entity` (optional, `binary_sensor`). `on` = Werktag; fehlt/`unavailable` → Mo–Fr.
- **Blöcke**: `block = local_hour // 3` (0…7), Index `block + 8 · (0 if workday else 1)` → 16 Profil-Parameter.
- **Wirkung nur wenn bestimmt** (je Parameter): `G` und `g`: `σ < 0,1 · room_heat_load_w`; `s`, `w`: `σ < 0,5 · θ` und `θ > 0`. Nicht bestimmte Parameter zählen 0.
- **Persistenz**: neuer Store je Raum `GainStore` (`<room>_<entry>_gains`), Inhalt: RLS-Zustände beider Ebenen (θ, P, Rauschschätzung, Zahl Proben), Zustand der Tiefpässe nicht (wird aus Stundenreihe neu gerechnet). Schema-Version im Store; bei Struktur-Änderung (andere Flächen im Raum) RLS der Stunden-Ebene neu.

## Tasks

### Task 1: RLS-Baustein (rein, ohne HA)

Datei `controller/mpc/rls.py`, Klasse `RecursiveLeastSquares(n, theta0, p0_diag, forgetting, lower_bounds, huber_k=2.0, trace_cap=None)`.

- `update(x, y, weight=1.0) -> UpdateResult(residual, clipped)`: Residuum `e = y − xᵀθ`; Rauschschätzung `σ²` als exponentielles Mittel von `e²` mit demselben Vergessen (Start `σ² = None` → erstes `e²`). Huber: `|e| > k·σ` → `e = sign(e)·k·σ`. Standard-RLS mit Vergessen λ, danach Projektion auf `lower_bounds` (Komponente unter Grenze → Grenze, zugehörige Zeile/Spalte von P nicht verändern). Spur von P über `trace_cap` → P skaliert.
- `std(i) = sqrt(P[i,i] · σ²)`; ohne σ² → `inf`.
- `to_dict()/from_dict()`.
- Reines Python, keine numpy-Pflicht (n ≤ 30; Matrix-Hilfen in `math_helper.py`) **(Claude)**: HA hat numpy, aber Tests und Klarheit gewinnen.

Tests `tests/controller/mpc/test_rls.py`:
- `y = 20·x + 50`, x gleichverteilt 5…20, Rauschen σ 10, 200 Proben, λ = 1 → θ innerhalb 2 % bzw. 5 W.
- Konstantes x = 12 (keine Anregung), 50 Proben → `std(G)` > 100 W (nicht bestimmbar).
- Untergrenze 0: wahres `s = −5` → gelernt 0.
- Huber: ein Ausreißer `+2000` nach 100 Proben verschiebt θ um < 2 % (ohne Huber > 10 %, Gegenprobe).
- Vergessen 0,996: Sprung des wahren Werts 50 → 100 nach 500 Proben, nach weiteren 500 Proben > 90.
- trace_cap: 1000 Proben x = 0 (keine Anregung) → Spur ≤ Kappe.
- Rundreise `to_dict`/`from_dict` gleich.

### Task 2: Nacht-Ebene `[UA, G]` — nur beobachten

- `learner.py` `_balance_stretch`: bei Outcome `LEARNED`/`LEARNED_AT_LIMIT` Punkt `x = [ΔT_mittel, −1]`, `y = (heat_j − stored_heat_j) / window_duration_s`, Gewicht `window_duration_s / 3600` an `self._night_rls.update`. λ = 0,95, θ0 = `[aktuelles UA_eff, 0]`, P0 = `diag((UA_basis)², (0,5·load)²)`, Untergrenzen `[0,15·UA_basis, −0,5·load]`. Obergrenze `|G| ≤ 0,5·load` per Clamp nach Update.
- UA-Schritt **unverändert** (Spec Schritt 1).
- `UaBalanceInfo` + Attribute am UA-Lernstatus: `learned_gain_w`, `gain_sigma_w`, `gain_fit_points`, `gain_determined`, `regression_ua_factor` (UA aus RLS / Basis, nur Diagnose).
- Persistenz: `GainStore` (Abschnitt Festlegungen), Laden in Coordinator-Setup wie `LearningFactorsStore`, Speichern verzögert nach jedem Punkt.
- `recalibrate`: optional `reset_gains: true` → beide RLS auf Start.

Tests (`test_learner.py`, Helfer `learner_helpers.py`): 3 simulierte Nächte ΔT 10/14/18 K, wahres UA 20 W/K, G 50 W → `learned_gain_w` 50 ± 5, `gain_determined` true bei load 1000 W; 5 Nächte gleiches ΔT 14 K → `gain_determined` false; Outcome `DRIFT`/`STORAGE_DOMINATED` liefert keinen Punkt; Faktor nach Bilanz identisch mit heutigem Ergebnis (Regression unverändert); Store-Rundreise.

### Task 3: Open-Meteo + Hub-/Raum-Konfiguration

- `irradiance.py`: `IrradianceProvider(hass, latitude, longitude, surfaces)` als `DataUpdateCoordinator`, Intervall 1 h (+ zufällige Minute gegen Spitzenlast **(Claude)**). Je Fläche ein GET `https://api.open-meteo.com/v1/forecast?latitude=…&longitude=…&hourly=global_tilted_irradiance&tilt=…&azimuth=…&past_days=1&forecast_days=2&timezone=UTC` über `async_get_clientsession`. Open-Meteo-Azimut = Kompass − 180. Stundenwert gilt laut Open-Meteo als Mittel der **vorangegangenen** Stunde → Zeitstempel auf Stundenmitte −30 min verschieben.
- `SunSeries`: `value_at(surface, ts)` (lineare Interpolation), `filtered_at(surface, ts, tau_s=3 h)` (Tiefpass über Reihe ab 24 h zurück, exakte Diskretisierung je Stunde), `fresh(ts)` = letzter Erfolg < 3 h. Ausfall → `None`, kein Halten.
- Hub-Flow Schritt `sun` (neu, nach `control`): `building_rotation_deg` (−180…180, Default 0), `roof_orientation`, `roof_tilt_deg` (0–90, Default 45), `workday_entity` (optional). Raum-Flow Schritt `settings` oder neuer Schritt `surfaces`: `window_surfaces`, `opaque_surfaces` (Mehrfachauswahl, beide optional — leer = kein Sonnenterm).
- Hub-Entities: `sensor.einstrahlung_<flaeche>` (W/m², Attribut `forecast` = Liste `[iso, W/m²]` 24 h voraus, `last_update`), Diagnose-Kategorie.
- Konfig für eigene Installation nach Deploy **per Reconfigure** setzen (keine Migration, Felder sind optional mit Default): Rotation −15, Dach `east_west`, 43°, Werktag `binary_sensor.workday_sensor`; Flächen je Raum laut Spec-Tabelle.
- `strings.json`, `translations/{en,de}.json`.

Tests: URL/Azimut-Umrechnung (Süd 165° → −15, Ost 75° → −105, Nord 345° → 165, West 255° → 75); Parsing einer gespeicherten Antwort (`tests/fixtures/open_meteo_gti.json`); Zeitverschiebung; Tiefpass gegen Sprung 0 → 500 W/m² (nach 3 h 316 ± 1); `fresh` false nach 3 h ohne Erfolg; HTTP-Fehler → letzte Reihe bleibt, `fresh` läuft ab; Config-Flow Hub + Raum mit neuen Feldern; Flächenliste aus Rotation + Dachform.

### Task 4: Stunden-Ebene — nur beobachten

- `controller/mpc/gain_learner.py` `HourlyGainLearner(gain_model, rls, ...)`, vom Learner je voller lokaler Stunde aufgerufen mit den History-Einträgen dieser Stunde + `SunSeries` + Tagesart.
- Probe: `ΔT_h` = Steigung Ausgleichsgerade (zeitgewichtet, gleiche Funktion wie `_trend_drift_c`, als Steigung herausziehen) × 1 h; `y = C_eff · ΔT_h / 3600 − P̄ + UA_eff · (T̄ − T̄_a) − G_bestimmt` mit `P̄` wie Bilanz (`_balance_point_power_w`: WW-Ladung und überbrückte Lücken = 0).
- `x` = 16 Block-Indikatoren (eine 1) + je Fensterfläche `I(t_mitte)/1000`, `I(t_mitte − 1 h)/1000` + je opaker Fläche `Ĩ(t_mitte)/1000`.
- Gesperrt: irgendein Eintrag mit `window_open`, `room_sensor_fallback`, `valve_position_missing`, `trv_adapting`, `radiator_not_heating`, `valve_saturated`; Lernen pausiert (Sperre, Vorlauf-Lücke, Außenwert fehlt) in der Stunde; Lücke > 20 min; < 6 Einträge; Fenster vor < 10 min geschlossen (Learner kennt `suppress_for_interval`); Neustart in der Stunde (erster Eintrag > 10 min nach Stundenbeginn). Sonne fehlt: nur verwerten, wenn Sonne die ganze Stunde unter Horizont (`sun_condition` nicht nutzbar — eigener Test über `sun.sun`-Elevation aus HA-Astral), sonst gesperrt.
- RLS: λ = 0,996, θ0 = 0, P0 = `diag((0,5·load)²)` Profil, `(2·load)²` Sonne, Untergrenzen: Profil `−0,5·load`, Sonne 0; Deckel je Diagonale = P0 (gebaut 04.11. statt Spur-Deckel; Fenster ein Term mit Stundenmittel statt 0 h/1 h, siehe Spec).
- Sensor `sensor.<raum>_zusatzwaerme` (W, jetzt wirkend nur bestimmte Terme — in Task 4 zusätzlich Attribut `ungated_w` = alle Terme), Attribute laut Spec `Sichtbar`, plus `last_hour_outcome` (`learned`, `window_open`, `gap`, `sun_missing`, …).
- Karte: Zeile „Zusatzwärme" (W + bestimmt-Anteil), nur Anzeige. **Zurückgestellt 04.11.**

Tests: synthetischer Raum (C 3,5 MJ/K, UA 30 W/K, Außen 5 °C, Raum 21 °C), Block 6 (18–21 Uhr Werktag) +100 W, Süd-Fenster `s` = 400 W bei 1000 W/m², Sonne als Tagesgang mit zufälliger Bewölkung, Sensor in 0,1-K-Stufen, 14 simulierte Tage → Block 6 = 100 ± 20 W, `s_south` 400 ± 25 %; nur klare Tage (keine Bewölkung) → `s` nicht bestimmt oder Summe Block 4 + Sonne stimmt (± 15 %); Schlafzimmer-Fall: Nacht-G 130 W, Tag −130 W → Blöcke 3–6 Werktag ≈ −130 ± 30 W; Sperr-Fälle je ein Test; Sonne fehlt am Tag → `sun_missing`.

**Neustart A** nach Task 1–4 (alle Räume). Regelung unverändert.

### Task 5: Auswertung (keine Code-Änderung)

Frühestens 10 Tage nach Neustart A, in der Tagesauswertung als eigener Abschnitt (Ablauf in `references/tagesauswertung.md` ergänzen):

- Nacht-Ebene je Raum: `learned_gain_w`, `gain_sigma_w`, Spanne ΔT der Punkte. Erwartung Schlafzimmer G ≈ 100–150 W.
- Stunden-Ebene: Profil Werktag/Wochenende je Raum, Sonnenkoeffizienten. Plausibel: Süd-/Westräume `s > 0`, Nord ≈ 0, Schlafzimmer tagsüber negativ, Küche/Esszimmer Abendblock positiv. `last_hour_outcome`-Verteilung (Anteil gesperrter Stunden).
- Vorhersagegüte: mittleres |Residuum| der Stundenproben mit vs. ohne Gewinne (Attribut `residual_rms_w` aus RLS-σ ergänzen in Task 4).
- Ergebnis + Empfehlung an Nutzer, Freigabe Task 6 durch Nutzer.

### Task 6: Gewinne in Regelung

- `RoomGainModel.gain_w(ts, sun)` = bestimmtes `G` + bestimmtes `g[block(ts)]` + Σ bestimmte `s·I` + Σ bestimmte `w·Ĩ`.
- `controller.py` `_simulate_room_temperature_c`: `net = heating − loss + gain_w(t)` je Schritt (t = Startzeit + Schritt). `_hold_demand_pct`: `hold = max(0, loss(Soll) − gain_w(now))`.
- `observer.py` `_propagate`: Gewinn zum Zeitpunkt des Schritts (Commit speichert `sun`-Referenz).
- `learner.py`: UA-Ziel `(P_netto + G) / ΔT`, wenn `G` bestimmt; Kapazitäts-Replay mit `gain_w` (nur nachts, also G + Nachtblöcke). Outcome-Text unverändert.
- Zusatzwärme-Sensor: `ungated_w` entfernen oder behalten (Diagnose) **(Claude: behalten)**.

Tests: Planer wählt bei +200 W Gewinn kleinere Öffnung (Zahlen aus bestehendem Planer-Test + Gewinn); Haltebedarf 0 bei Gewinn > Verlust; Beobachter driftet mit Gewinn nicht weg (konstanter Gewinn 100 W, 2 h, Fehler < 0,05 K); UA-Ziel mit G = 50 W: Bilanz 300 W bei ΔT 15 → Ziel (350/15)/Basis; nicht bestimmte Terme ändern nichts (alle bestehenden Tests grün ohne Anpassung).

**Neustart B** nach Nutzer-Okay aus Task 5.

### Task 7: Halte- und Mindestvorlauf mit Gewinnen

- `_calculate_hold_flow_temperature_c`: `hold = max(0, loss(Soll) − gain_w(now))`.
- `_calculate_recovery_flow`: Simulation über 6 h nutzt `gain_w(t)` mit Profil der durchlaufenen Blöcke und Sonnen-Prognose aus `SunSeries` (Prognose fehlt → Sonne 0, G und Profil bleiben).
- Hub-Leitwerte und Reserve folgen automatisch.
- Surplus-Gate unverändert (prüft Bedarf + Abstand zum Soll).

Tests: Haltevorlauf sinkt bei 200 W Gewinn (Zahl aus Emitter-Test); Recovery mit Sonne in Prognose ab Stunde 2 erreicht Ziel bei niedrigerem Vorlauf; Prognose fehlt → wie ohne Sonne.

**Neustart C** nach Nutzer-Okay; Offset-Empfehlung danach neu bewerten (Reserve-Werte vor/nach nicht vergleichen, wie bei Spreizungs-Fix).

### Task 8: Doku und Pflege (mit jedem Task mitziehen, hier Abschluss)

- `docs/mpc-guide.md`: neue Abschnitte „Gains and sun" (Modell, Lernen, Sperren, Bestimmt-Regel, Open-Meteo, Ausfall), Lernstatus-Tabelle um Attribute, Planer/Vorlauf-Abschnitte um Gewinn. Satz „Steady gains … end up in the factor" anpassen.
- README: Hub-Felder Sonne/Werktag, Raum-Flächen, Open-Meteo als externer Dienst (Datenschutz: Koordinaten gehen an Open-Meteo).
- Skill-Referenzen: `entscheidungen.md` (Bestimmt-Schwellen, Flächen-Modell), `diagnose.md` (Zusatzwärme-Sensor, `last_hour_outcome`), `tagesauswertung.md` (Task-5-Abschnitt dauerhaft).
- Plan `2026-11-02-planer-stellglied.md`: Task 7/8 auf diesen Plan verweisen.

## Deploy (nach Nutzer-Okay, nicht Teil der Umsetzung)

Neustart A (Task 1–4), danach Reconfigure Hub (Rotation −15, Dach Ost/West 43°, Werktag-Entity) und je Raum Flächen laut Spec-Tabelle. Neustart B (Task 6) und C (Task 7) je einzeln, alle Räume gleichzeitig. Commit/Push nur auf Zuruf.

## Risiken beim Bau

- History-Aufbewahrung reicht: `HISTORY_RETENTION_S` = 8 h + 1 h (geprüft 04.11.).
- Lokale Stunde vs. UTC: Blöcke lokal (Sommerzeit), Open-Meteo UTC — eine Umrechnung im Provider, Tests mit Zeitumstellung 25.10.
- Zahl Parameter je Raum bis 16 + 2·Fenster + Opak ≤ 30 → RLS in reinem Python ohne Last (Rechnung 1× je Stunde).

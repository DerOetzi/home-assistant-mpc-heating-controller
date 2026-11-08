# Offener Umsetzungsplan (Stand 2026-11-07)

Nur was noch zu tun ist, mit den Angaben für die Umsetzung. Erledigtes steht im Code, in `mpc-guide.md` und in der Git-Historie. Getroffene Entscheidungen stehen im Skill `heating-controller` (`references/entscheidungen.md`).

Regeln für jeden Schritt: alle Räume gleichzeitig. Tests `PYTHONDONTWRITEBYTECODE=1 ~/.venvs/heating-controller/bin/python -m pytest -q -p no:cacheprovider`, danach ruff. Change-Set zeigen. Neustart nur nach Nutzer-Okay. Doku (`mpc-guide.md`, README, `strings.json`, `translations/{en,de}.json`, Skill-References) mit jedem Schritt mitziehen.

## 1. Gewinne und Sonne

Stand: Nacht-Regression `[UA, G]`, Stunden-Profil, Sonne je Fläche (Open-Meteo GTI) lernen und sind sichtbar (`sensor.<raum>_zusatzwarme`, UA-Lernstatus-Attribute, Karte). Sie wirken **noch nicht** in Regelung.

Modell (je Raum, W): `C · dT/dt = P_hk − UA · (T − T_a) + G + g(Block, Tagesart) + Σ s_f · Ī_f + Σ w_o · Ĩ_o`. Ein Term wirkt nur, wenn bestimmt (Schwellen siehe `entscheidungen.md`), sonst 0.

### 1a. Auswertung (keine Code-Änderung)

Frühestens 10 Tage nach Deploy Gewinne (04.11.), also ab ca. 14.11., als eigener Abschnitt der Tagesauswertung:
- Nacht-Ebene je Raum: `learned_gain_w`, `gain_sigma_w`, Spanne ΔT der Punkte. Erwartung Schlafzimmer G ≈ 100–150 W.
- Stunden-Ebene: Profil Werktag/sonst, Sonnenkoeffizienten. Plausibel: Süd-/Westräume `s > 0`, Nord ≈ 0, Schlafzimmer tagsüber negativ, Esszimmer Abendblock positiv. Verteilung `last_hour_outcome` (Anteil gesperrter Stunden).
- Vorhersagegüte: `residual_rms_w` mit vs. ohne Gewinne.
- Ergebnis + Empfehlung an Nutzer. Nutzer gibt 1b frei.

### 1b. Gewinne in Regelung (Neustart B)

- `RoomGainModel.gain_w(ts, sun)` = bestimmtes `G` + bestimmtes `g[block(ts)]` + Σ bestimmte `s·I` + Σ bestimmte `w·Ĩ`.
- `controller.py` `_simulate_room_temperature_c`: `net = heating − loss + gain_w(t)` je Schritt. Über 30 min Werte der aktuellen Stunde, `I` aus Prognose.
- `_hold_demand_pct`: `hold = max(0, loss(Soll) − gain_w(now))`.
- `observer.py` `_propagate`: Gewinn zum Zeitpunkt des Schritts.
- `learner.py`: UA-Ziel `(P_netto + G) / ΔT`, wenn `G` bestimmt (Schritt 30 % bleibt, Grenzen 0,15/2 bleiben). Kapazitäts-Replay mit `gain_w` (nachts: G + Nachtblöcke).
- Attribut `ungated_w` am Zusatzwärme-Sensor behalten (Diagnose).

Tests: Planer wählt bei +200 W Gewinn kleinere Öffnung; Haltebedarf 0 bei Gewinn > Verlust; Beobachter mit konstantem Gewinn 100 W über 2 h Fehler < 0,05 K; UA-Ziel mit G = 50 W: Bilanz 300 W bei ΔT 15 → Ziel (350/15)/Basis; unbestimmte Terme ändern nichts (alle bestehenden Tests grün ohne Anpassung).

### 1c. Halte- und Mindestvorlauf mit Gewinnen (Neustart C, eigener Neustart)

- `_calculate_hold_flow_temperature_c`: `hold = max(0, loss(Soll) − gain_w(now))`.
- `_calculate_recovery_flow`: Simulation 6 h mit Profil der durchlaufenen Blöcke und Sonnen-Prognose je Stunde aus `SunSeries`. Prognose fehlt → Sonne 0, G und Profil bleiben.
- Hub-Leitwerte und Reserve folgen automatisch. Surplus-Gate unverändert.
- Danach Offset-Empfehlung neu bewerten; Reserve vor/nach nicht vergleichen.

Tests: Haltevorlauf sinkt bei 200 W Gewinn; Recovery mit Sonne ab Stunde 2 erreicht Ziel bei niedrigerem Vorlauf; Prognose fehlt → wie ohne Sonne.

## 3. Ladeplan Warmwasser im MPC

Anlass 05.11. 13:05–13:45: Zwangsladung, Heizkreis 40 min ohne Umlauf, Duschbad 20,8 → 20,5 °C, Rebekka lau; erst Offset +3 hielt. Ziel: Ladungen vorhersagen, gefährdete Räume heizen dosiert vor, Vorlauf steigt vorher und in der Erholung. Kein Vorheizen „auf Verdacht" in allen Räumen.

### 3a. `control/charge_plan.py` (reines Python)

`planned_charges`: Liste `(start, dauer, art, sicher)` für die nächsten 6 h.

| Art | Auslöser | Vorhersage |
|---|---|---|
| `night_end` | Ende Nachtfenster (Soll 38 → 46) | Ladung, wenn Speicher-Prognose < Normal − Geräte-Hysterese |
| `charge_window` | Beginn Ladefenster | Ladung, wenn Prognose < Ladefenster-Soll |
| `legionella` | geplanter Zyklus | immer, Dauer aus Ziel 60 °C |
| `reheat` | Abkühlung ohne Zapfung | Ladung, wenn Prognose < Soll − Hysterese in 6 h; `sicher = false` |

- Speicher-Prognose = Speicher jetzt − Abkühlrate × Zeit. Abkühlrate gelernt: Median der Stunden ohne Ladung und ohne Zapfung (Zapfung = Abfall > 1 K in 15 min). Start 0,5 K/h.
- Dauer = (Soll − Prognose) / Laderate + 5 min Anlauf (Daikin startet ~5 min nach Sollwechsel). Laderate gleitendes Mittel über 10 erkannte Ladungen, Start 0,25 K/min.
- Raten persistiert im Store des `hot_water`-Subentrys.
- Sensor `sensor.warmwasser_naechste_ladung`.
- PV-Überschuss- und Nach-Dusch-Ladungen nicht planbar: wirken wie heute (erkannt, Lernen gesperrt, kein Vorheizen).

Tests: synthetischer Speicherverlauf inkl. Zapfung.

### 3b. Controller: Vorheizen je Raum

Hub gibt jedem Raum die nächste Ladung mit `sicher = true` als `ChargeWindow(start_ts, end_ts)` in `RoomMpcInput`, wenn sie in `preheat_window_max` (90 min) beginnt oder läuft. `reheat` erst ab 30 min vorher.

`_calculate_charge_preheat(mpc_input, estimate, window)`:
1. Simulation: bis `start` mit aktueller Planöffnung, `start`–`end` ohne Heizleistung → Raumtemperatur bei `end`.
2. ≥ Soll − `charge_allowed_dip` (0,2 K): nichts tun.
3. Sonst kleinster `lift` (Schritt 0,05 K, Deckel `charge_preheat_max_lift` 0,5 K), mit dem Ziel `Soll + lift` bis `start` der Raum bei `end` ≥ Soll − Dip liegt (normaler Planer simuliert).
4. `lift` gilt ab `start − Vorlaufzeit` bis `start`. Vorlaufzeit = simulierte Zeit bei voller Öffnung für `lift` + 15 min, höchstens 90 min.
5. Deckel reicht nicht: `lift = Deckel`, Attribut `charge_at_risk`.

Attribute am Bedarfssensor: `charge_preheat_lift`, `charge_preheat_from`, `charge_at_risk`. 30-min-Horizont bleibt, Zuschlag wirkt über das Ziel.

`charge_min_flow_temperature_c` je Raum: kleinster Vorlauf, mit dem der Raum (mit Vorheizen, Ventil bis 100 %) bei `end` ≥ Soll − Dip liegt und 60 min nach `end` (`charge_recovery_s`) wieder auf Soll ist. Suche wie `_calculate_recovery_flow`, gleiche Spreizungsregel. Außerhalb Fenster `None`.

Tests: massearmer Raum (Duschbad-Parameter) gefährdet, schwerer Raum nicht, Deckel greift.

### 3c. Hub und Lerner

- `charge_flow = min(max(charge_min_flow aller Räume), lead_min_flow + marge + number.vorlauf_anhebung_warmwasser_max)`. Alle Räume zählen, nicht nur Leiträume. Fenster: Vorheizbeginn bis `end + charge_recovery_s`. In `flow`-Regel als Quelle `charge`, Anhebung/Rücknahme als Pflicht-Schreiben über Budget.
- Lerner: UA-Strecke endet bei Vorheizbeginn (gewollter Anstieg verletzt sonst Drift-Test). Betrifft vor allem `night_end` ~05:00, in Tagesauswertung beobachten.
- Während Ladung bleibt heutige Sperre. Kennlinien-Probe läuft weiter.

### 3d. Deploy

Neustart nach Okay, eine Zwangsladung beobachten (Duschbad/Rebekka gegen 05.11.).

## 4. Beobachten (laufend, Tagesauswertung)

- Ventilkennlinien seit Rampen-Probe 05.11.: Tabelle je TRV (Status, Klassen, Abstand zu Seed). `scattered` bei mehreren TRVs → innerer Regelkreis auf gemessene Öffnung erwägen. Weicht gelernte Kurve bei den meisten TRVs deutlich vom Seed ab → Seed im Profil nachziehen vorschlagen.
- Planer in Öffnung: Zu/Auf-Wechsel je TRV pro Nacht und `trv_setpoint_writes` gegen Nächte vor 03.11.
- Wirkung (h), (i), (k) und Heizkörper-Check-Regeln seit 31.10.
- UA-Einschwingen nach Daikin-Außenwert (01.11.) und Pumpenwechsel (04.11.).
- Strecken ab Sonne 2 h unter: Starts vor 22:00 (Abendnutzung) auf Drift prüfen.
- Reserve/Offset nach Spreizungs-Fix Tag/Nacht getrennt bewerten; vor 01.11. nicht vergleichbar.
- Arbeitszimmer unter Soll bei 100 % Ventil: vermutlich Voreinstellung — Nutzer prüfen lassen.
- e1/e2: Stufen bzw. Mindestabstand beim Nachführen der TRV-Sollwerte — klären, ob noch gewünscht.

## 5. Absenk-Simulation (`tools/`, offline, nicht in Integration)

Ziel: Strom-Ersparnis vs. Mehrbedarf beim Aufholen, wenn alle Räume (außer Flur EG, Dani — bleiben Basis) für X h um Y K abgesenkt werden, je Startzeit und Tagestyp. Ergebnis: HTML-Heatmap-Seite. Wiederholbar nach jeder Saison mit neu gelernten Parametern. Ändert nichts in HA, Absenklogik in Integration bleibt eigenes Thema.

### 5a. Eingaben

- **Raumparameter** per MCP aus Lernstatus-Attributen (UA-Faktor, Kapazitätsfaktor, Fläche, Bauweise, Heizkörper, Ventilkennlinie, Gewinne soweit bestimmt) → `tools/absenk_sim/rooms.json`. Kein Recorder-Zugriff aus Skript.
- **Wetter**: Open-Meteo-Archiv Leinburg, vergangene Winter stündlich (Temperatur, GTI je Fassade wie Integration). Offset zum Daikin-Fühler aus Überlappung Okt/Nov fitten. Tage klassieren: Tagesmittel < −5, −5…0, 0…5, 5…10, > 10 °C × klar/trüb. Je Klasse Mediantag + Streuung über alle Tage.
- **COP**: Form COP(T_außen, T_vorlauf) aus Datenblatt (Modell per `ha_get_device`). Kalibrierung gegen Daikin-Strom (nur 2-h-Summen, keine Wärme): Zwilling auf vergangene Tage mit gemessenem Vorlauf + Ventilöffnungen → Q je 2-h-Fenster, Skalierfaktor(en) fitten, sodass Q/COP Strom trifft. WW-Ladungen, Abtauen, Neustart-/Ausfallfenster raus (prüfen ob Onecta Heizen/WW trennt). Restfehler = Validierungsmaß.

### 5b. Zwilling

- `tools/absenk_sim/` importiert `models/{emitter,loss,capacity}.py` und Planer aus `controller.py` (keine Kopie der Physik), venv `~/.venvs/heating-controller`.
- Alle 10 Räume gleichzeitig, Schritt wie Controller. Gemeinsamer Vorlauf = max. Mindestvorlauf der Vorlauf-Leiträume, begrenzt durch Kurve + Offset und Auslegungsvorlauf. WP-Maximalleistung je T_außen als Deckel.
- Validierung vor Auswertung: vergangene Tage nachspielen, RMSE Raumtemperatur je Raum ausweisen.
- Grenzen markieren statt modellieren: Zellen mit Vorlauf unter Schwelle/Mindestmodulation (Takten) kennzeichnen.

### 5c. Szenarien

- Raster: Startstunde 0–23 × Dauer 1–24 h (+ 48 h) × Y = 1/2/3/4 K × Tagestyp.
- Benannt (Marker + Tabelle): Arbeitstag 7–17, Vormittag 8–13, Nachmittag 13–18, Abend 17–23, Nacht 22–6, Nacht lang 21–7, Ausflug 6–22, 24 h, Wochenende 48 h.
- Fenster 3 Tage: Einschwingtag, Absenkung, Nachlauftag mit vollem Aufholen. Ersparnis = Differenz zum Basisfall (immer Komfort) über ganzes Fenster.
- Aufholstrategien: (a) vorheizen mit bekannter Rückkehrzeit, Soll bei Rückkehr erreicht (Obergrenze, MPC kennt Rückkehr heute nicht); (b) Aufheizen erst bei Rückkehr.
- Sensitivität Kapazität × 0,5 / 1 / 2 (1R1C bildet träge Wandmasse nicht ab).

### 5d. Ausgaben

- Je Zelle: Strom kWh Basis/Szenario, Ersparnis %, Aufholdauer, Komfortdefizit Kh nach Rückkehr, Spitzenvorlauf, Takt-Markierung → `results.json`.
- HTML-Seite: Heatmap X = Startstunde, Y = Dauer, divergierende Farbe (Ersparnis/Mehrverbrauch). Auswahl Tagestyp, Y, Strategie, Kapazitäts-Sensitivität. Zweite Heatmap Aufholdauer/Komfortdefizit. Klick auf Zelle: Raumtemperaturen, Vorlauf, COP, Strom Basis vs. Szenario, Anteil je Raum.
- Vorab-Kennzahl: τ = C/UA je Raum.
- Später optional: €-Skala mit PV-Deckung aus SolarEdge typischer Tage.

### 5e. Reihenfolge

1. τ je Raum + `rooms.json`. 2. Wetter-Archiv + Klassierung. 3. Zwilling + Validierung gegen vergangene Tage. 4. COP-Fit gegen 2-h-Strom. 5. Szenario-Raster. 6. HTML. Sinnvoll nach 1b (Gewinne in Regelung), sonst Gewinne getrennt ein-/ausschaltbar.

Ergebnis an Nutzer; berührt Entscheidung „Keine Tag/Nacht-Absenkung“ — Bewertung trifft Nutzer.

## 6. Hysterese-Simulation (`tools/`, baut auf 5)

Ziel: Pendelbetrieb statt Halten — Räume auf Soll + X K heizen, dann bis Soll − Y K auskühlen lassen, wiederholt. Frage: Strom, WP-Takten und Komfort gegenüber heutigem Halten im Zielband. Gleicher Zwilling, gleiche Eingaben (Räume, Wetter-Tagestypen, COP-Fit) wie 5; Szenario-Modul daneben, kein eigener Zwilling.

### 6a. Betriebsarten

- (a) **Nur Räume pendeln, WP läuft durch**: TRVs je Raum mit Hysterese, gemeinsamer Vorlauf wie heute aus Leiträumen. Räume laufen phasenversetzt.
- (b) **Synchron, WP im Intervall**: alle Räume heizen gemeinsam bis +X, dann WP aus (Heizanforderung weg) bis erster Leitraum −Y erreicht. Heizphase mit Vorlauf für Anstieg in Zeit T_heiz (Parameter), Abkühlphase ohne Heizleistung.
- (c) **PV-gekoppelt**: wie (b), Heizphase bevorzugt bei Überschuss (SolarEdge-Tagesprofile), +X als thermischer Speicher.
- Vergleichsbasis: heutiges Halten (Zielband 0,1 K).

### 6b. WP-Modell (Zusatz zu 5)

- Mindestmodulation (Datenblatt) und Takt-Mindestzeiten als Grenze: unter Mindestleistung zählt Halten als Takten (Starts/h aus Leistungsbedarf geschätzt). Ohne Wärmezähler nur Modellschätzung, so ausweisen.
- COP je Phase aus T_außen und Vorlauf der Phase; Startverluste je Start als Parameter (Datenblatt/Literatur, Sensitivität 0 / typisch / hoch).
- Abtauen: COP-Abschlag im Bereich ca. −5…+5 °C wie in 5.

### 6c. Raster

- X = 0…1,5 K, Y = 0…1,5 K (Schritt 0,25) × Betriebsart × Tagestyp, Rechenfenster 2 Tage nach Einschwingtag.
- Mitteltemperatur verschiebt sich bei X ≠ Y: Ergebnis zweifach ausweisen — wie gerechnet und bezogen auf gleiche mittlere Raumtemperatur (UA-Korrektur), sonst vergleicht man Komfortniveaus statt Betriebsweisen.
- Sensitivität Kapazität × 0,5 / 1 / 2 (Zykluslänge hängt direkt an C).

### 6d. Ausgaben

- Je Zelle: Strom kWh vs. Basis (%), WP-Starts/Tag, mittlere Lauf- und Pausendauer, Zyklusdauer je Raum, Komfort (Kh unter Soll − 0,5, max. Abweichung), Spitzenvorlauf, TRV-Schreibzahl (Akku, Zigbee).
- HTML-Seite wie 5: Heatmap X × Y, Auswahl Betriebsart/Tagestyp/Bezug; Klick auf Zelle: Verlauf Räume, Vorlauf, WP an/aus, Strom.
- Gemeinsame Seite mit 5 möglich (Reiter Absenkung / Hysterese).

### 6e. Reihenfolge

Nach 5 Schritt 4 (Zwilling + COP-Fit validiert). 1. Betriebsart (a) und Basis, 2. (b) mit WP-Intervall, 3. (c) PV, 4. HTML.

Ergebnis an Nutzer; berührt Entscheidungen „Zielband 0,1 K“ und „WP-Heizbetrieb schaltet Nutzer“ — Bewertung trifft Nutzer.

## Später / offen (eigenes Design, nicht ungefragt)

- Ladefenster verschieben (PV, Raumlast).
- Sonnenschutz Daikin-Außenfühler fürs Frühjahr (Sonnenspitzen bis +14 K morgens) — Nutzer regelt selbst.
- Karte: Vorheizen und Ladeplan anzeigen — nach Betrieb entscheiden.
- Phase 4: G im Haltevorlauf über 1c hinaus, Heizkörperlüfter-Faktor, Flur-EG-Sonderfall, Nachbarraum-Kopplung.

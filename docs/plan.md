# Offener Umsetzungsplan (Stand 2026-11-06)

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

## 2. Vorlauf: Schatten beenden

`switch.vorlauf_schreiben` ist aus (Schatten). Offen:
- So 08.11.: Schattenwerte seit Neustart mit Fallback-Wartezeit gegen Ist und Tagesauswertung prüfen (Esszimmer bestimmt Vorlauf fast allein; nachts Schatten 3–8 K unter Offset +2; Schreibzahl gegen Budget), dann Nutzer schaltet `switch.vorlauf_schreiben` an.
- Danach: Prüfpunkt „Offset-Empfehlung" in `tagesauswertung.md` wird „Vorlauf-Regelung geprüft" (Schreibzahl, `external_change`, Budget).

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

## Später / offen (eigenes Design, nicht ungefragt)

- Ladefenster verschieben (PV, Raumlast).
- Sonnenschutz Daikin-Außenfühler fürs Frühjahr (Sonnenspitzen bis +14 K morgens) — Nutzer regelt selbst.
- Karte: Vorheizen und Ladeplan anzeigen — nach Betrieb entscheiden.
- Phase 4: G im Haltevorlauf über 1c hinaus, Heizkörperlüfter-Faktor, Flur-EG-Sonderfall, Nachbarraum-Kopplung.

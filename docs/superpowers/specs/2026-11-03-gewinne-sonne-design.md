# Gewinne und Sonne (Task 7 + Task 8) — Design

Stand 2026-11-03. Entwurf, noch nicht freigegeben. Nutzerentscheidungen 03.11.: Task 7 und Task 8 gemeinsam, Vorlage Thermocast (MIT), Sonne aus Open-Meteo, Gewinne nur als Stundenprofil (keine Leistungs-/Präsenzsensoren), Tor aus Task 6 Step 3 entfällt (Bestimmbarkeit zeigt die Kovarianz). Übrige Festlegungen sind als **(Claude)** markiert, offene Fragen am Ende.

## Absicht und Warum

Heute steckt jede Wärme, die nicht vom Heizkörper kommt, im UA-Faktor der Nacht. Folgen:

- **Schlafzimmer** (Befund 03.11.): UA nachts mit 120–150 W Personenwärme gelernt. Tagsüber fehlt diese Wärme, Haltebedarf und Haltevorlauf liegen zu tief.
- **Sonne**: Planer kennt keine Sonne. Räume mit Süd-/Westfenstern laufen an sonnigen Tagen über Soll, Ventile schließen erst danach.
- **UA und fester Gewinn** sind aus einer Nacht nicht trennbar (Befund 6, Fremdwärme entfernt 02.11.). Trennbar nur über mehrere Nächte mit verschiedenem ΔT.

Ziel: Wärmebilanz je Raum bekommt drei weitere Terme, die gelernt und im Planer genutzt werden. UA bleibt Nacht-Größe.

## Modell

Je Raum, Leistungsbilanz in W:

```
C · dT/dt = P_hk − UA · (T − T_a) + G + g(h, d)
           + Σ_f s_f · Ī_f(h)                 (Fenster, Stundenmittel)
           + Σ_o w_o · Ĩ_o(t)                 (opake Flächen)
```

`Ĩ_o` = Einstrahlung der opaken Fläche `o` durch Tiefpass 1. Ordnung, τ = 3 h **(Claude)**: Wand/Dach gibt Sonnenwärme verzögert und gedämpft ab. Ein Regressor je Fläche statt 1–6 Verzögerungen (Thermocast Dach): weniger Parameter, Form fest, Stärke gelernt.

| Term | Bedeutung | Gelernt aus | Vorzeichen |
|---|---|---|---|
| `UA` | Verlust | Nacht-Ebene | > 0, 0,15–2 × Basis wie heute |
| `G` | fester Gewinn, nachts gemessen (Personen schlafend, Nachbarräume, Erdreich negativ) | Nacht-Ebene | frei, `|G| ≤ 0,5 · room_heat_load_w` (Task 7) |
| `g(h, d)` | Abweichung vom Nacht-Gewinn je Tageszeit-Block `h` und Tagesart `d` | Stunden-Ebene | frei, Grenze wie `G` |
| `s_f` | Sonnen-Koeffizient Fensterfassade `f` (W bei 1000 W/m²) | Stunden-Ebene | ≥ 0 |
| `w_o` | Sonnen-Koeffizient opake Wand/Dachfläche `o` | Stunden-Ebene | ≥ 0 |
| `I_f`, `I_o` | Einstrahlung je Fläche (W/m²), Open-Meteo GTI | Eingang | — |

`g` ist bewusst relativ zur Nacht: Nachtstunden ergeben `g ≈ 0`, Schlafzimmer tagsüber `g < 0` (Personen weg). Damit gibt es keinen zweiten Schätzer für denselben Dauerzustand (Lehre aus Fremdwärme).

`s_f` nimmt Fensterfläche, g-Wert, Rollläden (Handbetrieb, nicht gemessen) und Verschattung (Haus 13 vor Westfassade) mit auf. Kein Flächen-Eingang.

## Ebene 1: Nacht (ersetzt Task 7)

- Jede Bilanz-Strecke mit Outcome `learned*` liefert einen Punkt: `mean_ΔT`, `P_netto = mean_heating_w + Speicherterm` (Werte wie heute im Lerner).
- RLS mit zwei Parametern `[UA, G]` auf `P_netto = UA · mean_ΔT − G`. Vergessen **0,95 je Nacht** (≈ 20 Nächte Gedächtnis) **(Claude)**. Gewicht je Punkt = Stunden der Strecke.
- **Bestimmbarkeit**: Standardabweichung von `G` aus der Kovarianz × Rauschschätzung. `G` gilt als bestimmt, wenn `σ_G < 0,1 · room_heat_load_w` **(Claude)**. Davor `G = 0`, UA wie heute aus Einzelnacht.
- Sobald `G` bestimmt: UA-Ziel der Nacht = `(P_netto + G) / mean_ΔT`, weiter mit Schritt 30 % wie heute. RLS schätzt nur `G` weiter; UA bleibt vom bewährten Einzelnacht-Schritt geführt **(Claude)**. Grund: eine Ausreißernacht verschiebt UA damit nicht stärker als heute.
- Grenzen `learned_at_limit` (0,15) und `implausible_target` (> 2) bleiben.

## Ebene 2: Stunde (Task 8)

- **Probe** je volle Stunde: Restwärme `r = C · ΔT_h / 1 h − P̄_hk + UA · (T̄ − T̄_a) − G` (W). `ΔT_h` aus Ausgleichsgerade über die Stunde (wie Drift heute), nicht aus Rändern.
- **Regressoren**: Indikator Tageszeit-Block `h` × Tagesart `d`, dazu Stundenmittel `Ī_f` je Fensterfassade des Raums, dazu `Ĩ_o(t)` je opaker Fläche (Außenwand, Dachschräge). Geändert beim Bau 04.11.: zwei Fenster-Terme 0 h / 1 h (Thermocast) waren bei Stundenproben fast gleich (Sonne glatt), Schätzung instabil (synthetisch offline +495/−163 statt 400/0, mit Untergrenze 807/0). Verzögerter Anteil steckt im Wand-Term.
- **Lernen**: RLS, Vergessen **0,996 je Stunde** (≈ 10 Tage, Thermocast), Projektion `s, w ≥ 0`, Huber-Dämpfung bei 2 σ der Innovation (Rauschen + Parameter-Unsicherheit), Deckel je Diagonale von P (gegen Aufblähen nie angeregter Blöcke, Sommer; Spur-Deckel hätte alle Parameter mitgebremst).
- **Gesperrte Stunden**: alle heutigen Sperren (Fenster offen + 10 min danach, TRV-Adaption, Raumsensor-Ersatz, Ventil fehlt, Heizkörper heizt nicht, Außenwert fehlt, Vorlauf > 16 min ohne Wert, Lernsperre des Raums) sowie `valve_saturated` in der Stunde, Lücke > 20 min, Neustart in der Stunde. Warmwasser-Ladung zählt wie heute als `P_hk = 0`, sperrt nicht.
- **Sonne fehlt** (Open-Meteo nicht erreichbar, Wert älter als 3 h): Stunde ohne Sonnen-Regressoren nur verwerten, wenn Sonne unter Horizont; sonst gesperrt. Kein Schätzen (gleiche Regel wie Außenwert).

## Eingang Sonne: Open-Meteo

- Core-Integration `open_meteo` liefert nur `weather.*` ohne Einstrahlung. heating_controller ruft die Forecast-API selbst: `hourly=global_tilted_irradiance`, `tilt=90`, `azimuth` je Fassade, `past_days=1`, `forecast_days=2`, Standort aus HA-Konfiguration. Ein Abruf je Fassade und Stunde über HA-`aiohttp`-Session, kein Schlüssel.
- **Flächen im Hub** (Liste Name + Kompass-Azimut + Neigung), Räume wählen je Fläche „Fenster" und/oder „opak". Hausvorgabe (15° gedreht): Fassaden Nord 345°, Ost 75°, Süd 165°, West 255°, Neigung 90°; Dach Ost 75°, Dach West 255°, Neigung 43° (aus Foto Südgiebel geschätzt, ±3°, Nutzer 04.11.). Umrechnung auf Open-Meteo (0 = Süd, −90 = Ost): `azimut − 180`. 6 Abrufe je Stunde.
- Hub-Sensor je Fläche `sensor.<flaeche>_einstrahlung` (W/m², mit Prognose als Attribut).
- Zuordnung im Haus (Nutzer 04.11.: opake Ostwände Hauptbad, Duschbad, Arbeitszimmer; Dachflächen Ost/West Schlafzimmer, Dani; übrige opake Flächen aus Außenwänden `grundriss-haus.md` **(Claude)**):

„Fenster + Wand" = zwei Terme (Fenster sofort, Wand verzögert), „nur Wand/Dach" = nur verzögerter Term. Geprüft Nutzer 04.11.

| Raum | Nord | Ost | Süd | West | Dach Ost | Dach West |
|---|---|---|---|---|---|---|
| Wohnzimmer | — | — | Fenster + Wand | Fenster + Wand | — | — |
| Esszimmer (mit Küche) | Fenster + Wand | nur Wand (Küche) | — | Fenster + Wand | — | — |
| Hauptbad | — | nur Wand | Fenster (zur Loggia) | — | — | — |
| Flur EG | — | nur Wand (Wandschrank) | — | Fenster + Wand (Glastür zum unbeheizten Windfang, Licht über dessen Glasbausteine; Haustür Süd am Windfang) | — | — |
| Rebekka | Fenster + Wand | — | — | Fenster + Wand | — | — |
| Duschbad | Fenster + Wand | nur Wand | — | — | — | — |
| Katharina | — | — | Fenster + Wand | Fenster + Wand | — | — |
| Arbeitszimmer | — | nur Wand | Fenster (über Winterbalkon) | — | — | — |
| Schlafzimmer | — | — | Fenster + Wand (Giebel) | — | nur Dach | nur Dach |
| Dani | Fenster + Wand (Giebel) | — | — | — | nur Dach | nur Dach |

Hauptbad und Arbeitszimmer ohne Süd-Wandterm: Südwand grenzt an Loggia bzw. Winterbalkon.

Nordflächen bekommen nur Streulicht, Dachschrägen liegen über Kriechgängen (Puffer). Koeffizient lernt das; bleibt er bei 0, kostet er nichts. Fenster- und Opak-Term derselben Fassade sehen dieselbe Einstrahlung, unterscheiden sich nur in der Verzögerung — trennbar über Tage mit Wolkenwechsel, sonst teilt RLS die Wirkung beliebig auf (Summe stimmt trotzdem).

## Verwendung

- **Planer** (`_simulate_room_temperature_c`) und **Beobachter** (`_propagate`): Verlust wird `UA · ΔT − G − g(h, d) − Σ s · I`. Über 30 min Vorhersage Werte der aktuellen Stunde; `I` aus Prognose.
- **Haltebedarf** (`_hold_demand_pct`): gleiche Gewinne zum aktuellen Zeitpunkt.
- Gewinne wirken erst, wenn ihr Parameter bestimmt ist (σ-Schwelle je Term), sonst 0. Erste Wochen also Verhalten wie heute.
- **Halte-/Mindestvorlauf**: mit Gewinnen, siehe Entschieden Punkt 3.
- Keine längere Vorausschau in diesem Design. Prognose liegt aber bereit (später vorausschauend schließen vor Sonne).

## Sichtbar

- Sensor je Raum `sensor.<raum>_zusatzwaerme` (W, jetzt wirkend: `G + g + Sonne`), Attribute: `fester_gewinn_w`, `gewinnprofil_w` (Blöcke × Tagesart), `sonne_w`, `sonnen_koeffizienten` je Fläche (Fenster/opak getrennt), `bestimmt` je Term, `stunden_proben`.
- UA-Lernstatus-Attribute `learned_gain_w`, `gain_sigma_w`, `gain_fit_points` (wie Task 7).
- Karte: Zeile Zusatzwärme (offen, beim Bau 04.11. zurückgestellt: Karte hat eigenes Rollen-System).
- `recalibrate`: optional `reset_gains: true` (setzt RLS beider Ebenen zurück).
- Persistenz: RLS-Zustand (θ, P, Rauschschätzung) im Lern-Store je Raum.

## Umsetzung in Schritten (je Schritt Neustart, alle Räume gleichzeitig)

1. **Nacht-Ebene, nur beobachten**: RLS `[UA, G]` läuft mit, Attribute sichtbar, UA-Schritt unverändert.
2. **Open-Meteo + Stunden-Ebene, nur beobachten**: Fassaden im Hub, Einstrahlung-Sensoren, Stunden-RLS lernt, `zusatzwaerme` sichtbar, Planer unverändert. Tagesauswertung prüft Plausibilität (Schlafzimmer tags `g < 0`, Südräume `s_Süd > 0`).
3. **Nutzung**: Nacht-UA mit `G`, Planer/Beobachter/Haltebedarf mit Gewinnen. Nach Nutzerfreigabe aus Schritt 1/2.
4. Halte-/Mindestvorlauf mit Gewinnen (eigener Neustart, damit Wirkung auf Reserve/Offset-Empfehlung getrennt sichtbar).

## Nicht-Ziele

- Kein Tageslernen von UA (Entscheidung „UA nur aus Nacht-Wärmebilanz" bleibt).
- Keine Nachbarraum-Kopplung (Phase 4).
- Keine Leistungs-/Präsenzsensoren als Regressoren.
- Keine Vorausschau > 30 min, keine Vorlaufsteuerung, kein Offset setzen.
- Kein 2R2C-Modell.

## Risiken

- **Stundenrauschen**: 0,1-K-Sensorschritte. Wohnzimmer `C ≈ 21 m² · 165 kJ/m²K ≈ 3,5 MJ/K`: 0,1 K/h ≈ 100 W. RLS mittelt, Huber dämpft; trotzdem ein Block braucht viele Tage. Darum 3-h-Blöcke statt Einzelstunden.
- **Sonne und Tagesprofil korrelieren** (Sonne immer mittags). Trennung nur über bewölkte Tage. Bestimmbarkeit aus Kovarianz zeigt das; ohne bewölkte Tage bleibt `s` unbestimmt und wirkt nicht.
- **Rollläden**: werden sie unregelmäßig genutzt, streut `s`. Kein Sensor, bewusst akzeptiert.
- **Open-Meteo-Ausfall**: Sonne 0, Stunden-Lernen tagsüber gesperrt, Regelung läuft weiter.

## Entschieden (Nutzer 04.11.)

1. **Profil: 8 Blöcke à 3 h × Tagesart** (00–03, 03–06, … 21–24) = 16 Werte je Raum.
2. **Tagesart aus Werktag-Entity im Hub** (optional, `binary_sensor`; hier `binary_sensor.workday_sensor`). Ohne Entity oder bei `unavailable`: Mo–Fr = Werktag.
3. **Halte- und Mindestvorlauf mit Gewinnen**: `G`, Tagesprofil und Sonne, je nur sobald bestimmt. Haltevorlauf mit Gewinnen der aktuellen Stunde. Recovery-Simulation (6 h) mit Profil der durchlaufenen Blöcke und Sonnen-Prognose je Stunde. Folge: Empfehlung sinkt an sonnigen Tagen; bleibt Sonne gegen Prognose aus, fehlt Reserve bis zur nächsten Rechnung.

## Offene Fragen an Nutzer

Keine. Nächster Schritt: Detailplan.

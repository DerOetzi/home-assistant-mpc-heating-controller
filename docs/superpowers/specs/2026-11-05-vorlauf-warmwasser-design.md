# Vorlauf- und Warmwassersteuerung im heating_controller — Design

Stand 2026-11-05. Entscheidungen des Nutzers aus der Sitzung vom 05.11. sind ohne Markierung, Festlegungen von Claude sind mit **(Claude)** markiert. Alle Zahlen unten sind Startwerte, jeder Wert ist im Betrieb als Entity oder in der Subentry-Konfiguration änderbar (Nutzer: „alles im Nachhinein konfigurierbar").

## Anlass

05.11., 13:05–13:45: Zwangsladung des Warmwassers. Das Dreiwegeventil steht dabei auf dem Speicher, der Strömungsschalter stoppt die Sekundärpumpe. Der Heizkreis hat 40 min keinen Umlauf, der 100-l-Puffer wird nicht genutzt.

- Duschbad (Handtuchheizkörper, kaum Masse) fiel von 20,8 auf 20,5 °C. Bei Vorlauf 34–38 °C (Offset 0) holte es trotz Ventil 100 % nicht auf. Mit Offset +3 (42–44 °C) hielt es. Rebekka hängt am selben OG-Strang und war ebenso lau.
- Nach der Ladung standen nur Esszimmer, Duschbad und Rebekka offen, alle anderen auf 0–1 %. Es ist also kein Verteilproblem, sondern der Vorlauf war zum Aufholen zu niedrig.

Der Regler kennt die Ladung heute nur nachträglich (Erkennung über Vorlaufsprung) und kann den Vorlauf nicht stellen. Den Offset empfiehlt er nur, die Ladezeiten legt eine fremde Automation fest.

## Ziele

1. **Ein Besitzer.** Die Integration setzt die Vorlauf-Stellgröße und den Sollwert des `water_heater` allein. Keine Automation schreibt daneben.
2. **Vorlaufsteuerung** als optionaler Subentry `flow`. Die Stellgröße ist je Installation wählbar:
   - (a) Offset auf die geräteeigene Heizkurve, hier `climate.heizung_leaving_water_offset`, Attribut `temperature`, Daikin witterungsgeführt
   - (b) fester Vorlauf-Sollwert, die Integration rechnet die Kurve selbst
3. **Warmwassersteuerung** als optionaler Subentry `hot_water`: Logik des Blueprints `domestic_hotwater_controller`, dazu Legionellenschutz und Zirkulationspumpe.
4. **Ladeplan als bekannte Störung im MPC.** Ladungen werden vorhergesagt. Räume, die die Pause nicht überstehen, heizen dosiert vor, und der Vorlauf steigt vorher und während der Erholung danach.
5. **Schreibbudget** für die Cloud-API des Wärmeerzeugers (Onecta ca. 200 Aufrufe am Tag), gemeinsam für beide Subentries.
6. **Hardware-neutral.** Wärmeerzeuger, `water_heater`, Speicher-, Außen-, Netz- und Batteriefühler sowie Pumpenschalter sind über Entities wählbar. Fehlt ein optionaler Eingang, entfällt das zugehörige Gate.
7. **Helfer, Templates und Automationen des Nutzers wandern in die Integration.** Entity-IDs bleiben, wo Verlauf oder Statistik daran hängt.

## Nicht-Ziele

- Kein automatisches Ein- und Ausschalten des Heizbetriebs der WP. Die Heizsaison wird weiter nur empfohlen (Entscheidung Testphase bleibt).
- Kein Verschieben von Ladungen auf günstige Zeiten in diesem Schritt (Phase 5, eigenes Design).
- Keine Benachrichtigungen aus der Integration. Meldungen bleiben kleine Automationen, die `binary_sensor`- bzw. `event`-Entities lesen (Nutzer).
- Keine Rückwärtskompatibilität (Projektregel). Migration nur für diese Installation, und zwar per Hand mit Helfer-Löschung und Umbenennung, wie beim Hub-Umbau.

## Anti-Ziele (wäre Fehlschlag, auch wenn es technisch läuft)

- Zwei Schreiber auf derselben Stellgröße, zum Beispiel ein Rest des Blueprints und die Integration.
- Das Tageslimit der Cloud wird erschöpft, und Pflichtschreibvorgänge (Rückfall, Zwangsladung, Legionellenschutz) gehen dadurch verloren.
- Offset-Takten: ständige Änderungen schneller als die Messung (Polling 10–15 min).
- Vorheizen in allen Räumen „auf Verdacht": Mehrverbrauch ohne Nutzen.
- Statistik-Verlauf der Heizkurven-Sensoren reißt ab, weil sich die Entity-ID ändert.

## Gemessene Grenzen (05.11.)

- Offset: `climate.heizung_leaving_water_offset` (`daikin_onecta`), −10…+10, Schritt 1. Ist-Vorlauf steht im Attribut `current_temperature`.
- Warmwasser: `water_heater.heizung`, 30…60 °C, Schritt 1, Modi `off`, `heat_pump`, `performance`.
- Onecta-Limit: `sensor.heizung_ratelimit_remaining_day` steht um 00:00 UTC auf 199. Verbrauch 136–142 am Tag, davon ca. 130 Polling (06–23 Uhr alle 10 min, nachts alle 15 min). Am Tagesende bleiben 57–63. Jeder Schreibvorgang kostet einen Aufruf.
- Vorlauf-Messwert kommt nur alle 10–15 min, Lücken einzelner Polls kommen vor.

## Aufbau

```
Hub (Entry)
├── room × n          (wie heute)
├── flow     ≤ 1      neu: Gerät „Vorlauf"
└── hot_water ≤ 1     neu: Gerät „Warmwasser"
```

Der Hub verbindet die Teile:
- Er sammelt die Raumwerte (`lead_min_flow`, `charge_min_flow` je Raum).
- Er reicht den Ladeplan von `hot_water` an die Räume weiter.
- Er hält das Schreibbudget.

Ohne `flow` und `hot_water` verhält sich die Integration wie heute.

`flow` und `hot_water` sind Subentry-Typen mit je eigenem Config- und Reconfigure-Flow. Ein zweiter Subentry desselben Typs wird abgelehnt.

## Hub-Erweiterungen

### Schreibbudget **(Claude)**

`WriteBudget` im Hub, von `flow` und `hot_water` genutzt. Konfiguration im Hub:

| Feld | Bedeutung | Start |
|---|---|---|
| `rate_limit_remaining_entity` | optional, verbleibende Aufrufe heute | `sensor.heizung_ratelimit_remaining_day` |
| `rate_limit_reserve` | darunter nur Pflichtschreibvorgänge | 20 |
| `max_writes_per_day` | gilt ohne Entity bzw. zusätzlich | 40 |

Regeln:
- Jeder Schreibvorgang hat eine Priorität: `required` (Rückfall-Offset, Zwangsladung, Legionellenschutz, Boost-Ende) oder `normal` (Offset-Feinanpassung, Moduswechsel Warmwasser).
- `normal` wird nur geschrieben, wenn noch Aufrufe über der Reserve übrig sind und das Tagesmaximum nicht erreicht ist. Sonst wird verworfen, ein Zähler steigt, und der Wert wird beim nächsten Durchlauf neu bewertet (keine Warteschlange).
- `required` wird immer versucht.
- Zähler der eigenen Schreibvorgänge im Store, Tageswechsel um 00:00 UTC (Onecta-Reset).
- Sensor `schreibbudget` am Hub: Zustand = verbleibende Aufrufe, Attribute = eigene Schreibvorgänge heute und verworfene.

### Wärmeerzeuger erreichbar

`binary_sensor.waermeerzeuger_erreichbar` am Hub ist aus, wenn der Vorlauf-Messwert länger als `max_flow_gap` (Start 30 min) fehlt. Das ersetzt die Logik von `sensor.vorlauf_regler` (Attribut `quelle`) für die Meldung. Die Automation „Daikin nicht erreichbar" liest künftig diesen Sensor. Das Lernen pausiert weiter nach 16 min ohne Wert (unverändert).

### Heizsaison

Bisher Helfer und zwei Automationen, künftig am Hub:

| Neu (Hub) | Ersetzt |
|---|---|
| `sensor.heizanforderung_leitraume_24h` (Stunden in 24 h, intern aus Ringpuffer 5-min-Raster, gespeichert) | gleichnamiger `history_stats`-Helfer |
| `number.heizsaison_einschaltschwelle` (6 h), `number.heizsaison_halteschwelle` (2 h) | gleichnamige `input_number` |
| `sensor.heizsaison_letzte_anforderung` (Zeitstempel, gespeichert) | `input_datetime.heizsaison_letzte_anforderung` + Automation „Anforderung stempeln" |
| `binary_sensor.heizsaison_einschalten_empfohlen` (24-h-Wert > Einschaltschwelle und Heizbetrieb aus) | Bedingung in „Leitraumkriterium" |
| `binary_sensor.heizsaison_ausschalten_empfohlen` (48 h nie über Halteschwelle und Heizbetrieb an) | ebenso |

Die Automation „Heizsaison Leitraumkriterium" wird zur reinen Meldung: Trigger auf die beiden `binary_sensor`s, dazu die tägliche Prüfung um 17:00. Die Dauer von 48 h wird konfigurierbar (`number`).

## Subentry `flow` (Vorlaufsteuerung)

### Konfiguration

| Feld | Pflicht | Start (hier) |
|---|---|---|
| `actuator_mode` | ja | `offset` (alternativ `setpoint`) |
| `actuator_entity` (`climate` → `set_temperature`, `number` → `set_value`) | ja | `climate.heizung_leaving_water_offset` |
| `actuator_min` / `_max` / `_step` | ja, Vorbelegung aus Entity-Attributen | −10 / +10 / 1 |
| `fallback_value` (Stellgröße bei Ausfall) | ja | 0 |

Den Vorlauf-Messwert und den Außenwert nimmt `flow` vom Hub, keine eigenen Felder.

### Entities (Gerät „Vorlauf")

Die Kurve war bisher in `input_number` und Templates, jetzt `number` am Gerät. Die Entity-IDs bleiben (siehe Migration).

| Entity | Bedeutung | Start |
|---|---|---|
| `number.heizkurve_aussentemperatur_kalt` / `_warm` | Kurvenpunkte außen | −13 / 18 |
| `number.heizkurve_vorlauftemperatur_kalt` / `_warm` | Kurvenpunkte Vorlauf | 54 / 29 |
| `number.heizkurve_erhohung_um_0_grad`, `number.heizkurve_erhohung_steigung` | Dreieck um 0 °C (Daikin [4.A]) | 2 / 4 |
| `number.vorlauf_marge` | Zuschlag auf Mindestvorlauf | 1 K **(Claude)** |
| `number.vorlauf_hysterese` | Mindestabweichung für neuen Schreibvorgang | 0,5 × Schritt **(Claude)** |
| `number.vorlauf_haltezeit` | Mindestabstand zwischen Schreibvorgängen | 30 min |
| `number.vorlauf_ladung_anhebung_max` | Deckel Anhebung um Ladung | +4 K |
| `number.vorlaufschwelle` | niedrigster sinnvoller Vorlauf (Heizanforderung) | 29 (= Kurve warm + Rückfall-Offset) **(Claude)** |
| `switch.vorlauf_schreiben` | aus = Schattenbetrieb (rechnet, schreibt nicht) | aus |
| `sensor.heizkurve_vorlauftemperatur` | Gerätekurve ohne Offset (Formel wie Template) | — |
| `sensor.heizkurve_offset` | Ist-Offset, aus Entity gelesen; bei Lücke gehalten | — |
| `sensor.heizkurve_vorlauftemperatur_effektiv` | Kurve + Ist-Offset | — |
| `sensor.vorlauf_soll` | gewünschter Vorlauf mit Attributen `source` (lead / charge / fallback), `room` | — |
| `sensor.vorlauf_stellgroesse_soll` | Stellgröße, die geschrieben würde bzw. wird | — |

**Vorlaufschwelle als feste Zahl (Claude):** Heute gilt `vorlaufschwelle_effektiv` = Kurve warm + Ist-Offset. Stellt der Regler den Offset selbst, ergäbe das eine Rückkopplung: Bedarf steigt, Offset steigt, Schwelle steigt, Heizanforderung fällt weg. Deshalb wird die Schwelle eine eigene Zahl. `sensor.vorlaufschwelle_effektiv` und `CONF_FLOW_THRESHOLD_ENTITY` entfallen, der Hub liest die `number`. Ohne `flow` gilt `flow_threshold_c` des Hubs wie heute.

**Reserve:** `CONF_FLOW_CURVE_ENTITY` entfällt mit `flow`. Der Hub rechnet die Reserve gegen `sensor.heizkurve_vorlauftemperatur_effektiv` intern. Ohne `flow` bleibt das Feld wie heute.

### Regelung

Läuft bei jeder Hub-Aktualisierung, höchstens alle 60 s.

1. `soll = max(lead_min_flow + marge, charge_flow)`. `charge_flow` siehe Ladeplan, ist 0 außerhalb des Fensters. Begrenzung nach unten auf die Vorlaufschwelle.
2. Modus `offset`: `stell = round_step(soll − kurve(außen))`, begrenzt auf min/max. Modus `setpoint`: `stell = round_step(soll)`.
3. Geschrieben wird, wenn alle Bedingungen gelten:
   - `switch.vorlauf_schreiben` an
   - |stell − ist| ≥ Hysterese
   - seit dem letzten eigenen Schreibvorgang ≥ Haltezeit
   - Budget `normal` frei

   Ausnahmen: Anhebung bei Ladungsbeginn und Rücknahme auf den Rückfallwert schreiben mit `required` und ohne Haltezeit.
4. **Ausfall:**
   - Außenwert fehlt oder `lead_min_flow` ist `None` (alle Räume ohne Rechnung): Rückfallwert schreiben (`required`), einmal, dann halten. Kein Schätzen, passend zur Regel „Außenwert-Ausfall nicht überbrücken".
   - Wärmeerzeuger nicht erreichbar: nichts schreiben (die Cloud nimmt es ohnehin nicht an). Nach Rückkehr gilt die normale Regel.
5. Heizbetrieb der WP aus (`heat_source_climate_entity` ≠ heat): nichts schreiben.
6. Fremde Änderung erkannt (Ist ≠ letzter eigener Wert, nicht aus unserem Kontext): keine Gegenwehr bis zur nächsten Haltezeit, dann übernimmt die Regel wieder. Der Sensor zeigt im Attribut `external_change` den Zeitpunkt. **(Claude)** Grund: Bedienung in der Daikin-App soll nicht sofort überschrieben werden, aber auch nicht dauerhaft gelten.

## Subentry `hot_water` (Warmwassersteuerung)

### Konfiguration: Eingänge

| Feld | Pflicht | hier |
|---|---|---|
| `water_heater_entity` | ja | `water_heater.heizung` |
| `tank_temperature_entity` | nein (sonst `current_temperature` des `water_heater`) | `sensor.heizung_domestichotwatertank_tank_temperature` |
| `grid_export_entity` (positiv = Einspeisung) | nein, ohne sie kein Überschussmodus | `sensor.netzeinspeisung_geglattet` |
| `battery_soc_entity` | nein | `sensor.solaredge_battery0_state_of_charge` |
| `battery_discharge_entity` | nein | `sensor.solaredge2mqtt_powerflow_battery_discharge` |
| `pv_forecast_entity` | nein (Legionellenschutz) | `sensor.solaredge2mqtt_forecast_energy_production_today` |
| `heater_reheat_hysteresis` | ja, Nachladen des Geräts unter Soll − x | 5 (Daikin [6-08]) |

Außenwert vom Hub (Daikin-Fühler, Netatmo fällt weg).

### Modus und Sollwert

Port der Blueprint-Logik, Verhalten zunächst gleich. Priorität von oben nach unten, erster Treffer gilt:

| Modus | Bedingung | Soll (Start) |
|---|---|---|
| Legionellenschutz | Zyklus läuft (siehe unten) | 60 |
| Boost | `switch.warmwasser_boost` an, max. 6 h, Neustart schaltet aus | 60 |
| Manuell | `select.warmwasser_modus` ≠ Automatik, fällt nach `manual_hold` (60 min) zurück | Soll des gewählten Modus |
| Überschuss | Einspeisung > untere Grenze stabil, Batterie-Gates, Außen außerhalb 12–24 °C (Sommer) oder Einspeisung > obere Grenze stabil, Speicher ≥ Gate. Release unter Release-Grenze | 57 |
| Ladefenster | 13–16 Uhr | 53, mindestens Speicher + Hysterese + 1 (Zwangsladung) |
| Nacht | 21–05 Uhr | 38 |
| Normal | sonst | 46 |

Weitere Regeln:
- Modus-Mindesthaltezeit 15 min, außer bei Legionellenschutz und Boost.
- Begrenzung auf `max_setpoint` (60).
- Geschrieben wird nur bei Änderung des Solls, mit Budget `normal`. Zwangsladung, Legionellenschutz und Boost-Ende laufen mit `required`.

Alle Grenzen und Sollwerte sind `number`- bzw. `time`-Entities am Gerät.

### Entities (Gerät „Warmwasser")

| Entity | ersetzt |
|---|---|
| `select.warmwasser_modus` (Automatik, Normal, Überschuss, Nacht, Aus) | `input_select.heizung_warmwasser_uberschuss` + `input_boolean.warmwasser_manual_override_blocked` + Automation „Manuelles Override" |
| `switch.warmwasser_boost` | `input_boolean.warmwasser_boost` |
| `sensor.warmwasser_aktiver_modus`, `sensor.warmwasser_soll` | — |
| `binary_sensor.warmwasser_ladung` | — (Erkennung, siehe unten) |
| `sensor.warmwasser_naechste_ladung` (Zeitstempel, Attribut `planned_charges`) | — |
| intern im Store: Einspeisung-oben-stabil, letzter Moduswechsel | `input_boolean.warmwasser_grid_upper_stable`, `input_datetime.warmwasser_modus_zuletzt_gewechselt` |

**Manuell (Claude):** Statt den Select zugleich als Ausgang und Eingang zu nutzen (heute: Blueprint schreibt, Override-Automation erkennt Handbedienung), hat der Select „Automatik" als Normalzustand. Jede andere Wahl gilt `manual_hold` lang, danach geht er zurück auf Automatik. Den aktiven Modus zeigt ein eigener Sensor.

### Legionellenschutz

Port von `legionella_protection_schedule`, Verhalten gleich:
- Wochentage und Startzeit (Di 13:00), Rückfallzeit 15:00
- PV-Prognose-Bedingung optional
- höchstens einmal am Tag
- Prüfung: Speicher ≥ 60 °C, Zirkulationsrücklauf ≥ 58 °C für 30 min, Zeitlimit 240 min
- Wiederholung alle 15 min innerhalb von 120 min, wenn Schreiben scheitert

Der Zyklus setzt intern Modus Legionellenschutz (statt Boost-Helfer) und fordert bei der Zirkulation Dauerlauf an.

Entities:
- `sensor.legionellenschutz_letzter_start` (ersetzt `input_datetime.legionellenschutz_letzter_start`)
- `binary_sensor.legionellenschutz_fehlgeschlagen`, hält bis zum nächsten erfolgreichen Zyklus
- `button.legionellenschutz_starten`

Die Meldung macht eine Automation.

### Zirkulation

Port von `domestic_hotwater_circulation_thermostatic`, eigener optionaler Abschnitt im Subentry, Verhalten gleich:
- Regeln 1–5 aus dem Blueprint
- Spitzenfenster, Start- und Stoppschwellen, Laufzeit- und Pausengrenzen
- Zapf-Erkennung
- Rückfall-Takt ohne Rücklauffühler
- Freigabe des Dauerlaufs bei 58 °C für 30 min

Eingänge: Pumpenschalter, Rücklauf, Austritt, Zapf-Sensoren, Anwesenheit, Tagmodus, externe Dauerlauf-Anforderung (optional, hier `input_boolean.warmwasser_zirkulation_dauerlauf`). Dazu kommt die interne Anforderung durch den Legionellenschutz. Die Zirkulation schreibt nicht in die Cloud und braucht kein Budget.

Entities: `switch.zirkulation_dauerlauf` (interne Anforderung, ersetzt den Helfer, wenn ihn sonst niemand nutzt), `sensor.zirkulation_grund` (warum an/aus).

### Ladeerkennung

- Ist `hot_water` konfiguriert, gilt eine Ladung als laufend, wenn der Speicher seit der letzten Messung um ≥ 0,5 K steigt **(Claude)**, oder die Vorlauf-Heuristik anschlägt (Vorlauf > Minimum der letzten Stunde + 5 K). Ende: beides 15 min nicht mehr erfüllt.
- Ohne `hot_water` bleibt die Vorlauf-Heuristik allein (heute).
- **Offen, Prüfpunkt Phase 2:** Hat Onecta ein Zustands-Attribut für „Warmwasserbetrieb aktiv" (Dreiwegeventil)? Falls ja, ersetzt es die Heuristik als Hauptsignal.

### Ladeplan

`planned_charges`: Liste `(start, dauer, art, sicher)` für die nächsten 6 h. Arten:

| Art | Auslöser | Vorhersage |
|---|---|---|
| `night_end` | Ende Nachtfenster: Soll springt 38 → 46 | Ladung, wenn Speicher-Prognose zum Zeitpunkt < Normal − Geräte-Hysterese |
| `charge_window` | Beginn Ladefenster | Ladung, wenn Prognose < Ladefenster-Soll (Zwangsladung) |
| `legionella` | geplanter Zyklus | immer, Dauer aus Ziel 60 °C |
| `reheat` | Abkühlung ohne Zapfung | Ladung, wenn Prognose < Soll − Geräte-Hysterese innerhalb 6 h. `sicher = false`, Zapfen beschleunigt |

Woraus die Vorhersage rechnet:
- **Speicher-Prognose:** Speicher jetzt − Abkühlrate × Zeit. Abkühlrate gelernt (Median der Stunden ohne Ladung und ohne Zapfung, Zapfung = Abfall > 1 K in 15 min) **(Claude)**. Start 0,5 K/h.
- **Dauer:** (Soll − Speicher-Prognose) / Laderate, plus 5 min Anlaufverzug (Daikin startet ca. 5 min nach Sollwechsel). Laderate gelernt aus erkannten Ladungen (gleitendes Mittel über 10). Start 0,25 K/min (05.11.: ca. 10 K in 40 min).
- Ladungen durch PV-Überschuss und nach dem Duschen sind nicht planbar. Sie wirken wie heute: erkannt, Lernen gesperrt, kein Vorheizen.

## Ladeplan im MPC

Der Hub gibt jedem Raum die nächste Ladung mit `sicher = true` als `ChargeWindow(start_ts, end_ts)`, wenn sie innerhalb von `preheat_window_max` (90 min) beginnt oder schon läuft. `reheat`-Einträge zählen erst, wenn sie innerhalb 30 min erwartet werden **(Claude)**: Ein unsicherer Plan soll nicht 90 min Vorheizen auslösen.

### Vorheizen je Raum

Neu im Controller: `_calculate_charge_preheat(mpc_input, estimate, window)`.

1. Simulation ab jetzt:
   - bis `start` mit der aktuellen Planöffnung
   - von `start` bis `end` ohne Heizleistung (Umlauf steht, Puffer ungenutzt)
   - Ergebnis: Raumtemperatur bei `end`
2. Liegt sie bei ≥ Soll − `charge_allowed_dip` (0,2 K), passiert nichts. Der Raum ist nicht gefährdet.
3. Sonst sucht der Controller den kleinsten Zuschlag `lift` (Schritt 0,05 K, Deckel `charge_preheat_max_lift` 0,5 K **(Claude)**). Bedingung: Mit dem Ziel `Soll + lift` bis `start`, simuliert mit dem normalen Planer, liegt der Raum bei `end` auf ≥ Soll − Dip.
4. `lift` gilt als Ziel-Zuschlag ab `start − Vorlaufzeit` bis `start`. Die Vorlaufzeit ist die simulierte Zeit, die der Raum bei voller Öffnung für `lift` braucht, plus 15 min, höchstens `preheat_window_max`. Vorher bleibt das Ziel normal.
5. Reicht der Deckel nicht, gilt `lift = Deckel`, und der Raum meldet `charge_at_risk` (Attribut). Den Rest muss der Vorlauf leisten.

Neue Raum-Attribute am Bedarfssensor: `charge_preheat_lift`, `charge_preheat_from`, `charge_at_risk`.

Der 30-min-Horizont des Planers bleibt. Der Zuschlag wirkt über das Ziel, der Planer bleibt unverändert.

### Vorlauf um die Ladung

Neu je Raum: `charge_min_flow_temperature_c`. Das ist der kleinste Vorlauf, mit dem der Raum (mit Vorheizen, Ventil bis 100 %) zwei Bedingungen erfüllt:
- bei `end` ≥ Soll − Dip
- 60 min nach `end` wieder auf Soll (`charge_recovery_s`)

Suche wie `_calculate_recovery_flow`, gleiche Spreizungsregel. Außerhalb des Fensters ist der Wert `None`.

Im Hub:

`charge_flow = min(max(charge_min_flow aller Räume), lead_min_flow + marge + vorlauf_ladung_anhebung_max)`

**Alle Räume** zählen, nicht nur Leiträume, mit Deckel (Nutzer). Fenster: ab Beginn des Vorheizens bis `end + charge_recovery_s`.

### Lernen

- Während der Ladung bleibt die heutige Sperre (Bedarf mit Heizvorlauf von vorher, Bilanz ohne Heizung, Heizkörper-Check startet neu).
- Vorheizen ist normales Heizen mit erhöhtem Ziel. Die Bilanz bucht die Leistung physikalisch richtig. Eine UA-Strecke endet trotzdem bei Vorheizbeginn **(Claude)**: Der gewollte Anstieg würde den Drift-Test (0,3 K) verletzen und die Strecke ohnehin verwerfen. Betroffen ist vor allem `night_end` um 05:00. Das in der Tagesauswertung beobachten.
- Die Kennlinien-Probe läuft weiter, der Abstand zum Ziel ist unabhängig vom Zuschlag.

## Migration (nur diese Installation, per Hand nach Deploy)

1. Backup.
2. Automationen ausschalten: Warmwasser Controller, Warmwasser Manuelles Override, Legionellenschutz Tag, Warmwasser Zirkulationspumpe, Heizsaison Anforderung stempeln.
3. Neustart und Subentries anlegen. `switch.vorlauf_schreiben` bleibt aus (Schatten).
4. Werte der alten Helfer in die neuen Entities übernehmen (Kurvenpunkte, Schwellen).
5. Alte Helfer und Templates löschen, danach die neuen Entities auf die alten IDs umbenennen:
   - `heizkurve_*`
   - `heizsaison_*`
   - `heizanforderung_leitraume_24h`
   - `sensor.heizkurve_vorlauftemperatur`, `_effektiv`, `sensor.heizkurve_offset`

   Statistik hängt an der Entity-ID und läuft damit weiter.
6. Wegfallende Helfer und Templates (nach Verwendungsprüfung mit `ha_search` auf Dashboards, Automationen, Karte):
   - `input_select.heizung_warmwasser_uberschuss`
   - `input_boolean.warmwasser_boost`, `_manual_override_blocked`, `_grid_upper_stable`
   - `input_datetime.warmwasser_modus_zuletzt_gewechselt`, `legionellenschutz_letzter_start`, `heizsaison_letzte_anforderung`
   - `sensor.vorlaufschwelle_effektiv`
   - `sensor.vorlauf_regler` (Template-Datei `templates/vorlauf_regler.yaml`), sofern kein Verbraucher mehr

   `input_boolean.warmwasser_zirkulation_dauerlauf` bleibt, falls Dashboard-Bedienung gewünscht, sonst ersetzt durch `switch.zirkulation_dauerlauf`.
7. Meldungs-Automationen anpassen bzw. neu anlegen:
   - Heizsaison: auf die `binary_sensor`s
   - Daikin nicht erreichbar: auf `binary_sensor.waermeerzeuger_erreichbar`
   - neu: Legionellenschutz fehlgeschlagen
8. 1–2 Tage Schatten, `vorlauf_soll` und `stellgroesse_soll` gegen Ist und Tagesauswertung prüfen, dann `switch.vorlauf_schreiben` an.
9. Blueprints im helper-collection-Repo bleiben für Fremdnutzer erhalten. README-Hinweis auf die Integration.

## Doku und Skill

- Skill `heating-controller`, Regel 7: „Offset/Heizkurve nur empfehlen" entfällt. Neu: Die Integration setzt die Stellgröße, wenn `flow` aktiv ist. Automatisches WP-Schalten bleibt verboten.
- `entscheidungen.md`: „Vorlaufsteuerung erst nach der Testphase" und „Offset nur als Empfehlung" werden ersetzt (05.11., Anlass WW-Ladung). Neu kommen Schreibbudget, Ladeplan, Vorheizen, Vorlaufschwelle als feste Zahl.
- `diagnose.md`: neue Entities, Fehlbilder (Budget erschöpft, Schatten an, fremde Offset-Änderung).
- `tagesauswertung.md`: Ladungen der Nacht und das Vorheizen bewerten, Prüfpunkt „Offset-Empfehlung" wird zu „Vorlauf-Regelung geprüft".
- `mpc-guide.md`, README, `card-design.md` (Karte zeigt Vorheizen?, später), Übersetzungen.
- Hausdoku `klima.md`: Warmwasser-Steuerung und Vorlauf-Stellgröße.

## Offen

- Onecta-Signal für „Warmwasserbetrieb aktiv" (Phase 2).
- Phase 5: Ladefenster verschieben (PV, Raumlast).
- Karte: Vorheizen und Ladeplan anzeigen? Erst nach Betrieb entscheiden.

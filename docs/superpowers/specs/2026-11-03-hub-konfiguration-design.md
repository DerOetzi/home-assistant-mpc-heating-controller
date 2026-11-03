# Hub-Konfiguration für heating_controller — Design

Stand 2026-11-03. Nutzer hat Rückfragen übersprungen („schreib das Design"); Entscheidungen unten, die nicht vom Nutzer stammen, sind als **(Claude)** markiert.

## Absicht und Warum

Heute ist jeder Raum ein eigener Config-Entry (`HeatingControllerConfigFlow`, VERSION 7). Etwa 20 Einstellungen gelten fachlich fürs ganze Haus, stehen aber zehnmal in der Konfiguration: Außentemperatur, Wärmepumpe, Vorlauf, Vorlaufschwelle, Lernfenster, Regelparameter, Auslegung. Folgen:

- Gefahr, Räume unterschiedlich zu konfigurieren — obwohl im Projekt gilt: Änderungen immer alle Räume gleichzeitig.
- Hausweite Kennzahlen (Haltevorlauf und Mindestvorlauf der Leiträume, Heizanforderung, Vorlauf-Reserve) baut der Nutzer heute als HA-Helfer (min_max, Templates). Andere Nutzer müssten sie nachbauen.
- Geplante Erweiterung „Gewinnprofil je Stunde" (Plan 2026-11-02, Task 8, Nutzer 03.11.) bringt hausweite Eingänge (PV-Leistung als Sonnen-Maß). Ohne Hub kämen sie zehnmal dazu.

Ziel: Ein Hub hält alles Hausweite einmal; Räume enthalten nur Raumdaten; der Hub liefert die hausweiten Kennzahlen als eigene Entities.

## Ziele

1. Ein Hub-Config-Entry je Installation (Haus). Räume sind **Config-Subentries** des Hubs (HA-Mechanismus `ConfigSubentryFlow`, wie in der Integration meals_and_groceries des Nutzers).
2. Hausweite Einstellungen nur im Hub, Raumeinstellungen nur im Raum (Aufteilung siehe Tabelle).
3. Hub-Entities ersetzen die Helfer des Nutzers für Leiträume, Heizanforderung und Reserve.
4. Migration der 10 bestehenden Räume ohne Verlust: gleiche Entity-IDs, gleiche Unique-IDs, Verlauf, gelernte Faktoren, Ventilkennlinien, Dashboards, Karte, Automationen funktionieren weiter.
5. Grundlage für Task 8 (Gewinnprofil): neue hausweite Eingänge kommen nur in den Hub.

## Nicht-Ziele

- Keine Änderung an Regelung, Modell oder Lernen. Verhalten nach der Migration identisch.
- Kein Heizkurven-Modell im Hub (Daikin-Kurve `sensor.heizkurve_*` bleibt Nutzer-Template, herstellerspezifisch).
- Keine Heizsaison-Logik im Hub (Automationen `heizsaison_*` bleiben beim Nutzer, lesen künftig Hub-Entities).
- Kein automatisches Setzen von Offset/Heizkurve.
- Keine Rückwärtskompatibilität für alte Konfigurationswege (Projektregel); Migration nur für diese Installation, Code darf danach entfernt werden.

## Anti-Ziele (wäre Fehlschlag, auch wenn es technisch läuft)

- Entity-IDs oder Unique-IDs der Raum-Entities ändern sich → Verlauf, Statistiken, Dashboards, Karte, Automationen reißen ab.
- Gelernte Werte (UA-, Kapazitätsfaktor, Ventilkennlinien, Flow-Gate-Zustand) gehen verloren oder werden beim Umzug gelöscht (`async_remove_entry` löscht heute die Stores!).
- Raumspezifische Overrides für hausweite Werte „für alle Fälle" — widerspricht der Regel „alle Räume gleichzeitig".
- Nicht-Admin-Bedienung (Modus, Komfort/Eco, Entsperren) wird schlechter.

## Aufteilung Hub / Raum

| Einstellung (heute `CONF_*`) | Hub | Raum |
|---|---|---|
| `outdoor_temperature_entity`, `heat_source_climate_entity`, `flow_temperature_entity` | x | |
| `flow_threshold_c`, `flow_threshold_entity` | x | |
| `pv_boost_entity` | x | |
| `pv_boost_enabled`, `pv_boost_temperature_offset_c` (heute je Raum verschieden: an in Wohnz., Essz., Katharina, Rebekka, Schlafz.; Offset 2,0 bzw. 1,0 K) | | x |
| `learning_window_entity` — **entfällt** (Nutzer 03.11.): kein Lernfenster-Entity mehr. Bis Task 8 lernt die UA-Bilanz im heutigen Sonnen-Fenster (Sonne ≥ 2 h unter bis Sonnenaufgang, `is_sun_condition_met`), ohne `input_boolean.nachtmodus`; ab Task 8 lernt das Gewinnprofil rund um die Uhr | – | – |
| `learning_enabled_entity` → wird **„Lernen sperren, solange aktiv"** (optional, z. B. Zusatzheizung im Raum; Schlafzimmer: `binary_sensor.klimaanlage_schlafzimmer_heizt`) | | x |
| `comfort_condition_entities` (gemeinsame Bedingungen, heute `input_boolean.heizung_komfortmodus`) | x | Checkbox „Hausweite Komfortbedingungen anwenden" (heute aus nur bei Flur EG) |
| `design_outdoor_temperature_c`, `design_temperature_system` | x | |
| `mpc_demand_hysteresis_pct`, `mpc_hold_time_s`, `mpc_hold_override_demand_pct`, `mpc_max_demand_step_pct` | x | |
| `flow_gate_close_surplus_c`, `flow_gate_open_surplus_c`, `flow_gate_hold_time_s` | x | |
| `max_sensor_age_s`, `stationary_range_c`, `target_band_c`, `trv_setpoint_mode` | x | |
| `frost_protection_temperature_c` | x | |
| Kurven-Entity für Reserve (neu, optional) | x | |
| Leitraum Heizgrenze (neu, Checkbox) | | x |
| Leitraum Vorlauf (neu, Checkbox) | | x |
| `room_name`, `trvs` (TRV-Bindung, Heizkörperdaten) | | x |
| `room_sensor_entity`, `window_contact_entities`, `room_comfort_condition_entities` | | x |
| `design_indoor_temperature_c`, `room_heat_load_w`, `room_area_m2`, `construction` | | x |
| `boost_enabled`, `boost_temperature_offset_c` | | x |

**Lernfenster und Lernen-Sperre (Nutzer, Freigabe 03.11.):** Kein zentrales Lernen-erlaubt und keine Tag/Nacht-Entity im Hub. Folge bis Task 8: Bilanzstrecken können schon ab Sonne 2 h unter beginnen (im November ~18:45) statt ab Nachtmodus (~22:00), also mit Abendnutzung (Kochen, Personen) im Fenster — Drift-Prüfung und Streckenstart „ruhige Stunde" fangen das teilweise ab; im Hub-Plan als bewusste Übergangsfolge vermerken und in der Tagesauswertung beobachten. Der hausweite Helfer `binary_sensor.lerneingaben_gemessen` (an, wenn Außenwert aus Daikin und Vorlauf gemessen, nicht aus Ersatzquelle; `delay_off` 16 min) entfällt. Ersatz: Der Hub bekommt Außentemperatur und Vorlauf **direkt** (Daikin-Sensoren, keine Template-Ersatzquelle); fehlt einer der Werte (unavailable/unknown), pausiert das Lernen aller Räume. Für den Außenwert ist das schon so (Entscheidung „Außenwert-Ausfall nicht überbrücken"); für den Vorlauf muss der Builder prüfen und ggf. ergänzen, dass fehlender Vorlauf das Lernen pausiert und kurze Poll-Lücken (Daikin pollt ~10 min) nicht jedes Mal eine Strecke abbrechen — Toleranz wie heute 16 min. Raum-Feld „Lernen sperren, solange aktiv" bleibt nur für raumeigene Gründe (Schlafzimmer: Klimaanlage heizt); der Helfer `binary_sensor.lernen_schlafzimmer_erlaubt` entfällt, das Feld zeigt direkt auf `binary_sensor.klimaanlage_schlafzimmer_heizt` (Logik umgekehrt: an = sperren).

Aufteilung geprüft gegen die echten 10 Einträge (`.storage/core.config_entries`, nur gelesen): alle Hub-Werte sind in allen Räumen gleich, außer den drei oben als Raum-Wert markierten (PV-Boost an/Offset, Lernen-Sperre beim Schlafzimmer, Komfortbedingungen bei Flur EG).
**(Claude)** Sonst keine Raum-Overrides für Hub-Werte (Zielband, TRV-Modus, Regelparameter heute überall gleich). Wird je Raum etwas nötig, wird es dann gezielt ergänzt.

## Hub-Entities

Gehören zu einem Hub-Gerät „Heizung" (eigener Device-Eintrag, nicht einem Raum zugeordnet).

| Hub-Entity | Ersetzt Helfer | Definition |
|---|---|---|
| Haltevorlauf Leiträume | `sensor.haltevorlauf_leitraume` (min_max max) | Maximum der Raum-Sensoren `*_haltevorlauftemperatur_mit_gate` über alle Räume mit Checkbox „Leitraum Heizgrenze"; Attribute: Raum mit dem Maximum, Liste der Leiträume |
| Mindestvorlauf Leiträume | `sensor.mindestvorlauf_leitraume` (min_max max) | Maximum der Raum-Sensoren `*_mindestvorlauftemperatur` über alle Räume mit Checkbox „Leitraum Vorlauf"; Attribute wie oben |
| Heizanforderung Leiträume (binary) | `binary_sensor.heizanforderung_leitraume` (Template) | `an`, wenn Haltevorlauf Leiträume ≥ Vorlaufschwelle (Hub-Einstellung `flow_threshold_entity`, sonst `flow_threshold_c`); nicht verfügbar, solange einer der beiden Werte fehlt — exakt das heutige Template (`states('sensor.haltevorlauf_leitraume') >= states('sensor.vorlaufschwelle_effektiv')`, availability auf beide) |
| Vorlauf-Reserve Halten / Mindest | `sensor.vorlauf_reserve_haltevorlauf`, `_mindestvorlauf` (Templates) | Kurven-Entity minus Haltevorlauf bzw. Mindestvorlauf Leiträume; nur angelegt, wenn Kurven-Entity konfiguriert |

Bleibt beim Nutzer: `sensor.heizanforderung_leitraume_24h` (history_stats) — wird auf die Hub-Binary umgestellt. `sensor.vorlaufschwelle_effektiv` bleibt Nutzer-Template und wird im Hub als Vorlaufschwellen-Entity gewählt. `sensor.aussentemperatur_regler` und `sensor.vorlauf_regler` braucht der Regler nicht mehr (Hub liest die Daikin-Sensoren direkt, siehe Lernen-Sperre); ob sie für Dashboards bleiben, entscheidet der Nutzer nach Deploy. `sensor.aussen_abweichung_daikin_netatmo` bleibt (reine Beobachtung).

**Entity-IDs der Hub-Entities:** Nach dem Deploy werden die alten Helfer entfernt und die Hub-Entities auf die alten Entity-IDs umbenannt (Dashboards, `klima-diagnose`, Automationen `heizsaison_*`, Tagesauswertung zeigen weiter auf dieselben IDs). Schritt im Deploy, nicht im Code.

## Laufzeit

- Hub-Entry lädt zuerst, hält die hausweiten Eingänge und baut die Hub-Entities.
- Je Raum-Subentry ein `HeatingRoomCoordinator` wie heute; seine Konfiguration ist Hub-Werte + Raum-Werte zusammengeführt. Die Regelung bekommt dieselben Werte wie heute.
- Hub-Entities aktualisieren sich, wenn sich ein Raum-Sensor ändert (Listener auf die Raum-Coordinators, kein eigener Poll nötig).
- Ändert der Nutzer eine Hub-Einstellung, werden alle Räume neu geladen (ein Reload des Hub-Entry).
- **(Claude)** Raum hinzufügen, ändern oder löschen nutzt den HA-Standard: Update-Listener lädt den ganzen Hub neu, also alle Räume (Startgnadenfrist, TRV-Schreibvorgänge, Ventilkurven-Flush, laufende Bilanzstrecken aller Räume beginnen neu). Raumänderungen sind selten; einzelnes Neuladen nur des betroffenen Raums wäre eine eigene Laufzeitstruktur und kommt erst, wenn das im Betrieb stört.
- Dienste `heating_controller.unblock` und `recalibrate` zielen weiter auf Entities/Geräte eines Raums; die Auflösung Entity → Raum muss über Subentry statt Config-Entry gehen.

## Konfigurationsablauf (UI)

- **Neue Installation:** „Integration hinzufügen" legt den Hub an (Schritte: Wärmepumpe/Außen/Vorlauf, Lernen, Regelparameter, Auslegung). Danach Räume über „Raum hinzufügen" (Subentry-Flow: Name, TRVs, Heizkörper, Raumsensor/Fenster, Auslegung des Raums, Boost).
- **Leiträume** (Nutzer): je Raum zwei Checkboxen im Raum-Subentry, „Leitraum Heizgrenze" und „Leitraum Vorlauf". Der Hub aggregiert über alle Räume mit gesetzter Checkbox. Vorteil: kein Verweis vom Hub auf Raum-IDs, Leitraum wird beim Anlegen des Raums festgelegt, gelöschter Raum fällt automatisch raus. Welche Räume Leiträume sind, zeigen die Hub-Entities als Attribut (Liste der Räume).
- **Raum bearbeiten:** Subentry-Reconfigure (ersetzt heutigen Options-Flow pro Raum).
- Nur ein Hub je Installation (`single_config_entry` bzw. Unique-ID des Hubs).
- Anlegen/Ändern bleibt Admin-Sache (HA-Standard für Config-Flows); Bedien-Entities der Räume bleiben für alle nutzbar.

## Migration (einmalig, diese Installation)

1. Config-Entry-VERSION erhöhen. Beim ersten Start mit neuer Version: aus den bestehenden Raum-Entries einen Hub-Entry erzeugen. Hub-Werte aus den Raum-Entries übernehmen (bei dieser Installation identisch, geprüft); Raum-Werte (PV-Boost, Lernen-Sperre beim Schlafzimmer = `binary_sensor.klimaanlage_schlafzimmer_heizt`, Komfort-Checkbox aus beim Flur EG) in den jeweiligen Subentry; `learning_enabled_entity` der übrigen Räume entfällt. Hub-Außen- und Vorlauf-Entity auf die Daikin-Sensoren direkt setzen (`sensor.heizung_climatecontrol_outdoor_temperature`, `sensor.heizung_climatecontrol_leaving_water_temperature`) statt `sensor.aussentemperatur_regler`/`sensor.vorlauf_regler`. Helfer `lerneingaben_gemessen`, `lernen_schlafzimmer_erlaubt` nach Deploy mit Nutzer-Okay entfernen (vorher Verwendungen prüfen). Sind Hub-Werte doch verschieden: Werte des ersten Raums, Abweichungen im Log warnen.
2. Jeder Raum wird Subentry mit **`subentry_id` = alte `entry_id`**. Dadurch bleiben Unique-IDs (`{entry_id}_{suffix}` in `entity.py`), Geräte-Identifier (`(DOMAIN, entry_id)`) und Store-Schlüssel (`room_name` + `entry_id` in `store.py`) unverändert. HA 2026.10 erlaubt eine vorgegebene `subentry_id` (`ConfigSubentry(subentry_id=...)`, geprüft). Die Karte findet Entities über `device_id` — Gerät umhängen, nicht neu anlegen.
3. Entity- und Device-Registry: Einträge der Räume auf Hub-Entry + jeweiligen Subentry umhängen (`config_entry_id`, `config_subentry_id`), Entity-IDs, Namen, Areas, Nutzer-Overrides bleiben.
4. Alte Raum-Entries entfernen, **ohne** dass `async_remove_entry` die Stores löscht (Migration markiert sie oder entfernt die Stores-Löschung für diesen Pfad).
5. Leitraum-Checkboxen aus den heutigen min_max-Helfern setzen: Heizgrenze = Wohnzimmer, Katharina, Rebekka, Schlafzimmer; Vorlauf = Wohnzimmer, Esszimmer, Katharina, Schlafzimmer, Dani. Alle übrigen Räume: beide aus.
6. Nach Deploy (Schritt im Deploy, per MCP mit Nutzer-Okay): alte Helfer entfernen, Hub-Entities auf alte IDs umbenennen, history_stats-Helfer auf Hub-Binary umstellen, Dashboards/Automationen prüfen.

Migrationscode trägt Kommentar „drop after season 2026/27".

## Rahmenbedingungen

- HA 2026.10 (Subentries verfügbar).
- Tests: `~/.venvs/heating-controller`, ganze Suite + ruff (`/home/oetzi/Projekte/home-assistant-config/.ruff.toml`) grün. Migration mit Tests aus echten Entry-Daten (VERSION 7) absichern, inkl. „Stores bleiben", „Unique-IDs gleich", „Registry umgehängt". Echte Daten: die 10 `heating_controller`-Einträge (alle VERSION 7) aus `/home/oetzi/Projekte/home-assistant-config/.storage/core.config_entries` **nur lesen** (nie schreiben), als Fixture übernehmen; Entity-IDs der TRVs/Sensoren dürfen bleiben, nichts Sensibles darin.
- `.py`-Änderungen brauchen HA-Neustart; Deploy nur nach Nutzer-Okay, vorher Backup (Migration ändert Registry, schwer rückgängig).
- Kein `git add`/`commit` durch Agenten; Commit auf Host bzw. nach Nutzer-Freigabe.
- Doku nachziehen: README (Einrichtung), `docs/mpc-guide.md`, `strings.json`, `translations/{en,de}.json`, Skill-Dateien (`diagnose.md`, `tagesauswertung.md` falls Entity-Namen betroffen).
- Repo liegt auf CIFS-Mount: kein `sed -i`, Greps nur gezielt.

## Dem Builder überlassen

- Aufteilung der Hub-Flow-Schritte und Texte im Detail.
- Wie Hub-Werte an die Raum-Coordinators gereicht werden (Zusammenführen beim Setup oder Zugriff auf Hub-Objekt), solange Raumverhalten identisch bleibt.
- Interne Struktur der Hub-Entities (eigener Coordinator oder Listener-Klasse).
- Reihenfolge der Implementierung in Tasks.

## Danach

Plan Task 8 (Gewinnprofil je Stunde + Sonnenterm über PV, eigenes Design) setzt auf den Hub: PV-Leistungs-Entity wird Hub-Einstellung.

# Hub-Konfiguration — Umsetzungsplan

Spec: `docs/superpowers/specs/2026-11-03-hub-konfiguration-design.md` (Commit d667716).
Umsetzung nativ in einer Sitzung, kein Agent. Tests nach jedem Task: `PYTHONDONTWRITEBYTECODE=1 ~/.venvs/heating-controller/bin/python -m pytest -q -p no:cacheprovider`, am Ende ruff.

## Festlegungen (Builder)

- Hub-Entry VERSION 8, `single_config_entry` im Manifest, Titel „Heizung". Räume = Subentries vom Typ `room`.
- **Migration in `async_setup` der Integration** (läuft vor jedem Entry-Setup, HA holt die Entry-Liste erst danach): erster v7-Raum-Entry wird in place zum Hub (gleiche `entry_id`, Daten = Hub-Werte, VERSION 8); jeder v7-Raum (auch der erste) wird Subentry mit `subentry_id` = alte `entry_id`; Entities/Geräte der übrigen Räume per Registry auf Hub + Subentry umgehängt, danach alte Entries entfernt. `async_remove_entry` löscht Stores nur bei VERSION ≥ 8 → Umzug lässt Stores stehen.
- Migrationsschritte v1→v7 entfallen (Projektregel: keine Rückwärtskompatibilität; einzige Installation steht auf v7). `async_migrate_entry` lehnt alles < 8 ab, was `async_setup` nicht umgezogen hat.
- `HeatingRoomCoordinator(hass, hub_entry, room_id, data, trvs)`: `data` = Hub-Daten + Raum-Daten zusammengeführt; `room_id` = Subentry-ID (Stores, Unique-IDs, Gerät). Regelung bekommt dieselben Werte wie heute.
- Hausweite Komfortbedingungen nur, wenn Raum-Checkbox `use_house_comfort_conditions` an.
- Lernfenster-Entity entfällt; Learner bleibt im Sonnenfenster (Default `learning_window_active=True`, `is_sun_condition_met`). Übergangsfolge (Bilanz ab Sonne 2 h unter statt ab Nachtmodus) in Tagesauswertung beobachten.
- `learning_enabled_entity` → Raum-Feld `learning_block_entity` (an = sperren). Lernen pausiert zusätzlich, wenn Vorlauf > 16 min keinen gültigen Wert hatte (Daikin pollt ~10 min). Außenwert-Ausfall pausiert wie bisher über `outdoor_temperature_missing`.
- `flow_threshold_c` als Hub-Zahl (Default 30), gilt ohne bzw. bei leerer Schwellen-Entity für die Regelung; Heizanforderung wird bei konfigurierter, aber leerer Entity unavailable (wie Template).
- Raum mit fehlerhafter Konfiguration (TRV nicht auflösbar, Pflichtwert fehlt) wird geloggt und übersprungen, übrige Räume laufen.
- Raum-Subentry gelöscht → Stores dieses Raums beim Reload löschen; Hub gelöscht → Stores aller Räume löschen.
- Hub-Gerät Identifier `(DOMAIN, f"{hub_entry_id}_hub")`, Unique-IDs `f"{hub_entry_id}_hub_{suffix}"`. Hub-Werte gerundet auf 0,1 (wie min_max `round_digits: 1`); Reserve unavailable, wenn Kurve keine Zahl oder Leitwert ≤ 0 (wie Template).

## Tasks

1. **const**: `SUBENTRY_TYPE_ROOM`, `CONF_LEARNING_BLOCK_ENTITY`, `CONF_USE_HOUSE_COMFORT_CONDITIONS`, `CONF_LEAD_HEATING_LIMIT`, `CONF_LEAD_FLOW`, `CONF_FLOW_CURVE_ENTITY`, Listen `HUB_KEYS`/`ROOM_KEYS`; `CONF_LEARNING_WINDOW_ENTITY`, `CONF_LEARNING_ENABLED_ENTITY` nur noch als `LEGACY_*` für Migration; `LEGACY_TRV_*` raus.
2. **Coordinator**: neue Signatur, Lernfenster raus, Lernen-Sperre + Vorlauf-Lücke → `set_learning_paused`, Schwelle aus `CONF_FLOW_THRESHOLD`. Tests: Konstruktor-Aufrufe anpassen, neue Tests Sperre/Vorlauflücke.
3. **Setup/Laufzeit** (`__init__.py`, `entity.py`, Plattformen): Coordinator je Subentry, `entry.runtime_data`, Plattformen mit `config_subentry_id`, Update-Listener → Reload, Dienste über `config_subentry_id`, Store-Aufräumen.
4. **Hub-Entities** (`hub.py`): Aggregator hört auf Raum-Coordinators + Schwellen-/Kurven-Entity; Sensoren Haltevorlauf/Mindestvorlauf Leiträume (Attribute `room`, `rooms`), Binary Heizanforderung, Reserve Halten/Mindest (nur mit Kurven-Entity).
5. **Config-Flow**: Hub-Flow (Quellen, Regelung) + Reconfigure; Raum-Subentry-Flow (Name/TRV-Zahl, TRVs, Signale, Einstellungen inkl. Leitraum-Checkboxen, Auslegung) + Reconfigure. Options-Flow entfällt.
6. **Migration** v7 → Hub in `async_setup`, Tests mit Fixture aus den echten 10 Einträgen (`tests/fixtures/config_entries_v7.json`): Hub-Werte, Raumwerte, Leitraum-Checkboxen, Schlafzimmer-Sperre, Flur-EG-Komfort, `subentry_id` = alte ID, Unique-IDs/Geräte umgehängt, Stores bleiben.
7. **Doku/Übersetzungen**: `strings.json`, `translations/{en,de}.json`, README, `docs/mpc-guide.md`, Skill-Referenzen (`diagnose.md`, `tagesauswertung.md`, `entscheidungen.md`), ruff.

## Deploy (nach Nutzer-Okay, nicht Teil der Umsetzung)

Backup → Neustart → Hub + 10 Räume prüfen (Entity-IDs, Lernstatus, Karte) → alte Helfer (`haltevorlauf_leitraume`, `mindestvorlauf_leitraume`, `heizanforderung_leitraume`, `vorlauf_reserve_*`, `lerneingaben_gemessen`, `lernen_schlafzimmer_erlaubt`) nach Verwendungsprüfung entfernen → Hub-Entities auf alte IDs umbenennen → history_stats prüfen (zeigt auf alte ID, passt nach Umbenennung). Kurven-Entity `sensor.heizkurve_vorlauftemperatur_effektiv` setzt die Migration selbst (Hausdaten erkannt an `sensor.aussentemperatur_regler`), ebenso Daikin-Außen/-Vorlauf, Lernsperre Schlafzimmer und Leitraum-Checkboxen.

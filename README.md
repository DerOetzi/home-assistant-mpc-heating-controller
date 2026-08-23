# Heating Controller

[![Validate](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/actions/workflows/validate.yml/badge.svg)](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/v/release/DerOetzi/home-assistant-mpc-heating-controller)](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration for **model-predictive-control (MPC) room heating** —
one config entry per room, combining a self-calibrating grey-box thermal model with
rate-limited demand control and a purpose-built Lovelace card to operate it.

## Features

- **Grey-box thermal model per room** — heat-loss (`ua_factor`) and thermal-capacity
  (`capacity_factor`) coefficients, learned online from each room's own temperature response
  and continuously self-calibrated while the room is actively heating. See
  [`docs/mpc-guide.md`](docs/mpc-guide.md) for how modes/setpoints combine, what the
  learning-status sensor means, and how to size a room correctly.
- **Per-TRV emitter modelling** — each thermostatic radiator valve is configured with its
  emitter type (panel radiator or towel radiator), panel radiator type (10/11/21/22/33) and
  dimensions, or a fixed nominal power for towel radiators — used to translate MPC demand
  into an actual valve target temperature.
- **Comfort / eco / boost / frost-protection mode state machine** — a `select` entity per
  room drives the active mode; `comfort`/`eco` setpoints are independently adjustable
  `number` entities, boost applies a temporary offset on top of comfort, and frost
  protection is the always-available floor.
- **Automatic vs. manually blocked operation** — a `binary_sensor` tracks whether the room is
  following its normal comfort/eco schedule or has been manually overridden; a dedicated
  `button` (and the `heating_controller.unblock` service, targetable at multiple rooms at
  once) clears the override and resumes automatic mode.
- **Rate-limited demand control** — hysteresis, a configurable hold time, and a maximum
  demand step per cycle keep the requested heating power from chattering.
- **Window-contact and room-specific comfort conditions** — open windows suspend demand;
  optional comfort-condition entities (e.g. a desk-plug "home office" signal, a guest
  switch) gate comfort mode per room, in addition to the shared schedule.
- **PV-surplus boost** — an optional binary entity raises the comfort setpoint while solar
  surplus is available.
- **Minimum flow temperature per room** — reports the flow temperature this room's emitters
  need to meet current demand, so a shared heat source's setpoint can be driven by whichever
  room actually needs it (see `sensor.<room>_mindestvorlauftemperatur` /
  `_minimum_flow_temperature`). A surplus gate suppresses the requirement while a room is
  coasting above its setpoint on stored heat, so the value stays usable as a "does this
  room need heat now" signal; `sensor.<room>_hold_flow_temperature` keeps reporting the
  ungated requirement for diagnosis. See [docs/mpc-guide.md](docs/mpc-guide.md).
- **Persisted learning** — each room's learned `ua_factor`/`capacity_factor` survive
  restarts (`homeassistant.helpers.storage.Store`, one file per room) and are removed
  cleanly if the room's config entry is deleted.
- **Custom Lovelace card** (`custom:heating-controller-card`) — a single card per room
  showing room temperature, configurable header sensors (humidity, CO₂, PM2.5, …), mode
  control, comfort/eco setpoints, boost, room-specific comfort conditions, and a detail
  section with per-TRV state, window contacts, and minimum flow temperature — with a visual
  editor, no YAML required. See [`docs/card-design.md`](docs/card-design.md).
- Fully bilingual UI (English/German), both backend (`strings.json`/`translations/`) and
  frontend card (`translations.js`).

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/DerOetzi/home-assistant-mpc-heating-controller`, category
   "Integration"
3. Install "Heating Controller" and restart Home Assistant

### Manual

1. Copy `custom_components/heating_controller/` into your Home Assistant
   `custom_components/` folder
2. Restart Home Assistant

## Setup

Add one config entry per room via **Settings → Devices & Services → Add Integration →
Heating Controller**. The setup flow walks through:

1. **Room name and TRV count** — how many thermostatic radiator valves heat this room.
2. **Per-TRV details** (repeated once per TRV) — the `climate` entity, an optional
   TRV-active switch, valve target-temperature range/step, emitter type, and (depending on
   emitter type) panel radiator type/dimensions or nominal power.
3. **Entities** — optional room temperature sensor, window contacts, room-specific comfort
   conditions, PV-boost entity, plus the required outdoor temperature sensor and shared heat
   source `climate` entity.
4. **Settings** — boost enable/offset, frost-protection temperature, PV-boost enable/offset.
5. **MPC parameters** — design indoor/outdoor temperature and system flow/return
   temperatures (used to size the emitter curve), room heat load, and demand-control
   hysteresis/hold-time/max-step tuning.

All of the above is editable afterwards via the entry's **Configure** options flow — it
re-runs the same steps pre-filled with the current values.

Deleting a room's config entry removes its entities and its persisted learning-factor file.

## Dashboard card

Once a room's entities exist, add a `custom:heating-controller-card` to a dashboard (via the
card picker's visual editor, or `type: custom:heating-controller-card` / `device: <device
id>` in YAML). Optional `header_entities` (e.g. a humidity or CO₂ sensor) and
`detail_entities` (arbitrary extra sensors, in addition to the auto-populated TRV/window
rows) are configured entirely through the editor's sortable lists — see
[`docs/card-design.md`](docs/card-design.md) for the full configuration reference.

## Notes

- Each room is its own config entry — deleting a room removes only that room's entities and
  learning data, other rooms are unaffected.
- Learning (the `ua_factor`/`capacity_factor` self-calibration) only runs while the shared
  heat source is actually calling for heat above the configured flow threshold; outside the
  heating season the room simply stops adjusting until it's needed again.
- The card doesn't care about entity IDs — rename any of a room's entities freely, the card
  keeps working.
- After updating the integration (HACS or manual), do a hard browser reload (Ctrl+F5) once
  so the updated Lovelace card is picked up — a normal refresh can serve a cached, outdated
  version.

## Development

```bash
pip install -e ".[test]"
pytest
```

`pytest` runs against a real Home Assistant test harness
(`pytest-homeassistant-custom-component`) plus a QuickJS-based harness that executes the
shipped Lovelace card's JavaScript directly, catching syntax errors that would otherwise
only surface as "custom element not found" in a browser.

## License

MIT — see [LICENSE](LICENSE).

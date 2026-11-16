# Heating Controller

[![Validate](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/actions/workflows/validate.yml/badge.svg)](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/v/release/DerOetzi/home-assistant-mpc-heating-controller)](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration for **room heating with self-regulating TRVs** —
one hub per house with the rooms as its sub-entries. The TRVs get each room's setpoint and
the room sensor's reading; a self-calibrating grey-box thermal model per room
turns its learned losses into the flow temperature the heat source should deliver. A
purpose-built Lovelace card operates it.

## Features

- **Grey-box thermal model per room** — heat-loss (`ua_factor`) and thermal-capacity
  (`capacity_factor`) coefficients, learned online from each room's own temperature response
  and continuously self-calibrated while the room is actively heating. See
  [`docs/mpc-guide.md`](docs/mpc-guide.md) for how modes/setpoints combine, what the
  learning-status sensors mean, and how to size a room correctly.
- **Radiator lag and a room observer** — the radiators follow the output at the reported
  valve opening with a
  lag derived from their water and steel mass, and the room model runs alongside the room,
  correcting itself towards the measurement, so the recovery flow counts the heat still in the
  radiators. Lasting gains (neighbours, appliances) end up in the learned heat-loss factor. See
  [`docs/mpc-guide.md`](docs/mpc-guide.md#radiator-lag-and-the-room-observer).
- **Per-TRV emitter modelling** — each thermostatic radiator valve is configured with its
  emitter type (panel radiator or towel radiator), panel radiator type (10/11/21/22/33) and
  dimensions, or a fixed nominal power for towel radiators. The TRV regulates the room with
  its own PI; the room's demand is the highest valve opening its TRVs report (sensor
  "Valve opening", entity `heating_demand`).
- **Comfort / eco / boost / frost-protection mode state machine** — a `select` entity per
  room drives the active mode; `comfort`/`eco` setpoints are independently adjustable
  `number` entities, boost applies a temporary offset on top of comfort, and frost
  protection is the always-available floor.
- **Automatic vs. manually blocked operation** — a `binary_sensor` tracks whether the room is
  following its normal comfort/eco schedule or has been manually overridden; a dedicated
  `button` (and the `heating_controller.unblock` service, targetable at multiple rooms at
  once) clears the override and resumes automatic mode.
- **Window contacts and comfort switches** — an open window drops the room to frost protection; the hub's
  comfort switch (off e.g. on holiday) puts every following room on eco, and a room can get
  a comfort switch of its own (e.g. a guest room), no helpers needed.
- **PV-surplus boost** — the hub's PV-surplus switch (by hand or from an automation)
  raises the setpoint of rooms with PV boost.
- **Flow requirements per room** — `sensor.<room>_hold_flow_temperature` reports the flow
  this room's radiators need to hold its setpoint at the current outdoor temperature (the
  feedforward of the flow control); `_gated_hold_flow_temperature` reads 0 while a surplus
  gate sees the room coasting above its setpoint on stored heat (the heating limit);
  `_boost_flow_temperature` carries the recovery flow while the room recovers after its
  setpoint went up, 0 otherwise. Hold and recovery flow use the same design spread as the
  valve power the factors are learned from. See [docs/mpc-guide.md](docs/mpc-guide.md).
- **Supply per room** — `sensor.<room>_supply` judges the flow on the room's valves, the
  same way the flow feedback does: undersupplied (valves fully open, room below target),
  supplied, oversupplied (valves well closed or room above target), or not rated with a
  reason (window open, frost protection, no heat, radiator not heating, boost, valve
  opening unknown).
- **TRV setpoints** — each TRV regulates the room with its own PI. The room hands it the
  active mode's target through `climate.set_temperature` and the room sensor's reading as
  external temperature (more often for a radiator marked covered), switches its
  heat-available switch with the heat source, and drops the setpoint to frost protection
  while a window is open. TRVs sharing a room share their load (Danfoss load balancing). Without an outdoor temperature only learning pauses. See
  [docs/mpc-guide.md](docs/mpc-guide.md#trv-control).
- **Persisted learning** — each room's learned `ua_factor`/`capacity_factor` survive
  restarts (`homeassistant.helpers.storage.Store`, one file per room) and are removed
  cleanly if the room is deleted from the hub. The admin-only
  `heating_controller.recalibrate` service sets or resets them, resets a room's learned
  gains.
- **Learning only when gains are small** — learning pauses while the sun is up and for two
  hours after sunset, while the outdoor temperature is missing or the flow temperature has
  had no reading for over 16 minutes, and while a room's optional *block learning* entity is
  on (e.g. an extra heater in that room). The heat-loss factor comes from a heat balance
  over each usable night stretch (four to eight hours) of the radiators' measured output, the capacity factor from the
  energy balance of the stretch after each setpoint jump (heating up learns, cooling down
  only estimates), as long as no sun reaches the room's windows. See [docs/mpc-guide.md](docs/mpc-guide.md#when-the-model-learns).
- **Gains and sun (learning only, not yet used by the control)** — over several nights a
  regression separates a steady gain from the heat-loss factor; every hour a second one
  learns a day profile (8 blocks of 3 hours, workdays and other days apart) and the sun on
  the room's windows, walls and roof surfaces, with irradiance from Open-Meteo. Shown in
  `sensor.<room>_additional_heat`. See [docs/mpc-guide.md](docs/mpc-guide.md#gains-and-sun).
- **Custom Lovelace card** (`custom:heating-controller-card`) — a single card per room
  showing room temperature, configurable header sensors (humidity, CO₂, PM2.5, …), mode
  control, comfort/eco setpoints, boost, the room's own comfort switch, and a detail
  section with per-TRV state, window contacts, and the room's supply — with a visual
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

**Settings → Devices & Services → Add Integration → Heating Controller** creates the hub,
once per house. It holds everything that applies to the whole house:

1. **Heat pump and house signals** — outdoor temperature sensor, heat source `climate`
   entity (operating state), measured flow temperature sensor, fixed flow threshold and an
   optional threshold entity, optional effective heating curve (for the flow reserve),
   frost-protection temperature, the flow
   gap after which the heat source counts as unreachable, and the write budget for the heat
   source's cloud (optional entity with the calls left today, reserve kept for required
   writes, own writes per day at most).
2. **Control and design** — design outdoor temperature and system flow/return temperatures
   (used to size the emitter curves), surplus gate and sensor age.
3. **Sun and workdays** — how the house is turned (the bearing of its north facade, e.g.
   −15° for a house turned 15° west), which way the roof surfaces face and their pitch, and
   an optional workday entity (on = workday; without it Monday to Friday). For the surfaces
   the rooms pick, the hub fetches hourly irradiance from
   [Open-Meteo](https://open-meteo.com/): **this sends the latitude and longitude of your
   Home Assistant to api.open-meteo.com.** No room with surfaces, no request.

**Reconfigure** on the hub changes these; all rooms reload.

Then **Add room** on the hub, once per room:

1. **Room name and TRV count** — how many thermostatic radiator valves heat this room.
2. **Per-TRV details** (repeated once per TRV) — the thermostat device, emitter type, and
   (depending on emitter type) panel radiator type/dimensions or nominal power. The
   device's profile supplies everything else, see [Supported devices](#supported-devices).
3. **Sensors and signals** — optional room temperature sensor, window contacts, whether the
   room follows the hub's comfort switch, whether it gets a comfort switch of its own,
   optional entity that blocks learning while on.
4. **Boost, PV boost and lead room** — boost enable/offset, PV-boost enable/offset, and
   whether the room is a lead room for the heating limit and/or the flow temperature.
5. **Room design** — design indoor temperature, room heat load, room area and construction
   class (the base heat capacity), and the building surfaces whose sun reaches the room:
   facades with windows, and outer walls or roof surfaces without (a wall with windows
   counts in both).

Optionally **Add flow control** on the hub, once: the integration then sets the heat
source's flow itself.

1. **Flow actuator** — what the entity sets: an *offset* on the heat source's own
   weather-compensated curve (e.g. Daikin `climate.*_leaving_water_offset`), or a *flow
   setpoint*; and the entity (`climate`, `number` or `input_number`).
2. **Actuator limits** — lowest and highest value and step (pre-filled from the entity),
   and the fallback value written once the outdoor value or the lead rooms' flow has been
   missing for 5 minutes.

The device *Vorlauf* then holds the heating curve as numbers (two points and an optional
raise around 0 °C, as the heat source has it), hysteresis, hold time, the most a
hot-water charge may raise the flow. The flow threshold replaces the hub's threshold
entity: in offset mode the curve's warm end (*Flow threshold effective*), in setpoint mode
a number on the device. The target never drops below it. *Write flow* starts off: the device computes and shows *Flow target* and
*Actuator target* but writes nothing until switched on. See
[Flow control](docs/mpc-guide.md#flow-control). *Heating curve recommendation cold* /
*warm* report the curve points the flow lead rooms need (hold flow at comfort with the
learned heat-loss factor, plus the valve feedback *Flow feedback*); each room reports its own as *Hold flow
temperature at curve cold/warm end*. See
[Setting the heating curve](docs/mpc-guide.md#setting-the-heating-curve).

Optionally **Add hot water control** on the hub, once: the integration then sets the
water heater's target, runs the legionella cycle and the circulation pump.

1. **Sources** — water heater, tank temperature, grid export, battery state of charge and
   discharge (each optional: without it its gate is off), and whether legionella
   protection and the circulation pump are handled too.
2. **Modes and times** — PV surplus thresholds (lower, stable upper, release), battery
   and tank gates, outdoor limits, minimum time between mode changes, manual hold, boost
   limit, night setback, charge window, forced charge and the heater's reheat hysteresis.
3. **Legionella protection** (if chosen) — weekdays, start, optional fallback start,
   retry window, PV forecast condition, outlet and circulation-return thresholds, hold
   time, verification timeout.
4. **Circulation pump** (if chosen) — pump switch, return and outlet sensors, draw
   detection, external continuous-run requests, gates, peak windows, thresholds, run
   and pause times, release of a continuous run.

The device *Warmwasser* holds the targets of each phase as numbers (normal, surplus,
boost, night, charge window, legionella), a mode select (automatic, or normal/surplus by
hand for the manual hold time), a boost switch, *Hot water target* and *Hot water phase*,
*Hot water charging* (rooms treat it as a pause), and — when chosen — legionella status,
a *failed* problem sensor, a start button, the circulation's main switch (off keeps the
pump off, e.g. for a repair), its continuous-run switch and its reason. See [Hot water](docs/mpc-guide.md#hot-water).

**Reconfigure** on a room re-runs steps 2–5 pre-filled (the name stays: the room's
learning files are keyed by it). Deleting a room removes its entities and its learning
files.

### Hub entities

The hub's device *Heizung* reports the lead rooms:

- *Lead rooms hold flow temperature* — highest gated hold flow among the heating-limit lead
  rooms (attributes: `room` with the highest value, `rooms`).
- *Lead rooms heating demand* — on while the hold flow reaches the flow threshold;
  unavailable while either is missing.
- *Flow reserve hold* — heating curve minus the lead rooms' hold flow, only with a heating
  curve; unavailable while the flow is 0 (all lead rooms in their surplus buffer).
- *Irradiance &lt;surface&gt;* — sun on each surface some room uses (W/m², diagnostic),
  with the next 24 hours as attribute `forecast` (not recorded); unavailable when the last fetch is more
  than three hours old.
- *Heat source reachable* — off once the measured flow temperature has been missing longer
  than the configured gap (diagnostic).
- *Write budget* — cloud calls left today (from the rate-limit entity, else the own daily
  maximum minus own writes); attributes `own_writes_today`, `dropped_today`. Used by the
  flow and hot-water control to stay inside the cloud's daily limit.
- *Heating demand last 24 h* — hours in which *Lead rooms heating demand* was on, kept
  across restarts (the time HA is down counts as no demand).
- *Heating season last demand* — last time those hours were above the hold threshold.
- *Heating season: switch on* / *switch off* — recommendations only, nothing is switched:
  on while the 24 h demand is above the switch-on threshold and the heat source does not
  heat; off while it heats and the hours have not been above the hold threshold for the
  switch-off time. Thresholds are the numbers *Heating season switch-on threshold* (6 h),
  *hold threshold* (2 h) and *switch off after* (48 h). Point a notification automation at
  the two binary sensors.

## Supported devices

A TRV is picked as a device. A device profile for its model then names the entities the
controller uses — found by their unique ID, so renaming them breaks nothing. The target
temperature range and step come from the climate entity:

| Device | Integration | Entities used |
|---|---|---|
| Danfoss Ally (014G2461) | Zigbee2MQTT | climate, `pi_heating_demand` (valve opening), `heat_available`, `adaptation_run_status`, `mounted_mode_active` |
| Danfoss Ally (eTRV0100, eTRV0101, eTRV0103) | ZHA | the same; not yet tested on a real device |

Only devices with a profile can be picked. For another model please
[open an issue](https://github.com/DerOetzi/home-assistant-mpc-heating-controller/issues)
with its manufacturer, model, integration, and the entities the device has (their entity
IDs and unique IDs, e.g. from the device page or `Settings → Entities`). Profiles are YAML
files in `custom_components/heating_controller/device_profiles/`, so a pull request with a
new one is welcome too.

What the controller does with each entity:

- **climate** — reads the TRV's own temperature and writes the target temperature.
- **Valve opening** — the heat the radiator really gives off, see
  [docs/mpc-guide.md](docs/mpc-guide.md#valve-position-the-heat-the-radiators-really-get).
  The highest opening of the room is its demand. An opening counts as unknown once the
  TRV reported nothing for 75 minutes, neither its opening nor its temperature: an unchanged
  opening is not reported again (a TRV with constant values reports about once an hour), and a Zigbee battery device that hangs stays available
  with its last values.
- **Heat available** — switched on while the heat source delivers heat, so the TRV knows
  whether opening its valve can help. A flow dip below the threshold (e.g. a defrost)
  switches it off only after 20 minutes.
- **Adaptation status** — while the TRV calibrates its valve, nothing is written to it
  (any write aborts the run), for at most three hours and except for forced frost
  protection; the room does not learn meanwhile. Nor does it learn while the TRV has no
  calibrated valve (status other than `found`).
- **Mounted mode** — while the TRV is in mounting mode the room learns nothing.

A room none of whose TRVs reports a valve opening has no measured demand: it learns nothing
(status `valve_position_missing`) and its radiators are not checked.

**Danfoss Ally:** set the algorithm scale factor on the device to 1 (Zigbee2MQTT:
`algorithm_scale_factor`), the quickest response of the TRV's own regulation. The controller
leaves the regulation to the TRV and only corrects its setpoint slowly; it does not write
this setting.

## Dashboard card

Once a room's entities exist, add a `custom:heating-controller-card` to a dashboard (via the
card picker's visual editor, or `type: custom:heating-controller-card` / `device: <device
id>` in YAML). Optional `header_entities` (e.g. a humidity or CO₂ sensor) and
`detail_entities` (arbitrary extra sensors, in addition to the auto-populated TRV/window
rows) are configured entirely through the editor's sortable lists — see
[`docs/card-design.md`](docs/card-design.md) for the full configuration reference.

With a flow device, `custom:heating-controller-curve-card` (`entity:` its *Hold flow
curves* sensor) plots the heat source's curve against each room's hold flow and lets you
try a different curve before setting it on the heat source.

## Notes

- Each room is a sub-entry of the hub — deleting a room removes only that room's entities and
  learning data, other rooms keep running. Adding, changing or deleting a room reloads all
  rooms.
- Disabling a room's device takes the room out of the integration until you enable it again:
  no control, no learning, not counted as a lead room. Its TRVs keep their last setpoint and its
  learning data stays. Disabling or enabling reloads all rooms.
- Learning (the `ua_factor`/`capacity_factor` self-calibration) only runs while the shared
  heat source is actually calling for heat at or above the configured flow threshold; outside the
  heating season the room simply stops adjusting until it's needed again. Within that time
  it also waits for the sun, the outdoor and flow readings and the room's block entity, see
  [docs/mpc-guide.md](docs/mpc-guide.md#when-the-model-learns).
- The card doesn't care about entity IDs — rename any of a room's entities freely, the card
  keeps working.
- After updating the integration (HACS or manual) and restarting Home Assistant, a normal
  browser reload picks up the updated Lovelace card: its URL carries a hash of the card's
  files, so a changed card is never served from the browser cache.

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

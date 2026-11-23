# The heating model (MPC)

Each room hands its TRVs the mode's setpoint and the room sensor's reading; the TRVs
regulate the room themselves. Alongside, a
model of the room learns how it loses and stores heat, and turns that into the flow
temperature the heat source should deliver. This page covers what to expect from that in
day-to-day use — for what the fields of the hub and the rooms actually do, see below.

## Modes and setpoints

- **Comfort** — the room's normal target temperature (the comfort-temperature slider on the
  card).
- **Eco** — comfort temperature plus the eco offset (normally negative, e.g. −2 °C) — a
  lower setback target, not a separate independent setpoint.
- **Boost** — comfort temperature plus a fixed boost offset, for a temporary push above
  normal comfort.
- **Frost protection** — a fixed floor temperature, independent of the comfort setpoint.
  This one is also **enforced automatically**, regardless of the selected mode, whenever a
  configured window contact in the room is open — you'll see "Frost protection enforced" in
  the card's header, and it clears itself as soon as the window closes. No action needed on
  your part. Without heat from the heat source the mode stays; only the TRVs'
  heat-available switches go off.
- **PV boost** — if enabled and the hub's PV-surplus switch is on, adds a further fixed
  offset on top of whichever mode is currently active.

**Comfort mode only engages automatically when every comfort switch the room follows is
on** — the hub's comfort switch (unless the room is set not to follow it) and the room's
own comfort switch, if it has one. The hub's switch restores on, a room's own switch off.
A room that follows neither stays on Eco until you switch it manually. The card shows only
a room's own switch in its header; labels, icons or hiding it are set in the card.

## Reading the learning status

The two factors learn in different ways (see [When the model learns](#when-the-model-learns)),
so each has its own status sensor.

**`sensor.<room>_ua_learning_status`** — where the heat balance for the heat-loss factor
stands:

| State | Meaning |
|---|---|
| `learned` | The last balance moved the factor towards its target: 30 % for an eight-hour stretch, less for a shorter one. |
| `learned_at_limit` | The target lay below 0.15: the radiators supplied almost nothing and the room lives off its neighbours. The factor moved towards 0.15 only. |
| `collecting` | A stretch is running. |
| `coasting` | The room is more than 0.1 K above its mode setpoint or more than 0.4 K below it; the stretch starts once it is back or has held its temperature for an hour. |
| `outside_window` | Sun condition not met. A room still moving after sunrise reports this, not `coasting`. |
| `paused` | Learning enabled entity off. |
| `too_short` | The stretch ended with fewer than four usable hours. |
| `gap` | A sample gap above 20 minutes. |
| `drift` | The trend over the stretch moved the room more than 0.3 K. A stretch that started on a still room away from its setpoint is first retried from where the room came near its setpoint, if four hours are left. |
| `small_temperature_difference` | Room on average less than 8 K above outside. |
| `storage_dominated` | The heat stored in or released by the room was more than half of the radiator heat. |
| `implausible_target` | The balance pointed at a factor above 2. |
| `room_sensor_fallback` | The configured room sensor is stale or unavailable and the TRVs stand in. |
| `valve_position_missing` | A linked valve position entity is stale or unavailable, or none of the room's TRVs has one. |
| `trv_adapting` | A TRV calibrates its valve and moves it by itself, has no calibrated valve (adaptation status other than found, e.g. `none` after mounting or `lost`), or is in mounting mode. |
| `radiator_not_heating` | A TRV asks for nearly full heat and the room does not get it, see [Radiator check](#radiator-check). |
| `outdoor_temperature_missing` | The outdoor temperature is missing, see [Sensor reliability](#sensor-reliability). The stretch before the outage is balanced; the next one starts once the value is back. |
| `valve_saturated` | For more than half the stretch every valve was at least 95 % open with the room more than 0.4 K below its mode setpoint. The heat assumed for a full opening is the least certain (a flow-limited radiator gives off less), so the factor is left alone; the target is kept for diagnostics. |
| `window_open` | A window contact in the room is open. |

| Attribute | Meaning |
|---|---|
| `collecting_since` | Start of the current stretch. |
| `due` | When that stretch reaches four hours, the earliest it can be balanced. |
| `last_learned` | End of the last balance that moved the factor. |
| `last_target_factor` | The factor the last balance pointed at. Also set for `storage_dominated` and `implausible_target`, which leave the factor alone. |
| `last_drift_c` | How far the room moved over that balance; also set for `drift`. |
| `last_window_h` | How many hours that balance covered. |
| `last_mean_outdoor_c` | Time-weighted mean outdoor temperature of the last stretch that got as far as the balance (from `small_temperature_difference` on; a retried stretch reports the retry). Earlier rejections such as `too_short` or `gap` keep the previous values. |
| `last_mean_room_c` | Same for the room temperature. |
| `last_mean_flow_c` | Same for the flow temperature, over the samples that had one; `null` if none did. |
| `last_mean_heating_w` | Mean radiator heat the balance assumed over that stretch (hot-water charges count as zero). |
| `learned_gain_w`, `gain_sigma_w`, `gain_fit_points`, `gain_determined`, `regression_ua_factor` | Night regression of steady gain and heat-loss factor, see [Gains and sun](#gains-and-sun). |

**`sensor.<room>_capacity_learning_status`** — how the last jump stretch (see
[When the model learns](#when-the-model-learns)) was judged:

| State | Meaning |
|---|---|
| `learned` | A heat-up stretch moved the factor 30 % of the way to its estimate. |
| `estimated` | A cool-down stretch gave an estimate; the factor stays as it is. |
| `running` | A stretch is in progress. |
| `waiting` | No setpoint jump yet. |
| `too_short` | The stretch lasted less than 10 minutes. |
| `too_small` | The room moved less than 0.4 K, or showed fewer than three different readings. |
| `gap` | More than 20 minutes without a sample, or the jump left the history before the stretch ended. |
| `outside_window` | Sun on the room's windows during the stretch or the two hours before it. |
| `implausible` | The net heat does not fit the room's change, or the estimate is outside 0.1–4. |
| `window_open` | A window was open, or a contact reported neither open nor closed, when the stretch started. |
| `room_sensor_fallback` | The TRVs stood in for the room sensor when the stretch started. |
| `valve_position_missing` | No valve opening reported when the stretch started. |
| `trv_adapting` | A TRV calibrated its valve, was uncalibrated or in mounting mode when the stretch started. |
| `radiator_not_heating` | A radiator stayed cold when the stretch started. |
| `paused` | Learning paused (block learning entity, flow or outdoor temperature missing) when the stretch started. |

Each of the flags from `window_open` on ends a running stretch early instead; the part before
it is still judged.

| Attribute | Meaning |
|---|---|
| `capacity_j_per_k` | Effective heat capacity the model uses: base capacity from area and construction times the learned capacity factor. |
| `last_stretch_kind` | `heat_up` or `cool_down`. |
| `last_stretch_start`, `last_stretch_end` | Start and end of the last stretch judged (end empty while running or when blocked at the start). |
| `last_stretch_room_delta_c` | How far the room moved over the stretch. |
| `last_stretch_estimate_factor` | Capacity factor the stretch's energy balance points to. |
| `last_stretch_saturated_share` | Share of the stretch with a fully open valve (diagnostic only). |
| `last_learned` | End of the last stretch that moved the factor. |
| `vent_capacity_factor` | Airing capacity factor (observed only, see [When the model learns](#when-the-model-learns)); 0.3 of the capacity factor until learned. |
| `vent_learned` | Whether an airing has been learned since the last reset. |
| `vent_outcome` | How the last airing was judged, with the states above. |
| `vent_stretch_start`, `vent_stretch_end`, `vent_stretch_room_delta_c`, `vent_stretch_estimate_factor` | As above, for the stretch after the last airing. |
| `vent_last_learned` | End of the last airing stretch that moved the airing factor. |

Adjustments are deliberately gradual — expect the model to sharpen over real heating days,
not to converge instantly. None of these states need any action from you;
they're diagnostic, useful mainly if a room's behavior seems off and you want to see whether
it's actively adapting.

## When the model learns

The two factors are learned in different ways, because a wrong heat-loss factor hardly shows
as a model error: the TRVs keep the room at its setpoint, whatever the model believes the
losses are.

- **Heat-loss factor (`ua_factor`): heat balance over a usable stretch.** Over the
  stretch the heat the radiators gave off (the output at the reported valve opening, lagged as described in
  [Radiator lag and the room observer](#radiator-lag-and-the-room-observer)) has to equal
  what the room lost plus what it stored. The factor that balances this is the target. A balance over eight hours moves
  the learned factor 30 % of the way there, a shorter one less (1 − 0.7^(hours/8)), so every hour counts the same however
  a night is split into stretches: a long winter night balanced as eight plus five hours moves the factor as much as one
  thirteen-hour stretch would. A stretch starts once the room is near its mode setpoint — no more than 0.1 K above it
  or 0.4 K below (a TRV holds the room within a few tenths, plus a sensor step); a room still coasting down on the evening's heat with the valve
  shut would end far below where it started, one recovering from below far above (status
  `coasting` until then) — or once it has held its temperature for an
  hour (trend below 0.05 K/h), counted from the start of that hour, so a room its
  neighbours keep above the setpoint still learns. It runs as long as learning is possible and is
  balanced once when it ends: learning is paused, a
  window contact opens, the configured room sensor drops out (the TRVs read up to a kelvin
  off at the radiator; the next stretch starts once it reports again), a TRV starts
  calibrating its valve, the outdoor temperature goes missing (see
  [Sensor reliability](#sensor-reliability)), the sun condition ends, or heating stays off for more than three
  controller cycles (15 minutes). A stretch that reaches eight hours is balanced right away.
  Shorter interruptions of heating (a missed heat-source poll, the flow briefly under the
  threshold) are bridged and count as no heating. A balance needs at least four gap-free
  hours (no gap above 20 minutes), a drift of at most 0.3 K, and on average at least 8 K
  between room and outside. The drift is taken from a straight line fitted through the
  whole stretch (samples weighted by the time they stand for), not from its edges, so
  heating pulses, a sensor reporting once an hour or a hot-water dip at the start do not
  count as stored heat. The stored heat is the drift times the base capacity from area and
  construction (see [Heat capacity](#heat-capacity-room-area-and-construction)), not the
  learned capacity factor: over hours the room's whole mass takes up or gives off heat, while
  a setpoint jump only shows its air and surfaces. Room and outdoor temperature are averaged
  over each interval between two samples, like the radiator heat. Samples taken during a hot-water charge (see
  [Hot-water charges](#hot-water-charges)) count as no heating. Each
  stretch is used once. Steady gains during the window (people, appliances,
  neighbouring rooms) end up in the factor — that is what the room needs in operation.
  Each balanced stretch is also a point for the night regression that separates a steady
  gain from the factor over several nights, see [Gains and sun](#gains-and-sun).
  Two checks keep measurement artefacts out: a stretch where the heat stored in or
  released by the room exceeds half of the radiators' heat depends more on the capacity
  than on the losses and is discarded, and so is a target above 2. A target below 0.15
  means the radiators supplied almost nothing — the room lives off its neighbours, for
  whom the model has no coupling: the factor then moves only down to 0.15 (status
  `learned_at_limit`, the measured target stays in `last_target_factor` of the UA learning status).
  A stretch rejected for drift reports the
  drift in `last_drift_c`.
- **Mode setpoint.** The learner judges against the setpoint of the room's mode
  (comfort, eco …), not the value written to the TRV: frost protection on an open window
  counts as no setpoint change.
- **Capacity factor (`capacity_factor`): jump stretches.** A held room hardly moves, so
  the capacity is learned where it does: after the mode setpoint jumps by at least 0.5 K
  (comfort/eco switch). The stretch starts at
  the jump — or once a window closed or the configuration changed 10 minutes ago — and
  ends when the room comes within 0.2 K of the new setpoint, the mode setpoint changes again by
  0.1 K or more, a flag from the table above appears, or after three hours. Over the
  stretch the energy balance gives the net heat: the heat the radiators gave off minus the
  loss with the current heat-loss factor (mean room against outdoor, per interval).
  Divided by the room's change and the base capacity it is the estimate. A heat-up stretch
  moves the factor 30 % of the way to it. A cool-down stretch only reports its estimate:
  the gains, unknown here, are a larger part of its balance. Stretches are judged with
  heating off as well, so a room cooling down in frost protection shows its estimate too.
- **Airing capacity (`vent_capacity_factor`), observed only.** After airing, the room
  air comes back quickly while walls and furniture stayed warm, so it shows a smaller
  capacity than a jump. The value has no effect on control yet. An airing counts if the
  window was open at least 5 minutes and the room cooled by at least 0.3 K. The stretch
  starts once the rebound is over — the room no longer warms faster over 15 minutes than
  the model expects with the current heat — at the earliest when the capacity block after
  closing ends, and is balanced and ended like a heat-up stretch. Until the first airing
  is learned the factor is 0.3 of the capacity factor, within the same limits.
- **Sun.** The heat-loss balance does not run while the sun is up or within two hours after
  sunset, when walls still release the afternoon's solar heat. Sunrise and sunset are
  calculated from the location configured in Home Assistant. A jump stretch only counts if
  the irradiance on each of the room's window surfaces stayed at or below 50 W/m² during the
  stretch and the two hours before it, so it can be learned at midday on an overcast day.
  Without irradiance data the sun condition decides at start and end; a room without window
  surfaces skips the check, since sun on its walls reaches the room only hours later and
  damped. Configure the window surfaces of every room with windows, or its jump stretches
  are not checked for sun at all.
- **Pause.** Both factors pause, and a window that started before the pause ended is
  discarded since part of its samples were taken while paused:
  - while the room's optional **block learning entity** is on — a reason only this room
    has, such as an extra heater (an air conditioner heating the bedroom). `unavailable`
    counts as off;
  - while the hub's **flow temperature** has had no reading for more than 16 minutes. A heat
    pump polled through its cloud reports about every 10 minutes and drops a poll now and
    then; a gap that short is bridged with the last reading. Link the measured sensor, not a
    template that fills in a calculated value: learning cannot tell the two apart;
  - while the outdoor temperature is missing (see [Sensor reliability](#sensor-reliability)).

  There is no separate learning window: the heat-loss balance runs in the sun condition
  above, starting two hours after sunset. Evening activity (cooking, people) can fall into
  a stretch; the drift check and the start once the room is settled catch part of it.

The heat-loss factor may end up well below 1: room heat loads usually come from a design
calculation for the worst case (neighbouring rooms unheated, full air change), which a room
in operation rarely sees. It is limited to 0.1–2. The capacity factor is limited to
0.15–3: the effective capacity a room shows on a jump is mostly its air and furniture,
far below the construction value.

The block learning entity is set per room.

### Recalibrating

The `heating_controller.recalibrate` service sets the learned factors of one or more rooms
directly, for instance to reset them after changing the learning rules or replacing a
radiator. It is limited to administrators.

```yaml
action: heating_controller.recalibrate
target:
  device_id: [...]
data:
  ua_factor: 1.0
  capacity_factor: 1.0
  reset_gains: true
```

A factor that is left out stays as it is, so a reset needs an explicit `1.0`; at least one
value (or a reset) is required. The call discards the heat-loss stretch and any jump
stretch in progress, because they were measured with the old factors. A new capacity factor also resets the airing factor to its start value. `reset_gains: true` forgets the
learned steady gain, day profile and sun terms (see [Gains and sun](#gains-and-sun)).

## Gains and sun

Heat the radiators do not supply — people, cooking, appliances, neighbouring rooms, the sun —
is learned on two levels. **At this stage it is only learned and shown; the control, the
hold and the boost flow do not use it yet.** Both levels use recursive least
squares with forgetting, bounds and damping of outliers (after
[Thermocast](https://github.com/drumtorben/thermocast)).

**Night: steady gain.** Every balanced heat-loss stretch is a point: the mean room-outdoor
difference and the net radiator heat (heat minus what the room stored). Over several nights
with different temperature differences, `net heat = UA × difference − G` separates a steady
gain `G` from the heat-loss factor — one night alone cannot. About 20 nights of memory; `G`
stays within half the design heat load. `G` counts as **determined** from three points on,
once its standard deviation is below a tenth of the design heat load. Points with the same
temperature difference never determine it. Shown on the UA learning status:
`learned_gain_w`, `gain_sigma_w`, `gain_fit_points`, `gain_determined` and
`regression_ua_factor` (the regression's own heat-loss factor, diagnostics only — the factor
the control uses still comes from the single-night step).

**Hourly: day profile and sun.** Every full clock hour gives the heat the model with the
heat-loss factor and the determined `G` does not explain: what the room stored (from a
straight line through the hour, with the base capacity as in the night balance), minus the radiator heat, plus the loss, minus `G`. A second
regression splits it into

- a **day profile**: 8 blocks of 3 hours (00–03, 03–06, …), workdays and other days apart —
  16 values. It is relative to the night: night blocks come out near zero, a bedroom whose
  sleepers raise the night's `G` comes out below zero by day;
- **sun through windows**: per facade with windows, the hour's irradiance on that facade
  times a learned coefficient (heat in W at 1000 W/m²; it takes in window area, glazing,
  blinds and shading);
- **sun through walls and roof surfaces**: per outer wall or roof surface, the irradiance
  through a first-order lag of 3 hours times a learned coefficient.

About 10 days of memory. Sun coefficients cannot go below zero. A profile value counts as
determined once it has hours from at least three days and its standard deviation is below a
tenth of the design heat load (with one evening, a hot shower in a small bathroom made a
block look known and kept the room below setpoint), a sun
coefficient once its standard deviation is below half its value. Sun and profile are told
apart by cloudy days; a stretch of clear days leaves the sun coefficients undetermined but
the sum right.

An hour is not learned (attribute `last_hour_outcome`) while learning is paused, the
outdoor or flow temperature is missing or heating is unavailable (`paused`), a window is open
(`window_open`) or was just closed (`suppressed`), the room sensor stands in for TRVs, a valve
opening is missing, a TRV adapts, a radiator stays cold or all valves were saturated, with a
gap over 20 minutes (`gap`), when the hour is not covered to within 10 minutes of both edges
— a restart within the hour — (`too_short`), or by day without a fresh sun series
(`sun_missing`).

**Irradiance.** The hub fetches global tilted irradiance for every surface some room uses
from Open-Meteo once an hour (the past day and two days ahead). This sends the location of
Home Assistant to api.open-meteo.com. A series older than three hours counts as missing: no
sun terms are learned by day then, and none is estimated. Surfaces: four facades from the
house's rotation (the bearing of its north facade) plus two roof surfaces with the roof's
pitch. Each surface's sensor (*Irradiance …*, W/m²) shows the current value and the next 24
hours as attribute `forecast` (not recorded).

**Shown.** `sensor.<room>_additional_heat` (German install: `sensor.<raum>_zusatzwarme`) is
the gain the model would count now — determined terms only. Attributes: `steady_gain_w`,
`profile_now_w`, `sun_now_w`, `ungated_w` (all terms, determined or not),
`gain_profile_workday_w` and `gain_profile_other_days_w` (8 values each), `profile_determined`,
`profile_days` (days with hours per block, counted up to three),
`sun_coefficients_w`, `sun_determined`, `hour_samples`, `residual_rms_w` (spread of the
hourly residual), `last_hour`, `last_hour_outcome`. The profile and sun attributes
(`gain_profile_*`, `profile_*`, `sun_coefficients_w`, `sun_determined`) are not recorded; read
them live. The state of both regressions survives restarts; changing a room's surfaces starts
its hourly level anew.

## Sizing a room correctly

The **design indoor/outdoor temperature**, **radiator design temperature system** (e.g.
55/45 °C), and **design heat load** you enter for the hub and the room describe how much
heat this room's actual radiators can put out at your real system's flow/return
temperatures — this sets the ceiling on what the model can ever ask the room's own radiators
for, independent of how well-calibrated the thermal model itself is.

If a room's supply (shown on its card) frequently reads as "Short ⚠" while the
flow control sits at its upper limit, its radiators are likely genuinely
undersized for the room relative to what was entered — worth revisiting the design heat
load / radiator dimensions for that room, or expecting that room to be the one that sets
the system's flow temperature.

## Heat capacity: room area and construction

The base heat capacity of a room is its **room area** times the effective heat capacity
per square metre of its **construction class** (ISO 13790, table 12):

| Construction | kJ/m²K | Typical |
|---|---|---|
| Very light | 80 | Timber frame, lightweight partitions, no screed |
| Light | 110 | Lightweight walls, wooden floors |
| Medium | 165 | Masonry behind interior insulation, screed |
| Heavy | 260 | Solid masonry or concrete with the mass facing the room (exterior insulation) |
| Very heavy | 370 | Massive concrete walls and floors |

The learned capacity factor (0.15–3) scales this base for furniture, open doors and heat
exchange with neighbouring rooms. Both values are required in the room's design step.

The capacity also drives the recovery flow temperature: a lighter room warms up faster in
the simulation, so it asks for a lower flow.

## TRV control

Each TRV regulates the room on its own: its PI opens the valve by its own reading. There is
no planner and no valve curve; the room hands it these things:

- **Setpoint** — the active mode's target (comfort, eco, boost, frost protection, plus a PV
  boost if on), always through
  `climate.set_temperature`. Rounded to the climate entity's `target_temp_step` (0.5 K
  without one) and clamped to its `min_temp`/`max_temp`. It does not depend on the model:
  the setpoint goes out without an outdoor temperature, without a room sensor reading and
  while the heat source has not reported yet. A target the TRV already reports is not
  written again, since every write wakes it.
- **Heat available** — the profile's heat-available switch, on while the heat source heats
  and the flow is above the threshold (see
  [Flow temperature and an unreachable heat source](#flow-temperature-and-an-unreachable-heat-source)).
  Without heat the setpoint stays; only the switch goes off.
- **Window open** — the setpoint drops to frost protection while a window contact of the
  room is open and returns to the mode target as soon as all are closed. The TRV's own
  open-window flag (Danfoss Ally: `window_open_external`) is not written by the
  integration; set it from an automation if wanted.
- **Room temperature** and **shared load** — see below, for TRVs whose profile has the
  entities.

The attribute `trv_setpoint_writes` of the room's "Valve opening" sensor counts the setpoint
writes per TRV since setup, `trv_external_writes` the room temperature writes,
`trv_target_temps` the target each TRV has, `load_room_mean` the mean load last handed to
the TRVs (`null` while they work alone) and `trv_valve_positions` the opening each TRV
reports right now (`pi_heating_demand`, `null` without a valve-position entity). The
recorder keeps the write counts and the mean load; the history of targets and openings is on
the TRV entities.

### Room temperature to the TRV

The TRV measures at the radiator, warmer than the room. A TRV whose profile names an
external temperature entity gets the room sensor's reading instead (Danfoss Ally:
`external_measured_room_sensor`, in 0.01 °C — the profile's `scale` converts), so all TRVs
of a room work on the same measurement. How the TRV uses it depends on its radiator, set by
the user on the TRV's radiator-covered switch (`radiator_covered`); the integration only
reads it:

- **Exposed** (switch off or none): the TRV keeps regulating on its own sensor and offsets
  it by the room sensor. The reading goes out on a change of 0.1 K but at most every
  30 minutes, and at least every 2.5 hours (the Ally drops it after 3 hours).
- **Covered** (switch on): the TRV regulates on the room sensor alone. On a change every
  5 minutes at most, at least every 25 minutes (the Ally drops it after 35 minutes).
  Switching it sends the reading at once.

While the room sensor's reading is unknown or stale nothing is sent; the TRV returns to its
own sensor after its timeout. A room without a room sensor hands its TRVs the profile's
`off_value` once (Ally: −8000, own sensor). Nothing is written to a TRV running an
adaptation.

### Shared load

TRVs of one room whose profile names a load-balancing switch, a load estimate and a mean
load (Danfoss Ally: `load_balancing_enable`, `load_estimate`, `load_room_mean`) share the
room's load: with two or more of them the integration turns load balancing on and every
15 minutes hands each the mean of their load estimates, leaving out one that reported
nothing for 90 minutes or reports below −500 (unknown). A room with a single such TRV turns
it off. The radiators then open alike instead of one at its limit while the other has room
to spare, which also keeps the flow feedback (highest opening) from raising the flow for
nothing.

While a TRV calibrates its valve (adaptation run, reported by the device profile's
adaptation status entity) no setpoint and no heat-available switch is written to it: a
Danfoss Ally aborts the run on any write. The hold ends with the run, after three hours at
the latest (a run took about 1 h 50 min). A frost protection setpoint for an open window
follows after the run. As the TRV moves its valve by itself meanwhile,
the room learns nothing then (status `trv_adapting`). The room learns nothing either while a TRV has no calibrated valve — its
profile's ready status does not match, as a Danfoss Ally reports `none` from mounting until
its next adaptation run and `lost` after losing the curve — or while it is in mounting mode
(profile's mounted-mode binary sensor on). Writes go on then: an uncalibrated TRV still has
to heat.

## Radiator check

Air in a radiator or a valve stuck shut looks the same to the controller: the TRV asks for
heat, yet the room does not get it. Per TRV the radiator counts as not heating once all of
these held for two hours without a break:

- the TRV's demand (its reported valve opening) is at least 80 %,
- the room is at least 0.5 K below its mode setpoint,
- heating is available and the flow lets the fully open radiator give off at least 30 % of
  its design output (from the design system and the radiator's exponent; a 55/45 radiator
  in a 21 °C room needs about 34 °C),
- the model expects the room to rise: its output with the valves fully open exceeds the
  loss at the current room and outdoor temperature by at least 20 % of its design heat
  load,
- the room did not rise by 0.3 K; a room that does starts the two hours over.

A hot-water charge (see [Hot-water charges](#hot-water-charges)), an open window or an
adapting TRV (calibrating, uncalibrated or in mounting mode) starts the two hours over, as
does half an hour without a calculation. The verdict holds while there is nothing to test
and clears once the room rises 0.3 K above where it stood at the verdict or the TRV's demand falls below 50 %; during a
hot-water charge, with a window open or a TRV adapting neither counts.

Meanwhile the room learns nothing (status `radiator_not_heating`). The running heat-balance stretch ends where the two hours that led to the verdict
started: their heat is in doubt as well. `binary_sensor.<room>_radiator_not_heating` (problem) is on, its attribute
`trv_entities` names the TRVs: an automation can send a notification to check — bleed —
the radiator.

## Valve position: the heat the radiators really get

A TRV is a thermostat, not a valve positioner: the controller sets its target temperature,
and the TRV opens its valve by its own reading at the radiator. A small demand can thus open
a valve almost fully. Where the TRV's device profile names a valve opening entity (Danfoss
Ally: `pi_heating_demand`), the observer, the heat balance and the jump stretches use the heat the radiator gives
off at that opening (sensor `measured_heating_power`).

The opening is taken as proportional to the flow. With less water the radiator cools it
further and its output stays high: at a 40 °C flow a 10 % opening still gives about 44 % of
full output, 50 % about 87 %. A TRV without such an entity counts with the room's highest
reported opening. If the entity goes stale or
unavailable, the room's learning stops (status
`valve_position_missing`) until it reports again. The same holds once the TRV reported
nothing for 75 minutes, neither its opening nor its own temperature (an unchanged opening is
not reported again; a TRV with constant values reports about once an hour). The room's demand is the highest opening among the TRVs with a
current one; with none it is unknown and the radiators are not checked. A room none of whose TRVs has such an
entity never learns: without a measured opening it has no heat to balance.

## Radiator lag and the room observer

A radiator does not stop heating when its valve closes: the water and steel in it keep
giving off heat for a while, and after the valve opens it takes a while to warm up. The
model follows the output at the reported opening with a first-order lag whose time constant comes from the
radiator itself — water content and steel mass from panel type and dimensions (towel
radiators from their nominal power) over the emitter's conductance at the current flow
temperature. Cooling down only goes through the surface; heating up the inflowing water
helps, scaled by the demand. A 1 m type 22 radiator at 600 mm cools off over about 20
minutes at a 40 °C flow and warms up in 5–10.

Each room runs its model alongside the room as a state observer (sensor
`radiator_heating_power`, attribute `estimated_temp_c` of the room temperature sensor):

- Every calculation propagates the estimate with the power the radiators were heading for
  (from the reported valve openings; none without heat or during a hot-water charge) and pulls it
  towards the measured temperature (time constant 5 minutes). A difference above 1 K is
  taken as a jump — a sensor source switching over — and the estimate restarts from the
  measurement.
- A lasting difference between model and room is learned into the heat-loss factor; a
  separate offset term could not be told apart from it at a near-constant room-outdoor
  difference.
- The recovery flow temperature starts from the estimate and the radiators' current output,
  so heat still in the radiators counts. The hold flow temperature uses the lag as well; the
  flow requirement stays weather-driven.

## Sensor reliability

TRV and outdoor temperature readings are checked against **max sensor age (s)**: once a
reading is older than that, it's treated as unavailable rather than trusted indefinitely, and
the room pauses its calculation until a fresh reading arrives. The age counts from when the
room last read the entity, not from its `last_changed`: every room reads all its inputs
every 5 minutes, so an outdoor sensor that reports whole degrees and keeps one value for
hours stays fresh. An entity that turns `unavailable` or `unknown` counts as missing at once.

The room sensor is stricter, because its reading goes to the TRVs: its age counts from its
last report (`last_reported`), and after 150 minutes without one it is stale. A battery
sensor that hangs stays available with its last value; the room then falls back to the TRVs'
own temperatures (`room_sensor_fallback`) and sends them no room temperature any more.
The flow feedback and the supply then take the room's deficit as unknown: the TRVs sit at
the radiator and read warm while it heats. A
sensor with a constant reading reports about once an hour, so one lost report is covered.
A sensor that reports only on a change (some Zigbee sensors with 0.1 K steps) can stay
silent longer in a steady room; a template sensor that blends it with a second, regularly
reporting sensor keeps the room value fresh.
With max sensor age 0 no reading ever goes stale.
Implausible single-sample jumps are also filtered rather than acted on immediately, so one
noisy reading doesn't swing a room's demand; a jump that repeats over a few polls is
accepted as a real change.

The flow temperature is exempt from that filter. Its jumps are physical —
compressor start, defrost, or a hot-water charge showing up on a shared leaving-water sensor
— and it only feeds the radiators' output (observer, heat balance, radiator check), never
the hold or boost flow (see below).

Without an outdoor temperature the model cannot calculate, but the TRVs need nothing from
it: they keep getting the mode's setpoint (see [TRV control](#trv-control)). Learning
pauses for both factors (status `outdoor_temperature_missing`) and the model's values
(powers, flow requirements) keep their last result. Once the outdoor temperature is back
the heat balance starts a new stretch. Feed the outdoor entity a measured value or nothing:
an estimate during an outage would be learned from as if it were measured.

### After a restart

After Home Assistant starts, the integrations restore their entities over a minute or more.
Until then the hub computes, writes and learns nothing. It waits for the entities it needs:
per TRV the climate entity and the profile's heat-available switch, adaptation status,
external temperature and load entities; per room the room sensor when one is configured;
the outdoor temperature, the flow temperature entity, the heat source, and the flow
device's actuator. Window contacts and other optional inputs are not waited for. Once all
of them have a known state and none came in for 30 seconds, or 3 minutes after the start
at the latest, every room runs once in full (setpoints, heat-available switches, room
temperature and load to the TRVs), then the flow and hot water. On the timeout the log
warns `Starting after 180 s without …` with the entities still missing. A reload of the
integration starts at once, after its own entities have restored comfort temperature, eco
offset and the comfort switches, so no setpoint from the defaults reaches the TRVs or the
learner. Until a room runs, its base temperature sensor is unavailable.

A TRV entity the hub writes to (external temperature, load balancing, mean load) that
comes back after being unavailable gets its value again at once.

### Hot-water charges

A heat pump charging its hot-water tank sends hot water to the tank, not the radiators,
while a shared leaving-water sensor reads its flow spiking. With the hub's measured flow and
the heat source's aim (curve plus offset, or the setpoint) the hub decides for all rooms and
the flow feedback alike: a charge starts when the flow is more than 6 K above the aim or
the hot-water device's tank detector starts one, and ends once the flow, after having been
that high, is back below aim + 3 K (a charge only the tank saw ends with the tank
detector). The tank reading comes late from the cloud: a tank start within 30 min after
the flow ended a charge belongs to that charge and starts none. Without flow or aim each room falls back to its own flow: more than 5 K above
the lowest one of the last hour, or the hot-water device's charge. Meanwhile the radiators' output is
computed with the heating flow from before the charge, not the hot reading; the observer lets the radiators cool down, the heat balance counts
no heating and the radiator check starts its hour over. A flow raised for more than an hour
is the new heating flow. While heating is unavailable the reference starts over; a heat
pump ramping up after such a pause may read as a charge for a while.

## Flow temperature and an unreachable heat source

The flow temperature comes from the required **flow temperature entity**, a sensor; the
heat source `climate` entity only supplies the operating state. This lets a template
sensor fall back to a calculated value (a heating curve, for instance) while the heat
source's cloud connection drops out, without the room losing its flow temperature.

The hub's *Heat source reachable* turns off once the flow temperature entity has had no
number for longer than the hub's flow gap (default 30 min). It is checked every minute, so
a cloud that repeats the same value still counts as reachable. Learning pauses earlier,
after 16 min without a value.

While the flow temperature entity is `unavailable` or `unknown` (for instance for a moment
while its template is reloaded), the room keeps the last flow temperature it reported, so a
reload does not switch the TRVs off and on. While the heat source entity is `unavailable`
or `unknown`, the room keeps its last known operating state: TRVs that were active stay active and are checked against the flow
threshold with whatever the flow temperature entity reports. A room that has not seen a
known state since startup stays inactive. If the heat source really stopped, the TRVs stay
open without heat coming — notify yourself about long outages from the flow sensor.

If the flow threshold and the fallback both come from the same heating curve, the
threshold check is fixed during an outage: with the threshold at the curve's warm end and
the outdoor temperature clamped to the curve range, the fallback never drops below the
threshold, so the room stays active. Learning does not see the difference between a
measured and a calculated flow temperature, so the hub should read the measured one; a
fallback template keeps the rooms controlled but lets learning continue on calculated
values.

## Multiple rooms sharing one heat source

Each room independently reports the flow it needs to hold its setpoint, and its supply
judged on its valves (see the card's "Supply" row). If your heat source's flow temperature
is shared across several rooms, the room asking for the highest flow temperature at any
moment is the one actually determining what the shared source needs to deliver — worth
keeping in mind when tuning any individual room's design values.

The hub sums this up over the **lead rooms**, marked per room by two checkboxes:

- **Lead room for the heating limit** — *Lead rooms hold flow temperature* is the highest
  gated hold flow among them; *Lead rooms heating demand* is on while it reaches the flow
  threshold (the threshold entity if linked, else the hub's fixed threshold). Rooms that
  live off their neighbours or are heated by something else are better left out, or they
  keep the heat pump running for nothing.
- **Lead room for the flow temperature** — the flow control takes their ungated hold flow
  as feedforward, their boost flow as a floor during a flow boost and their valve openings
  as feedback (see [Flow control](#flow-control)).

With an effective heating curve entity on the hub, *Flow reserve hold* reports the curve
minus *Lead rooms hold flow temperature*: negative means the curve does not reach what a
lead room needs. It is rounded to 0.1 K and unavailable while the lead flow is 0 (all lead
rooms in their surplus buffer).

### Supply per room

`sensor.<room>_supply` judges the flow on the room's valves with the same rules as the
flow feedback: **short** (`undersupplied`) when the feedback would step up for this room
(highest valve ≥ 98 % and the room at least 0.2 K short, or > 90 % and at least 0.3 K
short), **at limit** (`supplied`) when it holds the flow (≥ 80 % and at most 0.1 K
above target; deficits rounded to 0.1 K as for the feedback), **headroom** (`oversupplied`) otherwise: this room would get by on less
flow. The flow follows the neediest lead room, so headroom is the usual state of most
rooms, not a fault. It is **not rated** while the valves say nothing about the
flow; the `reason` attribute names why: window open, frost protection, no heat (heat
source off, flow below the threshold, hot-water charge), catching up after such a pause
(see *catch-up* under flow control), radiator not heating, boost, or
a valve opening that is unknown. Attributes `demand_pct` and `deficit_c` show what was
judged. Unlike the feedback it covers every room, lead room or not.

`sensor.<room>_supply_count` (flow lead rooms only, minutes) runs the feedback's count on
this room alone, for comparison; it has no effect on the flow. Same rules, capped at +60;
at −30 the room alone would have stepped up (attribute `would_step_up` counts those, kept
across restarts) and its count starts over. A real step or a reset starts every room count
over, a freeze drops what they have towards up, a room without a usable sample waits.

### Heating season

The hub sums up how many of the last 24 hours *Lead rooms heating demand* was on. The
duration, not the moment, decides: a cold morning hour does not start the season, a few
mild days do not end it.

- **Switch on** is recommended while the 24 h sum is above the switch-on threshold (6 h)
  and the heat source is not heating.
- Every minute the sum is above the hold threshold (2 h) stamps *Heating season last
  demand*. **Switch off** is recommended while the heat source heats and that stamp is
  older than the switch-off time (48 h).

Both are recommendations; switching the heat pump stays with you or your own automation. The
season card offers a hand switch (heat/off) for it.
The demand periods and the stamp survive restarts; the time Home Assistant is down counts
as no demand.

## Flow control

With a flow control sub-entry the integration owns the heat source's flow. Every minute it
works out:

- **Feedforward** = highest ungated hold flow of the flow lead rooms (what the learned
  model says they need at their setpoint).
- **Feedback** `feedback_k` (*Flow feedback*, K): what the valves say the model misses.
  The lead demand is the highest valve opening among the flow lead rooms, leaving out
  rooms with an open window, in frost protection, with a radiator that does not heat,
  in a flow boost or with an unknown opening.
  - **Count**, one per minute of what the valves say:
    - a room **wants more**: down. It wants more when it is above 90 % open and more
      than 0.2 K below its target (mode setpoint against the room sensor), or at 98 % or
      more and more than 0.1 K below it (or its room value is unknown); 0.1 K short is
      still the TRV's own control band. The deficit is compared rounded half up to
      0.1 K, the room sensors' resolution: 0.14 K short counts as 0.1, 0.15 K as 0.2. The room value is the room sensor only; while it is
      stale the TRVs' reading does not stand in, the deficit is unknown.
    - else a room **holds** the flow (80 % or more open, rounded at most 0.1 K above its
      target): back towards 0. A room rounded 0.2 K or more above its target does not hold: its
      valve is open on the TRV's own, colder reading, more flow would not help it.
    - else a room below 80 % open and not above its target (as for holding): up.
    - else (only rooms above their target: cooking, sun): the count stays.
    - A room that wants more counts the faster the further it is short: its rounded
      deficit over 0.2 K, at least 1× and at most 3× (0.4 K short: twice as fast).
    - At −30 minutes one actuator step **up**, at +60 minutes (at night +90) one step
      **down**; then the count starts at 0. After a step the count stays at 0 on that
      step's side for a lock (radiators and rooms answer over hours); the other direction
      stays open. Lock up 60 minutes, down 90; at night up 45, down 120 — a room short at
      night cools further until the morning, a step down at night has little use. Night
      is the heat-loss learner's: two hours after sunset until sunrise; the current time
      decides. A short want after a long surplus only takes minutes off, a
      long surplus first has to be worked off before a step up. Without any usable room
      the count waits. It starts at 0 after a restart.
  - Limited to ±3 K. No step that the target limits would cut off anyway.
  - **Frozen** (no step, no counting, what the count has towards up is dropped: after a
    hot-water charge or defrost the valves first catch up on the pause) while *Write
    flow* is off, the heat source does not heat or is unreachable, a hot-water charge
    runs, the measured flow is below the threshold (defrost), the lead flow is missing
    (fallback), a flow boost runs, or the last wanted value has not gone out (budget,
    hold time). The value survives restarts.
  - **Catch-up**: when a pause of the room heating ends (hot-water charge or defrost,
    one after the other counting as one pause), the rooms catch up on what cooled off.
    For as long as the pause lasted, at most 60 minutes, a room that wants more does not
    count down; a room that holds still brings the count back to 0, low demand still
    counts up. The supply of every room is *not rated* (`catch_up`) meanwhile. Not
    stored: a restart ends it. *Reset flow feedback* ends it too.
  - Button *Reset flow feedback* (flow device, config) sets it and the count to 0.
- **Target flow** = feedforward + feedback, at most feedforward + 6 K, the curve plus the
  highest offset (setpoint mode: the highest setpoint) and the radiators' design flow.
  A **flow boost** (a lead room recovering after its mode setpoint went up) raises it to
  that room's recovery flow; a planned hot-water charge can raise it further (up to
  *Flow raise for hot water at most* above that). Never below the flow threshold.
  Attribute `source` says which won: `lead`, `boost`, `charge`, `threshold` or
  `fallback`; further attributes `feedforward_flow_c`, `room`, `feedback_k`,
  `measured_correction_k`, `measured_correction_frozen_reason`, `boost_flow_c`,
  `boost_room`.
- *Flow feedback* attributes: `lead_demand_pct`, `lead_room`, `lead_deficit_c`,
  `feedforward_flow_c`, `frozen`, `frozen_reason`, `flow_timer_min` (the count, minutes; up at −30, down at +60, at night +90),
  `step_up_at_min` / `step_down_at_min` (where the count steps, the night's value at night),
  `night`, `locked_direction` (+1 / −1 while a step's lock holds that direction back, else 0),
  `locked_until` (when that lock ends),
  `catch_up_until` / `catch_up_reason` (end and cause of a running catch-up, else empty),
  `room_variant_up` / `room_variant_down` (steps a count per room would have taken: up when
  one room's count reaches −30, down when every rated lead room's count is at +60; kept across restarts),
  `last_change`. The lead is the room
  that decides: one that wants more, else the most open one that holds the flow, else
  the most open one.
- **Measured correction** `measured_correction_k` (offset mode): how far the heat source
  really lands off its effective curve (curve plus offset). Measured flow minus effective
  curve, smoothed with a time constant of 2 hours (the heat source reports whole degrees),
  limited to ±2 K. It learns only in calm operation: while the feedback may move (not
  frozen, see above), no catch-up runs, and 30 minutes after the last change of the
  actuator (also after a start). Otherwise it keeps its value;
  `measured_correction_frozen_reason` says why (`feedback_frozen`, `catch_up`,
  `settling`, `no_measurement`). The value survives restarts. Seen on a Daikin: below
  0 °C outdoor it lands about 1 K below its curve, around +5 °C slightly above.
- **Actuator value**: in offset mode target minus the heat source's curve at the current
  outdoor value plus the measured correction, in setpoint mode the target itself. Rounded
  up to the step, so the flow never falls below the target; clamped to the limits. The
  upper bound of the target moves with the correction too.

It writes only when all hold:

- *Write flow* is on (off = shadow: the values show, nothing is written);
- the unrounded need differs from the reported value by at least the hysteresis;
- the hold time (default 30 min) has passed since the last write;
- the hub's write budget allows it.

A cloud reports a write only with its next poll, so until then the old value does not
count as a change. A value someone else set (app, display) is left alone for one hold
time, then the control takes over again; attribute `external_change` on *Actuator target*
says when.

Exceptions to the hold time, written as required writes:

- **Fallback**: outdoor value missing (offset mode) or no lead room with a flow for
  5 minutes: the fallback value is written once, then kept. No estimate is made. The
  5 minutes cover a start or reload, when the rooms have no estimate yet; meanwhile
  *Actuator target* shows the fallback with reason `fallback_wait` and nothing is
  written.
- **Back from fallback**, and **entering or leaving a hot-water charge window**: the new
  value goes out at once.

Nothing is written while the heat source does not heat or is unreachable.

The flow threshold (heating demand, TRV release, lowest target) is, in offset mode, the
curve's warm end without offset. The offset is the control's own output: a threshold
moving with it would sink with the rooms' demand, and the heating demand would only
follow the outdoor temperature. In setpoint mode the threshold is a number on the device.

## Hot water

With a hot water sub-entry the integration owns the water heater's target.

**Target**, every minute, first match wins:

| Phase | When | Target |
|---|---|---|
| Legionella | cycle waiting or running | legionella number |
| Boost | boost switch on (off after the boost limit and on restart) | boost number |
| Surplus | surplus mode | surplus number |
| Charge window | inside the window | window number (heating season: heating window number) |
| Night | inside the night | night number (heating season: heating night number, night until its own end) |
| Normal | otherwise | normal number |

Surplus mode needs grid export above the lower threshold (or, once in surplus, above the
release threshold), battery and tank gates, and either outdoor above the summer limit or
export above the upper threshold stable for its time with outdoor above the lower limit.
Mode changes wait for the minimum hold time; a mode chosen by hand holds for the manual
hold time, then automatic takes over.

**Heating season**: while the heat source heats (its climate in `heat`), the charge window
and night take their heating-season numbers and the night lasts until *Night until, heating
season*: one main charge in the window, otherwise only a safety reheat.

**Forced charge**: a heater that reheats only below target minus its hysteresis would not
start for a target just above the tank. Legionella, boost, surplus and the charge window
therefore ask at least tank + hysteresis + 1 (capped at the highest target), and keep a
target already above their own so it does not climb with the tank.

The target is written only when it differs from the heater's. A cloud shows it with its
next poll, so the same value is not sent again for 15 minutes. Surplus switching is a
normal write the budget may drop; all other targets are required writes.

**Charge detection**: the heat source's cloud has no "hot water active" signal. A charge
counts as running from a write that puts the target above tank + hysteresis (15 min), and
while the tank rose at least 1 K between polls in the last 15 minutes. Rooms treat it like
the flow-based detection: no heating power during the charge.

**Legionella cycle**: on the scheduled weekdays it starts at the start time when the PV
forecast condition holds, else at the fallback time regardless. It is skipped when the
outlet is already within 3 K of its threshold, or a cycle started that day. Once the
heater reports the target it runs: outlet at least its threshold, circulation return at
least its threshold for the hold time (restarting when it drops). Done when both hold;
failed after the verification timeout, or when it could not start before the last retry
window ended. A cycle survives a restart. The circulation gets a continuous-run request
when it starts and loses it when it fails.

**Circulation**: evaluated on every change of its sensors and every 15 seconds, by the
rules of the thermostatic blueprint, behind a main switch (off: pump off, nothing else
evaluated, also no continuous run of a legionella cycle, whose return check then fails):
continuous run (released once the return held the
release temperature for the release time), blocked heater states and presence/day-mode
gates, a fixed interval without return value, stop when hot or after the maximum run,
start after the minimum pause when the loop cooled below the peak or off-peak threshold
or on a draw. The reason sensor keeps the reason of the last switch while
the pump stays in that state; *unknown* only after a start or when the pump was switched
from outside.

## How the hold and boost flow are computed

Each room computes two flow requirements:

- **Hold** (`sensor.<room>_hold_flow_temperature`) — the flow needed to hold the setpoint
  at the current outdoor temperature (see the surplus gate below).
- **Recovery** (`sensor.<room>_boost_flow_temperature`) — only after the mode's setpoint
  went up by at least 0.5 K (not after a window closed), until the room is within 0.2 K
  of it or six hours have passed: the lowest flow that brings it back within six hours
  with its valves fully open. The sensor reads 0 outside a flow boost; the recovery flow
  is still shown there as attribute `recovery_flow_temperature_c` but not asked for, the
  valve feedback of the flow control covers a room that stays short. It is found by
  simulating the room with the same
  radiator, heat-loss and capacity model the observer uses, including the learned
  factors. A room within 0.1 K of its setpoint counts as there, and a room whose outdoor
  temperature is above its setpoint asks for no recovery at all.

Both take the radiator output at the design spread of the configured system (mean water
temperature = flow − 5 K for 55/45), the same assumption as the valve power, the heat balance
and the jump stretches. The learned heat-loss factor fits that assumption, so the hold flow
is the flow at which the open valves give off exactly the modelled loss. An earlier version
shrank the spread with the load (design spread × power / heat load) for the flow requirement
only; with learned factors well below 1 that read about 4 K below the flow a room was seen to
need. The radiator check's 30 % gate and the share a partly open valve lets through still
come from the radiator's own balance (see [Valve position](#valve-position-the-heat-the-radiators-really-get)): they ask how
warm the water at the valve is, not how much heat the model books.

Neither depends on the measured flow temperature, so a stale reading or a hot-water charge
cannot move the requirement. Six hours is deliberately slow: a heat pump recovers more
efficiently at a lower flow over a longer time, and the shared source serves whichever room
asks for the most, so one slightly cool room must not pull the whole house up.

The search runs from 10 K below the room's **flow threshold** up to the flow temperature of
the configured **design temperature system** (55 °C for 55/45). A requirement below the
threshold stays visible as such rather than being rounded up to what the source delivers at
minimum. If even the design flow cannot bring the room back within six hours, the
recovery flow reads the design flow and the `recovery_flow_saturated` attribute is set.

The flow threshold is not a number in the room's configuration but an entity linked in the
**Sensors and signals** step: an `input_number` helper, a `number` entity, or a `sensor` a
heat source publishes it on. The rooms read it live, so trying out a different lower limit
on the heat pump means changing one helper instead of reconfiguring every room. Without a
linked entity, or while it reports nothing usable, 30 °C applies.

Once the flow has reached the threshold, heat counts as available until the flow has been
below it for 20 minutes without a break. A defrost drops the flow by about 10 K for one
report; without the tolerance every room would shut and reopen. The heat source leaving
`heat` ends it at once, and before the flow first reaches the threshold no tolerance applies.

## The surplus gate

The hold flow is computed at the setpoint, not at the room's actual temperature — so on its
own it would keep reporting a requirement for a room sitting well above setpoint on stored
heat, typically after a warm spell in spring or autumn. For the heating limit the surplus
gate suppresses it in exactly that case. It closes only when both signals agree:

- the TRVs report their valves shut (the room's valve opening at 0 %), **and**
- the room is above its current target by more than **"close above setpoint"**

While a TRV that reports its opening has been silent for 75 minutes the gate stays as it is:
its valve may well be open.

Both are needed. Demand alone also reads 0 % for a room sitting exactly at setpoint, which
is precisely where the floor must stay.

Closing waits for **"hold time"** to pass with the condition continuously true, because
closing withdraws a heat requirement and can be what keeps a heat source from starting.
Reopening has no such delay and happens on the first cycle where demand appears or the
surplus falls below **"reopen below setpoint"** — restoring a requirement is the safe
direction. The two different thresholds are what stops a room drifting around the
deadband from flapping the requirement on and off.

The gated hold is exposed as `sensor.<room>_gated_hold_flow_temperature` (attribute
`flow_gate_closed`). It is the signal for deciding whether the heating season is on: it
rises with the weather, drops to 0 while a room coasts on stored heat, and ignores a room
that is merely below setpoint after airing.

Because the gated value reads 0 for much of the shoulder season, the underlying number
stays available separately as `sensor.<room>_hold_flow_temperature`. It reports the
ungated hold requirement — what the room would need to hold its setpoint at the current
outdoor temperature, regardless of stored heat — and keeps accruing long-term statistics
throughout. It depends only on the setpoint and the outdoor temperature, which makes it
the useful one for judging where your building's heating limit actually sits; the gated
entity is the one to act on.

## Setting the heating curve

With a flow device, every room also reports its hold flow at the two outdoor end points
of the heating curve (*Hold flow temperature at curve cold/warm end*, diagnostic): the
same calculation as the hold flow, at the room's comfort temperature instead of the
current mode's, with the learned heat-loss factor. Attributes: the outdoor value, the
target, `ua_factor` and `ua_last_learned` — a factor that has not been learned recently
makes the point provisional.

The flow device turns these into *Heating curve recommendation cold* / *warm*: the highest
value among the flow lead rooms plus the flow feedback `feedback_k` (what the valves say
the model misses). Attributes `room`, `rooms`, `feedback_k`,
`current_curve_flow_c` (the curve number of that end) and `difference_k` (current minus
recommended; positive means the curve is set higher than the lead rooms need). Read it
before changing the heat source's curve:

- Gains are left out, so the points err high — the safe side for a curve that also has to
  hold through the night.
- The model uses the design spread at every load, so a radiator needs about half the
  spread above the room before it gives off anything. The warm end therefore cannot drop
  far below room temperature plus half the spread, even when the load there is tiny.
- The model's need is curved (steeper at the warm end); a straight curve through both
  recommended points sits slightly below it in between. The raise around 0 °C or a
  slightly higher warm end covers that.
- In offset mode the warm end is also the flow threshold: lowering it moves the heating
  limit with it.

*Hold flow curves* (flow device, diagnostic) carries every room's comfort hold flow over
the curve's outdoor range, 2 K beyond each end, in whole degrees: attributes `outdoor_c`,
`rooms` (`name`, `lead`, `flow_c`), `feedback_k`, `lead_demand_pct`, `lead_room` and the
current `outdoor_temperature_c`. Its state is the lead rooms' highest hold flow plus
feedback at the current outdoor value. Curves and state are not stepped up to the control's 0.5 K
grid (0.1 K instead), so the curves come out smooth; the control itself still steps up.
The curves stay out of the recorder. The heating curve card draws them (see
[`card-design.md`](card-design.md#heating-curve-card)).

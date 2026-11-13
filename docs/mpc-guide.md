# The heating model (MPC)

Each room predicts how its own temperature responds to heating and adjusts its radiators'
target temperature accordingly, rather than just reacting to whether the room is currently
above or below setpoint. This page covers what to expect from that in day-to-day use — for
what the fields of the hub and the rooms actually do, see below.

## Modes and setpoints

- **Comfort** — the room's normal target temperature (the comfort-temperature slider on the
  card).
- **Eco** — comfort temperature plus the eco offset (normally negative, e.g. −2 °C) — a
  lower setback target, not a separate independent setpoint.
- **Boost** — comfort temperature plus a fixed boost offset, for a temporary push above
  normal comfort.
- **Frost protection** — a fixed floor temperature, independent of the comfort setpoint.
  This one is also **enforced automatically**, regardless of the selected mode, whenever a
  configured window contact in the room is open or its TRVs report as inactive — you'll see
  "Frost protection enforced" in the card's header, and it clears itself as soon as the
  window closes or the TRVs are active again. No action needed on your part.
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
| `learned` | The last balance moved the factor 30 % towards its target. |
| `learned_at_limit` | The target lay below 0.15: the radiators supplied almost nothing and the room lives off its neighbours. The factor moved towards 0.15 only. |
| `collecting` | A stretch is running. |
| `coasting` | The room is more than 0.1 K above its setpoint or below its target band; the stretch starts once it is back or has held its temperature for an hour. |
| `outside_window` | Sun condition not met. A room still moving after sunrise reports this, not `coasting`. |
| `paused` | Learning enabled entity off. |
| `too_short` | The stretch ended with fewer than four usable hours. |
| `gap` | A sample gap above 20 minutes. |
| `drift` | The trend over the stretch moved the room more than 0.3 K. A stretch that started on a still room away from its setpoint is first retried from where the room came near its setpoint, if four hours are left. |
| `small_temperature_difference` | Room on average less than 8 K above outside. |
| `storage_dominated` | The heat stored in or released by the room was more than half of the radiator heat. |
| `implausible_target` | The balance pointed at a factor above 2. |
| `room_sensor_fallback` | The configured room sensor is stale or unavailable and the TRVs stand in. |
| `valve_position_missing` | A linked valve position entity is stale or unavailable. |
| `trv_adapting` | A TRV calibrates its valve and moves it by itself, has no calibrated valve (adaptation status other than found, e.g. `none` after mounting or `lost`), or is in mounting mode. |
| `radiator_not_heating` | A radiator stays cold although its valve reports open, see [Radiator check](#radiator-check). |
| `outdoor_temperature_missing` | The outdoor temperature is missing and the TRVs hold the target on their own, see [Sensor reliability](#sensor-reliability). The stretch before the outage is balanced; the next one starts once the value is back. |
| `valve_saturated` | For more than half the stretch every valve was at least 95 % open with the room below its target band. The heat assumed for a full opening is the least certain (a flow-limited radiator gives off less), so the factor is left alone; the target is kept for diagnostics. |
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

Adjustments are deliberately gradual — expect the model to sharpen over real heating days,
not to converge instantly. None of these states need any action from you;
they're diagnostic, useful mainly if a room's behavior seems off and you want to see whether
it's actively adapting.

## When the model learns

The two factors are learned in different ways, because a wrong heat-loss factor hardly shows
as a prediction error: the controller keeps the room at the setpoint its prediction aims
for, whatever the model believes the losses are.

- **Heat-loss factor (`ua_factor`): heat balance over a usable stretch.** Over the
  stretch the heat the radiators gave off (the requested power, lagged as described in
  [Radiator lag and the room observer](#radiator-lag-and-the-room-observer)) has to equal
  what the room lost plus what it stored. The factor that balances this is the target, and each balance moves the learned
  factor 30 % of the way there. A stretch starts once the room is near its setpoint — no more than 0.1 K above it or
  below the target band; a room still coasting down on the evening's heat with the valve
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
  count as stored heat. Samples taken during a hot-water charge (see
  [Hot-water charges](#hot-water-charges)) count as no heating. Each
  stretch is used once. Steady gains during the window (people, appliances,
  neighbouring rooms) end up in the factor — that is what the room needs in operation.
  Each balanced stretch is also a point for the night regression that separates a steady
  gain from the factor over several nights, see [Gains and sun](#gains-and-sun).
  Two checks keep measurement artefacts out: a stretch where the heat stored in or
  released by the room exceeds half of the requested heat depends more on the capacity
  than on the losses and is discarded, and so is a target above 2. A target below 0.15
  means the radiators supplied almost nothing — the room lives off its neighbours, for
  whom the model has no coupling: the factor then moves only down to 0.15 (status
  `learned_at_limit`, the measured target stays in `last_target_factor` of the UA learning status).
  A stretch rejected for drift reports the
  drift in `last_drift_c`.
- **Capacity factor (`capacity_factor`): jump stretches.** A held room hardly moves, so
  the capacity is learned where it does: after the setpoint jumps by at least 0.5 K
  (comfort/eco switch, end of frost protection, a window closing). The stretch starts at
  the jump — or once a window closed or the configuration changed 10 minutes ago — and
  ends when the room comes within 0.2 K of the new setpoint, the setpoint changes again by
  0.1 K or more, a flag from the table above appears, or after three hours. Over the
  stretch the energy balance gives the net heat: the heat the radiators gave off minus the
  loss with the current heat-loss factor (mean room against outdoor, per interval).
  Divided by the room's change and the base capacity it is the estimate. A heat-up stretch
  moves the factor 30 % of the way to it. A cool-down stretch only reports its estimate:
  the gains, unknown here, are a larger part of its balance. Stretches are judged with
  heating off as well, so a room cooling down in frost protection shows its estimate too.
- **Sun.** The heat-loss balance does not run while the sun is up or within two hours after
  sunset, when walls still release the afternoon's solar heat. Sunrise and sunset are
  calculated from the location configured in Home Assistant. A jump stretch only counts if
  the irradiance on each of the room's window surfaces stayed at or below 50 W/m² during the
  stretch and the two hours before it, so it can be learned at midday on an overcast day.
  Without irradiance data the sun condition decides at start and end; a room without window
  surfaces skips the check.
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
  reset_valve_curves: true
```

A factor that is left out stays as it is, so a reset needs an explicit `1.0`; at least one
value (or a reset) is required. The call discards the heat-loss stretch and any jump
stretch in progress, because they were measured with the old factors. `reset_gains: true` forgets the
learned steady gain, day profile and sun terms (see [Gains and sun](#gains-and-sun)).
`reset_valve_curves: true` forgets the samples of every TRV's valve curve in the room, also
one still open; the profile's starting value applies until new bins fill (see
[Valve curve](#valve-curve)) — for instance after a change to how samples are taken, a new
pre-setting or a hydraulic change. The reset is written at once.

## Gains and sun

Heat the radiators do not supply — people, cooking, appliances, neighbouring rooms, the sun —
is learned on two levels. **At this stage it is only learned and shown; the control, the
hold and the minimum flow temperature do not use it yet.** Both levels use recursive least
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
straight line through the hour), minus the radiator heat, plus the loss, minus `G`. A second
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

If a room's minimum flow temperature (shown on its card) frequently reads as "undersupplied
⚠" even once the model has had time to learn, its radiators are likely genuinely
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

The capacity also drives the valve demand and the recovery flow temperature: a lighter
room reaches its setpoint faster in the prediction, so the controller asks for less
preheating and backs off earlier. After changing a room's area or construction, watch for
overshoot or demand that switches back and forth before tuning the demand smoothing below.

## TRV target mode

The controller's demand is the **target valve opening** (sensor `heating_demand`, named
"Target valve opening"), and it turns that into a target temperature for each TRV; the TRV
opens its valve by its own reading at the radiator. **Relative** (default) sets the target a
step above that reading. A TRV with a valve curve (see [Valve curve](#valve-curve): its
learned curve, until then the profile's starting value) gets the offset its curve needs for
the target opening —
every TRV of the room the same opening, whatever its radiator's size — rounded to the
nearest step of the TRV, but at least the first step above reading plus 0.1 K. Rounding up
instead moved a small offset by up to a whole step, and a Danfoss Ally already opens about
45 % at 0.5 K. Only a TRV with neither (no valve entity, or a TRV model whose profile has no
starting value and that has not learned its curve yet) falls back, as a last resort, to a demand-dependent step weighted by its
radiator's share of the room's power — 0.1 K at the smallest active demand up to 3 K at full
demand, rounded up to the TRV's step — so a small demand opens the valve only partly.
(A Danfoss Ally freshly opened about 55 % at 1.1–1.5 K above its reading and about 90 % from
2.4 K on.)
The current target is kept while it is still above the reading and within one step of the
new one, since every write wakes the TRV. **Absolute** spreads the target over the TRV's
range (18 °C up to its maximum) by demand; there even a few percent of demand ask for 20 °C
or more and the valve opens almost fully. A TRV without a reading falls back to absolute.
The mode is set on the hub for all rooms; the attribute `trv_setpoint_writes` of the
target valve opening sensor counts the target writes per TRV since setup, and its attribute
`trv_valve_positions` lists the opening each TRV reports right now (`pi_heating_demand`,
`null` without a valve-position entity). The recorder keeps only `trv_setpoint_writes`; the
history of targets and openings is on the TRV entities.

Every write wakes the TRV, so a valve switches between shut and open sparingly: a shut
valve opens from 10 % target opening on, an open one stays open down to 5 %, and a TRV switches
at most every 15 minutes — except when frost protection is forced (open window, heat
source off), which shuts it at once. On the valve curve these openings are much more heat
than their number suggests: at a 40 °C flow 10 % opening gives about 44 % of full output,
5 % about 27 %. The planner therefore never plans an opening below the threshold that
applies — while any TRV of the room is shut, nothing between 0 and 10 % — since the TRV
would get its minimum target and none of the planned heat. A room whose hold opening lies
below the threshold thus alternates between shut and the threshold opening. On top, the
TRV's 0.5 K step and the 0.1 K floor above its reading make even a small offset open it
noticeably, so a room near its hold opening may still pulse in a PWM-like way; worth
watching in the live evaluation.

While a TRV calibrates its valve (adaptation run, reported by the device profile's
adaptation status entity) nothing is written to it, neither target nor heat-available
switch: a Danfoss Ally aborts the run on any write. The hold ends with the run, after three
hours at the latest (a run took about 1 h 50 min), and forced frost protection still goes
through. As the TRV moves its valve by itself meanwhile, the room learns nothing then (status
`trv_adapting`). The room learns nothing either while a TRV has no calibrated valve — its
profile's ready status does not match, as a Danfoss Ally reports `none` from mounting until
its next adaptation run and `lost` after losing the curve — or while it is in mounting mode
(profile's mounted-mode binary sensor on). Writes go on then: an uncalibrated TRV still has
to heat.

## Radiator check

Air in a radiator or a valve stuck shut looks the same to the controller: the TRV reports
its valve open, yet the radiator stays cold. Per TRV, once the valve has reported at least
90 % for an hour with heating available and a flow warm enough for the fully open radiator
to give off at least 30 % of its design output (from the design system and the radiator's
exponent; a 55/45 radiator in a 21 °C room needs about 34 °C), the radiator counts as not
heating if over that hour the TRV's reading neither rose by 0.3 K nor sits 1 K above the
room, and it never warmed during that opening. The rise counts from the lowest reading of
the 15 minutes before the valve opened: a radiator that gets water warms its TRV within one
controller cycle. A room that rose by 0.3 K over the hour holds off the verdict for that
hour as well — where a radiator fan blows room air past the TRV, it reads close to the room
even next to a warm radiator — but unlike a warm TRV it does not clear the whole opening:
sun or a neighbour may lift a room too, and room sensors step in 0.1 K. Below that flow even
a sound radiator stays barely warm, so the hour starts over. A hot-water charge (see
[Hot-water charges](#hot-water-charges)) starts the hour over. A verdict needs a cold start:
the valve at no more than 15 % for at least 30 minutes, and the opening within 15 minutes of
that. A TRV already warm from a partly open valve shows neither rise nor lead at a low flow,
so such an hour stays without a verdict. A radiator whose valve never closes is not judged.
The verdict holds while the valve is shut and clears as soon as an open valve warms the TRV
again or the room rises by 0.3 K over the hour.

Meanwhile the room learns nothing (status `radiator_not_heating`). The running heat-balance stretch ends where the valve opened for the hour that led to
the verdict: that hour's heat is in doubt as well. `binary_sensor.<room>_radiator_not_heating` (problem) is on, its attribute
`trv_entities` names the TRVs: an automation can send a notification to check — bleed —
the radiator.

## Valve position: the heat the radiators really get

A TRV is a thermostat, not a valve positioner: the controller sets its target temperature,
and the TRV opens its valve by its own reading at the radiator. A small demand can thus open
a valve almost fully. Where the TRV's device profile names a valve opening entity (Danfoss
Ally: `pi_heating_demand`), the observer, the heat balance and the jump stretches then use the heat the radiator gives
off at that opening instead of the requested power (sensor `measured_heating_power`).

The opening is taken as proportional to the flow. With less water the radiator cools it
further and its output stays high: at a 40 °C flow a 10 % opening still gives about 44 % of
full output, 50 % about 87 %. A TRV without such an entity counts with the demand, linearly.
The planner uses the same curve: it searches the target opening, and the power it expects
at an opening (sensor `requested_heating_power`) is what this curve gives there. Planned as
a linear share of the available power, a small demand opened the valves far more than
planned (a bathroom at 15 % demand got 50 % opening and 130–190 W instead of about 32 W)
and the room cycled hourly. If the entity goes stale or
unavailable, the room's learning stops (status
`valve_position_missing`) until it reports again.

## Valve curve

Each TRV that reports its valve opening learns how far the valve opens for a setpoint
offset (setpoint minus the TRV's own reading). The Danfoss PI behind it differs from valve
to valve — pre-setting, insert, how the TRV sits — so one curve per TRV. In relative mode the
control sets each TRV's offset from its curve (see [TRV target mode](#trv-target-mode)), read
anew on every calculation: the learned curve once `learned`, until then — and for a
`scattered` TRV — the profile's starting value. The planner counts in openings, and the
starting value maps them to offsets far closer than the power-weighted fallback, which only
a TRV without starting value and learned curve gets. One or two learned bins are not merged
in before `learned`: they say nothing about the rest of the range.

A sample starts whenever the TRV reports a new setpoint: the offset is that setpoint minus
the TRV's reading at that moment. Only a heating offset of the relative mode starts one —
0.1 to 4.0 K (the largest relative offset of 3 K plus a rounding step, with margin) and a
setpoint above the TRV's minimum target. An off write (the minimum target, some 15 K below
the reading), an absolute or eco jump starts none and ends any open sample. A Danfoss
Ally ramps its valve after a new setpoint, about +10 % every 5 minutes for up to half an
hour, so the sample waits for the ramp to end: from 10 minutes on the opening is read
every 5 minutes (a reading up to 30 s early counts), and the first one within 2
percentage points of the reading before is the sample — at the earliest 15 minutes after
the setpoint. A sample is dropped if another setpoint comes first, if the valve still
moves 45 minutes after the setpoint, if the opening is missing, or if at any cycle
meanwhile the TRV adapted its valve, had no calibrated valve or was in mounting mode, a hot-water charge ran, a window was open, the radiator check
flagged this radiator, frost protection was forced, or the valve entity reported nothing.
Preview calculations feed nothing.

Samples go into bins 0.5 K wide; each keeps its last 20. A bin counts from 3 samples, its
point is the median offset and the median opening. The curve is the learned points; points
of the profile's starting value (`valve_characteristic_seed`, see the README) fill in only
more than 0.25 K below or above the learned ones, and the openings are made monotone.
Without a starting value and no learned point from 2.5 K on, 3.0 K counts as fully open.

| Status | Meaning |
|---|---|
| `seed` | No bin has 3 samples yet: the starting value (if any) is the curve. |
| `learning` | Bins count, but fewer than 3 or none at 2.0 K or more: still the starting value (if any); the bins are not merged yet. |
| `learned` | At least 3 bins count, one of them at 2.0 K or more: the learned points, the starting value filling in beyond the learned span. |
| `scattered` | A bin with at least 5 samples spreads more than 30 percentage points (p90−p10): the TRV's PI integrates too much for a fixed curve, so the starting value (if any) is used. |

Without a starting value a TRV has no curve until `learned` (and none while `scattered`); it
then gets the weighted fallback.

The diagnostic sensor `sensor.<room>_valve_curve` (the entity id follows the HA language:
on a German install `sensor.<raum>_ventilkennlinie`) counts the TRVs with status `learned`. Its
attributes name each TRV with a valve entity, with `status` and `bins`
(`[offset °C, opening %, samples, spread %]`). One delayed write per room bundles the samples
that finish within 10 minutes; a pending one is written at once when the room unloads
(reload, removal) and when Home Assistant shuts down, so finished samples survive a restart. A sample still open at the restart is lost.
`heating_controller.recalibrate` with `reset_valve_curves: true` clears them (see
[Recalibrating](#recalibrating)).

## Radiator lag and the room observer

A radiator does not stop heating when its valve closes: the water and steel in it keep
giving off heat for a while, and after the valve opens it takes a while to warm up. The
model follows the requested power with a first-order lag whose time constant comes from the
radiator itself — water content and steel mass from panel type and dimensions (towel
radiators from their nominal power) over the emitter's conductance at the current flow
temperature. Cooling down only goes through the surface; heating up the inflowing water
helps, scaled by the demand. A 1 m type 22 radiator at 600 mm cools off over about 20
minutes at a 40 °C flow and warms up in 5–10.

Each room runs its model alongside the room as a state observer (sensor
`radiator_heating_power`, attribute `estimated_temp_c` of the room temperature sensor):

- Every calculation propagates the estimate with the power the radiators were heading for
  (from the valve opening where reported, otherwise the requested power) and pulls it
  towards the measured temperature (time constant 5 minutes). A difference above 1 K is
  taken as a jump — a sensor source switching over — and the estimate restarts from the
  measurement.
- A lasting difference between model and room is learned into the heat-loss factor; a
  separate offset term could not be told apart from it at a near-constant room-outdoor
  difference.
- The valve demand is planned from the estimate and the radiators' current output, so a
  room whose radiators are still hot closes earlier. The demand that holds the setpoint is
  the valve opening at which the radiators give off the loss at the setpoint. The hold and recovery flow
  temperatures use the lag as well; the flow requirement stays weather-driven.

## Target band

Each cycle the controller tries every target valve opening from 0 to 100 % in 5 % steps and
predicts the room 30 minutes ahead, with the power the radiators give off at that opening
(see [Valve position](#valve-position-the-heat-the-radiators-really-get)). A prediction between the setpoint minus the **target band** (default
0.1 K, one step of the usual room sensor) and the setpoint counts as on target; among
those, the demand nearest the one that holds the setpoint wins. Below the band the
demand that comes closest wins, above the setpoint the lowest one.

Without the band a heavy room with little radiator reserve at a low flow temperature
cannot climb the last 0.1 K within 30 minutes at any demand, and the valve stayed fully
open for hours over one sensor step. With it the room settles within the band, a few
hundredths to a tenth of a kelvin below the setpoint. A band of 0 aims at the exact
setpoint. The band is set on the hub for all rooms.

## Demand smoothing

Three settings in the hub's "Control and design" step keep the requested
heating power from chattering cycle to cycle:

- **Demand hysteresis (%)** — a change smaller than this from the last requested value is
  ignored.
- **Hold time (s)** / **hold override demand (%)** — once demand has changed, further
  changes are held back for this long unless they're large enough (past the override
  threshold) to cut through immediately regardless of the hold.
- **Max demand step per 5 minutes (%)** — caps how much requested demand can rise or fall
  per 5 minutes, even for a large, genuine change — smooths out the transition instead of
  jumping straight there. The cap scales with the time since the previous calculation, so
  several calculations within seconds (state changes arrive in bursts) allow only a
  proportionally small step in total; the first calculation after a start allows the full step.
  After a start (HA restart, integration reload) the steps begin at the valve opening the
  TRVs report — their mean, if the room has several — not at 0: the TRVs still hold the
  previous target, and ramping up from 0 would first write a low target and shut a radiator
  that was open. A report that arrives within 10 minutes of the start still seeds the ramp;
  without one it starts at 0.

The smoothed demand never stops between 0 and the valve's open threshold (10 % while a TRV
of the room is shut, 5 % while all are open, see [TRV target mode](#trv-target-mode)): the
TRV would get its minimum target there while the model counted the valve open. Rising, the
output stays at 0 until the stepped value reaches the threshold; falling below it, the output
goes to 0.

Larger values make a room's demand steadier but slower to react; smaller values make it more
responsive but more prone to small back-and-forth adjustments.

Smoothing only applies in normal operation. While frost protection is forced (a window is
open, or the heat source is unavailable), the demand is applied at once, so an opened window
closes the valve on the same calculation instead of stepping down over the hold time. A room
below the frost protection temperature still gets heat. Once the forced mode ends, smoothing
resumes from that demand.

## Sensor reliability

Room and outdoor temperature readings are checked against **max sensor age (s)**: once a
reading is older than that, it's treated as unavailable rather than trusted indefinitely, and
the room pauses its calculation until a fresh reading arrives. The age counts from when the
room last read the entity, not from its `last_changed`: every room reads all its inputs
every 5 minutes, so an outdoor sensor that reports whole degrees and keeps one value for
hours stays fresh. An entity that turns `unavailable` or `unknown` counts as missing at once.
Implausible single-sample jumps are also filtered rather than acted on immediately, so one
noisy reading doesn't swing a room's demand; a jump that repeats over a few polls is
accepted as a real change.

The flow temperature is exempt from that filter. Its jumps are physical —
compressor start, defrost, or a hot-water charge showing up on a shared leaving-water sensor
— and it only feeds the valve demand, never the minimum flow temperature (see below).

Without an outdoor temperature the model cannot plan. The room then hands control to its
TRVs: each gets the target of the active mode (comfort, eco, boost, frost protection, plus a
PV boost if on) as an absolute setpoint, rounded to its step, and regulates on its own
reading. Learning pauses for both factors (status `outdoor_temperature_missing`) and the
planned values (demand, powers, flow requirements) read
unknown. `binary_sensor.<room>_outdoor_temperature_missing` (problem) is on meanwhile, with
the attributes `since` and `fallback_target_temperature_c`. Once the outdoor temperature is
back, the MPC takes over on the next calculation; the heat balance starts a new stretch from
then. Feed the outdoor entity a measured value or nothing: an estimate during an outage would
be learned from as if it were measured.

Right after startup the outdoor entity and the heat source may not have reported yet. Until
the outdoor temperature has been read once, or has been missing for a full poll cycle (5
minutes), the room hands the TRVs no fallback and the binary sensor stays off; learning
pauses all the same. Likewise, until the heat source entity has reported a known state
*and* a valid flow temperature has come in, or for at most those 5 minutes, the room does
not count as unheated: it forces no frost protection, writes no TRV target and leaves the
heat-available switches alone. The mode shown meanwhile reads frost protection. Frost
protection for a real reason — an open window, a mode the user picked — still applies at
once, only the switches wait. If the heat source is still unknown after the 5 minutes, the
room forces frost protection as before.

### Hot-water charges

A heat pump charging its hot-water tank sends hot water to the tank, not the radiators,
while a shared leaving-water sensor reads its flow spiking. A flow more than 5 K above the
lowest one of the last hour counts as such a charge. Meanwhile the valve demand is planned
with the heating flow from before the charge, so the valves do not close on the hot reading;
the observer lets the radiators cool down, the heat balance counts
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

Each room independently reports its own required minimum flow temperature (see the card's
"Minimum flow temperature" row). If your heat source's flow temperature is shared across
several rooms, the room asking for the highest flow temperature at any moment is the one
actually determining what the shared source needs to deliver — worth keeping in mind when
tuning any individual room's design values.

The hub sums this up over the **lead rooms**, marked per room by two checkboxes:

- **Lead room for the heating limit** — *Lead rooms hold flow temperature* is the highest
  gated hold flow among them; *Lead rooms heating demand* is on while it reaches the flow
  threshold (the threshold entity if linked, else the hub's fixed threshold). Rooms that
  live off their neighbours or are heated by something else are better left out, or they
  keep the heat pump running for nothing.
- **Lead room for the flow temperature** — *Lead rooms minimum flow temperature* is the
  highest minimum flow (recovery included) among them, the flow the heating curve should
  reach.

With an effective heating curve entity on the hub, *Flow reserve hold* and *Flow reserve
minimum* report the curve minus each value: negative means the curve does not reach what a
lead room needs. Both values are rounded to 0.1 K; the reserves are unavailable while the
lead flow is 0 (all lead rooms in their surplus buffer).

### Heating season

The hub sums up how many of the last 24 hours *Lead rooms heating demand* was on. The
duration, not the moment, decides: a cold morning hour does not start the season, a few
mild days do not end it.

- **Switch on** is recommended while the 24 h sum is above the switch-on threshold (6 h)
  and the heat source is not heating.
- Every minute the sum is above the hold threshold (2 h) stamps *Heating season last
  demand*. **Switch off** is recommended while the heat source heats and that stamp is
  older than the switch-off time (48 h).

Both are recommendations; switching the heat pump stays with you or your own automation.
The demand periods and the stamp survive restarts; the time Home Assistant is down counts
as no demand.

## Flow control

With a flow control sub-entry the integration owns the heat source's flow. Every minute it
works out:

- **Target flow** = *Lead rooms minimum flow temperature* + margin (default 1 K). A planned
  hot-water charge can raise it further (up to *Flow raise for hot water at most* above
  that). Never below the flow threshold. Attribute `source` says
  which won: `lead`, `charge`, `threshold` or `fallback`.
- **Actuator value**: in offset mode target minus the heat source's curve at the current
  outdoor value, in setpoint mode the target itself. Rounded up to the step, so the flow
  never falls below the target and the margin stays whole; clamped to the limits.

It writes only when all hold:

- *Write flow* is on (off = shadow: the values show, nothing is written);
- the unrounded need differs from the reported value by at least the hysteresis (so the
  margin can shrink by at most the hysteresis before a write);
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
| Charge window | inside the window | window number |
| Night | inside the night | night number |
| Normal | otherwise | normal number |

Surplus mode needs grid export above the lower threshold (or, once in surplus, above the
release threshold), battery and tank gates, and either outdoor above the summer limit or
export above the upper threshold stable for its time with outdoor above the lower limit.
Mode changes wait for the minimum hold time; a mode chosen by hand holds for the manual
hold time, then automatic takes over.

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

## How the minimum flow temperature is computed

The minimum flow temperature is the larger of two requirements:

- **Hold** — the flow needed to hold the setpoint at the current outdoor temperature (see
  the surplus gate below).
- **Recovery** — for a room below its setpoint, the lowest flow that brings it back within
  six hours with its valves fully open. It is found by simulating the room with the same
  radiator, heat-loss and capacity model the valve demand uses, including the learned
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
requirement reads the design flow and the `recovery_flow_saturated` attribute is set. The
recovery part on its own is exposed as `recovery_flow_temperature_c`.

The flow threshold is not a number in the room's configuration but an entity linked in the
**Sensors and signals** step: an `input_number` helper, a `number` entity, or a `sensor` a
heat source publishes it on. The rooms read it live, so trying out a different lower limit
on the heat pump means changing one helper instead of reconfiguring every room. Without a
linked entity, or while it reports nothing usable, 30 °C applies.

## The surplus gate

A room's minimum flow temperature has a floor: the flow needed to *hold* its setpoint at
the current outdoor temperature. That floor is what keeps the source from dropping its
flow the moment a room reaches its setpoint, which would only cool it down again.

The floor is computed at the setpoint, though, not at the room's actual temperature — so
on its own it would keep reporting a requirement for a room sitting well above setpoint on
stored heat, typically after a warm spell in spring or autumn. The surplus gate suppresses
the floor in exactly that case. It closes only when both signals agree:

- the model wants no heat over its prediction horizon (target valve opening at 0 %), **and**
- the room is above its current target by more than **"close above setpoint"**

Both are needed. Demand alone also reads 0 % for a room sitting exactly at setpoint, which
is precisely where the floor must stay.

Closing waits for **"hold time"** to pass with the condition continuously true, because
closing withdraws a heat requirement and can be what keeps a heat source from starting.
Reopening has no such delay and happens on the first cycle where demand appears or the
surplus falls below **"reopen below setpoint"** — restoring a requirement is the safe
direction. The two different thresholds are what stops a room drifting around the
deadband from flapping the requirement on and off.

While the gate is closed the card's minimum-flow row reads "Coasting on stored heat" and
the `sensor.<room>_min_flow_temperature` entity reads 0. That is distinct from "No
requirement", which means the outdoor temperature alone holds the setpoint — the surplus
case will come back once the stored heat is used up, the no-requirement case will not until
the weather turns.

The gated hold on its own — without the recovery part — is exposed as
`sensor.<room>_gated_hold_flow_temperature`. It is the signal for deciding whether the
heating season is on: it rises with the weather, drops to 0 while a room coasts on stored
heat, and ignores a room that is merely below setpoint after airing, which the minimum flow
temperature does count.

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
value among the flow lead rooms plus the margin. Attributes `room`, `rooms`,
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
`rooms` (`name`, `lead`, `flow_c`), `margin_k` and the current `outdoor_temperature_c`.
Its state is the lead rooms' highest hold flow plus margin at the current outdoor value.
The curves stay out of the recorder. The heating curve card draws them (see
[`card-design.md`](card-design.md#heating-curve-card)).

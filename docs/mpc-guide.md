# The heating model (MPC)

Each room predicts how its own temperature responds to heating and adjusts its radiators'
target temperature accordingly, rather than just reacting to whether the room is currently
above or below setpoint. This page covers what to expect from that in day-to-day use — for
what the fields in the setup/Configure flow actually do, see below.

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
- **PV boost** — if enabled and the configured PV-surplus entity is on, adds a further fixed
  offset on top of whichever mode is currently active.

**Comfort mode only engages automatically when every configured comfort condition is
true** — both the house-wide ones and any room-specific ones for that room. A room with no
comfort-condition entity configured at all simply stays on Eco until you switch it
manually — the room-specific condition is optional precisely so a room without one doesn't
need automatic comfort switching at all.

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
| `outside_window` | Learning window entity off, or sun. A room still moving after sunrise reports this, not `coasting`, and a learning window that closes after sunrise balances nothing. |
| `paused` | Learning enabled entity off. |
| `too_short` | The stretch ended with fewer than four usable hours. |
| `gap` | A sample gap above 20 minutes. |
| `drift` | The trend over the stretch moved the room more than 0.3 K. A stretch that started on a still room away from its setpoint is first retried from where the room came near its setpoint, if four hours are left. |
| `small_temperature_difference` | Room on average less than 8 K above outside. |
| `storage_dominated` | The heat stored in or released by the room was more than half of the radiator heat. |
| `implausible_target` | The balance pointed at a factor above 2. |
| `room_sensor_fallback` | The configured room sensor is stale or unavailable and the TRVs stand in. |
| `valve_position_missing` | A linked valve position entity is stale or unavailable. |
| `trv_adapting` | A TRV calibrates its valve and moves it by itself. |
| `radiator_not_heating` | A radiator stays cold although its valve reports open, see [Radiator check](#radiator-check). |
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

**`sensor.<room>_capacity_learning_status`** — what the last 30-minute replay did with the
capacity factor:

| State | Meaning |
|---|---|
| `learned` | The factor was adjusted slightly. |
| `stationary` | The room moved less than the stationary range. |
| `small_error` | The replay ended within 0.15 K of the room. |
| `small_change` | The replay itself moved less than 0.1 K. |
| `outside_window` | Sun up or less than two hours after sunset. |
| `disturbed` | Outdoor temperature drifted more than 1 K or the heating flow jumped more than 5 K. A hot-water charge does not count, see [Hot-water charges](#hot-water-charges). |
| `suppressed` | A window is open or was just closed (10 min), or the configuration changed. |
| `room_sensor_fallback` | The window holds samples taken while the TRVs stood in for the room sensor. |
| `valve_position_missing` | The window holds samples without a reported valve opening. |
| `trv_adapting` | The window holds samples taken while a TRV calibrated its valve. |
| `radiator_not_heating` | The window holds samples taken while a radiator stayed cold. |
| `paused` | Learning enabled entity off. |
| `waiting` | No complete window yet. |
| `disabled` | Heating unavailable. |

Attribute `capacity_j_per_k`: the effective heat capacity the model uses — base capacity from
area and construction times the learned capacity factor.

Adjustments per cycle are intentionally small — expect the model to sharpen gradually over
real heating days, not to converge instantly. None of these states need any action from you;
they're diagnostic, useful mainly if a room's behavior seems off and you want to see whether
it's actively adapting.

## When the model learns

The two factors are learned in different ways, because a wrong heat-loss factor hardly shows
as a prediction error: the controller keeps the room at the setpoint its prediction aims
for, whatever the model believes the losses are.

- **Heat-loss factor (`ua_factor`): heat balance over a usable stretch.** Over the
  stretch the heat the radiators gave off (the requested power, lagged as described in
  [Radiator lag and the room observer](#radiator-lag-and-the-room-observer)) has to equal
  what the room lost plus what it stored. The observer's disturbance is left out. The factor that balances this is the target, and each balance moves the learned
  factor 30 % of the way there. A stretch starts once the room is near its setpoint — no more than 0.1 K above it or
  below the target band; a room still coasting down on the evening's heat with the valve
  shut would end far below where it started, one recovering from below far above (status
  `coasting` until then) — or once it has held its temperature for an
  hour (trend below 0.05 K/h), counted from the start of that hour, so a room its
  neighbours keep above the setpoint still learns. It runs as long as learning is possible and is
  balanced once when it ends: the learning window entity turns off, learning is paused, a
  window contact opens, the configured room sensor drops out (the TRVs read up to a kelvin
  off at the radiator; the next stretch starts once it reports again), a TRV starts
  calibrating its valve, the sun condition ends, or heating stays off for more than three
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
  Two checks keep measurement artefacts out: a stretch where the heat stored in or
  released by the room exceeds half of the requested heat depends more on the capacity
  than on the losses and is discarded, and so is a target above 2. A target below 0.15
  means the radiators supplied almost nothing — the room lives off its neighbours, for
  whom the model has no coupling: the factor then moves only down to 0.15 (status
  `learned_at_limit`, the measured target stays in `last_target_factor` of the UA learning status), and
  the observer's disturbance carries the rest. A stretch rejected for drift reports the
  drift in `last_drift_c`.
- **Capacity factor (`capacity_factor`): replay of 30-minute windows.** If the room moved
  further than the **stationary range** (default 0.2 K between the highest and lowest
  reading of the window), the window is replayed with the power the radiators gave off,
  the observer's disturbance and the current heat-loss factor. A room that rose more than the replay, or
  fell more, is lighter than modelled; one that rose or fell less is heavier. Windows the
  room barely moved in, or that the replay hardly moves in (below 0.1 K), are skipped.
- **Sun.** Neither factor learns while the sun is up or within two hours after sunset,
  when walls still release the afternoon's solar heat. Sunrise and sunset are calculated
  from the location configured in Home Assistant.
- **Learning window entity.** The heat-loss factor additionally only learns while the
  optional learning window entity is on — a night-mode helper, for
  instance, so evening activity does not count. `unavailable` counts as off. Without an
  entity this condition is always met. The capacity factor ignores it: its best signal is the
  recovery after airing a room, which rarely happens at night.
- **Learning enabled entity.** An optional external veto for both factors: while it is off,
  nothing is learned, and a window that started before it came back on is discarded, since
  part of its samples were taken while paused. `unavailable` counts as off. Use it when the
  inputs themselves cannot be trusted — for instance a template that falls back to an
  estimated outdoor temperature while the real sensor is down. Without an entity learning is
  never paused.

The heat-loss factor may end up well below 1: room heat loads usually come from a design
calculation for the worst case (neighbouring rooms unheated, full air change), which a room
in operation rarely sees. It is limited to 0.1–2.

The learning window entity, the learning enabled entity and the stationary range are set
per room in the Configure flow.

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
```

A factor that is left out stays as it is, so a reset needs an explicit `1.0`; at least one
value is required. The call discards the measurement window in progress, because its
prediction was made with the old factors.

The optional `disturbance_w` sets the observer's disturbance (see
[Radiator lag and the room observer](#radiator-lag-and-the-room-observer)), limited to the
room's heat load in either direction; `0` resets it. The disturbance makes up for whatever
the model gets wrong, so after resetting the factors or replacing the room sensor it no
longer fits — reset it with them, or it only catches up over the next heating night. A
call with only `disturbance_w` leaves the factors and the running windows alone.

## Sizing a room correctly

The **design indoor/outdoor temperature**, **radiator design temperature system** (e.g.
55/45 °C), and **design heat load** you enter in the setup/Configure flow describe how much
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

The learned capacity factor (0.5–3) scales this base for furniture, open doors and heat
exchange with neighbouring rooms. Both values are required; a room without them stops with
a setup error until they are entered in the Configure flow.

The capacity also drives the valve demand and the recovery flow temperature: a lighter
room reaches its setpoint faster in the prediction, so the controller asks for less
preheating and backs off earlier. After changing a room's area or construction, watch for
overshoot or demand that switches back and forth before tuning the demand smoothing below.

## TRV target mode

The controller turns its demand into a target temperature for each TRV, and the TRV opens
its valve by its own reading at the radiator. **Relative** (default) sets the target a
demand-dependent step above that reading — 0.1 K at the smallest active demand up to 3 K at
full demand, rounded up to the TRV's step — so a small demand opens the valve only partly.
(A Danfoss Ally freshly opened about 55 % at 1.1–1.5 K above its reading and about 90 % from
2.4 K on.)
The current target is kept while it is still above the reading and within one step of the
new one, since every write wakes the TRV. **Absolute** spreads the target over the TRV's
range (18 °C up to its maximum) by demand; there even a few percent of demand ask for 20 °C
or more and the valve opens almost fully. A TRV without a reading falls back to absolute.
The mode is set per room in the Configure flow; the attribute `trv_setpoint_writes` of the
heating demand sensor counts the target writes per TRV since setup.

Every write wakes the TRV, so a valve switches between shut and open sparingly: a shut
valve opens from 10 % demand on, an open one stays open down to 5 %, and a TRV switches
at most every 15 minutes — except when frost protection is forced (open window, heat
source off), which shuts it at once.

While a TRV calibrates its valve (adaptation run, reported by the device profile's
adaptation status entity) nothing is written to it, neither target nor heat-available
switch: a Danfoss Ally aborts the run on any write. The hold ends with the run, after three
hours at the latest (a run took about 1 h 50 min), and forced frost protection still goes
through. As the TRV moves its valve by itself meanwhile, the room learns nothing then (status
`trv_adapting`) and the disturbance is held.

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
[Hot-water charges](#hot-water-charges)) starts the hour over. The verdict holds while the
valve is shut and clears as soon as an open valve warms the TRV again.

Meanwhile the room learns nothing (status `radiator_not_heating`) and the disturbance is
held. The running heat-balance stretch ends where the valve opened for the hour that led to
the verdict: that hour's heat is in doubt as well. `binary_sensor.<room>_radiator_not_heating` (problem) is on, its attribute
`trv_entities` names the TRVs: an automation can send a notification to check — bleed —
the radiator.

## Valve position: the heat the radiators really get

A TRV is a thermostat, not a valve positioner: the controller sets its target temperature,
and the TRV opens its valve by its own reading at the radiator. A small demand can thus open
a valve almost fully. Where the TRV's device profile names a valve opening entity (Danfoss
Ally: `pi_heating_demand`), the observer, the heat balance and the capacity replay then use the heat the radiator gives
off at that opening instead of the requested power (sensor `measured_heating_power`).

The opening is taken as proportional to the flow. With less water the radiator cools it
further and its output stays high: at a 40 °C flow a 10 % opening still gives about 44 % of
full output, 50 % about 87 %. A TRV without such an entity counts with the demand. If the entity goes stale or
unavailable, the room's learning stops (status
`valve_position_missing`) and the disturbance is held until it reports again.

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
- What remains feeds a **disturbance** (sensor `disturbance_power`): lasting heat the
  model does not explain, from neighbouring rooms, appliances, or a heat load or radiator
  sized off. It needs at least twelve hours from zero to its limit (the room's heat load in
  either direction) and only moves under the conditions the factors learn under — heating
  available, no sun, window closed and settled, room sensor reporting, learning not paused.
  During the day it keeps last night's value; otherwise the sun would be taken up and
  missing for hours after sunset. Without heating it keeps the value of the last heating
  night: the radiator model's error, part of what it takes up, is missing then. During a
  hot-water charge the radiators get no water and cool down with their lag; the disturbance
  keeps its value. It
  does not move further in a direction the valve can no longer follow: no rise while the
  valve is shut (below 5 %) — a room coasting above its setpoint on stored heat is no
  lasting gain — and no fall while it is fully open (95 % and up). The other direction
  only moves where the controller needs out of the limit: with the valve shut a fall only
  while the room is below the target band, fully open a rise only while it is above the
  setpoint. Otherwise sensor steps would ratchet it one way. The heat balance leaves the
  disturbance out, so a learned heat-loss factor takes up the neighbours' heat as well:
  when a balance moves the factor, the disturbance hands over what the change now
  explains (factor change × base heat loss × the stretch's mean room-outdoor difference),
  so the same heat is not counted twice and the model's net loss for that night stays
  as it was. It survives restarts and can be set with `recalibrate`.
- The valve demand is planned from the estimate, the radiators' current output and the
  disturbance, so a room whose radiators are still hot closes earlier. The hold and
  recovery flow temperatures use the lag but leave the disturbance out: the flow
  requirement stays weather-driven.

## Target band

Each cycle the controller tries every valve demand from 0 to 100 % and predicts the room
30 minutes ahead. A prediction between the setpoint minus the **target band** (default
0.1 K, one step of the usual room sensor) and the setpoint counts as on target; among
those, the demand nearest the one that holds the setpoint wins. Below the band the
demand that comes closest wins, above the setpoint the lowest one.

Without the band a heavy room with little radiator reserve at a low flow temperature
cannot climb the last 0.1 K within 30 minutes at any demand, and the valve stayed fully
open for hours over one sensor step. With it the room settles within the band, a few
hundredths to a tenth of a kelvin below the setpoint. A band of 0 aims at the exact
setpoint. The band is set per room in the Configure flow.

## Demand smoothing

Three settings in the Configure flow's "Grey-box thermal model" step keep the requested
heating power from chattering cycle to cycle:

- **Demand hysteresis (%)** — a change smaller than this from the last requested value is
  ignored.
- **Hold time (s)** / **hold override demand (%)** — once demand has changed, further
  changes are held back for this long unless they're large enough (past the override
  threshold) to cut through immediately regardless of the hold.
- **Max demand step per cycle (%)** — caps how much requested demand can rise or fall in a
  single cycle, even for a large, genuine change — smooths out the transition instead of
  jumping straight there.

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
the room pauses its calculation until a fresh reading arrives. Implausible single-sample
jumps are also filtered rather than acted on immediately, so one noisy reading doesn't swing
a room's demand; a jump that repeats over a few polls is accepted as a real change.

The flow temperature is exempt from that filter. Its jumps are physical —
compressor start, defrost, or a hot-water charge showing up on a shared leaving-water sensor
— and it only feeds the valve demand, never the minimum flow temperature (see below).

### Hot-water charges

A heat pump charging its hot-water tank sends hot water to the tank, not the radiators,
while a shared leaving-water sensor reads its flow spiking. A flow more than 5 K above the
lowest one of the last hour counts as such a charge. Meanwhile the valve demand is planned
with the heating flow from before the charge, so the valves do not close on the hot reading;
the observer lets the radiators cool down, the disturbance holds, the heat balance counts
no heating and the radiator check starts its hour over. A flow raised for more than an hour
is the new heating flow. While heating is unavailable the reference starts over; a heat
pump ramping up after such a pause may read as a charge for a while.

## Flow temperature and an unreachable heat source

The flow temperature comes from the required **flow temperature entity**, a sensor; the
heat source `climate` entity only supplies the operating state. This lets a template
sensor fall back to a calculated value (a heating curve, for instance) while the heat
source's cloud connection drops out, without the room losing its flow temperature.

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
measured and a calculated flow temperature; pause it through the learning enabled entity
while the fallback is in use.

## Multiple rooms sharing one heat source

Each room independently reports its own required minimum flow temperature (see the card's
"Minimum flow temperature" row). If your heat source's flow temperature is shared across
several rooms, the room asking for the highest flow temperature at any moment is the one
actually determining what the shared source needs to deliver — worth keeping in mind when
tuning any individual room's design values.

## How the minimum flow temperature is computed

The minimum flow temperature is the larger of two requirements:

- **Hold** — the flow needed to hold the setpoint at the current outdoor temperature (see
  the surplus gate below).
- **Recovery** — for a room below its setpoint, the lowest flow that brings it back within
  six hours with its valves fully open. It is found by simulating the room with the same
  radiator, heat-loss and capacity model the valve demand uses, including the learned
  factors. A room within 0.1 K of its setpoint counts as there, and a room whose outdoor
  temperature is above its setpoint asks for no recovery at all.

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

- the model wants no heat over its prediction horizon (heating demand at 0 %), **and**
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

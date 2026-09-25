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

## Reading the MPC learning status

The `sensor.<room>_mpc_learning_status` entity tells you what the self-calibration is
currently doing:

| State | Meaning |
|---|---|
| Learned | The last cycle adjusted the room's model slightly based on how it actually responded to heating. |
| Waiting | Not enough recent history yet to draw a conclusion — normal shortly after startup or after a gap. |
| No correction needed | The room ended up within 0.15 K of what the model predicted, so the cycle carries no correction worth applying. |
| Window disturbed | The measurement window itself was unusable: the outdoor temperature drifted more than 1 K, or the flow temperature jumped more than 5 K (a hot-water charge, for instance), so the room's response cannot be attributed to the model. |
| Suppressed | Learning is paused on purpose — a window in the room was opened (60 min) or just closed (30 min for the heat-loss factor, 10 min for the capacity factor), or the configuration changed. Not to be confused with the measurement window above. |
| Outside learning window | The measurement window fell outside the time the room is allowed to learn: the sun was up or had set less than two hours before, or — for the heat-loss factor — the learning window entity was off. See below. |
| Disabled | The heat source isn't calling for heat at or above its configured flow threshold right now (e.g. outside the heating season) — learning simply doesn't run, and resumes on its own once heating starts again. |

Adjustments per cycle are intentionally small — expect the model to sharpen gradually over
real heating days, not to converge instantly. None of these states need any action from you;
they're diagnostic, useful mainly if a room's behavior seems off and you want to see whether
it's actively adapting.

## When the model learns

The model has no term for heat gains — sun through the windows, people, appliances. Every
gain therefore shows up as a prediction error and would otherwise be booked onto one of the
two factors, mostly as a heat-loss factor that drifts lower and lower in mild weather. The
controller limits learning to windows in which gains are small, and picks the factor from
what the room actually did:

- **Which factor.** If the room temperature stayed within the **stationary range** (default
  0.2 K between the highest and lowest reading of the 30-minute window) the error goes to
  the heat-loss factor (`ua_factor`), no matter how much heating power flowed. If it moved
  further, in either direction, the error goes to the capacity factor (`capacity_factor`).
- **Sun.** Neither factor learns while the sun is up or within two hours after sunset,
  when walls still release the afternoon's solar heat. Sunrise and sunset are calculated
  from the location configured in Home Assistant.
- **Learning window entity.** The heat-loss factor additionally only learns while the
  optional learning window entity is on for the whole 30 minutes — a night-mode helper, for
  instance, so evening activity does not count. `unavailable` counts as off. Without an
  entity this condition is always met. The capacity factor ignores it: its best signal is the
  recovery after airing a room, which rarely happens at night.

Both the learning window entity and the stationary range are set per room in the Configure
flow.

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
factor is required. The call discards the measurement window in progress, because its
prediction was made with the old factors.

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

## Sensor reliability

Room and outdoor temperature readings are checked against **max sensor age (s)**: once a
reading is older than that, it's treated as unavailable rather than trusted indefinitely, and
the room pauses its calculation until a fresh reading arrives. Implausible single-sample
jumps are also filtered rather than acted on immediately, so one noisy reading doesn't swing
a room's demand; a jump that repeats over a few polls is accepted as a real change.

The heat source's flow temperature is exempt from that filter. Its jumps are physical —
compressor start, defrost, or a hot-water charge showing up on a shared leaving-water sensor
— and it only feeds the valve demand, never the minimum flow temperature (see below).

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

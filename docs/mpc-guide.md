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
| No correction needed | The room ended up within 0.15 K of what the model gives when replaying the window, the room barely moved (heat-loss factor learns from the heat balance instead, see below), or the replay itself hardly moved, so the cycle carries no correction worth applying. |
| Window disturbed | The measurement window itself was unusable: the outdoor temperature drifted more than 1 K, or the flow temperature jumped more than 5 K (a hot-water charge, for instance), so the room's response cannot be attributed to the model. |
| Suppressed | Learning is paused on purpose — a window in the room was opened (60 min) or just closed (30 min for the heat-loss factor, 10 min for the capacity factor), or the configuration changed. For the heat-loss factor this restarts the heat-balance window. Not to be confused with the measurement window above. |
| Outside learning window | The measurement window fell outside the time the room is allowed to learn: the sun was up or had set less than two hours before, or — for the heat-loss factor — the learning window entity was off. See below. |
| Paused | The learning enabled entity is off, or the measurement window started before it came back on. Neither factor learns. See below. |
| Disabled | The heat source isn't calling for heat at or above its configured flow threshold right now (e.g. outside the heating season) — learning simply doesn't run, and resumes on its own once heating starts again. |

The state covers one cycle and both factors. Where the heat-loss heat balance stands is in
the sensor's attributes:

| Attribute | Meaning |
|---|---|
| `ua_balance_status` | Result of the last check: `learned`, `collecting` (stretch still running), `too_short` (stretch ended with fewer than four usable hours), `outside_window` (learning window entity off, or sun), `paused`, `gap` (a sample gap above 20 minutes), `drift` (room ended more than 0.3 K away from where it started), `small_temperature_difference` (room on average less than 3 K above outside). |
| `ua_balance_collecting_since` | Start of the current usable stretch — after the last balance, a restart, a pause, the learning window opening, a window contact or heating being off for longer than a short interruption. |
| `ua_balance_due` | When that stretch reaches four hours, the earliest it can be balanced. |
| `ua_balance_last_learned` | End of the last balance that moved the factor. |
| `ua_balance_last_target_factor` | The factor that balance pointed at; the learned factor moves 30 % towards it. |
| `ua_balance_last_drift_c` | How far the room moved over that balance. |
| `ua_balance_last_window_h` | How many hours that balance covered. |

Adjustments per cycle are intentionally small — expect the model to sharpen gradually over
real heating days, not to converge instantly. None of these states need any action from you;
they're diagnostic, useful mainly if a room's behavior seems off and you want to see whether
it's actively adapting.

## When the model learns

The two factors are learned in different ways, because a wrong heat-loss factor hardly shows
as a prediction error: the controller keeps the room at the setpoint its prediction aims
for, whatever the model believes the losses are.

- **Heat-loss factor (`ua_factor`): heat balance over a usable stretch.** Over the
  stretch the heat the controller requested has to equal what the room lost plus what it
  stored. The factor that balances this is the target, and each balance moves the learned
  factor 30 % of the way there. A stretch runs as long as learning is possible and is
  balanced once when it ends: the learning window entity turns off, learning is paused, a
  window contact opens, the sun condition ends, or heating stays off for more than three
  controller cycles (15 minutes). A stretch that reaches eight hours is balanced right away.
  Shorter interruptions of heating (a missed heat-source poll, the flow briefly under the
  threshold) are bridged and count as no heating. A balance needs at least four gap-free
  hours (no gap above 20 minutes), the room ending within 0.3 K of where it started, and on
  average at least 3 K between room and outside. Samples where the flow temperature sits
  more than 5 K above the stretch's median (a hot-water charge) count as no heating. Each
  stretch is used once. Steady gains during the window (people, appliances,
  neighbouring rooms) end up in the factor — that is what the room needs in operation.
- **Capacity factor (`capacity_factor`): replay of 30-minute windows.** If the room moved
  further than the **stationary range** (default 0.2 K between the highest and lowest
  reading of the window), the window is replayed with the power that was actually
  requested and the current heat-loss factor. A room that rose more than the replay, or
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

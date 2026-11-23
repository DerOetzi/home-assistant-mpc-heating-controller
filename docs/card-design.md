# Lovelace card

`custom:heating-controller-card` shows one room, driven entirely by its
`heating_controller` device — mode, setpoints, TRVs, windows, and any comfort conditions
the room has, without hand-listing entities.

## Adding a card

Use the dashboard editor's "+ Add card" picker (search "Heating Controller") to configure it
visually, or add it directly in YAML:

```yaml
type: custom:heating-controller-card
device: 881f0600eb9b171338f6d0b71e44eff9   # required, the room's device
title: Living Room                         # optional, defaults to the device name
header_entities:                           # optional, see "Editing the lists" below
  - sensor.living_room_humidity
  - entity: sensor.living_room_co2
    icon: mdi:molecule-co2
detail_entities:                           # optional, see "Editing the lists" below
  - key: room_sensor
    name: Sensor
  - key: trv:climate.heizung_living_room_big
    name: Big radiator
  - entity: sensor.living_room_dew_point
    name: Dew point
    icon: mdi:thermometer-water
  - entity: sensor.heizung_living_room_big_adaptation_run_status
    name: Adaptation
    state_labels:                          # optional, YAML only
      found: Done
      none: Not yet
  - key: window:binary_sensor.living_room_window_contact
    hidden: true
```

You don't need to write `header_entities`/`detail_entities` by hand — the visual editor's
sortable lists (see below) generate this for you. The one exception is `state_labels` on a
hand-added detail row: a numeric sensor is shown rounded with its unit, any other state as
text — in Home Assistant's own wording, or relabelled through this map. The editor keeps the
map when you reorder or rename the row, but can't edit it.

## What you see

**Header** (always visible): a mode icon and status line — "Automatic"/"Blocked" plus the
active mode, or "Frost protection enforced" if a window is open or the room's TRVs report as
inactive. An **Unblock** button appears next to the title whenever the room is currently
blocked (manually overridden); tapping the header itself expands or collapses the card.
While a radiator stays cold although its valve is open (`binary_sensor.<room>_radiator_not_heating`,
see the MPC guide's "Radiator check"), an amber radiator icon shows in the header, collapsed
or not; its tooltip names the TRVs and tapping it opens that sensor.

**Header values** (always visible): the room's own temperature, followed by any extra
sensors you've added (humidity, CO₂, PM2.5, …).

**Room-specific comfort conditions** (always visible, only if the room has any): one row
per condition, directly below the header values — see "Comfort conditions" below.

**Boost** (always visible, only if the room supports boost): a single button. Tap to
activate boost. Once active, the only way out is **Unblock** in the header — tapping Boost
again does nothing, so you can't accidentally toggle back to automatic mode with a second
tap on the same spot.

**Details** (expanded): room sensor, each TRV (current vs. target temperature, plus the
valve opening it reports, e.g. "21.1 °C → 22 °C · 34 %"), window contacts, and the room's additional heat (`sensor.<room>_additional_heat`: steady gain, day
profile and sun, see the MPC guide's "Gains and sun"), in whatever order you've set — tap any
row to open its more-info dialog. The additional heat shows what the model may use; while
terms are still being learned, their unconfirmed total follows in brackets, e.g.
"0 W (learning: 85 W)".

**Heating control** (expanded): a Comfort/Eco/Frost-protection mode switcher (always
tappable, independent of Boost/Unblock), a comfort-temperature slider, an eco-offset
stepper, and the supply row (see below).

## Editing the lists

Open the card's editor (dashboard edit mode → the card → the pencil/settings icon). Besides
**Device** and **Title**, there are up to three sortable lists:

- **Header sensors** — the room's own temperature reading is always the first entry; drag it
  elsewhere, hide it, or give it a caption (handy when one card covers more than one room,
  e.g. a shared radiator circuit — caption it "Kitchen" and add the kitchen's own
  temperature/humidity sensors alongside with a "Living Room" caption). It can't be deleted
  or pointed at a different entity, since it's the room's own reading. Add any other sensor
  via the entity picker at the bottom — those are fully yours: rename, re-icon, reorder, or
  remove freely.
- **Details** — the room sensor, each TRV, each window contact, and the additional heat are seeded from the
  device automatically; the eye icon hides one without deleting it (they always reflect the
  room's actual hardware, so there's nothing meaningful to delete). Anything you add via the
  entity picker is a normal, fully editable/removable row.
- **Comfort conditions** (only shown if the room has any) — mirrors whichever
  room-specific comfort-condition entities are configured on the room itself; sort, hide, or
  customize each state's icon/label here, but adding or removing an entity happens in the
  integration's own **Configure** flow, not on the card.

## Comfort conditions

A room-specific comfort condition (e.g. a desk-plug "home office" signal, a guest switch)
gates comfort mode for that room specifically, on top of the shared house-wide schedule.

- **Switch / input_boolean** conditions render as an On/Off toggle — tap either side to set
  it directly.
- Anything else (e.g. a `schedule` or other read-only `binary_sensor`) shows only the
  current state; tapping it opens more-info instead, since there's no second state to switch
  to from the card.

Each condition's icon and label per state are customizable in the editor; without an
override they fall back to a generic "On"/"Off".

## Supply

This row shows whether the flow reaches this room, read from its valves the same way the
flow feedback reads them (`sensor.<room>_supply`):

| Shown value | Meaning |
|---|---|
| "Undersupplied · 100 % ⚠" | Valves (nearly) fully open and the room below its target — the flow is too low. |
| "Supplied · 85 %" | Valves open enough to keep the flow from going down. |
| "Oversupplied · 30 %" | Valves well closed, or open only with the room above its target — the flow could be lower. |
| "Not rated · reason" | The valves say nothing about the flow right now: window open, frost protection, no heat (heat pump off, below its threshold, hot water), radiator not heating, boost, or valve opening unknown. |

The percentage is the room's highest valve opening.

## What the card doesn't cover

- A room without its own `heating_controller` device (e.g. one sharing another room's TRV
  circuit) has no card of its own — fold its sensors into the sharing room's card instead
  (see the header/detail caption trick above).
- Other climate equipment (e.g. a standalone air conditioner) isn't part of this card — add
  it as a separate card alongside.
- The card's configuration takes entities, not other cards.

## Heating curve card

`custom:heating-controller-curve-card` belongs to the flow device, not to a room. It
plots flow temperature over outdoor temperature:

- the heat source's curve, as set in the flow device's curve numbers;
- each room's comfort hold flow as its model gives it (without feedback; the gap to the
  recommendation shows what the model misses) — chips under the chart show or hide a
  room; flow lead rooms (marked with a dot) start visible, the rest hidden, dashed when
  shown;
- the recommendation (highest lead room plus feedback) as a thin grey line;
- a trial curve (dashed) with its own cold/warm flow, raise and raise width. "Copy
  heating curve" resets it; "Ends from rooms" sets its cold and warm ends so the trial
  curve stays at or above the highest hold flow plus feedback of all rooms, the lead
  rooms or the rooms shown over its whole range: the straight line through both ends,
  both raised by the largest deficit in between, rounded up to whole degrees.
  Below, the tightest point of both curves against the recommendation: the smallest
  reserve inside the curve's range, red when negative. Then the valve feedback in K
  with the lead demand and its room.

Tap or hover the chart to read every value at one outdoor temperature; a dotted line
marks the current one. The trial curve and the visible rooms are kept in the browser.
"Apply trial curve" writes its fields that differ into the curve numbers (after listing
them for confirmation). Folded below, "Heating curve settings" edits all six curve
numbers directly; each is written when it changes. The curve numbers say what the heat
source is set to: set the same on the heat source.

```yaml
type: custom:heating-controller-curve-card
entity: sensor.vorlauf_hold_flow_curves   # the flow device's "Hold flow curves" sensor
title: Heizkurve                          # optional
```

## Rooms card

`custom:heating-controller-rooms-card` shows every room of the integration in one table,
grouped by Home Assistant floor (lowest level first, rooms without a floor last). It needs
no entities: rooms are the integration's devices with a heating mode. Inside a floor the
rooms follow `rooms:` (device ids or names), then the integration's own order. Tabs switch
between three views; the last one picked is kept in the browser.

- **Supply:** mode icon (a dot marks a flow lead room), actual and target temperature,
  supply judgement with lead demand (tooltip: hint or reason), hold flow with gate (dimmed
  while the gate is closed, highlighted for the room that sets the flow), hold flow and
  boost flow ("–" while no boost is needed). Behind the room name (in every view) a
  thermometer marks the lead room that sets the flow right now ("Lead room") and an arrow
  the room the valve feedback follows ("Feedback"), captioned where the card has room. The supply
  chips share one width.
- **Radiators:** actual temperature, TRV target, valve opening per TRV (labelled by what
  differs in their names), window state, a warning while a radiator doesn't heat.
- **Learning:** UA and capacity status (✓ learned, ⚠ worth a look) with their factors,
  additional heat, learned gain G with σ, what is determined (G / profile hours / sun
  terms), residual RMS.

Tap a value to open its entity. The card writes nothing.

On a narrow card (phone, one-column section) each room becomes a tile: the columns stack
as caption and value, as many side by side as fit. A room with several TRVs shows one tile per valve,
captioned by its label (e.g. "Gross", "Klein"); a single one is captioned "Valve". All views switch below 780 px card
width, the width the learning view needs. With more than
one floor the tiles show one floor at a time, picked by a second row of tabs (remembered
in the browser like the view).

```yaml
type: custom:heating-controller-rooms-card
title: Räume                         # optional
view: supply                         # optional start view
views: [supply, radiators, learning] # optional; one view hides the tabs
rooms: [Wohnzimmer, Esszimmer]       # optional order inside a floor
```

## Flow card

`custom:heating-controller-flow-card` shows what the flow device and the hub decide about
the shared flow. It needs no configuration (optional `title:`); its entities are found by
translation key.

- **Warnings** in the header: heat source unreachable, outdoor temperature missing,
  radiators not heating (how many).
- **Flow target** with, beside it, its source (lead rooms, boost, threshold, hot water
  charge, fallback) and why it holds; below that the lead rooms, the one that sets the
  flow in bold (or the boost room).
- **Calculation:** feedforward + feedback = lead target, beside a boost. Right below, the
  **feedback**: a bar from the step down (left, blue) to the step up (right, red; the night's
  times at night, then a moon beside the heading) with the count. A step's lock hatches
  its own side of the bar and shows a lock with the time left in place of the step. Then
  the room with the widest open valve and its deficit, freeze and catch-up, the last change, the would-be steps per room, and a
  reset button (asks first). Under **control**: curve + measured correction = base (what the
  heat source gives without offset), target − base = need, rounded up to the offset. The
  offset is green when the heat source has it, else yellow with the actual offset in its
  label. Below: why the correction pauses,
  write errors, an offset changed elsewhere.

Tap a value to open its entity.

```yaml
type: custom:heating-controller-flow-card
title: Vorlauf   # optional
```

## Heating season card

`custom:heating-controller-season-card` shows the heating season the hub works out and lets
you switch the heat source by hand. No configuration (optional `title:`); entities are found
by translation key.

- **Header:** a chip when switching on or off is recommended, and a *Heat | Off* switch for
  the heat source's climate (`heat_source_entity` of the season sensors). The current mode
  is marked; the other one asks first, then calls `climate.set_hvac_mode`. Nothing switches
  by itself.
- **Heating limit:** hold flow of the heating-limit lead rooms ≥ or < the flow threshold →
  demand yes/no (green on yes). Below: the lead rooms, the one that sets the hold flow in
  bold; the reserve to the curve and since when the demand holds.
- **Demand last 24 h:** a bar 0–24 h with the demand hours and ticks at the hold and the
  switch-on threshold; the last demand and, while heating, when switching off would be
  recommended (last demand + switch-off time).

The thresholds are set on their number entities, not on the card.

```yaml
type: custom:heating-controller-season-card
title: Heizsaison   # optional
```

## Localization

The card follows Home Assistant's own UI language and currently ships English and German;
any other language falls back to English.

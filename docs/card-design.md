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

**Header values** (always visible): the room's own temperature, followed by any extra
sensors you've added (humidity, CO₂, PM2.5, …).

**Room-specific comfort conditions** (always visible, only if the room has any): one row
per condition, directly below the header values — see "Comfort conditions" below.

**Boost** (always visible, only if the room supports boost): a single button. Tap to
activate boost. Once active, the only way out is **Unblock** in the header — tapping Boost
again does nothing, so you can't accidentally toggle back to automatic mode with a second
tap on the same spot.

**Details** (expanded): room sensor, each TRV (current vs. target temperature), and window
contacts, in whatever order you've set — tap any row to open its more-info dialog.

**Heating control** (expanded): a Comfort/Eco/Frost-protection mode switcher (always
tappable, independent of Boost/Unblock), a comfort-temperature slider, an eco-offset
stepper, and the minimum flow temperature row (see below).

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
- **Details** — the room sensor, each TRV, and each window contact are seeded from the
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

## Minimum flow temperature

This row shows what your heat source's flow temperature needs to be for this room's
radiators to keep up with current demand:

| Shown value | Meaning |
|---|---|
| "No requirement" | The room isn't asking for heat right now. |
| "Below operating threshold" | The heat source isn't running warm enough yet to compare against. |
| "Source inactive" | No heat source is currently running. |
| A temperature with ⚠ | The heat source's actual flow temperature is **below** what this room needs — it's under-supplied. |
| A plain temperature | The heat source is meeting this room's requirement. |

If several rooms share one heat source, whichever room shows the highest required
temperature is the one currently limiting the system.

## What the card doesn't cover

- A room without its own `heating_controller` device (e.g. one sharing another room's TRV
  circuit) has no card of its own — fold its sensors into the sharing room's card instead
  (see the header/detail caption trick above).
- Other climate equipment (e.g. a standalone air conditioner) isn't part of this card — add
  it as a separate card alongside.
- The card's configuration takes entities, not other cards.

## Localization

The card follows Home Assistant's own UI language and currently ships English and German;
any other language falls back to English.

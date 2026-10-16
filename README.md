# AisRadar

A [PiClock3](https://github.com/n0bel/PiClock3) widget. A half-circle radar
looking out from a point on the shore, plotted from live AIS.

The flat edge is the beach. You set that point with a latitude and a
longitude, which way the water is with a bearing, and how far the arc
reaches in miles or kilometers. Targets inside the half-circle are drawn
as a plan-position display: range rings, a sweep, and the ship itself.
AIS reports a type code and the hull dimensions (bow, stern, port,
starboard). A tanker, cargo ship, passenger ship, tug, fishing boat, or
pleasure craft is drawn as that outline, and a longer reported hull is a
longer mark. A target with no type stays a dot with a course line. Under
the scope, in small text, is the radius and the time the feed last answered.

```
18 mi  ·  14:02:11
```

The widget fills the layout region it is given, the same way every other
PiClock3 widget is sized. `font-size` is a fraction of that region's
height, again the same setting other widgets use. To make the scope
bigger on the page, change the region in `layouts/shore.yaml` (or point
`region:` at another one). Do not look for a pixel width.

## Feeds

Either source. Both are free. Neither key belongs in `config.yaml`.

| `source` | What it is | Key |
|---|---|---|
| `aishub` | Polls the [AISHub](https://www.aishub.net/api) web service, at most once a minute | `username` — an AISHub username. Their free access is earned by feeding them a receiver. |
| `aisstream` | Holds a websocket to [AISStream](https://aisstream.io/documentation) open while the page is showing | `apikey` — a free key from aisstream.io |

Put them in `ApiKeys.yaml` beside `PyQtPiClock3.py`:

```yaml
aishub: YOUR_USERNAME
aisstream: YOUR_KEY
```

And, once, in the clock config:

```yaml
apikeys: !include ApiKeys.yaml
```

The defaults are already `{apikeys.aishub}` and `{apikeys.aisstream}`. With
no key the scope still draws — rings, sweep, caption `no key` — and it
does not invent ships.

AISHub's human JSON is `format=1&output=json`. The box is the circle
around the shore point; the widget keeps only the half you are looking
out. AISStream is filtered to position reports and static data. A page
that is not showing closes the socket.

## Install

From the top of a PiClock3 checkout:

```
git clone https://github.com/ShawnPGHPublic/piclock3-ais-radar plugins/AisRadar
python3 PyQtPiClock3.py plugins/AisRadar/examples/example.yaml
```

The folder name is the plugin name:

```yaml
widgets:
  ais:
    plugin: plugins.AisRadar
    region: radar
    latitude: 51.126
    longitude: 1.327
    bearing: 150
    radius: 18
    radius-unit: mi
    source: aishub
```

`bearing` is degrees true. `0` looks north, `90` east. The example looks
southeast from Dover across the Channel, on the `shore` layout that ships
in `layouts/`. That layout adds a `radar` region. It does not replace any
region a shipped layout already has.

`radius-unit` is `mi` or `km`. The caption prints the radius in that unit,
not in whatever set the rest of the clock is using, because the radius is
a setting and not a converted weather reading.

To check the shape without drawing:

```
python3 PyQtPiClock3.py plugins/AisRadar/examples/example.yaml --check
```

## Settings

| | |
|---|---|
| `latitude`, `longitude` | Shore point. A number, or `{location.latitude}`. |
| `bearing` | Degrees true. The arc opens this way. |
| `radius`, `radius-unit` | Reach of the arc, `mi` or `km`. |
| `source` | `aishub` or `aisstream`. |
| `username`, `apikey` | `{apikeys.aishub}` and `{apikeys.aisstream}`. |
| `refresh` | Seconds. AISHub is never asked more than once a minute. |
| `max-age` | Minutes a target stays after its last report. |
| `max-targets` | Nearest targets kept when the box is crowded. |
| `sweep` | Seconds for the beam to cross the half-circle. |
| `legend` | `true` or `false`. The type key above the arc. Off by default. |
| `font-size` | Caption size, a fraction of the region height. |

Color follows the theme when that color is light enough to read on the
dark glass. A dark theme color falls back to phosphor green. The glass
itself stays dark so the scope still reads on a light page.

The trace is themed. The sweep is the same color, brighter. There is no
map under it: this is the radar, not a chart.

## Credit

Vessel positions are other people's receivers.

- AISHub — "Data: AISHub contributors", [aishub.net](https://www.aishub.net)
- AISStream — [aisstream.io](https://aisstream.io)

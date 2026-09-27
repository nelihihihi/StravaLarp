# gpxgen 

A single-file Python script that builds a timed GPX activity from a Google Maps
directions link, or edits an existing GPX file (new start time, pace, name,
creator, elevation or heart rate).For them strava larpers

It only uses the Python standard library, so there's nothing to install.

## Requirements

- Python 3.9 or newer (it uses `zoneinfo` and `str.removesuffix`)
- An internet connection for `create`, and for `modify --fetch-ele`

## Quick start

```sh
# Google Maps directions link -> GPX, running at 6:30/km
python3 gpxgen.py create "MAPS_LINK" \
    --start "2026-09-27 20:00" --tz +01:00 --pace 6:30 -o run.gpx

# Same idea, cycling, timed from a start and an end time
python3 gpxgen.py create MAPS_LINK --start 07:00 --end 07:50 --sport cycling

# Move an existing file to a new start time and keep its original timing
python3 gpxgen.py modify run.gpx --start "2026-09-28 06:30"

# Retime an existing file at a new pace, with modelled heart rate
python3 gpxgen.py modify run.gpx --pace 5:45 --hr 160 -o faster.gpx
```

When it finishes, the script prints a summary: distance, duration, average
pace, start and end time, elevation range and gain, and average and max heart rate.

## Commands

### `create LINK`

Turns a Google Maps directions link into a GPX track.

1. Follows `maps.app.goo.gl` short links until it reaches a `google.com/maps/dir/...` URL.
2. Reads the stops from the link. A stop can be typed coordinates or a named place.
3. Routes between the stops along real streets using OSRM (OpenStreetMap data).
4. Moves along the route at a constant pace, writing one point every `--interval` seconds.
5. Adds elevation from a terrain model and heart rate from a simple simulation, unless you turn them off.

Needs `--start` plus one of `--pace`, `--speed`, `--duration` or `--end`.

| Option | Meaning |
|---|---|
| `--profile {foot,bike,car}` | Which road network to route on. By default it follows `--sport`, then the travel mode in the link, then `foot`. |

### `modify GPX`

Rewrites an existing `.gpx` file. It reads track points (`trkpt`), or route
points (`rtept`) if there are no track points.

- **Only `--start`:** shifts the whole activity to the new start and keeps the original spacing between points.
- **`--pace` / `--speed` / `--duration` / `--end`:** retimes the same path at a constant pace.
- **Elevation:** keeps the file's elevation. If the file has none, or you pass `--fetch-ele`, it fetches terrain data.
- **Heart rate:** keeps the file's heart rate. `--hr` replaces it with modelled values, and `--no-hr` removes it.
- **Name, creator, sport:** keeps the file's values unless you override them.

The output defaults to `<input>_modified.gpx`.

| Option | Meaning |
|---|---|
| `--fetch-ele` | Replace the file's elevation with terrain data |

## Options for both commands

| Option | Default | Meaning |
|---|---|---|
| `-o, --output` | `activity_YYYYMMDD_HHMM.gpx` / `<input>_modified.gpx` | Output file |
| `--start` | — | `"2026-09-27 20:00"`, or `"20:00"` for today (in `modify`, the original activity's date) |
| `--tz` | this computer's timezone | `+01:00`, `UTC-5`, or a name like `Europe/London` |
| `--pace` | — | Per km (`6:30`), or per mile (`10:30/mi`) |
| `--speed` | — | Average km/h |
| `--duration` | — | Total time: `45:00` or `1:05:00` |
| `--end` | — | End time. A bare clock time earlier than the start rolls over past midnight. |
| `--sport` | `running` | `running`, `walking`, `hiking` or `cycling` |
| `--name` | e.g. "Evening Run" | Activity name, picked from the time of day and sport |
| `--creator` | `gpxgen` | The GPX `creator` attribute |
| `--hr BPM` | by sport | Heart rate the model settles around (running 150, cycling 135, hiking 125, walking 105) |
| `--hr-start BPM` | `95` | Heart rate at the start |
| `--no-hr` | | Leave heart rate out |
| `--no-ele` | | Leave elevation out |
| `--interval` | `1` | Seconds between points (minimum 1) |
| `--seed` | `42` | Random seed for heart-rate variation, so runs are repeatable |

`--pace`, `--speed`, `--duration` and `--end` can't be combined; use only one.

Run `python3 gpxgen.py create -h` or `python3 gpxgen.py modify -h` for the built-in help.

## How it works

- **Distance and pace.** Points sampled every second cut slightly across each
  bend, so the written track is a little shorter than the route. The script
  times the activity against the distance of the sampled points, not the route,
  so the pace in apps such as Strava matches what you asked for.
- **Elevation.** Samples the Copernicus DEM via the
  Open-Meteo elevation API
  every 25 m. The model counts building roofs as ground in dense city blocks,
  so the script runs a rolling median to remove those spikes and then a rolling
  mean over about 300 m to smooth the result.
- **Heart rate.** A simple model, not real data. It rises from `--hr-start`
  towards the steady value, drifts up slowly over time, reacts to the slope
  over the last 50 m, and adds a little random variation.
- **Output.** GPX 1.1 with Garmin's `TrackPointExtension` for heart rate, which
  Strava, Garmin Connect and most other tools can read. Times are written in UTC.

## Network services

| Service | Used for |
|---|---|
| `maps.app.goo.gl` | Following short links |
| `routing.openstreetmap.de` (OSRM) | Street routing (`create` only) |
| `api.open-meteo.com` | Elevation |

These are free public services, so please don't hammer them. `modify` without
`--fetch-ele` on a file that already has elevation works offline.

## Limitations

- Pace is constant for the whole activity. There are no pauses, warm-ups or
  slower sections on hills.
- Only Google Maps **directions** links (`/maps/dir/...`) work. A link to a
  single place or a search result won't.
- Elevation is terrain height, so bridges, tunnels and overpasses show the
  ground below or above them.

#!/usr/bin/env python3
"""Make a timed GPX activity from a Google Maps directions link, or modify an existing GPX.

  create   Google Maps link + start time + pace/duration/end  ->  GPX
  modify   existing GPX  ->  GPX with a new start time, pace, name, creator, elevation or heart rate

Only needs the Python standard library. Routes follow streets using OpenStreetMap (OSRM);
elevation comes from the Copernicus DEM via Open-Meteo; heart rate is a simple model.
"""
import argparse, bisect, json, math, random, re, statistics, sys, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

UA = {"User-Agent": "gpxgen/1.0"}
OSRM = {
    "foot": "https://routing.openstreetmap.de/routed-foot/route/v1/foot/",
    "bike": "https://routing.openstreetmap.de/routed-bike/route/v1/bike/",
    "car": "https://routing.openstreetmap.de/routed-car/route/v1/driving/",
}
ELEVATION_API = "https://api.open-meteo.com/v1/elevation"
SPORT_PROFILE = {"running": "foot", "walking": "foot", "hiking": "foot", "cycling": "bike"}
SPORT_NOUN = {"running": "Run", "walking": "Walk", "hiking": "Hike", "cycling": "Ride"}
SPORT_HR = {"running": 150, "walking": 105, "hiking": 125, "cycling": 135}  # default settled bpm
MAPS_MODE_PROFILE = {"0": "car", "1": "bike", "2": "foot"}  # Google's !3e travel-mode codes


# ---------- small helpers ----------

def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode()

def haversine(a, b):  # metres; a, b are (lon, lat)
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

def cumulative(path):
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + haversine(a, b))
    return cum

def interp(xs, ys, x):  # linear interpolation of ys over ascending xs
    i = bisect.bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    return ys[i - 1] if x1 == x0 else ys[i - 1] + (x - x0) / (x1 - x0) * (ys[i] - ys[i - 1])

def parse_clock(s):  # "6:30" -> 390 s, "1:05:00" -> 3900 s
    secs = 0.0
    for part in s.split(":"):
        secs = secs * 60 + float(part)
    return secs

def parse_tz(s):  # None = this computer's timezone
    if not s or s == "local":
        return None
    m = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?", s.strip(), re.I)
    if m:
        off = timedelta(hours=int(m[2]), minutes=int(m[3] or 0))
        return timezone(-off if m[1] == "-" else off)
    return ZoneInfo(s)

def parse_when(s, tz, base=None):
    """'2026-09-27 20:00' or '20:00' (on base's date, else today) -> aware datetime."""
    s = s.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        day = (base or datetime.now(tz)).date()
        dt = datetime.combine(day, datetime.strptime(s, "%H:%M:%S" if s.count(":") == 2 else "%H:%M").time())
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz) if tz else dt.astimezone()
    return dt

def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def fmt_secs(s):
    s = round(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---------- inputs: Google Maps link, existing GPX ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def resolve_maps_link(url):
    """Follow maps.app.goo.gl / goo.gl short links until we have a /maps/dir/ URL."""
    found = re.search(r"https?://\S+", url)  # tolerate stray text around a pasted link
    if not found:
        sys.exit(f"That doesn't look like a link (it should start with https://): {url}")
    url = found[0]
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(5):
        if "/maps/dir/" in url:
            return url
        try:
            opener.open(urllib.request.Request(url, headers=UA), timeout=30)
            break
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
            else:
                raise
    sys.exit(f"Not a Google Maps directions link (expected /maps/dir/...): {url}")

def parse_maps_dir(url):
    """Return ([(lon, lat), ...], travel-mode code or None) from a /maps/dir/ URL.

    Stops typed as coordinates are in the path; named places carry their coordinates in the
    data= blob as !1d<lon>!2d<lat>, in the same order as the stops.
    """
    url = url.split("?", 1)[0]
    path, _, data = url.partition("/data=")
    stops = [urllib.parse.unquote_plus(s) for s in path.split("/maps/dir/", 1)[1].split("/")
             if s and not s.startswith("@")]
    named = [(float(lon), float(lat)) for lon, lat in re.findall(r"!1d(-?[\d.]+)!2d(-?[\d.]+)", data)]
    points = []
    for s in stops:
        m = re.fullmatch(r"\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*", s)
        if m:
            points.append((float(m[2]), float(m[1])))
        elif named:
            points.append(named.pop(0))
        else:
            sys.exit(f"Couldn't find coordinates for stop '{s}' in the link.")
    if len(points) < 2:
        sys.exit("The link needs at least a start and an end point.")
    mode = re.search(r"!3e(\d)", data)
    return points, mode and mode[1]

def route(points, profile):
    coords = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in points)
    d = json.loads(fetch(f"{OSRM[profile]}{coords}?overview=full&geometries=geojson"))
    if d.get("code") != "Ok":
        sys.exit(f"Routing failed: {d.get('message', d.get('code'))}")
    return [tuple(c) for c in d["routes"][0]["geometry"]["coordinates"]]

def read_gpx(filename):
    """Return (points, meta). Points are dicts with lon, lat, ele, time, hr (None when missing)."""
    root = ET.parse(filename).getroot()
    tag = lambda e: e.tag.rsplit("}", 1)[-1]
    first = lambda e, name, conv: next((conv(c.text) for c in e.iter() if tag(c) == name and c.text), None)
    elems = [e for e in root.iter() if tag(e) == "trkpt"] or [e for e in root.iter() if tag(e) == "rtept"]
    points = [{"lon": float(e.get("lon")), "lat": float(e.get("lat")),
               "ele": first(e, "ele", float),
               "time": first(e, "time", lambda t: datetime.fromisoformat(t.strip().replace("Z", "+00:00"))),
               "hr": first(e, "hr", lambda v: round(float(v)))} for e in elems]
    if len(points) < 2:
        sys.exit(f"{filename} has fewer than 2 track/route points.")
    meta = {"creator": root.get("creator"), "name": first(root, "name", str), "type": first(root, "type", str)}
    return points, meta


# ---------- elevation and heart rate ----------

def terrain(path, cum, step=25.0):
    """Elevation along the path from the DEM, as a function of distance."""
    total = cum[-1]
    ds = [min(i * step, total) for i in range(int(total // step) + 2)]
    lons, lats = [p[0] for p in path], [p[1] for p in path]
    ll = [(interp(cum, lons, d), interp(cum, lats, d)) for d in ds]
    elev = []
    for i in range(0, len(ll), 100):  # API takes up to 100 points per request
        chunk = ll[i:i + 100]
        q = urllib.parse.urlencode({"latitude": ",".join(f"{la:.6f}" for _, la in chunk),
                                    "longitude": ",".join(f"{lo:.6f}" for lo, _ in chunk)})
        elev += json.loads(fetch(f"{ELEVATION_API}?{q}"))["elevation"]
    # The DEM counts rooftops as ground in dense blocks: a rolling median drops the spikes,
    # then a rolling mean (~300 m) smooths what's left.
    win = lambda xs, i, r: xs[max(0, i - r):i + r + 1]
    elev = [statistics.median(win(elev, i, 4)) for i in range(len(elev))]
    elev = [statistics.fmean(win(elev, i, 6)) for i in range(len(elev))]
    return lambda d: interp(ds, elev, d)

def model_hr(samples, ele_at, hr_start, hr_steady, seed):
    """Heart rate that rises from hr_start to hr_steady, drifts up slowly, and responds to climbs."""
    rng = random.Random(seed)
    out, hr, noise, prev_t = [], float(hr_start), 0.0, None
    for t, d, *_ in samples:
        dt = 0.0 if prev_t is None else t - prev_t
        grade = 0.0 if ele_at is None or d < 50 else (ele_at(d) - ele_at(d - 50)) / 50
        target = hr_steady + 0.12 * t / 60 + max(-6, min(10, 400 * grade))
        hr += (target - hr) * min(1.0, dt / 40)
        if dt:
            noise = noise * 0.9 ** dt + rng.gauss(0, 0.6 * math.sqrt(dt))
        out.append(round(hr + noise))
        prev_t = t
    return out


# ---------- assembling and writing ----------

def constant_pace_samples(path, cum, duration, interval):
    """(t, d, lon, lat) every `interval` seconds, moving along the path at constant speed."""
    total, lons, lats = cum[-1], [p[0] for p in path], [p[1] for p in path]
    samples, t = [], 0.0
    while True:
        t = min(t, duration)
        d = total * t / duration
        samples.append((t, d, interp(cum, lons, d), interp(cum, lats, d)))
        if t >= duration:
            return samples
        t += interval

def paced_samples(path, cum, args, start):
    """Constant-pace samples whose timing matches the distance Strava will measure from them.

    Sampled points cut a little off every bend, so the written track is slightly shorter than
    the route; time it against the sampled distance instead (two passes is plenty).
    """
    dist = cum[-1]
    for _ in range(2):
        duration = round(resolve_duration(args, dist, start))
        samples = constant_pace_samples(path, cum, duration, args.interval)
        dist = cumulative([(lon, lat) for *_, lon, lat in samples])[-1]
    return samples

def resolve_duration(args, total_m, start):
    if args.pace:
        p = args.pace.lower().replace(" ", "")
        per_km = parse_clock(p.removesuffix("/mi")) / 1.609344 if p.endswith("/mi") else parse_clock(p.removesuffix("/km"))
        return total_m / 1000 * per_km
    if args.speed:
        return total_m / 1000 / args.speed * 3600
    if args.duration:
        return parse_clock(args.duration)
    if args.end:
        end = parse_when(args.end, start.tzinfo, base=start)
        if end <= start and re.fullmatch(r"\s*\d{1,2}:\d{2}(:\d{2})?\s*", args.end):
            end += timedelta(days=1)  # "23:50" -> "00:30" crosses midnight
        if end <= start:
            sys.exit("--end must be after the start time.")
        return (end - start).total_seconds()
    return None

def default_name(start, sport):
    h = start.hour
    part = "Morning" if 5 <= h < 12 else "Afternoon" if h < 18 else "Evening" if h < 22 else "Night"
    return f"{part} {SPORT_NOUN.get(sport, 'Activity')}"

def write_gpx(filename, samples, start, eles, hrs, creator, name, sport):
    tpx = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
    lines = []
    for k, (t, _, lon, lat) in enumerate(samples):
        ele = f"<ele>{eles[k]:.1f}</ele>" if eles else ""
        hr = (f"<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{hrs[k]}</gpxtpx:hr>"
              f"</gpxtpx:TrackPointExtension></extensions>") if hrs else ""
        when = iso_utc(start + timedelta(seconds=round(t)))
        lines.append(f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">{ele}<time>{when}</time>{hr}</trkpt>')
    body = "\n".join(lines)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator={quoteattr(creator)} xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="{tpx}">
  <metadata>
    <name>{escape(name)}</name>
    <time>{iso_utc(start)}</time>
  </metadata>
  <trk>
    <name>{escape(name)}</name>
    <type>{escape(sport)}</type>
    <trkseg>
{body}
    </trkseg>
  </trk>
</gpx>
""")

def summary(filename, samples, start, eles, hrs):
    total = cumulative([(lon, lat) for *_, lon, lat in samples])[-1]  # as Strava will measure it
    duration = samples[-1][0]
    end = start + timedelta(seconds=round(duration))
    print(f"Wrote {filename}")
    print(f"  {total / 1000:.2f} km in {fmt_secs(duration)} ({fmt_secs(duration / (total / 1000))}/km), "
          f"{len(samples)} points")
    print(f"  {start:%Y-%m-%d %H:%M:%S} -> {end:%H:%M:%S} (UTC{start:%z})")
    if eles:
        gain = sum(max(0.0, b - a) for a, b in zip(eles, eles[1:]))
        print(f"  elevation {min(eles):.0f}-{max(eles):.0f} m, gain {gain:.0f} m")
    if hrs:
        print(f"  heart rate avg {statistics.fmean(hrs):.0f}, max {max(hrs)} bpm")


# ---------- commands ----------

def cmd_create(args):
    tz = parse_tz(args.tz)
    if not args.start:
        sys.exit("create needs --start.")
    if not (args.pace or args.speed or args.duration or args.end):
        sys.exit("create needs one of --pace, --speed, --duration or --end.")
    stops, mode = parse_maps_dir(resolve_maps_link(args.link))
    sport = args.sport or "running"
    profile = args.profile or SPORT_PROFILE.get(sport) or MAPS_MODE_PROFILE.get(mode, "foot")
    print(f"{len(stops)} stops from the link, routing by {profile}...")
    path = route(stops, profile)
    cum = cumulative(path)
    start = parse_when(args.start, tz)
    samples = paced_samples(path, cum, args, start)

    ele_at = None if args.no_ele else terrain(path, cum)
    eles = [ele_at(d) for _, d, *_ in samples] if ele_at else None
    hrs = None if args.no_hr else model_hr(samples, ele_at, args.hr_start, args.hr or SPORT_HR[sport], args.seed)

    out = args.output or f"activity_{start:%Y%m%d_%H%M}.gpx"
    write_gpx(out, samples, start, eles, hrs, args.creator or "gpxgen", args.name or default_name(start, sport), sport)
    summary(out, samples, start, eles, hrs)

def cmd_modify(args):
    tz = parse_tz(args.tz)
    pts, meta = read_gpx(args.gpx)
    path = [(p["lon"], p["lat"]) for p in pts]
    cum = cumulative(path)
    times = [p["time"] for p in pts]
    has_times = all(times)
    orig_start = times[0] if has_times else None
    if args.start:
        start = parse_when(args.start, tz, base=orig_start)
    elif orig_start:
        start = orig_start.astimezone(tz) if tz else orig_start.astimezone()
    else:
        sys.exit(f"{args.gpx} has no timestamps; give --start.")

    if args.pace or args.speed or args.duration or args.end:  # retime along the same path at a constant pace
        samples = paced_samples(path, cum, args, start)
    elif has_times:  # keep the original timing, just shift it
        samples = [((ti - orig_start).total_seconds(), d, lon, lat) for ti, d, (lon, lat) in zip(times, cum, path)]
    else:
        sys.exit(f"{args.gpx} has no timestamps; give one of --pace, --speed, --duration or --end.")

    known = lambda key: ([c for c, p in zip(cum, pts) if p[key] is not None],
                         [p[key] for p in pts if p[key] is not None])
    ele_d, ele_v = known("ele")
    if args.no_ele:
        ele_at = None
    elif args.fetch_ele or not ele_v:
        ele_at = terrain(path, cum)
    else:
        ele_at = lambda d: interp(ele_d, ele_v, d)
    eles = [ele_at(d) for _, d, *_ in samples] if ele_at else None

    sport = args.sport or meta["type"] or "running"
    hr_d, hr_v = known("hr")
    if args.no_hr:
        hrs = None
    elif args.hr:
        hrs = model_hr(samples, ele_at, args.hr_start, args.hr, args.seed)
    elif hr_v:  # keep the file's heart rate, mapped by distance
        hrs = [round(interp(hr_d, hr_v, d)) for _, d, *_ in samples]
    else:
        hrs = None

    out = args.output or args.gpx.removesuffix(".gpx") + "_modified.gpx"
    creator = args.creator or meta["creator"] or "gpxgen"
    name = args.name or meta["name"] or default_name(start, sport)
    write_gpx(out, samples, start, eles, hrs, creator, name, sport)
    summary(out, samples, start, eles, hrs)

def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--output", help="output .gpx file")
    common.add_argument("--start", help='start time: "2026-09-27 20:00", or "20:00" for today')
    common.add_argument("--tz", help='timezone for --start/--end: "+01:00" or "Europe/London" (default: this computer\'s)')
    timing = common.add_mutually_exclusive_group()
    timing.add_argument("--pace", help='pace per km, e.g. "6:30" (or "10:30/mi")')
    timing.add_argument("--speed", type=float, help="average speed in km/h")
    timing.add_argument("--duration", help='total time, e.g. "45:00" or "1:05:00"')
    timing.add_argument("--end", help='end time, e.g. "20:45" or "2026-09-27 20:45"')
    common.add_argument("--sport", choices=sorted(SPORT_NOUN), help="default: running (create) / the file's type (modify)")
    common.add_argument("--name", help='activity name (default e.g. "Evening Run")')
    common.add_argument("--creator", help="the GPX creator attribute")
    common.add_argument("--hr", type=int, metavar="BPM", help="model heart rate settling around BPM")
    common.add_argument("--hr-start", type=int, default=95, metavar="BPM", help="heart rate at the start (default 95)")
    common.add_argument("--no-hr", action="store_true", help="leave heart rate out")
    common.add_argument("--no-ele", action="store_true", help="leave elevation out")
    common.add_argument("--interval", type=int, default=1, help="seconds between points (default 1)")
    common.add_argument("--seed", type=int, default=42, help="random seed for heart-rate variation")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create", parents=[common], help="Google Maps directions link -> GPX")
    c.add_argument("link", help="Google Maps directions link (maps.app.goo.gl/... or google.com/maps/dir/...)")
    c.add_argument("--profile", choices=sorted(OSRM), help="routing network (default: from --sport)")
    c.set_defaults(func=cmd_create)
    m = sub.add_parser("modify", parents=[common], help="change an existing GPX file")
    m.add_argument("gpx", help="input .gpx file")
    m.add_argument("--fetch-ele", action="store_true", help="replace the file's elevation with DEM terrain data")
    m.set_defaults(func=cmd_modify)

    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1")
    args.func(args)

if __name__ == "__main__":
    main()

"""A busy /api/buses body from the timetable, for checking the bus layer at any hour.

The live feed goes quiet in the evening and overnight. This places every bus the timetable has
under way at a moment (by default the Monday morning peak, 30 November 2026 at 08:00 in Nicosia)
at its scheduled place along its route shape, in the shape /api/buses returns, so a browser test
can replay a full street (Playwright request interception) without touching the live feed.

    uv run python tools/bus_model/busy_feed.py OUT.json [--at 2026-11-30T08:00] [--near-km 12]
                                               [--also 2026-11-30T07:40 ...]

`--also` adds the buses of more moments (as other buses), for a heavier load than the city ever
runs: about 90 buses are under way within 12 km at the peak, and a second moment makes about 180.
Positions are the timetable's, not real fixes: good for drawing, not for delays.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from chatbot.mobility.gtfs import FEEDS, TZ, Timetable, m_per_deg_lon, point_at  # noqa: E402
from chatbot.settings import CACHE_DIR  # noqa: E402

CENTRE = (33.3615, 35.1725)  # lon, lat: the old town


def scheduled_along(tt: Timetable, trip, stops_along, secs: int) -> tuple[float, float] | None:
    """(metres along the shape, speed in km/h) at `secs` after the service midnight."""
    arr, dep = trip.arr, trip.dep
    for i in range(len(trip.stops) - 1):
        if dep[i] <= secs <= arr[i + 1]:
            span = arr[i + 1] - dep[i]
            t = 0.0 if span <= 0 else (secs - dep[i]) / span
            a = stops_along[i] + t * (stops_along[i + 1] - stops_along[i])
            v = 0.0 if span <= 0 else (stops_along[i + 1] - stops_along[i]) / span * 3.6
            return a, v
        if arr[i] <= secs < dep[i]:
            return stops_along[i], 0.0  # waiting at a stop
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--at", default="2026-11-30T08:00")
    ap.add_argument("--also", nargs="*", default=[], help="more moments, drawn as more buses")
    ap.add_argument("--near-km", type=float, default=12.0, help="only buses this close to the old town")
    args = ap.parse_args()
    at = datetime.fromisoformat(args.at).replace(tzinfo=TZ)
    moments = [at] + [datetime.fromisoformat(a).replace(tzinfo=TZ) for a in args.also]
    zips = [(f, CACHE_DIR / "gtfs" / f"{f.number}_google_transit.zip") for f in FEEDS]
    tt = Timetable.from_zips([(f, p) for f, p in zips if p.exists()])
    buses = []
    for n, when in enumerate(moments):
        buses += place(tt, when, at, args.near_km, suffix="" if n == 0 else f"-{n}")
    body = {"status": "live", "error": None, "count": len(buses), "fetched_at": int(at.timestamp()),
            "feed_time": int(at.timestamp()), "timetable": "ready", "buses": buses}
    Path(args.out).write_text(json.dumps(body), encoding="utf-8")
    print(f"{len(buses)} buses at {at:%a %d %b %H:%M} within {args.near_km:g} km -> {args.out}")


def place(tt: Timetable, when: datetime, at: datetime, near_km: float, suffix: str) -> list[dict]:
    buses = []
    for trip, day in tt.trips_running(when):
        shape = tt.shapes.get(trip.shape_id or "")
        stops_along = tt.along(trip)
        line = tt.lines.get(trip.line_id)
        if shape is None or stops_along is None or line is None:
            continue
        secs = int((when - tt.midnight(day)).total_seconds())
        hit = scheduled_along(tt, trip, stops_along, secs)
        if hit is None:
            continue
        along, kmh = hit
        lon, lat, heading = point_at(shape, along)
        d = math.hypot((lon - CENTRE[0]) * m_per_deg_lon(CENTRE[1]), (lat - CENTRE[1]) * 110_574)
        if d > near_km * 1000:
            continue
        buses.append({
            "id": f"bus-tt-{trip.idx}{suffix}", "line": line.short, "line_id": line.id, "color": line.color,
            "text_color": line.text_color, "headsign": trip.headsign, "lon": round(lon, 6),
            "lat": round(lat, 6), "heading": round(heading), "speed_kmh": round(min(kmh, 70)),
            "reported": int(at.timestamp()), "along_m": round(along, 1), "shape_id": trip.shape_id,
            "delay_s": 0, "delay_source": "timetable",
        })
    return buses


if __name__ == "__main__":
    main()

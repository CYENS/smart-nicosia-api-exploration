"""The buses on the road now: the national GTFS-RT feed, joined to the timetable.

The feed (plain HTTP on a raw IP, island-wide, about 440 KB) is fetched at most every
`TTL_S` by one request shared by every page and tool, and kept as the last good copy for a few
minutes when it fails, so a hiccup doesn't empty the map. Past that the fleet says plainly that
the feed is unavailable.

Each vehicle is matched to its static trip by route, direction and first departure (the live
feed's trip ids are its own), placed along the trip's shape (so the page can glide it along the
street, not across blocks), and given its next stops with times. Delays come from the feed's
trip updates when they are believable, and otherwise from where the bus is against the
timetable (said as an estimate).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from chatbot.mobility.gtfs import TZ, Timetable, Trip, point_at, project

log = logging.getLogger(__name__)

RT_URL = "http://20.19.98.194:8328/Api/api/gtfs-realtime"
LICENCE = "CC BY 4.0, Ministry of Transport, Communications and Works (Cyprus National Access Point)"
TTL_S = 30.0  # vehicles report every 30 to 90 s; one fetch serves everyone for this long
KEEP_S = 300.0  # the last good copy stands in this long while the feed fails
STALE_S = 600.0  # a vehicle silent for longer has left service
# Greater Nicosia and the roads into it, as lon_min, lat_min, lon_max, lat_max.
AREA = (33.05, 34.98, 33.70, 35.28)
# Feed delays outside this are glitches (a bus parked at its terminus an hour early).
DELAY_MIN_S, DELAY_MAX_S = -600, 1800
NEXT_STOPS = 6


@dataclass
class Snapshot:
    fetched_at: float  # epoch seconds
    feed_time: float | None
    vehicles: list[dict]
    updates: dict[tuple, list[tuple[int, str, int | None, int | None]]] = field(default_factory=dict)
    error: str | None = None
    stale: bool = False

    @property
    def status(self) -> str:
        if self.error and not self.vehicles and not self.stale:
            return "unavailable"
        return "stale" if self.stale else "live"


def _key(trip) -> tuple[str, int, str]:
    return (trip.route_id, int(trip.direction_id), trip.start_time)


def parse_feed(data: bytes) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    try:
        msg.ParseFromString(data)
    except DecodeError as e:
        raise ValueError(f"the bus feed sent something that isn't GTFS-RT: {e}") from None
    return msg


class Fleet:
    """The live buses, refreshed on demand at most every TTL_S."""

    def __init__(self, fetch, timetable, now=None, ttl_s: float = TTL_S):
        self.fetch = fetch  # fetch(url) -> bytes
        self.timetable = timetable  # callable -> Timetable | None
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ttl_s = ttl_s
        self._snap: Snapshot | None = None
        self._lock = threading.Lock()
        self._along: dict[str, tuple[str, float]] = {}  # vehicle -> (trip id, metres along)
        self.fetches = 0

    def snapshot(self) -> Snapshot:
        now = self.now().timestamp()
        snap = self._snap
        if snap and now - snap.fetched_at < self.ttl_s:
            return snap
        with self._lock:  # one fetch at a time; the others take its result
            snap = self._snap
            if snap and now - snap.fetched_at < self.ttl_s:
                return snap
            self._snap = self._refresh(snap)
            return self._snap

    def _refresh(self, previous: Snapshot | None) -> Snapshot:
        now = self.now().timestamp()
        tt = self.timetable()
        try:
            self.fetches += 1
            msg = parse_feed(self.fetch(RT_URL))
        except Exception as e:  # noqa: BLE001 - the feed is fragile; say so, keep the last copy
            reason = str(e)[:200]
            log.warning("bus feed failed: %s", reason)
            if previous and previous.vehicles and now - previous.fetched_at < KEEP_S:
                return Snapshot(now - self.ttl_s + 10, previous.feed_time, previous.vehicles,
                                previous.updates, reason, stale=True)
            return Snapshot(now - self.ttl_s + 15, None, [], {}, reason)
        updates: dict[tuple, list] = {}
        for e in msg.entity:
            if e.HasField("trip_update"):
                tu = e.trip_update
                rows = []
                for s in tu.stop_time_update:
                    arr = s.arrival.delay if s.HasField("arrival") and s.arrival.HasField("delay") else None
                    dep = s.departure.delay if s.HasField("departure") and s.departure.HasField("delay") else None
                    rows.append((s.stop_sequence, s.stop_id, arr, dep))
                rows.sort(key=lambda r: r[0])
                updates[_key(tu.trip)] = rows
        vehicles = []
        for e in msg.entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            p = v.position
            if not (AREA[0] <= p.longitude <= AREA[2] and AREA[1] <= p.latitude <= AREA[3]):
                continue
            if v.timestamp and now - v.timestamp > STALE_S:
                continue
            if tt is not None and v.trip.route_id not in tt.routes:
                continue  # another town's buses passing through
            bus = self._bus(tt, v, updates, now)
            if bus:
                vehicles.append(bus)
        header = msg.header.timestamp or None
        return Snapshot(now, float(header) if header else None, vehicles, updates)

    def _bus(self, tt: Timetable | None, v, updates: dict, now: float) -> dict | None:
        vid = (v.vehicle.id or v.vehicle.label or "").strip()
        if not vid:
            return None
        p = v.position
        bus = {
            "id": f"bus-{vid}", "vehicle": vid, "plate": v.vehicle.license_plate or v.vehicle.label or None,
            "route_id": v.trip.route_id, "direction": int(v.trip.direction_id),
            "start_time": v.trip.start_time, "start_date": v.trip.start_date,
            "lon": round(p.longitude, 6), "lat": round(p.latitude, 6),
            "bearing": round(p.bearing) if p.HasField("bearing") else None,
            "speed_kmh": round(p.speed * 3.6) if p.HasField("speed") else None,
            "reported": int(v.timestamp) if v.timestamp else int(now),
            "line_id": None, "line": "?", "color": "#8798b0", "text_color": "#0e1828",
            "operator": None, "headsign": None, "trip": None, "shape_id": None,
            "along_m": None, "next_stops": [], "delay_s": None, "delay_source": None,
        }
        if tt is None:
            return bus
        route = tt.routes.get(v.trip.route_id)
        line = tt.lines.get(route.line_id) if route else None
        if line:
            bus.update(line_id=line.id, line=line.short, color=line.color, text_color=line.text_color,
                       operator=line.operator, line_name=line.name)
        trip = tt.trip_for_live(v.trip.route_id, int(v.trip.direction_id), v.trip.start_time,
                                v.trip.start_date)
        if trip is None:
            return bus
        bus.update(trip=trip.idx, trip_id=trip.id, headsign=trip.headsign, shape_id=trip.shape_id)
        try:
            day = datetime.strptime(v.trip.start_date, "%Y%m%d").date()
        except ValueError:
            day = datetime.fromtimestamp(now, TZ).date()
        base = tt.midnight(day)
        shape = tt.shapes.get(trip.shape_id or "")
        stops_along = tt.along(trip)
        along = None
        if shape is not None:
            prev = self._along.get(vid)
            lo, hi = -1.0, float("inf")
            if prev and prev[0] == trip.id:  # it only moves forward along its own trip
                lo, hi = prev[1] - 150, prev[1] + 4000
            along, off = project(shape, p.longitude, p.latitude, lo, hi)
            if off > 120 and lo > 0:
                along, off = project(shape, p.longitude, p.latitude)
            if off > 250:
                along = None  # off its route (a detour, or a bad fix): don't snap it
            else:
                self._along[vid] = (trip.id, along)
                lon, lat, heading = point_at(shape, along)
                bus["heading"] = round(heading)
                bus["along_m"] = round(along, 1)
                bus["shape_len_m"] = round(shape.length_m)
        if bus.get("heading") is None:
            bus["heading"] = bus["bearing"]
        # Where it is in the trip: the first stop still ahead.
        pos = 0
        if along is not None and stops_along is not None:
            pos = next((i for i, a in enumerate(stops_along) if a > along + 15), len(trip.stops))
        elif v.stop_id and v.stop_id in tt.stop_index:
            s = tt.stop_index[v.stop_id]
            pos = next((i for i, x in enumerate(trip.stops) if x == s), 0)
        bus["progress"] = {"stop": pos, "stops": len(trip.stops)}
        if along is not None and stops_along is not None and along >= stops_along[-1] - 40:
            bus["finished"] = True  # at its last stop: done, not late
            return bus
        delay, source = self._delay(trip, pos, updates.get(_key(v.trip)), stops_along, along, base, now)
        bus["delay_s"], bus["delay_source"] = delay, source
        nxt = []
        for i in range(pos, min(len(trip.stops), pos + NEXT_STOPS)):
            st = tt.stops[trip.stops[i]]
            when = base + timedelta(seconds=trip.arr[i])
            item = {"id": f"stop-{st.id}", "name": st.name, "scheduled": when.strftime("%H:%M"),
                    "lon": st.lon, "lat": st.lat}
            if delay is not None:
                item["expected"] = (when + timedelta(seconds=delay)).strftime("%H:%M")
            nxt.append(item)
        bus["next_stops"] = nxt
        if pos == 0 and trip.dep and base.timestamp() + trip.dep[0] > now + 60:
            bus["waiting"] = (base + timedelta(seconds=trip.dep[0])).strftime("%H:%M")
        return bus

    @staticmethod
    def _delay(trip: Trip, pos: int, rows, stops_along, along, base: datetime,
               now: float) -> tuple[int | None, str | None]:
        """Seconds late (negative: early) at the next stop. The feed's own figure when it is
        believable; else where the bus is against the timetable."""
        if rows:
            seq_rows = [r for r in rows if r[0] <= pos] or rows[:1]
            r = seq_rows[-1]
            d = r[3] if r[0] == 0 and r[3] is not None else (r[2] if r[2] is not None else r[3])
            if d is not None and DELAY_MIN_S <= d <= DELAY_MAX_S and not (pos == 0 and d < 0):
                return int(d), "feed"
        if along is None or stops_along is None or not len(trip.stops):
            return None, None
        if pos == 0:  # not left its first stop yet: late only once its departure has passed
            late = now - (base.timestamp() + trip.dep[0])
            return (int(late), "timetable") if 60 < late <= DELAY_MAX_S else (None, None)
        if pos >= len(trip.stops):
            return None, None
        a0, a1 = stops_along[pos - 1], stops_along[pos]
        t0, t1 = trip.dep[pos - 1], trip.arr[pos]
        frac = 0.0 if a1 <= a0 else max(0.0, min(1.0, (along - a0) / (a1 - a0)))
        due = base.timestamp() + t0 + frac * (t1 - t0)
        d = now - due
        return (int(d), "timetable") if DELAY_MIN_S <= d <= DELAY_MAX_S else (None, None)


def delay_text(s: int | None) -> str | None:
    """'3 min late', 'on time', '2 min early'."""
    if s is None:
        return None
    m = round(s / 60)
    if abs(m) < 1:
        return "on time"
    return f"{m} min late" if m > 0 else f"{-m} min early"

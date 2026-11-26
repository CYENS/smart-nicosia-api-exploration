"""The bus timetable: Nicosia's static GTFS from the Cyprus national access point.

Three feeds run buses in Greater Nicosia, all published by the Ministry of Transport on
motionbuscard.org.cy under CC BY 4.0: NPT (the city buses), Intercity (to and from the other
towns) and Pame Express (the GSP park and ride). Their stop ids are one national registry, so
the three merge into one timetable. The zips are cached in data/chatbot_cache/gtfs/ and fetched
again once a day at most; when a fetch fails the last copy keeps working.

Times are seconds after the service day's midnight and can pass 24:00 (a trip that starts at
23:40 runs into the next calendar day on its own service day). A trip runs on a date when its
service runs then (calendar.txt and calendar_dates.txt, both optional).
"""

from __future__ import annotations

import bisect
import csv
import io
import logging
import math
import threading
import time
import zipfile
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chatbot.settings import CACHE_DIR, TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

GTFS_URL = "https://motionbuscard.org.cy/opendata/downloadfile?file=GTFS%5C{n}_google_transit.zip&rel=True"
REFRESH_S = 24 * 3600  # the feeds change every few months; look once a day at most
LICENCE = "CC BY 4.0, Ministry of Transport, Communications and Works (Cyprus National Access Point)"


@dataclass(frozen=True)
class Feed:
    key: str  # used in line ids: line-npt-26
    operator: str
    number: int  # the file number on motionbuscard.org.cy
    about: str

    @property
    def url(self) -> str:
        return GTFS_URL.format(n=self.number)


FEEDS = (
    Feed("npt", "NPT", 9, "Nicosia city buses"),
    Feed("ic", "Intercity", 5, "Intercity buses between the towns"),
    Feed("pame", "Pame Express", 11, "Park and ride"),
)


@dataclass(frozen=True)
class Stop:
    idx: int
    id: str
    code: str
    name: str
    name_el: str | None
    lat: float
    lon: float


@dataclass(frozen=True)
class Line:
    """What a passenger calls a line: one operator's route number, both directions."""
    id: str  # line-npt-26
    feed: str
    operator: str
    short: str  # "26"
    name: str  # the long name of its first route, "Makario - Pournara - Makario"
    name_el: str | None
    color: str  # tuned for the night map (see line_color)
    text_color: str
    raw_color: str  # as the operator publishes it
    route_ids: tuple[str, ...]


@dataclass(frozen=True)
class Route:
    id: str
    line_id: str
    long_name: str
    direction_hint: int


@dataclass
class Trip:
    idx: int
    id: str
    route_id: str
    line_id: str
    service: str  # feed-qualified service id, "npt:25066"
    headsign: str
    headsign_el: str | None
    shape_id: str | None
    direction: int
    stops: array = field(default_factory=lambda: array("i"))  # stop idx per stop
    arr: array = field(default_factory=lambda: array("i"))  # seconds after the service midnight
    dep: array = field(default_factory=lambda: array("i"))

    @property
    def start(self) -> int:
        return self.dep[0] if self.dep else 0


@dataclass
class Shape:
    id: str
    coords: list[tuple[float, float]]  # (lon, lat)
    cum: array  # metres along the shape at each point

    @property
    def length_m(self) -> float:
        return self.cum[-1] if self.cum else 0.0


# -- colours ------------------------------------------------------------------------------------

NIGHT = (0x0E, 0x18, 0x28)


def _lum(rgb: tuple[float, float, float]) -> float:
    def ch(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _rgb(hexstr: str) -> tuple[int, int, int] | None:
    h = (hexstr or "").strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def line_color(raw: str) -> tuple[str, str]:
    """An operator's line colour lifted until it stands out on the night map (3:1 against
    #0e1828), and the text colour that reads on it. Dark blues and olives otherwise vanish."""
    rgb = _rgb(raw) or (0x3D, 0x8B, 0xFF)
    mixed = rgb
    for k in range(0, 11):
        t = k / 10 * 0.7
        mixed = tuple(round(c + (255 - c) * t) for c in rgb)
        if contrast(mixed, NIGHT) >= 3.0:
            break
    color = "#%02x%02x%02x" % mixed
    text = "#0e1828" if contrast(mixed, (0x0E, 0x18, 0x28)) >= contrast(mixed, (255, 255, 255)) else "#ffffff"
    return color, text


# -- geometry -----------------------------------------------------------------------------------

M_PER_DEG_LAT = 110_574.0


def m_per_deg_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


def cumulative(coords: list[tuple[float, float]]) -> array:
    out = array("d", [0.0])
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        kx = m_per_deg_lon((y1 + y2) / 2)
        out.append(out[-1] + math.hypot((x2 - x1) * kx, (y2 - y1) * M_PER_DEG_LAT))
    return out


def project(shape: Shape, lon: float, lat: float, lo_m: float = -1.0,
            hi_m: float = math.inf) -> tuple[float, float]:
    """(metres along the shape, metres off it) of the shape's nearest point to (lon, lat),
    looking only between lo_m and hi_m along it (a loop passes some places twice)."""
    best = (math.inf, 0.0)
    kx = m_per_deg_lon(lat)
    c = shape.coords
    first = max(0, bisect.bisect_right(shape.cum, lo_m) - 1) if lo_m > 0 else 0
    for i in range(first, len(c) - 1):
        if shape.cum[i] > hi_m:
            break
        if shape.cum[i + 1] < lo_m:
            continue
        ax, ay = (c[i][0] - lon) * kx, (c[i][1] - lat) * M_PER_DEG_LAT
        bx, by = (c[i + 1][0] - lon) * kx, (c[i + 1][1] - lat) * M_PER_DEG_LAT
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(px, py)
        if d < best[0]:
            best = (d, shape.cum[i] + t * (shape.cum[i + 1] - shape.cum[i]))
    return best[1], best[0]


def point_at(shape: Shape, along: float) -> tuple[float, float, float]:
    """(lon, lat, heading in degrees clockwise from north) at `along` metres."""
    c, cum = shape.coords, shape.cum
    if len(c) < 2:
        return (c[0][0], c[0][1], 0.0) if c else (0.0, 0.0, 0.0)
    along = max(0.0, min(along, cum[-1]))
    i = max(0, min(len(c) - 2, bisect.bisect_right(cum, along) - 1))
    span = cum[i + 1] - cum[i]
    t = 0.0 if span == 0 else (along - cum[i]) / span
    (x1, y1), (x2, y2) = c[i], c[i + 1]
    kx = m_per_deg_lon((y1 + y2) / 2)
    heading = (math.degrees(math.atan2((x2 - x1) * kx, (y2 - y1) * M_PER_DEG_LAT)) + 360) % 360
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1), heading


def slice_shape(shape: Shape, a: float, b: float) -> list[list[float]]:
    """The shape between a and b metres along it, as [[lon, lat], ...]."""
    if b < a:
        a, b = b, a
    pts = [list(point_at(shape, a)[:2])]
    for (x, y), m in zip(shape.coords, shape.cum):
        if a < m < b:
            pts.append([x, y])
    pts.append(list(point_at(shape, b)[:2]))
    return [[round(x, 6), round(y, 6)] for x, y in pts]


def simplify(coords: list[tuple[float, float]], tolerance_m: float = 2.0) -> list[tuple[float, float]]:
    """Douglas-Peucker in local metres: a lighter line for the page that looks the same."""
    if len(coords) < 3:
        return list(coords)
    lat0 = coords[0][1]
    kx = m_per_deg_lon(lat0)
    pts = [(x * kx, y * M_PER_DEG_LAT) for x, y in coords]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        s, e = stack.pop()
        (ax, ay), (bx, by) = pts[s], pts[e]
        dx, dy = bx - ax, by - ay
        norm2 = dx * dx + dy * dy
        worst, at = -1.0, -1
        for i in range(s + 1, e):
            px, py = pts[i]
            if norm2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / norm2))
                d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > worst:
                worst, at = d, i
        if worst > tolerance_m:
            keep[at] = True
            stack += [(s, at), (at, e)]
    return [c for c, k in zip(coords, keep) if k]


# -- parsing ------------------------------------------------------------------------------------


def _seconds(text: str) -> int | None:
    try:
        h, m, s = text.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def _rows(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _day(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%Y%m%d").date()
    except ValueError:
        return None


class Timetable:
    """Every stop, line, trip and shape of the feeds, with what the planner and the live map
    need: departures by stop, trips by (route, direction, start) for matching the live feed,
    and stop distances along each trip's shape."""

    def __init__(self) -> None:
        self.stops: list[Stop] = []
        self.stop_index: dict[str, int] = {}
        self.lines: dict[str, Line] = {}
        self.routes: dict[str, Route] = {}
        self.trips: list[Trip] = []
        self.shapes: dict[str, Shape] = {}
        self.feeds: list[dict] = []  # per feed: operator, version, valid from and to
        self.service_dates: dict[str, set[date]] = defaultdict(set)
        self._weekly: list[tuple[str, tuple[int, ...], date, date]] = []
        self._removed: dict[str, set[date]] = defaultdict(set)
        # departures per stop: (seconds, trip idx, position in the trip), sorted by seconds
        self._dep_s: list[array] = []
        self._dep_trip: list[array] = []
        self._dep_pos: list[array] = []
        self._by_start: dict[tuple[str, int, int], list[int]] = defaultdict(list)
        self._lines_of_stop: list[set[str]] = []
        self._calls: list[array] = []
        self._along: dict[tuple, array] = {}
        self._lock = threading.Lock()
        self._active: dict[date, frozenset[str]] = {}

    # -- loading ---------------------------------------------------------------------------

    @classmethod
    def from_zips(cls, zips: list[tuple[Feed, Path | bytes]]) -> Timetable:
        tt = cls()
        for feed, src in zips:
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                tt._load_feed(feed, z)
        tt._index()
        return tt

    def _load_feed(self, feed: Feed, z: zipfile.ZipFile) -> None:
        tr: dict[tuple[str, str, str], str] = {}
        for r in _rows(z, "translations.txt"):
            if r.get("language") == "el":
                tr[(r.get("table_name", ""), r.get("field_name", ""), r.get("record_id", ""))] = r.get("translation", "")
        info = next(_rows(z, "feed_info.txt"), None) or {}
        self.feeds.append({"key": feed.key, "operator": feed.operator, "about": feed.about,
                           "version": info.get("feed_version"), "url": feed.url,
                           "valid_from": info.get("feed_start_date"),
                           "valid_to": info.get("feed_end_date")})
        for r in _rows(z, "stops.txt"):
            sid = r["stop_id"]
            if sid in self.stop_index:
                continue  # one national registry: the same stop in another feed
            try:
                lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
            except (KeyError, ValueError):
                continue
            self.stop_index[sid] = len(self.stops)
            self.stops.append(Stop(len(self.stops), sid, (r.get("stop_code") or sid).strip(),
                                   (r.get("stop_name") or sid).strip(),
                                   tr.get(("stops", "stop_name", sid)) or None, lat, lon))
        by_short: dict[str, list[dict]] = defaultdict(list)
        for r in _rows(z, "routes.txt"):
            by_short[(r.get("route_short_name") or r["route_id"]).strip()].append(r)
        for short, routes in by_short.items():
            lid = f"line-{feed.key}-{short.lower().replace(' ', '')}"
            first = routes[0]
            color, text = line_color(first.get("route_color") or "")
            self.lines[lid] = Line(
                lid, feed.key, feed.operator, short, (first.get("route_long_name") or "").strip(),
                tr.get(("routes", "route_long_name", first["route_id"])) or None, color, text,
                "#" + (first.get("route_color") or "3d8bff").strip().lstrip("#"),
                tuple(r["route_id"] for r in routes))
            for r in routes:
                self.routes[r["route_id"]] = Route(r["route_id"], lid, (r.get("route_long_name") or "").strip(), 0)
        for r in _rows(z, "calendar.txt"):
            days = tuple(i for i, k in enumerate(("monday", "tuesday", "wednesday", "thursday",
                                                  "friday", "saturday", "sunday")) if r.get(k) == "1")
            a, b = _day(r.get("start_date", "")), _day(r.get("end_date", ""))
            if a and b:
                self._weekly.append((f"{feed.key}:{r['service_id']}", days, a, b))
        for r in _rows(z, "calendar_dates.txt"):
            d = _day(r.get("date", ""))
            if d is None:
                continue
            key = f"{feed.key}:{r['service_id']}"
            if r.get("exception_type") == "2":
                self._removed[key].add(d)
            else:
                self.service_dates[key].add(d)
        pts: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        for r in _rows(z, "shapes.txt"):
            try:
                pts[r["shape_id"]].append((int(r["shape_pt_sequence"]), float(r["shape_pt_lon"]),
                                           float(r["shape_pt_lat"])))
            except (KeyError, ValueError):
                continue
        for sid, p in pts.items():
            p.sort()
            coords = [(x, y) for _, x, y in p]
            self.shapes[sid] = Shape(sid, coords, cumulative(coords))
        trip_at: dict[str, int] = {}
        for r in _rows(z, "trips.txt"):
            route = self.routes.get(r["route_id"])
            if route is None:
                continue
            idx = len(self.trips)
            trip_at[r["trip_id"]] = idx
            try:
                direction = int(r.get("direction_id") or 0)
            except ValueError:
                direction = 0
            self.trips.append(Trip(idx, r["trip_id"], r["route_id"], route.line_id,
                                   f"{feed.key}:{r['service_id']}",
                                   (r.get("trip_headsign") or "").strip(),
                                   tr.get(("trips", "trip_headsign", r["trip_id"])) or None,
                                   r.get("shape_id") or None, direction))
        times: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        for r in _rows(z, "stop_times.txt"):
            t = trip_at.get(r["trip_id"])
            s = self.stop_index.get(r["stop_id"])
            if t is None or s is None:
                continue
            arr, dep = _seconds(r.get("arrival_time", "")), _seconds(r.get("departure_time", ""))
            if arr is None and dep is None:
                continue
            try:
                seq = int(r["stop_sequence"])
            except (KeyError, ValueError):
                continue
            times[t].append((seq, s, arr if arr is not None else dep, dep if dep is not None else arr))
        for t, rows in times.items():
            rows.sort()
            trip = self.trips[t]
            trip.stops = array("i", (s for _, s, _, _ in rows))
            trip.arr = array("i", (a for _, _, a, _ in rows))
            trip.dep = array("i", (d for _, _, _, d in rows))
            last = self.stops[trip.stops[-1]].name
            # Headsigns are cut at 17 characters ("Alampra Bus Stati"): the last stop says it whole.
            if not trip.headsign or (last.lower().startswith(trip.headsign.lower()) and len(last) > len(trip.headsign)):
                trip.headsign = last
            if trip.headsign.isupper():  # Intercity writes "LARNACA"
                trip.headsign = trip.headsign.title()

    def _index(self) -> None:
        n = len(self.stops)
        deps: list[list[tuple[int, int, int]]] = [[] for _ in range(n)]
        calls: list[list[int]] = [[] for _ in range(n)]
        self._lines_of_stop = [set() for _ in range(n)]
        for trip in self.trips:
            if len(trip.stops) < 2:
                continue
            self._by_start[(trip.route_id, trip.direction, trip.start)].append(trip.idx)
            for pos, s in enumerate(trip.stops):
                self._lines_of_stop[s].add(trip.line_id)
                calls[s].append(trip.idx)
                if pos < len(trip.stops) - 1:  # no one boards at the last stop
                    deps[s].append((trip.dep[pos], trip.idx, pos))
        for lst in deps:
            lst.sort()
            self._dep_s.append(array("i", (x[0] for x in lst)))
            self._dep_trip.append(array("i", (x[1] for x in lst)))
            self._dep_pos.append(array("i", (x[2] for x in lst)))
        self._calls = [array("i", sorted(set(c))) for c in calls]

    def trips_calling(self, stop_idx: int) -> array:
        """Every trip that stops here (terminus included), by trip idx."""
        return self._calls[stop_idx]

    # -- services --------------------------------------------------------------------------

    def active(self, day: date) -> frozenset[str]:
        """The service ids running on `day`."""
        with self._lock:
            hit = self._active.get(day)
        if hit is not None:
            return hit
        on = {s for s, dates in self.service_dates.items() if day in dates}
        for s, days, a, b in self._weekly:
            if a <= day <= b and day.weekday() in days and day not in self._removed[s]:
                on.add(s)
        out = frozenset(on)
        with self._lock:
            if len(self._active) > 14:
                self._active.clear()
            self._active[day] = out
        return out

    def runs(self, trip: Trip, day: date) -> bool:
        return trip.service in self.active(day)

    @staticmethod
    def midnight(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=TZ)

    # -- lookups ---------------------------------------------------------------------------

    def stop(self, stop_id: str) -> Stop | None:
        i = self.stop_index.get(stop_id)
        return self.stops[i] if i is not None else None

    def lines_at(self, stop: Stop) -> list[Line]:
        return sorted((self.lines[i] for i in self._lines_of_stop[stop.idx]), key=line_order)

    def trip_for_live(self, route_id: str, direction: int, start_time: str,
                      start_date: str | None) -> Trip | None:
        """The static trip a live vehicle is running: the live feed's trip ids are its own,
        so match on route, direction and the first departure, on the service date."""
        start = _seconds(start_time or "")
        if start is None:
            return None
        cands = self._by_start.get((route_id, direction, start)) or []
        day = _day(start_date) if start_date else None
        for t in cands:
            if day is None or self.runs(self.trips[t], day):
                return self.trips[t]
        return self.trips[cands[0]] if cands else None

    def along(self, trip: Trip) -> array | None:
        """Metres along the trip's shape at each of its stops (each found after the one before,
        so a loop that passes a stop twice keeps its order)."""
        shape = self.shapes.get(trip.shape_id or "")
        if shape is None:
            return None
        key = (shape.id, trip.stops.tobytes())  # trips of one pattern share it
        with self._lock:
            hit = self._along.get(key)
        if hit is not None:
            return hit
        out = array("d")
        lo = -1.0
        for s in trip.stops:
            st = self.stops[s]
            # The next stop is usually within a few kilometres: look there first.
            a, off = project(shape, st.lon, st.lat, lo_m=lo, hi_m=lo + 4000)
            if off > 80:
                a, off = project(shape, st.lon, st.lat, lo_m=lo)
            out.append(a)
            lo = a - 5
        with self._lock:
            if len(self._along) > 4000:
                self._along.clear()
            self._along[key] = out
        return out

    def departures(self, stop_idxs: list[int], start: datetime, minutes: float,
                   lines: set[str] | None = None) -> list[tuple[datetime, Trip, int]]:
        """Departures from these stops between start and start + minutes, soonest first, as
        (when, trip, position of the stop in the trip). Trips of yesterday's service day that
        run past midnight count too."""
        start = start.astimezone(TZ)
        end = start + timedelta(minutes=minutes)
        out: list[tuple[datetime, Trip, int]] = []
        today = start.date()
        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            base = self.midnight(day)
            lo = int((start - base).total_seconds())
            hi = int((end - base).total_seconds())
            if hi < 0 or lo > 48 * 3600:
                continue
            active = self.active(day)
            for s in stop_idxs:
                secs = self._dep_s[s]
                i = bisect.bisect_left(secs, lo)
                j = bisect.bisect_right(secs, hi)
                for k in range(i, j):
                    trip = self.trips[self._dep_trip[s][k]]
                    if trip.service not in active or (lines and trip.line_id not in lines):
                        continue
                    out.append((base + timedelta(seconds=secs[k]), trip, self._dep_pos[s][k]))
        out.sort(key=lambda x: (x[0], x[1].line_id))
        return out

    def trips_running(self, at: datetime) -> list[tuple[Trip, date]]:
        """Trips under way at `at` by the timetable (for "how many buses should be out")."""
        at = at.astimezone(TZ)
        out = []
        for day in (at.date() - timedelta(days=1), at.date()):
            secs = int((at - self.midnight(day)).total_seconds())
            active = self.active(day)
            for t in self.trips:
                if t.dep and t.service in active and t.dep[0] <= secs <= t.arr[-1]:
                    out.append((t, day))
        return out

    def near(self, lat: float, lon: float, radius_m: float, limit: int = 12,
             arriving: bool = False) -> list[tuple[float, Stop]]:
        """Stops within radius_m of a point, nearest first, as (metres, stop): stops a bus
        leaves from, or with `arriving` any stop a bus calls at (a terminus too)."""
        kx = m_per_deg_lon(lat)
        dlat, dlon = radius_m / M_PER_DEG_LAT, radius_m / kx
        out = []
        for s in self.stops:
            if abs(s.lat - lat) > dlat or abs(s.lon - lon) > dlon:
                continue
            d = math.hypot((s.lon - lon) * kx, (s.lat - lat) * M_PER_DEG_LAT)
            if d <= radius_m and (self._dep_s[s.idx] or (arriving and len(self._calls[s.idx]))):
                out.append((d, s))
        out.sort(key=lambda x: x[0])
        return out[:limit]


def line_order(line: Line) -> tuple:
    """City lines first, then by number as a passenger reads it (2 before 10)."""
    num = "".join(ch for ch in line.short if ch.isdigit())
    return ({"npt": 0, "pame": 1, "ic": 2}.get(line.feed, 3), int(num) if num else 9999, line.short)


# -- the cached copy on disk --------------------------------------------------------------------


class TimetableSource:
    """Loads the timetable from the cached zips, fetching a feed when its copy is missing or a
    day old. Loading takes a few seconds, so it happens once, in the background, and again
    after a refresh brings a new version."""

    def __init__(self, fetch, cache_dir: Path | None = None, feeds: tuple[Feed, ...] = FEEDS,
                 refresh_s: float = REFRESH_S):
        self.fetch = fetch  # fetch(url) -> bytes
        self.cache_dir = cache_dir or (CACHE_DIR / "gtfs")
        self.feeds = feeds
        self.refresh_s = refresh_s
        self._tt: Timetable | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()
        self._loading = threading.Event()
        self.error: str | None = None

    @property
    def current(self) -> Timetable | None:
        """The timetable in use, never starting a load (for the data sources panel)."""
        return self._tt

    @property
    def loaded_at(self) -> float:
        """When the timetable in use was loaded (epoch seconds; 0 before the first)."""
        return self._loaded_at

    def path(self, feed: Feed) -> Path:
        return self.cache_dir / f"{feed.number}_google_transit.zip"

    def _refresh_files(self) -> bool:
        """Fetch the feeds whose copy is old. True if any file changed."""
        changed = False
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for feed in self.feeds:
            p = self.path(feed)
            if p.exists() and time.time() - p.stat().st_mtime < self.refresh_s:
                continue
            try:
                data = self.fetch(feed.url)
                zipfile.ZipFile(io.BytesIO(data)).testzip()
            except Exception as e:  # noqa: BLE001 - keep the copy we have
                self.error = f"{feed.operator} timetable not refreshed: {e}"
                log.warning("%s", self.error)
                if p.exists():
                    p.touch()  # look again tomorrow, not on every request
                continue
            old = p.read_bytes() if p.exists() else None
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
            changed = changed or old != data
        return changed

    def get(self, wait_s: float = 0.0) -> Timetable | None:
        """The timetable, loading it on first use. With wait_s, waits that long for a load
        under way; otherwise None until it is ready."""
        tt = self._tt
        stale = time.time() - self._loaded_at > self.refresh_s
        if tt is not None and not stale:
            return tt
        if self._lock.acquire(blocking=False):
            self._loading.clear()
            threading.Thread(target=self._load, daemon=True, name="gtfs-load").start()
        if tt is None and wait_s:
            self._loading.wait(wait_s)
        return self._tt

    def _load(self) -> None:
        try:
            changed = self._refresh_files()
            if self._tt is not None and not changed:
                self._loaded_at = time.time()
                return
            zips = [(f, self.path(f)) for f in self.feeds if self.path(f).exists()]
            if not zips:
                self.error = self.error or "no timetable could be downloaded"
                self._loaded_at = time.time() - self.refresh_s + 600  # try again in 10 minutes
                return
            started = time.monotonic()
            tt = Timetable.from_zips(zips)
            self._tt, self._loaded_at = tt, time.time()
            log.info("timetable: %d stops, %d lines, %d trips in %.1f s", len(tt.stops),
                     len(tt.lines), len(tt.trips), time.monotonic() - started)
        except Exception as e:  # noqa: BLE001 - a broken zip leaves the old timetable
            self.error = f"timetable failed to load: {e}"
            log.exception("timetable load failed")
            self._loaded_at = time.time() - self.refresh_s + 600
        finally:
            self._loading.set()
            self._lock.release()

"""Road speeds: the national Bluetooth travel times on Nicosia's main roads.

The Cyprus National Access Point (traffic4cyprus.org.cy, DATEX II, CC BY 4.0) times cars
between pairs of Bluetooth detectors about every 5 minutes. A path ("1004->1008") runs one way
between two detector sites; its geometry comes from the paths inventory, the sites' names from
the sites inventory (both cached on disk, refreshed daily), and the live speeds from the data
publication (fetched at most every 5 minutes, one request for every page and tool, and handed
to the twin's memory, so road speeds replay later and the usual for each hour builds up).

A speed means little without the road's own free-flow speed, which the feed doesn't give:
- measured: the 85th percentile of the speeds the memory holds for the road over 4 weeks,
  once it holds a day of readings (the speed most drivers manage when the road is clear);
- until then, estimated: the road's length over Valhalla's typical driving time for it (speed
  limits and road classes, no traffic), fetched once per road and cached;
- in the minute before that estimate exists, assumed: the urban limit, 50 km/h.
Speed against free flow puts each road in a band: moving freely, busy, slow, very slow.

The feed can stall: it keeps answering, with a fresh publication time, while the detectors behind
it have sent nothing new, so every path carries the same old measurement time (on 26 November
2026 they stopped at 23:42 and the feed republished that reading for days). That is its own
status, "stalled", never "no readings": the speeds say when the last measurement was, and each
road falls back to its usual speed for this hour of this weekday from the memory, marked as the
usual (`usual_state`), never as a reading. A road the memory knows too little about stays "no
reading".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from collector import parse
from collector.config import Config

from chatbot.mobility.gtfs import TZ, cumulative
from chatbot.settings import CACHE_DIR

log = logging.getLogger(__name__)

NAP = "https://www.traffic4cyprus.org.cy/swarco3/api/Data/"
PATHS_URL = NAP + "PredefinedLocationPublication"
SITES_URL = NAP + "BTMeasurementSiteTablePublication"
LIVE_URL = Config().nap_url  # PredefinedLocationDataPublication, as the collector polls it
LICENCE = "CC BY 4.0, Cyprus National Access Point (traffic4cyprus.org.cy)"
INVENTORY_S = 24 * 3600
LIVE_TTL_S = 300.0  # the feed publishes about every 5 minutes
STALE_S = 30 * 60  # a reading older than this is no reading
# Paths wholly inside Greater Nicosia (lon_min, lat_min, lon_max, lat_max).
AREA = (33.24, 35.07, 33.48, 35.23)
MIN_PLAUSIBLE_KMH = 3.0
ASSUMED_KMH = 50.0  # Cyprus's urban speed limit, while a road has no estimate yet
MEASURED_MIN = 288  # a day of 5-minute readings before free flow is the road's own
FREE_FLOW_Q = 0.85
FREE_FLOW_DAYS = 28
USUAL_WEEKS = 8
USUAL_MIN_DAYS = 2  # a stalled feed's roads show their usual only from two or more such days

BANDS = (  # (state, label, lowest share of free flow)
    ("moving", "Moving freely", 0.75),
    ("busy", "Busy", 0.55),
    ("slow", "Slow", 0.35),
    ("very_slow", "Very slow", 0.0),
)
BAND_LABELS = {s: label for s, label, _ in BANDS} | {"no_reading": "No reading"}
# Night base map colours (the page steps them for the day map): a recessive neutral for roads
# that flow, then one warm hue that brightens as they slow (validated as an ordinal ramp on
# #0e1828). Line width rises with slowness too, so colour never carries it alone.
SPEED_COLORS = {"moving": "#718295", "busy": "#bd6350", "slow": "#f17458",
                "very_slow": "#ff9a71", "no_reading": "#424e5e"}
LEGEND = {BAND_LABELS[s]: SPEED_COLORS[s] for s in ("moving", "busy", "slow", "very_slow", "no_reading")}
# While the feed is stalled the map draws the usual speeds dotted, in the muted ink, not the
# live ramp: nothing on it can pass for a reading.
USUAL_COLOR = "#8798b0"
USUAL_LABEL = "Usual for this hour"
STALLED_LEGEND = {USUAL_LABEL: USUAL_COLOR, BAND_LABELS["no_reading"]: SPEED_COLORS["no_reading"]}


def band(speed: float | None, free: float | None) -> str:
    if speed is None or not free:
        return "no_reading"
    share = speed / free
    return next(s for s, _, lo in BANDS if share >= lo)


def since_words(t: datetime, now: datetime) -> str:
    """When something last happened, as people say it: '14:05' today, 'Thursday 23:42' this
    week, '19 November 23:42' before."""
    local, today = t.astimezone(TZ), now.astimezone(TZ).date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if 0 < (today - local.date()).days < 7:
        return local.strftime("%A %H:%M")
    return f"{local.day} {local:%B %H:%M}"


def stalled_note(last: datetime, now: datetime) -> str:
    return f"The national road detectors have sent nothing new since {since_words(last, now)}."


def is_stalled(newest: datetime | None, now: datetime) -> bool:
    """The feed's newest measurement is older than a reading counts for."""
    return newest is not None and (now - newest).total_seconds() > STALE_S


@dataclass
class Segment:
    id: str  # road-12
    nap_id: str
    name: str  # "Griva - Prokopiou to Prodromou 2 - Griva"
    road: str | None  # the street both ends share, "Griva"
    short: str  # for tight spaces: the road, else "Kalamon roundabout to Athalassas"
    from_site: str
    to_site: str
    from_name: str | None
    to_name: str | None
    coords: list[tuple[float, float]]  # (lon, lat), in the direction of travel
    length_m: float

    @property
    def middle(self) -> tuple[float, float]:
        return self.coords[len(self.coords) // 2]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sites(xml_text: str) -> dict[str, dict]:
    """BTMeasurementSiteTablePublication: site number -> {name, lat, lon}."""
    out = {}
    for site in ET.fromstring(xml_text).iter():
        if _local(site.tag) != "measurementSite":
            continue
        f: dict[str, list[str]] = {}
        for el in site.iter():
            n = _local(el.tag)
            if n in ("value", "measurementSiteIdentification", "latitude", "longitude") and el.text:
                f.setdefault(n, []).append(el.text.strip())
        ident = (f.get("measurementSiteIdentification") or [None])[0]
        if not ident:
            continue
        names = [v for v in f.get("value", []) if v.lower() != "bluetooth"]
        try:
            lat, lon = float(f["latitude"][0]), float(f["longitude"][0])
        except (KeyError, ValueError, IndexError):
            lat = lon = None
        out[ident] = {"name": names[0] if names else None, "lat": lat, "lon": lon}
    return out


COORDS = re.compile(r"<gml:coordinates>(.*?)</gml:coordinates>", re.S)


def parse_paths(xml_text: str) -> dict[str, tuple[str, list[tuple[float, float]]]]:
    """PredefinedLocationPublication: path id -> (name "1004->1008", [(lon, lat), ...]). The
    line sits in posList as escaped GML with "lat,lon" pairs."""
    out = {}
    for ref in ET.fromstring(xml_text).iter():
        if _local(ref.tag) != "predefinedLocationReference":
            continue
        name = pos = None
        for el in ref.iter():
            if _local(el.tag) == "value" and name is None:
                name = (el.text or "").strip()
            elif _local(el.tag) == "posList":
                pos = el.text or ""
        coords: list[tuple[float, float]] = []
        m = COORDS.search(pos or "")
        for pair in (m.group(1).split() if m else []):
            try:
                la, lo = pair.split(",")[:2]
                coords.append((round(float(lo), 6), round(float(la), 6)))
            except ValueError:
                continue
        if ref.get("id") and name and len(coords) >= 2:
            out[ref.get("id")] = (name, coords)
    return out


def _title(name: str | None) -> str | None:
    """Site names come in either case ("LEOFOROS STROVOLOU"): read them in title case."""
    if not name:
        return None
    return name.title() if name.isupper() else name


def road_name(a: str | None, b: str | None) -> str | None:
    """The street two site names share ("Griva - Prokopiou", "Prodromou 2 - Griva" -> "Griva")."""
    def parts(s: str | None) -> dict[str, str]:
        out = {}
        for p in re.split(r"\s*(?:-|&)\s*", s or ""):
            p = re.sub(r"\s+(\d+|I+|A\d+)$", "", p.strip())
            if len(p) >= 4 and not p.lower().endswith("roundabout"):
                out[p.lower()] = p
        return out
    pa, pb = parts(a), parts(b)
    common = [pa[k] for k in pa if k in pb]
    return common[0] if common else None


def build_segments(paths: dict, sites: dict) -> list[Segment]:
    out = []
    for nap_id, (name, coords) in paths.items():
        if not all(AREA[0] <= x <= AREA[2] and AREA[1] <= y <= AREA[3] for x, y in coords):
            continue
        a, _, b = name.partition("->")
        fa, fb = _title((sites.get(a) or {}).get("name")), _title((sites.get(b) or {}).get("name"))
        label = f"{fa or 'site ' + a} to {fb or 'site ' + b}"
        road = road_name(fa, fb)
        first = [re.split(r"\s+-\s+|\s*&\s*", x or "")[0] or None for x in (fa, fb)]
        short = road or f"{first[0] or 'site ' + a} to {first[1] or 'site ' + b}"
        out.append(Segment(f"road-{nap_id}", nap_id, label, road, short, a, b, fa, fb,
                           coords, cumulative(coords)[-1]))
    out.sort(key=lambda s: int(s.nap_id) if s.nap_id.isdigit() else 0)
    return out


def parse_moment(at: object, now: datetime) -> datetime | None:
    """A moment from ?at= or a tool: an ISO time (local if it has no zone), epoch ms, or None."""
    if at is None or at == "":
        return None
    if isinstance(at, (int, float)) or (isinstance(at, str) and at.strip().isdigit()):
        return datetime.fromtimestamp(float(at) / 1000, tz=timezone.utc)
    text = str(at).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{at!r} isn't a time: give ISO like 2026-11-26T08:30") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    if dt > now + timedelta(minutes=1):
        raise ValueError("that moment is in the future; road speeds have no forecast yet")
    return dt


class Roads:
    """Nicosia's Bluetooth road segments with their speeds now or at a moment."""

    def __init__(self, fetch_text, memory=None, router=None, now=None,
                 cache_dir=None, live_ttl_s: float = LIVE_TTL_S):
        self.fetch_text = fetch_text  # fetch_text(url) -> str
        self.memory = memory
        self.router = router
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.cache_dir = cache_dir or CACHE_DIR
        self.live_ttl_s = live_ttl_s
        self._segments: list[Segment] | None = None
        self._segments_at = 0.0
        self._live: tuple[float, dict[str, parse.TravelTimeRow], str | None] | None = None
        self._lock = threading.Lock()
        self._inv_lock = threading.Lock()
        self._measured: tuple[float, dict[str, tuple[float, int]]] | None = None
        self._reference: dict[str, dict] | None = None
        self._reference_running = False
        self._usual: dict[tuple, tuple[float, dict]] = {}
        self.error: str | None = None
        # What the last good fetch found: when, its newest measurement and its publication time.
        self._read_at: float | None = None
        self._read_ok_at: float | None = None
        self.newest: datetime | None = None
        self.published: datetime | None = None

    # -- the inventory ---------------------------------------------------------------------

    def _cached_text(self, name: str, url: str) -> str:
        path = self.cache_dir / name
        if path.exists() and time.time() - path.stat().st_mtime < INVENTORY_S:
            return path.read_text(encoding="utf-8")
        try:
            text = self.fetch_text(url)
            ET.fromstring(text)  # never cache a broken copy
        except Exception as e:  # noqa: BLE001 - yesterday's copy is fine for an inventory
            if path.exists():
                log.warning("road inventory %s not refreshed: %s", name, e)
                path.touch()
                return path.read_text(encoding="utf-8")
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text

    def segments(self) -> list[Segment]:
        with self._inv_lock:
            if self._segments is not None and time.time() - self._segments_at < INVENTORY_S:
                return self._segments
            try:
                paths = parse_paths(self._cached_text("nap_paths.xml", PATHS_URL))
                sites = parse_sites(self._cached_text("nap_sites.xml", SITES_URL))
            except Exception as e:  # noqa: BLE001
                self.error = f"the road inventory couldn't be read: {e}"
                if self._segments is not None:
                    return self._segments
                raise
            self._segments, self._segments_at = build_segments(paths, sites), time.time()
            return self._segments

    def segment(self, sid: str) -> Segment | None:
        sid = sid.strip().lower()
        return next((s for s in self.segments() if s.id == sid), None)

    # -- live readings ---------------------------------------------------------------------

    def live(self) -> tuple[float, dict[str, parse.TravelTimeRow], str | None]:
        """(fetched at, the latest reading per path id, error). One fetch every LIVE_TTL_S."""
        now = time.time()
        hit = self._live
        if hit and now - hit[0] < self.live_ttl_s:
            return hit
        with self._lock:
            hit = self._live
            if hit and now - hit[0] < self.live_ttl_s:
                return hit
            self._read_at = now
            try:
                rows = parse.parse_nap(self.fetch_text(LIVE_URL))
            except Exception as e:  # noqa: BLE001 - keep what we had, say why
                err = f"the national traffic feed didn't answer: {str(e)[:160]}"
                log.warning("%s", err)
                old = hit[1] if hit else {}
                self._live = (now - self.live_ttl_s + 60, old, err)  # try again in a minute
                return self._live
            if self.memory is not None and self.memory.recorder is not None:
                # A stalled feed repeats one measurement: the memory keeps it once (its key is
                # the path and the measurement time), so it never weighs on the usual.
                self.memory.recorder.travel_times(rows)
            self._read_ok_at = now
            self.newest = max((r.measured_at for r in rows), default=None)
            self.published = next((r.publication_time for r in rows if r.publication_time), None)
            self._live = (now, {r.segment_id: r for r in rows}, None)
            return self._live

    def health(self) -> dict | None:
        """The feed as the last fetch left it, without fetching: when it was read and whether
        that worked, its newest measurement and publication time, and whether it is stalled
        (answering, but with nothing measured in the last 30 minutes). None before any read."""
        hit = self._live
        if hit is None:
            return None
        now = self.now()
        return {"read_at": self._read_at or hit[0], "read_ok_at": self._read_ok_at, "error": hit[2],
                "newest": self.newest, "published": self.published,
                "stalled": hit[2] is None and is_stalled(self.newest, now)}

    # -- free flow and the usual -------------------------------------------------------------

    def measured_free_flow(self) -> dict[str, tuple[float, int]]:
        """Path id -> (85th percentile speed, readings) over 4 weeks, from the memory."""
        hit = self._measured
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        out: dict[str, tuple[float, int]] = {}
        if self.memory is not None:
            since = self.now() - timedelta(days=FREE_FLOW_DAYS)
            try:
                with self.memory.reading() as con:
                    rows = con.execute(
                        f"""SELECT segment_id, quantile_cont(speed_kmh, {FREE_FLOW_Q}), count(*)
                            FROM travel_times WHERE speed_kmh > 0 AND measured_at >= ?
                            GROUP BY 1""", [since]).fetchall()
                out = {sid: (float(v), int(n)) for sid, v, n in rows if v}
            except Exception as e:  # noqa: BLE001 - Unavailable or an old file: estimate instead
                log.info("free flow from memory unavailable: %s", e)
        self._measured = (time.time(), out)
        return out

    def _reference_path(self):
        return self.cache_dir / "road_reference.json"

    def references(self) -> dict[str, dict]:
        """Path id -> {kmh, geom}: Valhalla's typical speed, computed once per road (in the
        background, one request a second) and kept on disk."""
        if self._reference is None:
            try:
                self._reference = json.loads(self._reference_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._reference = {}
        if self.router is not None and not self._reference_running:
            def stale(s: Segment) -> bool:  # new geometry, or no estimate and a day since trying
                e = self._reference.get(s.nap_id) or {}
                return e.get("geom") != _geom_key(s) or (not e.get("kmh") and time.time() - e.get("at", 0) > INVENTORY_S)
            missing = [s for s in self._segments or [] if stale(s)]
            if missing:
                self._reference_running = True
                threading.Thread(target=self._fill_references, args=(missing,), daemon=True,
                                 name="road-reference").start()
        return self._reference

    def _fill_references(self, segments: list[Segment]) -> None:
        try:
            for s in segments:
                pts = [s.coords[round(i * (len(s.coords) - 1) / 4)] for i in range(5)]
                pts = [p for k, p in enumerate(pts) if k == 0 or p != pts[k - 1]]
                entry: dict = {"geom": _geom_key(s), "kmh": None, "at": time.time()}
                try:
                    r = self.router.route([(lat, lon) for lon, lat in pts], "drive", [])
                    km, minutes = r["distance_km"], r["duration_min"]
                    if minutes > 0 and km * 1000 <= 1.6 * s.length_m + 150:
                        entry["kmh"] = round(s.length_m / 1000 / (minutes / 60), 1)
                    elif minutes > 0 and km > 0:
                        # A route much longer than the road went round a block (a dual
                        # carriageway, one-way streets): its own speed still says how fast
                        # the network runs there.
                        entry["kmh"] = round(km / (minutes / 60), 1)
                except Exception as e:  # noqa: BLE001 - leave this one unestimated
                    log.info("no reference speed for %s: %s", s.id, e)
                    entry["error"] = str(e)[:120]
                self._reference[s.nap_id] = entry
                time.sleep(1.1)
            path = self._reference_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._reference, indent=0), encoding="utf-8")
        finally:
            self._reference_running = False

    def free_flow(self, seg: Segment) -> tuple[float | None, str]:
        """(km/h, how it is known: measured | estimated | assumed)."""
        m = self.measured_free_flow().get(seg.nap_id)
        if m and m[1] >= MEASURED_MIN:
            return round(m[0], 1), "measured"
        ref = (self.references() or {}).get(seg.nap_id) or {}
        if ref.get("kmh"):
            return float(ref["kmh"]), "estimated"
        # Until the estimate is in (a minute or two after the first start): the road's posted
        # limit where the street rules know it (chatbot/streets), else the urban limit.
        posted = self.posted_limit(seg) if self.posted_limit is not None else None
        if posted:
            return float(posted), "posted"
        return ASSUMED_KMH, "assumed"

    # The street rules set this (chatbot/streets): Segment -> its posted limit in km/h, or None.
    posted_limit = None

    def usual(self, nap_ids: list[str], at: datetime) -> dict[str, tuple[float, int]]:
        """Path id -> (median speed, days) at this hour of this weekday over the stored weeks
        before today, from the memory (empty when it has none yet)."""
        if self.memory is None or not nap_ids:
            return {}
        local = at.astimezone(TZ)
        key = (local.strftime("%Y-%m-%d %H"), tuple(nap_ids))
        hit = self._usual.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        out = self._usual_from_memory(nap_ids, local)
        if len(self._usual) > 64:
            self._usual.clear()
        self._usual[key] = (time.time(), out)
        return out

    def _usual_from_memory(self, nap_ids: list[str], local: datetime) -> dict[str, tuple[float, int]]:
        start = local - timedelta(weeks=USUAL_WEEKS)
        today = local.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            with self.memory.reading() as con:
                rows = con.execute(
                    """SELECT segment_id, median(speed_kmh), count(DISTINCT CAST(measured_at AS DATE))
                       FROM travel_times
                       WHERE list_contains(?, segment_id) AND speed_kmh > 0
                         AND measured_at >= ? AND measured_at < ?
                         AND date_part('hour', measured_at) = ? AND date_part('isodow', measured_at) = ?
                       GROUP BY 1""",
                    [list(nap_ids), start, today, local.hour, local.isoweekday()]).fetchall()
        except Exception as e:  # noqa: BLE001
            log.info("usual road speeds unavailable: %s", e)
            return {}
        return {sid: (round(float(v), 1), int(n)) for sid, v, n in rows if v}

    def recorded(self, at: datetime) -> dict[str, tuple[float | None, float | None, datetime]]:
        """Path id -> (speed, travel time, measured at): the last reading at or before `at`
        within 20 minutes, from the memory."""
        if self.memory is None:
            raise LookupError("the twin's memory is off, so past road speeds aren't kept")
        if self.memory.recorder is not None:
            self.memory.recorder.flush(2.0)
        with self.memory.reading() as con:
            rows = con.execute(
                """SELECT segment_id, arg_max(speed_kmh, measured_at), arg_max(travel_time_s, measured_at),
                          max(measured_at)
                   FROM travel_times WHERE measured_at > ? AND measured_at <= ? GROUP BY 1""",
                [at - timedelta(minutes=20), at]).fetchall()
        return {sid: (v, t, m) for sid, v, t, m in rows}

    def series(self, nap_id: str, hours: int = 24, end: datetime | None = None) -> list[tuple[datetime, float]]:
        """15-minute mean speeds over the `hours` up to `end` (default now), from the memory."""
        if self.memory is None:
            return []
        end = end or self.now()
        since = end - timedelta(hours=hours)
        try:
            with self.memory.reading() as con:
                rows = con.execute(
                    """SELECT time_bucket(INTERVAL 15 MINUTE, measured_at) AS t, avg(speed_kmh)
                       FROM travel_times WHERE segment_id = ? AND measured_at >= ? AND measured_at <= ?
                         AND speed_kmh > 0
                       GROUP BY 1 ORDER BY 1""", [nap_id, since, end]).fetchall()
        except Exception as e:  # noqa: BLE001
            log.info("road series unavailable: %s", e)
            return []
        return [(t, round(float(v), 1)) for t, v in rows if v is not None]

    # -- the whole picture -----------------------------------------------------------------

    def speeds(self, at: datetime | None = None) -> dict:
        """Every segment with its speed, free flow and band, now or at a past moment."""
        segs = self.segments()
        now = self.now()
        error = None
        newest = None
        if at is None:
            fetched, live, error = self.live()
            readings = {sid: (r.speed_kmh, r.travel_time_s, r.measured_at) for sid, r in live.items()}
            newest = max((r.measured_at for r in live.values()), default=None)
            moment = now
        else:
            try:
                readings = self.recorded(at)
            except LookupError as e:
                return {"status": "unavailable", "error": str(e), "segments": [], "at": at}
            except Exception as e:  # noqa: BLE001 - Unavailable: missing, busy or broken
                return {"status": "unavailable", "error": str(e), "segments": [], "at": at}
            moment = at
        # The feed answered, but its newest measurement is old: the detectors have stalled.
        stalled = at is None and error is None and is_stalled(newest, now)
        usual = self.usual([s.nap_id for s in segs], moment)
        out = []
        for s in segs:
            speed, tt, measured = readings.get(s.nap_id, (None, None, None))
            stale = measured is None or (moment - measured).total_seconds() > STALE_S
            # A crawl this slow is a phone that stopped between the detectors (a driver parked,
            # a walker), not the road.
            doubtful = speed is not None and speed < MIN_PLAUSIBLE_KMH
            if stale or doubtful:
                speed = None
            free, how = self.free_flow(s)
            state = band(speed, free)
            u = usual.get(s.nap_id)
            # While stalled, the usual for this hour stands in on the map, as the usual.
            usual_state = band(u[0], free) if stalled and u and u[1] >= USUAL_MIN_DAYS else None
            out.append({
                "id": s.id, "name": s.name, "road": s.road, "short": s.short,
                "speed_kmh": round(speed, 1) if speed is not None else None,
                "travel_time_s": round(tt) if tt is not None and speed is not None else None,
                "free_flow_kmh": free, "free_flow": how,
                "share_of_free_flow": round(speed / free, 2) if speed is not None and free else None,
                "state": state, "state_label": BAND_LABELS[state],
                "usual_kmh": u[0] if u else None, "usual_days": u[1] if u else 0,
                "usual_state": usual_state,
                "usual_label": f"Usually {BAND_LABELS[usual_state].lower()}" if usual_state else None,
                "measured_at": measured.astimezone(TZ).isoformat(timespec="minutes") if measured else None,
                "length_m": round(s.length_m),
            })
        with_speed = [r for r in out if r["speed_kmh"] is not None]
        status = "live" if at is None else "recorded"
        if stalled:
            status = "stalled"
        elif not with_speed:
            status = "unavailable" if (error or at is not None) else "no_readings"
        res = {"status": status, "error": error, "segments": out, "at": moment,
               "measured": len(with_speed), "total": len(out)}
        if stalled:
            res |= {"last_measured_at": newest.astimezone(TZ).isoformat(timespec="minutes"),
                    "note": stalled_note(newest, now),
                    "usual": sum(1 for r in out if r["usual_state"])}
        return res


def _geom_key(s: Segment) -> str:
    return hashlib.sha1(json.dumps(s.coords).encode()).hexdigest()[:12]


def slowest(rows: list[dict], n: int = 5) -> list[dict]:
    """The roads furthest below their free flow, slowest first."""
    have = [r for r in rows if r["share_of_free_flow"] is not None]
    return sorted(have, key=lambda r: (r["share_of_free_flow"], r["speed_kmh"]))[:n]


def segment_feature(s: Segment, row: dict | None) -> dict:
    row = row or {}
    state = row.get("state", "no_reading")
    speed = row.get("speed_kmh")
    usual_state = row.get("usual_state")  # set only while the feed is stalled
    props = {"id": s.id, "label": f"{round(speed)} km/h" if speed is not None else "",
             "state": state, "color": SPEED_COLORS[state], "entity_kind": "road_segment",
             "speed_kmh": speed, "free_flow_kmh": row.get("free_flow_kmh"),
             "share": row.get("share_of_free_flow"), "name": s.name,
             "popup": f"{s.name}\n" + (f"{round(speed)} km/h, {BAND_LABELS[state].lower()}"
                                       if speed is not None else "No reading")}
    if usual_state:
        props |= {"usual_state": usual_state, "usual_kmh": row.get("usual_kmh"), "color": USUAL_COLOR,
                  "popup": f"{s.name}\nNo reading. {row.get('usual_label')} at this hour "
                           f"({round(row['usual_kmh'])} km/h)"}
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in s.coords]},
            "properties": props}


def usually_slowest(rows: list[dict], n: int = 5) -> list[dict]:
    """While the feed is stalled: the roads usually furthest below their free flow at this hour."""
    have = [r for r in rows if r.get("usual_state") and r["free_flow_kmh"]]
    return sorted(have, key=lambda r: r["usual_kmh"] / r["free_flow_kmh"])[:n]


__all__ = ["Roads", "Segment", "band", "slowest", "usually_slowest", "segment_feature", "parse_moment",
           "LEGEND", "STALLED_LEGEND", "SPEED_COLORS", "BAND_LABELS", "since_words", "stalled_note",
           "is_stalled"]

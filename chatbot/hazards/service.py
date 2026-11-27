"""The hazard feeds for the twin: read on their own cadence, kept, and recorded into the memory.

`Hazards` (one per Agent, `agent.hazards`) polls in a background thread:

- EMSC every 5 minutes while a page listens (every 15 otherwise): the quakes of the last two days
  (on the first read, the last 31), then the felt-report counts of the quakes near enough to
  matter, and the felt reports' positions for a quake that raises an incident (a zip of about
  1 MB, read at most four times while the reports come in, never for a small distant quake).
- Meteoalarm every 15 minutes: the JSON API, or the Atom feed when it fails. A warning that
  leaves the feed before its expiry was withdrawn, and ends then.
- EFFIS every 3 hours: today's fire danger and the next two days' forecast, three images of about
  1 KB. A past day the city clock asks for is read once and kept on disk.

Everything read goes into the memory's own `hazards` table (written here, by id, so a revised
magnitude or an updated warning replaces the row), so past hazards replay with the city clock and
the watch's detectors read them (chatbot/watch/hazards.py). After a change, `on_change` tells the
watch to look soon. Without a memory, or with recording off, they live in this process only.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from chatbot.hazards import feeds as F
from chatbot.hazards.feeds import TZ, Hazard
from chatbot.memory import Memory, Unavailable
from chatbot.settings import CACHE_DIR, Settings
from chatbot.web import WebError, get_bytes, request

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hazards (
    id VARCHAR PRIMARY KEY,          -- eq-<EMSC unid> | wx-<CAP identifier> | fire-<day>
    kind VARCHAR,                    -- earthquake | warning | fire
    source VARCHAR,                  -- emsc | meteoalarm | effis
    start_ts TIMESTAMPTZ,            -- origin time | onset | the day's start
    end_ts TIMESTAMPTZ,              -- null | expiry (or withdrawn) | the day's end
    issued_ts TIMESTAMPTZ,
    level VARCHAR,
    value DOUBLE,
    lat DOUBLE,
    lon DOUBLE,
    title VARCHAR,
    details VARCHAR,                 -- JSON: everything else the feed said
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ
);
"""
COLUMNS = ("id, kind, source, start_ts, end_ts, issued_ts, level, value, lat, lon, title, details, "
           "first_seen, last_seen")

# How often each feed is read: (while a page listens, otherwise), seconds.
EVERY = {"earthquakes": (300.0, 900.0), "warnings": (900.0, 900.0), "fire": (3 * 3600.0, 3 * 3600.0)}
RETRY_S = 300.0  # a feed that failed is tried again after this, whatever its cadence
KEEP = timedelta(days=63)  # what this process keeps in hand (the clock reaches 62 days back)
QUAKES_FIRST = timedelta(days=31)
QUAKES_AGAIN = timedelta(days=2)  # later reads: the last two days (revisions, late solutions)
FELT_WITHIN = timedelta(days=3)  # felt reports are counted for quakes this recent...
FELT_CANDIDATE = (2.0, 150.0)  # ...this strong and this close
FELT_POSITIONS_MIN = 3  # positions are read once a quake has this many reports
FELT_POSITIONS_KM = 40.0  # ...and raises an incident, or is this close
FELT_READS = 4
FELT_GROWTH = 1.25
FELT_GAP = timedelta(minutes=30)
FELT_FOR = timedelta(hours=24)
FIRE_FROM = date(2018, 1, 1)  # EFFIS's fire danger archive starts here
FIRE_AHEAD_DAYS = 2
PAST_FIRE_GAP_S = 2.0  # past days the clock asks for: one image every 2 s at most
START_DELAY_S = 8.0
FEED_NAMES = {"earthquakes": "EMSC", "warnings": "Meteoalarm", "fire": "EFFIS"}


def to_row(h: Hazard) -> list:
    return [h.id, h.kind, h.source, h.start, h.end, h.issued, h.level, h.value, h.lat, h.lon, h.title,
            json.dumps(h.details, ensure_ascii=False, separators=(",", ":")), h.first_seen, h.last_seen]


def from_row(r: tuple) -> Hazard:
    (hid, kind, source, start, end, issued, level, value, lat, lon, title, details, first, last) = r
    try:
        d = json.loads(details or "{}")
    except ValueError:
        d = {}
    return Hazard(hid, kind, source, start, end, title, lat, lon, level, value, issued, d, first, last)


def from_memory(memory: Memory, since: datetime, until: datetime | None = None,
                ids: list[str] | None = None) -> list[Hazard]:
    """Hazards the memory holds that were (or will be) true after `since` and began before
    `until`. Raises Unavailable when the memory can't be read; a memory from before hazards
    holds none."""
    sql = f"SELECT {COLUMNS} FROM hazards WHERE "
    args: list = []
    if ids is not None:
        sql += "list_contains(?, id)"
        args.append(ids)
    else:
        sql += "coalesce(end_ts, start_ts + INTERVAL 3 HOUR) >= ? AND start_ts <= ?"
        args += [since, until or datetime(2100, 1, 1, tzinfo=timezone.utc)]
    with memory.reading(timeout=5.0) as con:
        try:
            rows = con.execute(sql + " ORDER BY start_ts", args).fetchall()
        except duckdb.CatalogException:
            return []
    return [from_row(r) for r in rows]


def _same(a: Hazard, b: Hazard) -> bool:
    keep = ("read_at", "read_on")

    def d(h: Hazard) -> dict:
        out = {k: v for k, v in h.details.items() if k not in keep}
        if isinstance(out.get("felt"), dict):
            out["felt"] = {k: v for k, v in out["felt"].items() if k not in keep}
        return out
    return ((a.start, a.end, a.title, a.level, a.value, a.lat, a.lon) ==
            (b.start, b.end, b.title, b.level, b.value, b.lat, b.lon) and d(a) == d(b))


class Hazards:
    def __init__(self, settings: Settings, memory: Memory | None = None, *,
                 now: Callable[[], datetime] | None = None, fetch_text=request,
                 fetch_bytes=get_bytes, cache_dir: Path | None = None,
                 listening: Callable[[], int] | None = None):
        self.s = settings
        self.memory = memory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._text = fetch_text
        self._bytes = fetch_bytes
        self.cache_dir = cache_dir or CACHE_DIR / "hazards"
        self.listening = listening or (lambda: 0)
        self.known: dict[str, Hazard] = {}
        self.on_change: list[Callable[[], None]] = []
        self.health: dict[str, dict] = {f: {"read_at": None, "ok_at": None, "error": None, "note": None}
                                        for f in EVERY}
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()  # one request at a time, from any thread
        self._pending: set[str] = set()
        self._loaded = False
        self._next: dict[str, float] = {}
        self._felt: dict[str, list[tuple[datetime, int]]] = {}  # quake -> (read at, located) per read
        self._past_fire_at = 0.0
        self._past_fire_failed: dict[date, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the loop ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="twin-hazards", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(START_DELAY_S):
            return
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the feeds must outlive any one read
                log.exception("hazards: refresh failed")
            self._stop.wait(60.0)

    def refresh(self, force: bool = False, only: tuple[str, ...] | None = None) -> list[str]:
        """Read every feed that is due (all of them with force). Returns the ids that changed."""
        self.load()
        changed: list[str] = []
        for feed in (only or tuple(EVERY)):
            if not force and time.monotonic() < self._next.get(feed, 0.0):
                continue
            listening, idle = EVERY[feed]
            self._next[feed] = time.monotonic() + (listening if self.listening() else idle)
            h = self.health[feed]
            h["read_at"] = time.time()
            try:
                changed += getattr(self, f"_read_{feed}")()
                h["ok_at"], h["error"] = time.time(), None
            except (WebError, ValueError, ET.ParseError, zipfile.BadZipFile, OSError) as e:
                h["error"] = str(e)[:300]
                self._next[feed] = time.monotonic() + RETRY_S
                log.info("hazards: %s unavailable: %s", feed, e)
        self.save()
        if changed:
            for cb in list(self.on_change):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    log.exception("hazards: on_change failed")
        return changed

    # -- requests ----------------------------------------------------------------------------------

    def _get_text(self, url: str, params: dict | None = None, accept: str = "application/json") -> str:
        with self._fetch_lock:
            return self._text(url, user_agent=self.s.user_agent, params=params, timeout=30.0,
                              accept=accept)

    def _get_bytes(self, url: str) -> bytes:
        with self._fetch_lock:
            return self._bytes(url, user_agent=self.s.user_agent, timeout=60.0)

    # -- keeping ---------------------------------------------------------------------------------

    def _put(self, h: Hazard) -> list[str]:
        now = self.now()
        with self._lock:
            old = self.known.get(h.id)
            if old is not None:
                h.first_seen = old.first_seen or now
                if "felt" in old.details and "felt" not in h.details:
                    h.details["felt"] = old.details["felt"]
                same = _same(old, h)
            else:
                h.first_seen = h.first_seen or now
                same = False
            h.last_seen = now
            self.known[h.id] = h
            self._pending.add(h.id)
        return [] if same else [h.id]

    def load(self) -> None:
        """What the memory holds of the last 63 days, once (until it can be read)."""
        if self._loaded:
            return
        if self.memory is None or not self.memory.exists():
            self._loaded = True
            return
        try:
            rows = from_memory(self.memory, self.now() - KEEP)
        except Unavailable as e:
            log.info("hazards: memory unavailable, reading it later: %s", e)
            return
        with self._lock:
            for h in rows:
                self.known.setdefault(h.id, h)
            self._loaded = True

    def save(self) -> bool:
        """Write what changed into the memory. False if it was busy: it goes with the next."""
        if self.memory is None or self.memory.recorder is None:
            with self._lock:
                self._pending.clear()
            return True
        with self._lock:
            ids = sorted(self._pending)
            rows = [to_row(self.known[i]) for i in ids if i in self.known]
        if not rows:
            return True
        try:
            with self.memory.writing(timeout=5.0) as con:
                con.execute(SCHEMA)
                con.executemany(f"INSERT OR REPLACE INTO hazards ({COLUMNS}) VALUES "
                                f"({', '.join('?' * 14)})", rows)
        except Exception as e:  # noqa: BLE001 - busy or broken: keep them for the next save
            log.warning("hazards: could not record: %s", e)
            return False
        with self._lock:
            self._pending.difference_update(ids)
        return True

    # -- earthquakes -------------------------------------------------------------------------------

    def _read_earthquakes(self) -> list[str]:
        now = self.now()
        first = self.health["earthquakes"]["ok_at"] is None
        since = now - (QUAKES_FIRST if first else QUAKES_AGAIN)
        body = self._get_text(F.EMSC_URL, {"format": "json", **F.EMSC_BOX, "limit": 500,
                                           "orderby": "time",
                                           "starttime": since.strftime("%Y-%m-%dT%H:%M:%S")})
        payload = json.loads(body) if body.strip() else {"features": []}  # 204: no quakes
        changed: list[str] = []
        for q in F.parse_emsc(payload):
            changed += self._put(q)
        changed += self._read_felt(now)
        n = len(payload.get("features") or [])
        self.health["earthquakes"]["note"] = f"{n} quake{'s' if n != 1 else ''} around Cyprus since {F.wall(since)[:10]}"
        return changed

    def _felt_candidates(self, now: datetime) -> list[Hazard]:
        mag, km = FELT_CANDIDATE
        with self._lock:
            return [h for h in self.known.values() if h.kind == "earthquake"
                    and timedelta(0) <= now - h.start <= FELT_WITHIN and (h.value or 0) >= mag
                    and (h.details.get("distance_km") or 9999) <= km]

    def _read_felt(self, now: datetime) -> list[str]:
        """Felt-report counts for the quakes that matter, and positions for those that raise an
        incident or are close."""
        # The ones that matter most first: an aftershock sequence can hold a hundred small ones.
        quakes = sorted(self._felt_candidates(now), key=lambda h: (F.quake_rules(h)[0], h.value or 0),
                        reverse=True)[:40]
        if not quakes:
            return []
        unids = ",".join(q.details["unid"] for q in quakes)
        url = f"{F.FELT_URL}?unids=[{urllib.parse.quote(unids, safe=',_')}]&includeTestimonies=false&format=json"
        body = self._get_text(url)
        counts = F.parse_felt_counts(json.loads(body) if body.strip() else [])
        changed: list[str] = []
        for q in quakes:
            n = counts.get(q.details["unid"], 0)
            felt = dict(q.details.get("felt") or {})
            if felt.get("count") == n and not self._want_positions(q, n, now):
                continue
            felt["count"] = max(n, felt.get("located") or 0)
            felt["read_at"] = F.wall(now)
            if self._want_positions(q, n, now):
                try:
                    reports = F.parse_felt_zip(self._get_bytes(
                        f"{F.FELT_URL}?unids=[{q.details['unid']}]&includeTestimonies=true&format=json"))
                    felt = F.felt_summary(reports, n, now)
                    self._felt.setdefault(q.id, []).append((now, len(reports)))
                except (WebError, zipfile.BadZipFile, ValueError) as e:
                    log.info("hazards: felt reports of %s unavailable: %s", q.id, e)
                    self._felt.setdefault(q.id, []).append((now, felt.get("located") or 0))
            changed += self._put(F.with_felt(q, felt))
        return changed

    def _want_positions(self, q: Hazard, n: int, now: datetime) -> bool:
        if n < FELT_POSITIONS_MIN or now - q.start > FELT_FOR:
            return False
        raises = F.quake_rules(q)[0]
        if not raises and (q.details.get("distance_km") or 9999) > FELT_POSITIONS_KM:
            return False
        reads = self._felt.get(q.id, [])
        located = (q.details.get("felt") or {}).get("located")
        if not reads and located is None:
            return True
        if len(reads) >= FELT_READS or (reads and now - reads[-1][0] < FELT_GAP):
            return False
        return n >= (located or 0) * FELT_GROWTH

    # -- warnings ----------------------------------------------------------------------------------

    def _read_warnings(self) -> list[str]:
        now = self.now()
        try:
            warnings = F.parse_meteoalarm_api(json.loads(self._get_text(F.METEOALARM_API)))
            via = "the JSON API"
        except (WebError, ValueError) as e:
            log.info("hazards: Meteoalarm's API failed (%s), reading the Atom feed", e)
            warnings = F.parse_meteoalarm_atom(self._get_text(F.METEOALARM_ATOM,
                                                              accept="application/atom+xml"))
            via = "the Atom feed"
        warnings = [w for w in warnings if w.details.get("msg_type") != "Cancel"]
        changed: list[str] = []
        for w in warnings:
            changed += self._put(w)
        seen = {w.id for w in warnings}
        with self._lock:
            gone = [h for h in self.known.values() if h.kind == "warning" and h.id not in seen
                    and (h.end is None or h.end > now) and not h.details.get("withdrawn_at")]
        for h in gone:  # left the feed before its expiry: withdrawn (or replaced by an update)
            changed += self._put(replace(h, start=min(h.start, now), end=now,
                                         details={**h.details, "withdrawn_at": F.wall(now)}))
        n = sum(1 for w in warnings if F.warning_in_force(w, now))
        self.health["warnings"]["note"] = (f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''} "
                                           f"for Cyprus ({n} in force), from {via}" if warnings
                                           else f"no warning for Cyprus, from {via}")
        return changed

    # -- fire danger -------------------------------------------------------------------------------

    def _fire_url(self, day: date) -> str:
        params = {"service": "WMS", "version": "1.1.1", "request": "GetMap", "LAYERS": "mf010.fwi",
                  "STYLES": "", "SRS": "EPSG:4326", "BBOX": ",".join(str(x) for x in F.FIRE_BBOX),
                  "WIDTH": F.FIRE_SIZE[0], "HEIGHT": F.FIRE_SIZE[1], "FORMAT": "image/png",
                  "TRANSPARENT": "true", "TIME": day.isoformat()}
        return f"{F.EFFIS_WMS}?{urllib.parse.urlencode(params)}"

    def _read_fire(self) -> list[str]:
        now = self.now()
        today = now.astimezone(TZ).date()
        changed: list[str] = []
        levels = []
        for k in range(FIRE_AHEAD_DAYS + 1):
            day = today + timedelta(days=k)
            h = F.parse_fire_png(self._get_bytes(self._fire_url(day)), day, now)
            if h is not None:
                changed += self._put(h)
                levels.append(F.FIRE_WORDS[h.level].lower())
        self.health["fire"]["note"] = (f"the Nicosia district today: {levels[0]}" if levels
                                       else "no fire danger data over the Nicosia district")
        return changed

    def fire_day(self, day: date) -> Hazard | None:
        """A day's fire danger in the Nicosia district: kept, or read from EFFIS once (a past day
        is kept on disk too). None when EFFIS has nothing for it."""
        self.load()
        with self._lock:
            hit = self.known.get(f"fire-{day.isoformat()}")
        if hit is not None:
            return hit
        now = self.now()
        today = now.astimezone(TZ).date()
        if day < FIRE_FROM or day > today + timedelta(days=FIRE_AHEAD_DAYS):
            return None
        if day >= today:
            return None  # today and ahead come with the regular read
        failed = self._past_fire_failed.get(day)
        if failed and time.monotonic() - failed < 600:
            return None
        path = self.cache_dir / f"effis_fwi_{day.isoformat()}.png"
        try:
            if path.exists():
                blob = path.read_bytes()
            else:
                wait = self._past_fire_at + PAST_FIRE_GAP_S - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._past_fire_at = time.monotonic()
                blob = self._get_bytes(self._fire_url(day))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob)
            h = F.parse_fire_png(blob, day, now)
        except (WebError, OSError, ValueError) as e:
            log.info("hazards: fire danger of %s unavailable: %s", day, e)
            self._past_fire_failed[day] = time.monotonic()
            return None
        if h is None:
            return None
        self._put(h)
        self.save()
        return h

    # -- reading -----------------------------------------------------------------------------------

    def get(self, hid: str) -> Hazard | None:
        self.load()
        with self._lock:
            hit = self.known.get(hid)
        if hit is not None or self.memory is None or not self.memory.exists():
            return hit
        try:
            rows = from_memory(self.memory, self.now(), ids=[hid])
        except Unavailable:
            return None
        return rows[0] if rows else None

    def all(self, kind: str | None = None) -> list[Hazard]:
        self.load()
        with self._lock:
            return [h for h in self.known.values() if kind is None or h.kind == kind]

    def earthquakes(self, until: datetime, days: float = 30, min_magnitude: float = 0.0) -> list[Hazard]:
        since = until - timedelta(days=days)
        return sorted((h for h in self.all("earthquake")
                       if since <= h.start <= until and (h.value or 0) >= min_magnitude),
                      key=lambda h: h.start, reverse=True)

    def warnings(self, at: datetime) -> list[Hazard]:
        """The warnings issued by `at` and not yet over then (in force, or for later)."""
        return sorted((h for h in self.all("warning")
                       if (h.issued or h.start) <= at and (h.end is None or at < h.end)),
                      key=lambda h: h.start)

    def moment(self, at: datetime | None = None, days: float = 30, read_fire: bool = True) -> dict:
        """The hazards at a moment (None: now): the quakes of the `days` before it, the warnings
        issued and not over, that day's fire danger in the Nicosia district (and, live, the next
        two days' forecast)."""
        now = self.now()
        t = at or now
        day = t.astimezone(TZ).date()
        fire = self.fire_day(day) if read_fire else self.known.get(f"fire-{day.isoformat()}")
        ahead = []
        if at is None:
            ahead = [h for k in range(1, FIRE_AHEAD_DAYS + 1)
                     if (h := self.known.get(f"fire-{(day + timedelta(days=k)).isoformat()}"))]
        return {"at": F.wall(t), "live": at is None,
                "earthquakes": [h.public() for h in self.earthquakes(t, days)],
                "warnings": [h.public(t) for h in self.warnings(t)],
                "fire": fire.public() if fire else None,
                "fire_ahead": [h.public() for h in ahead],
                "status": self.status()}

    def status(self) -> dict:
        out = {}
        for feed, h in self.health.items():
            out[feed] = {"source": FEED_NAMES[feed],
                         "read_at": F.wall(datetime.fromtimestamp(h["ok_at"], timezone.utc)) if h["ok_at"] else None,
                         "error": h["error"], "note": h["note"]}
        return out

    def feed_state(self, feed: str) -> tuple[str, float | None, str | None]:
        """For the data sources panel: (state, when last read well, a note)."""
        h = self.health[feed]
        if h["ok_at"] is None:
            return ("unavailable", h["read_at"], h["error"]) if h["error"] else \
                ("waiting", None, "read in the background a few seconds after the server starts")
        if h["error"]:
            return "stale", h["ok_at"], f"the last read failed ({h['error'][:120]}); the last good one stands"
        return "live", h["ok_at"], h["note"]

"""The island's electricity system, live: how much power Cyprus uses, how much comes from wind
and (estimated) solar, and the grid's carbon, every 15 minutes.

The source is the Transmission System Operator's generation page (`tso.py`): an HTML table, not
an API, so `Grid` is gentle with it.

- It reads the page at most once every 15 minutes, and only when the next row can be there
  (the operator fills a quarter hour about 12 minutes after it ends). Failures back off to an
  hour. A copy of the last good read is kept on disk (`data/chatbot_cache/grid/`), so a restart
  doesn't read it again, and when the page fails or changes its layout that copy stands in and
  the source panel says so.
- Every row it reads goes into the twin's memory, table `grid_power` (created on first write;
  the collector's schema is untouched), so the city clock replays the grid and a usual day
  builds up. Once a day at most it fills the last 15 days from the operator's archive page.
- A small keeper thread reads it while the server runs (one request per publication), so the
  record has no holes when no page is open.

Readings: `snapshot(at)` for the page, the inspector and the tool; `series(start, end)`; `at(t)`;
`usual(end)`; `carbon_at(times)` for the lamps' CO2 (chatbot/history.py); `quarter_shares`
for the lighting scenario. The carbon method is in `carbon.py`. The figures are the whole
island's (the Republic's side), never Nicosia's alone.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from collector import db

from chatbot import web
from chatbot.energy import carbon
from chatbot.energy.tso import PAGE_URL, TZ, LayoutChanged, Page, Reading, archive_url, parse_page
from chatbot.settings import CACHE_DIR

log = logging.getLogger(__name__)

STEP = timedelta(minutes=15)
MIN_INTERVAL = timedelta(minutes=15)  # never read the page more often than it is published
PUBLISH_LAG = timedelta(minutes=12)  # a quarter hour is filled about this long after it
MAX_BACKOFF = timedelta(hours=1)
LIVE_AGE = timedelta(minutes=45)  # an older newest row makes "now" stale
MOMENT_WINDOW = timedelta(minutes=30)  # a past moment takes a row at most this much older
KEEP_RECENT = timedelta(hours=50)  # rows kept in memory beside the twin's memory
BACKFILL_DAYS = 15
USUAL_WEEKS = 4
USUAL_TTL_S = 600
KEEPER_START_S = 20
KEEPER_TICK_S = 60
WHERE = "The whole island's grid (the Republic's side), not Nicosia alone"
LABELS = {"demand_mw": "Demand", "conventional_mw": "Oil-fired stations", "wind_mw": "Wind",
          "solar_mw": "Solar and small plants, estimated"}
SOLAR_NOTE = ("The operator's estimate of distributed generation: rooftop solar, solar parks on "
              "the distribution network and a few small biomass plants (about 14 MW, day and night)")

TABLE = """CREATE TABLE IF NOT EXISTS grid_power (
    ts TIMESTAMPTZ PRIMARY KEY,  -- the quarter hour, as the operator stamps it
    demand_mw DOUBLE,            -- total demand
    wind_mw DOUBLE,              -- wind on the transmission system
    distributed_mw DOUBLE,       -- the operator's estimate: rooftop solar, small plants
    conventional_mw DOUBLE,      -- the oil-fired stations
    capacity_mw DOUBLE,          -- the oil-fired stations' available capacity
    source VARCHAR,              -- 'live' (today's page) or 'archive'
    fetched TIMESTAMPTZ
)"""
COLUMNS = {"us": "BIGINT", "demand_mw": "DOUBLE", "wind_mw": "DOUBLE", "distributed_mw": "DOUBLE",
           "conventional_mw": "DOUBLE", "capacity_mw": "DOUBLE", "source": "VARCHAR", "fetched": "BIGINT"}
SELECT = ("SELECT ts, demand_mw, wind_mw, distributed_mw, conventional_mw, capacity_mw "
          "FROM grid_power WHERE ts >= ? AND ts <= ? ORDER BY ts")


def floor_quarter(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def local_iso(t: datetime) -> str:
    return t.astimezone(TZ).isoformat(timespec="minutes")


def local_midnight(t: datetime) -> datetime:
    return t.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def _r(v: float | None, digits: int = 0):
    if v is None:
        return None
    return round(v) if digits == 0 else round(v, digits)


def point(r: Reading, now: datetime | None = None) -> dict:
    """A reading for the page and the model: MW by source, the carbon and the shares."""
    c = carbon.intensity(r.conventional_mw, r.demand_mw)
    ren = carbon.renewable_share(r.wind_mw, r.distributed_mw, r.demand_mw)
    d = r.demand_mw or 0
    out = {"t": local_iso(r.t), "demand_mw": _r(r.demand_mw), "conventional_mw": _r(r.conventional_mw),
           "wind_mw": _r(r.wind_mw), "solar_mw": _r(r.distributed_mw), "capacity_mw": _r(r.capacity_mw),
           "carbon_kg_kwh": _r(c, 3), "renewable_pct": _r(None if ren is None else 100 * ren, 1),
           "wind_pct": _r(100 * r.wind_mw / d, 1) if r.wind_mw is not None and d else None,
           "solar_pct": _r(100 * r.distributed_mw / d, 1) if r.distributed_mw is not None and d else None}
    if now is not None:
        out["age_min"] = max(0, round((now - r.t).total_seconds() / 60))
    return out


def _rows_to_readings(rows: list[tuple]) -> list[Reading]:
    out = []
    for ts, demand, wind, dist, conv, cap in rows:
        t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        out.append(Reading(t=t.astimezone(timezone.utc), demand_mw=demand, wind_mw=wind,
                           distributed_mw=dist, conventional_mw=conv, capacity_mw=cap))
    return out


def stored(memory, start: datetime, end: datetime) -> list[Reading]:
    """The grid as the twin's memory holds it between two moments (none if it can't be read)."""
    if memory is None:
        return []
    from chatbot.memory import Unavailable

    try:
        with memory.reading() as con:
            try:
                rows = con.execute(SELECT, [start, end]).fetchall()
            except duckdb.CatalogException:  # nothing recorded yet: no table
                return []
    except Unavailable:
        return []
    return _rows_to_readings(rows)


def quarter_shares(memory, start: datetime, end: datetime,
                   extra: list[Reading] | None = None) -> tuple[dict[int, float], int]:
    """The oil-fired share of demand by minute of the local day (0, 15, ... 1425), the median
    over the days recorded between two moments, and how many days that is. The lighting
    scenario weights each quarter hour of the lamps' night with it."""
    rows = {r.t: r for r in stored(memory, start, end)}
    for r in extra or ():
        if start <= r.t <= end:
            rows[r.t] = r
    by: dict[int, list[float]] = {}
    days: set[date] = set()
    for r in rows.values():
        s = carbon.share(r.conventional_mw, r.demand_mw)
        if s is None:
            continue
        lt = r.t.astimezone(TZ)
        by.setdefault(lt.hour * 60 + lt.minute, []).append(s)
        days.add(lt.date())
    return {m: statistics.median(v) for m, v in by.items()}, len(days)


class Grid:
    """The island's grid now and as the twin recorded it. One lives on the Agent."""

    def __init__(self, settings, memory=None, *, cache_dir: Path | None = CACHE_DIR / "grid",
                 fetch_text=None, clock=None):
        self.settings = settings
        self.memory = memory
        self.recording = memory is not None and bool(getattr(settings, "record", True))
        self.fetch_text = fetch_text or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cache_dir = cache_dir
        self.installed: dict[str, float] = {}
        self.fetched_at: datetime | None = None  # the last good read
        self.tried_at: datetime | None = None
        self.error: str | None = None
        self.layout_changed = False
        self.failures = 0
        self._recent: dict[datetime, Reading] = {}
        self._pending: dict[datetime, tuple[Reading, str]] = {}
        self._lock = threading.Lock()
        self._fetching = threading.Lock()
        self._flushing = threading.Lock()
        self._usual: dict[tuple, tuple[float, tuple[list[dict], int]]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load_copy()

    # -- reading the page --------------------------------------------------------------------

    def _fetch(self, url: str) -> str:
        return web.request(url, user_agent=self.settings.user_agent, accept="text/html", timeout=20)

    def latest(self) -> Reading | None:
        with self._lock:
            return self._recent[max(self._recent)] if self._recent else None

    def due(self, now: datetime | None = None) -> bool:
        """Whether a read of the page now could bring a new row, without reading it more often
        than it is published."""
        now = now or self.clock()
        if self.tried_at is None:
            return True
        wait = min(MIN_INTERVAL * (2 ** self.failures), MAX_BACKOFF)
        if now - self.tried_at < wait:
            return False
        latest = self.latest()
        if latest is not None and not self.failures and now < latest.t + STEP + PUBLISH_LAG:
            return False
        return True

    def refresh(self, force: bool = False) -> bool:
        """Read today's page if it is due. True if a good copy was read. Never raises; a caller
        that finds another read under way doesn't wait for it."""
        if not (force or self.due()):
            return False
        if not self._fetching.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            self.tried_at = now
            try:
                page = parse_page(self.fetch_text(PAGE_URL))
            except LayoutChanged as e:
                self.failures += 1
                self.layout_changed = True
                self.error = f"the operator's page changed its layout ({e}); the last good copy stands in"
                log.warning("grid: %s", self.error)
                return False
            except web.WebError as e:
                self.failures += 1
                self.error = str(e)
                log.info("grid: page not read: %s", e)
                return False
            except Exception as e:  # noqa: BLE001 - a feed never breaks an answer
                self.failures += 1
                self.error = f"{type(e).__name__}: {e}"
                log.warning("grid: page not read: %s", self.error)
                return False
            self.failures = 0
            self.error = None
            self.layout_changed = False
            self.fetched_at = now
            self._merge(page)
            self.record(page.readings, "live")
            self._save_copy()
            return True
        finally:
            self._fetching.release()

    def _merge(self, page: Page) -> None:
        with self._lock:
            for r in page.readings:
                self._recent[r.t] = r
            if page.installed:
                self.installed = dict(page.installed)
            newest = max(self._recent) if self._recent else None
            if newest is not None:
                for t in [t for t in self._recent if t < newest - KEEP_RECENT]:
                    del self._recent[t]

    # -- the copy on disk --------------------------------------------------------------------

    def _copy_path(self) -> Path | None:
        return self.cache_dir / "recent.json" if self.cache_dir else None

    def _save_copy(self) -> None:
        path = self._copy_path()
        if path is None:
            return
        with self._lock:
            body = {"fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
                    "installed": self.installed,
                    "readings": [r.public() for r in sorted(self._recent.values(), key=lambda r: r.t)]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.info("grid: copy not saved: %s", e)

    def _load_copy(self) -> None:
        path = self._copy_path()
        if path is None or not path.exists():
            return
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            for d in body.get("readings", []):
                t = datetime.fromisoformat(d.pop("t")).astimezone(timezone.utc)
                self._recent[t] = Reading(t=t, **{k: d.get(k) for k in (
                    "demand_mw", "wind_mw", "distributed_mw", "conventional_mw", "capacity_mw")})
            self.installed = body.get("installed") or {}
            if body.get("fetched_at"):
                self.fetched_at = datetime.fromisoformat(body["fetched_at"])
                self.tried_at = self.fetched_at  # a restart doesn't read the page again at once
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.info("grid: copy not read: %s", e)

    # -- recording ---------------------------------------------------------------------------

    def record(self, readings: list[Reading], source: str) -> None:
        """Keep rows for the twin's memory and write them from a background thread."""
        if not self.recording or not readings:
            return
        with self._lock:
            for r in readings:
                self._pending[r.t] = (r, source)
        threading.Thread(target=self.flush, name="grid-record", daemon=True).start()

    def flush(self, timeout: float = 5.0) -> bool:
        """Write the waiting rows. False if the memory was busy (they wait for the next time)."""
        if not self._flushing.acquire(timeout=timeout + 5):  # one write at a time
            return False
        try:
            with self._lock:
                batch, self._pending = self._pending, {}
            if not batch:
                return True
            fetched = int(self.clock().timestamp() * 1e6)
            rows = [(int(r.t.timestamp() * 1e6), r.demand_mw, r.wind_mw, r.distributed_mw,
                     r.conventional_mw, r.capacity_mw, src, fetched) for r, src in batch.values()]
            try:
                with self.memory.writing(timeout) as con:
                    con.execute(TABLE)
                    db.bulk(con, COLUMNS, rows,
                            """INSERT OR REPLACE INTO grid_power
                               SELECT make_timestamptz(us), demand_mw, wind_mw, distributed_mw,
                                      conventional_mw, capacity_mw, source, make_timestamptz(fetched)
                               FROM __SRC__""")
                return True
            except Exception as e:  # noqa: BLE001 - busy or broken: the rows wait
                log.info("grid: rows not recorded yet: %s", e)
                with self._lock:
                    for t, v in batch.items():
                        self._pending.setdefault(t, v)
                return False
        finally:
            self._flushing.release()

    def backfill_if_needed(self) -> int:
        """Fill the last 15 days from the operator's archive, once a day at most, when the
        memory lacks them. Returns the rows read (0 when nothing was needed or it failed)."""
        if not self.recording:
            return 0
        today = self.clock().astimezone(TZ).date()
        marker = self.cache_dir / "backfill.json" if self.cache_dir else None
        try:
            if marker and marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("day") == today.isoformat():
                return 0
        except (OSError, ValueError):
            pass
        first = today - timedelta(days=BACKFILL_DAYS)
        start = datetime.combine(first, datetime.min.time(), TZ)
        have = stored(self.memory, start, local_midnight(self.clock()))
        if len(have) < 0.9 * 96 * BACKFILL_DAYS:
            try:
                page = parse_page(self.fetch_text(archive_url(first, BACKFILL_DAYS)))
            except (LayoutChanged, web.WebError) as e:
                log.info("grid: archive not read: %s", e)
                return 0
            self.record(page.readings, "archive")
            n = len(page.readings)
        else:
            n = 0
        if marker:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({"day": today.isoformat(), "rows": n}), encoding="utf-8")
            except OSError:
                pass
        return n

    # -- the keeper --------------------------------------------------------------------------

    def start(self) -> None:
        """Read the page as it is published while the server runs, after the back-fill."""
        if self._thread is None and self.recording:
            self._thread = threading.Thread(target=self._keep, name="grid-keeper", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pending:
            self.flush()

    def _keep(self) -> None:
        if self._stop.wait(KEEPER_START_S):
            return
        try:
            self.backfill_if_needed()
        except Exception:  # noqa: BLE001 - the keeper outlives a bad back-fill
            log.exception("grid: back-fill failed")
        while not self._stop.is_set():
            try:
                self.refresh()
                if self._pending:
                    self.flush()
            except Exception:  # noqa: BLE001
                log.exception("grid: refresh failed")
            self._stop.wait(KEEPER_TICK_S)

    # -- readings ----------------------------------------------------------------------------

    def series(self, start: datetime, end: datetime) -> list[Reading]:
        """Every row between two moments: the memory's, with the newest read laid over it."""
        rows = {r.t: r for r in stored(self.memory, start, end)}
        with self._lock:
            for t, r in self._recent.items():
                if start <= t <= end:
                    rows[t] = r
            for t, (r, _) in self._pending.items():
                if start <= t <= end:
                    rows[t] = r
        return [rows[t] for t in sorted(rows)]

    def at(self, t: datetime, window: timedelta = MOMENT_WINDOW) -> Reading | None:
        """The row in force at a moment: the newest at or before it, within `window`."""
        rows = [r for r in self.series(t - window, t) if r.t <= t]
        return rows[-1] if rows else None

    def carbon_at(self, times: list[datetime]) -> list[float | None]:
        """kg CO2 per kWh at each moment, None where the twin has no record (for history.py)."""
        if not times:
            return []
        rows = {r.t: r for r in self.series(min(times) - MOMENT_WINDOW, max(times))}
        out = []
        for t in times:
            q = floor_quarter(t)
            r = rows.get(q) or rows.get(q - STEP)
            out.append(None if r is None else carbon.intensity(r.conventional_mw, r.demand_mw))
        return out

    def usual(self, end: datetime, weeks: int = USUAL_WEEKS) -> tuple[list[dict], int]:
        """The usual day for the day type of `end` (weekday, or Saturday and Sunday): median
        demand and carbon per quarter hour over the stored weeks before that day, and how many
        days it rests on."""
        day = end.astimezone(TZ).date()
        weekend = day.weekday() >= 5
        key = (day, weekend, weeks)
        hit = self._usual.get(key)
        now_s = datetime.now(timezone.utc).timestamp()
        if hit and now_s - hit[0] < USUAL_TTL_S:
            return hit[1]
        midnight = local_midnight(end)
        rows = self.series(midnight - timedelta(weeks=weeks), midnight - timedelta(seconds=1))
        by: dict[str, list[tuple[float, float | None]]] = {}
        days: set[date] = set()
        for r in rows:
            lt = r.t.astimezone(TZ)
            if (lt.weekday() >= 5) != weekend or r.demand_mw is None:
                continue
            days.add(lt.date())
            by.setdefault(lt.strftime("%H:%M"), []).append(
                (r.demand_mw, carbon.intensity(r.conventional_mw, r.demand_mw)))
        out = []
        for hm in sorted(by):
            vals = by[hm]
            cs = [c for _, c in vals if c is not None]
            out.append({"t": hm, "demand_mw": round(statistics.median(d for d, _ in vals)),
                        "carbon_kg_kwh": round(statistics.median(cs), 3) if cs else None})
        result = (out, len(days))
        self._usual[key] = (now_s, result)
        return result

    def snapshot(self, at: datetime | None = None) -> dict:
        """The grid now (reading the page if a new row is due) or at a moment of the city clock
        (the memory only): the reading in force, the day up to then, the usual day, the peak."""
        now = self.clock()
        live = at is None or at >= now - timedelta(minutes=1)
        if live:
            self.refresh()
        moment = now if live else at
        r = self.at(moment, LIVE_AGE * 4 if live else MOMENT_WINDOW)
        day = self.series(local_midnight(moment), moment)
        if live:
            status = ("unavailable" if r is None else
                      "stale" if self.error or moment - r.t > LIVE_AGE else "live")
        else:
            status = "known" if r is not None else "unknown"
        pts = [point(x) for x in day]
        peak = max((p for p in pts if p["demand_mw"] is not None), key=lambda p: p["demand_mw"], default=None)
        clean = min((p for p in pts if p["carbon_kg_kwh"] is not None), key=lambda p: p["carbon_kg_kwh"], default=None)
        usual, usual_days = self.usual(moment)
        out = {
            "status": status, "live": live, "at": local_iso(moment),
            "now": point(r, moment) if r else None,
            "day": pts, "usual": usual, "usual_days": usual_days,
            "peak": {"t": peak["t"], "demand_mw": peak["demand_mw"]} if peak else None,
            "cleanest": {"t": clean["t"], "carbon_kg_kwh": clean["carbon_kg_kwh"]} if clean else None,
            "installed": self.installed, "labels": LABELS, "solar_note": SOLAR_NOTE,
            "where": WHERE, "method": carbon.METHOD, "page": PAGE_URL,
            "fetched_at": local_iso(self.fetched_at) if self.fetched_at else None,
            "error": self.error if live else None,
        }
        if not live and r is None:
            out["reason"] = "the twin's memory holds no record of the grid at this moment"
        return out

    def health(self) -> dict:
        """For the data sources panel: when it was last read and how that went."""
        latest = self.latest()
        return {"fetched_at": self.fetched_at, "tried_at": self.tried_at, "error": self.error,
                "layout_changed": self.layout_changed,
                "latest": latest.t if latest else None, "latest_mw": latest.demand_mw if latest else None}


from chatbot.energy import entities as _kinds  # noqa: E402,F401 - registers the grid's kind

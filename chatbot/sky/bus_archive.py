"""Lightweight background GTFS-RT bus position recording and replay (simulation-research Idea #4).

Stores live bus snapshots from `Mobility.fleet` into its own DuckDB table `bus_positions`
created via `memory.writing()` (without altering `collector/db.py` core schema). Deduplicates
by `(vehicle_id, reported_ts)` and enforces 14-day retention.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

from collector import db

from chatbot.settings import TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

RETENTION_DAYS = 14
INTERVAL_S = 60.0
REPLAY_WINDOW_S = 180
PRUNE_EVERY_S = 3600.0
NO_BUS_RECORD_NOTE = "no recorded bus positions for this moment"

TABLE = """CREATE TABLE IF NOT EXISTS bus_positions (
    vehicle_id VARCHAR NOT NULL,
    reported_ts TIMESTAMPTZ NOT NULL,
    bus_id VARCHAR NOT NULL,
    line VARCHAR,
    line_id VARCHAR,
    color VARCHAR,
    text_color VARCHAR,
    headsign VARCHAR,
    operator VARCHAR,
    trip_id VARCHAR,
    shape_id VARCHAR,
    along_m DOUBLE,
    lat DOUBLE NOT NULL,
    lon DOUBLE NOT NULL,
    heading DOUBLE,
    speed_kmh DOUBLE,
    delay_s INTEGER,
    delay_source VARCHAR,
    waiting VARCHAR,
    finished BOOLEAN,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (vehicle_id, reported_ts)
)"""

_COLS = {
    "vehicle_id": "VARCHAR",
    "reported_ts": "BIGINT",
    "bus_id": "VARCHAR",
    "line": "VARCHAR",
    "line_id": "VARCHAR",
    "color": "VARCHAR",
    "text_color": "VARCHAR",
    "headsign": "VARCHAR",
    "operator": "VARCHAR",
    "trip_id": "VARCHAR",
    "shape_id": "VARCHAR",
    "along_m": "DOUBLE",
    "lat": "DOUBLE",
    "lon": "DOUBLE",
    "heading": "DOUBLE",
    "speed_kmh": "DOUBLE",
    "delay_s": "INTEGER",
    "delay_source": "VARCHAR",
    "waiting": "VARCHAR",
    "finished": "BOOLEAN",
    "recorded_at": "BIGINT",
}


class BusArchiver:
    """Periodically polls Mobility.fleet and archives snapshots to `bus_positions`."""

    def __init__(self, mobility, memory, *, interval_s: float = INTERVAL_S,
                 retention_days: int = RETENTION_DAYS):
        self.mobility = mobility
        self.memory = memory
        self.interval_s = interval_s
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}  # vehicle_id -> last reported_s
        self._last_prune = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rows_written = 0
        self.flushes = 0
        self.last_error: str | None = None

    def record_snapshot(self, snap, now: datetime | None = None) -> int:
        """Write unseen vehicle positions from `snap` into `bus_positions`. Never raises."""
        if self.memory is None or snap is None or not getattr(snap, "vehicles", None):
            return 0
        now = now or datetime.now(timezone.utc)
        now_us = int(now.timestamp() * 1_000_000)
        rows = []
        pending_seen: dict[str, int] = {}
        with self._lock:
            for b in snap.vehicles:
                vid = str(b.get("vehicle") or "").strip()
                rep = b.get("reported")
                lat, lon = b.get("lat"), b.get("lon")
                if not vid or rep is None or lat is None or lon is None:
                    continue
                rep_s = int(rep)
                if self._seen.get(vid) == rep_s:
                    continue
                rows.append((
                    vid,
                    rep_s * 1_000_000,
                    str(b.get("id") or f"bus-{vid}"),
                    b.get("line"),
                    b.get("line_id"),
                    b.get("color"),
                    b.get("text_color"),
                    b.get("headsign"),
                    b.get("operator"),
                    b.get("trip_id"),
                    b.get("shape_id"),
                    b.get("along_m"),
                    float(lat),
                    float(lon),
                    b.get("heading"),
                    b.get("speed_kmh"),
                    b.get("delay_s"),
                    b.get("delay_source"),
                    b.get("waiting"),
                    bool(b.get("finished", False)),
                    now_us,
                ))
                pending_seen[vid] = rep_s
        if not rows:
            return 0
        try:
            with self.memory.writing(timeout=3.0) as con:
                con.execute(TABLE)
                written = db.bulk(
                    con, _COLS, rows,
                    """INSERT OR IGNORE INTO bus_positions
                       SELECT vehicle_id, make_timestamptz(reported_ts), bus_id, line, line_id,
                              color, text_color, headsign, operator, trip_id, shape_id, along_m,
                              lat, lon, heading, speed_kmh, delay_s, delay_source, waiting,
                              finished, make_timestamptz(recorded_at)
                       FROM __SRC__""",
                )
                if time.monotonic() - self._last_prune >= PRUNE_EVERY_S:
                    cutoff_us = int((now - timedelta(days=self.retention_days)).timestamp() * 1_000_000)
                    con.execute("DELETE FROM bus_positions WHERE reported_ts < make_timestamptz(?)", [cutoff_us])
                    self._last_prune = time.monotonic()
            with self._lock:
                self._seen.update(pending_seen)
                self.flushes += 1
                self.rows_written += written
                self.last_error = None
            return written
        except Exception as e:  # noqa: BLE001 - never crash background recorder
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("bus_positions archive skipped: %s", self.last_error)
            return 0

    def step(self) -> int:
        if self.mobility is None or self.memory is None:
            return 0
        try:
            snap = self.mobility.snapshot()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:200]
            return 0
        return self.record_snapshot(snap, self.mobility.now())

    def start(self) -> None:
        if self.memory is None or self.mobility is None or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bus-archiver", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Wait briefly so server startup settles before first snapshot pull.
        if self._stop.wait(5.0):
            return
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("bus archiver loop failed")
            wait_s = max(1.0, self.interval_s - (time.monotonic() - started))
            if self._stop.wait(wait_s):
                break

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


def buses_at(memory, at: datetime, *, lines: set[str] | None = None,
             timetable_state: str = "loading", window_s: int = REPLAY_WINDOW_S) -> dict:
    """Historical bus replay from `bus_positions` at moment `at`."""
    as_of = at.astimezone(TZ).isoformat(timespec="seconds")
    empty = {
        "status": "unavailable",
        "error": NO_BUS_RECORD_NOTE,
        "note": NO_BUS_RECORD_NOTE,
        "known": False,
        "recorded": False,
        "at": as_of,
        "count": 0,
        "fetched_at": int(at.timestamp() * 1000),
        "feed_time": None,
        "timetable": timetable_state,
        "buses": [],
    }
    if memory is None:
        return empty
    lo = at - timedelta(seconds=window_s)
    hi = at + timedelta(seconds=30)
    try:
        with memory.reading() as con:
            rows = con.execute(
                """SELECT bus_id, line, line_id, color, text_color, headsign, lon, lat,
                          heading, speed_kmh, epoch_ms(reported_ts), along_m, shape_id,
                          delay_s, delay_source, finished, waiting
                   FROM (
                       SELECT *, ROW_NUMBER() OVER (PARTITION BY vehicle_id ORDER BY reported_ts DESC) AS rn
                       FROM bus_positions
                       WHERE reported_ts BETWEEN ? AND ?
                   )
                   WHERE rn = 1
                   ORDER BY bus_id""",
                [lo, hi],
            ).fetchall()
    except duckdb.CatalogException:
        return empty
    except Exception as e:  # noqa: BLE001
        log.warning("bus replay read failed: %s", e)
        return {**empty, "error": f"the twin's memory couldn't be read ({e})"}
    if not rows:
        return empty
    vehicles: list[dict] = []
    for (bid, ln, lid, col, tcol, head, lon, lat, hdg, spd, rep_ms, along, sid,
         delay_s, delay_src, finished, waiting) in rows:
        if lines and (lid or "").lower() not in lines:
            continue
        item = {
            "id": bid,
            "line": ln or "?",
            "line_id": lid,
            "color": col or "#8798b0",
            "text_color": tcol or "#0e1828",
            "headsign": head,
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            "heading": round(hdg) if hdg is not None else None,
            "speed_kmh": round(spd) if spd is not None else None,
            "reported": int(rep_ms // 1000),
            "along_m": along,
            "shape_id": sid,
            "delay_s": delay_s,
            "delay_source": delay_src,
            "finished": bool(finished) if finished else None,
            "waiting": waiting,
        }
        vehicles.append({k: v for k, v in item.items() if v is not None})
    return {
        "status": "recorded",
        "error": None,
        "known": True,
        "recorded": True,
        "at": as_of,
        "count": len(vehicles),
        "fetched_at": int(at.timestamp() * 1000),
        "feed_time": None,
        "timetable": timetable_state,
        "buses": vehicles,
    }


def archive_status(memory) -> dict:
    """Summary of stored `bus_positions` rows for health/status checks."""
    if memory is None:
        return {"rows": 0, "oldest": None, "newest": None, "retention_days": RETENTION_DAYS}
    try:
        with memory.reading() as con:
            n, lo, hi = con.execute(
                "SELECT count(*), min(reported_ts), max(reported_ts) FROM bus_positions"
            ).fetchone()
    except (duckdb.CatalogException, Exception):  # noqa: BLE001
        return {"rows": 0, "oldest": None, "newest": None, "retention_days": RETENTION_DAYS}
    return {
        "rows": int(n or 0),
        "oldest": lo.astimezone(TZ).isoformat(timespec="seconds") if lo else None,
        "newest": hi.astimezone(TZ).isoformat(timespec="seconds") if hi else None,
        "retention_days": RETENTION_DAYS,
    }

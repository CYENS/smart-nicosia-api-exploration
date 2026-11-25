"""The twin's memory: the collector's DuckDB file, written and read by the chatbot.

- `Recorder` writes what the chatbot fetches anyway (device snapshots, history ranges, traffic
  and speed answers, alarms, the inventory and the camera list) into the collector's tables,
  from a background thread, so no request ever waits on the database. It costs the city API
  nothing: it only keeps what was already fetched.
- `Memory` reads the file: which stretches it covers, raw readings, traffic and speed, its own
  health, and read-only SQL for the `run_sql` tool.

The chatbot and a separately running collector share one file by taking turns (every
connection is short, and both wait briefly for the other). Inside this process `db.GATE`
keeps reads and writes apart, since DuckDB can't hold one file read-only and read-write at
once. When the file is missing, busy for more than a moment or broken, readers raise
`Unavailable` (callers fall back to the city API or say so) and the recorder keeps its rows
in memory until the next flush. See docs/twin-memory.md.
"""

from __future__ import annotations

import datetime as dt
import decimal
import functools
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from collector import db, interpret
from collector.config import Config
from collector.jobs import (
    AlarmsJob,
    AttributesJob,
    Context,
    DevicesJob,
    Fetched,
    store_speeds,
    store_traffic,
)
from collector.parse import SpeedRow, TrafficRow

log = logging.getLogger(__name__)

# Every reader connection in this process shares one read-only instance with these settings
# (DuckDB requires one configuration per file per process). The first connection also sets the
# time zone and locks the configuration, so SQL can't change any of it.
READER_CONFIG = {"enable_external_access": False, "memory_limit": "1GB"}
READER_WAIT_S = 1.5  # how long a reader waits for the file before giving up
WRITER_WAIT_S = 5.0  # how long the recorder waits for its turn before trying again later


class Unavailable(Exception):
    """The store can't answer right now. `reason`: missing | busy | error."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class SqlRejected(ValueError):
    """A run_sql query that the guards refused or that failed; the message says why."""


def _ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


class Memory:
    def __init__(self, path: Path | str, *, timezone_name: str = "Asia/Nicosia",
                 record: bool = True, reader_wait_s: float = READER_WAIT_S,
                 writer_wait_s: float = WRITER_WAIT_S, cfg: Config | None = None):
        self.path = Path(path)
        self.tz_name = timezone_name
        self.tz = ZoneInfo(timezone_name)
        self.reader_wait_s = reader_wait_s
        self.writer_wait_s = writer_wait_s
        # The collector's settings for the jobs whose store() the recorder reuses.
        self.cfg = replace(cfg or Config(), db_path=self.path, timezone=timezone_name,
                           log_file=None)
        self.collecting = False  # the full collector runs in this process (CHATBOT_COLLECT=1)
        self._open_lock = threading.Lock()
        self.recorder = Recorder(self) if record else None

    # -- connections -------------------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    def _open_reader(self, wait_s: float) -> duckdb.DuckDBPyConnection:
        def open_once() -> duckdb.DuckDBPyConnection:
            con = duckdb.connect(str(self.path), read_only=True, config=dict(READER_CONFIG))
            try:
                with self._open_lock:
                    if not con.execute("SELECT current_setting('lock_configuration')").fetchone()[0]:
                        con.execute(f"SET GLOBAL TimeZone = '{self.tz_name}'")
                        con.execute("SET GLOBAL lock_configuration = true")
            except Exception:
                con.close()
                raise
            return con

        try:
            return db.open_waiting(open_once, wait_s)
        except duckdb.IOException as e:
            if db.is_lock_error(e):
                raise Unavailable("busy", "another program is writing to the twin's memory; "
                                          "try again in a moment") from None
            raise Unavailable("error", f"the twin's memory can't be opened: {e}") from None
        except duckdb.Error as e:
            raise Unavailable("error", f"the twin's memory can't be opened: {e}") from None

    @contextmanager
    def reading(self, timeout: float | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
        """A read-only connection (no files, no extensions, locked settings), or Unavailable."""
        if not self.path.exists():
            raise Unavailable("missing", "the twin's memory is empty: nothing has been recorded")
        wait = self.reader_wait_s if timeout is None else timeout
        deadline = time.monotonic() + wait
        try:
            hold = db.GATE.hold(write=False, timeout=wait)
            hold.__enter__()
        except db.GateTimeout:
            raise Unavailable("busy", "the twin's memory is busy saving; try again in a moment"
                              ) from None
        try:
            con = self._open_reader(max(0.05, deadline - time.monotonic()))
            try:
                yield con
            finally:
                con.close()
        finally:
            hold.__exit__(None, None, None)

    @contextmanager
    def writing(self, timeout: float | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
        wait = self.writer_wait_s if timeout is None else timeout
        with db.session(self.path, gate_timeout=wait, wait_s=wait,
                        timezone_name=self.tz_name) as con:
            yield con

    # -- what the store covers ---------------------------------------------------------------

    def coverage(self, device_type: str, devices: list[str], start_ms: int,
                 end_ms: int) -> dict[str, list[tuple[int, int]]]:
        """Per device, the stretches within [start, end] the store holds every reading for."""
        gap = db.gap_ms(device_type)
        with self.reading() as con:
            try:
                rows = con.execute(
                    """SELECT device, epoch_ms(start_ts), epoch_ms(end_ts) FROM coverage
                       WHERE device_type = ? AND epoch_ms(end_ts) >= ? AND epoch_ms(start_ts) <= ?
                         AND (device = '' OR list_contains(?, device))""",
                    [device_type, start_ms - gap, end_ms, list(devices)]).fetchall()
            except duckdb.CatalogException:  # a file from before the coverage table
                rows = []
        everyone = [(s, e + gap) for d, s, e in rows if d == ""]
        own: dict[str, list[tuple[int, int]]] = {}
        for d, s, e in rows:
            if d:
                own.setdefault(d, []).append((s, e + gap))
        out = {}
        for name in devices:
            spans = db.merge_spans(everyone + own.get(name, []), 0)
            out[name] = [(max(s, start_ms), min(e, end_ms)) for s, e in spans
                         if e >= start_ms and s <= end_ms]
        return out

    # -- raw readings ------------------------------------------------------------------------

    def readings(self, devices: list[str], keys: tuple[str, ...] | list[str], start_ms: int,
                 end_ms: int) -> dict[str, list[dict]]:
        """Stored readings per device, shaped like the API's records ({key, timestamp, value})."""
        if not devices:
            return {}
        with self.reading() as con:
            rows = con.execute(
                """SELECT device, key, epoch_ms(ts), value FROM telemetry
                   WHERE list_contains(?, device) AND list_contains(?, key)
                     AND ts BETWEEN make_timestamptz(? * 1000) AND make_timestamptz(? * 1000)""",
                [list(devices), list(keys), int(start_ms), int(end_ms)]).fetchall()
        out: dict[str, list[dict]] = {}
        for device, key, ts, value in rows:
            out.setdefault(device, []).append({"key": key, "timestamp": ts, "value": value})
        return out

    def inventory(self, device_type: str) -> list[tuple[str, float | None, float | None, str | None]]:
        """(name, lat, lon, display_name) of the stored devices of a type, for when the city
        API can't list them."""
        with self.reading() as con:
            return con.execute(
                """SELECT name, lat, lon, display_name FROM devices
                   WHERE type = ? AND coalesce(in_inventory, true) ORDER BY name""",
                [device_type]).fetchall()

    # -- traffic and speed -------------------------------------------------------------------

    def traffic_cameras(self) -> list[dict]:
        with self.reading() as con:
            rows = con.execute(
                """SELECT analytics_id, name, lat, lon, device_name FROM traffic_cameras
                   ORDER BY analytics_id""").fetchall()
        return [{"analytics_id": a, "name": n, "lat": la, "lon": lo, "device_name": d}
                for a, n, la, lo, d in rows]

    def traffic(self, granularity: str, start: dt.datetime, end: dt.datetime,
                analytics_ids: list[int] | None) -> list[tuple]:
        """(bucket_start, analytics_id, object_type, total) summed over counting lines.
        `analytics_ids` None means the city-wide rows (analytics_id 0)."""
        ids = [0] if analytics_ids is None else list(analytics_ids)
        with self.reading() as con:
            return con.execute(
                """SELECT bucket_start, analytics_id, object_type, sum(total)
                   FROM traffic_counts
                   WHERE granularity = ? AND list_contains(?, analytics_id)
                     AND bucket_start >= ? AND bucket_start < ?
                   GROUP BY ALL ORDER BY bucket_start""",
                [granularity, ids, start, end]).fetchall()

    def speeds(self, start: dt.datetime, end: dt.datetime) -> list[tuple]:
        """City-wide average speed per hour: the mean of the 5-minute buckets where the
        collector stored them, else the hourly ones the chatbot recorded."""
        with self.reading() as con:
            return con.execute(
                """SELECT date_trunc('hour', bucket_start) AS hour,
                          coalesce(avg(value) FILTER (WHERE granularity = 'FIVE_MIN'),
                                   avg(value) FILTER (WHERE granularity = 'ONE_HOUR'),
                                   avg(value)) AS kmh
                   FROM speeds
                   WHERE line_type = 'AVERAGE_SPEED' AND value IS NOT NULL
                     AND bucket_start >= ? AND bucket_start < ?
                   GROUP BY 1 ORDER BY 1""", [start, end]).fetchall()

    # -- health ------------------------------------------------------------------------------

    def health(self, now: dt.datetime) -> dict:
        """Freshness, row counts and coverage per source, for /api/health/data."""
        out: dict = {"store": {"path": str(self.path), "exists": self.path.exists()},
                     "collecting": self.collecting,
                     "recorder": self.recorder.status() if self.recorder else {"on": False}}
        if not self.path.exists():
            out["store"]["status"] = "missing"
            return out
        out["store"]["size_mb"] = round(self.path.stat().st_size / 1e6, 1)
        week_ago = now - dt.timedelta(days=7)

        def local(t):
            return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if t else None

        def age_min(t):
            return round((now - t).total_seconds() / 60) if t else None

        try:
            with self.reading(timeout=3.0) as con:
                per_type = con.execute(
                    """SELECT d.type, t.source, count(*), count(DISTINCT t.device), min(t.ts),
                              max(t.ts)
                       FROM telemetry t JOIN devices d ON d.name = t.device GROUP BY ALL"""
                ).fetchall()
                devices = con.execute(
                    "SELECT type, list(name) FROM devices WHERE in_inventory GROUP BY type"
                ).fetchall()
                try:
                    spans = con.execute(
                        """SELECT device_type, device, source, epoch_ms(start_ts),
                                  epoch_ms(end_ts) FROM coverage""").fetchall()
                except duckdb.CatalogException:
                    spans = []
                tables = {}
                for table, col in (("traffic_counts", "bucket_start"), ("speeds", "bucket_start"),
                                   ("travel_times", "measured_at"), ("events", "ts"),
                                   ("alarms", "last_seen"), ("state_changes", "ts")):
                    n, lo, hi = con.execute(
                        f"SELECT count(*), min({col}), max({col}) FROM {table}").fetchone()
                    tables[table] = (n, lo, hi)
                jobs = con.execute(
                    """SELECT job, max(started_at), arg_max(status, started_at)
                       FROM job_runs GROUP BY job ORDER BY job""").fetchall()
        except Unavailable as e:
            out["store"]["status"] = e.reason
            out["store"]["message"] = str(e)
            return out
        out["store"]["status"] = "ok"
        names = {t: n for t, n in devices}
        sources = []
        for device_type, kind in (("Lighting", "street_light"), ("Parking", "parking_sensor"),
                                  ("Wastebin", "waste_bin"), ("smartsense", "air_station"),
                                  ("InfoPoints", "info_screen"), ("Camera", "camera")):
            mine = [r for r in per_type if r[0] == device_type]
            newest = max((r[5] for r in mine), default=None)
            gap = db.gap_ms(device_type)
            t0, t1 = _ms(week_ago), _ms(now)
            every = [(s, e + gap) for t, d, _, s, e in spans if t == device_type and not d]
            pct = None
            if names.get(device_type):
                own: dict[str, list] = {}
                for t, d, _, s, e in spans:
                    if t == device_type and d:
                        own.setdefault(d, []).append((s, e + gap))
                fractions = []
                for name in names[device_type]:
                    merged = db.merge_spans(every + own.get(name, []), 0)
                    covered = sum(max(0, min(e, t1) - max(s, t0)) for s, e in merged)
                    fractions.append(covered / (t1 - t0))
                pct = round(100 * sum(fractions) / len(fractions), 1)
            sources.append({
                "source": kind, "device_type": device_type, "table": "telemetry",
                "rows": sum(r[2] for r in mine), "rows_by_source": {r[1]: r[2] for r in mine},
                "devices": max((r[3] for r in mine), default=0),
                "devices_in_inventory": len(names.get(device_type) or []),
                "oldest": local(min((r[4] for r in mine), default=None)),
                "newest": local(newest), "age_min": age_min(newest),
                "covered_last_7_days_pct": pct,
                "covered_by": sorted({s for t, _, s, _, _ in spans if t == device_type}),
            })
        for table, (n, lo, hi) in tables.items():
            sources.append({"source": table, "table": table, "rows": n, "oldest": local(lo),
                            "newest": local(hi), "age_min": age_min(hi)})
        out["sources"] = sources
        out["jobs"] = [{"job": j, "last_run": local(t), "status": s} for j, t, s in jobs]
        return out

    # -- read-only SQL -----------------------------------------------------------------------

    def run_sql(self, sql: str, *, max_rows: int = 500, time_limit_s: float = 5.0
                ) -> tuple[list[str], list[str], list[tuple], bool]:
        """One SELECT on a read-only connection with no file access, stopped after
        `time_limit_s`. Returns (columns, types, rows, truncated), at most max_rows rows."""
        try:
            statements = duckdb.extract_statements(sql)
        except duckdb.Error as e:
            raise SqlRejected(f"the SQL doesn't parse: {_first_line(e)}") from None
        if len(statements) != 1:
            raise SqlRejected(f"send exactly one statement (got {len(statements)})")
        statement = statements[0]
        if statement.type != duckdb.StatementType.SELECT:
            raise SqlRejected(f"only SELECT queries can run here, not {statement.type.name}")
        query = statement.query.strip().rstrip(";")
        with self.reading() as con:
            timer = threading.Timer(time_limit_s, con.interrupt)
            timer.start()
            try:
                cur = con.execute(f"SELECT * FROM (\n{query}\n) AS q LIMIT {int(max_rows) + 1}")
                rows = cur.fetchall()
                columns = [d[0] for d in cur.description]
                types = [str(d[1]) for d in cur.description]
            except duckdb.InterruptException:
                raise SqlRejected(f"the query ran for more than {time_limit_s:g} s and was "
                                  "stopped; aggregate more, or narrow the time range") from None
            except duckdb.Error as e:
                raise SqlRejected(f"{type(e).__name__}: {_first_line(e)}") from None
            finally:
                timer.cancel()
        return columns, types, rows[:max_rows], len(rows) > max_rows

    def json_value(self, v: object) -> object:
        """A DuckDB value as JSON a chart can read (times in local ISO, like the other tools)."""
        if isinstance(v, dt.datetime):
            if v.tzinfo is not None:
                v = v.astimezone(self.tz)
            return v.strftime("%Y-%m-%dT%H:%M:%S" if v.second else "%Y-%m-%dT%H:%M")
        if isinstance(v, dt.date):
            return v.isoformat()
        if isinstance(v, dt.time):
            return v.strftime("%H:%M")
        if isinstance(v, dt.timedelta):
            return v.total_seconds()
        if isinstance(v, decimal.Decimal):
            return float(v)
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, (bytes, bytearray)):
            return v.hex()
        if isinstance(v, float) and v != v:
            return None
        if isinstance(v, (list, tuple)):
            return [self.json_value(x) for x in v]
        if isinstance(v, dict):
            return {str(k): self.json_value(x) for k, x in v.items()}
        return v


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__


# -- the recorder ------------------------------------------------------------------------------


@dataclass
class _Batch:
    telemetry: dict[tuple[str, str, int], str | None] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    spans: list[db.Span] = field(default_factory=list)
    inventory: list | None = None
    attributes: dict[str, list[dict]] = field(default_factory=dict)
    cameras: list[dict] | None = None
    traffic: dict[tuple, TrafficRow] = field(default_factory=dict)
    speeds: dict[tuple, tuple[str, str, SpeedRow]] = field(default_factory=dict)
    alarms: dict[str, list] = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.telemetry or self.spans or self.inventory or self.attributes
                    or self.cameras or self.traffic or self.speeds or self.alarms)

    def size(self) -> int:
        return len(self.telemetry) + len(self.traffic) + len(self.speeds)


def _never_raises(fn):
    """Recorder methods take the lock, return at once and never raise: recording must never
    slow down or break an answer."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            with self._lock:
                fn(self, *args, **kwargs)
                self._bound()
        except Exception as e:  # noqa: BLE001
            log.warning("recorder: %s failed: %s", fn.__name__, e)
    return wrapper


class Recorder:
    """Keeps what the chatbot fetched and writes it to the store every `flush_every_s`.

    Every method here returns at once and never raises: recording must never slow down or
    break an answer. Rows wait in memory (at most `max_rows`) while the store is busy."""

    def __init__(self, memory: Memory, *, flush_every_s: float = 30.0, max_rows: int = 300_000):
        self.memory = memory
        self.flush_every_s = flush_every_s
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._batch = _Batch()
        self._written: dict[tuple[str, str], int] = {}  # newest ts stored per (device, key)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.flushes = 0
        self.rows_written = 0
        self.busy_retries = 0
        self.dropped_rows = 0
        self.last_flush: dt.datetime | None = None
        self.last_error: str | None = None

    # -- what the chatbot hands over ---------------------------------------------------------

    @_never_raises
    def inventory(self, payload: list) -> None:
        """getTenantDevices as fetched."""
        if payload:
            self._batch.inventory = payload

    @_never_raises
    def attributes(self, records_by_device: dict[str, list[dict]]) -> None:
        """getLatestAttribute records per device."""
        self._batch.attributes.update({d: r for d, r in records_by_device.items() if r})

    @_never_raises
    def cameras(self, rows: list[dict]) -> None:
        """The traffic cameras as collector.jobs.map_cameras gives them."""
        if rows:
            self._batch.cameras = rows

    @_never_raises
    def snapshot(self, device_type: str, records_by_device: dict[str, list[dict]], at_ms: int,
                 *, every_device: bool) -> None:
        """Latest readings at `at_ms`, for every device of the type or only for these."""
        keys = set(interpret.READING_KEYS.get(device_type, ()))
        for device, records in records_by_device.items():
            self._batch.types[device] = device_type
            for r in records:
                key, ts = r["key"], int(r["timestamp"])
                if key in keys and ts > self._written.get((device, key), -1):
                    self._batch.telemetry[(device, key, ts)] = r.get("value")
        if every_device:
            self._batch.spans.append((device_type, "", "chatbot", at_ms, at_ms))
        else:
            self._batch.spans.extend((device_type, d, "chatbot", at_ms, at_ms)
                                     for d in records_by_device)

    @_never_raises
    def readings(self, device_type: str, records_by_device: dict[str, list[dict]],
                 spans: list[tuple[str, int, int]]) -> None:
        """A range of past readings (getTelemetryRange), complete over `spans`
        [(device, start_ms, end_ms)]."""
        for device, records in records_by_device.items():
            self._batch.types[device] = device_type
            for r in records:
                self._batch.telemetry[(device, r["key"], int(r["timestamp"]))] = r.get("value")
        self._batch.spans.extend((device_type, d, "chatbot", s, e) for d, s, e in spans)

    @_never_raises
    def traffic(self, rows: list[TrafficRow]) -> None:
        """Finished traffic buckets (per camera and line, or city-wide)."""
        for r in rows:
            self._batch.traffic[(r.granularity, r.bucket_start, r.analytics_id, r.line_name,
                                 r.object_type)] = r

    @_never_raises
    def speeds(self, rows: list[SpeedRow], granularity: str, line_type: str) -> None:
        for r in rows:
            self._batch.speeds[(granularity, line_type, r.bucket_start)] = (granularity,
                                                                            line_type, r)

    @_never_raises
    def alarms(self, payload_by_state: dict[str, list]) -> None:
        self._batch.alarms.update(payload_by_state)

    def _bound(self) -> None:
        """Drop the oldest rows beyond max_rows (and the coverage they backed)."""
        extra = self._batch.size() - self.max_rows
        if extra <= 0:
            return
        for key in list(self._batch.telemetry)[:extra]:
            del self._batch.telemetry[key]
        self._batch.spans.clear()  # what remains may no longer be complete
        self.dropped_rows += extra
        if self._batch.size() > self.max_rows:  # traffic or speed rows over the limit
            self._batch.traffic.clear()
            self._batch.speeds.clear()

    # -- writing -----------------------------------------------------------------------------

    def pending(self) -> int:
        with self._lock:
            return self._batch.size() + len(self._batch.spans)

    def flush(self, timeout: float | None = None) -> bool:
        """Write everything waiting. False if the store was busy (the rows stay waiting)."""
        with self._flush_lock:
            with self._lock:
                batch, self._batch = self._batch, _Batch()
            if batch.empty():
                return True
            try:
                with self.memory.writing(timeout) as con:
                    written = self._write(con, batch)
            except (db.GateTimeout, duckdb.IOException) as e:
                if isinstance(e, duckdb.IOException) and not db.is_lock_error(e):
                    return self._failed(e)
                self.busy_retries += 1
                with self._lock:
                    self._merge_back(batch)
                    self._bound()
                return False
            except Exception as e:  # noqa: BLE001 - a broken batch is dropped, not retried
                return self._failed(e)
            for (device, key, ts) in batch.telemetry:
                if ts > self._written.get((device, key), -1):
                    self._written[(device, key)] = ts
            self.flushes += 1
            self.rows_written += written
            self.last_flush = db.utcnow()
            self.last_error = None
            return True

    def _failed(self, e: Exception) -> bool:
        self.last_error = f"{type(e).__name__}: {_first_line(e)}"
        log.warning("recorder: could not write to %s: %s", self.memory.path, self.last_error)
        return False

    def _merge_back(self, older: _Batch) -> None:
        """Put a batch that couldn't be written back in front of what arrived since."""
        newer = self._batch
        older.telemetry.update(newer.telemetry)
        older.types.update(newer.types)
        older.spans.extend(newer.spans)
        older.inventory = newer.inventory or older.inventory
        older.attributes.update(newer.attributes)
        older.cameras = newer.cameras or older.cameras
        older.traffic.update(newer.traffic)
        older.speeds.update(newer.speeds)
        older.alarms.update(newer.alarms)
        self._batch = older

    def _write(self, con: duckdb.DuckDBPyConnection, b: _Batch) -> int:
        now = db.utcnow()
        ctx = Context(self.memory.cfg, now)
        written = 0
        if b.inventory:
            written += DevicesJob().store(con, Fetched(b.inventory), ctx).rows
        if b.attributes:
            written += AttributesJob().store(con, Fetched(b.attributes), ctx).rows
        if b.cameras:
            _store_cameras(con, b.cameras, now)
        if b.telemetry:
            # derive_state_changes joins devices, so every device needs its row first.
            db.bulk(
                con, {"name": "VARCHAR", "type": "VARCHAR"}, list(b.types.items()),
                f"""INSERT INTO devices (name, type, in_inventory, first_seen)
                    SELECT name, type, true, make_timestamptz({int(now.timestamp() * 1e6)})
                    FROM __SRC__ ON CONFLICT (name) DO NOTHING""")
            rows = [(d, k, ts, v) for (d, k, ts), v in b.telemetry.items()]
            written += db.store_telemetry(con, rows, "chatbot", b.types)
        if b.spans:
            db.add_coverage(con, b.spans)
        if b.traffic:
            written += store_traffic(con, list(b.traffic.values()), now)
        if b.speeds:
            groups: dict[tuple[str, str], list[SpeedRow]] = {}
            for g, lt, r in b.speeds.values():
                groups.setdefault((g, lt), []).append(r)
            for (g, lt), rows in groups.items():
                written += store_speeds(con, rows, g, lt, now)
        if b.alarms:
            AlarmsJob().store(con, Fetched(b.alarms), ctx)
        return written

    # -- the background thread ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="twin-recorder", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_every_s)
            self._wake.clear()
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - the thread must outlive any one flush
                log.exception("recorder: flush failed")

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the thread and write what is waiting (best effort)."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush(timeout)

    def status(self) -> dict:
        return {"on": True, "pending_rows": self.pending(), "flushes": self.flushes,
                "rows_written": self.rows_written, "busy_retries": self.busy_retries,
                "dropped_rows": self.dropped_rows,
                "last_flush": self.last_flush.astimezone(self.memory.tz).strftime(
                    "%Y-%m-%d %H:%M:%S") if self.last_flush else None,
                "last_error": self.last_error}


def _store_cameras(con: duckdb.DuckDBPyConnection, rows: list[dict], now: dt.datetime) -> None:
    """The camera list as collector.jobs.CamerasMapJob stores it."""
    cols = ["analytics_id", "name", "status", "stream_id", "stream_name", "lines",
            "device_name", "lat", "lon", "location_source"]
    types = {"analytics_id": "INTEGER", "stream_id": "INTEGER", "lat": "DOUBLE", "lon": "DOUBLE"}
    db.bulk(
        con, {c: types.get(c, "VARCHAR") for c in cols},
        [tuple(r.get(c) for c in cols) for r in rows],
        f"""INSERT OR REPLACE INTO traffic_cameras
            SELECT *, make_timestamptz({int(now.timestamp() * 1e6)}) FROM __SRC__""")

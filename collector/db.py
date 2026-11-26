"""DuckDB storage: schema, short-lived connections and fast idempotent inserts.

Connections are opened per job and closed straight away. DuckDB lets one process hold the file
at a time, so this keeps `collector status` (and a backfill) able to get in between jobs.
All timestamps are TIMESTAMPTZ.

Sharing the file (see docs/twin-memory.md):

- Between processes, DuckDB allows one writer or several readers, never both. Everybody keeps
  connections short (open, work, close) and `connect` waits while another process holds the
  file, so the collector, a backfill and the chatbot take turns.
- Within one process, every connection to a file must use the same configuration, so a
  read-only connection can't be open while a read-write one is. `GATE` makes the sessions of
  one process take turns: readers together, one writer alone.
- `coverage` records which stretches of time the store holds for each device type (or one
  device), so a reader can tell "nothing happened" from "nobody was recording".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from collector import interpret

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    name VARCHAR PRIMARY KEY,
    type VARCHAR,
    lat DOUBLE,
    lon DOUBLE,
    label VARCHAR,
    display_name VARCHAR,            -- attribute `name`, e.g. "Mnasiadou 2" (lamps carry the street)
    postal_code VARCHAR,
    metadata VARCHAR,                -- attribute `metadata` (JSON text)
    inactivity_timeout_ms BIGINT,
    last_activity TIMESTAMPTZ,       -- attribute `lastActivityTime`
    last_telemetry TIMESTAMPTZ,      -- newest telemetry timestamp we stored
    in_inventory BOOLEAN,
    first_seen TIMESTAMPTZ,
    inventory_seen TIMESTAMPTZ,
    attributes_updated TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS attributes (
    device VARCHAR, key VARCHAR, ts TIMESTAMPTZ, value VARCHAR,
    PRIMARY KEY (device, key)
);
CREATE TABLE IF NOT EXISTS telemetry (
    device VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    value VARCHAR,
    value_num DOUBLE,
    source VARCHAR,                  -- latest | stream | backfill | chatbot
    PRIMARY KEY (device, key, ts)
);
CREATE TABLE IF NOT EXISTS coverage (
    device_type VARCHAR NOT NULL,
    device VARCHAR NOT NULL,         -- '' = every device of the type
    source VARCHAR NOT NULL,         -- collector | backfill | chatbot
    start_ts TIMESTAMPTZ NOT NULL,   -- readings are complete from here...
    end_ts TIMESTAMPTZ NOT NULL,     -- ...to here (plus COVERAGE_GAP, see below)
    PRIMARY KEY (device_type, device, source, start_ts)
);
CREATE TABLE IF NOT EXISTS state_changes (
    device VARCHAR NOT NULL,
    device_type VARCHAR,
    key VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    old_value VARCHAR,
    new_value VARCHAR,
    old_state VARCHAR,
    new_state VARCHAR,
    PRIMARY KEY (device, key, ts)
);
CREATE TABLE IF NOT EXISTS events (
    ts TIMESTAMPTZ NOT NULL,
    device VARCHAR,
    message VARCHAR NOT NULL,
    PRIMARY KEY (ts, message)
);
CREATE TABLE IF NOT EXISTS alarms (
    id VARCHAR PRIMARY KEY,
    device VARCHAR,
    device_type VARCHAR,
    type VARCHAR,
    severity VARCHAR,
    status VARCHAR,
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    created_ts TIMESTAMPTZ,
    ack_ts TIMESTAMPTZ,
    clear_ts TIMESTAMPTZ,
    additional_info VARCHAR,
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS traffic_counts (
    granularity VARCHAR NOT NULL,    -- day | hour
    bucket_start TIMESTAMPTZ NOT NULL,
    analytics_id INTEGER NOT NULL,   -- 0 = all cameras (city-wide)
    line_name VARCHAR NOT NULL,      -- '' for city-wide totals
    object_type INTEGER NOT NULL,    -- see object_types
    dir1 INTEGER,                    -- crossings in direction "1" of the counting line
    dir2 INTEGER,
    total INTEGER,
    fetched_at TIMESTAMPTZ,
    PRIMARY KEY (granularity, bucket_start, analytics_id, line_name, object_type)
);
CREATE TABLE IF NOT EXISTS object_types (id INTEGER PRIMARY KEY, label VARCHAR);
CREATE TABLE IF NOT EXISTS speeds (
    granularity VARCHAR NOT NULL,    -- FIVE_MIN | ONE_HOUR | ...
    line_type VARCHAR NOT NULL,      -- AVERAGE_SPEED
    bucket_start TIMESTAMPTZ NOT NULL,
    sample_time TIMESTAMPTZ,
    label VARCHAR,
    value DOUBLE,                    -- km/h, city-wide
    fetched_at TIMESTAMPTZ,
    PRIMARY KEY (granularity, line_type, bucket_start)
);
CREATE TABLE IF NOT EXISTS travel_times (
    segment_id VARCHAR NOT NULL,
    segment_name VARCHAR,            -- "1004->1008": from site -> to site
    from_site VARCHAR,
    to_site VARCHAR,
    measured_at TIMESTAMPTZ NOT NULL,
    travel_time_s DOUBLE,
    speed_kmh DOUBLE,
    publication_time TIMESTAMPTZ,
    PRIMARY KEY (segment_id, measured_at)
);
CREATE TABLE IF NOT EXISTS traffic_cameras (
    analytics_id INTEGER PRIMARY KEY,
    name VARCHAR,
    status VARCHAR,
    stream_id INTEGER,
    stream_name VARCHAR,
    lines VARCHAR,                   -- JSON: counting lines (id, name, direction, types)
    device_name VARCHAR,             -- Camera device with exactly the same name, if any
    lat DOUBLE,
    lon DOUBLE,
    location_source VARCHAR,         -- device | manual | NULL (unknown)
    updated_at TIMESTAMPTZ
);
CREATE SEQUENCE IF NOT EXISTS job_run_id;
CREATE TABLE IF NOT EXISTS job_runs (
    id BIGINT DEFAULT nextval('job_run_id') PRIMARY KEY,
    job VARCHAR,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    status VARCHAR,                  -- ok | partial | error
    requests INTEGER,
    rows_written BIGINT,
    errors INTEGER,
    message VARCHAR
);
CREATE TABLE IF NOT EXISTS api_errors (
    ts TIMESTAMPTZ,
    job VARCHAR,
    endpoint VARCHAR,
    params VARCHAR,
    kind VARCHAR,                    -- http | incoresoft | parse
    message VARCHAR
);
CREATE TABLE IF NOT EXISTS watermarks (
    name VARCHAR PRIMARY KEY,
    value BIGINT,
    updated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS backfill_progress (
    device VARCHAR,
    keys VARCHAR,
    oldest_ms BIGINT,                -- oldest timestamp reached so far
    target_ms BIGINT,                -- paging stops here
    done BOOLEAN,
    updated_at TIMESTAMPTZ,
    PRIMARY KEY (device, keys)
);
"""

LOCK_HINTS = ("lock", "being used by another process", "conflicting lock")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_lock_error(e: BaseException) -> bool:
    """Whether a DuckDB error means another process (or connection) holds the file."""
    return isinstance(e, duckdb.IOException) and any(h in str(e).lower() for h in LOCK_HINTS)


class GateTimeout(TimeoutError):
    """The process's other sessions held the database for longer than the caller would wait."""


class Gate:
    """Readers together, one writer alone, within one process. Waiting writers go first, so a
    stream of short reads can't keep a write out for ever."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @contextmanager
    def hold(self, *, write: bool, timeout: float | None = None) -> Iterator[None]:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if write:
                self._writers_waiting += 1
            try:
                while self._writer or (self._readers if write else self._writers_waiting):
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        raise GateTimeout("the database is busy in this process")
                    self._cond.wait(left)
            finally:
                if write:
                    self._writers_waiting -= 1
            if write:
                self._writer = True
            else:
                self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                if write:
                    self._writer = False
                else:
                    self._readers -= 1
                self._cond.notify_all()


GATE = Gate()


def open_waiting(open_fn, wait_s: float):
    """Call open_fn() until it stops failing because another process holds the file."""
    deadline = time.monotonic() + wait_s
    delay = 0.1
    while True:
        try:
            return open_fn()
        except duckdb.IOException as e:
            if not is_lock_error(e) or time.monotonic() > deadline:
                raise
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())) or 0.01)
            delay = min(delay * 2, 2.0)


def connect(
    path: Path | str,
    *,
    read_only: bool = False,
    wait_s: float = 120.0,
    timezone_name: str = "Asia/Nicosia",
) -> duckdb.DuckDBPyConnection:
    """Open the database, waiting while another process holds the file lock."""
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = open_waiting(lambda: duckdb.connect(str(path), read_only=read_only), wait_s)
    con.execute(f"SET TimeZone = '{timezone_name}'")
    if not read_only:
        con.execute(SCHEMA)
    return con


@contextmanager
def session(
    path: Path | str, *, gate_timeout: float | None = None, **kwargs
) -> Iterator[duckdb.DuckDBPyConnection]:
    """A short-lived connection, taking its turn with the process's other sessions (`GATE`)."""
    with GATE.hold(write=not kwargs.get("read_only", False), timeout=gate_timeout):
        con = connect(path, **kwargs)
        try:
            yield con
        finally:
            con.close()


# -- coverage ----------------------------------------------------------------------------------

# Two observations of a device type at most this far apart count as one unbroken stretch, and a
# stretch is good for this long after its last observation: about one report interval.
COVERAGE_GAP: dict[str, timedelta] = {
    "Parking": timedelta(minutes=20),  # status changes come at any time
    "Lighting": timedelta(minutes=65),  # lit lamps report at least hourly
    "Wastebin": timedelta(minutes=65),
    "smartsense": timedelta(minutes=65),
    "InfoPoints": timedelta(minutes=65),
    "Camera": timedelta(minutes=20),
    "ParkingLot": timedelta(minutes=65),
}
DEFAULT_GAP = timedelta(minutes=30)

# (device_type, device or '' for all, source, start_ms, end_ms)
Span = tuple[str, str, str, int, int]


def gap_ms(device_type: str) -> int:
    return int(COVERAGE_GAP.get(device_type, DEFAULT_GAP).total_seconds() * 1000)


def merge_spans(spans: Iterable[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    """Merge (start, end) intervals that overlap or lie at most `gap` apart."""
    out: list[list[int]] = []
    for s, e in sorted((min(a, b), max(a, b)) for a, b in spans):
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def add_coverage(con: duckdb.DuckDBPyConnection, spans: Iterable[Span]) -> int:
    """Record stretches of complete readings, merged with the stored ones they touch."""
    new: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    for device_type, device, source, start, end in spans:
        if start is None or end is None:
            continue
        new.setdefault((device_type, device or "", source), []).append((int(start), int(end)))
    if not new:
        return 0
    keys = list(new)
    existing = con.execute(  # the table holds merged stretches, so it stays small
        """SELECT device_type, device, source, epoch_ms(start_ts), epoch_ms(end_ts)
           FROM coverage WHERE list_contains(?, device_type) AND list_contains(?, source)""",
        [sorted({k[0] for k in keys}), sorted({k[2] for k in keys})],
    ).fetchall()
    for t, d, s, a, b in existing:
        if (t, d, s) in new:
            new[(t, d, s)].append((a, b))
    rows = [(t, d, s, a, b) for (t, d, s), items in new.items()
            for a, b in merge_spans(items, gap_ms(t))]
    bulk(
        con,
        {"device_type": "VARCHAR", "device": "VARCHAR", "source": "VARCHAR"},
        keys,
        """DELETE FROM coverage USING __SRC__ k
           WHERE coverage.device_type = k.device_type AND coverage.device = k.device
             AND coverage.source = k.source""",
    )
    return bulk(
        con,
        {"device_type": "VARCHAR", "device": "VARCHAR", "source": "VARCHAR", "s": "BIGINT",
         "e": "BIGINT"},
        rows,
        """INSERT INTO coverage SELECT device_type, device, source, make_timestamptz(s * 1000),
                  make_timestamptz(e * 1000) FROM __SRC__""",
    )


# -- bulk loading ------------------------------------------------------------------------------


def _json_default(o):
    if isinstance(o, datetime):
        return int(o.timestamp() * 1_000_000)
    raise TypeError(type(o))


def bulk(
    con: duckdb.DuckDBPyConnection,
    columns: dict[str, str],
    rows: Sequence[Sequence],
    sql: str,
) -> int:
    """Run `sql` against the rows exposed as relation `__SRC__` with the given column types.

    Rows go through a temporary NDJSON file, which DuckDB reads about 100x faster than
    executemany(). datetimes travel as epoch microseconds; declare them BIGINT and wrap them in
    make_timestamptz() in `sql`. Returns the number of rows the statement reports.
    """
    if not rows:
        return 0
    names = list(columns)
    fd, tmp = tempfile.mkstemp(suffix=".ndjson", prefix="collector-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(dict(zip(names, row)), default=_json_default))
                f.write("\n")
        spec = "{" + ", ".join(f"'{n}': '{t}'" for n, t in columns.items()) + "}"
        src = f"read_json(?, format = 'newline_delimited', columns = {spec})"
        cursor = con.execute(sql.replace("__SRC__", src), [tmp])
        try:
            result = cursor.fetchone()
        except duckdb.InvalidInputException:  # statement without a result set
            return 0
        return int(result[0]) if result and isinstance(result[0], int) else 0
    finally:
        os.remove(tmp)


# -- telemetry ---------------------------------------------------------------------------------

TELEMETRY_COLUMNS = {"device": "VARCHAR", "key": "VARCHAR", "ms": "BIGINT", "value": "VARCHAR",
                     "value_num": "DOUBLE"}


def insert_telemetry(
    con: duckdb.DuckDBPyConnection,
    rows: Iterable[tuple[str, str, int, str | None]],
    source: str,
) -> int:
    """Insert (device, key, ts_ms, value) rows, ignoring ones already stored. Returns new rows."""
    prepared = [(d, k, int(ts), v, interpret.to_number(v)) for d, k, ts, v in rows]
    if not prepared:
        return 0
    return bulk(
        con,
        TELEMETRY_COLUMNS,
        prepared,
        f"""INSERT OR IGNORE INTO telemetry (device, key, ts, value, value_num, source)
            SELECT device, key, make_timestamptz(ms * 1000), value, value_num, '{source}'
            FROM __SRC__""",
    )


def touch_last_telemetry(
    con: duckdb.DuckDBPyConnection, newest: dict[str, int]
) -> None:
    """Advance devices.last_telemetry to the newest reading timestamp per device."""
    if not newest:
        return
    bulk(
        con,
        {"device": "VARCHAR", "ms": "BIGINT"},
        list(newest.items()),
        """UPDATE devices SET last_telemetry = greatest(coalesce(last_telemetry, s.ts), s.ts)
           FROM (SELECT device, make_timestamptz(ms * 1000) AS ts FROM __SRC__) s
           WHERE devices.name = s.device""",
    )


def newest_reading_per_device(
    rows: Iterable[tuple[str, str, int, str | None]], device_types: dict[str, str]
) -> dict[str, int]:
    """Newest timestamp per device, counting only keys that are evidence of a report."""
    newest: dict[str, int] = {}
    for device, key, ts, _ in rows:
        reading_keys = interpret.READING_KEYS.get(device_types.get(device, ""), ())
        if key in reading_keys and ts > newest.get(device, 0):
            newest[device] = ts
    return newest


def derive_state_changes(
    con: duckdb.DuckDBPyConnection,
    since_by_pair: dict[tuple[str, str], int],
) -> int:
    """Recompute state_changes for (device, key) pairs from `since` (ms) onwards.

    For each pair it reads the last stored value before `since` plus everything after, classifies
    each value with interpret.classify(), and replaces the pair's changes in that range. Doing it
    from stored telemetry makes it correct even when rows arrive out of order (backfill).
    """
    pairs = [(d, k, ms) for (d, k), ms in since_by_pair.items()]
    if not pairs:
        return 0
    con.execute("DROP TABLE IF EXISTS _affected")
    bulk(
        con,
        {"device": "VARCHAR", "key": "VARCHAR", "ms": "BIGINT"},
        pairs,
        """CREATE TEMP TABLE _affected AS
           SELECT a.device, a.key, d.type AS device_type, make_timestamptz(a.ms * 1000) AS since
           FROM __SRC__ a JOIN devices d ON d.name = a.device""",
    )
    rows = con.execute(
        """
        WITH base AS (
            SELECT a.device, a.key, a.device_type, a.since,
                   (SELECT max(t.ts) FROM telemetry t
                    WHERE t.device = a.device AND t.key = a.key AND t.ts < a.since) AS prev_ts
            FROM _affected a
        )
        SELECT b.device, b.key, b.device_type, b.since, t.ts, t.value
        FROM base b JOIN telemetry t ON t.device = b.device AND t.key = b.key
        WHERE t.ts >= coalesce(b.prev_ts, b.since)
        ORDER BY b.device, b.key, t.ts
        """
    ).fetchall()
    changes = []
    prev: tuple[str, str] | None = None
    last_state = last_value = None
    for device, key, device_type, since, ts, value in rows:
        if (device, key) != prev:
            prev = (device, key)
            last_state = last_value = None
        state = interpret.classify(device_type, key, value)
        if state is None:
            continue
        if last_state is not None and state != last_state and ts >= since:
            changes.append((device, device_type, key, ts, last_value, value, last_state, state))
        last_state, last_value = state, value
    con.execute(
        """DELETE FROM state_changes USING _affected a
           WHERE state_changes.device = a.device AND state_changes.key = a.key
             AND state_changes.ts >= a.since"""
    )
    written = bulk(
        con,
        {"device": "VARCHAR", "device_type": "VARCHAR", "key": "VARCHAR", "ts": "BIGINT",
         "old_value": "VARCHAR", "new_value": "VARCHAR", "old_state": "VARCHAR",
         "new_state": "VARCHAR"},
        changes,
        """INSERT OR REPLACE INTO state_changes
           SELECT device, device_type, key, make_timestamptz(ts), old_value, new_value,
                  old_state, new_state FROM __SRC__""",
    )
    con.execute("DROP TABLE IF EXISTS _affected")
    return written


def tracked_pairs(
    rows: Iterable[tuple[str, str, int, str | None]], device_types: dict[str, str]
) -> dict[tuple[str, str], int]:
    """(device, key) -> oldest ts among rows whose key feeds state_changes."""
    out: dict[tuple[str, str], int] = {}
    for device, key, ts, _ in rows:
        if key in interpret.TRACKED_KEYS.get(device_types.get(device, ""), ()):
            pair = (device, key)
            out[pair] = min(ts, out.get(pair, ts))
    return out


def store_telemetry(
    con: duckdb.DuckDBPyConnection,
    rows: list[tuple[str, str, int, str | None]],
    source: str,
    device_types: dict[str, str],
    *,
    derive: bool = True,
) -> int:
    """Insert telemetry, advance last_telemetry and refresh state_changes. Returns new rows."""
    written = insert_telemetry(con, rows, source)
    touch_last_telemetry(con, newest_reading_per_device(rows, device_types))
    if derive and written:
        derive_state_changes(con, tracked_pairs(rows, device_types))
    return written


# -- small helpers -----------------------------------------------------------------------------


def device_types(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return dict(con.execute("SELECT name, type FROM devices").fetchall())


def devices_of_type(con: duckdb.DuckDBPyConnection, device_type: str) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT name FROM devices WHERE type = ? AND in_inventory ORDER BY name", [device_type]
        ).fetchall()
    ]


def get_watermark(con: duckdb.DuckDBPyConnection, name: str) -> int | None:
    row = con.execute("SELECT value FROM watermarks WHERE name = ?", [name]).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def set_watermark(con: duckdb.DuckDBPyConnection, name: str, value: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO watermarks VALUES (?, ?, ?)", [name, int(value), utcnow()]
    )


def record_error(
    con: duckdb.DuckDBPyConnection, job: str, endpoint: str, params: dict | None, kind: str,
    message: str, when: datetime | None = None,
) -> None:
    con.execute(
        "INSERT INTO api_errors VALUES (?, ?, ?, ?, ?, ?)",
        [when or utcnow(), job, endpoint, json.dumps(params or {}, default=str), kind, message],
    )

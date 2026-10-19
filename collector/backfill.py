"""`collector backfill`: a gentle one-off fetch of the history the API still holds.

- Device telemetry pages getTelemetryRange backwards (1,000 records per page, shared across keys)
  with at most 2 requests in flight and a pause between requests. Progress is saved per device,
  so an interrupted backfill resumes where it stopped.
- The city event log pages the same way (1,000 events is about two weeks).
- Traffic counts use group_by=stream in chunks of at most 30 days (longer ranges take 30-60 s or
  time out), plus one city-wide hourly request per day.
- City speed uses generalReports in 7-day chunks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from collector import db, interpret, parse
from collector.config import Config
from collector.http import ApiError, Client
from collector.jobs import (
    store_events,
    store_speeds,
    store_traffic,
    to_ms,
    traffic_request,
)

log = logging.getLogger(__name__)

MAX_CONCURRENCY = 2
DEFAULT_TYPES = ("Parking", "Lighting", "Wastebin", "smartsense", "InfoPoints", "Camera")
WHAT = ("events", "speed", "traffic", "telemetry")


@dataclass
class BackfillOptions:
    since: datetime
    until: datetime
    what: tuple[str, ...] = WHAT
    types: tuple[str, ...] = DEFAULT_TYPES
    devices: tuple[str, ...] | None = None
    max_requests: int = 5000
    concurrency: int = 1
    delay_s: float = 0.5
    reset: bool = False
    flush_rows: int = 20_000
    flush_seconds: float = 15.0


@dataclass
class BackfillReport:
    requests: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    devices_done: int = 0
    devices_pending: int = 0
    stopped_by_budget: bool = False

    def add(self, what: str, n: int) -> None:
        self.rows[what] = self.rows.get(what, 0) + n


def run_backfill(cfg: Config, opts: BackfillOptions, *, client: Client | None = None,
                 log_fn: Callable[[str], None] = print) -> BackfillReport:
    if not 1 <= opts.concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY}")
    client = client or Client(cfg, timeout_s=max(cfg.timeout_s, 120.0),
                              request_delay_s=opts.delay_s)
    report = BackfillReport()
    started = db.utcnow()

    def budget_left() -> int:
        return opts.max_requests - client.stats.requests

    try:
        for what in opts.what:
            if budget_left() <= 0:
                report.stopped_by_budget = True
                break
            log_fn(f"== {what}")
            {"events": _events, "speed": _speed, "traffic": _traffic,
             "telemetry": _telemetry}[what](cfg, opts, client, report, budget_left, log_fn)
    finally:
        report.requests = client.stats.requests
        if budget_left() <= 0:
            report.stopped_by_budget = True
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            con.execute(
                """INSERT INTO job_runs (job, started_at, finished_at, status, requests,
                                         rows_written, errors, message)
                   VALUES ('backfill', ?, ?, ?, ?, ?, ?, ?)""",
                [started, db.utcnow(), "partial" if report.errors else "ok", report.requests,
                 sum(report.rows.values()), len(report.errors),
                 f"rows {report.rows}; devices done {report.devices_done}, "
                 f"pending {report.devices_pending}"[:2000]],
            )
            for e in report.errors[:50]:
                db.record_error(con, "backfill", "", None, "backfill", e)
    return report


# -- city event log ----------------------------------------------------------------------------


def _events(cfg, opts, client, report, budget_left, log_fn):
    with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
        oldest = con.execute("SELECT min(ts) FROM events").fetchone()[0]
    end = to_ms(oldest) if oldest and not opts.reset else to_ms(opts.until)
    start = to_ms(opts.since)
    if end <= start:
        log_fn("events: already back to --since")
        return
    total = 0
    try:
        for page in client.range_pages("TENANT", cfg.tenant, ("toastDesc",), start, end,
                                       stop=lambda: budget_left() <= 0):
            with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
                total += store_events(con, parse.parse_events(page))
    except ApiError as e:
        report.errors.append(str(e))
    report.add("events", total)
    log_fn(f"events: {total} new")


# -- city speed --------------------------------------------------------------------------------


def _speed(cfg, opts, client, report, budget_left, log_fn):
    gb, lt = cfg.speed_group_by, cfg.speed_line_type
    with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
        oldest = con.execute(
            "SELECT min(bucket_start) FROM speeds WHERE granularity = ? AND line_type = ?",
            [gb, lt]).fetchone()[0]
    end = oldest if oldest and not opts.reset else opts.until
    chunk = timedelta(days=7)
    total = 0
    while end > opts.since and budget_left() > 0:
        start = max(opts.since, end - chunk)
        try:
            payload = client.get_json("generalReports", group_by=gb, line_type=lt,
                                      start_date=to_ms(start), end_date=to_ms(end))
        except ApiError as e:
            report.errors.append(str(e))
            break
        rows = parse.parse_speed(payload or [], ZoneInfo(cfg.timezone))
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            total += store_speeds(con, rows, gb, lt, db.utcnow())
        log_fn(f"speed: {start:%Y-%m-%d}..{end:%Y-%m-%d} {len(rows)} buckets")
        if not rows and start < datetime(2023, 9, 1, tzinfo=start.tzinfo):
            break  # history starts around Oct 2023
        end = start
    report.add("speeds", total)


# -- traffic counts ----------------------------------------------------------------------------


def _traffic(cfg, opts, client, report, budget_left, log_fn):
    tz = ZoneInfo(cfg.timezone)
    first = opts.since.astimezone(tz).date()
    last = min(opts.until.astimezone(tz).date(), db.utcnow().astimezone(tz).date())
    last -= timedelta(days=1)  # only finished days
    with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
        have_daily = {r[0] for r in con.execute(
            """SELECT DISTINCT CAST(timezone(?, bucket_start) AS DATE) FROM traffic_counts
               WHERE granularity = 'day' AND analytics_id > 0""", [cfg.timezone]).fetchall()}
        have_hourly = {r[0] for r in con.execute(
            """SELECT CAST(timezone(?, bucket_start) AS DATE) FROM traffic_counts
               WHERE granularity = 'hour' AND analytics_id = 0
               GROUP BY 1 HAVING count(DISTINCT bucket_start) >= 20""",
            [cfg.timezone]).fetchall()}
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    if opts.reset:
        have_daily = have_hourly = set()
    missing_daily = [d for d in days if d not in have_daily]
    max_days = min(cfg.traffic_max_days_per_request, 30)
    total = 0
    for a, b in _runs(missing_daily, max_days):
        if budget_left() <= 0:
            return
        try:
            payload = traffic_request(client, "stream", a, b, tz)
        except ApiError as e:
            report.errors.append(f"traffic {a}..{b}: {e}")
            continue
        rows = parse.parse_traffic_stream(payload, tz)
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            total += store_traffic(con, rows, db.utcnow())
        log_fn(f"traffic per camera: {a}..{b} {len(rows)} rows")
    for d in [d for d in days if d not in have_hourly]:
        if budget_left() <= 0:
            return
        try:
            payload = traffic_request(client, "hour", d, d, tz)
        except ApiError as e:
            report.errors.append(f"traffic hourly {d}: {e}")
            continue
        rows = parse.parse_traffic_buckets(payload, tz, day=d)
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            total += store_traffic(con, rows, db.utcnow())
    report.add("traffic_counts", total)
    log_fn(f"traffic: {total} rows")


def _runs(days: list[date], max_len: int) -> list[tuple[date, date]]:
    """Group sorted dates into contiguous runs of at most max_len days."""
    out: list[tuple[date, date]] = []
    for d in sorted(days):
        if out and d == out[-1][1] + timedelta(days=1) and (d - out[-1][0]).days < max_len:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


# -- device telemetry --------------------------------------------------------------------------


def _telemetry(cfg, opts, client, report, budget_left, log_fn):
    with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
        types = db.device_types(con)
        if opts.devices:
            names = [d for d in opts.devices if d in types]
            missing = sorted(set(opts.devices) - set(names))
            if missing:
                report.errors.append(f"unknown devices skipped: {', '.join(missing)}")
        else:
            names = [r[0] for r in con.execute(
                "SELECT name FROM devices WHERE in_inventory AND list_contains(?, type) "
                "ORDER BY type, name", [list(opts.types)]).fetchall()]
        progress = {
            r[0]: (r[1], r[2], r[3])
            for r in con.execute(
                "SELECT device, oldest_ms, target_ms, done FROM backfill_progress").fetchall()
        }
    since_ms, until_ms = to_ms(opts.since), to_ms(opts.until)
    work: list[tuple[str, tuple[str, ...], int]] = []
    for name in names:
        keys = interpret.READING_KEYS.get(types[name])
        if not keys:
            continue
        end = until_ms
        if name in progress and not opts.reset:
            oldest, target, done = progress[name]
            if done and target is not None and target <= since_ms:
                report.devices_done += 1
                continue
            if oldest is not None:
                end = oldest  # inclusive: re-reads the boundary timestamp, duplicates dropped
        if end > since_ms:
            work.append((name, keys, end))
    log_fn(f"telemetry: {len(work)} devices to page back to {opts.since:%Y-%m-%d}")
    if not work:
        return

    out: queue.Queue = queue.Queue()
    stop = threading.Event()

    def over_budget() -> bool:
        return stop.is_set() or budget_left() <= 0

    def worker(name: str, keys: tuple[str, ...], end: int) -> None:
        last_len = None
        try:
            for page in client.range_pages("DEVICE", name, keys, since_ms, end, stop=over_budget):
                last_len = len(page)
                out.put(("page", name, page))
            # Done if the last page was short (history exhausted) or paging ended on its own;
            # not if the budget stopped it, including before its first request.
            finished = (last_len is not None and last_len < cfg.range_cap) or not over_budget()
            out.put(("end", name, finished))
        except ApiError as e:
            out.put(("error", name, str(e)))
        except Exception as e:  # never leave the consumer waiting
            out.put(("error", name, f"{type(e).__name__}: {e}"))

    pool = ThreadPoolExecutor(max_workers=opts.concurrency)
    for item in work:
        pool.submit(worker, *item)
    remaining = len(work)
    buffer: list[tuple[str, str, int, str | None]] = []
    oldest: dict[str, int] = {}
    finished: dict[str, bool] = {}  # device -> reached --since (or the start of its history)
    affected: dict[tuple[str, str], int] = {}
    last_flush = time.monotonic()
    total = 0

    def flush() -> None:
        nonlocal buffer, last_flush, total
        touched = set(oldest) | set(finished)
        if not buffer and not touched:
            return
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            total += db.insert_telemetry(con, buffer, "backfill")
            db.touch_last_telemetry(con, db.newest_reading_per_device(buffer, types))
            rows = [(d, ",".join(interpret.READING_KEYS[types[d]]), oldest.get(d), since_ms,
                     finished.get(d, False)) for d in touched]
            db.bulk(
                con,
                {"device": "VARCHAR", "keys": "VARCHAR", "oldest": "BIGINT", "target": "BIGINT",
                 "done": "BOOLEAN"},
                rows,
                """INSERT INTO backfill_progress
                   SELECT device, keys, oldest, target, done, now() FROM __SRC__
                   ON CONFLICT (device, keys) DO UPDATE SET
                       oldest_ms = coalesce(least(excluded.oldest_ms, backfill_progress.oldest_ms),
                                            excluded.oldest_ms, backfill_progress.oldest_ms),
                       target_ms = excluded.target_ms, done = excluded.done,
                       updated_at = excluded.updated_at""",
            )
        for pair, ms in db.tracked_pairs(buffer, types).items():
            affected[pair] = min(ms, affected.get(pair, ms))
        buffer = []
        last_flush = time.monotonic()

    try:
        while remaining:
            try:
                kind, name, payload = out.get(timeout=1.0)
            except queue.Empty:
                kind = None
            if kind == "page":
                buffer.extend(parse.telemetry_rows(name, payload))
                if payload:
                    page_oldest = min(int(r["timestamp"]) for r in payload)
                    oldest[name] = min(page_oldest, oldest.get(name, page_oldest))
            elif kind == "end":
                remaining -= 1
                finished[name] = payload
                if payload:
                    report.devices_done += 1
                else:
                    report.devices_pending += 1
            elif kind == "error":
                remaining -= 1
                report.devices_pending += 1
                report.errors.append(f"{name}: {payload}")
            if len(buffer) >= opts.flush_rows or time.monotonic() - last_flush > opts.flush_seconds:
                flush()
                log_fn(f"telemetry: {total} new readings, {client.stats.requests} requests, "
                       f"{remaining} devices left")
    except KeyboardInterrupt:
        stop.set()
        log_fn("interrupted: saving progress")
        raise
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        while not out.empty():  # pages that arrived after the loop ended
            kind, name, payload = out.get_nowait()
            if kind == "page":
                buffer.extend(parse.telemetry_rows(name, payload))
                if payload:
                    page_oldest = min(int(r["timestamp"]) for r in payload)
                    oldest[name] = min(page_oldest, oldest.get(name, page_oldest))
            elif kind == "end":
                finished[name] = payload
        flush()
        if affected:
            log_fn(f"telemetry: deriving state changes for {len(affected)} series")
            with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
                report.add("state_changes", db.derive_state_changes(con, affected))
        report.add("telemetry", total)
        log_fn(f"telemetry: {total} new readings")

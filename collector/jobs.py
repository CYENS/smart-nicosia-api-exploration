"""Polling jobs. Each job reads what it needs from the DB, fetches from the API with no DB
connection open, then stores the results. That keeps the DuckDB file free between jobs."""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from collector import db, interpret, parse
from collector.config import Config
from collector.http import ApiError, Client

log = logging.getLogger(__name__)

# Device counts for tenant CYTA on 19 Oct 2026, used for estimates before the inventory exists.
KNOWN_COUNTS = {
    "Parking": 303,
    "Lighting": 240,
    "Wastebin": 139,
    "Camera": 31,
    "InfoPoints": 15,
    "smartsense": 7,
    "ParkingLot": 5,
}


@dataclass
class Context:
    cfg: Config
    now: datetime  # aware UTC

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.timezone)

    @property
    def now_ms(self) -> int:
        return to_ms(self.now)

    @property
    def today(self) -> date:
        return self.now.astimezone(self.tz).date()


@dataclass
class Fetched:
    data: Any = None
    errors: list[ApiError] = field(default_factory=list)


@dataclass
class Stored:
    rows: int = 0
    message: str = ""


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_each(
    client: Client, names: Iterable[str], fn: Callable[[str], Any], concurrency: int = 1
) -> Fetched:
    """Call fn(name) for every name; collect results by name and errors, never raising."""
    results: dict[str, Any] = {}
    errors: list[ApiError] = []

    def one(name: str):
        try:
            return name, fn(name), None
        except ApiError as e:
            return name, None, e

    names = list(names)
    if concurrency <= 1:
        outcomes = map(one, names)
    else:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        outcomes = pool.map(one, names)
    try:
        for name, value, error in outcomes:
            if error is not None:
                errors.append(error)
            else:
                results[name] = value
    finally:
        if concurrency > 1:
            pool.shutdown()
    return Fetched(results, errors)


class Job:
    name: str = ""
    nokia: bool = True  # counts against the Smart Nicosia request budget

    def prepare(self, con: duckdb.DuckDBPyConnection, ctx: Context) -> Any:
        return None

    def fetch(self, client: Client, plan: Any, ctx: Context) -> Fetched:
        raise NotImplementedError

    def store(self, con: duckdb.DuckDBPyConnection, fetched: Fetched, ctx: Context) -> Stored:
        raise NotImplementedError

    def requests_per_run(self, counts: dict[str, int], cfg: Config) -> float:
        return 1.0


# -- inventory ---------------------------------------------------------------------------------


class DevicesJob(Job):
    name = "devices"

    def fetch(self, client, plan, ctx):
        return Fetched(
            client.get_json("getTenantDevices", entityType="TENANT", entityName=ctx.cfg.tenant)
        )

    def store(self, con, fetched, ctx):
        devices = parse.parse_devices(fetched.data or [])
        if not devices:
            raise ValueError("getTenantDevices returned no devices; inventory left unchanged")
        now_us = int(ctx.now.timestamp() * 1_000_000)
        db.bulk(
            con,
            {"name": "VARCHAR", "type": "VARCHAR", "lat": "DOUBLE", "lon": "DOUBLE",
             "label": "VARCHAR"},
            [(d["name"], d["type"], d["lat"], d["lon"], d["label"]) for d in devices],
            f"""INSERT INTO devices (name, type, lat, lon, label, in_inventory, first_seen,
                                     inventory_seen)
                SELECT name, type, lat, lon, label, true, make_timestamptz({now_us}),
                       make_timestamptz({now_us})
                FROM __SRC__
                ON CONFLICT (name) DO UPDATE SET
                    type = excluded.type, lat = excluded.lat, lon = excluded.lon,
                    label = excluded.label, in_inventory = true,
                    inventory_seen = excluded.inventory_seen""",
        )
        con.execute(
            "UPDATE devices SET in_inventory = coalesce(inventory_seen = make_timestamptz(?), false)",
            [now_us],
        )
        by_type: dict[str, int] = {}
        for d in devices:
            by_type[d["type"]] = by_type.get(d["type"], 0) + 1
        summary = ", ".join(f"{n} {t}" for t, n in sorted(by_type.items(), key=lambda x: -x[1]))
        return Stored(len(devices), f"{len(devices)} devices: {summary}")


class AttributesJob(Job):
    name = "attributes"

    def prepare(self, con, ctx):
        return [r[0] for r in con.execute(
            "SELECT name FROM devices WHERE in_inventory ORDER BY name").fetchall()]

    def fetch(self, client, names, ctx):
        return fetch_each(client, names, client.latest_attributes, ctx.cfg.concurrency)

    def store(self, con, fetched, ctx):
        attr_rows = []
        device_rows = []
        for device, records in fetched.data.items():
            for r in records:
                attr_rows.append((device, r["key"], int(r["timestamp"]), r.get("value")))
            latest = interpret.latest_from_records(records)

            def val(key):
                v = latest.get(key, (None, None))[1]
                return None if v in (None, "None") else v

            timeout = interpret.to_number(val("inactivityTimeout"))
            activity = interpret.to_number(val("lastActivityTime"))
            device_rows.append((
                device, val("name"), val("postalcode"), val("metadata"),
                int(timeout) if timeout else None, int(activity) if activity else None,
            ))
        db.bulk(
            con,
            {"device": "VARCHAR", "key": "VARCHAR", "ms": "BIGINT", "value": "VARCHAR"},
            attr_rows,
            """INSERT OR REPLACE INTO attributes
               SELECT device, key, make_timestamptz(ms * 1000), value FROM __SRC__""",
        )
        now_us = int(ctx.now.timestamp() * 1_000_000)
        db.bulk(
            con,
            {"device": "VARCHAR", "display_name": "VARCHAR", "postal_code": "VARCHAR",
             "metadata": "VARCHAR", "timeout_ms": "BIGINT", "activity_ms": "BIGINT"},
            device_rows,
            f"""UPDATE devices SET
                    display_name = s.display_name, postal_code = s.postal_code,
                    metadata = s.metadata, inactivity_timeout_ms = s.timeout_ms,
                    last_activity = CASE WHEN s.activity_ms IS NULL THEN NULL
                                         ELSE make_timestamptz(s.activity_ms * 1000) END,
                    attributes_updated = make_timestamptz({now_us})
                FROM __SRC__ s WHERE devices.name = s.device""",
        )
        return Stored(len(attr_rows), f"attributes for {len(device_rows)} devices")

    def requests_per_run(self, counts, cfg):
        return float(sum(counts.values()))


# -- per-device latest telemetry ---------------------------------------------------------------


class LatestJob(Job):
    """getLatestTelemetry with a keys= filter for every device of one type."""

    def __init__(self, name: str, device_type: str, keys: tuple[str, ...] | None = None):
        self.name = name
        self.device_type = device_type
        self.keys = keys or interpret.READING_KEYS[device_type]

    def prepare(self, con, ctx):
        return {"names": db.devices_of_type(con, self.device_type)}

    def fetch(self, client, plan, ctx):
        return fetch_each(
            client,
            plan["names"],
            lambda n: client.latest_telemetry(n, self.keys),
            ctx.cfg.concurrency,
        )

    def store(self, con, fetched, ctx):
        rows = []
        for device, records in fetched.data.items():
            rows.extend(parse.telemetry_rows(device, records))
        types = {d: self.device_type for d in fetched.data}
        written = db.store_telemetry(con, rows, "latest", types)
        return Stored(written, f"{len(fetched.data)} devices polled, {written} new readings")

    def requests_per_run(self, counts, cfg):
        return float(counts.get(self.device_type, 0))


# -- TENANT feeds ------------------------------------------------------------------------------


class StreamJob(Job):
    """Device messages mirrored on the TENANT entity: status, level, state per message.

    One request returns up to 1,000 records (about 20 minutes of the whole city), so this records
    every parking, lamp and bin change for a few hundred requests a day."""

    name = "stream"
    overlap = timedelta(minutes=10)
    first_lookback = timedelta(minutes=30)

    def prepare(self, con, ctx):
        return {
            "watermark": db.get_watermark(con, self.name),
            "types": db.device_types(con),
        }

    def fetch(self, client, plan, ctx):
        cfg = ctx.cfg
        wm = plan["watermark"]
        floor = ctx.now_ms - int(cfg.stream_max_lookback_hours * 3_600_000)
        start = max(wm - int(self.overlap.total_seconds() * 1000), floor) if wm else (
            ctx.now_ms - int(self.first_lookback.total_seconds() * 1000))
        records: list[dict] = []
        pages = 0
        last_len = 0
        for page in client.range_pages(
            "TENANT", cfg.tenant, cfg.stream_keys, start, ctx.now_ms,
            max_pages=cfg.stream_max_pages,
        ):
            pages += 1
            last_len = len(page)
            records.extend(page)
        truncated = pages >= cfg.stream_max_pages and last_len >= cfg.range_cap
        return Fetched({"records": records, "pages": pages, "truncated": truncated, **plan})

    def store(self, con, fetched, ctx):
        d = fetched.data
        types = d["types"]
        parsed = parse.parse_stream(d["records"], set(types) if types else None)
        written = db.store_telemetry(con, parsed.rows, "stream", types)
        if parsed.newest_ts:
            db.set_watermark(con, self.name, max(parsed.newest_ts, d["watermark"] or 0))
        msg = (f"{d['pages']} pages, {parsed.messages} messages, {written} new readings, "
               f"{parsed.ambiguous} ambiguous, {parsed.unknown_devices} from unknown devices")
        if d["truncated"]:
            msg += "; GAP: hit stream_max_pages before reaching the previous watermark"
        return Stored(written, msg)

    def requests_per_run(self, counts, cfg):
        return 1.3  # one page usually; two when a poll window spans >1,000 records


class EventsJob(Job):
    """The city's running alarm log: TENANT toastDesc ("YW1337: Fill level above ... 80 %")."""

    name = "events"
    overlap = timedelta(minutes=30)
    first_lookback = timedelta(days=1)

    def prepare(self, con, ctx):
        return db.get_watermark(con, self.name)

    def fetch(self, client, wm, ctx):
        start = (wm - int(self.overlap.total_seconds() * 1000)) if wm else (
            ctx.now_ms - int(self.first_lookback.total_seconds() * 1000))
        records: list[dict] = []
        for page in client.range_pages(
            "TENANT", ctx.cfg.tenant, ("toastDesc",), start, ctx.now_ms,
            max_pages=ctx.cfg.events_max_pages,
        ):
            records.extend(page)
        return Fetched({"records": records, "watermark": wm})

    def store(self, con, fetched, ctx):
        written = store_events(con, parse.parse_events(fetched.data["records"]))
        newest = max((int(r["timestamp"]) for r in fetched.data["records"]), default=None)
        if newest:
            db.set_watermark(con, self.name, max(newest, fetched.data["watermark"] or 0))
        return Stored(written, f"{len(fetched.data['records'])} events fetched, {written} new")


def store_events(con, events: list[tuple[int, str | None, str]]) -> int:
    return db.bulk(
        con,
        {"ms": "BIGINT", "device": "VARCHAR", "message": "VARCHAR"},
        events,
        """INSERT OR IGNORE INTO events
           SELECT make_timestamptz(ms * 1000), device, message FROM __SRC__""",
    )


class AlarmsJob(Job):
    name = "alarms"

    def fetch(self, client, plan, ctx):
        cfg = ctx.cfg
        return fetch_each(
            client,
            cfg.alarm_states,
            lambda state: client.get_json(
                "getAlarms", entityType="TENANT", entityName=cfg.tenant,
                limit=cfg.alarm_limit, state=state,
            ),
        )

    def store(self, con, fetched, ctx):
        alarms = [parse.parse_alarm(a) for payload in fetched.data.values() for a in payload]
        now_us = int(ctx.now.timestamp() * 1_000_000)

        def ts(col):
            return f"CASE WHEN {col} IS NULL THEN NULL ELSE make_timestamptz({col} * 1000) END"

        cols = ["id", "device", "device_type", "type", "severity", "status", "start_ms",
                "end_ms", "created_ms", "ack_ms", "clear_ms", "additional_info"]
        types = {c: ("BIGINT" if c.endswith("_ms") else "VARCHAR") for c in cols}
        written = db.bulk(
            con,
            types,
            [tuple(a[c] for c in cols) for a in alarms],
            f"""INSERT INTO alarms
                SELECT id, device, device_type, type, severity, status, {ts('start_ms')},
                       {ts('end_ms')}, {ts('created_ms')}, {ts('ack_ms')}, {ts('clear_ms')},
                       additional_info, make_timestamptz({now_us}), make_timestamptz({now_us})
                FROM __SRC__
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status, severity = excluded.severity,
                    end_ts = excluded.end_ts, ack_ts = excluded.ack_ts,
                    clear_ts = excluded.clear_ts, last_seen = excluded.last_seen""",
        )
        capped = [s for s, p in fetched.data.items() if len(p) >= ctx.cfg.alarm_limit]
        msg = f"{len(alarms)} alarms seen"
        if capped:
            msg += f"; {', '.join(capped)} capped at {ctx.cfg.alarm_limit} (API has no paging)"
        return Stored(written, msg)

    def requests_per_run(self, counts, cfg):
        return float(len(cfg.alarm_states))


# -- national Bluetooth travel times -----------------------------------------------------------


class TravelTimesJob(Job):
    name = "travel_times"
    nokia = False

    def fetch(self, client, plan, ctx):
        return Fetched(client.get_text(ctx.cfg.nap_url, "PredefinedLocationDataPublication"))

    def store(self, con, fetched, ctx):
        rows = parse.parse_nap(fetched.data)
        written = store_travel_times(con, rows)
        measured = sum(r.travel_time_s is not None for r in rows)
        return Stored(written, f"{len(rows)} segments ({measured} with data), {written} new")


def store_travel_times(con, rows: list[parse.TravelTimeRow]) -> int:
    return db.bulk(
        con,
        {"segment_id": "VARCHAR", "segment_name": "VARCHAR", "from_site": "VARCHAR",
         "to_site": "VARCHAR", "measured": "BIGINT", "travel_time_s": "DOUBLE",
         "speed_kmh": "DOUBLE", "published": "BIGINT"},
        [(r.segment_id, r.segment_name, r.from_site, r.to_site, r.measured_at, r.travel_time_s,
          r.speed_kmh, r.publication_time) for r in rows],
        """INSERT OR IGNORE INTO travel_times
           SELECT segment_id, segment_name, from_site, to_site, make_timestamptz(measured),
                  travel_time_s, speed_kmh,
                  CASE WHEN published IS NULL THEN NULL ELSE make_timestamptz(published) END
           FROM __SRC__""",
    )


# -- traffic counts and city speed -------------------------------------------------------------


def traffic_request(client: Client, group_by: str, first: date, last: date, tz: ZoneInfo,
                    end: datetime | None = None, va_ids: str = "[]") -> Any:
    """getTrafficReports for local days first..last (inclusive), or up to `end`."""
    start = parse.local_midnight(first, tz)
    stop = end or parse.local_midnight(last + timedelta(days=1), tz) - timedelta(milliseconds=1)
    return client.get_json(
        "getTrafficReports", va_ids=va_ids, group_by=group_by,
        start_date=to_ms(start), end_date=to_ms(stop),
    )


def store_traffic(con, rows: list[parse.TrafficRow], now: datetime) -> int:
    now_us = int(now.timestamp() * 1_000_000)
    return db.bulk(
        con,
        {"granularity": "VARCHAR", "bucket": "BIGINT", "analytics_id": "INTEGER",
         "line_name": "VARCHAR", "object_type": "INTEGER", "dir1": "INTEGER", "dir2": "INTEGER",
         "total": "INTEGER"},
        [(r.granularity, r.bucket_start, r.analytics_id, r.line_name, r.object_type, r.dir1,
          r.dir2, r.total) for r in rows],
        f"""INSERT OR REPLACE INTO traffic_counts
            SELECT granularity, make_timestamptz(bucket), analytics_id, line_name, object_type,
                   dir1, dir2, total, make_timestamptz({now_us})
            FROM __SRC__""",
    )


class TrafficDailyJob(Job):
    """Per-camera, per-counting-line daily counts (group_by=stream) for finished local days."""

    name = "traffic_daily"

    def prepare(self, con, ctx):
        wm = db.get_watermark(con, self.name)  # ordinal of the last stored local day
        yesterday = ctx.today - timedelta(days=1)
        first = (date.fromordinal(wm) + timedelta(days=1)) if wm else (
            yesterday - timedelta(days=ctx.cfg.traffic_initial_days - 1))
        max_days = min(ctx.cfg.traffic_max_days_per_request, 30)
        return parse.day_range(first, yesterday, max_days) if first <= yesterday else []

    def fetch(self, client, chunks, ctx):
        done = []
        for first, last in chunks:
            try:
                done.append((first, last, traffic_request(client, "stream", first, last, ctx.tz)))
            except ApiError as e:
                return Fetched(done, [e])  # keep finished chunks; retry the rest next time
        return Fetched(done)

    def store(self, con, fetched, ctx):
        rows = []
        for first, last, payload in fetched.data:
            rows.extend(parse.parse_traffic_stream(payload, ctx.tz))
        written = store_traffic(con, rows, ctx.now)
        if fetched.data:
            db.set_watermark(con, self.name, fetched.data[-1][1].toordinal())
            span = f"{fetched.data[0][0]}..{fetched.data[-1][1]}"
        else:
            span = "nothing due"
        return Stored(written, f"days {span}: {written} rows")


class TrafficHourlyJob(Job):
    """City-wide hourly counts for today (and yesterday, until it has been fetched complete)."""

    name = "traffic_hourly"

    def prepare(self, con, ctx):
        return db.get_watermark(con, self.name)

    def fetch(self, client, wm, ctx):
        today = ctx.today
        days = [(today, ctx.now)]
        midnight = parse.local_midnight(today, ctx.tz)
        if wm is None or wm < to_ms(midnight):
            days.insert(0, (today - timedelta(days=1), None))
        out = []
        for day, end in days:
            try:
                out.append((day, traffic_request(client, "hour", day, day, ctx.tz, end=end)))
            except ApiError as e:
                return Fetched(out, [e])
        return Fetched(out)

    def store(self, con, fetched, ctx):
        rows = []
        for day, payload in fetched.data:
            rows.extend(parse.parse_traffic_buckets(payload, ctx.tz, day=day))
        written = store_traffic(con, rows, ctx.now)
        if fetched.data and not fetched.errors:
            db.set_watermark(con, self.name, ctx.now_ms)
        return Stored(written, f"{len(fetched.data)} day(s), {written} hourly rows")

    def requests_per_run(self, counts, cfg):
        return 1 + 1 / 24


def store_speeds(con, rows: list[parse.SpeedRow], granularity: str, line_type: str,
                 now: datetime) -> int:
    now_us = int(now.timestamp() * 1_000_000)
    return db.bulk(
        con,
        {"bucket": "BIGINT", "sample": "BIGINT", "label": "VARCHAR", "value": "DOUBLE"},
        [(r.bucket_start, r.sample_time, r.label, r.value) for r in rows],
        f"""INSERT OR REPLACE INTO speeds
            SELECT '{granularity}', '{line_type}', make_timestamptz(bucket),
                   CASE WHEN sample IS NULL THEN NULL ELSE make_timestamptz(sample) END,
                   label, value, make_timestamptz({now_us})
            FROM __SRC__""",
    )


class SpeedJob(Job):
    """City-wide average speed (generalReports), five-minute buckets."""

    name = "speed"

    def prepare(self, con, ctx):
        return db.get_watermark(con, self.name)

    def fetch(self, client, wm, ctx):
        cfg = ctx.cfg
        floor = ctx.now_ms - int(cfg.speed_max_catchup_days * 86_400_000)
        start = max(wm - 3_600_000, floor) if wm else ctx.now_ms - 3 * 3_600_000
        return Fetched(client.get_json(
            "generalReports", group_by=cfg.speed_group_by, line_type=cfg.speed_line_type,
            start_date=start, end_date=ctx.now_ms,
        ))

    def store(self, con, fetched, ctx):
        rows = parse.parse_speed(fetched.data or [], ctx.tz)
        written = store_speeds(con, rows, ctx.cfg.speed_group_by, ctx.cfg.speed_line_type, ctx.now)
        if rows:
            db.set_watermark(con, self.name, max(to_ms(r.bucket_start) for r in rows))
        return Stored(written, f"{len(rows)} buckets")


# -- camera mapping ----------------------------------------------------------------------------

CAMERA_CSV_FIELDS = ["analytics_id", "name", "stream_name", "status", "line_names", "lat", "lon",
                     "note"]


def read_camera_csv(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {int(r["analytics_id"]): r for r in csv.DictReader(f)}


def write_camera_csv(path: Path, rows: dict[int, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMERA_CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for key in sorted(rows):
            w.writerow({k: rows[key].get(k, "") for k in CAMERA_CSV_FIELDS})


def map_cameras(
    feeds: list[parse.CameraFeed],
    cameras: dict[str, tuple[float | None, float | None]],
    manual: dict[int, dict],
) -> tuple[list[dict], dict[int, dict]]:
    """Link analytics feeds to Camera devices by exact (case-sensitive) name.

    Returns table rows and the CSV rows to keep for hand geocoding: every existing CSV row plus
    any unmatched feed not yet in it (with blank lat/lon)."""
    rows = []
    csv_rows = dict(manual)
    for f in feeds:
        device = next((n for n in (f.name, f.stream_name) if n in cameras), None)
        lat = lon = None
        source = None
        if device:
            lat, lon = cameras[device]
            source = "device"
        else:
            entry = csv_rows.setdefault(f.analytics_id, {
                "analytics_id": f.analytics_id, "name": f.name, "stream_name": f.stream_name,
                "status": f.status, "line_names": " | ".join(f.line_names), "lat": "",
                "lon": "", "note": "",
            })
            lat = interpret.to_number(entry.get("lat"))
            lon = interpret.to_number(entry.get("lon"))
            source = "manual" if lat is not None and lon is not None else None
        rows.append({
            "analytics_id": f.analytics_id, "name": f.name, "status": f.status,
            "stream_id": f.stream_id, "stream_name": f.stream_name, "lines": f.lines_json,
            "device_name": device, "lat": lat, "lon": lon, "location_source": source,
        })
    return rows, csv_rows


class CamerasMapJob(Job):
    """Traffic-analytics feeds -> Camera devices (exact name) or hand coordinates (CSV)."""

    name = "cameras_map"

    def prepare(self, con, ctx):
        return {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT name, lat, lon FROM devices WHERE type = 'Camera'").fetchall()}

    def fetch(self, client, cameras, ctx):
        return Fetched({
            "analytics": client.get_json("analytics"),
            "object_types": client.get_json("object_types"),
            "cameras": cameras,
        })

    def store(self, con, fetched, ctx):
        d = fetched.data
        db.bulk(
            con, {"id": "INTEGER", "label": "VARCHAR"},
            [(int(k), v) for k, v in (d["object_types"] or {}).items()],
            "INSERT OR REPLACE INTO object_types SELECT id, label FROM __SRC__",
        )
        feeds = parse.parse_analytics(d["analytics"])
        csv_path = ctx.cfg.camera_csv
        before = read_camera_csv(csv_path)
        rows, csv_rows = map_cameras(feeds, d["cameras"], before)
        if set(csv_rows) != set(before):
            write_camera_csv(csv_path, csv_rows)
        now_us = int(ctx.now.timestamp() * 1_000_000)
        cols = ["analytics_id", "name", "status", "stream_id", "stream_name", "lines",
                "device_name", "lat", "lon", "location_source"]
        types = {"analytics_id": "INTEGER", "stream_id": "INTEGER", "lat": "DOUBLE",
                 "lon": "DOUBLE"}
        db.bulk(
            con, {c: types.get(c, "VARCHAR") for c in cols},
            [tuple(r[c] for c in cols) for r in rows],
            f"""INSERT OR REPLACE INTO traffic_cameras
                SELECT *, make_timestamptz({now_us}) FROM __SRC__""",
        )
        matched = sum(r["location_source"] == "device" for r in rows)
        manual = sum(r["location_source"] == "manual" for r in rows)
        return Stored(len(rows), f"{len(rows)} feeds: {matched} matched to Camera devices, "
                      f"{manual} hand-geocoded, {len(rows) - matched - manual} without location "
                      f"(edit {csv_path})")

    def requests_per_run(self, counts, cfg):
        return 2.0


# -- registry ----------------------------------------------------------------------------------


def build_jobs() -> dict[str, Job]:
    jobs: list[Job] = [
        DevicesJob(),
        AttributesJob(),
        CamerasMapJob(),
        StreamJob(),
        EventsJob(),
        AlarmsJob(),
        TravelTimesJob(),
        LatestJob("parking", "Parking"),
        LatestJob("lamps", "Lighting"),
        LatestJob("bins", "Wastebin"),
        LatestJob("infopoints", "InfoPoints"),
        LatestJob("air", "smartsense"),
        LatestJob("cameras", "Camera"),
        TrafficDailyJob(),
        TrafficHourlyJob(),
        SpeedJob(),
    ]
    return {j.name: j for j in jobs}


JOBS = build_jobs()

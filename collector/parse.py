"""Pure functions that turn API payloads into table rows. No I/O, so they're easy to test."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector.interpret import to_number

ALL_CAMERAS = 0  # analytics_id used for city-wide traffic totals


def _float(value) -> float | None:
    return to_number(value)


# -- inventory ---------------------------------------------------------------------------------


def parse_devices(payload: Iterable[Mapping]) -> list[dict]:
    out = []
    for d in payload:
        kv = {k["key"]: k["value"] for k in d.get("device_key_type") or []}
        out.append(
            {
                "name": d["device_name"],
                "type": d["device_type"],
                "lat": _float(kv.get("latitude")),
                "lon": _float(kv.get("longitude")),
                "label": d.get("device_label"),
            }
        )
    return out


def telemetry_rows(device: str, records: Iterable[Mapping]) -> list[tuple[str, str, int, str | None]]:
    """[{key, timestamp, value}] -> [(device, key, ts_ms, value)]."""
    return [(device, r["key"], int(r["timestamp"]), r.get("value")) for r in records]


# -- TENANT stream -----------------------------------------------------------------------------


@dataclass
class StreamParse:
    rows: list[tuple[str, str, int, str | None]]
    messages: int  # timestamps with exactly one deviceName
    ambiguous: int  # timestamps shared by several devices: skipped
    unknown_devices: int  # messages from devices outside the inventory: skipped
    newest_ts: int | None


def parse_stream(records: Iterable[Mapping], known_devices: set[str] | None = None) -> StreamParse:
    """Rebuild device telemetry from the TENANT time series.

    The TENANT entity mirrors every device message: at the message's timestamp it stores
    `deviceName` plus that message's keys (status, level, state, ...), with null for keys the
    message lacked. Records are joined on timestamp; a timestamp claimed by two devices can't be
    attributed and is skipped.
    """
    by_ts: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    newest = None
    for r in records:
        ts = int(r["timestamp"])
        newest = ts if newest is None or ts > newest else newest
        by_ts[ts][r["key"]].append(r.get("value"))
    rows: list[tuple[str, str, int, str | None]] = []
    messages = ambiguous = unknown = 0
    for ts, keys in by_ts.items():
        names = {n for n in keys.pop("deviceName", []) if n}
        if len(names) != 1:
            ambiguous += len(names) > 1
            continue
        device = names.pop()
        if known_devices is not None and device not in known_devices:
            unknown += 1
            continue
        messages += 1
        for key, values in keys.items():
            vals = {v for v in values if v not in (None, "None")}
            if len(vals) == 1:
                rows.append((device, key, ts, vals.pop()))
    return StreamParse(rows, messages, ambiguous, unknown, newest)


def parse_events(records: Iterable[Mapping]) -> list[tuple[int, str | None, str]]:
    """toastDesc records "YW1337: Fill level above threshold of 80 %" -> (ts, device, message)."""
    out = []
    for r in records:
        text = r.get("value") or ""
        device, sep, rest = text.partition(": ")
        out.append((int(r["timestamp"]), device if sep else None, text))
    return out


# -- alarms ------------------------------------------------------------------------------------


def parse_alarm(a: Mapping) -> dict:
    info: dict = {}
    try:
        info = json.loads(a.get("additional_info") or "{}") or {}
    except ValueError:
        pass

    def ms(key):
        v = int(a.get(key) or 0)
        return v or None

    return {
        "id": a["id"],
        "device": info.get("devicename"),
        "device_type": info.get("devicetype"),
        "type": a.get("type"),
        "severity": a.get("severity"),
        "status": a.get("status"),
        "start_ms": ms("start_ts"),
        "end_ms": ms("end_ts"),
        "created_ms": ms("created_time"),
        "ack_ms": ms("ack_ts"),
        "clear_ms": ms("clear_ts"),
        "additional_info": a.get("additional_info"),
    }


# -- traffic -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrafficRow:
    granularity: str  # "day" or "hour"
    bucket_start: datetime  # aware, start of the local day/hour
    analytics_id: int  # ALL_CAMERAS (0) for city-wide totals
    line_name: str  # "" for city-wide totals
    object_type: int
    dir1: int | None
    dir2: int | None
    total: int | None


def _counts(c: Mapping) -> tuple[int | None, int | None, int | None]:
    def g(k):
        v = c.get(k)
        return int(v) if v is not None else None

    return g("1"), g("2"), g("total")


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def parse_traffic_stream(payload: Mapping, tz: ZoneInfo) -> list[TrafficRow]:
    """group_by=stream: {data: {group_by: {"DD-MM-YYYY": {analytics_id: {line: {type: counts}}}}}}."""
    rows = []
    for day_key, cams in (payload.get("data", {}).get("group_by") or {}).items():
        day = datetime.strptime(day_key, "%d-%m-%Y").date()
        start = local_midnight(day, tz)
        for va_id, lines in cams.items():
            for line_name, types in lines.items():
                for obj_type, counts in types.items():
                    rows.append(
                        TrafficRow("day", start, int(va_id), line_name, int(obj_type), *_counts(counts))
                    )
    return rows


def parse_traffic_buckets(
    payload: Mapping,
    tz: ZoneInfo,
    *,
    day: date | None = None,
    analytics_id: int = ALL_CAMERAS,
) -> list[TrafficRow]:
    """group_by=hour or day: {data: {group_by: [{key: "HH:MM" | "DD-MM-YYYY", data: {type: counts}}]}}.

    Hour keys carry no date and are local time, so `day` (the local date queried) is required for
    them, and an hourly query must not cross local midnight.
    """
    rows = []
    for bucket in payload.get("data", {}).get("group_by") or []:
        key = bucket["key"]
        if re.fullmatch(r"\d{2}:\d{2}", key):
            if day is None:
                raise ValueError("hourly traffic buckets need the local date they belong to")
            hh, mm = map(int, key.split(":"))
            start = local_midnight(day, tz).replace(hour=hh, minute=mm)
            granularity = "hour"
        else:
            start = local_midnight(datetime.strptime(key, "%d-%m-%Y").date(), tz)
            granularity = "day"
        for obj_type, counts in (bucket.get("data") or {}).items():
            rows.append(
                TrafficRow(granularity, start, analytics_id, "", int(obj_type), *_counts(counts))
            )
    return rows


# -- city speed --------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeedRow:
    bucket_start: datetime
    sample_time: datetime | None
    label: str
    value: float | None


def parse_speed(payload: Iterable[Mapping], tz: ZoneInfo) -> list[SpeedRow]:
    """generalReports [{label: "10/19/2026 - 11:25", time: ms, value: "43.0"}]; labels are local."""
    rows = []
    for r in payload:
        label = r["label"]
        for fmt in ("%m/%d/%Y - %H:%M", "%m/%d/%Y"):
            try:
                start = datetime.strptime(label, fmt).replace(tzinfo=tz)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unrecognised generalReports label {label!r}")
        sample = (
            datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc) if r.get("time") else None
        )
        rows.append(SpeedRow(start, sample, label, _float(r.get("value"))))
    return rows


# -- national Bluetooth travel times (DATEX II) ------------------------------------------------


@dataclass(frozen=True)
class TravelTimeRow:
    segment_id: str
    segment_name: str | None
    from_site: str | None
    to_site: str | None
    measured_at: datetime
    travel_time_s: float | None
    speed_kmh: float | None
    publication_time: datetime | None


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip().replace("Z", "+00:00")
    # .NET writes 7 fractional digits; Python accepts at most 6.
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    return datetime.fromisoformat(text)


def parse_nap(xml_text: str) -> list[TravelTimeRow]:
    """PredefinedLocationDataPublication: one record per segment, -1 meaning no measurement."""
    root = ET.fromstring(xml_text)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    publication = None
    for el in root:
        if local(el.tag) == "publicationTime":
            publication = _parse_iso(el.text)
    rows = []
    for ref in root.iter():
        if local(ref.tag) != "predefinedLocationReference":
            continue
        fields: dict[str, str | None] = {}
        for el in ref.iter():
            name = local(el.tag)
            if name in ("value", "obs_t_time", "obs_speed", "measurement_timestamp"):
                fields.setdefault(name, el.text)
        measured = _parse_iso(fields.get("measurement_timestamp"))
        if measured is None:
            continue
        name = fields.get("value")
        src, _, dst = (name or "").partition("->")
        t = _float(fields.get("obs_t_time"))
        v = _float(fields.get("obs_speed"))
        rows.append(
            TravelTimeRow(
                segment_id=ref.get("id"),
                segment_name=name,
                from_site=src or None,
                to_site=dst or None,
                measured_at=measured,
                travel_time_s=None if t is None or t < 0 else t,
                speed_kmh=None if v is None or v < 0 else v,
                publication_time=publication,
            )
        )
    return rows


# -- cameras -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraFeed:
    analytics_id: int
    name: str
    status: str | None
    stream_id: int | None
    stream_name: str | None
    line_names: tuple[str, ...]
    lines_json: str


def parse_analytics(payload: Mapping) -> list[CameraFeed]:
    feeds = []
    for a in payload.get("analytics") or []:
        stream = a.get("stream") or {}
        lines = (a.get("module") or {}).get("lines") or []
        slim_lines = [
            {k: line.get(k) for k in ("id", "name", "direction", "count_type", "types")}
            for line in lines
        ]
        feeds.append(
            CameraFeed(
                analytics_id=int(a["id"]),
                name=a.get("name"),
                status=a.get("status"),
                stream_id=stream.get("id"),
                stream_name=stream.get("name"),
                line_names=tuple(line.get("name") for line in lines),
                lines_json=json.dumps(slim_lines, ensure_ascii=False),
            )
        )
    return feeds


def day_range(first: date, last: date, max_days: int) -> list[tuple[date, date]]:
    """Split [first, last] (inclusive) into chunks of at most max_days days."""
    chunks = []
    start = first
    while start <= last:
        end = min(last, start + timedelta(days=max_days - 1))
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks

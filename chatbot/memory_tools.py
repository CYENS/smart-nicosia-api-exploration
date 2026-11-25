"""Chat tools over the twin's memory: `query_history` (a metric over hours, days and weeks, per
device or for a whole selection, against the typical day) and `run_sql` (read-only SQL over the
collector's tables). Also `entity_history`, the series behind /api/history/{entity_id}.

chatbot/tools.py registers TOOL_DEFS and HANDLERS. This module imports chatbot.tools only
inside functions, because tools.py imports it while it is still being built.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from collector.http import ApiError

from chatbot import history
from chatbot.city import KINDS, Device, DeviceState, label_and_street
from chatbot.context import OBJECT_TYPES
from chatbot.geo import haversine_m, loose_match
from chatbot.history import (
    AGGREGATES,
    BASELINE_WEEKS,
    BUCKETS,
    METRICS,
    TIME_BUCKETS,
    TZ,
    WEEKDAYS,
    Metric,
    NoHistory,
    Series,
)
from chatbot.memory import SqlRejected, Unavailable
from chatbot.store import STATE_COLORS, legend_for, point

DEVICE_KINDS = ["street_light", "parking_sensor", "waste_bin", "air_station"]
QUERY_KINDS = [*DEVICE_KINDS, "camera", "city_speed"]
PERIODS = ["today", "yesterday", "last_24_hours", "last_7_days", "last_14_days", "last_30_days",
           "this_week", "last_week", "this_month", "last_month"]
TYPES_BY_NAME = {v: k for k, v in OBJECT_TYPES.items()}
TRAFFIC_METRICS: dict[str, tuple[Metric, tuple[int, ...]]] = {
    name: (Metric(name, "counts", label, lambda st, v: None, across="sum", over_time="sum"), types)
    for name, label, types in (
        *((n, f"{n} counted crossing the cameras' lines", (t,)) for t, n in OBJECT_TYPES.items()),
        ("vehicles", "cars, motorcycles, buses and trucks counted", (2, 3, 4, 5)),
        ("total", "everything counted (people and vehicles)", tuple(OBJECT_TYPES)),
    )}
SPEED_METRICS = {"speed_kmh": Metric("speed_kmh", "km/h", "city-wide average traffic speed",
                                     lambda st, v: None)}
BLANK = None  # set lazily: an Interpretation for entities replayed from history
MAX_MODEL_ROWS = 60
CACHE_TTL_S = 120.0


def _tools():
    from chatbot import tools  # imported late: the tools package imports this module

    return tools


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [],
            "additionalProperties": False}


def _metric_list() -> str:
    lines = []
    for kind, metrics in METRICS.items():
        lines.append(f"{kind}: " + "; ".join(m.describe() for m in metrics.values()))
    lines.append("camera: " + "; ".join(m.describe() for m, _ in TRAFFIC_METRICS.values()))
    lines.append("city_speed: " + "; ".join(m.describe() for m in SPEED_METRICS.values()))
    return "\n".join(lines)


QUERY_HISTORY = {
    "name": "query_history",
    "description": (
        "Answer from the twin's memory, its own store of past readings: how a metric behaved "
        "over hours, days or weeks (up to 62 days back), for one device, a street, an area, or "
        "the whole city, optionally against the typical day. Use it for 'how full is parking on "
        "Makariou on weekday evenings', 'which bins were full most often this month', 'energy "
        "the lamps used last week', 'is the air worse than usual today', 'busiest hour of the "
        "week on Ledras'. Moments are interpreted by the live rules (a lamp is on only if it "
        "reported in the last 70 minutes, stale sensors count as unknown), and moments the store "
        "holds nothing for are left out, not guessed: coverage_pct says how complete the answer "
        "is. For a replay on the map of the last hours, get_history is the better fit; for "
        "anything this can't express (joins, events, alarms, road segments) use run_sql.\n"
        "Metrics per kind:\n" + _metric_list() + "\n"
        "Returns rows (a sample) and a data_ref with every row (`time` as local ISO for time "
        "buckets, `frame` = bucket index, `id` per entity), and a layer_ref of the entities "
        "with places, holding the range's value per entity and, for time buckets, a time-lapse "
        "frame per bucket that a chart of the data_ref drives. With baseline true each row also "
        "gets usual, usual_low and usual_high: the median and quartiles for that hour of that "
        "weekday (or that weekday, for days) over the stored weeks."),
    "input_schema": _obj({
        "kind": {"type": "string", "enum": QUERY_KINDS,
                 "description": "What to ask about. camera means the traffic counts, city_speed "
                                "the city-wide average speed."},
        "metric": {"type": "string",
                   "description": "One of the metrics listed for the kind, e.g. occupancy_pct."},
        "ids": {"type": "array", "maxItems": 50, "items": {"type": "string"},
                "description": "Only these devices (ids like YP974, YW1394) or cameras (names)."},
        "street": {"type": "string",
                   "description": "Street name to match. Lamps, air stations and cameras carry "
                                  "street names, bins often do; parking sensors don't, so "
                                  "those within radius_m (default 120) of the lamps, bins "
                                  "and screens named for the street are used."},
        "near_place": {"type": "string",
                       "description": "Place to search around, or 'my location'."},
        "near_lat": {"type": "number"},
        "near_lon": {"type": "number"},
        "radius_m": {"type": "number", "minimum": 20, "maximum": 10000,
                     "description": "Radius for near_place or a located street (default 500 "
                                    "for near_place)."},
        "period": {"type": "string", "enum": PERIODS,
                   "description": "The range (default last_7_days). Rolling ranges end now."},
        "from": {"type": "string",
                 "description": "Start, local time: '2026-11-02' or '2026-11-02T18:00'. Overrides "
                                "period."},
        "to": {"type": "string",
               "description": "End, local time; a bare date means the end of that day. Default "
                              "now."},
        "bucket": {"type": "string", "enum": BUCKETS,
                   "description": "hour, day, week, month: a series over time. hour_of_day, "
                                  "weekday, weekday_hour: a profile over the range (weekday_hour "
                                  "is the hour by weekday grid). total: one value per bucket "
                                  "(with per entity, a ranking). Default hour up to 2 days, "
                                  "else day."},
        "per": {"type": "string", "enum": ["all", "entity"],
                "description": "all (default): one series for the whole selection. entity: one "
                               "per device or camera, for rankings and comparisons."},
        "aggregate": {"type": "string", "enum": AGGREGATES,
                      "description": "How moments in a bucket combine. Default: sum for counts "
                                     "and energy, mean otherwise."},
        "weekdays": {"type": "array", "uniqueItems": True,
                     "items": {"type": "string", "enum": WEEKDAYS},
                     "description": "Only these days, e.g. mon..fri for weekdays."},
        "hours": _obj({"from": {"type": "integer", "minimum": 0, "maximum": 23},
                       "to": {"type": "integer", "minimum": 1, "maximum": 24}},
                      ["from", "to"]) | {
            "description": "Only these local hours, from inclusive to exclusive (17 to 21 is "
                           "17:00 to 20:59); to below from wraps past midnight."},
        "baseline": {"type": "boolean",
                     "description": "Add the typical value (usual, usual_low, usual_high) for "
                                    "'compared with usual'."},
    }, ["kind", "metric"]),
    "eager_input_streaming": True,
}

SQL_SCHEMA = """\
Tables (DuckDB SQL). Times are TIMESTAMPTZ and the session time zone is Asia/Nicosia, so \
hour(ts), dayofweek(ts) (0 = Sunday), date_trunc('day', ts) and strftime(ts, '%H:%M') are local.
- devices(name, type, lat, lon, display_name, postal_code, last_activity, last_telemetry, \
in_inventory): every device. type: Lighting (street lights), Parking (parking sensors), \
Wastebin, smartsense (air and noise stations), InfoPoints (info screens), Camera. A lamp's \
display_name carries its street ('Mnasiadou 2').
- telemetry(device, key, ts, value, value_num, source): raw readings, value_num when numeric. \
Keys: Lighting level (0 dark, above 0 lit), powerstats (JSON text; json_extract(value, \
'$.last_pwr_act_avg') is watts); Parking status ('1' occupied, '0' free), vbatt; Wastebin \
level (fill %), temperature, pickup_time (epoch ms); smartsense caqi, PM2.5, PM10, NO2, O3, \
Sound_level (dB), T (deg C), RH (%).
- state_changes(device, device_type, key, ts, old_value, new_value, old_state, new_state): \
every change of meaning: parking free/occupied, lamp lit/dark, bin low/medium/high/full, CAQI \
band, camera and screen state. Rows with new_state = 'full' are the moments bins filled.
- events(ts, device, message): the city's alarm log ('YW1337: Fill level above threshold of \
80 %').
- alarms(id, device, device_type, type, severity, status, start_ts, end_ts, clear_ts, \
first_seen, last_seen).
- traffic_counts(granularity, bucket_start, analytics_id, line_name, object_type, dir1, dir2, \
total): camera counts, granularity 'day' or 'hour' (always filter on it). analytics_id 0 is \
the whole city (line_name ''); a camera's days are per counting line, its hours (when kept) \
have line_name ''. object_type 0 people, 1 bike, 2 car, 3 motorcycle, 4 bus, 5 truck.
- traffic_cameras(analytics_id, name, lat, lon, device_name, status).
- speeds(granularity, line_type, bucket_start, value): city-wide average speed in km/h, \
granularity 'FIVE_MIN' or 'ONE_HOUR' (filter on one).
- travel_times(segment_id, segment_name, from_site, to_site, measured_at, travel_time_s, \
speed_kmh): national Bluetooth road segments, no coordinates.
- coverage(device_type, device, source, start_ts, end_ts): stretches the store holds every \
reading for ('' = all devices of the type).
- job_runs(job, started_at, finished_at, status, requests, rows_written, message)."""

RUN_SQL = {
    "name": "run_sql",
    "description": (
        "Run one read-only SQL query over the twin's memory, for questions query_history "
        "can't express: counting alarms or events, rankings with custom conditions, joins "
        "between devices, road segment travel times, how fresh or complete the store is. "
        "Prefer query_history for a metric over time: it applies the live rules (staleness, "
        "lamps by night and day), which raw readings don't. One SELECT (WITH is fine), on a "
        "read-only connection with no files, ATTACH, COPY, INSTALL or LOAD, stopped after 5 "
        "seconds, at most max_rows rows. Aggregate in SQL rather than fetching raw rows. Name "
        "a device or camera column `id` and include lat and lon to get a layer_ref; times come "
        "back as local ISO text, so a chart of the data_ref can use them as temporal.\n"
        + SQL_SCHEMA),
    "input_schema": _obj({
        "sql": {"type": "string", "minLength": 6, "maxLength": 6000,
                "description": "One SELECT statement."},
        "title": {"type": "string", "description": "What the result is, for its data_ref."},
        "max_rows": {"type": "integer", "minimum": 1, "maximum": 2000,
                     "description": "Rows to return at most (default 500)."},
    }, ["sql"]),
    "eager_input_streaming": True,
}

TOOL_DEFS = [QUERY_HISTORY, RUN_SQL]


# -- time ranges -------------------------------------------------------------------------------


def parse_local(text: str, *, end: bool) -> datetime:
    text = text.strip()
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        raise _tools().ToolError(f"can't read {text!r} as a date or date-time "
                                 "(use 2026-11-02 or 2026-11-02T18:00)") from None
    bare_date = len(text) <= 10
    t = t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)
    if bare_date and end:
        t += timedelta(days=1)
    return t


def time_range(a: dict, now: datetime) -> tuple[datetime, datetime]:
    """The asked range as aware datetimes, ending no later than now."""
    local = now.astimezone(TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    period = a.get("period") or "last_7_days"
    start, end = {
        "today": (midnight, local),
        "yesterday": (midnight - timedelta(days=1), midnight),
        "last_24_hours": (local - timedelta(hours=24), local),
        "last_7_days": (local - timedelta(days=7), local),
        "last_14_days": (local - timedelta(days=14), local),
        "last_30_days": (local - timedelta(days=30), local),
        "this_week": (midnight - timedelta(days=local.weekday()), local),
        "last_week": (midnight - timedelta(days=local.weekday() + 7),
                      midnight - timedelta(days=local.weekday())),
        "this_month": (midnight.replace(day=1), local),
        "last_month": ((midnight.replace(day=1) - timedelta(days=1)).replace(day=1),
                       midnight.replace(day=1)),
    }[period]
    if a.get("from"):
        start = parse_local(a["from"], end=False)
    if a.get("to"):
        end = parse_local(a["to"], end=True)
    end = min(end, local)
    if start >= end:
        raise _tools().ToolError("the range is empty: `from` must be before `to` and before now")
    if end - start > timedelta(days=history.MAX_DAYS):
        raise _tools().ToolError(f"the range is longer than {history.MAX_DAYS} days; narrow it")
    return start, end


def _label(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%a %d %b %Y %H:%M")


# -- choosing entities -------------------------------------------------------------------------


def _blank():
    global BLANK
    if BLANK is None:
        from collector.interpret import Interpretation

        BLANK = Interpretation("", "unknown", True, None, None, "")
    return BLANK


def device_entities(ctx, device_type: str) -> list[DeviceState]:
    """Every device of a type with its label and street, without asking any device for
    readings: the inventory and the attributes are cached. From the store if the API fails."""
    try:
        devices = ctx.city.devices_of(device_type)
        attrs = ctx.city.attributes(device_type)
        return [DeviceState(d, *label_and_street(d, attrs.get(d.name, {})), _blank())
                for d in devices]
    except ApiError:
        if ctx.memory is None:
            raise
        rows = ctx.memory.inventory(device_type)
        out = []
        for name, lat, lon, display in rows:
            d = Device(name, device_type, lat, lon)
            out.append(DeviceState(d, *label_and_street(d, {"name": display} if display else {}),
                                   _blank()))
        return out


def street_anchors(ctx, street: str) -> list[tuple[float, float]]:
    """Where the devices named for a street stand: lamps ("Makariou 11"), bins and screens
    whose names carry it. Parking sensors carry no street names, so these trace the street."""
    out = []
    for device_type in ("Lighting", "Wastebin", "InfoPoints", "smartsense"):
        try:
            states = device_entities(ctx, device_type)
        except (ApiError, Unavailable):
            continue
        out += [(s.device.lat, s.device.lon) for s in states if s.device.lat is not None
                and loose_match(street, f"{s.street or ''} {s.label}")]
    return out


def _select(ctx, kind: str, items: list, a: dict, *, name_of: Callable, place_of: Callable,
            text_of: Callable) -> tuple[list, str | None]:
    """Apply ids, street and near filters. Returns (items, how they were matched)."""
    T = _tools()
    how = None
    if a.get("ids"):
        want = {i.strip().lower() for i in a["ids"]}
        items = [x for x in items if name_of(x).lower() in want]
        found = {name_of(x).lower() for x in items}
        missing = sorted(want - found)
        if not items:
            raise T.ToolError(f"none of {a['ids']} is a known {kind.replace('_', ' ')}")
        if missing:
            how = f"ids not found: {', '.join(missing)}"
    center = None
    radius = a.get("radius_m")
    if a.get("street"):
        by_name = [x for x in items if loose_match(a["street"], text_of(x))]
        if by_name:
            items = by_name
            how = f"street matching {a['street']!r}"
        elif kind in ("parking_sensor", "waste_bin") and (anchors := street_anchors(ctx, a["street"])):
            # The devices that do carry the street's name (lamps, bins, screens) trace it.
            radius = radius or 120

            def near_street(x) -> bool:
                lat, lon = place_of(x)
                return lat is not None and any(
                    haversine_m(la, lo, lat, lon) <= radius for la, lo in anchors)
            items = [x for x in items if near_street(x)]
            how = (f"within {radius:g} m of the {len(anchors)} lamps, bins and screens named "
                   f"for {a['street']} (these sensors carry no street names)")
        elif kind in ("parking_sensor", "waste_bin"):
            center = T.resolve_point(ctx, a["street"], None, None)
            radius = radius or 400
            how = (f"within {radius:g} m of {center[2]} (these sensors carry no street names, "
                   "and no device names the street, so one point on it was used)")
        else:
            raise T.ToolError(f"no {kind.replace('_', ' ')} on a street matching {a['street']!r}")
    if center is None and (a.get("near_place") or a.get("near_lat") is not None):
        center = T.resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
        radius = radius or 500
        how = f"within {radius:g} m of {center[2]}"
    if center is not None:
        def dist(x):
            lat, lon = place_of(x)
            return haversine_m(center[0], center[1], lat, lon) if lat is not None else 1e12
        items = sorted((x for x in items if dist(x) <= radius), key=dist)
    if not items:
        raise T.ToolError(f"no {kind.replace('_', ' ')}s match those filters"
                          + (f" ({how})" if how else ""))
    return items, how


# -- series per kind ---------------------------------------------------------------------------


def _merge(a_rd: history.Readings, b_rd: history.Readings) -> tuple[dict, dict]:
    records = {k: list(v) for k, v in b_rd.records.items()}
    for name, recs in a_rd.records.items():
        have = {(r["key"], int(r["timestamp"])) for r in records.get(name, [])}
        records.setdefault(name, []).extend(
            r for r in recs if (r["key"], int(r["timestamp"])) not in have)
    covered = {}
    for name in set(a_rd.covered) | set(b_rd.covered):
        covered[name] = sorted(a_rd.covered.get(name, []) + b_rd.covered.get(name, []))
    return records, covered


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def device_series(ctx, kind: str, metric: Metric, states: list[DeviceState], start: datetime,
                  end: datetime, *, baseline: bool, step_min: int | None = None):
    """(series, baseline series or None, Readings) for devices, replayed with interpret."""
    device_type = KINDS[kind]
    names = [s.device.name for s in states]
    keys = tuple(dict.fromkeys(history.HISTORY_KEYS[device_type]
                               + ((history.AIR_KEYS[metric.name],)
                                  if metric.name in history.AIR_KEYS else ())))
    span = end - start
    step = step_min or (15 if span <= timedelta(days=2) else 30 if span <= timedelta(days=14)
                        else 60)
    times = history.grid(start, end, step)
    if not times:
        raise _tools().ToolError("the range is shorter than one step; widen it")
    lookback = int(history.LOOKBACK[device_type].total_seconds() * 1000)
    rd = history.readings(ctx.city, ctx.memory, device_type, names, keys, _ms(times[0]),
                          _ms(end), lookback_ms=lookback)
    fs, fv = history.replay(device_type, states, rd.records, times, rd.covered)
    series = history.device_series(metric, times, step / 60, names, fs, fv)
    base = None
    if baseline:
        b_start = start - timedelta(weeks=BASELINE_WEEKS)
        try:
            before = history.readings(None, ctx.memory, device_type, names, keys, _ms(b_start),
                                      _ms(start), lookback_ms=lookback)
            records, covered = _merge(rd, before)
        except NoHistory:
            records, covered, b_start = rd.records, rd.covered, start
        btimes = history.grid(b_start, end, 60)
        bfs, bfv = history.replay(device_type, states, records, btimes, covered)
        base = history.device_series(metric, btimes, 1.0, names, bfs, bfv)
    return series, base, rd


def _hour_grid(start: datetime, end: datetime) -> list[datetime]:
    return history.grid(start, end - timedelta(seconds=1), 60) or [start]


def _day_grid(start: datetime, end: datetime) -> list[datetime]:
    d = start.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while d < end:
        out.append(d.astimezone(timezone.utc))
        d = (d + timedelta(days=1, hours=2)).replace(hour=0)  # DST-safe next midnight
    return out


def traffic_series(ctx, metric: Metric, types: tuple[int, ...], cams: dict[str, list[int]],
                   city_wide: bool, bucket: str, start: datetime, end: datetime):
    """Traffic counts per camera (or the whole city) per hour or day, from the store."""
    T = _tools()
    hourly = bucket in ("hour", "hour_of_day", "weekday_hour")
    grain = "hour" if hourly else "day"
    times = _hour_grid(start, end) if hourly else _day_grid(start, end)
    if city_wide:
        rows = ctx.memory.traffic(grain, start, end, None)
        if grain == "day":  # the cameras' own days add up to the city's, where they exist
            per_cam = ctx.memory.traffic("day", start, end, [i for ids in cams.values()
                                                             for i in ids])
            cam_days = {t for t, *_ in per_cam}
            rows = [r for r in rows if r[0] not in cam_days] + [
                (t, 0, o, n) for t, _, o, n in per_cam]
        owner = {0: "city"}
        entities = ["city"]
    else:
        ids = [i for v in cams.values() for i in v]
        rows = ctx.memory.traffic(grain, start, end, ids)
        owner = {i: name for name, v in cams.items() for i in v}
        entities = list(cams)
    if not rows:
        raise T.ToolError(
            f"the twin's memory holds no {'hourly ' if hourly else ''}traffic counts for "
            + ("these cameras" if not city_wide else "the city") + " in this range"
            + (". Hourly counts per camera are kept only for days someone asked get_traffic "
               "about; try bucket day" if hourly and not city_wide else "")
            + ". get_traffic answers today, yesterday and the last 7 or 30 days from the city "
              "API")
    want = set(types)
    sums: dict[tuple[str, int], float] = {}
    seen: set[tuple[str, int]] = set()  # the camera counted something in that bucket
    for t, aid, otype, total in rows:
        if aid not in owner:
            continue
        key = (owner[aid], int(t.timestamp()))
        seen.add(key)
        if otype in want:
            sums[key] = sums.get(key, 0) + (total or 0)
    index = {int(t.timestamp()): k for k, t in enumerate(times)}
    values = [[None] * len(times) for _ in entities]
    for j, name in enumerate(entities):
        for ts, k in index.items():
            if (name, ts) in seen:  # counting then, so a type it never saw is a real 0
                values[j][k] = sums.get((name, ts), 0.0)
    return Series(times, 1.0 if hourly else 24.0, entities, values)


def speed_series(ctx, start: datetime, end: datetime) -> Series:
    rows = ctx.memory.speeds(start, end)
    if not rows:
        raise _tools().ToolError("the twin's memory holds no speeds for this range; get_speed "
                                 "answers the last 48 hours from the city API")
    times = _hour_grid(start, end)
    by = {int(t.timestamp()): v for t, v in rows}
    return Series(times, 1.0, ["city"], [[by.get(int(t.timestamp())) for t in times]])


# -- assembling the answer ---------------------------------------------------------------------


def _dominant(states: list[str]) -> str:
    known = [s for s in states if s != "unknown"]
    return Counter(known).most_common(1)[0][0] if known else "unknown"


def _usual(base: history.Baseline | None, key) -> dict:
    if base is None:
        return {}
    got = base.by_key.get(base.key_of(key))
    if not got:
        return {"usual": None, "usual_low": None, "usual_high": None}
    return {"usual": got[0], "usual_low": got[1], "usual_high": got[2]}


def _frame_label(key: datetime, bucket: str) -> str:
    return key.strftime({"hour": "%a %d %b %H:%M", "day": "%a %d %b", "week": "week of %d %b",
                         "month": "%B %Y"}[bucket])


def assemble(kind: str, metric: Metric, series: Series, meta: dict[str, dict], a: dict,
             start: datetime, end: datetime, base_series: Series | None, *, source: str,
             coverage: float | None, notes: list[str], matched_by: str | None) -> dict:
    """Bucket a series into rows, a map layer and the reply. Returns a payload the handler
    stores (rows, layer) and the result dict for the model."""
    bucket = a.get("bucket") or ("hour" if end - start <= timedelta(days=2) else "day")
    per = a.get("per") or "all"
    how = a.get("aggregate") or metric.over_time
    weekdays = {WEEKDAYS.index(d) + 1 for d in a.get("weekdays") or []} or None
    hours = (a["hours"]["from"], a["hours"]["to"] % 24) if a.get("hours") else None
    b = history.bucketize(series, metric, bucket, how, weekdays=weekdays, hours=hours)
    if not b.keys:
        raise _tools().ToolError("no moments in that range pass the weekday and hour filters")
    notes = list(notes)
    base = None
    base_for: dict[str, history.Baseline | None] = {}
    if a.get("baseline"):
        if base_series is None:
            notes.append("no baseline: the store has no weeks to compare with")
        elif per == "all":
            base = history.baseline(base_series, metric, bucket, weekdays=weekdays, hours=hours,
                                    range_start=start)
        elif len(series.entities) <= 25:
            for name in series.entities:
                base_for[name] = history.baseline(base_series, metric, bucket, entity=name,
                                                  weekdays=weekdays, hours=hours,
                                                  range_start=start)
            base = next((x for x in base_for.values() if x is not None), None)
        else:
            notes.append("a baseline per entity is given for 25 entities at most; use per all")
        if base is None and base_series is not None and not any(base_for.values()):
            notes.append(f"no baseline for bucket {bucket!r} and this metric")
    total = history.bucketize(series, metric, "total", how, weekdays=weekdays, hours=hours)
    whole = {n: total.entity_values[n].get("total") for n in series.entities}

    rows: list[dict] = []
    if per == "all":
        for k, key in enumerate(b.keys):
            rows.append({"frame": k, **history.bucket_fields(key, bucket),
                         metric.name: b.combined[key], **_usual(base, key),
                         "known_pct": b.coverage[key]})
    else:
        for name in series.entities:
            m = meta.get(name, {})
            for k, key in enumerate(b.keys):
                rows.append({"id": name, "label": m.get("label", name), "frame": k,
                             **history.bucket_fields(key, bucket),
                             metric.name: b.entity_values[name][key],
                             **_usual(base_for.get(name), key)})
        if bucket == "total":
            for r in rows:
                r |= {f: meta.get(r["id"], {}).get(f) for f in ("street", "lat", "lon")}
            rows.sort(key=lambda r: (r[metric.name] is None, -(r[metric.name] or 0)))

    # The map: every entity with a place, its value over the range, and a frame per bucket.
    placed = [n for n in series.entities if meta.get(n, {}).get("lat") is not None]
    layer = None
    if placed:
        member: dict = {}
        for k, t in enumerate(series.times):
            local = t.astimezone(TZ)
            if history.keep_moment(local, weekdays, hours):
                member.setdefault(history.bucket_key(local, bucket), []).append(k)
        j_of = {n: series.entities.index(n) for n in placed}

        def state_of(n: str, ks: list[int]) -> str:
            if series.states is None:
                return "camera"
            return _dominant([series.states[j_of[n]][k] for k in ks])

        all_ks = [k for ks in member.values() for k in ks]
        feats = []
        for n in placed:
            m = meta[n]
            st = state_of(n, all_ks)
            value = whole.get(n)
            feats.append(point(m["lon"], m["lat"], {
                "id": n, "label": m.get("street") or m.get("label") or n,
                "name": m.get("label") or n, "state": st,
                "color": STATE_COLORS.get(st, STATE_COLORS["unknown"]),
                "popup": f"{m.get('label') or n}\n{metric.name}: "
                         f"{'no data' if value is None else value} {metric.unit}",
                **({metric.name: value} if value is not None else {})}))
        frames = None
        if bucket in TIME_BUCKETS and 1 < len(b.keys) <= history.MAX_FRAMES:
            per_frame = [[state_of(n, member.get(key, [])) for n in placed] for key in b.keys]
            names = sorted({s for row in per_frame for s in row})
            idx = {s: i for i, s in enumerate(names)}
            frames = {
                "times": [_frame_label(key, bucket) for key in b.keys],
                "iso": [key.strftime("%Y-%m-%dT%H:%M") for key in b.keys],
                "state_names": names,
                "states": [[idx[s] for s in row] for row in per_frame],
                "values": {metric.name: [[b.entity_values[n][key] for n in placed]
                                         for key in b.keys]},
            }
        legend_states = sorted({f["properties"]["state"] for f in feats})
        layer = {"features": feats, "frames": frames, "legend": legend_for(legend_states),
                 "value_fields": [metric.name]}

    filters = {}
    if weekdays:
        filters["weekdays"] = [WEEKDAYS[d - 1] for d in sorted(weekdays)]
    if hours:
        filters["hours"] = f"{hours[0]:02d}:00 to {hours[1] or 24:02d}:00"
    res: dict = {
        "kind": kind, "metric": metric.name, "unit": metric.unit, "means": metric.label,
        "from": _label(start), "to": _label(end), "bucket": bucket, "per": per,
        "aggregate": how, **({"filters": filters} if filters else {}),
        "entities": len(series.entities), "on_map": len(placed),
        **({"matched_by": matched_by} if matched_by else {}),
        "source": source, "coverage_pct": coverage,
    }
    if per == "all":
        vals = [(k, b.combined[k]) for k in b.keys if b.combined[k] is not None]
        res["overall"] = total.combined.get("total")
        if len(vals) > 1:
            hi = max(vals, key=lambda x: x[1])
            lo = min(vals, key=lambda x: x[1])
            res["highest"] = {"value": hi[1], **history.bucket_fields(hi[0], bucket)}
            res["lowest"] = {"value": lo[1], **history.bucket_fields(lo[0], bucket)}
        if base is not None and bucket in ("week", "total"):
            res["usual_overall"] = _usual(base, "total").get("usual")
    else:
        ranked = sorted(((n, v) for n, v in whole.items() if v is not None), key=lambda x: -x[1])
        res["top"] = [{"id": n, "label": meta.get(n, {}).get("label", n), metric.name: v}
                      for n, v in ranked[:10]]
        if len(ranked) > 10:
            res["bottom"] = [{"id": n, "label": meta.get(n, {}).get("label", n),
                              metric.name: v} for n, v in ranked[-3:]]
        res["without_data"] = sum(1 for v in whole.values() if v is None)
    if base is not None:
        res["baseline"] = {
            "weeks": base.weeks, "from": _label(base.start), "to": _label(base.end),
            "includes_range": base.includes_range,
            "method": "median and quartiles " + {
                "hour": "for that hour of that weekday", "weekday_hour": "for that hour of that "
                "weekday", "day": "for that weekday", "weekday": "for that weekday",
                "hour_of_day": "for that hour", "week": "of the weekly values",
                "total": "of the weekly values"}[bucket] + " over the stored weeks"}
        if base.weeks < history.MIN_BASELINE_WEEKS:
            notes.append(f"the baseline rests on {base.weeks} week(s) only: call it thin")
    if coverage is not None and coverage < 90:
        notes.append(f"the store covers {coverage:g}% of this range; buckets without data are "
                     "null")
    if per == "entity" and bucket == "total":  # a ranking: the top of it
        res["rows"] = [{k: v for k, v in r.items() if k not in ("lat", "lon") and v is not None}
                       for r in rows[:15]]
        if len(rows) > 15:
            res["rows_note"] = f"the top 15 of {len(rows)} rows; data_ref holds them all"
    elif per == "all":
        every = -(-len(rows) // MAX_MODEL_ROWS)
        res["rows"] = rows[::every]
        if every > 1:
            res["rows_note"] = (f"one row in {every} of {len(rows)}; data_ref holds them all. "
                                "known_pct is the share of device moments with a known state")
    res["rows_total"] = len(rows)
    if notes:
        res["notes"] = notes
    return {"result": res, "rows": rows, "layer": layer, "per": per, "bucket": bucket}


# -- handlers ----------------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, fn: Callable[[], dict]) -> dict:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
    value = fn()
    with _cache_lock:
        if len(_cache) > 64:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (time.time(), value)
    return value


def compute_query(ctx, a: dict) -> dict:
    T = _tools()
    kind = a["kind"]
    if ctx.memory is None and kind in ("camera", "city_speed"):
        raise T.ToolError("the twin's memory is off in this copy (CHATBOT_RECORD=0)")
    now = ctx.city.now()
    start, end = time_range(a, now)
    notes: list[str] = []
    try:
        if kind in DEVICE_KINDS:
            metric = METRICS[kind].get(a["metric"])
            if metric is None:
                raise T.ToolError(f"metric for {kind} must be one of {sorted(METRICS[kind])}")
            states = [s for s in device_entities(ctx, KINDS[kind])]
            states, how = _select(ctx, kind, states, a, name_of=lambda s: s.device.name,
                                  place_of=lambda s: (s.device.lat, s.device.lon),
                                  text_of=lambda s: f"{s.street or ''} {s.label}")
            series, base, rd = device_series(ctx, kind, metric, states, start, end,
                                             baseline=bool(a.get("baseline")))
            meta = {s.device.name: {"label": s.label, "street": s.street, "lat": s.device.lat,
                                    "lon": s.device.lon} for s in states}
            notes += rd.notes
            return assemble(kind, metric, series, meta, a, start, end, base, source=rd.source,
                            coverage=rd.coverage_pct(), notes=notes, matched_by=how)
        if kind == "camera":
            if a["metric"] not in TRAFFIC_METRICS:
                raise T.ToolError(f"metric for camera must be one of {sorted(TRAFFIC_METRICS)}")
            metric, types = TRAFFIC_METRICS[a["metric"]]
            try:
                cams = ctx.memory.traffic_cameras()
            except Unavailable:
                cams = []
            if not cams:
                cams = ctx.city.traffic_cameras()
            by_name: dict[str, dict] = {}
            for c in cams:
                name = c.get("name") or str(c["analytics_id"])
                entry = by_name.setdefault(name, {"name": name, "ids": [], "lat": c.get("lat"),
                                                  "lon": c.get("lon")})
                entry["ids"].append(c["analytics_id"])
                if entry["lat"] is None:
                    entry["lat"], entry["lon"] = c.get("lat"), c.get("lon")
            filtered = bool(a.get("ids") or a.get("street") or a.get("near_place")
                            or a.get("near_lat") is not None)
            chosen, how = _select(ctx, kind, list(by_name.values()), a,
                                  name_of=lambda c: c["name"],
                                  place_of=lambda c: (c["lat"], c["lon"]),
                                  text_of=lambda c: c["name"])
            per = a.get("per") or "all"
            city_wide = not filtered and per == "all"
            bucket = a.get("bucket") or ("hour" if end - start <= timedelta(days=2) else "day")
            series = traffic_series(ctx, metric, types, {c["name"]: c["ids"] for c in chosen},
                                    city_wide, bucket, start, end)
            base = None
            if a.get("baseline"):
                try:
                    base = traffic_series(ctx, metric, types,
                                          {c["name"]: c["ids"] for c in chosen}, city_wide,
                                          "hour" if bucket in ("hour", "hour_of_day",
                                                               "weekday_hour") else "day",
                                          start - timedelta(weeks=BASELINE_WEEKS), end)
                    if base.step_h == 24:  # daily counts: compare day with day
                        base = _days_as_hours(base)
                except T.ToolError:
                    base = None
            meta = {c["name"]: {"label": c["name"], "lat": c["lat"], "lon": c["lon"]}
                    for c in chosen}
            if city_wide:
                meta = {"city": {"label": "all cameras"}}
            cov = _series_coverage(series)
            notes.append("counts are line crossings: one person can be counted at several "
                         "cameras")
            return assemble(kind, metric, series, meta, a, start, end, base, source="memory",
                            coverage=cov, notes=notes, matched_by=how)
        metric = SPEED_METRICS.get(a["metric"])
        if metric is None:
            raise T.ToolError(f"metric for city_speed must be one of {sorted(SPEED_METRICS)}")
        series = speed_series(ctx, start, end)
        base = None
        if a.get("baseline"):
            try:
                base = speed_series(ctx, start - timedelta(weeks=BASELINE_WEEKS), end)
            except T.ToolError:
                base = None
        notes.append("one city-wide average: there is no per-street speed in this data")
        return assemble(kind, metric, series, {"city": {"label": "city"}}, a, start, end, base,
                        source="memory", coverage=_series_coverage(series), notes=notes,
                        matched_by=None)
    except NoHistory as e:
        raise T.ToolError(str(e)) from None
    except Unavailable as e:
        raise T.ToolError(str(e)) from None


def _days_as_hours(s: Series) -> Series:
    """A daily series for baseline(): each day's value at its midnight, as if hourly, so days
    roll back up into the same day."""
    return Series(s.times, 1.0, s.entities, s.values, s.states)


def _series_coverage(s: Series) -> float:
    cells = [v for row in s.values for v in row]
    return round(100 * sum(v is not None for v in cells) / max(1, len(cells)), 1)


def h_query_history(ctx, a: dict) -> dict:
    loc = ctx.user_location or {}
    key = json.dumps([a, loc.get("lat"), loc.get("lon"), int(ctx.city.now().timestamp() // 60)],
                     sort_keys=True, default=str)
    payload = _cached(key, lambda: compute_query(ctx, a))
    res = json.loads(json.dumps(payload["result"]))  # the model's copy, never the cached one
    m = res["metric"]
    title = f"{res['means'][:1].upper()}{res['means'][1:]} ({res['unit']}), {res['from']} to {res['to']}"
    layer_ref = None
    if payload["layer"]:
        L = payload["layer"]
        layer_ref = ctx.store.add_layer(f"{res['kind']}_{m}", L["features"], geometry="point",
                                        title=title, legend=L["legend"],
                                        value_fields=L["value_fields"], frames=L["frames"])
        res["layer_ref"] = layer_ref
        if L["frames"]:
            res["map_replay"] = (f"layer_ref replays {m} per {payload['bucket']} "
                                 f"({len(L['frames']['times'])} frames); show it with "
                                 f"'columns_3d' or 'heatmap' and value_field {m}")
    if payload["per"] == "entity":
        link = {"key": "id"}
    elif layer_ref and payload["layer"]["frames"]:
        link = {"key": "frame", "layer": layer_ref}
    else:
        link = None
    res["data_ref"] = ctx.store.add_dataset(f"{res['kind']}_{m}", payload["rows"], title=title,
                                            link=link)
    res["_summary"] = (f"{m} by {res['bucket']}, {res['rows_total']} rows from {res['source']}"
                       f" ({res['coverage_pct']:g}% covered)" if res.get("coverage_pct") is not None
                       else f"{m} by {res['bucket']}, {res['rows_total']} rows")
    return res


LAT_COLS = ("lat", "latitude")
LON_COLS = ("lon", "lng", "longitude")
ID_COLS = ("id", "device", "name", "camera")


def h_run_sql(ctx, a: dict) -> dict:
    T = _tools()
    mem = ctx.memory
    if mem is None:
        raise T.ToolError("the twin's memory is off in this copy (CHATBOT_RECORD=0)")
    started = time.monotonic()
    try:
        columns, types, rows, truncated = mem.run_sql(a["sql"], max_rows=a.get("max_rows") or 500)
    except (SqlRejected, Unavailable) as e:
        raise T.ToolError(str(e)) from None
    names: list[str] = []
    for c in columns:  # duplicate names would overwrite each other in the rows
        n, i = c, 2
        while n in names:
            n, i = f"{c}_{i}", i + 1
        names.append(n)
    out = [{n: mem.json_value(v) for n, v in zip(names, r)} for r in rows]
    lower = {n.lower(): n for n in names}
    lat = next((lower[c] for c in LAT_COLS if c in lower), None)
    lon = next((lower[c] for c in LON_COLS if c in lower), None)
    idc = next((lower[c] for c in ID_COLS if c in lower), None)
    numeric = [n for n, t in zip(names, types)
               if any(x in t.upper() for x in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "REAL"))]
    layer_ref = None
    places = None
    if lat and lon:
        places = [(r.get(lat), r.get(lon), r) for r in out]
    elif idc:
        try:
            where = {d.name: (d.lat, d.lon) for d in ctx.city.devices()}
        except ApiError:
            where = {}
        if any(r.get(idc) in where for r in out):
            places = [(*where.get(r.get(idc), (None, None)), r) for r in out]
    if places:
        feats = [point(lo, la, {
            "id": str(r.get(idc)) if idc else str(k), "label": str(r.get(idc) or k),
            "state": "place", "color": STATE_COLORS["place"],
            "popup": "\n".join(f"{n}: {r[n]}" for n in names[:6]),
            **{n: r[n] for n in numeric if isinstance(r.get(n), (int, float))}})
            for k, (la, lo, r) in enumerate(places)
            if isinstance(la, (int, float)) and isinstance(lo, (int, float))]
        if feats:
            layer_ref = ctx.store.add_layer("sql", feats, geometry="point",
                                            title=a.get("title") or "Query result",
                                            legend={"result": STATE_COLORS["place"]},
                                            value_fields=numeric)
    data_ref = ctx.store.add_dataset("sql", out, title=a.get("title") or "Query result",
                                     link={"key": idc} if layer_ref and idc else None)

    def short(v):
        return v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v

    res = {"columns": [{"name": n, "type": t} for n, t in zip(names, types)],
           "rows_returned": len(out), "truncated": truncated,
           "rows": [{k: short(v) for k, v in r.items()} for r in out[:30]],
           "data_ref": data_ref, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if layer_ref:
        res["layer_ref"] = layer_ref
    if len(out) > 30:
        res["rows_note"] = f"the first 30 of {len(out)} rows; data_ref holds them all"
    if truncated:
        res["truncated_note"] = f"stopped at {len(out)} rows; aggregate more or raise max_rows"
    res["_summary"] = f"{len(out)} rows" + (" (truncated)" if truncated else "")
    return res


HANDLERS = {"query_history": h_query_history, "run_sql": h_run_sql}


# -- the inspector's series --------------------------------------------------------------------

DEFAULT_ENTITY_METRIC = {"street_light": "lit_pct", "parking_sensor": "occupancy_pct",
                         "waste_bin": "fill_pct", "air_station": "caqi"}


class EntityNotFound(KeyError):
    pass


def entity_history(ctx, entity_id: str, hours: int = 24, metric_name: str | None = None) -> dict:
    """One device's metric over the last `hours`, with its typical-day band, for a sparkline:
    {id, kind, metric, unit, points: [{time, value, usual, usual_low, usual_high}], ...}."""
    want = entity_id.strip().lower()
    try:
        device_type = next((d.type for d in ctx.city.devices() if d.name.lower() == want), None)
    except ApiError:
        device_type = None
        for t in (KINDS[k] for k in DEVICE_KINDS):
            if ctx.memory is not None and any(n.lower() == want
                                               for n, *_ in ctx.memory.inventory(t)):
                device_type = t
                break
    kind = next((k for k in DEVICE_KINDS if KINDS[k] == device_type), None)
    if kind is None:
        raise EntityNotFound(entity_id)
    found = next((s for s in device_entities(ctx, device_type)
                  if s.device.name.lower() == want), None)
    if found is None:
        raise EntityNotFound(entity_id)
    metric = METRICS[kind].get(metric_name or DEFAULT_ENTITY_METRIC[kind])
    if metric is None:
        raise ValueError(f"metric for {kind} must be one of {sorted(METRICS[kind])}")
    end = ctx.city.now()
    start = end - timedelta(hours=hours)
    series, base, rd = device_series(ctx, kind, metric, [found], start, end, baseline=True,
                                     step_min=15 if hours <= 48 else 60)
    usual = history.baseline(base, metric, "hour", range_start=start) if base else None
    points = []
    for t, v in zip(series.times, series.values[0]):
        local = t.astimezone(TZ)
        row = {"time": local.strftime("%Y-%m-%dT%H:%M"), "value": v}
        if usual is not None:
            row |= _usual(usual, local.replace(minute=0, second=0, microsecond=0))
        points.append(row)
    return {"id": found.device.name, "kind": kind, "label": found.label, "street": found.street,
            "metric": metric.name, "unit": metric.unit, "means": metric.label,
            "hours": hours, "points": points, "source": rd.source,
            "coverage_pct": rd.coverage_pct(),
            "baseline": {"weeks": usual.weeks, "includes_range": usual.includes_range,
                         "method": "median and quartiles for that hour of that weekday"}
            if usual else None,
            "notes": rd.notes}

"""Device history: the one history reader, map time-lapses, and metrics over weeks.

- `readings()` is the history reader every replay and question uses. It answers from the
  twin's memory (chatbot/memory.py) and fills what the memory doesn't cover from the city API
  (getTelemetryRange), for the last `API_HOURS` only. What it fetches is recorded, so the next
  question about the same stretch costs no request.
- `replay()` interprets the readings at evenly spaced moments with collector.interpret, so a
  frame says "on" or "full" by exactly the rules the live tools use, including lamp night/day
  logic and staleness. Moments the store holds no readings for are "unknown", never guessed.
- `METRICS`, `bucketize()` and `baseline()` turn frames into numbers over hours, days and
  weeks, and the typical day (the median for that hour of that weekday over the stored weeks)
  that "compared with usual" means.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector import db, interpret, sun
from collector.http import ApiError

from chatbot.city import DeviceState
from chatbot.memory import Memory, Unavailable
from chatbot.settings import TIMEZONE

TZ = ZoneInfo(TIMEZONE)

# The keys a frame needs per device type: few, so a day of history is one request per device.
HISTORY_KEYS: dict[str, tuple[str, ...]] = {
    "Lighting": ("level", "powerstats"),
    "Parking": ("status", "vbatt"),  # vbatt is the ~4-hourly heartbeat that keeps status fresh
    "Wastebin": ("level",),
    "smartsense": ("caqi", "Sound_level", "PM2.5", "NO2"),
}
# Readings fetched from before the first frame, so the first frame knows each device's state.
# Lamps need a day to tell "offline" from "no data"; parking status changes only on arrivals.
LOOKBACK: dict[str, timedelta] = {
    "Lighting": timedelta(hours=24),
    "Parking": timedelta(hours=24),
    "Wastebin": timedelta(hours=6),
    "smartsense": timedelta(hours=3),
}
MAX_FRAMES = 200
API_HOURS = 72  # the city API fills gaps this far back at most (plus a replay's lookback)
MIN_FILL_SHARE = 0.02  # smaller gaps than this share of the range aren't worth the requests
MAX_DAYS = 62  # the longest range a question can cover


class NoHistory(Exception):
    """Neither the twin's memory nor the city API can cover the range. The message says why."""


def frame_times(now: datetime, hours: int, step_min: int) -> list[datetime]:
    """Moments every `step_min` minutes over the last `hours`, aligned to the step (18:00, 18:15)."""
    step = step_min * 60
    end = int(now.timestamp()) // step * step
    n = hours * 60 // step_min
    return [datetime.fromtimestamp(end - (n - k) * step, tz=timezone.utc) for k in range(n + 1)]


def grid(start: datetime, end: datetime, step_min: int) -> list[datetime]:
    """Moments every `step_min` minutes from `start` (rounded up to the step) to `end`."""
    step = step_min * 60
    first = -(-int(start.timestamp()) // step) * step
    last = int(end.timestamp()) // step * step
    return [datetime.fromtimestamp(t, tz=timezone.utc) for t in range(first, last + 1, step)]


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


# -- the history reader ------------------------------------------------------------------------


@dataclass
class Readings:
    records: dict[str, list[dict]]  # per device, API-shaped {key, timestamp, value}
    covered: dict[str, list[tuple[int, int]]]  # per device, ms stretches with every reading
    sources: list[str]  # "memory", "city_api"
    start_ms: int
    end_ms: int
    notes: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return "+".join(self.sources) or "none"

    def coverage_pct(self) -> float:
        """Share of device-time in [start, end] the readings are complete for."""
        span = max(1, self.end_ms - self.start_ms)
        if not self.covered:
            return 0.0
        got = [sum(e - s for s, e in spans) / span for spans in self.covered.values()]
        return round(100 * sum(got) / len(got), 1)


def _holes(spans: list[tuple[int, int]], start: int, end: int, min_ms: int) -> list[tuple[int, int]]:
    out, at = [], start
    for s, e in sorted(spans):
        if s - at > min_ms:
            out.append((at, s))
        at = max(at, e)
    if end - at > min_ms:
        out.append((at, end))
    return out


def readings(city, memory: Memory | None, device_type: str, names: list[str],
             keys: tuple[str, ...], start_ms: int, end_ms: int, *,
             lookback_ms: int = 0) -> Readings:
    """Readings for `names` between start and end (plus `lookback_ms` before, to know each
    device's state at the start): from the twin's memory, with the city API filling the part
    of the last API_HOURS the memory doesn't cover. Raises NoHistory when neither can help."""
    gap = db.gap_ms(device_type)
    out = Readings({}, {n: [] for n in names}, [], start_ms, end_ms)
    memory_error: Unavailable | None = None
    if memory is not None:
        if memory.recorder is not None:
            memory.recorder.flush(timeout=2.0)  # what the chatbot just fetched counts too
        try:
            covered = memory.coverage(device_type, names, start_ms, end_ms)
            if any(covered.values()):
                out.records = memory.readings(names, keys, start_ms - lookback_ms, end_ms)
                out.covered = covered
                out.sources.append("memory")
        except Unavailable as e:
            memory_error = e
            out.notes.append(f"the twin's memory was not available ({e})")

    holes_by = {n: _holes(out.covered[n], start_ms, end_ms, gap) for n in names}
    holes = [h for hs in holes_by.values() for h in hs]
    if holes and city is not None:
        floor = city.now_ms() - API_HOURS * 3_600_000
        fetch_from = max(min(h[0] for h in holes), floor, start_ms)
        missing = max(sum(max(0, b - max(a, fetch_from)) for a, b in hs)
                      for hs in holes_by.values())
        live_end = any(b >= end_ms - gap for b in (h[1] for h in holes)) and (
            end_ms >= city.now_ms() - gap)
        # Worth a request per device when the gap is a real part of the range, or when it is
        # the present: the newest moments of a replay must not show as unknown.
        if end_ms - fetch_from > gap and (
                live_end or missing >= max(gap, MIN_FILL_SHARE * (end_ms - start_ms))):
            # The store already knows each device's state where the gap starts: no lookback.
            known = all(any(s <= fetch_from <= e for s, e in out.covered[n]) for n in names)
            try:
                got, complete = city.history_complete(
                    device_type, names, tuple(keys),
                    fetch_from - (0 if known else lookback_ms), end_ms)
            except ApiError as e:
                if not out.sources:
                    raise
                out.notes.append(f"the city API failed, so gaps stay empty ({e})")
            else:
                for name, recs in got.items():
                    have = {(r["key"], int(r["timestamp"])) for r in out.records.get(name, [])}
                    out.records.setdefault(name, []).extend(
                        r for r in recs if (r["key"], int(r["timestamp"])) not in have)
                    since = max(fetch_from, complete.get(name, fetch_from))
                    out.covered[name] = db.merge_spans(out.covered[name] + [(since, end_ms)], 0)
                out.sources.append("city_api")
        if min(h[0] for h in holes) < floor - gap:
            out.notes.append(
                f"the city API is only asked for the last {API_HOURS} hours, and the twin's "
                "memory has gaps before that: those moments show as unknown")
    if not out.sources:
        if memory_error is not None:
            raise NoHistory(str(memory_error))
        raise NoHistory(
            f"the twin's memory holds no readings for this range, and the city API is only "
            f"asked for the last {API_HOURS} hours. Running the collector (or "
            "`collector backfill`) fills the memory")
    return out


# -- replaying frames --------------------------------------------------------------------------


def _inside(spans: list[tuple[int, int]], t_ms: float, i: int) -> tuple[bool, int]:
    while i < len(spans) and spans[i][1] < t_ms:
        i += 1
    return (i < len(spans) and spans[i][0] <= t_ms), i


def replay(device_type: str, states: list[DeviceState], records: dict[str, list[dict]],
           times: list[datetime], covered: dict[str, list[tuple[int, int]]] | None = None,
           ) -> tuple[list[list[str]], dict[str, list[list[float | None]]]]:
    """Interpret every device at every moment.

    Returns (states[frame][device], values[field][frame][device]), where the fields are the
    numeric ones DeviceState.values() gives for the type. Stale readings (other than lamps, whose
    silence is meaningful) become "unknown" with no values, as the live tools treat them, and
    so does every moment outside a device's `covered` stretches, when they are given.

    The interpretation is reused while a device has no new readings and its staleness can't
    have changed (interpret's own STALE_AFTER), which makes weeks of frames cheap. Lamps are
    interpreted at every frame: night and day change their meaning.
    """
    lamp_kwargs = []
    if device_type == "Lighting":
        for t in times:
            night = sun.is_night(t)
            lamp_kwargs.append({"night": night, "night_since": sun.night_started(t) if night else None})
    limit_s = interpret.STALE_AFTER.get(device_type, timedelta(hours=2)).total_seconds()
    out_states: list[list[str]] = [[] for _ in times]
    out_values: dict[str, list[list[float | None]]] = {}
    if states:  # the fields DeviceState.values() gives depend on the type only
        blank = interpret.Interpretation(device_type, "unknown", True, None, None, "")
        fields = list(DeviceState(states[0].device, "", None, blank).values())
        out_values = {f: [[] for _ in times] for f in fields}
    nothing = dict.fromkeys(out_values)
    for s in states:
        recs = sorted((int(r["timestamp"]), r["key"], r.get("value"))
                      for r in records.get(s.device.name, []))
        spans = sorted(covered.get(s.device.name, [])) if covered is not None else None
        span_i = 0
        latest: dict[str, tuple[int, str | None]] = {}
        i = 0
        prev = None  # (interp, state, values) of the previous frame
        for k, t in enumerate(times):
            t_ms = t.timestamp() * 1000
            fresh = False
            while i < len(recs) and recs[i][0] <= t_ms:
                latest[recs[i][1]] = (recs[i][0], recs[i][2])
                i += 1
                fresh = True
            inside = True
            if spans is not None:
                inside, span_i = _inside(spans, t_ms, span_i)
            if not inside:
                state, values, prev = "unknown", nothing, None
            elif (prev is not None and not fresh and device_type != "Lighting"
                    and (prev[0].stale or prev[0].as_of is None
                         or (t - prev[0].as_of).total_seconds() <= limit_s)):
                _, state, values = prev
            else:
                kwargs = lamp_kwargs[k] if lamp_kwargs else {}
                interp = interpret.interpret(device_type, latest, t, **kwargs)
                values = DeviceState(s.device, s.label, s.street, interp).values()
                state = interp.state
                if interp.stale and device_type != "Lighting":
                    state, values = "unknown", nothing
                prev = (interp, state, values)
            out_states[k].append(state)
            for name, rows in out_values.items():
                v = values.get(name)
                rows[k].append(round(v, 1) if isinstance(v, (int, float)) else None)
    return out_states, out_values


def frame_rows(device_type: str, times: list[datetime], states: list[list[str]],
               values: dict[str, list[list[float | None]]]) -> list[dict]:
    """One summary row per frame (counts per state and the headline average), for charts."""
    rows = []
    for k, t in enumerate(times):
        local = t.astimezone(TZ)
        counts = Counter(states[k])
        row: dict = {"frame": k, "time": local.strftime("%Y-%m-%dT%H:%M"),
                     "label": local.strftime("%a %H:%M"), **counts}

        def mean(field: str) -> float | None:
            vals = [v for v in values.get(field, [[]] * len(times))[k] if v is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        if device_type == "Lighting":
            watts = [v or 0 for v, st in zip(values.get("power_w", [[]] * len(times))[k],
                                             states[k]) if st == "on"]
            row["lit_power_kw"] = round(sum(watts) / 1000, 2)
        elif device_type == "Parking":
            known = counts["occupied"] + counts["free"]
            row["occupancy_pct"] = round(100 * counts["occupied"] / known, 1) if known else None
        elif device_type == "Wastebin":
            row["avg_level_pct"] = mean("level_pct")
        elif device_type == "smartsense":
            row["avg_caqi"] = mean("caqi")
            row["avg_noise_db"] = mean("noise_db")
        rows.append(row)
    return rows


# -- metrics -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    label: str
    value: Callable[[str, dict], float | None]  # one device at one moment: (state, values)
    across: str = "mean"  # combining devices at one moment: mean | sum
    over_time: str = "mean"  # combining moments in a bucket: mean | sum
    per_hour: bool = False  # a rate: multiplied by the hours between moments (kWh, hours full)
    entering: str | None = None  # counts moments a device entered this state (arrivals)
    # A per-moment factor from FACTORS the value is multiplied by (the grid's carbon for CO2);
    # a moment the factor doesn't know is unknown. `sources` and `note` go with its answers.
    factor: str | None = None
    sources: tuple[str, ...] = ()
    note: str | None = None

    def describe(self) -> str:
        return f"{self.name} ({self.unit}): {self.label}"


# Per-moment factors a metric can be weighted by: name -> fn(moments) -> [factor or None].
# chatbot/energy registers "grid_co2", the grid's kg CO2 per kWh at each moment.
FACTORS: dict[str, Callable[[list[datetime]], list[float | None]]] = {}


def register_factor(name: str, fn: Callable[[list[datetime]], list[float | None]]) -> None:
    FACTORS[name] = fn


def _factors(metric: Metric, times: list[datetime]) -> list[float | None] | None:
    if metric.factor is None:
        return None
    fn = FACTORS.get(metric.factor)
    if fn is None:
        return [None] * len(times)
    try:
        out = list(fn(times))
    except Exception:  # noqa: BLE001 - a missing factor makes the moments unknown, never an error
        return [None] * len(times)
    return out if len(out) == len(times) else [None] * len(times)


def _is(state: str, *yes: str, unknown: tuple[str, ...] = ("unknown",)) -> float | None:
    if state in unknown:
        return None
    return 1.0 if state in yes else 0.0


def _pct(state: str, *yes: str) -> float | None:
    share = _is(state, *yes)
    return None if share is None else 100 * share


def _field(name: str, scale: float = 1.0) -> Callable[[str, dict], float | None]:
    def get(state: str, values: dict) -> float | None:
        v = (values or {}).get(name)
        return None if v is None else v * scale
    return get


def _lit_kw(state: str, values: dict) -> float | None:
    if state == "unknown":
        return None
    return ((values or {}).get("power_w") or 0) / 1000 if state == "on" else 0.0


METRICS: dict[str, dict[str, Metric]] = {
    "parking_sensor": {m.name: m for m in (
        Metric("occupancy_pct", "%", "share of the bays with a known state that are occupied",
               lambda st, v: _pct(st, "occupied")),
        Metric("occupied_bays", "bays", "bays occupied", lambda st, v: _is(st, "occupied"),
               across="sum"),
        Metric("free_bays", "bays", "bays free", lambda st, v: _is(st, "free"), across="sum"),
        Metric("arrivals", "arrivals", "times a free bay became occupied",
               lambda st, v: _is(st, "occupied"), across="sum", over_time="sum",
               entering="occupied"),
    )},
    "street_light": {m.name: m for m in (
        Metric("lit_pct", "%", "share of the lamps that are lit",
               lambda st, v: _pct(st, "on")),
        Metric("lamps_lit", "lamps", "lamps lit", lambda st, v: _is(st, "on"), across="sum"),
        Metric("lamps_offline", "lamps", "lamps silent at night (offline)",
               lambda st, v: _is(st, "offline"), across="sum"),
        Metric("power_kw", "kW", "power drawn by lit lamps", _lit_kw, across="sum"),
        Metric("energy_kwh", "kWh", "energy used by lit lamps", _lit_kw, across="sum",
               over_time="sum", per_hour=True),
        Metric("co2_kg", "kg CO2", "CO2 from the energy lit lamps used, at the grid's estimated "
               "carbon for each quarter hour (the Transmission System Operator's figures)", _lit_kw,
               across="sum", over_time="sum", per_hour=True, factor="grid_co2",
               sources=("tso_grid", "oil_co2"),
               note="CO2 uses the grid's carbon at each quarter hour: the oil-fired stations' share "
                    "of the island's demand then, times 0.70 kg CO2 per kWh they generate (an "
                    "estimate); moments the twin has no record of the grid for count as unknown"),
    )},
    "waste_bin": {m.name: m for m in (
        Metric("fill_pct", "%", "fill level", _field("level_pct")),
        Metric("full_bins", "bins", "bins at 80% or more", lambda st, v: _is(st, "full"),
               across="sum"),
        Metric("hours_full", "h", "hours spent at 80% or more", lambda st, v: _is(st, "full"),
               across="sum", over_time="sum", per_hour=True),
        Metric("became_full", "times", "times a bin reached 80% or more",
               lambda st, v: _is(st, "full"), across="sum", over_time="sum", entering="full"),
    )},
    "air_station": {m.name: m for m in (
        Metric("caqi", "CAQI", "air quality index (CAQI)", _field("caqi")),
        Metric("pm2_5", "µg/m³", "fine particles PM2.5", _field("pm2_5")),
        Metric("pm10", "µg/m³", "particles PM10", _field("pm10")),
        Metric("no2", "µg/m³", "nitrogen dioxide", _field("no2")),
        Metric("o3", "µg/m³", "ozone", _field("o3")),
        Metric("noise_db", "dB", "noise level", _field("noise_db")),
        Metric("temperature_c", "°C", "temperature at the station", _field("temperature_c")),
    )},
}
# Readings each air metric needs beyond HISTORY_KEYS["smartsense"].
AIR_KEYS = {"pm10": "PM10", "o3": "O3", "temperature_c": "T"}


@dataclass
class Series:
    """Values per entity per moment, with the moments in UTC."""
    times: list[datetime]
    step_h: float
    entities: list[str]
    values: list[list[float | None]]  # [entity][moment]
    states: list[list[str]] | None = None  # [entity][moment], for the map's colours


def device_series(metric: Metric, times: list[datetime], step_h: float, names: list[str],
                  states: list[list[str]], values: dict[str, list[list[float | None]]]) -> Series:
    """A metric's values from replayed frames (states[frame][device], values[field][...])."""
    fields = list(values)
    weights = _factors(metric, times)
    per: list[list[float | None]] = []
    per_states: list[list[str]] = []
    for j, _ in enumerate(names):
        row: list[float | None] = []
        st_row: list[str] = []
        prev_state = None
        for k in range(len(times)):
            st = states[k][j]
            vals = {f: values[f][k][j] for f in fields}
            v = metric.value(st, vals)
            if metric.entering is not None and v is not None:
                v = 1.0 if (st == metric.entering and prev_state not in (None, "unknown",
                                                                         metric.entering)) else 0.0
                if prev_state in (None, "unknown"):
                    v = None
            if v is not None and metric.per_hour:
                v *= step_h
            if v and weights is not None:  # nothing drawn is no CO2, whatever the grid did
                v = None if weights[k] is None else v * weights[k]
            row.append(v)
            st_row.append(st)
            prev_state = st
        per.append(row)
        per_states.append(st_row)
    return Series(times, step_h, list(names), per, per_states)


# -- buckets and aggregates --------------------------------------------------------------------

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BUCKETS = ["hour", "day", "week", "month", "hour_of_day", "weekday", "weekday_hour", "total"]
AGGREGATES = ["mean", "median", "min", "max", "sum", "p90"]
TIME_BUCKETS = ("hour", "day", "week", "month")


def aggregate(values: list[float], how: str) -> float | None:
    if not values:
        return None
    if how == "mean":
        return sum(values) / len(values)
    if how == "sum":
        return sum(values)
    if how == "median":
        return statistics.median(values)
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    if how == "p90":
        return _quantile(values, 0.9)
    raise ValueError(how)


def _quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def bucket_key(t: datetime, bucket: str):
    """The bucket a local moment falls in, as a sortable key."""
    if bucket == "hour":
        return t.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        d = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    if bucket == "month":
        return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if bucket == "hour_of_day":
        return t.hour
    if bucket == "weekday":
        return t.isoweekday()
    if bucket == "weekday_hour":
        return (t.isoweekday(), t.hour)
    return "total"


def bucket_fields(key, bucket: str) -> dict:
    """Row fields naming a bucket, ready for charts (local ISO times, weekday names)."""
    if bucket in TIME_BUCKETS:
        out = {"time": key.strftime("%Y-%m-%dT%H:%M")}
        if bucket == "day":
            iso = key.isocalendar()
            out |= {"date": key.strftime("%Y-%m-%d"), "weekday": key.strftime("%a"),
                    "weekday_num": key.isoweekday(), "week": f"{iso.year}-W{iso.week:02d}"}
        elif bucket == "hour":
            out |= {"weekday": key.strftime("%a"), "hour": key.hour}
        elif bucket == "week":
            out["week"] = f"{key.isocalendar().year}-W{key.isocalendar().week:02d}"
        return out
    if bucket == "hour_of_day":
        return {"hour": key, "hour_label": f"{key:02d}:00"}
    if bucket == "weekday":
        return {"weekday": WEEKDAYS[key - 1].capitalize(), "weekday_num": key}
    if bucket == "weekday_hour":
        return {"weekday": WEEKDAYS[key[0] - 1].capitalize(), "weekday_num": key[0],
                "hour": key[1]}
    return {}


def keep_moment(t_local: datetime, weekdays: set[int] | None, hours: tuple[int, int] | None) -> bool:
    """Whether a local moment passes the weekday (1 = Monday) and hour [from, to) filters."""
    if weekdays and t_local.isoweekday() not in weekdays:
        return False
    if hours:
        a, b = hours
        h = t_local.hour
        if (a <= h < b) if a < b else (h >= a or h < b):
            return True
        return False
    return True


@dataclass
class Bucketed:
    keys: list  # bucket keys, sorted
    entity_values: dict[str, dict]  # entity -> {key: value}
    combined: dict  # key -> value (all entities together)
    coverage: dict  # key -> share of entity-moments with a value
    moments: dict  # key -> moments counted


def combine_moment(metric: Metric, vals: list[float | None], min_share: float = 0.5
                   ) -> float | None:
    """All entities at one moment. None when fewer than `min_share` of them have a value:
    a sum over half the city isn't the city's figure."""
    known = [v for v in vals if v is not None]
    if not vals or len(known) < min_share * len(vals) or not known:
        return None
    return sum(known) / len(known) if metric.across == "mean" else sum(known)


def bucketize(series: Series, metric: Metric, bucket: str, how: str | None = None, *,
              weekdays: set[int] | None = None, hours: tuple[int, int] | None = None,
              min_share: float = 0.5) -> Bucketed:
    """Aggregate a series into buckets, per entity and for all entities together."""
    how = how or metric.over_time
    members: dict = {}
    for k, t in enumerate(series.times):
        local = t.astimezone(TZ)
        if keep_moment(local, weekdays, hours):
            members.setdefault(bucket_key(local, bucket), []).append(k)
    keys = sorted(members, key=lambda x: (isinstance(x, str), x))
    entity_values: dict[str, dict] = {}
    for j, name in enumerate(series.entities):
        row = series.values[j]
        entity_values[name] = {
            key: _round(aggregate([row[k] for k in ks if row[k] is not None], how))
            for key, ks in members.items()}
    combined, coverage = {}, {}
    for key, ks in members.items():
        per_moment = [combine_moment(metric, [series.values[j][k] for j in
                                              range(len(series.entities))], min_share)
                      for k in ks]
        combined[key] = _round(aggregate([v for v in per_moment if v is not None], how))
        total = len(ks) * max(1, len(series.entities))
        have = sum(series.values[j][k] is not None for j in range(len(series.entities))
                   for k in ks)
        coverage[key] = round(100 * have / total, 1) if total else 0.0
    return Bucketed(keys, entity_values, combined, coverage, {k: len(v) for k, v in members.items()})


def _round(v: float | None) -> float | None:
    if v is None:
        return None
    return round(v, 2) if abs(v) < 10 else round(v, 1)


# -- typical-day baselines ---------------------------------------------------------------------

BASELINE_WEEKS = 8
MIN_BASELINE_WEEKS = 3


@dataclass
class Baseline:
    """The usual value per bucket key: the median (and quartiles) over the stored weeks."""
    by_key: dict  # baseline key -> (median, p25, p75, n)
    weeks: int
    start: datetime
    end: datetime
    includes_range: bool
    key_of: Callable  # bucket key -> baseline key


def baseline_key_fn(bucket: str) -> Callable | None:
    """How a bucket finds its usual value: an hour by its weekday and hour, a day by its
    weekday, an hour of the day by the hour. None when a bucket has no typical value."""
    if bucket in ("hour", "weekday_hour"):
        return lambda key: (key.isoweekday(), key.hour) if bucket == "hour" else key
    if bucket in ("day", "weekday"):
        return lambda key: key.isoweekday() if bucket == "day" else key
    if bucket == "hour_of_day":
        return lambda key: key
    if bucket in ("week", "total"):
        return lambda key: "week"
    return None


def baseline(series: Series, metric: Metric, bucket: str, *, entity: str | None = None,
             weekdays: set[int] | None = None, hours: tuple[int, int] | None = None,
             range_start: datetime | None = None, min_share: float = 0.5) -> Baseline | None:
    """The typical value for each bucket, from an hourly series spanning the stored weeks.

    Hourly values are grouped by weekday and hour (or rolled up into days or weeks first, for
    day, weekday, week and total buckets), and each group gives its median and quartiles. Weeks
    before `range_start` are used when at least MIN_BASELINE_WEEKS of them hold data;
    otherwise every stored week is (`includes_range`)."""
    key_of = baseline_key_fn(bucket)
    if key_of is None or (bucket in ("week", "total") and metric.over_time == "sum"):
        return None
    if entity is not None:
        j = series.entities.index(entity)
        sub = Series(series.times, series.step_h, [entity], [series.values[j]])
    else:
        sub = series
    hourly = bucketize(sub, metric, "hour", weekdays=weekdays, hours=hours, min_share=min_share)
    values = hourly.combined if entity is None else hourly.entity_values[entity]
    points = [(k, v) for k, v in values.items() if v is not None]
    if not points:
        return None

    def weeks_in(pts):
        return {bucket_key(k, "week") for k, _ in pts}

    includes = True
    if range_start is not None:
        before = [(k, v) for k, v in points if k < range_start.astimezone(TZ)]
        if len(weeks_in(before)) >= MIN_BASELINE_WEEKS:
            points, includes = before, False
    groups: dict = {}
    if bucket in ("hour", "weekday_hour", "hour_of_day"):
        for k, v in points:
            groups.setdefault((k.isoweekday(), k.hour) if bucket != "hour_of_day" else k.hour,
                              []).append(v)
    else:
        roll = "day" if bucket in ("day", "weekday") else "week"
        rolled: dict = {}
        for k, v in points:
            rolled.setdefault(bucket_key(k, roll), []).append(v)
        for day, vs in rolled.items():
            v = aggregate(vs, metric.over_time)
            groups.setdefault(day.isoweekday() if roll == "day" else "week", []).append(v)
    by_key = {}
    for g, vs in groups.items():
        if vs:
            by_key[g] = (_round(statistics.median(vs)), _round(_quantile(vs, 0.25)),
                         _round(_quantile(vs, 0.75)), len(vs))
    ks = [k for k, _ in points]
    return Baseline(by_key, len(weeks_in(points)), min(ks), max(ks), includes, key_of)

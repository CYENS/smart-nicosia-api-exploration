"""The waste collection study: the city's bins as the twin's memory knows them over a period,
the measured baseline, each policy replayed on them, and the numbers a council would ask for.

`bin_data` turns the bins and their readings (read from the memory by chatbot/scenarios/waste.py,
never the city API) into series and emptyings; `Study` models each bin; `baseline` replays the
city's own pickups on the model and `policy_outcome` a policy, routing every round; `backtest`
compares the model with what the sensors read.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from collector.interpret import to_number

from chatbot.scenarios.rounds import Crew, Matrix, plan_round
from chatbot.scenarios.waste_model import (
    AT_CAPACITY,
    DAY_MS,
    FULL,
    HOUR_MS,
    LOW_LIFT,
    Bin,
    BinModel,
    DayPlan,
    Pickup,
    Policy,
    Shape,
    Simulation,
    bigger_container,
    clean,
    detect_pickups,
    fill_rate,
    level_at,
    local,
    measured_hours,
    pooled_spread,
    simulate,
    stretch_rates,
    weekly_shape,
)

KEYS = ("level", "pickup_time", "level_before_pickup", "level_after_pickup")
MIN_READINGS = 60  # about five days of two-hourly readings
STUCK_SPAN = 3.0  # a sensor whose readings move less than this all period is stuck
FULL_SHARE = 0.8  # never emptied and at 80 % or more for this share of readings: sat full
DEFAULT_LEFTOVER = 2.0
STANDARD_SIZES = (1500.0, 2500.0, 3200.0, 5000.0)  # container sizes the city already uses
STREAMS = {"Glass": "glass", "Underground": "underground"}

STATUS_LABELS = {
    "modelled": "Modelled",
    "no_readings": "No readings in the period",
    "few_readings": "Too few readings",
    "partial": "Started reporting after the period began",
    "stuck": "Sensor reads the same all period",
    "sat_full": "Sat full all period, never emptied",
    "no_rate": "No stretch long enough to measure filling",
}


@dataclass
class BinData:
    bin: Bin
    times: list[int]
    levels: list[float]
    pickups: list[Pickup]
    status: str = "modelled"
    rates: list[tuple[float, float]] = field(default_factory=list)
    model: BinModel | None = None


def parse_bin(device: dict) -> Bin | None:
    """A Bin from a device row {name, display_name, lat, lon, metadata}."""
    if device.get("lat") is None or device.get("lon") is None:
        return None
    meta = device.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    extra = meta.get("extra") or {}
    stream = STREAMS.get(str(extra.get("trashtype") or ""), "glass")
    volume = to_number(extra.get("volume")) or 2500.0
    if volume > 10000:  # one bin says 25000: a typing slip for 2500, the common size
        volume /= 10
    return Bin(device["name"], device.get("display_name") or device["name"],
               float(device["lat"]), float(device["lon"]), stream, float(volume))


def parse_records(records: Sequence[dict]) -> tuple[list[int], list[float], list[Pickup]]:
    """A bin's readings ({key, timestamp, value}) -> level times and values, and its own
    pickup reports (pickup_time with the levels before and after, at the same timestamp)."""
    times, levels = [], []
    by_ts: dict[int, dict] = defaultdict(dict)
    for r in records:
        key, ts = r["key"], int(r["timestamp"])
        v = to_number(r.get("value"))
        if key == "level":
            times.append(ts)
            levels.append(v)
        elif key in ("pickup_time", "level_before_pickup", "level_after_pickup"):
            by_ts[ts][key] = v
    t, lv = clean(times, levels)
    reported = []
    for ts, rec in by_ts.items():
        when = rec.get("pickup_time")
        if when is None:
            continue
        reported.append(Pickup(int(when), rec.get("level_before_pickup"),
                               rec.get("level_after_pickup"), "sensor"))
    seen, unique = set(), []
    for p in sorted(reported, key=lambda p: p.t_ms):
        if p.t_ms not in seen:
            seen.add(p.t_ms)
            unique.append(p)
    return t, lv, unique


def bin_data(devices: Sequence[dict], records: dict[str, list[dict]], tz: ZoneInfo) -> list[BinData]:
    """Every bin with a position, its level readings and its emptyings."""
    out = []
    for dev in devices:
        b = parse_bin(dev)
        if b is None:
            continue
        times, levels, reported = parse_records(records.get(b.id, []))
        out.append(BinData(b, times, levels, detect_pickups(times, levels, reported, tz=tz)))
    return out


def classify(d: BinData, start_ms: int, end_ms: int) -> str:
    inside = [v for t, v in zip(d.times, d.levels) if start_ms <= t < end_ms]
    if not inside:
        return "no_readings"
    if len(inside) < MIN_READINGS:
        return "few_readings"
    if level_at(d.times, d.levels, start_ms) is None:
        return "partial"  # nothing says where it stood when the period began
    if max(inside) - min(inside) < STUCK_SPAN:
        return "stuck"
    emptied = any(start_ms <= p.t_ms < end_ms for p in d.pickups)
    if not emptied and sum(v >= FULL for v in inside) >= FULL_SHARE * len(inside):
        return "sat_full"
    if not d.rates:
        return "no_rate"
    return "modelled"




class Study:
    """The bins over a period, modelled. `start_ms` is a local midnight; `days` whole days."""

    def __init__(self, data: list[BinData], start_ms: int, days: int, tz: ZoneInfo):
        self.data = data
        self.start_ms = start_ms
        self.days = days
        self.end_ms = start_ms + days * DAY_MS
        self.tz = tz
        for d in data:
            d.rates = stretch_rates(d.times, d.levels, d.pickups) if d.times else []
            d.status = classify(d, start_ms, self.end_ms)
        self.spread = pooled_spread(d.rates for d in data if d.status == "modelled")
        afters = [p.after for d in data for p in d.pickups if p.after is not None]
        self.leftover = statistics.median(afters) if afters else DEFAULT_LEFTOVER
        for d in data:
            if d.status != "modelled":
                continue
            rate = fill_rate(d.rates, self.spread)
            own = [p.after for p in d.pickups if p.after is not None]
            start = level_at(d.times, d.levels, start_ms)
            d.model = BinModel(d.bin, rate, statistics.median(own) if own else self.leftover,
                               start if start is not None else d.levels[0])
        self.shapes: dict[str, Shape] = {}
        for stream in sorted({d.bin.stream for d in data}):
            members = [d for d in data if d.bin.stream == stream and d.status == "modelled"]
            self.shapes[stream] = weekly_shape(((d.times, d.levels, d.pickups) for d in members),
                                               tz)

    @property
    def models(self) -> list[BinModel]:
        return [d.model for d in self.data if d.model]

    def modelled(self) -> list[BinData]:
        return [d for d in self.data if d.model]

    def shape_of(self, stream: str) -> Shape:
        return self.shapes.get(stream) or Shape.flat()

    def status_counts(self) -> Counter:
        return Counter(d.status for d in self.data)

    def measured_pickups(self) -> dict[str, list[Pickup]]:
        return {d.bin.id: [p for p in d.pickups if self.start_ms <= p.t_ms < self.end_ms]
                for d in self.modelled()}

    def known(self) -> dict[str, list[bool]]:
        return {d.bin.id: measured_hours(d.times, d.levels, self.start_ms, self.days * 24)[0]
                for d in self.modelled()}

    def measured_round_days(self) -> tuple[int, ...]:
        """Weekdays the city's trucks went out on in at least two of the period's weeks (a day
        with three emptyings or more counts as a round)."""
        per_day = Counter(local(p.t_ms, self.tz).date()
                          for ps in self.measured_pickups().values() for p in ps)
        weekday_weeks = Counter(day.weekday() for day, n in per_day.items() if n >= 3)
        return tuple(sorted(w for w, n in weekday_weeks.items() if n >= 2)) or (0,)


# -- what a policy (or the city) did ------------------------------------------------------------


@dataclass
class Outcome:
    full_hours: float
    capacity_hours: float
    bins_at_capacity: int
    collections: float
    low_lifts: float
    km: float
    drive_h: float
    stops: int
    trips: int
    bigger: dict[str, float]  # bin id -> the container size it would need
    per_bin: dict[str, dict]
    plans: list[DayPlan]
    daily: list[dict]

    def hours(self, service_min: float, unload_min: float) -> float:
        """Driver hours: driving, plus every lift and every unloading."""
        return self.drive_h + self.stops * service_min / 60 + self.trips * unload_min / 60


def route_day(matrix: Matrix, crew: Crew,
              stops_by_stream: dict[str, list[tuple[Bin, float, float]]], t_ms: int) -> DayPlan:
    plan = DayPlan(t_ms)
    for stream, stops in sorted(stops_by_stream.items()):
        plan.trips.extend(plan_round(stream, stops, matrix, crew))
    return plan


def plan_totals(plans: Sequence[DayPlan]) -> tuple[float, float, int, int]:
    km = sum(p.km for p in plans)
    drive_h = sum(t.drive_min for p in plans for t in p.trips) / 60
    stops = sum(len(t.stops) for p in plans for t in p.trips)
    trips = sum(len(p.trips) for p in plans)
    return km, drive_h, stops, trips


def city_plans(study: Study, matrix: Matrix, crew: Crew, start_hour: int = 6) -> list[DayPlan]:
    """The city's own pickups, routed day by day the same way as every policy's rounds. The
    trucks' real routes aren't known, so these are the kilometres an efficient round through
    the same bins would drive: like for like with the policies, and likely fewer than the real
    trucks drove."""
    by_day: dict[tuple, list[tuple[Bin, float, float]]] = defaultdict(list)
    for d in study.modelled():
        for p in d.pickups:
            if not study.start_ms <= p.t_ms < study.end_ms:
                continue
            before = p.before if p.before is not None else level_at(d.times, d.levels,
                                                                    p.t_ms - HOUR_MS)
            lv = before if before is not None else 50.0
            by_day[(local(p.t_ms, study.tz).date(), d.bin.stream)].append(
                (d.bin, lv, d.bin.volume_l / 1000 * lv / 100))
    plans = []
    for day in sorted({k[0] for k in by_day}):
        t = int(datetime(day.year, day.month, day.day, start_hour, tzinfo=study.tz).timestamp() * 1000)
        plans.append(route_day(matrix, crew, {s: v for (dd, s), v in by_day.items() if dd == day}, t))
    return plans


def sensor_hours(study: Study) -> dict[str, dict]:
    """Per bin, what its sensor read over the period: hours full and at capacity, the hours its
    readings cover, its emptyings and how many of them were of a bin under 30 % full."""
    out = {}
    for d in study.modelled():
        known, vals = measured_hours(d.times, d.levels, study.start_ms, study.days * 24)
        picks = [p for p in d.pickups if study.start_ms <= p.t_ms < study.end_ms]
        lows = 0
        for p in picks:
            before = p.before if p.before is not None else level_at(d.times, d.levels,
                                                                    p.t_ms - HOUR_MS)
            lows += before is not None and before < LOW_LIFT
        out[d.bin.id] = {
            "full_hours": sum(1 for v in vals if v is not None and v >= FULL),
            "capacity_hours": sum(1 for v in vals if v is not None and v >= AT_CAPACITY),
            "known_hours": sum(known), "collections": len(picks), "low_lifts": lows}
    return out


def sim_outcome(study: Study, sim: Simulation, policy: Policy, fill: float) -> Outcome:
    km, drive_h, stops, trips = plan_totals(sim.plans)
    per_bin = {bid: {"full_hours": o.full_hours, "capacity_hours": o.capacity_hours,
                     "collections": o.collections, "low_lifts": o.low_lifts}
               for bid, o in sim.bins.items()}
    # a bigger container is for a busy week: the fast end of the bin's range, or beyond it
    # when the fill-rate assumption is set above that
    bigger = {}
    for m in study.models:
        size = bigger_container(m, policy, STANDARD_SIZES, max(1.0, fill))
        if size:
            bigger[m.bin.id] = size
    daily = []
    for row in sim.daily:
        plans = [p for p in sim.plans if (p.t_ms - sim.start_ms) // DAY_MS == row["day"]]
        daily.append({**row, "km": round(sum(p.km for p in plans), 2),
                      "hours": round(sum(p.hours for p in plans), 2)})
    return Outcome(sim.total("full_hours"), sim.total("capacity_hours"),
                   sum(o.capacity_hours > 0 for o in sim.bins.values()), sim.total("collections"),
                   sim.total("low_lifts"), km, drive_h, stops, trips, bigger, per_bin, sim.plans,
                   daily)


def baseline(study: Study, matrix: Matrix, crew: Crew, fill: float = 0.0,
             start_hour: int = 6) -> Outcome:
    """The city's period on the model: its own pickups replayed on the bins' fill rates (at
    `fill` within each bin's range), so the baseline and every policy share the model and its
    biases (the backtest says how far the model is from the sensors), with those pickups routed
    day by day."""
    policy = Policy("measured", study.measured_round_days(), start_hour=start_hour)
    sim = simulate(study.models, study.start_ms, study.days, policy, shape_of=study.shape_of,
                   tz=study.tz, fill=fill, measured=study.measured_pickups())
    sim.plans = city_plans(study, matrix, crew, start_hour)
    return sim_outcome(study, sim, policy, fill)


def policy_outcome(study: Study, policy: Policy, matrix: Matrix, crew: Crew,
                   fill: float = 0.0) -> Outcome:
    """A policy replayed over the period, every round routed."""
    def planner(stream, stops, t_ms):
        return plan_round(stream, stops, matrix, crew)

    sim = simulate(study.models, study.start_ms, study.days, policy, shape_of=study.shape_of,
                   tz=study.tz, fill=fill, planner=planner, truck_m3=crew.truck_m3)
    return sim_outcome(study, sim, policy, fill)


# -- backtest -----------------------------------------------------------------------------------


def _bt(metric: str, measured: float, modelled: float, lo_hi: Sequence[float], period: str,
        note: str, kind: str) -> dict:
    err = None if not measured else round(100 * (modelled - measured) / measured, 1)
    return {"metric": metric, "unit": "bin-hours", "measured": round(measured),
            "modelled": round(modelled), "low": round(min([*lo_hi, modelled])),
            "high": round(max([*lo_hi, modelled])), "error_pct": err, "period": period,
            "note": note, "kind": kind}


def _measured_total(study: Study, ids: set[str], start_ms: int, days: int, level: float) -> float:
    total = 0
    for d in study.modelled():
        if d.bin.id in ids:
            _, vals = measured_hours(d.times, d.levels, start_ms, days * 24)
            total += sum(1 for v in vals if v is not None and v >= level)
    return total


def _daily_rows(study: Study, sim: Simulation, start_ms: int, days: int, ids: set[str]) -> list[dict]:
    """Per day, bin-hours full as read and as modelled."""
    measured = [0] * days
    for d in study.modelled():
        if d.bin.id not in ids:
            continue
        _, vals = measured_hours(d.times, d.levels, start_ms, days * 24)
        for h, v in enumerate(vals):
            if v is not None and v >= FULL:
                measured[h // 24] += 1
    out = []
    for i in range(days):
        modelled = next((r["full_hours"] for r in sim.daily if r["day"] == i), None)
        out.append({"time": local(start_ms + i * DAY_MS + 12 * HOUR_MS, study.tz).date().isoformat(),
                    "measured": measured[i], "modelled": modelled})
    return out


def backtest(study: Study) -> list[dict]:
    """Replay the city's own pickups on the model and compare the bin-hours full (80 % or more)
    and at capacity (95 % or more) with what the sensors read, over the hours the readings
    cover. Out of sample first (fill rates from the first half of the period, replayed over the
    second half from the levels read at its start: the honest test), then in sample (rates from
    the whole period). Each comes with the model's range (every bin at the slow and at the fast
    end of its fill rates) and, for the first, its days."""
    out = []
    known = study.known()
    measured = study.measured_pickups()
    tz = study.tz
    half = study.days // 2
    if half >= 7:
        second = study.start_ms + half * DAY_MS
        days2 = study.days - half
        first = Study(_cut(study.data, second), study.start_ms, half, tz)
        fitted = {m.bin.id: m for m in first.models}
        models = []
        for d in study.modelled():
            m = fitted.get(d.bin.id)
            lv = level_at(d.times, d.levels, second)
            if m and lv is not None:
                models.append(BinModel(d.bin, m.rate, m.leftover, lv))
        ids = {m.bin.id for m in models}
        known2 = {b: k[half * 24:] for b, k in known.items() if b in ids}
        picks2 = {b: [p for p in ps if p.t_ms >= second] for b, ps in measured.items() if b in ids}
        runs = {f: simulate(models, second, days2, Policy("measured"), shape_of=first.shape_of,
                            tz=tz, fill=f, measured=picks2, known=known2) for f in (-1.0, 0.0, 1.0)}
        row = _bt("Bin-hours full (80 % or more), out of sample",
                  _measured_total(study, ids, second, days2, FULL), runs[0.0].total("full_hours"),
                  [runs[-1.0].total("full_hours"), runs[1.0].total("full_hours")],
                  f"{_day(second, tz)} to {_day(study.end_ms - DAY_MS, tz)}",
                  f"Fill rates measured over the first {half} days only, for the {len(models)} bins "
                  f"they could be measured for; the city's own pickups in the last {days2} days "
                  "replayed on them from the levels read when those days began.", "out_of_sample")
        row["rows"] = _daily_rows(study, runs[0.0], second, days2, ids)
        out.append(row)
    runs = {f: simulate(study.models, study.start_ms, study.days, Policy("measured"),
                        shape_of=study.shape_of, tz=tz, fill=f, measured=measured, known=known)
            for f in (-1.0, 0.0, 1.0)}
    ids = {m.bin.id for m in study.models}
    period = f"{_day(study.start_ms, tz)} to {_day(study.end_ms - DAY_MS, tz)}"
    pooled = sum(1 for m in study.models if m.rate.basis == "pooled")
    row = _bt("Bin-hours full (80 % or more), in sample",
              _measured_total(study, ids, study.start_ms, study.days, FULL),
              runs[0.0].total("full_hours"),
              [runs[-1.0].total("full_hours"), runs[1.0].total("full_hours")], period,
              f"The city's {sum(len(v) for v in measured.values())} pickups replayed on fill rates "
              f"measured over the same days. {pooled} of {len(study.models)} bins were emptied "
              "at most once, so their range is borrowed from the bins emptied more often.",
              "in_sample")
    if not out:
        row["rows"] = _daily_rows(study, runs[0.0], study.start_ms, study.days, ids)
    out.append(row)
    out.append(_bt("Bin-hours at capacity (95 % or more), in sample",
                   _measured_total(study, ids, study.start_ms, study.days, AT_CAPACITY),
                   runs[0.0].total("capacity_hours"),
                   [runs[-1.0].total("capacity_hours"), runs[1.0].total("capacity_hours")], period,
                   "The model counts more hours at capacity than the sensors read: a full "
                   "container's sensor reads anywhere from about 85 to 100 as the heap under it "
                   "shifts, so readings dip below 95 while the model holds the bin at the top. "
                   "Read hours at capacity as an upper bound; between policies the comparison "
                   "holds, since both sides use the model.", "capacity"))
    return out


def _cut(data: list[BinData], end_ms: int) -> list[BinData]:
    out = []
    for d in data:
        keep = [(t, v) for t, v in zip(d.times, d.levels) if t < end_ms]
        out.append(BinData(d.bin, [t for t, _ in keep], [v for _, v in keep],
                           [p for p in d.pickups if p.t_ms < end_ms]))
    return out


def _day(t_ms: int, tz: ZoneInfo) -> str:
    t = local(t_ms, tz)
    return f"{t.day} {t.strftime('%b %Y')}"


def midnight_after(t_ms: int, tz: ZoneInfo) -> int:
    """The first local midnight at or after t."""
    t = local(t_ms, tz)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if day < t:
        day = (day + timedelta(days=1)).replace(tzinfo=tz)
    return int(day.replace(tzinfo=tz).timestamp() * 1000)


def midnight_before(t_ms: int, tz: ZoneInfo) -> int:
    t = local(t_ms, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(t.replace(tzinfo=tz).timestamp() * 1000)


def period_start(now_ms: int, days: int, tz: ZoneInfo) -> int:
    """The local midnight `days` whole days before today's."""
    today = local(now_ms, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days)
    return int(start.replace(tzinfo=tz).timestamp() * 1000)


__all__ = ["BinData", "Outcome", "STATUS_LABELS", "Study", "backtest", "baseline", "bin_data",
           "city_plans", "midnight_after", "midnight_before", "parse_bin", "parse_records",
           "period_start", "policy_outcome", "sensor_hours"]

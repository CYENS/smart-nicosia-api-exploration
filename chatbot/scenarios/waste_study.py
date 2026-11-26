"""The waste collection study: the city's bins as the twin's memory knows them over a period,
the measured baseline, each policy replayed on them, and the numbers a council would ask for.

`load` reads the bins and their readings from the memory (never the city API); `Study` turns
them into per-bin models; `evaluate` runs a policy three times (each bin filling at its lower,
central and upper rate), routes every round and returns the KPIs with ranges; `backtest`
replays the city's own pickups on the model and compares with what the sensors read.
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
    FillRate,
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
WEEKS_A_YEAR = 365.25 / 7
STREAMS = {"Glass": "glass", "Underground": "underground"}

STATUS_LABELS = {
    "modelled": "Modelled",
    "no_readings": "No readings in the period",
    "few_readings": "Too few readings",
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
    if max(inside) - min(inside) < STUCK_SPAN:
        return "stuck"
    emptied = any(start_ms <= p.t_ms < end_ms for p in d.pickups)
    if not emptied and sum(v >= FULL for v in inside) >= FULL_SHARE * len(inside):
        return "sat_full"
    if not d.rates:
        return "no_rate"
    return "modelled"


@dataclass
class Assumptions:
    """Every number the model takes on trust, with its range and where it comes from."""
    truck_m3: float = 20.0
    service_min: float = 4.0
    service_range: tuple[float, float] = (3.0, 6.0)
    unload_min: float = 20.0
    unload_range: tuple[float, float] = (15.0, 30.0)
    shift_h: float = 8.0
    fuel_l_per_km: float = 0.70
    fuel_range: tuple[float, float] = (0.57, 1.0)
    co2_kg_per_l: float = 2.68
    co2_range: tuple[float, float] = (2.51, 2.70)
    depot: tuple[float, float] = (35.1385, 33.3365)
    depot_name: str = "Strovolos industrial area"

    def crew(self) -> Crew:
        return Crew(self.truck_m3, self.service_min, self.unload_min, self.shift_h)


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


# -- measured -----------------------------------------------------------------------------------


@dataclass
class Outcome:
    """What a policy (or the city) did over the period, per bin and in total."""
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

    def hours(self, a: Assumptions, service_min: float | None = None,
              unload_min: float | None = None) -> float:
        s = a.service_min if service_min is None else service_min
        u = a.unload_min if unload_min is None else unload_min
        return self.drive_h + self.stops * s / 60 + self.trips * u / 60


def route_day(study: Study, matrix: Matrix, a: Assumptions,
              stops_by_stream: dict[str, list[tuple[Bin, float, float]]], t_ms: int) -> DayPlan:
    plan = DayPlan(t_ms)
    for stream, stops in sorted(stops_by_stream.items()):
        plan.trips.extend(plan_round(stream, stops, matrix, a.crew()))
    return plan


def plan_totals(plans: Sequence[DayPlan]) -> tuple[float, float, int, int]:
    km = sum(p.km for p in plans)
    drive_h = sum(t.drive_min for p in plans for t in p.trips) / 60
    stops = sum(len(t.stops) for p in plans for t in p.trips)
    trips = sum(len(p.trips) for p in plans)
    return km, drive_h, stops, trips


def city_plans(study: Study, matrix: Matrix, a: Assumptions) -> list[DayPlan]:
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
        t = int(datetime(day.year, day.month, day.day, 6, tzinfo=study.tz).timestamp() * 1000)
        plans.append(route_day(study, matrix, a,
                               {s: v for (dd, s), v in by_day.items() if dd == day}, t))
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


def baseline(study: Study, matrix: Matrix, a: Assumptions) -> dict[str, Outcome]:
    """The city's month on the model: its own pickups replayed on the bins' lower, central and
    upper fill rates, so the baseline and every policy share the model and its biases (the
    backtest says how far the model is from the sensors)."""
    plans = city_plans(study, matrix, a)
    policy = Policy("measured", study.measured_round_days())
    out = {}
    for which in ("low", "central", "high"):
        sim = simulate(study.models, study.start_ms, study.days, policy, shape_of=study.shape_of,
                       tz=study.tz, which=which, measured=study.measured_pickups())
        sim.plans = plans
        out[which] = sim_outcome(study, sim, policy)
    return out


def sim_outcome(study: Study, sim: Simulation, policy: Policy) -> Outcome:
    km, drive_h, stops, trips = plan_totals(sim.plans)
    per_bin = {bid: {"full_hours": o.full_hours, "capacity_hours": o.capacity_hours,
                     "collections": o.collections, "low_lifts": o.low_lifts}
               for bid, o in sim.bins.items()}
    bigger = {}
    for m in study.models:
        size = bigger_container(m, policy, STANDARD_SIZES)
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


def run_policy(study: Study, policy: Policy, matrix: Matrix, a: Assumptions) -> dict[str, Outcome]:
    """The policy with every bin at its lower, central and upper fill rate."""
    def planner(stream, stops, t_ms):
        return plan_round(stream, stops, matrix, a.crew())

    out = {}
    for which in ("low", "central", "high"):
        sim = simulate(study.models, study.start_ms, study.days, policy, shape_of=study.shape_of,
                       tz=study.tz, which=which, planner=planner, truck_m3=a.truck_m3)
        out[which] = sim_outcome(study, sim, policy)
    return out


# -- KPIs ---------------------------------------------------------------------------------------


def _r(v: float | None, nd: int = 1) -> float | None:
    if v is None:
        return None
    return round(v, nd) if nd else round(v)


def kpis(study: Study, base: Outcome, runs: dict[str, Outcome], a: Assumptions) -> list[dict]:
    """Per week (CO2 per year), with the scenario's range: the bins at their lower and upper
    fill rates, and the low and high end of every factor that enters the number."""
    wk = 7 / study.days
    c = runs["central"]
    variants = list(runs.values())

    def span(values: list[float]) -> tuple[float, float]:
        return min(values), max(values)

    def fuel(o: Outcome, f: float) -> float:
        return o.km * f * wk

    def row(id_, label, unit, b, s, lo_hi, better="lower", nd=1, note=None):
        lo, hi = lo_hi
        out = {"id": id_, "label": label, "unit": unit, "baseline": _r(b, nd), "scenario": _r(s, nd),
               "delta": _r(s - b, nd) if b is not None else None, "better": better,
               "low": _r(lo, nd), "high": _r(hi, nd)}
        if note:
            out["note"] = note
        return out

    hours_lo = [o.hours(a, a.service_range[0], a.unload_range[0]) * wk for o in variants]
    hours_hi = [o.hours(a, a.service_range[1], a.unload_range[1]) * wk for o in variants]
    fuel_lo = [fuel(o, a.fuel_range[0]) for o in variants]
    fuel_hi = [fuel(o, a.fuel_range[1]) for o in variants]
    co2 = lambda litres, k: litres * WEEKS_A_YEAR * k / 1000  # noqa: E731
    return [
        row("overflow_hours", "Bin-hours at capacity", "bin-hours a week",
            base.capacity_hours * wk, c.capacity_hours * wk,
            span([o.capacity_hours * wk for o in variants]), nd=0,
            note="Hours a bin read 95 % or more, when waste starts going beside it"),
        row("bins_overflowing", "Bins that reach capacity", "bins",
            base.bins_at_capacity, c.bins_at_capacity,
            span([o.bins_at_capacity for o in variants]), nd=0),
        row("full_hours", "Bin-hours full", "bin-hours a week", base.full_hours * wk,
            c.full_hours * wk, span([o.full_hours * wk for o in variants]), nd=0,
            note="Hours at 80 % or more, the city's alarm level"),
        row("collections", "Collections", "a week", base.collections * wk, c.collections * wk,
            span([o.collections * wk for o in variants]), nd=0),
        row("low_lifts", "Collections of bins under 30 % full", "a week", base.low_lifts * wk,
            c.low_lifts * wk, span([o.low_lifts * wk for o in variants]), nd=0),
        row("truck_km", "Truck kilometres", "km a week", base.km * wk, c.km * wk,
            span([o.km * wk for o in variants]), nd=0),
        row("driver_hours", "Driver hours", "hours a week", base.hours(a) * wk, c.hours(a) * wk,
            (min(hours_lo), max(hours_hi)), nd=1),
        row("fuel", "Diesel", "litres a week", fuel(base, a.fuel_l_per_km),
            fuel(c, a.fuel_l_per_km), (min(fuel_lo), max(fuel_hi)), nd=0),
        row("co2", "CO2 from the trucks", "tonnes a year", co2(fuel(base, a.fuel_l_per_km), a.co2_kg_per_l),
            co2(fuel(c, a.fuel_l_per_km), a.co2_kg_per_l),
            (co2(min(fuel_lo), a.co2_range[0]), co2(max(fuel_hi), a.co2_range[1])), nd=1),
        row("bigger_bins", "Bins that need a bigger container", "bins", len(base.bigger),
            len(c.bigger), span([len(o.bigger) for o in variants]), nd=0,
            note="Would reach capacity between two rounds even at a busy week's fill rate"),
    ]


# -- backtest -----------------------------------------------------------------------------------


def backtest(study: Study, data_for_fit: list[BinData] | None = None) -> list[dict]:
    """Replay the city's own pickups on the model and compare the hours full (80 % or more) and
    at capacity with what the sensors read, over the hours the readings cover.

    In sample: rates measured over the whole period. Out of sample: rates from the first half
    only, replayed over the second half from the levels read at its start (the honest test)."""
    out = []
    known = study.known()
    measured = study.measured_pickups()
    period = f"{_day(study.start_ms, study.tz)} to {_day(study.end_ms - DAY_MS, study.tz)}"
    meas_full = meas_cap = 0
    for d in study.modelled():
        k, vals = measured_hours(d.times, d.levels, study.start_ms, study.days * 24)
        meas_full += sum(1 for v in vals if v is not None and v >= FULL)
        meas_cap += sum(1 for v in vals if v is not None and v >= AT_CAPACITY)
    replay = {w: simulate(study.models, study.start_ms, study.days, Policy("measured"),
                          shape_of=study.shape_of, tz=study.tz, which=w, measured=measured,
                          known=known)
              for w in ("low", "central", "high")}
    pooled = sum(1 for m in study.models if m.rate.basis == "pooled")
    out.append(_bt("Bin-hours full (80 % or more), in sample", meas_full,
                   replay["central"].total("full_hours"),
                   [replay[w].total("full_hours") for w in ("low", "high")], period,
                   f"The city's {sum(len(v) for v in measured.values())} pickups replayed on fill "
                   f"rates measured over the same weeks. {pooled} of {len(study.models)} bins have "
                   "one stretch between emptyings, so their range is borrowed from the others."))
    out.append(_bt("Bin-hours at capacity (95 % or more), in sample", meas_cap,
                   replay["central"].total("capacity_hours"),
                   [replay[w].total("capacity_hours") for w in ("low", "high")], period,
                   "Capacity hours hinge on the last hours before a pickup, so they swing more "
                   "than hours full."))
    half = study.days // 2
    if half >= 7:
        first = Study(_cut(study.data, study.start_ms, study.start_ms + half * DAY_MS),
                      study.start_ms, half, study.tz)
        second_start = study.start_ms + half * DAY_MS
        days2 = study.days - half
        fitted = {m.bin.id: m for m in first.models}
        models = []
        for d in study.modelled():
            m = fitted.get(d.bin.id)
            lv = level_at(d.times, d.levels, second_start)
            if m and lv is not None:
                models.append(BinModel(d.bin, m.rate, m.leftover, lv))
        ids = {m.bin.id for m in models}
        known2 = {bid: k[half * 24:] for bid, k in known.items() if bid in ids}
        picks2 = {bid: [p for p in ps if p.t_ms >= second_start] for bid, ps in measured.items()
                  if bid in ids}
        sim = simulate(models, second_start, days2, Policy("measured"), shape_of=first.shape_of,
                       tz=study.tz, measured=picks2, known=known2)
        lo_hi = [simulate(models, second_start, days2, Policy("measured"), shape_of=first.shape_of,
                          tz=study.tz, which=w, measured=picks2, known=known2).total("full_hours")
                 for w in ("low", "high")]
        meas2 = 0
        for d in study.modelled():
            if d.bin.id in ids:
                _, vals = measured_hours(d.times, d.levels, second_start, days2 * 24)
                meas2 += sum(1 for v in vals if v is not None and v >= FULL)
        out.append(_bt("Bin-hours full (80 % or more), out of sample", meas2,
                       sim.total("full_hours"), lo_hi,
                       f"{_day(second_start, study.tz)} to {_day(study.end_ms - DAY_MS, study.tz)}",
                       f"Fill rates from the first {half} days only, for the {len(models)} bins "
                       "they could be measured for; the city's pickups in the last "
                       f"{days2} days replayed on them."))
    return out


def _bt(metric: str, measured: float, modelled: float, lo_hi: list[float], period: str,
        note: str) -> dict:
    err = None if not measured else round(100 * (modelled - measured) / measured, 1)
    return {"metric": metric, "measured": round(measured), "modelled": round(modelled),
            "low": round(min(lo_hi + [modelled])), "high": round(max(lo_hi + [modelled])),
            "error_pct": err, "period": period, "note": note}


def _cut(data: list[BinData], start_ms: int, end_ms: int) -> list[BinData]:
    out = []
    for d in data:
        keep = [(t, v) for t, v in zip(d.times, d.levels) if t < end_ms]
        out.append(BinData(d.bin, [t for t, _ in keep], [v for _, v in keep],
                           [p for p in d.pickups if p.t_ms < end_ms]))
    return out


def _day(t_ms: int, tz: ZoneInfo) -> str:
    t = local(t_ms, tz)
    return f"{t.day} {t.strftime('%b %Y')}"


def period_start(now_ms: int, days: int, tz: ZoneInfo) -> int:
    """The local midnight `days` whole days before today's."""
    today = local(now_ms, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days)
    return int(start.replace(tzinfo=tz).timestamp() * 1000)


__all__ = ["Assumptions", "BinData", "bin_data", "FillRate", "Outcome", "STATUS_LABELS", "Study",
           "backtest", "baseline", "kpis", "sensor_hours", "parse_bin", "parse_records", "period_start",
           "run_policy"]

"""The waste collection model: what the twin's memory says about each bin, and collection
policies replayed hour by hour on it.

Pure functions on plain data, so every step can be tested on fixtures:

- `detect_pickups` finds the emptyings of a bin: the bin's own pickup reports (`pickup_time`,
  with the level before and after) and falls in its level readings that the next reading
  confirms (a single low reading between two high ones is the sensor, not a truck).
- `fill_rate` measures how fast a bin fills, in % of its container a day, from the stretches
  between emptyings (the net rise over each, between medians of three readings so one jumpy
  reading can't swing it), with a range from how much the stretches differ.
- `weekly_shape` is when bins fill: an hour-of-day and a weekday factor per stream, pooled over
  every bin of the stream and shrunk towards flat where the readings are noisy.
- `simulate` replays a policy over a period: levels rise at the modelled rate, a collection
  empties a bin to its usual leftover, and every hour at 80 % or more (the city's alarm level)
  or at capacity (95 % or more: a full container reads 95 to 100) is counted.
- `measured_hours` counts the same hours in the readings themselves, for the baseline and the
  backtest.

Levels are % of the container; times are epoch milliseconds; the city's clock (Asia/Nicosia)
decides weekdays and hours.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
FULL = 80.0  # the city's alarm level (collector.interpret.BIN_BANDS)
AT_CAPACITY = 95.0  # a full container reads 95 to 100; beyond, waste goes beside it
LOW_LIFT = 30.0  # a collection of a bin below this is a lift that could have waited
STALE_MS = 6 * HOUR_MS  # a reading speaks for the hours after it for this long (bins report every 2 h)

# Pickup detection: a fall of at least DROP points to AFTER_MAX or below, or of BIG_DROP points to
# anything (some containers read 40 % just after a lift), from the lower of the two readings
# before, which the next reading confirms (it stays within CONFIRM_SLACK of the low one).
DROP = 25.0
AFTER_MAX = 25.0
BIG_DROP = 35.0
CONFIRM_SLACK = 15.0
MIN_GAP_MS = 6 * HOUR_MS  # two emptyings of one bin closer than this are one
# The city's own pickup reports fall between 06:00 and 13:00 (92 of 96 in November 2026); a fall
# at night is the sensor more often than a truck, so only falls in these hours count.
WORK_HOURS = (5, 15)
SAME_EVENT_MS = 4 * HOUR_MS  # a level drop this close to a reported pickup is that pickup

# Fill rates rest on readings below this (the sensor saturates near the top) ...
RATE_MAX_LEVEL = 90.0
# ... on stretches between emptyings with at least this many readings over this long.
STRETCH_MIN_POINTS = 6
STRETCH_MIN_MS = 24 * HOUR_MS
SHAPE_TAU = 0.5  # how far real hour and weekday factors are expected to stray from 1


# -- data ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bin:
    id: str
    name: str
    lat: float
    lon: float
    stream: str  # "glass" or "underground": collected by different trucks
    volume_l: float


@dataclass(frozen=True)
class Pickup:
    t_ms: int
    before: float | None
    after: float | None
    source: str  # "sensor": the bin's own report; "level": a confirmed fall in its readings


@dataclass(frozen=True)
class FillRate:
    per_day: float  # % of the container a day, central
    low: float
    high: float
    stretches: int  # stretches between emptyings it rests on
    basis: str  # "stretches": range from their spread; "pooled": one stretch, range borrowed


@dataclass
class BinModel:
    bin: Bin
    rate: FillRate
    leftover: float  # the level a collection leaves (glass shards, a stuck bag), median measured
    start_level: float


@dataclass(frozen=True)
class Shape:
    """When bins fill, as factors with mean 1: by two-hour block of the day and by weekday."""
    hour: tuple[float, ...]  # 12 two-hour blocks, 00:00 to 22:00
    weekday: tuple[float, ...]  # Monday to Sunday
    readings: int = 0

    def factor(self, weekday: int, hour: int) -> float:
        return self.hour[hour // 2] * self.weekday[weekday]

    @staticmethod
    def flat() -> Shape:
        return Shape((1.0,) * 12, (1.0,) * 7)


def clean(times: Sequence[int], levels: Sequence[float | None]) -> tuple[list[int], list[float]]:
    """Readings in time order, one per moment, levels clamped to 0..100, gaps dropped."""
    seen: dict[int, float] = {}
    for t, v in zip(times, levels):
        if v is None or isinstance(v, bool) or not math.isfinite(float(v)):
            continue
        seen[int(t)] = min(100.0, max(0.0, float(v)))
    ts = sorted(seen)
    return ts, [seen[t] for t in ts]


# -- pickups ------------------------------------------------------------------------------------


def detect_pickups(times: Sequence[int], levels: Sequence[float],
                   reported: Iterable[Pickup] = (), *, tz: ZoneInfo | None = None,
                   hours: tuple[int, int] = WORK_HOURS) -> list[Pickup]:
    """The emptyings of one bin, oldest first: its own reports, and the falls in its readings
    that no report covers. With `tz`, only those between `hours` (local, start to end) count."""

    def working(t_ms: int) -> bool:
        return tz is None or hours[0] <= local(t_ms, tz).hour < hours[1]

    out = sorted((p for p in reported if working(p.t_ms)), key=lambda p: p.t_ms)
    drops: list[Pickup] = []
    for i in range(1, len(times)):
        before = min(levels[max(0, i - 2):i])  # the lower of the two before: one spike up can't fake it
        now = levels[i]
        fall = before - now
        if not ((fall >= DROP and now <= AFTER_MAX) or fall >= BIG_DROP):
            continue
        nxt = levels[i + 1] if i + 1 < len(levels) else None
        if nxt is not None and nxt > now + CONFIRM_SLACK:
            continue  # a spike down, not an emptying
        if drops and times[i] - drops[-1].t_ms < MIN_GAP_MS:
            continue
        if not working(times[i]):
            continue
        drops.append(Pickup(int(times[i]), before, now, "level"))
    for d in drops:
        if not any(abs(d.t_ms - p.t_ms) < SAME_EVENT_MS for p in out):
            out.append(d)
    return sorted(out, key=lambda p: p.t_ms)


def level_at(times: Sequence[int], levels: Sequence[float], t_ms: int) -> float | None:
    """The newest reading at or before t (within STALE_MS), else the first one after it."""
    i = bisect.bisect_right(times, t_ms) - 1
    if i >= 0 and t_ms - times[i] <= STALE_MS:
        return levels[i]
    if i + 1 < len(times) and times[i + 1] - t_ms <= STALE_MS:
        return levels[i + 1]
    return None


# -- fill rates ---------------------------------------------------------------------------------


def stretches(times: Sequence[int], levels: Sequence[float],
              pickups: Sequence[Pickup]) -> list[list[tuple[int, float]]]:
    """The readings of each stretch between emptyings while the bin fills: from an emptying to
    the second of two readings in a row at RATE_MAX_LEVEL or more (past it the sensor sits at
    its top and says nothing more about the rate), when long enough to show the filling."""
    cuts = [p.t_ms for p in pickups]
    out: list[list[tuple[int, float]]] = []
    cur: list[tuple[int, float]] = []
    k = 0
    capped = False
    for t, v in zip(times, levels):
        while k < len(cuts) and t >= cuts[k]:
            out.append(cur)
            cur, capped = [], False
            k += 1
        if capped:
            continue
        cur.append((t, v))
        # at the top: two readings in a row at RATE_MAX_LEVEL or more (one alone is a spike)
        capped = len(cur) >= 2 and v >= RATE_MAX_LEVEL and cur[-2][1] >= RATE_MAX_LEVEL
    out.append(cur)
    return [s for s in out
            if len(s) >= STRETCH_MIN_POINTS and s[-1][0] - s[0][0] >= STRETCH_MIN_MS]


def net_rise(points: Sequence[tuple[int, float]]) -> float:
    """% a day from the start of a stretch to its end: the median of its first three readings
    to the median of its last three. One jumpy reading can't swing it, and a crate of bottles
    dropped in at once counts in full (a robust slope would treat that step as an outlier and
    say the bin fills slower than it does)."""
    head, tail = points[:3], points[-3:]
    v0 = statistics.median(v for _, v in head)
    t0 = statistics.median(t for t, _ in head)
    v1 = statistics.median(v for _, v in tail)
    t1 = statistics.median(t for t, _ in tail)
    if t1 <= t0:
        return 0.0
    return max(0.0, (v1 - v0) / ((t1 - t0) / DAY_MS))


def stretch_rates(times: Sequence[int], levels: Sequence[float],
                  pickups: Sequence[Pickup]) -> list[tuple[float, float]]:
    """(% a day, days long) for each stretch; a stretch that ends lower than it starts (noise,
    settling) counts as 0."""
    return [(net_rise(s), (s[-1][0] - s[0][0]) / DAY_MS)
            for s in stretches(times, levels, pickups)]


def weighted_median(values: Sequence[tuple[float, float]]) -> float:
    """Median of (value, weight) pairs."""
    pts = sorted(values)
    total = sum(w for _, w in pts)
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= total / 2:
            return v
    return pts[-1][0]


def fill_rate(rates: Sequence[tuple[float, float]], pooled_spread: float) -> FillRate | None:
    """A bin's rate from its stretches: the duration-weighted median, and the lower and upper
    quartile of the stretches when there are three or more. With fewer, the range is the
    central rate times (1 -/+ pooled_spread), the relative half-spread of the bins that have
    three or more."""
    if not rates:
        return None
    central = weighted_median(rates)
    if len(rates) >= 3:
        qs = statistics.quantiles([r for r, _ in rates], n=4, method="inclusive")
        low, high = min(qs[0], central), max(qs[2], central)
        return FillRate(central, low, high, len(rates), "stretches")
    return FillRate(central, central * (1 - pooled_spread), central * (1 + pooled_spread),
                    len(rates), "pooled")


def pooled_spread(all_rates: Iterable[Sequence[tuple[float, float]]], default: float = 0.5) -> float:
    """Median relative half-spread (half the interquartile range over the median) of the bins
    with three stretches or more; `default` when there are none."""
    spreads = []
    for rates in all_rates:
        if len(rates) < 3:
            continue
        vals = [r for r, _ in rates]
        med = statistics.median(vals)
        if med <= 0.2:  # a bin that barely fills says nothing about relative spread
            continue
        q = statistics.quantiles(vals, n=4, method="inclusive")
        spreads.append((q[2] - q[0]) / 2 / med)
    return min(0.9, statistics.median(spreads)) if spreads else default


# -- when bins fill -----------------------------------------------------------------------------


def weekly_shape(series: Iterable[tuple[Sequence[int], Sequence[float], Sequence[Pickup]]],
                 tz: ZoneInfo, tau: float = SHAPE_TAU) -> Shape:
    """Hour-of-day and weekday factors from every rise and fall between consecutive readings of
    the bins of one stream (both below RATE_MAX_LEVEL, at most 4 hours apart, no emptying
    between): the summed change over the summed time in each block, over the stream's overall
    rate. Each factor is pulled towards 1 by how unsure it is (its standard error against a
    spread of `tau` between real factors), so noisy sensors give a flat week rather than a
    made-up one; then the two are scaled so their product averages 1."""
    hour: list[list[tuple[float, float]]] = [[] for _ in range(12)]
    week: list[list[tuple[float, float]]] = [[] for _ in range(7)]
    total = 0
    for times, levels, pickups in series:
        cuts = [p.t_ms for p in pickups]
        for i in range(1, len(times)):
            t0, t1 = times[i - 1], times[i]
            if t1 - t0 > 4 * HOUR_MS or t1 <= t0:
                continue
            if levels[i] >= RATE_MAX_LEVEL or levels[i - 1] >= RATE_MAX_LEVEL:
                continue
            k = bisect.bisect_right(cuts, t0)
            if k < len(cuts) and cuts[k] <= t1:
                continue
            mid = local((t0 + t1) // 2, tz)
            inc = (levels[i] - levels[i - 1], (t1 - t0) / HOUR_MS)
            hour[mid.hour // 2].append(inc)
            week[mid.weekday()].append(inc)
            total += 1
    sum_t = sum(dh for block in hour for _, dh in block)
    overall = sum(dv for block in hour for dv, _ in block) / sum_t if sum_t else 0.0
    if overall <= 0:
        return Shape(Shape.flat().hour, Shape.flat().weekday, total)

    def factors(blocks: list[list[tuple[float, float]]]) -> list[float]:
        out = []
        for incs in blocks:
            st = sum(dh for _, dh in incs)
            if not st:
                out.append(1.0)
                continue
            r = sum(dv for dv, _ in incs) / st
            se = math.sqrt(sum((dv - r * dh) ** 2 for dv, dh in incs)) / st / overall
            raw = r / overall
            out.append(max(0.05, 1 + (raw - 1) * tau ** 2 / (tau ** 2 + se ** 2)))
        mean = sum(out) / len(out)
        return [f / mean for f in out]

    h = factors(hour)
    w = factors(week)
    grid_mean = sum(a * b for a in h for b in w) / (len(h) * len(w))
    k = 1 / math.sqrt(grid_mean)
    return Shape(tuple(round(a * k, 4) for a in h), tuple(round(b * k, 4) for b in w), total)


# -- policies -----------------------------------------------------------------------------------


ROUND_DAYS = {  # rounds a week -> weekdays (0 = Monday), spread as evenly as a week allows
    1: (0,), 2: (0, 3), 3: (0, 2, 4), 4: (0, 1, 3, 4), 5: (0, 1, 2, 3, 4),
    6: (0, 1, 2, 3, 4, 5), 7: (0, 1, 2, 3, 4, 5, 6),
}


@dataclass(frozen=True)
class Policy:
    kind: str  # "measured" | "fixed" | "threshold" | "forecast"
    round_days: tuple[int, ...] = ROUND_DAYS[2]
    threshold: float = 70.0  # threshold: collect bins at or above this
    forecast_level: float = 90.0  # forecast: collect bins expected to pass this before the next round
    start_hour: int = 6  # rounds leave the depot at this hour, local time

    def max_gap_days(self) -> int:
        """The longest wait between two round days, in days."""
        days = sorted(set(self.round_days))
        if not days:
            return 7
        return max(((days[(i + 1) % len(days)] - d) % 7) or 7 for i, d in enumerate(days))


@dataclass
class Stop:
    bin_id: str
    eta_min: float  # minutes after the round leaves the depot
    level: float  # % when the truck arrives (as decided at the start of the round)
    load_m3: float


@dataclass
class Trip:
    stream: str
    truck: int
    stops: list[Stop]
    start_min: float  # minutes after the round leaves the depot
    end_min: float
    drive_km: float
    drive_min: float
    service_min: float
    unload_min: float


@dataclass
class DayPlan:
    t_ms: int  # when the round left the depot
    trips: list[Trip] = field(default_factory=list)

    @property
    def km(self) -> float:
        return sum(t.drive_km for t in self.trips)

    @property
    def hours(self) -> float:
        return sum(t.drive_min + t.service_min + t.unload_min for t in self.trips) / 60


# planner(stream, [(bin, level, load_m3)], t_ms) -> trips; supplied by rounds.py (or a test)
Planner = Callable[[str, list[tuple[Bin, float, float]], int], list[Trip]]


@dataclass
class BinOutcome:
    full_hours: int = 0
    capacity_hours: int = 0
    collections: int = 0
    low_lifts: int = 0
    known_hours: int = 0  # hours the comparison counts (all, or those the readings cover)


@dataclass
class Simulation:
    policy: Policy
    start_ms: int
    days: int
    bins: dict[str, BinOutcome]
    plans: list[DayPlan]
    daily: list[dict]  # per day: bins_full, collections, km, hours

    def total(self, attr: str) -> int:
        return sum(getattr(o, attr) for o in self.bins.values())


def rate_at(rate: FillRate, fill: float) -> float:
    """% a day at `fill` within the bin's range: -1 its low end, 0 its usual rate, 1 its high end,
    in a straight line between."""
    f = max(-1.0, min(1.0, fill))
    return rate.per_day + f * ((rate.high - rate.per_day) if f > 0 else (rate.per_day - rate.low))


def hour_factors(start_ms: int, hours: int, shape: Shape, tz: ZoneInfo) -> list[float]:
    out = []
    for h in range(hours):
        t = datetime.fromtimestamp((start_ms + h * HOUR_MS) / 1000, tz=timezone.utc).astimezone(tz)
        out.append(shape.factor(t.weekday(), t.hour))
    return out


def local(t_ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).astimezone(tz)


def round_starts(start_ms: int, days: int, policy: Policy, tz: ZoneInfo) -> list[int]:
    """The moments rounds leave the depot within [start, start + days)."""
    out = []
    first = local(start_ms, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days + 1):
        day = datetime.fromordinal(first.toordinal() + d).replace(tzinfo=tz)
        if day.weekday() not in policy.round_days:
            continue
        t = int(day.replace(hour=policy.start_hour).timestamp() * 1000)
        if start_ms <= t < start_ms + days * DAY_MS:
            out.append(t)
    return out


def simulate(models: Sequence[BinModel], start_ms: int, days: int, policy: Policy, *,
             shape_of: Callable[[str], Shape], tz: ZoneInfo, fill: float = 0.0,
             planner: Planner | None = None,
             measured: dict[str, Sequence[Pickup]] | None = None,
             known: dict[str, Sequence[bool]] | None = None,
             truck_m3: float = 20.0) -> Simulation:
    """Replay `policy` hour by hour over `days` from `start_ms`.

    `fill` places each bin's real fill rate within its measured range (-1 its slow end, 0 its
    usual rate, 1 its fast end); a forecast policy always predicts with the usual rate, as a
    real planner would, so the slow and fast runs also show what a wrong forecast costs. `measured` (policy "measured") gives the city's
    own emptyings to replay. `known`, per bin and hour, limits the counted hours to those the
    readings cover (for comparing with them). `planner` routes each round; without one, stops
    are collected at the round's start."""
    hours = days * 24
    factors: dict[str, list[float]] = {}
    for m in models:
        if m.bin.stream not in factors:
            factors[m.bin.stream] = hour_factors(start_ms, hours, shape_of(m.bin.stream), tz)

    def per_hour(m: BinModel, at: float = fill) -> float:
        return rate_at(m.rate, at) / 24

    level = {m.bin.id: m.start_level for m in models}
    out = {m.bin.id: BinOutcome() for m in models}
    by_id = {m.bin.id: m for m in models}
    starts = round_starts(start_ms, days, policy, tz) if policy.kind != "measured" else []
    start_hours = {int((t - start_ms) // HOUR_MS): t for t in starts}
    next_round = {}  # hour index -> hour index of the following round
    sorted_rounds = sorted(start_hours)
    for i, h in enumerate(sorted_rounds):
        next_round[h] = sorted_rounds[i + 1] if i + 1 < len(sorted_rounds) else h + 24 * policy.max_gap_days()
    empties: dict[int, list[str]] = {}  # hour index -> bins emptied in it
    if policy.kind == "measured":
        for bid, picks in (measured or {}).items():
            if bid not in by_id:
                continue
            for p in picks:
                h = int((p.t_ms - start_ms) // HOUR_MS)
                if 0 <= h < hours:
                    empties.setdefault(h, []).append(bid)
    plans: list[DayPlan] = []
    daily: list[dict] = []
    day_full = 0
    day_collections = 0

    for h in range(hours):
        if h in start_hours:
            chosen = choose(policy, models, level, h, next_round[h], factors, per_hour)
            plan = DayPlan(start_hours[h])
            by_stream: dict[str, list[tuple[Bin, float, float]]] = {}
            for bid in chosen:
                m = by_id[bid]
                by_stream.setdefault(m.bin.stream, []).append(
                    (m.bin, level[bid], m.bin.volume_l / 1000 * level[bid] / 100))
            for stream, stops in sorted(by_stream.items()):
                if planner:
                    plan.trips.extend(planner(stream, stops, start_hours[h]))
                else:
                    plan.trips.append(Trip(stream, 0, [Stop(b.id, 0, lv, ld) for b, lv, ld in stops],
                                           0, 0, 0, 0, 0, 0))
            for trip in plan.trips:
                for s in trip.stops:
                    empties.setdefault(h + int(s.eta_min // 60), []).append(s.bin_id)
            plans.append(plan)
        for bid in empties.pop(h, ()):  # collected during this hour
            m = by_id[bid]
            o = out[bid]
            o.collections += 1
            o.low_lifts += level[bid] < LOW_LIFT
            day_collections += 1
            level[bid] = m.leftover
        for m in models:
            bid = m.bin.id
            lv = min(100.0, level[bid] + per_hour(m) * factors[m.bin.stream][h])
            level[bid] = lv
            if known is not None:
                kb = known.get(bid)
                if kb is None or h >= len(kb) or not kb[h]:
                    continue
            o = out[bid]
            o.known_hours += 1
            if lv >= FULL:
                o.full_hours += 1
                day_full += 1
            if lv >= AT_CAPACITY:
                o.capacity_hours += 1
        if h % 24 == 23:
            daily.append({"day": h // 24, "full_hours": day_full, "collections": day_collections})
            day_full = day_collections = 0
    for d in daily:
        day_plans = [p for p in plans if int((p.t_ms - start_ms) // DAY_MS) == d["day"]]
        d["km"] = round(sum(p.km for p in day_plans), 2)
        d["hours"] = round(sum(p.hours for p in day_plans), 2)
    return Simulation(policy, start_ms, days, out, plans, daily)


def choose(policy: Policy, models: Sequence[BinModel], level: dict[str, float], h: int,
           next_h: int, factors: dict[str, list[float]],
           per_hour: Callable[..., float]) -> list[str]:
    """The bins a round collects, decided as it leaves the depot."""
    if policy.kind == "fixed":
        return [m.bin.id for m in models]
    if policy.kind == "threshold":
        return [m.bin.id for m in models if level[m.bin.id] >= policy.threshold]
    if policy.kind == "forecast":
        out = []
        for m in models:
            f = factors[m.bin.stream]
            rate = per_hour(m, 0.0)
            # the level the bin would reach by the next round has emptied it (its start plus
            # the usual few hours on the road), forecast with the central rate
            ahead = sum(f[k % len(f)] for k in range(h, next_h + 4))
            if level[m.bin.id] + rate * ahead >= policy.forecast_level:
                out.append(m.bin.id)
        return out
    return []


# -- the readings themselves --------------------------------------------------------------------


def measured_hours(times: Sequence[int], levels: Sequence[float], start_ms: int,
                   hours: int) -> tuple[list[bool], list[float | None]]:
    """Per hour from start: whether a reading covers it (the newest within STALE_MS), and the
    level it reads."""
    known, vals = [], []
    for h in range(hours):
        t = start_ms + (h + 1) * HOUR_MS - 1  # the hour's last moment, as simulate counts it
        i = bisect.bisect_right(times, t) - 1
        if i >= 0 and t - times[i] <= STALE_MS:
            known.append(True)
            vals.append(levels[i])
        else:
            known.append(False)
            vals.append(None)
    return known, vals


def bigger_container(m: BinModel, policy: Policy, sizes: Sequence[float],
                     fill: float = 1.0) -> float | None:
    """The container a bin would need so that, collected at every round, it does not reach
    capacity before the next one: None when its own is big enough. The fill over the longest gap
    between round days uses the bin's rate at `fill` (by default its fast end: a busy week, not
    an average one); the answer is the smallest standard size that holds it, or the fill itself
    rounded up to 100 litres when none does."""
    days = policy.max_gap_days()
    rise = rate_at(m.rate, fill) * days
    room = AT_CAPACITY - m.leftover
    if rise <= room:
        return None
    need_l = m.bin.volume_l * rise / room
    for s in sorted(sizes):
        if s >= need_l:
            return s
    return math.ceil(need_l / 100) * 100

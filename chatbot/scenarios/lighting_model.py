"""The lighting scenario's model, apart from the lab's plumbing: the sun's times, what each lamp
does today (learnt from its own readings), and what a policy would make it do over a year.

What the lamps tell the twin (checked on the memory, November 2026):

- `powerstats` carries the power at that moment (`last_pwr_act_avg`, W) and a cumulative energy
  counter (`last_energy_act`) that counts watt-hours: it rises 5 per 10 minutes at 29 W.
- `level` is 100 when lit and 0 when powered but dark; no lamp has ever reported a level between.
- About 50 lamps (on NEMA sockets) report every 10 minutes, day and night, and draw about 0.5 to
  0.8 W while dark. The rest report about hourly, only while powered.

So a lamp's switching times come from the readings either side of each switch where they are
close together (within 35 minutes), else from its energy counter across the silent day (how
long it was lit between its last morning reading and its first evening one, split between dawn
and dusk the way the closely read lamps split it), else from the closely read lamps' pattern.
Times are kept as minutes after sunset and after sunrise, so a lamp's schedule carries over to
every night of the year.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from collector import sun

TZ = ZoneInfo("Asia/Nicosia")
SUNSET_DEG = sun.SUNSET_ELEVATION_DEG  # -0.833
CIVIL_DEG = -6.0
LIT_W = 3.0  # above this a lamp is lit; below, a controller on standby
CLOSE_S = 35 * 60  # readings either side of a switch this close give its time
WINDOW_EVENING = (-3 * 3600, 4 * 3600)  # where a dusk switch is looked for, around sunset
WINDOW_MORNING = (-4 * 3600, 3 * 3600)  # and a dawn switch, around sunrise
MIN_NIGHTS = 3  # nights of evidence before a lamp's own times are used


# -- the sun -----------------------------------------------------------------------------------


def _local(d: date, hour: int, minute: int = 0) -> float:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ).timestamp()


def _cross(lo: float, hi: float, deg: float, rising: bool) -> float:
    """When the sun's elevation crosses `deg` between lo and hi (epoch s), to 10 s."""
    def above(t: float) -> bool:
        return sun.solar_elevation(datetime.fromtimestamp(t, timezone.utc)) > deg

    for _ in range(30):
        if hi - lo <= 10:
            break
        mid = (lo + hi) / 2
        if above(mid) == rising:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


@dataclass(frozen=True)
class Night:
    """The night that starts on the evening of `day` (local), times in epoch seconds."""
    day: date
    sunset: float
    sunrise: float
    dusk: float  # civil dusk: the sun 6° below the horizon
    dawn: float  # civil dawn

    @property
    def hours(self) -> float:
        return (self.sunrise - self.sunset) / 3600


@lru_cache(maxsize=2048)
def night(day: date) -> Night:
    nxt = day + timedelta(days=1)
    eve_lo, eve_hi = _local(day, 14), _local(day, 22)
    mor_lo, mor_hi = _local(nxt, 3), _local(nxt, 10)
    return Night(day, _cross(eve_lo, eve_hi, SUNSET_DEG, False), _cross(mor_lo, mor_hi, SUNSET_DEG, True),
                 _cross(eve_lo, eve_hi, CIVIL_DEG, False), _cross(mor_lo, mor_hi, CIVIL_DEG, True))


def nights_between(start: float, end: float) -> list[Night]:
    """Every night whose evening falls on a local date from the day before start to end."""
    d = datetime.fromtimestamp(start, TZ).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, TZ).date()
    out = []
    while d <= last:
        out.append(night(d))
        d += timedelta(days=1)
    return out


def clock_at(n: Night, hhmm: str) -> float:
    """A clock time of the night (18:00 is the evening of n.day, 05:00 the next morning)."""
    h, m = map(int, hhmm.split(":"))
    return _local(n.day if h >= 12 else n.day + timedelta(days=1), h, m)


def overlap(a1: float, b1: float, a2: float, b2: float) -> float:
    return max(0.0, min(b1, b2) - max(a1, a2))


# -- what a lamp does today --------------------------------------------------------------------


@dataclass
class Readings:
    """One lamp's readings in a period, oldest first: (t, lit) from its level and power, and
    (t, W, Wh counter) from its power stats."""
    states: list[tuple[float, bool]] = field(default_factory=list)
    power: list[tuple[float, float | None, float | None]] = field(default_factory=list)

    def before(self, t: float) -> Readings:
        return Readings([s for s in self.states if s[0] < t], [p for p in self.power if p[0] < t])


@dataclass
class Schedule:
    watts: float | None  # at full light (the median of its lit readings)
    standby: float  # while powered and dark (0 when it is silent by day)
    on_min: float  # minutes after sunset it switches on (negative: before)
    off_min: float  # minutes after sunrise it switches off
    on_basis: str  # measured | meter | city
    off_basis: str
    nights: int  # nights with any reading

    @property
    def basis(self) -> str:
        if self.on_basis == self.off_basis == "measured":
            return "measured"
        return "meter" if "meter" in (self.on_basis, self.off_basis) else (
            "measured" if "measured" in (self.on_basis, self.off_basis) else "city")


def wattage(r: Readings) -> tuple[float | None, float, bool]:
    """(full power, standby power, whether standby was seen)."""
    lit = [w for _, w, _ in r.power if w is not None and w > LIT_W]
    dark = [w for _, w, _ in r.power if w is not None and w <= LIT_W]
    full = statistics.median(lit) if lit else None
    if len(dark) >= 3:
        return full, statistics.median(dark), True
    return full, 0.0, False


def _switches(r: Readings, nights: list[Night]) -> tuple[list[float], list[float]]:
    """Switch-on minutes after sunset and switch-off minutes after sunrise, for the nights where
    readings either side of the switch are close together."""
    ons, offs = [], []
    st = r.states
    if not st:
        return ons, offs
    times = [t for t, _ in st]
    for n in nights:
        lo, hi = n.sunset + WINDOW_EVENING[0], n.sunset + WINDOW_EVENING[1]
        i = bisect.bisect_left(times, lo)
        while i < len(st) and st[i][0] <= hi and not st[i][1]:
            i += 1
        if 0 < i < len(st) and st[i][0] <= hi and st[i][1] and not st[i - 1][1] \
                and st[i][0] - st[i - 1][0] <= CLOSE_S:
            ons.append(((st[i][0] + st[i - 1][0]) / 2 - n.sunset) / 60)
        lo, hi = n.sunrise + WINDOW_MORNING[0], n.sunrise + WINDOW_MORNING[1]
        j = bisect.bisect_right(times, hi) - 1
        while j >= 0 and st[j][0] >= lo and not st[j][1]:
            j -= 1
        if 0 <= j < len(st) - 1 and st[j][0] >= lo and st[j][1] and not st[j + 1][1] \
                and st[j + 1][0] - st[j][0] <= CLOSE_S:
            offs.append(((st[j][0] + st[j + 1][0]) / 2 - n.sunrise) / 60)
    return ons, offs


def _meter_splits(r: Readings, nights: list[Night], watts: float, standby: float,
                  city_on: float, city_off: float) -> tuple[list[float], list[float]]:
    """Across each silent day, the counter says how long the lamp was lit after its last morning
    reading and before its first evening one; split that between dawn and dusk as the city's
    closely read lamps do."""
    ons, offs = [], []
    lit = [(t, e) for t, w, e in r.power if e is not None and w is not None and w > LIT_W]
    if len(lit) < 2 or watts <= LIT_W:
        return ons, offs
    by_night = {n.day: n for n in nights}
    for (t1, e1), (t2, e2) in zip(lit, lit[1:]):
        gap = t2 - t1
        if gap < 6 * 3600 or gap > 20 * 3600:
            continue  # not a day between a morning and an evening
        d_morning = datetime.fromtimestamp(t1, TZ).date() - timedelta(days=1)
        d_evening = datetime.fromtimestamp(t2, TZ).date()
        a, b = by_night.get(d_morning), by_night.get(d_evening)
        if a is None or b is None or not (a.sunrise - 5 * 3600 < t1 < a.sunrise + 3 * 3600) \
                or not (b.sunset - 3 * 3600 < t2 < b.sunset + 5 * 3600):
            continue
        lit_s = ((e2 - e1) - standby * gap / 3600) / watts * 3600
        if lit_s < 0 or lit_s > 5 * 3600:
            continue
        tail_p = max(0.0, a.sunrise + city_off * 60 - t1)
        head_p = max(0.0, t2 - (b.sunset + city_on * 60))
        share = tail_p / (tail_p + head_p) if tail_p + head_p > 0 else 0.5
        offs.append((t1 + lit_s * share - a.sunrise) / 60)
        ons.append((t2 - lit_s * (1 - share) - b.sunset) / 60)
    return ons, offs


def infer(readings: dict[str, Readings], nights: list[Night]) -> tuple[dict[str, Schedule], dict]:
    """Each lamp's schedule, and the closely read lamps' pattern {on_min, off_min, lamps}."""
    first: dict[str, tuple] = {}
    for lamp, r in readings.items():
        w, sb, _ = wattage(r)
        first[lamp] = (w, sb, *_switches(r, nights))
    m_on = [statistics.median(ons) for _, _, ons, _ in first.values() if len(ons) >= MIN_NIGHTS]
    m_off = [statistics.median(offs) for _, _, _, offs in first.values() if len(offs) >= MIN_NIGHTS]
    city = {"on_min": statistics.median(m_on) if m_on else 0.0,
            "off_min": statistics.median(m_off) if m_off else 0.0,
            "lamps_on": len(m_on), "lamps_off": len(m_off)}
    out = {}
    for lamp, (w, sb, ons, offs) in first.items():
        r = readings[lamp]
        on_b = off_b = "city"
        on_v, off_v = city["on_min"], city["off_min"]
        if len(ons) >= MIN_NIGHTS:
            on_v, on_b = statistics.median(ons), "measured"
        if len(offs) >= MIN_NIGHTS:
            off_v, off_b = statistics.median(offs), "measured"
        if w and "city" in (on_b, off_b):
            m_ons, m_offs = _meter_splits(r, nights, w, sb, city["on_min"], city["off_min"])
            if on_b == "city" and len(m_ons) >= MIN_NIGHTS:
                on_v, on_b = statistics.median(m_ons), "meter"
            if off_b == "city" and len(m_offs) >= MIN_NIGHTS:
                off_v, off_b = statistics.median(m_offs), "meter"
        seen = {datetime.fromtimestamp(t - 12 * 3600, TZ).date() for t, _ in r.states}
        out[lamp] = Schedule(w, sb, on_v, off_v, on_b, off_b, len(seen))
    return out, city


# -- replaying a schedule against the meter ----------------------------------------------------


def lit_seconds(s: Schedule, t1: float, t2: float, nights: dict[date, Night]) -> float:
    d = datetime.fromtimestamp(t1, TZ).date() - timedelta(days=1)
    last = datetime.fromtimestamp(t2, TZ).date()
    total = 0.0
    while d <= last:
        n = nights.get(d) or night(d)
        total += overlap(t1, t2, n.sunset + s.on_min * 60, n.sunrise + s.off_min * 60)
        d += timedelta(days=1)
    return total


def meter_intervals(r: Readings, watts: float) -> list[tuple[float, float, float]]:
    """(t1, t2, Wh) between consecutive counter readings, leaving out resets and long gaps."""
    pts = [(t, e) for t, _, e in r.power if e is not None]
    out = []
    for (t1, e1), (t2, e2) in zip(pts, pts[1:]):
        dt = t2 - t1
        if dt <= 0 or dt > 26 * 3600:
            continue
        de = e2 - e1
        if de < 0 or de > 1.3 * max(watts, 1.0) * dt / 3600 + 5:
            continue  # a counter reset or a glitch
        out.append((t1, t2, de))
    return out


def replay(schedules: dict[str, Schedule], readings: dict[str, Readings], start: float, end: float,
           nights: dict[date, Night]) -> dict:
    """Today's schedule replayed over [start, end] where each lamp's meter counted, against what
    it counted: totals, per local day, and per lamp."""
    measured = modelled = 0.0
    days: dict[str, list[float]] = {}
    per_lamp: dict[str, tuple[float, float]] = {}
    for lamp, s in schedules.items():
        if not s.watts:
            continue
        m_l = o_l = 0.0
        for t1, t2, wh in meter_intervals(readings[lamp], s.watts):
            if t1 < start or t2 > end:
                continue
            lit = lit_seconds(s, t1, t2, nights)
            model = (s.watts * lit + s.standby * (t2 - t1 - lit)) / 3600
            m_l += wh
            o_l += model
            day = datetime.fromtimestamp((t1 + t2) / 2 - 12 * 3600, TZ).date().isoformat()
            acc = days.setdefault(day, [0.0, 0.0])
            acc[0] += wh
            acc[1] += model
        if m_l > 0:
            per_lamp[lamp] = (m_l, o_l)
            measured += m_l
            modelled += o_l
    return {"measured_kwh": measured / 1000, "modelled_kwh": modelled / 1000,
            "error_pct": 100 * (modelled - measured) / measured if measured else None,
            "days": {d: (a / 1000, b / 1000) for d, (a, b) in sorted(days.items())},
            "lamps": per_lamp}


def median_lamp_error(per_lamp: dict[str, tuple[float, float]]) -> float | None:
    errs = [abs(o - m) / m * 100 for m, o in per_lamp.values() if m > 50]  # lamps with 50 Wh or more
    return statistics.median(errs) if errs else None


# -- a policy over a year ----------------------------------------------------------------------


@dataclass
class Policy:
    switch_by_sun: bool = False
    dusk_offset_min: float = 0
    dawn_offset_min: float = 0
    dim: bool = False
    dim_level: float = 50
    dim_from: str = "01:00"
    dim_to: str = "05:00"
    part_night: bool = False
    off_from: str = "01:00"
    off_to: str = "05:00"


def lamp_night(s: Schedule, n: Night, p: Policy, dark_ok: bool) -> tuple[float, float, float, float]:
    """Seconds lit today; and in the policy, seconds at full light, dimmed and switched off
    (part-night), for one lamp and one night. `dark_ok`: its street may go dark."""
    base = max(0.0, (n.sunrise + s.off_min * 60) - (n.sunset + s.on_min * 60))
    if p.switch_by_sun:
        a, b = n.dusk + p.dusk_offset_min * 60, n.dawn - p.dawn_offset_min * 60
    else:
        a, b = n.sunset + s.on_min * 60, n.sunrise + s.off_min * 60
    lit = max(0.0, b - a)
    off = 0.0
    o1 = o2 = 0.0
    if p.part_night and dark_ok:
        o1, o2 = clock_at(n, p.off_from), clock_at(n, p.off_to)
        off = overlap(a, b, o1, o2)
    dim = 0.0
    if p.dim:
        d1, d2 = clock_at(n, p.dim_from), clock_at(n, p.dim_to)
        dim = overlap(a, b, d1, d2)
        if off:
            dim -= max(0.0, min(b, d2, o2) - max(a, d1, o1))
    return base, lit - off - dim, dim, off

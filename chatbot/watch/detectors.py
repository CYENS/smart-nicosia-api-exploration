"""What the twin notices on its own: detectors over its memory and the city's own alarms.

`observe()` reads one window of the twin's memory (readings, coverage, the city's event feed and
alarms) into an `Observed`; every detector is a plain function of it, so each one is tested on
fixtures. A detector returns `Detection`s: one device, one rule, when it started (and ended, or
None while it is still true), how sure the twin is, and the evidence with times.

Meaning comes from collector/interpret.py and history.replay, never from here: a lamp is dark
at night by the same rule the strip and the inspector use, a bay is occupied by the same status
rule, a bin's fill band is interpret's. Silence only counts where the memory covers the silent
stretch (coverage), so "nobody was recording" never reads as "the device went quiet".

Detectors:

- lamp_dark: a lamp silent at night (offline), until it reports again.
- lamp_day: a lamp reporting lit while the sun is well up (12 degrees or more).
- lamp_power: a lamp at full level drawing power unlike the other lamps of its street
  (lamps come in power classes, so "similar" means the classes of three or more lamps there).
- parking_stuck: a bay reporting occupied for 3 days or more while it keeps reporting.
- bin_jump: a bin's fill rising 50 points or more between two reports under 3 hours apart.
- bin_silent: a bin with no report for 12 hours.
- air_silent: an air station with no report for 3 hours.
- city_alarm / city_event: the city's own alarms (active, from getAlarms) and its event feed
  (toastDesc: lost connections, full bins, air warnings, screens without heartbeats).
- road_feed_stalled: the national road detectors' feed answering with nothing new for 30
  minutes (its newest measurement is old), until a fresh one comes. Like a device's silence it
  counts only while someone is looking: the feed must have been read in the last 15 minutes,
  else the detector says nothing either way.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector import interpret, sun
from collector.interpret import Interpretation

from chatbot import history
from chatbot.city import TYPE_TO_KIND, CityData, Device, DeviceState
from chatbot.memory import Memory, Unavailable
from chatbot.settings import TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

WINDOW = timedelta(hours=72)  # what one evaluation looks back over
LAMP_WINDOW = timedelta(hours=30)  # lamps: last night and tonight
PARKING_LOOKBACK = timedelta(days=8)  # to find when a bay's run of "occupied" began
STEP = timedelta(minutes=15)
DARK_FOR = timedelta(minutes=45)  # offline this long before a dark lamp counts (now)...
DARK_BLIP = timedelta(minutes=90)  # ...and an episode that ended must have lasted this long

DAY_ELEVATION_DEG = 12.0  # the sun this high is day by any lamp's photocell or schedule
STUCK_AFTER = timedelta(days=3)
JUMP_POINTS = 50.0
JUMP_WITHIN = timedelta(hours=3)
BIN_SILENT_AFTER = timedelta(hours=12)
AIR_SILENT_AFTER = timedelta(hours=3)
MIN_COVERED = 0.9  # silence counts only where the memory covers this much of it
FEED_STALLED_AFTER = timedelta(minutes=30)  # roads.STALE_S: a measurement counts this long
FEED_LOOKED_WITHIN = timedelta(minutes=15)  # a feed read longer ago than this says nothing
POWER_FULL_LEVEL = 90.0  # compare lamps at full level only (dimmed lamps draw less)
POWER_CLASS_RATIO = 1.3  # powers within 30% of each other are one class of lamp
POWER_OFF_BY = 0.3  # a lamp this far from the nearest class of 3+ lamps is out of line
POWER_MIN_W = 8.0

# The keys each detector reads, per device type, and how far back.
KEYS = {
    "Lighting": (("level", "powerstats", "state", "last_lamp_status"), LAMP_WINDOW),
    "Parking": (("status", "vbatt", "temperature"), PARKING_LOOKBACK),
    "Wastebin": (("level", "pickup_time"), WINDOW),
    "smartsense": (("caqi",), WINDOW),
}

SEVERITIES = ("high", "medium", "low")
RANK = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # low 0, medium 1, high 2


def worst(*severities: str) -> str:
    return max(severities, key=lambda s: RANK.get(s, 0))


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def hhmm(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


def when(t: datetime, now: datetime | None = None) -> str:
    """'21:40' today, 'Tue 21:40' this week, '18 Nov 21:40' before."""
    local = t.astimezone(TZ)
    if now is not None:
        today = now.astimezone(TZ).date()
        if local.date() == today:
            return local.strftime("%H:%M")
        if (today - local.date()).days < 7:
            return local.strftime("%a %H:%M")
    return f"{local.day} {local:%b %H:%M}"


def span(d: timedelta) -> str:
    s = int(d.total_seconds())
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 2 * 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m:02d} min" if m and h < 6 else f"{h} h"
    return f"{s // 86400} days"


# -- what a detector returns ---------------------------------------------------------------------


@dataclass
class Evidence:
    t: datetime
    text: str
    device: str | None = None
    kind: str = "reading"  # reading | alarm | event | detection
    value: float | None = None
    unit: str | None = None

    def to_dict(self) -> dict:
        out = {"t": self.t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S"), "text": self.text,
               "kind": self.kind}
        if self.device:
            out["device"] = self.device
        if self.value is not None:
            out["value"] = round(self.value, 1)
            out["unit"] = self.unit
        return out


@dataclass
class Detection:
    rule: str
    family: str  # outage | lighting | power | parking | bins | air | other: what may group
    device: str
    device_type: str
    start: datetime
    end: datetime | None  # None: still true now
    severity: str
    what: str  # one line about this device: "dark at night since 21:40"
    evidence: list[Evidence] = field(default_factory=list)
    source: str = "twin"  # twin (a detector) | city (its alarms and event feed)
    label: str | None = None
    street: str | None = None
    lat: float | None = None
    lon: float | None = None
    count: int = 1  # how many times the city logged it (repeated events of one device)
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable across evaluations: the same device, rule and start is the same detection."""
        return f"{self.rule}:{self.device}:{_ms(self.start) // 60_000}"

    @property
    def active(self) -> bool:
        return self.end is None

    @property
    def link_time(self) -> datetime:
        """When it began, for grouping: things that went quiet together have their last
        reports close together, whatever moment the rules call them offline."""
        s = self.extra.get("silent_since")
        return _dt(s) if s else self.start

    def to_member(self) -> dict:
        return {"key": self.key, "rule": self.rule, "family": self.family,
                "device": self.device, "device_type": self.device_type,
                "kind": TYPE_TO_KIND.get(self.device_type, self.device_type),
                "label": self.label or self.device, "street": self.street,
                "lat": self.lat, "lon": self.lon, "link": _ms(self.link_time),
                "start": _ms(self.start),
                "end": _ms(self.end) if self.end else None, "severity": self.severity,
                "what": self.what, "source": self.source, "count": self.count,
                **({"extra": self.extra} if self.extra else {})}


# -- what the memory holds for a window ----------------------------------------------------------


@dataclass
class Place:
    name: str
    type: str
    lat: float | None
    lon: float | None
    label: str
    street: str | None
    last_reading: datetime | None = None


@dataclass
class Observed:
    now: datetime
    devices: dict[str, Place]
    readings: dict[str, list[tuple[int, str, str | None]]]  # device -> sorted (ms, key, value)
    covered: dict[str, list[tuple[int, int]]]  # device -> ms stretches the memory covers
    events: list[tuple[int, str | None, str]] = field(default_factory=list)  # (ms, device, text)
    alarms: list[dict] = field(default_factory=list)  # active (or recently seen) city alarms
    live: dict[str, Interpretation] = field(default_factory=dict)  # device -> live verdict
    notes: list[str] = field(default_factory=list)
    # Feeds the twin reads that aren't devices, as their last read left them (never fetched for
    # this): source id -> Roads.health() (read_ok_at, error, newest, published).
    feeds: dict[str, dict] = field(default_factory=dict)

    def of_type(self, device_type: str) -> list[Place]:
        return [p for p in self.devices.values() if p.type == device_type]

    def covered_share(self, device: str, start: datetime, end: datetime) -> float:
        a, b = _ms(start), _ms(end)
        if b <= a:
            return 1.0
        got = sum(max(0, min(e, b) - max(s, a)) for s, e in self.covered.get(device, []))
        return got / (b - a)


_NOT_A_STREET = re.compile(r"nema|controller|\(|^\W*$", re.I)


def tidy_street(street: str | None) -> str | None:
    """A lamp's street as people say it: "MAKARIOU NEW" is Makariou; controller names aren't
    streets."""
    if not street:
        return None
    s = re.sub(r"\s+new$", "", street.strip(), flags=re.I).strip()
    if not s or _NOT_A_STREET.search(s):
        return None
    return s.title() if s.isupper() else s


def _labels(city: CityData | None, name: str, device_type: str, lat, lon) -> tuple[str, str | None]:
    if city is None:
        return name, None
    label, street = city.cached_label(Device(name, device_type, lat, lon))
    return label, tidy_street(street)


def observe(memory: Memory | None, city: CityData | None, now: datetime, *,
            window: timedelta = WINDOW) -> Observed:
    """Read one window of the memory for the detectors. Raises Unavailable when it can't."""
    if memory is None:
        raise Unavailable("missing", "the twin's memory is off")
    start = now - window
    with memory.reading() as con:
        rows = con.execute(
            """SELECT name, type, lat, lon, last_telemetry FROM devices
               WHERE coalesce(in_inventory, true)""").fetchall()
        devices = {}
        for name, device_type, lat, lon, last in rows:
            if device_type not in TYPE_TO_KIND:
                continue
            label, street = _labels(city, name, device_type, lat, lon)
            devices[name] = Place(name, device_type, lat, lon, label, street, last)
        readings: dict[str, list] = defaultdict(list)
        for device_type, (keys, back) in KEYS.items():
            got = con.execute(
                """SELECT t.device, epoch_ms(t.ts), t.key, t.value FROM telemetry t
                   JOIN devices d ON d.name = t.device
                   WHERE d.type = ? AND list_contains(?, t.key) AND t.ts >= ? AND t.ts <= ?""",
                [device_type, list(keys), now - back, now]).fetchall()
            for device, ts, key, value in got:
                readings[device].append((int(ts), key, value))
        events = con.execute(
            "SELECT epoch_ms(ts), device, message FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts",
            [start, now]).fetchall()
        alarms = con.execute(
            """SELECT id, device, device_type, type, severity, status, epoch_ms(start_ts),
                      epoch_ms(end_ts), epoch_ms(clear_ts), epoch_ms(last_seen)
               FROM alarms WHERE status LIKE 'ACTIVE%' AND last_seen >= ?""",
            [now - timedelta(hours=1)]).fetchall()
    for recs in readings.values():
        recs.sort()
    covered: dict[str, list[tuple[int, int]]] = {}
    for device_type, (_, back) in KEYS.items():
        names = [p.name for p in devices.values() if p.type == device_type]
        if names:
            covered |= memory.coverage(device_type, names, _ms(now - back), _ms(now))
    obs = Observed(now, devices, dict(readings), covered,
                   [(int(t), d, m) for t, d, m in events],
                   [dict(zip(("id", "device", "device_type", "type", "severity", "status",
                              "start_ms", "end_ms", "clear_ms", "last_seen_ms"), a))
                    for a in alarms])
    if city is not None:
        for device_type in ("Lighting", "Parking", "Wastebin", "smartsense"):
            for name, state in city.cached_states(device_type).items():
                obs.live[name] = state.interp
    return obs


# -- lamps ---------------------------------------------------------------------------------------


def _states(obs: Observed, device_type: str) -> list[DeviceState]:
    blank = Interpretation(device_type, "unknown", True, None, None, "")
    return [DeviceState(Device(p.name, p.type, p.lat, p.lon), p.label, p.street, blank)
            for p in obs.of_type(device_type)]


def _records(obs: Observed, names: list[str]) -> dict[str, list[dict]]:
    return {n: [{"key": k, "timestamp": t, "value": v} for t, k, v in obs.readings.get(n, [])]
            for n in names}


def lamps_dark(obs: Observed) -> list[Detection]:
    """Lamps offline at night (silent, by interpret.lamp's rule), from the replayed frames,
    until they report again. Moments the memory doesn't cover are unknown, never offline."""
    states = _states(obs, "Lighting")
    if not states:
        return []
    start = obs.now - LAMP_WINDOW
    times = history.grid(start, obs.now, int(STEP.total_seconds() // 60))
    names = [s.device.name for s in states]
    frames, _ = history.replay("Lighting", states, _records(obs, names), times,
                               {n: obs.covered.get(n, []) for n in names})
    out = []
    for j, s in enumerate(states):
        name = s.device.name
        reports = [t for t, k, _ in obs.readings.get(name, [])]
        k = 0
        while k < len(times):
            if frames[k][j] != "offline":
                k += 1
                continue
            # One night's run of offline moments: it ends when the lamp reports again, or when
            # day comes (a silent lamp by day is off, as it should be).
            k1 = k
            while k1 < len(times) and frames[k1][j] == "offline":
                k1 += 1
            first = times[k]
            before = [t for t in reports if t <= _ms(first)]
            last = _dt(before[-1]) if before else obs.devices[name].last_reading
            stop = times[k1] if k1 < len(times) else None
            back = next((t for t in reports if t > _ms(first) and (stop is None or t <= _ms(stop))), None)
            end = _dt(back) if back else stop
            lasted = STEP * (k1 - k)
            k = k1
            if lasted < (DARK_FOR if end is None else DARK_BLIP):
                continue
            ev = [Evidence(first, "offline at night: no report for over 70 minutes", name, "detection")]
            if last:
                ev.insert(0, Evidence(last, "last report", name))
            if back:
                ev.append(Evidence(end, "reported again", name))
            elif end is not None:
                ev.append(Evidence(end, "day came: lamps are silent by day, so it can only be "
                                        "told again after dusk", name, "detection"))
            silent_since = last or first
            what = (f"dark at night, silent since {when(silent_since, obs.now)}" if end is None
                    else f"dark at night from {hhmm(first)} to {hhmm(end)}")
            out.append(Detection(
                "lamp_dark", "outage", name, "Lighting", first, end, "medium", what, ev,
                label=s.label, street=s.street, lat=s.device.lat, lon=s.device.lon,
                extra={"silent_since": _ms(silent_since), "reported_again": bool(back)}))
    return out


def lamps_by_day(obs: Observed) -> list[Detection]:
    """Lamps that reported lit while the sun was well up (DAY_ELEVATION_DEG or more)."""
    out = []
    for p in obs.of_type("Lighting"):
        lit = []
        for t, key, value in obs.readings.get(p.name, []):
            if key != "level":
                continue
            if not interpret.lamp_lit(interpret.to_number(value), None, None):
                continue
            at = _dt(t)
            elev = sun.solar_elevation(at, p.lat or sun.NICOSIA[0], p.lon or sun.NICOSIA[1])
            if elev >= DAY_ELEVATION_DEG:
                lit.append((at, interpret.to_number(value), elev))
        if not lit:
            continue
        first, last = lit[0][0], lit[-1][0]
        dark_since = next((_dt(t) for t, k, v in obs.readings.get(p.name, [])
                           if k == "level" and _dt(t) > last
                           and interpret.lamp_lit(interpret.to_number(v), None, None) is False), None)
        quiet = obs.now - last > interpret.LAMP_ON_WINDOW
        end = dark_since or (last + interpret.LAMP_ON_WINDOW if quiet else None)
        ev = [Evidence(t, f"lit at level {lvl:g} with the sun {elev:.0f} degrees up", p.name,
                       value=lvl, unit="%") for t, lvl, elev in lit[:3]]
        if len(lit) > 3:
            ev.append(Evidence(last, f"still lit ({len(lit)} reports by day)", p.name))
        out.append(Detection("lamp_day", "lighting", p.name, "Lighting", first, end, "medium",
                             f"lit by day at {hhmm(first)}", ev, label=p.label, street=p.street,
                             lat=p.lat, lon=p.lon, count=len(lit)))
    return out


def _power(value: str | None, t: int, at: datetime) -> float | None:
    """The lamp's active power in a powerstats reading, read by interpret.lamp."""
    if not value or not str(value).startswith("{"):
        return None
    d = interpret.lamp({"powerstats": (t, value)}, at, night=True).detail
    return d.get("power_w")


def _classes(values: list[float]) -> list[list[float]]:
    """1D clusters: sorted powers split where one is more than POWER_CLASS_RATIO times the last."""
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v <= max(out[-1][-1], 1.0) * POWER_CLASS_RATIO:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def lamps_power(obs: Observed) -> list[Detection]:
    """Lamps at full level drawing power unlike the classes of 3+ lamps on their street."""
    since = obs.now - LAMP_WINDOW
    per_lamp: dict[str, list[tuple[datetime, float]]] = {}
    for p in obs.of_type("Lighting"):
        recs = obs.readings.get(p.name, [])
        level_at: list[tuple[int, float]] = [(t, interpret.to_number(v) or 0.0)
                                             for t, k, v in recs if k == "level"]
        got = []
        for t, k, v in recs:
            if k != "powerstats" or _dt(t) < since:
                continue
            lvl = next((x for ts, x in reversed(level_at) if ts <= t + 1000), None)
            if lvl is None or lvl < POWER_FULL_LEVEL:
                continue
            w = _power(v, t, _dt(t))
            if w is not None and w > 0:
                got.append((_dt(t), w))
        if got:
            per_lamp[p.name] = got
    by_street: dict[str, list[str]] = defaultdict(list)
    for name in per_lamp:
        street = obs.devices[name].street
        if street:
            by_street[street].append(name)
    out = []
    for street, names in by_street.items():
        typical = {n: statistics.median(w for _, w in per_lamp[n]) for n in names}
        if len(typical) < 4:
            continue
        classes = [c for c in _classes(list(typical.values())) if len(c) >= 3]
        if not classes:
            continue
        for n, w in typical.items():
            if any(min(c) <= w <= max(c) for c in classes):
                continue
            cls = min(classes, key=lambda c: abs(statistics.median(c) - w))
            near = statistics.median(cls)
            if abs(w - near) < POWER_MIN_W or abs(w - near) / near < POWER_OFF_BY:
                continue
            readings = per_lamp[n]
            if len(readings) < 3:
                continue
            p = obs.devices[n]
            ev = [Evidence(readings[0][0], f"drew {readings[0][1]:.0f} W at full level", n,
                           value=readings[0][1], unit="W"),
                  Evidence(readings[-1][0], f"drew {readings[-1][1]:.0f} W at full level", n,
                           value=readings[-1][1], unit="W"),
                  Evidence(readings[-1][0], f"{len(cls)} similar lamps on {street} draw about "
                                            f"{near:.0f} W", kind="detection", value=near, unit="W")]
            out.append(Detection(
                "lamp_power", "power", n, "Lighting", readings[0][0],
                None if obs.now - readings[-1][0] < timedelta(hours=18) else readings[-1][0],
                "low", f"draws {w:.0f} W where similar lamps draw {near:.0f} W", ev,
                label=p.label, street=street, lat=p.lat, lon=p.lon,
                extra={"power_w": round(w, 1), "similar_w": round(near, 1)}))
    return out


# -- parking, bins, air ---------------------------------------------------------------------------


def _alive(obs: Observed, device: str, device_type: str) -> datetime | None:
    """The newest reading (any key) if it is fresh by interpret's STALE_AFTER, else None."""
    recs = obs.readings.get(device)
    if not recs:
        return None
    newest = _dt(recs[-1][0])
    return newest if obs.now - newest <= interpret.STALE_AFTER[device_type] else None


def parking_stuck(obs: Observed) -> list[Detection]:
    """Bays reporting occupied for STUCK_AFTER or more while they keep reporting."""
    out = []
    for p in obs.of_type("Parking"):
        alive = _alive(obs, p.name, "Parking")
        if alive is None:
            continue
        statuses = [(t, interpret.classify("Parking", "status", v))
                    for t, k, v in obs.readings.get(p.name, []) if k == "status"]
        statuses = [(t, s) for t, s in statuses if s]
        if not statuses or statuses[-1][1] != "occupied":
            continue
        free = [t for t, s in statuses if s == "free"]
        run = [t for t, s in statuses if s == "occupied" and (not free or t > free[-1])]
        since = _dt(run[0])
        at_least = not free  # no "free" in the whole lookback: occupied at least since then
        if obs.now - since < STUCK_AFTER:
            continue
        if obs.covered_share(p.name, max(since, obs.now - PARKING_LOOKBACK), obs.now) < MIN_COVERED:
            continue
        days = (obs.now - since).days
        ev = [Evidence(since, ("occupied in every report from here on" if at_least else "turned occupied"),
                       p.name),
              Evidence(_dt(run[-1]), f"still occupied ({len(run)} reports in a row)", p.name),
              Evidence(alive, "last sign of life: the sensor keeps reporting", p.name)]
        out.append(Detection(
            "parking_stuck", "parking", p.name, "Parking", since, None, "low",
            f"occupied for {'at least ' if at_least else ''}{days} days while it keeps reporting",
            ev, label=p.label, street=p.street, lat=p.lat, lon=p.lon,
            extra={"days": days, "at_least": at_least}))
    return out


def bins_jump(obs: Observed) -> list[Detection]:
    """A bin's fill rising JUMP_POINTS or more between two reports under JUMP_WITHIN apart."""
    out = []
    for p in obs.of_type("Wastebin"):
        levels = [(t, interpret.to_number(v)) for t, k, v in obs.readings.get(p.name, [])
                  if k == "level"]
        levels = [(t, v) for t, v in levels if v is not None]
        for (t0, a), (t1, b) in zip(levels, levels[1:]):
            if b - a < JUMP_POINTS or _dt(t1) - _dt(t0) > JUMP_WITHIN:
                continue
            after = [(t, v) for t, v in levels if t > t1 and _dt(t) - _dt(t1) <= timedelta(hours=6)]
            back = next(((t, v) for t, v in after if b - v >= 40), None)
            ev = [Evidence(_dt(t0), f"fill {a:.0f}%", p.name, value=a, unit="%"),
                  Evidence(_dt(t1), f"fill {b:.0f}%, {b - a:.0f} points in "
                                    f"{span(_dt(t1) - _dt(t0))}", p.name, value=b, unit="%")]
            what = f"jumped from {a:.0f}% to {b:.0f}% in {span(_dt(t1) - _dt(t0))}"
            if back:
                ev.append(Evidence(_dt(back[0]), f"back to {back[1]:.0f}% with no pickup: "
                                                 "probably a misreading", p.name,
                                   value=back[1], unit="%"))
                what += f", then back to {back[1]:.0f}%"
            end = _dt(back[0]) if back else (_dt(t1) + timedelta(hours=6))
            out.append(Detection("bin_jump", "bins", p.name, "Wastebin", _dt(t1),
                                 end if end <= obs.now else None, "low", what, ev,
                                 label=p.label, street=p.street, lat=p.lat, lon=p.lon,
                                 extra={"from": a, "to": b}))
    return out


def _silent(obs: Observed, device_type: str, after: timedelta, rule: str, family: str,
            severity: str, noun: str) -> list[Detection]:
    out = []
    for p in obs.of_type(device_type):
        recs = obs.readings.get(p.name, [])
        last = _dt(recs[-1][0]) if recs else p.last_reading
        live = obs.live.get(p.name)
        if live is not None and live.as_of is not None and (last is None or live.as_of > last):
            last = live.as_of
        if last is None or obs.now - last < after:
            continue
        if obs.covered_share(p.name, last, obs.now) < MIN_COVERED:
            continue
        start = last + after
        out.append(Detection(
            rule, family, p.name, device_type, start, None, severity,
            f"no {noun} for {span(obs.now - last)}",
            [Evidence(last, "last report", p.name),
             Evidence(start, f"silent for {span(after)}: counted as gone quiet", p.name,
                      "detection")],
            label=p.label, street=p.street, lat=p.lat, lon=p.lon,
            extra={"silent_since": _ms(last)}))
    return out


def bins_silent(obs: Observed) -> list[Detection]:
    return _silent(obs, "Wastebin", BIN_SILENT_AFTER, "bin_silent", "outage", "low", "report")


def air_silent(obs: Observed) -> list[Detection]:
    return _silent(obs, "smartsense", AIR_SILENT_AFTER, "air_silent", "outage", "medium",
                   "reading")


# -- feeds ---------------------------------------------------------------------------------------

ROAD_FEED = "source-road_speeds"  # the page opens it in the data sources panel


def road_feed_stalled(obs: Observed) -> list[Detection] | None:
    """The road detectors' feed answering with an old measurement: one detection while it is
    stalled, none once a fresh measurement comes. None (can't tell, so nothing ends) when the
    feed hasn't been read lately or its last read failed."""
    h = obs.feeds.get("road_speeds")
    if not h or h.get("error") or not h.get("read_ok_at") or h.get("newest") is None:
        return None
    read = datetime.fromtimestamp(h["read_ok_at"], tz=timezone.utc)
    if obs.now - read > FEED_LOOKED_WITHIN:
        return None
    newest: datetime = h["newest"]
    if obs.now - newest <= FEED_STALLED_AFTER:
        return []
    start = newest + FEED_STALLED_AFTER
    since = when(newest, obs.now)
    published = h.get("published")
    ev = [Evidence(newest, "the newest measurement in the feed", ROAD_FEED),
          Evidence(start, f"nothing new for {span(FEED_STALLED_AFTER)}: counted as stalled", ROAD_FEED,
                   "detection"),
          Evidence(read, "the feed still answers" + (f", published {hhmm(published)}," if published else "")
                   + f" with the measurement of {since}", ROAD_FEED)]
    return [Detection(
        "road_feed_stalled", "feed", ROAD_FEED, "feed", start, None, "medium",
        f"nothing new since {since}", ev, label="National road detectors",
        extra={"silent_since": _ms(newest), "source": "road_speeds",
               "story": (f"The national road speeds feed (traffic4cyprus.org.cy) still answers, but "
                         f"its Bluetooth detectors have sent nothing new since {since}, so no road "
                         "has a live speed. The map shows each road's usual speed for the hour "
                         "until they send again.")})]


# -- the city's own alarms -----------------------------------------------------------------------

# What each kind of city message means here: (family, severity, short words).
CITY_KINDS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"lost connection", re.I), "outage", "medium", "lost connection"),
    (re.compile(r"no heartbeat", re.I), "outage", "medium", "no heartbeat from the player"),
    (re.compile(r"aqi not reported", re.I), "outage", "low", "air index not reported"),
    (re.compile(r"fill level above", re.I), "bins", "low", "full (above 80%)"),
    (re.compile(r"caqi", re.I), "air", "medium", "poor air (high CAQI)"),
]
REPEAT_WITHIN = timedelta(hours=8)  # the same message from one device within this is one run
EVENT_LASTS = timedelta(hours=2)  # an event with nothing after it is taken as over after this


def city_kind(text: str) -> tuple[str, str, str]:
    for pattern, family, severity, words in CITY_KINDS:
        if pattern.search(text or ""):
            return family, severity, words
    return "other", "low", (text or "alarm").strip().lower()


def city_events(obs: Observed) -> list[Detection]:
    """The city's event feed, one detection per run of the same message from one device."""
    runs: dict[tuple[str, str], list[list[int]]] = defaultdict(list)
    texts: dict[tuple[str, str], str] = {}
    for t, device, message in obs.events:
        if not device:
            continue
        kind = message.partition(": ")[2] or message
        key = (device, kind.lower())
        texts[key] = kind
        groups = runs[key]
        if groups and _dt(t) - _dt(groups[-1][-1]) <= REPEAT_WITHIN:
            groups[-1].append(t)
        else:
            groups.append([t])
    out = []
    active_alarms = {(a["device"], (a["type"] or "").lower()) for a in obs.alarms}
    for (device, kind_l), groups in runs.items():
        family, severity, words = city_kind(kind_l)
        p = obs.devices.get(device)
        device_type = p.type if p else _guess_type(device)
        for g in groups:
            first, last = _dt(g[0]), _dt(g[-1])
            end, why = _event_end(obs, device, device_type, family, last)
            if end is None and (device, kind_l) not in active_alarms and why is None:
                end = None if obs.now - last <= EVENT_LASTS else last + EVENT_LASTS
            ev = [Evidence(_dt(t), f"city alarm: {texts[(device, kind_l)]}", device, "event")
                  for t in g[:4]]
            if len(g) > 4:
                ev.append(Evidence(last, f"city alarm again ({len(g)} times since "
                                         f"{when(first, obs.now)})", device, "event"))
            if end is not None and why:
                ev.append(Evidence(end, why, device))
            what = words if len(g) == 1 else f"{words}, {len(g)} times"
            out.append(Detection(
                "city_event", family, device, device_type, first, end, severity, what, ev,
                source="city", label=p.label if p else device, street=p.street if p else None,
                lat=p.lat if p else None, lon=p.lon if p else None, count=len(g),
                extra={"message": texts[(device, kind_l)]}))
    return out


def _event_end(obs: Observed, device: str, device_type: str, family: str, last: datetime
               ) -> tuple[datetime | None, str | None]:
    """When a run of city alarms stopped being true, from what the device did after it:
    (end, why). (None, reason) while it is still true by the readings; (None, None) when the
    readings can't tell (another device type, or the memory doesn't cover it)."""
    recs = [(t, k, v) for t, k, v in obs.readings.get(device, []) if t > _ms(last)]
    if device_type not in KEYS:
        return None, None
    if family == "outage":
        if recs:
            return _dt(recs[0][0]), "reported again"
        if obs.covered_share(device, last, obs.now) >= MIN_COVERED:
            return None, "no report since"
        return None, None
    if family == "bins":
        levels = [(t, interpret.to_number(v)) for t, k, v in recs if k == "level"]
        drop = next((t for t, v in levels if v is not None and interpret.bin_band(v) != "full"), None)
        if drop:
            return _dt(drop), "fill back under 80%"
        if levels:
            return None, "still full"
        return None, None
    if family == "air":
        caqi = [(t, interpret.to_number(v)) for t, k, v in recs if k == "caqi"]
        ok = next((t for t, v in caqi if v is not None and v < 75), None)
        if ok:
            return _dt(ok), "the index back under 75"
        if caqi:
            return None, "still high"
        return None, None
    return None, None


def city_alarms(obs: Observed) -> list[Detection]:
    """Active city alarms (getAlarms) the event feed has no run for: each one as it stands."""
    seen = {(d.device, (d.extra.get("message") or "").lower()) for d in city_events(obs)}
    out = []
    for a in obs.alarms:
        kind = (a["type"] or "").lower()
        if not a["device"] or (a["device"], kind) in seen:
            continue
        family, severity, words = city_kind(kind)
        if (a["severity"] or "").upper() == "CRITICAL":
            severity = "high"
        p = obs.devices.get(a["device"])
        start = _dt(a["start_ms"]) if a["start_ms"] else obs.now
        out.append(Detection(
            "city_alarm", family, a["device"], p.type if p else (a["device_type"] or "?"),
            start, None, severity, words,
            [Evidence(start, f"city alarm raised: {a['type']} ({(a['severity'] or '').lower()})",
                      a["device"], "alarm")],
            source="city", label=p.label if p else a["device"], street=p.street if p else None,
            lat=p.lat if p else None, lon=p.lon if p else None,
            extra={"message": a["type"], "alarm_id": a["id"]}))
    return out


def _guess_type(device: str) -> str:
    if device.startswith("YL"):
        return "Lighting"
    if device.startswith("YP"):
        return "Parking"
    if device.startswith("YW"):
        return "Wastebin"
    if re.fullmatch(r"[0-9a-f]{12}", device):
        return "smartsense"
    return "?"


# Each detector and the rule its detections carry.
DETECTORS = ((lamps_dark, "lamp_dark"), (lamps_by_day, "lamp_day"), (lamps_power, "lamp_power"),
             (parking_stuck, "parking_stuck"), (bins_jump, "bin_jump"),
             (bins_silent, "bin_silent"), (air_silent, "air_silent"),
             (city_events, "city_event"), (city_alarms, "city_alarm"),
             (road_feed_stalled, "road_feed_stalled"))
RULES = tuple(rule for _, rule in DETECTORS)


def detect(obs: Observed) -> tuple[list[Detection], set[str]]:
    """Every detector over one window: (detections, the rules whose detector ran). A detector
    that fails, or returns None because it can't tell this time, is skipped and its rule left
    out, so nothing it found before is taken as over."""
    out: list[Detection] = []
    ran: set[str] = set()
    for fn, rule in DETECTORS:
        try:
            found = fn(obs)
            if found is None:
                continue
            out += found
            ran.add(rule)
        except Exception:  # noqa: BLE001 - one broken detector must not hide the others
            log.exception("detector %s failed", fn.__name__)
    return out, ran


def member_json(members: list[dict]) -> str:
    return json.dumps(members, ensure_ascii=False, separators=(",", ":"))

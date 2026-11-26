"""The city at a moment, for the city clock: what the twin's memory says every device was doing
then, by the same rules the live tools use.

- `Moments.snapshot(kind, at)` is every lamp, bay, bin or air station at one moment of the past.
  Moments are 15 minutes apart (the clock's step); they are read and replayed six hours at a
  time and kept, so scrubbing through an evening reads the store once per kind. Only the memory
  answers: the city API is never asked about the past for the clock (a scrub would cost hundreds
  of requests), and a moment the memory holds nothing for is "unknown", never the last known
  state (the same rule as `history.replay`).
- `strip_at`, `lamps_at` and `entity_at` give the page's live strip, its night lamps and the
  inspector's record as they were at that moment.
- `track(days)` is what the clock's track draws: how much of the city the memory covers, hour
  by hour, and the events it logged (full bins, air alerts, lost connections).
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector import interpret, sun
from collector.http import ApiError
from collector.interpret import Interpretation, caqi_band

from chatbot import history
from chatbot.city import KINDS, CityData, Device, DeviceState
from chatbot.memory import Memory, Unavailable
from chatbot.settings import TIMEZONE
from chatbot.store import STATE_COLORS, point

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

STEP_MIN = 15  # the clock's moments
BLOCK_H = 6  # moments are replayed this many hours at a time
CACHE_BLOCKS = 96
LIVE_TTL_S = 60  # a block that reaches now is replayed again after a minute
TRACK_TTL_S = 300
MAX_DAYS = history.MAX_DAYS  # how far back the clock goes at most
CLOCK_KINDS = ("street_light", "parking_sensor", "waste_bin", "air_station")
NOTHING = "the twin's memory holds nothing for this moment"


class OutOfRange(ValueError):
    """A moment the clock can't show: in the future, or further back than MAX_DAYS."""


def parse_at(value: str | int | float | None, now: datetime) -> datetime | None:
    """A moment from the page or a tool: epoch milliseconds, or ISO local time (Nicosia) or with
    an offset. None (or a moment within a minute of now) means live. Raises OutOfRange."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).strip().lstrip("-").isdigit():
        t = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    else:
        try:
            t = datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise OutOfRange(f"can't read {value!r} as a moment (use 2026-11-21T21:00)") from None
        t = t.replace(tzinfo=TZ) if t.tzinfo is None else t
    if t > now + timedelta(minutes=1):
        raise OutOfRange("that moment is in the future; the clock shows the past and now")
    if t >= now - timedelta(minutes=1):
        return None
    if t < now - timedelta(days=MAX_DAYS):
        raise OutOfRange(f"the clock reaches back {MAX_DAYS} days at most")
    return t


def frame_of(at: datetime) -> datetime:
    """The clock's moment at or before `at` (every 15 minutes, on the quarter hour)."""
    step = STEP_MIN * 60
    return datetime.fromtimestamp(int(at.timestamp()) // step * step, tz=timezone.utc)


def label(at: datetime) -> str:
    return at.astimezone(TZ).strftime("%a %d %b %Y %H:%M")


def local_iso(at: datetime) -> str:
    return at.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


# -- blocks of moments -------------------------------------------------------------------------


@dataclass
class Block:
    """Six hours of moments for every device of one type, replayed from the memory."""
    device_type: str
    start: datetime
    times: list[datetime]
    devices: list[DeviceState]
    states: list[list[str]]  # [moment][device]
    values: dict[str, list[list[float | None]]]  # [field][moment][device]
    covered: list[float]  # [moment] share of the devices the memory covers, 0..1
    error: str | None = None
    built: float = field(default_factory=time.time)
    live: bool = False


@dataclass
class Snapshot:
    """One kind at one moment."""
    kind: str
    at: datetime  # the clock's moment (on the quarter hour)
    devices: list[DeviceState]
    states: list[str]
    values: list[dict]
    coverage_pct: float
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.coverage_pct > 0

    def by_state(self) -> Counter:
        return Counter(self.states)

    def reason(self) -> str | None:
        if self.error:
            return f"the twin's memory was not available ({self.error})"
        return None if self.known else NOTHING


class Moments:
    """The city at past moments, from the twin's memory, cached per kind and six hours."""

    def __init__(self, city: CityData | None, memory: Memory | None, *,
                 now: Callable[[], datetime] | None = None):
        self.city = city
        self.memory = memory
        self._now = now
        self._lock = threading.Lock()
        self._blocks: OrderedDict[tuple[str, int], Block] = OrderedDict()
        self._building: dict[tuple[str, int], threading.Lock] = {}
        self._track: dict[int, tuple[float, dict]] = {}

    def now(self) -> datetime:
        if self._now is not None:
            return self._now()
        return self.city.now() if self.city is not None else datetime.now(timezone.utc)

    # -- devices -------------------------------------------------------------------------------

    def devices(self, device_type: str) -> list[DeviceState]:
        """Every placed device of the type, with its label and street, as blank states."""
        found: list[Device] = []
        if self.city is not None:
            try:
                found = self.city.devices_of(device_type)
            except ApiError as e:
                log.info("inventory unavailable for the clock: %s", e)
        if not found and self.memory is not None:
            try:
                found = [Device(n, device_type, la, lo)
                         for n, la, lo, _ in self.memory.inventory(device_type)]
            except Unavailable:
                found = []
        blank = Interpretation(device_type, "unknown", True, None, None, "")
        out = []
        for d in found:
            if d.lat is None or d.lon is None:
                continue
            lab, street = self.city.cached_label(d) if self.city is not None else (d.name, None)
            out.append(DeviceState(d, lab, street, blank))
        return out

    # -- snapshots -----------------------------------------------------------------------------

    def block(self, device_type: str, at: datetime) -> Block:
        span = BLOCK_H * 3600
        start_s = int(at.timestamp()) // span * span
        key = (device_type, start_s)
        with self._lock:
            hit = self._blocks.get(key)
            if hit and (not hit.live or time.time() - hit.built < LIVE_TTL_S):
                self._blocks.move_to_end(key)
                return hit
            gate = self._building.setdefault(key, threading.Lock())
        with gate:  # one reader per block; the others wait for its answer
            with self._lock:
                hit = self._blocks.get(key)
                if hit and (not hit.live or time.time() - hit.built < LIVE_TTL_S):
                    return hit
            built = self._build(device_type, datetime.fromtimestamp(start_s, tz=timezone.utc))
            with self._lock:
                self._blocks[key] = built
                self._blocks.move_to_end(key)
                while len(self._blocks) > CACHE_BLOCKS:
                    self._blocks.popitem(last=False)
                self._building.pop(key, None)
            return built

    def _build(self, device_type: str, start: datetime) -> Block:
        now = self.now()
        end = start + timedelta(hours=BLOCK_H) - timedelta(minutes=STEP_MIN)
        live = end >= frame_of(now)
        end = min(end, frame_of(now))
        times = history.grid(start, end, STEP_MIN) if end >= start else [start]
        devices = self.devices(device_type)
        names = [s.device.name for s in devices]
        empty = Block(device_type, start, times, devices,
                      [["unknown"] * len(devices) for _ in times], {}, [0.0] * len(times),
                      live=live)
        if not devices or self.memory is None:
            empty.error = None if devices else "no devices of this kind are known"
            return empty
        try:
            # Only the memory: the city API is left alone for the clock (city=None).
            rd = history.readings(
                None, self.memory, device_type, names, history.HISTORY_KEYS[device_type],
                _ms(times[0]), _ms(times[-1]),
                lookback_ms=int(history.LOOKBACK[device_type].total_seconds() * 1000))
        except history.NoHistory as e:
            # Nothing recorded is an answer (unknown); a busy or broken store is asked again soon.
            if "busy" in str(e) or "can't be opened" in str(e):
                empty.error, empty.live = str(e), True
            return empty
        states, values = history.replay(device_type, devices, rd.records, times, rd.covered)
        covered = []
        for t in times:
            t_ms = _ms(t)
            inside = sum(any(s <= t_ms <= e for s, e in rd.covered.get(n, [])) for n in names)
            covered.append(inside / len(names))
        return Block(device_type, start, times, devices, states, values, covered, live=live)

    def snapshot(self, kind: str, at: datetime) -> Snapshot:
        if kind not in CLOCK_KINDS:
            raise ValueError(f"the clock replays {', '.join(CLOCK_KINDS)}, not {kind}")
        frame = frame_of(at)
        b = self.block(KINDS[kind], frame)
        k = next((i for i, t in enumerate(b.times) if t == frame), len(b.times) - 1)
        vals = [{f: rows[k][j] for f, rows in b.values.items() if rows[k][j] is not None}
                for j in range(len(b.devices))]
        return Snapshot(kind, frame, b.devices, b.states[k] if b.states else [], vals,
                        round(100 * b.covered[k], 1), b.error)

    # -- the page's readouts at a moment -------------------------------------------------------

    def strip_at(self, at: datetime, weather: dict | None = None) -> dict:
        """The live strip's readouts as they were at `at`, each honest about what the memory
        covers: a kind it holds nothing for is {"unknown": true, "reason": ...}."""
        out: dict = {"at": local_iso(frame_of(at)), "label": label(frame_of(at)), "live": False}
        snaps = {k: self.snapshot(k, at) for k in CLOCK_KINDS}

        def unknown(s: Snapshot) -> dict:
            return {"unknown": True, "reason": s.reason(), "coverage_pct": s.coverage_pct}

        lights = snaps["street_light"]
        if lights.known:
            c = lights.by_state()
            out["lights"] = {"on": c.get("on", 0), "total": len(lights.states) - c.get("unknown", 0),
                             "coverage_pct": lights.coverage_pct}
        else:
            out["lights"] = unknown(lights)
        parking = snaps["parking_sensor"]
        if parking.known:
            c = parking.by_state()
            out["parking"] = {"free": c.get("free", 0), "total": c.get("free", 0) + c.get("occupied", 0),
                              "coverage_pct": parking.coverage_pct}
        else:
            out["parking"] = unknown(parking)
        bins = snaps["waste_bin"]
        if bins.known:
            c = bins.by_state()
            out["bins"] = {"full": c.get("full", 0), "total": sum(v for s, v in c.items() if s != "unknown"),
                           "coverage_pct": bins.coverage_pct}
        else:
            out["bins"] = unknown(bins)
        air = snaps["air_station"]
        caqis = [v["caqi"] for v in air.values if isinstance(v.get("caqi"), (int, float))]
        if caqis:
            median = statistics.median(caqis)
            out["air"] = {"caqi": round(median), "band": caqi_band(median), "stations": len(caqis),
                          "coverage_pct": air.coverage_pct}
        else:
            out["air"] = unknown(air)
        if weather:
            out["weather"] = weather
        return out

    def lamps_at(self, at: datetime) -> dict:
        """The street lamps as they were at `at`, for the map's night lighting."""
        s = self.snapshot("street_light", at)
        return {"type": "FeatureCollection", "at": local_iso(s.at), "known": s.known,
                "coverage_pct": s.coverage_pct, "features": [
                    point(d.device.lon, d.device.lat, {
                        "id": d.device.name, "label": d.label, "state": st,
                        "power_w": v.get("power_w")})
                    for d, st, v in zip(s.devices, s.states, s.values)]}

    def states_at(self, kind: str, at: datetime) -> dict:
        """Every device of a kind at `at`: {id: {"s": state, ...values}}, for restating the
        layers on the map."""
        s = self.snapshot(kind, at)
        return {"kind": kind, "at": local_iso(s.at), "label": label(s.at), "known": s.known,
                "coverage_pct": s.coverage_pct, "reason": s.reason(),
                "colors": {st: STATE_COLORS.get(st, STATE_COLORS["unknown"])
                           for st in {*s.states, "unknown"}},
                "devices": {d.device.name: {"s": st, **v}
                            for d, st, v in zip(s.devices, s.states, s.values)}}

    # -- one device at a moment, for the inspector ---------------------------------------------

    def device_at(self, device: Device, at: datetime) -> tuple[Interpretation, bool]:
        """One device interpreted at `at` from every reading the memory holds for it, and
        whether the memory covers that moment."""
        blank = Interpretation(device.type, "unknown", True, None, None, NOTHING)
        if self.memory is None:
            return blank, False
        t_ms = _ms(at)
        try:
            covered = self.memory.coverage(device.type, [device.name], t_ms, t_ms)
            if not any(s <= t_ms <= e for s, e in covered.get(device.name, [])):
                return blank, False
            back = history.LOOKBACK.get(device.type, timedelta(hours=6))
            keys = interpret.READING_KEYS.get(device.type, ())
            records = self.memory.readings([device.name], keys, _ms(at - back), t_ms)
        except Unavailable as e:
            return Interpretation(device.type, "unknown", True, None, None,
                                  f"the twin's memory was not available ({e})"), False
        latest = interpret.latest_from_records(records.get(device.name, []))
        kwargs = {}
        if device.type == "Lighting":
            night = sun.is_night(at)
            kwargs = {"night": night, "night_since": sun.night_started(at) if night else None}
        return interpret.interpret(device.type, latest, at, **kwargs), True

    # -- the track: coverage and events --------------------------------------------------------

    def track(self, days: int = 31) -> dict:
        """What the clock's track shows for the last `days`: the share of each kind's devices
        the memory covers, hour by hour, the oldest moment it holds, and the events it logged
        per hour (full bins, air alerts, lost connections and other faults)."""
        days = max(1, min(MAX_DAYS, days))
        with self._lock:
            hit = self._track.get(days)
            if hit and time.time() - hit[0] < TRACK_TTL_S:
                return hit[1]
        now = self.now()
        end = now.astimezone(TZ).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        start = end - timedelta(days=days)
        hours = int((end - start).total_seconds() // 3600)
        out: dict = {"now": local_iso(now), "from": local_iso(start), "step_min": STEP_MIN,
                     "max_days": MAX_DAYS, "earliest": None,
                     "coverage": {"step_min": 60, "start": local_iso(start), "kinds": {},
                                  "all": [0] * hours},
                     "events": [], "status": "ok"}
        if self.memory is None:
            out["status"] = "off"
            return out
        t0, t1 = _ms(start), _ms(end)
        totals = [0.0] * hours
        weight = 0
        earliest: int | None = None
        try:
            for kind in CLOCK_KINDS:
                devices = self.devices(KINDS[kind])
                names = [d.device.name for d in devices]
                if not names:
                    continue
                covered = self.memory.coverage(KINDS[kind], names, t0, t1)
                acc = [0.0] * hours
                for spans in covered.values():
                    for s, e in spans:
                        earliest = s if earliest is None else min(earliest, s)
                        h = max(0, (s - t0) // 3_600_000)
                        while h < hours:
                            a, b = t0 + h * 3_600_000, t0 + (h + 1) * 3_600_000
                            if a >= e:
                                break
                            acc[h] += max(0, min(b, e) - max(a, s)) / 3_600_000
                            h += 1
                pct = [round(100 * v / len(names)) for v in acc]
                out["coverage"]["kinds"][kind] = pct
                for h in range(hours):
                    totals[h] += acc[h]
                weight += len(names)
            if weight:
                out["coverage"]["all"] = [round(100 * v / weight) for v in totals]
            out["events"] = self._events(start, end)
        except Unavailable as e:
            out["status"] = e.reason
            out["message"] = str(e)
            return out
        if earliest is not None:
            out["earliest"] = local_iso(datetime.fromtimestamp(earliest / 1000, tz=timezone.utc))
        with self._lock:
            self._track[days] = (time.time(), out)
        return out

    def _events(self, start: datetime, end: datetime) -> list[dict]:
        with self.memory.reading() as con:
            rows = con.execute(
                f"""SELECT date_trunc('hour', ts) AS hour, {EVENT_KIND} AS kind, count(*)
                    FROM events WHERE ts >= ? AND ts < ? GROUP BY ALL ORDER BY 1""",
                [start, end]).fetchall()
        by: dict[str, dict] = {}
        for hour, kind, n in rows:
            key = local_iso(hour)
            by.setdefault(key, {"t": key, "full_bins": 0, "air": 0, "faults": 0})[kind] += n
        return list(by.values())

    def events_near(self, at: datetime, hours: float = 1.0, limit: int = 8) -> list[dict]:
        """The events the memory logged in the hour before `at` (newest first)."""
        if self.memory is None:
            return []
        try:
            with self.memory.reading() as con:
                rows = con.execute(
                    f"""SELECT ts, device, message, {EVENT_KIND} FROM events
                        WHERE ts > ? AND ts <= ? ORDER BY ts DESC LIMIT ?""",
                    [at - timedelta(hours=hours), at, limit]).fetchall()
        except Unavailable:
            return []
        return [{"time": ts.astimezone(TZ).strftime("%H:%M"), "device": d, "event": m, "kind": k}
                for ts, d, m, k in rows]


# How an event reads on the track: a full bin, an air alert, or a fault (lost connections, a
# lamp or screen reporting a problem).
EVENT_KIND = """CASE WHEN message ILIKE '%fill level above%' THEN 'full_bins'
                     WHEN message ILIKE '%CAQI%' OR message ILIKE '%AQI not reported%' THEN 'air'
                     ELSE 'faults' END"""

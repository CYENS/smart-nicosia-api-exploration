"""Street lighting policy: what switching by the sun, dimming, switching off quiet streets late
at night, or an LED retrofit would save on the lamps the twin measures, against what they draw
today, and where it would leave streets darker.

The baseline is each lamp's own behaviour, learnt from its readings in the period (see
lighting_model.py): its power at full light, its standby, and its switching times as minutes
after sunset and sunrise. A policy is replayed on every night of the year before the period
ends, with the sun's real times for each date. The backtest replays today's schedule over the
period and compares it with the lamps' own energy counters; the holdout learns the schedule on
the first half of the period and replays the second.

Night footfall comes from the city's people-counting cameras: one request per camera for its
hour-of-day profile over the last 30 days (cached for the day). A lamp takes the counts of a
camera on its street within 250 m, or of any camera within 100 m; elsewhere nobody counts.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from chatbot.geo import haversine_m
from chatbot.scenarios import lighting_model as lm
from chatbot.scenarios.base import Period, Result, Scenario, ScenarioContext, ScenarioError, kpi
from chatbot.scenarios.registry import register

log = logging.getLogger(__name__)
TZ = lm.TZ


def _times() -> list[str]:
    """Half hours through the night, 18:00 to 07:30."""
    out = []
    for k in range(28):
        h = (18 + k // 2) % 24
        out.append(f"{h:02d}:{30 * (k % 2):02d}")
    return out


TIMES = _times()

PARAMS = {
    "type": "object",
    "properties": {
        "switch_by_sun": {
            "type": "boolean", "default": False, "title": "Switch by sunset and sunrise",
            "description": "Switch on at civil dusk plus an offset and off at civil dawn minus an "
                           "offset, in place of today's switching.", "x-group": "switching"},
        "dusk_offset_min": {
            "type": "integer", "default": 0, "minimum": -30, "maximum": 60, "x-step": 5, "x-unit": "min",
            "title": "Switch on after civil dusk",
            "description": "Minutes after civil dusk (the sun 6° below the horizon, about 25 minutes "
                           "after sunset) the lamps come on. Negative is before.",
            "x-group": "switching", "x-when": "switch_by_sun"},
        "dawn_offset_min": {
            "type": "integer", "default": 0, "minimum": -30, "maximum": 60, "x-step": 5, "x-unit": "min",
            "title": "Switch off before civil dawn",
            "description": "Minutes before civil dawn (about 25 minutes before sunrise) the lamps go "
                           "off. Negative is after.",
            "x-group": "switching", "x-when": "switch_by_sun"},
        "dim": {
            "type": "boolean", "default": False, "title": "Dim late at night",
            "description": "Dim every lamp to one level in one window of the night.", "x-group": "dimming"},
        "dim_level": {
            "type": "integer", "default": 50, "minimum": 10, "maximum": 90, "x-step": 5, "x-unit": "%",
            "title": "Dim to", "description": "The light level while dimmed, as a share of full light.",
            "x-group": "dimming", "x-when": "dim"},
        "dim_from": {"type": "string", "enum": TIMES, "default": "01:00", "title": "Dim from",
                     "description": "When the dimming starts.", "x-group": "dimming", "x-when": "dim"},
        "dim_to": {"type": "string", "enum": TIMES, "default": "05:00", "title": "Dim until",
                   "description": "When full light comes back (or dawn, if earlier).",
                   "x-group": "dimming", "x-when": "dim"},
        "part_night": {
            "type": "boolean", "default": False, "title": "Switch off quiet streets late at night",
            "description": "Switch off the lamps on streets where the cameras count almost no one "
                           "in that window.", "x-group": "part_night"},
        "off_from": {"type": "string", "enum": TIMES, "default": "01:00", "title": "Off from",
                     "description": "When the quiet streets go dark.", "x-group": "part_night",
                     "x-when": "part_night"},
        "off_to": {"type": "string", "enum": TIMES, "default": "05:00", "title": "Off until",
                   "description": "When they light again (or dawn, if earlier).", "x-group": "part_night",
                   "x-when": "part_night"},
        "quiet_max_people": {
            "type": "number", "default": 5, "minimum": 0, "maximum": 50, "x-step": 1,
            "x-unit": "people an hour", "title": "A street counts as quiet below",
            "description": "The average people an hour its camera counts in that window, over the "
                           "last 30 days. Dimmed or dark lamps on busier streets are flagged.",
            "x-group": "part_night"},
        "include_unmeasured": {
            "type": "boolean", "default": False, "title": "Also switch off streets no camera counts",
            "description": "Off by default: without a camera nobody knows who walks there at night.",
            "x-group": "part_night", "x-when": "part_night"},
        "retrofit": {
            "type": "boolean", "default": False, "title": "Replace non-LED lamps with LED",
            "description": "Replace the lamps whose power at full light shows they are not LED.",
            "x-group": "retrofit"},
        "retrofit_min_w": {
            "type": "integer", "default": 90, "minimum": 40, "maximum": 200, "x-step": 5, "x-unit": "W",
            "title": "Count a lamp as non-LED from",
            "description": "A lamp drawing at least this at full light counts as a sodium or similar "
                           "lamp. 90 W is above every LED class the city runs today.",
            "x-group": "retrofit", "x-when": "retrofit"},
        "safety_radius_m": {
            "type": "integer", "default": 50, "minimum": 20, "maximum": 150, "x-step": 10, "x-unit": "m",
            "title": "Safety check distance",
            "description": "A dimmed or dark lamp this close to a bus stop or a dangerous building is "
                           "flagged.", "x-group": "safety"},
        "price_eur_kwh": {
            "type": "number", "default": 0.20, "minimum": 0.20, "maximum": 0.32, "x-step": 0.01,
            "x-unit": "EUR/kWh", "x-assumption": True, "title": "Electricity price",
            "x-source": "The lamps' own records: 101 lamps carry 0.20 EUR/kWh, 21 carry 0.32, the "
                        "other 118 none.",
            "description": "What a kWh costs the city."},
        "co2_kg_kwh": {
            "type": "number", "default": 0.55, "minimum": 0.47, "maximum": 0.63, "x-step": 0.01,
            "x-unit": "kg CO2/kWh", "x-assumption": True, "title": "Grid emission factor",
            "x-source": "EAC's 2023 energy mix: 0.627 kg per kWh sold. Ember's 2025 data: about "
                        "0.49, falling as solar grows.",
            "description": "CO2 emitted for each kWh the lamps draw from the Cyprus grid."},
        "dim_power_floor_pct": {
            "type": "integer", "default": 10, "minimum": 0, "maximum": 25, "x-step": 1, "x-unit": "%",
            "x-assumption": True, "title": "Power kept when dimmed",
            "x-source": "Typical LED driver curves. These lamps never dimmed in the data, so it "
                        "can't be measured here yet.",
            "description": "The share of full power a dimmed lamp still draws whatever its level; the "
                           "rest falls in step with the light."},
        "led_power_pct": {
            "type": "integer", "default": 55, "minimum": 43, "maximum": 74, "x-step": 1, "x-unit": "%",
            "x-assumption": True, "title": "LED power for the same light",
            "x-source": "US DOE GATEWAY demonstrations (PNNL, New York City, 2011): LED luminaires "
                        "used 26% to 57% less than the sodium lamps they replaced.",
            "description": "An LED lamp's power as a share of the lamp it replaces."},
        "led_cost_eur": {
            "type": "integer", "default": 400, "minimum": 250, "maximum": 600, "x-step": 10,
            "x-unit": "EUR per lamp", "x-assumption": True, "title": "LED lamp, installed",
            "x-source": "A planning figure for an LED street luminaire and its fitting; replace it "
                        "with the city's own tender prices.",
            "description": "What replacing one lamp costs."},
    },
}

# Map colours, for the night map: energy glows warmer the more a lamp draws; savings in green.
ENERGY_BANDS = ((150, "Under 150 kWh a year", "#5a4626"), (250, "150 to 250 kWh", "#9a6a28"),
                (350, "250 to 350 kWh", "#dc8f2e"), (math.inf, "350 kWh or more", "#ffc46b"))
SAVING_BANDS = (("Uses more", "#ff5a4e"), ("No change", "#5b6b80"), ("Saves under 10%", "#2f6b4c"),
                ("Saves 10% to 40%", "#3aa56a"), ("Saves 40% or more", "#86efac"))
SAFETY_STATES = {"night_people": ("Night pedestrians", "#ff5a4e"),
                 "dangerous_building": ("Dangerous building", "#f59e0b"),
                 "bus_stop": ("Bus stop", "#38bdf8")}
STREET_COLOR = "#ff5a4e"
BASIS_LABELS = {"measured": "measured from readings either side of each switch",
                "meter": "from its energy counter across the silent day",
                "city": "assumed like the closely read lamps"}
CAMERA_ON_STREET_M = 250
CAMERA_ANY_M = 100
FOOTFALL_DAYS = 30


# -- reading the data --------------------------------------------------------------------------


@dataclass
class Lamp:
    id: str
    lat: float
    lon: float
    label: str
    street: str
    tariff: float | None
    camera: dict | None = None  # {id, name, distance_m, hours: [24 people an hour]}
    stop: tuple[str, float] | None = None  # nearest bus stop (name, metres)
    building: dict | None = None  # nearest register building {id, address, status, distance_m}


@dataclass
class Data:
    period: Period
    lamps: dict[str, Lamp]
    schedules: dict[str, lm.Schedule]
    city: dict
    year: list[lm.Night]
    profile: lm.Night
    backtest: dict
    no_power: list[str]
    notes: list[str]
    period_nights: int
    lit_hours_now: float  # today's average lit hours a night over the period, across lamps
    cameras: list[dict] = field(default_factory=list)
    street_cameras: dict[str, list[dict]] = field(default_factory=dict)  # every camera on a street's lamps
    memo: dict = field(default_factory=dict)

    def street_people(self, street: str, t_from: str, t_to: str) -> float | None:
        """People an hour on a street between two clock times: its busiest camera's count, or
        None where no camera counts."""
        cams = self.street_cameras.get(street)
        return max(_people(c["hours"], t_from, t_to) for c in cams) if cams else None


def _letters(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if c.isalpha() and not unicodedata.combining(c))
    return s.replace("ph", "f")


def _same_street(a: str, b: str) -> bool:
    a, b = _letters(a), _letters(b)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.8)


def street_of(name: str | None) -> str | None:
    """'Makariou 13' -> 'Makariou'; 'DAVILLA NEW 17' -> 'Davilla'; a controller's name -> None."""
    if not name or re.match(r"(?i)^(nema|light controller)\b", name):
        return None
    s = re.sub(r"\s*\d+[A-Za-z*]?\s*$", "", name).strip()
    s = re.sub(r"(?i)\s+new$", "", s).strip()
    if not s:
        return None
    return s.title() if s.isupper() else s


def _load(ctx: ScenarioContext, start: float, end: float) -> tuple[list[tuple], dict[str, lm.Readings]]:
    if ctx.memory is None:
        raise ScenarioError("the twin's memory isn't available, and the lighting scenario learns "
                            "from it")
    try:
        with ctx.memory.reading(timeout=10.0) as con:
            devices = con.execute(
                """SELECT name, lat, lon, display_name, metadata FROM devices
                   WHERE type = 'Lighting' AND coalesce(in_inventory, true) AND lat IS NOT NULL
                   ORDER BY name""").fetchall()
            rows = con.execute(
                """SELECT t.device, epoch_ms(t.ts) / 1000.0 AS t,
                          CASE WHEN t.key = 'level' THEN try_cast(t.value AS DOUBLE) END,
                          CASE WHEN t.key = 'powerstats' AND json_valid(t.value) THEN coalesce(
                              try_cast(json_extract_string(t.value, '$.last_pwr_act_avg') AS DOUBLE),
                              try_cast(json_extract_string(t.value, '$.last_pwr_act') AS DOUBLE)) END,
                          CASE WHEN t.key = 'powerstats' AND json_valid(t.value) THEN
                              try_cast(json_extract_string(t.value, '$.last_energy_act') AS DOUBLE) END,
                          t.key
                   FROM telemetry t JOIN devices d ON d.name = t.device
                   WHERE d.type = 'Lighting' AND t.key IN ('level', 'powerstats')
                     AND t.ts BETWEEN make_timestamptz(?::BIGINT * 1000000) AND make_timestamptz(?::BIGINT * 1000000)
                   ORDER BY t.device, t.ts""", [int(start), int(end)]).fetchall()
    except ScenarioError:
        raise
    except Exception as e:  # noqa: BLE001 - Unavailable or a broken file: say so plainly
        raise ScenarioError(f"the twin's memory can't be read just now ({e}); try again in a moment") from None
    readings: dict[str, lm.Readings] = defaultdict(lm.Readings)
    for device, t, level, w, e, key in rows:
        r = readings[device]
        if key == "level" and level is not None:
            r.states.append((t, level > 0))
        elif key == "powerstats":
            if w is not None or e is not None:
                r.power.append((t, w, e))
            if w is not None:
                r.states.append((t, w > lm.LIT_W))
    for r in readings.values():
        r.states.sort()
    return devices, dict(readings)


def _camera_rows(ctx: ScenarioContext) -> list[dict]:
    rows: list[dict] = []
    if ctx.city is not None:
        try:
            rows = ctx.city.traffic_cameras()
        except Exception as e:  # noqa: BLE001
            log.warning("traffic cameras not listed: %s", e)
    if not rows and ctx.memory is not None:
        try:
            with ctx.memory.reading() as con:
                rows = [{"analytics_id": a, "name": n, "lat": la, "lon": lo, "lines": li}
                        for a, n, la, lo, li in con.execute(
                            "SELECT analytics_id, name, lat, lon, lines FROM traffic_cameras").fetchall()]
        except Exception as e:  # noqa: BLE001
            log.warning("traffic cameras not read from the memory: %s", e)
    out = []
    for r in rows:
        try:
            lines = json.loads(r.get("lines") or "[]") if isinstance(r.get("lines"), str) else (r.get("lines") or [])
        except ValueError:
            lines = []
        if r.get("lat") is None or not any(0 in (ln.get("types") or []) for ln in lines):
            continue  # no coordinates, or it doesn't count people
        out.append({"id": int(r["analytics_id"]), "name": r.get("name") or str(r["analytics_id"]),
                    "lat": float(r["lat"]), "lon": float(r["lon"])})
    return out


def footfall(ctx: ScenarioContext, end: datetime, cameras: list[dict]) -> tuple[list[dict], str | None]:
    """Each people-counting camera's average people an hour, hour by hour of the day, over the
    30 days before `end`'s local midnight. One request per camera, kept for the day."""
    stop = datetime.combine(end.astimezone(TZ).date(), datetime.min.time(), TZ)
    start = stop - timedelta(days=FOOTFALL_DAYS)
    cache: Path | None = None
    if ctx.cache_dir is not None:
        cache = Path(ctx.cache_dir) / f"footfall_{stop:%Y%m%d}.json"
        if cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                hours = {int(k): v for k, v in saved["hours"].items()}
                return [{**c, "hours": hours[c["id"]]} for c in cameras if c["id"] in hours], None
            except (ValueError, KeyError):
                pass
    if ctx.city is None or not cameras:
        return [], "The camera counts can't be read just now, so no street counts as measured."

    def one(cam: dict):
        try:
            payload = ctx.city.client.get_json(
                "getTrafficReports", group_by="hour", start_date=int(start.timestamp() * 1000),
                end_date=int(stop.timestamp() * 1000) - 1, va_ids=f"[{cam['id']}]")
        except Exception as e:  # noqa: BLE001 - one camera failing leaves it unmeasured
            log.warning("footfall for camera %s failed: %s", cam["id"], e)
            return cam, None
        hours = [0.0] * 24
        for b in (payload.get("data") or {}).get("group_by") or []:
            m = re.fullmatch(r"(\d{2}):\d{2}", str(b.get("key", "")))
            people = ((b.get("data") or {}).get("0") or {}).get("total")
            if m and people is not None:
                hours[int(m.group(1))] = round(float(people) / FOOTFALL_DAYS, 2)
        return cam, hours

    got = {cam["id"]: h for cam, h in ctx.city.each(one, cameras) if h is not None}
    if cache is not None and got:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"hours": got}), encoding="utf-8")
        except OSError:
            pass
    note = None if got else "The camera counts can't be read just now, so no street counts as measured."
    return [{**c, "hours": got[c["id"]]} for c in cameras if c["id"] in got], note


def _stops(ctx: ScenarioContext, box) -> tuple[list[tuple[str, float, float]], str | None]:
    tt = None
    if ctx.mobility is not None:
        try:
            tt = ctx.mobility.timetable(wait_s=15.0)
        except Exception as e:  # noqa: BLE001
            log.warning("bus timetable not read: %s", e)
    if tt is None:
        return [], "The bus timetable isn't loaded, so the safety check couldn't look for bus stops."
    s, w, n, e = box
    return [(st.name, st.lat, st.lon) for st in tt.stops if s <= st.lat <= n and w <= st.lon <= e], None


def _buildings(ctx: ScenarioContext, box) -> tuple[list[dict], str | None]:
    recs = []
    if ctx.registers is not None:
        try:
            recs = ctx.registers.records(wait_s=10.0)
        except Exception as e:  # noqa: BLE001
            log.warning("registers not read: %s", e)
    if not recs:
        return [], ("The dangerous-building registers aren't loaded, so the safety check couldn't "
                    "look for them.")
    s, w, n, e = box
    return [{"id": r["id"], "address": r.get("address") or r.get("street") or "", "status": r.get("status"),
             "lat": r["lat"], "lon": r["lon"]} for r in recs
            if r.get("lat") is not None and s <= r["lat"] <= n and w <= r["lon"] <= e], None


def _nearest(lat: float, lon: float, items, key=lambda x: (x[1], x[2])):
    best, dist = None, math.inf
    for it in items:
        la, lo = key(it)
        d = haversine_m(lat, lon, la, lo)
        if d < dist:
            best, dist = it, d
    return best, dist


def _streets(devices: list[tuple], ctx: ScenarioContext) -> dict[str, str]:
    """Each lamp's street: from its name, else from the nearest place with a street, else from the
    nearest lamp that has one. Spellings of one street are merged."""
    named = {d[0]: street_of(d[3]) for d in devices}
    canon: list[str] = []
    counts: Counter = Counter()
    for s in named.values():
        if s:
            counts[s] += 1
    for s, _ in counts.most_common():
        if not any(_same_street(s, c) for c in canon):
            canon.append(s)

    def canonical(s: str) -> str:
        return next((c for c in canon if _same_street(s, c)), s)

    places = []
    if ctx.places is not None and getattr(ctx.places, "ready", False):
        try:
            places = [p for p in ctx.places.all() if p.get("street")]
        except Exception:  # noqa: BLE001
            places = []
    out = {}
    pos = {d[0]: (d[1], d[2]) for d in devices}
    for name, s in named.items():
        if s:
            out[name] = canonical(s)
            continue
        la, lo = pos[name]
        p, dist = _nearest(la, lo, places, key=lambda p: (p["lat"], p["lon"])) if places else (None, math.inf)
        if p is not None and dist <= 60:
            out[name] = canonical(str(p["street"]))
            continue
        others = [(n, *pos[n]) for n, x in named.items() if x]
        o, dist = _nearest(la, lo, others) if others else (None, math.inf)
        out[name] = canonical(named[o[0]]) if o is not None and dist <= 80 else "An unnamed street"
    return out


def _camera_for(lamp: Lamp, cameras: list[dict]) -> dict | None:
    best = None
    for c in cameras:
        d = haversine_m(lamp.lat, lamp.lon, c["lat"], c["lon"])
        words = re.split(r"[^A-Za-zΑ-Ωα-ω]+", c["name"])
        on_street = any(_same_street(lamp.street, w) for w in words if len(w) > 3) or \
            _letters(lamp.street) in _letters(c["name"])
        if (on_street and d <= CAMERA_ON_STREET_M) or d <= CAMERA_ANY_M:
            rank = (0 if on_street else 1, d)
            if best is None or rank < best[0]:
                best = (rank, {"id": c["id"], "name": c["name"], "distance_m": round(d), "hours": c["hours"],
                               "on_street": on_street})
    return best[1] if best else None


def prepare(ctx: ScenarioContext, period: Period) -> Data:
    start, end = period.start.timestamp(), period.end.timestamp()
    devices, readings = _load(ctx, start - 86400, end)
    if not devices:
        raise ScenarioError("the twin's memory holds no street lamps")
    nights_p = lm.nights_between(start, end)
    by_day = {n.day: n for n in nights_p}
    for lamp_id, *_ in devices:
        readings.setdefault(lamp_id, lm.Readings())
    schedules, city = lm.infer(readings, nights_p)
    if not any(s.watts for s in schedules.values()):
        raise ScenarioError("no lamp reported its power in this period, so there is nothing to model; "
                            "try a longer period")

    # The backtest: the whole period, and the honest one (learn the first half, replay the second).
    full = lm.replay(schedules, readings, start, end, by_day)
    mid = start + (end - start) / 2
    first_half, _ = lm.infer({k: r.before(mid) for k, r in readings.items()},
                             [n for n in nights_p if n.sunset < mid])
    for k, s in first_half.items():
        if s.watts is None:
            s.watts = schedules[k].watts  # a lamp first seen lit later keeps its wattage
    holdout = lm.replay(first_half, readings, mid, end, by_day)

    streets = _streets(devices, ctx)
    lamps = {}
    for name, lat, lon, label, meta in devices:
        tariff = None
        try:
            tariff = float(json.loads(meta or "{}").get("extra", {}).get("cost") or 0) or None
        except (ValueError, AttributeError):
            pass
        lamps[name] = Lamp(name, float(lat), float(lon), label or name, streets[name], tariff)
    lats = [x.lat for x in lamps.values()]
    lons = [x.lon for x in lamps.values()]
    box = (min(lats) - 0.003, min(lons) - 0.004, max(lats) + 0.003, max(lons) + 0.004)
    notes = []
    cameras, note = footfall(ctx, period.end, [c for c in _camera_rows(ctx)
                                               if box[0] <= c["lat"] <= box[2] and box[1] <= c["lon"] <= box[3]])
    if note:
        notes.append(note)
    stops, note = _stops(ctx, box)
    if note:
        notes.append(note)
    buildings, note = _buildings(ctx, box)
    if note:
        notes.append(note)
    for lamp in lamps.values():
        lamp.camera = _camera_for(lamp, cameras)
        st, d = _nearest(lamp.lat, lamp.lon, stops) if stops else (None, math.inf)
        lamp.stop = (st[0], round(d)) if st is not None else None
        b, d = _nearest(lamp.lat, lamp.lon, buildings, key=lambda r: (r["lat"], r["lon"])) if buildings else (None, math.inf)
        lamp.building = {**{k: b[k] for k in ("id", "address", "status")}, "distance_m": round(d)} if b else None

    street_cameras: dict[str, dict[int, dict]] = defaultdict(dict)
    for lamp in lamps.values():
        if lamp.camera:
            street_cameras[lamp.street][lamp.camera["id"]] = lamp.camera
    end_day = period.end.astimezone(TZ).date()
    year = [lm.night(end_day - timedelta(days=k)) for k in range(365, 0, -1)]
    profile = lm.night(end_day - timedelta(days=1))
    if profile.sunrise > end:
        profile = lm.night(end_day - timedelta(days=2))
    modelled = [s for s in schedules.values() if s.watts]
    lit_now = statistics.mean(
        (n.sunrise + s.off_min * 60 - n.sunset - s.on_min * 60) / 3600 for s in modelled for n in nights_p[1:])
    return Data(period, lamps, schedules, city, year, profile,
                {"full": full, "holdout": holdout, "mid": mid}, [k for k, s in schedules.items() if not s.watts],
                notes, len(nights_p) - 1, lit_now, cameras,
                {k: list(v.values()) for k, v in street_cameras.items()})


# -- a policy over a year ----------------------------------------------------------------------


def _people(hours: list[float], t_from: str, t_to: str) -> float:
    """Average people an hour between two clock times of the night (half-hour steps)."""
    i, j = TIMES.index(t_from), TIMES.index(t_to)
    vals = [hours[int(TIMES[k][:2])] for k in range(i, j)]
    return sum(vals) / len(vals) if vals else 0.0


def _policy(params: dict) -> lm.Policy:
    return lm.Policy(params["switch_by_sun"], params["dusk_offset_min"], params["dawn_offset_min"],
                     params["dim"], params["dim_level"], params["dim_from"], params["dim_to"],
                     params["part_night"], params["off_from"], params["off_to"])


POLICY_KEYS = ("switch_by_sun", "dusk_offset_min", "dawn_offset_min", "dim", "dim_level", "dim_from",
               "dim_to", "part_night", "off_from", "off_to", "quiet_max_people", "include_unmeasured",
               "retrofit", "retrofit_min_w", "safety_radius_m")


def physical(data: Data, params: dict) -> dict:
    """Hours per lamp over the year, by month, through the profile night, and the safety check,
    for one policy (memoised: the assumptions don't change hours)."""
    key = tuple(params[k] for k in POLICY_KEYS)
    if key in data.memo:
        return data.memo[key]
    pol = _policy(params)
    quiet = params["quiet_max_people"]
    radius = params["safety_radius_m"]
    lamps_out = {}
    months: dict[str, list[float]] = defaultdict(lambda: [0.0] * 7)
    slots = [data.profile.sunset - 3600 + k * 900 for k in range(int((data.profile.sunrise - data.profile.sunset + 7200) / 900) + 1)]
    prof = [[0.0] * 7 for _ in slots]
    for lid, s in data.schedules.items():
        if not s.watts:
            continue
        lamp = data.lamps[lid]
        # A street goes dark or stays lit as a whole, by its busiest camera.
        off_people = data.street_people(lamp.street, pol.off_from, pol.off_to)
        counted = off_people is not None
        dark_ok = (counted and off_people < quiet) or (not counted and params["include_unmeasured"])
        cand = bool(params["retrofit"]) and s.watts >= params["retrofit_min_w"]
        base = full = dim = off = 0.0
        for n in data.year:
            b, f, d, o = lm.lamp_night(s, n, pol, dark_ok)
            base, full, dim, off = base + b, full + f, dim + d, off + o
            m = datetime.fromtimestamp(n.sunset, TZ).strftime("%Y-%m")
            acc = months[m]
            # base lit Wh, base standby Wh, full Wh (not candidates / candidates), dimmed Wh (same),
            # scenario standby Wh: the assumptions scale them in evaluate().
            acc[0] += s.watts * b / 3600
            acc[1] += s.standby * (86400 - b) / 3600
            acc[2 + cand] += s.watts * f / 3600
            acc[4 + cand] += s.watts * d / 3600
            acc[6] += s.standby * (86400 - f - d) / 3600
        # The profile night, quarter hour by quarter hour.
        n = data.profile
        pa, pb = n.sunset + s.on_min * 60, n.sunrise + s.off_min * 60
        if pol.switch_by_sun:
            sa, sb = n.dusk + pol.dusk_offset_min * 60, n.dawn - pol.dawn_offset_min * 60
        else:
            sa, sb = pa, pb
        o1, o2 = (lm.clock_at(n, pol.off_from), lm.clock_at(n, pol.off_to)) if pol.part_night and dark_ok else (0, 0)
        d1, d2 = (lm.clock_at(n, pol.dim_from), lm.clock_at(n, pol.dim_to)) if pol.dim else (0, 0)
        for i, t in enumerate(slots):
            row = prof[i]
            if pa <= t < pb:
                row[0] += s.watts
            else:
                row[1] += s.standby
            if sa <= t < sb and not (o1 <= t < o2):
                row[(4 if d1 <= t < d2 else 2) + cand] += s.watts
            else:
                row[6] += s.standby
        reduced = dim + off
        reasons = []
        people = None
        if reduced > 0 and counted:
            win = [data.street_people(lamp.street, pol.dim_from, pol.dim_to)] if dim > 0 else []
            if off > 0:
                win.append(off_people)
            people = max(win)
            if people >= quiet:
                reasons.append("night_people")
        if reduced > 0 and lamp.building and lamp.building["distance_m"] <= radius:
            reasons.append("dangerous_building")
        if reduced > 0 and lamp.stop and lamp.stop[1] <= radius:
            reasons.append("bus_stop")
        lamps_out[lid] = {"base_h": base / 3600, "full_h": full / 3600, "dim_h": dim / 3600,
                          "off_h": off / 3600, "cand": cand, "dark_ok": dark_ok, "reasons": reasons,
                          "people": people, "reduced_h": reduced / 3600}
    out = {"lamps": lamps_out, "months": dict(sorted(months.items())), "slots": slots, "profile": prof}
    data.memo[key] = out
    return out


def _band(kwh: float) -> tuple[str, str]:
    for limit, label, color in ENERGY_BANDS:
        if kwh < limit:
            return label, color
    return ENERGY_BANDS[-1][1], ENERGY_BANDS[-1][2]


def _saving_band(base: float, scen: float) -> tuple[str, str]:
    colors = dict(SAVING_BANDS)
    if scen > base + 0.5:
        return "Uses more", colors["Uses more"]
    share = (base - scen) / base if base > 0 else 0
    if share < 0.005:
        return "No change", colors["No change"]
    if share < 0.10:
        return "Saves under 10%", colors["Saves under 10%"]
    if share < 0.40:
        return "Saves 10% to 40%", colors["Saves 10% to 40%"]
    return "Saves 40% or more", colors["Saves 40% or more"]


def _point(lamp: Lamp, props: dict) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lamp.lon, lamp.lat]},
            "properties": {"id": lamp.id, "label": f"{lamp.label} ({lamp.id})", "street": lamp.street, **props}}


def _street_line(points: list[tuple[float, float]]) -> list[list[float]]:
    """A street drawn through its lamps, in order along the line they make."""
    if len(points) < 2:
        return [[lo, la] for la, lo in points]
    mla = sum(p[0] for p in points) / len(points)
    mlo = sum(p[1] for p in points) / len(points)
    kx = math.cos(math.radians(mla))
    xs = [((lo - mlo) * kx, la - mla) for la, lo in points]
    sxx = sum(x * x for x, _ in xs)
    syy = sum(y * y for _, y in xs)
    sxy = sum(x * y for x, y in xs)
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(angle), math.sin(angle)
    order = sorted(range(len(points)), key=lambda i: xs[i][0] * ux + xs[i][1] * uy)
    return [[points[i][1], points[i][0]] for i in order]


def evaluate(data: Data, params: dict) -> Result:
    ph = physical(data, params)
    price, factor = params["price_eur_kwh"], params["co2_kg_kwh"]
    floor = params["dim_power_floor_pct"] / 100
    share = floor + (1 - floor) * params["dim_level"] / 100  # a dimmed lamp's share of full power
    led = params["led_power_pct"] / 100
    hours_year = 24 * len(data.year)

    per_lamp = {}
    for lid, h in ph["lamps"].items():
        s = data.schedules[lid]
        w = s.watts * (led if h["cand"] else 1.0)
        base = (s.watts * h["base_h"] + s.standby * (hours_year - h["base_h"])) / 1000
        scen = (w * (h["full_h"] + h["dim_h"] * share)
                + s.standby * (hours_year - h["full_h"] - h["dim_h"])) / 1000
        led_saving = (s.watts * (1 - led) * (h["full_h"] + h["dim_h"] * share)) / 1000 if h["cand"] else 0.0
        per_lamp[lid] = (base, scen, led_saving)
    e_base = sum(b for b, _, _ in per_lamp.values())
    e_scen = sum(s for _, s, _ in per_lamp.values())
    cands = [lid for lid, h in ph["lamps"].items() if h["cand"]]
    flagged = {lid: h for lid, h in ph["lamps"].items() if h["reasons"]}
    unknown = [lid for lid, h in ph["lamps"].items()
               if h["reduced_h"] > 0 and data.street_people(data.lamps[lid].street, "01:00", "01:30") is None]
    kpis = [
        kpi("energy", "Energy a year", "MWh", e_base / 1000, e_scen / 1000, digits=1),
        kpi("cost", "Electricity cost a year", "EUR", e_base * price, e_scen * price, digits=0),
        kpi("co2", "CO2 a year", "t", e_base * factor / 1000, e_scen * factor / 1000, digits=2),
        kpi("safety", "Dimmed or dark lamp-hours at flagged lamps", "lamp-hours a year", 0.0,
            sum(h["reduced_h"] for h in flagged.values()), digits=0,
            note=f"{len(flagged)} lamps near night pedestrians, a bus stop or a dangerous building"),
    ]
    if params["retrofit"]:
        invest = len(cands) * params["led_cost_eur"]
        saving = sum(x for _, _, x in per_lamp.values()) * price
        kpis.append(kpi("investment", "LED investment", "EUR", 0.0, float(invest), digits=0,
                        note=f"{len(cands)} lamps at {params['led_cost_eur']} EUR each"))
        kpis.append(kpi("payback", "LED payback", "years", None, invest / saving if saving > 0 else None,
                        digits=1, note="The retrofit's cost over what it alone saves a year, at "
                                       "today's price; upkeep savings not counted"))

    # Map layers: each lamp's energy today and in the scenario, what it saves, and the flags.
    base_feats, scen_feats, change_feats, flag_feats = [], [], [], []
    for lid, (b, s_, _) in per_lamp.items():
        lamp, h, sch = data.lamps[lid], ph["lamps"][lid], data.schedules[lid]
        common = {"watts": round(sch.watts, 1), "kwh_base": round(b, 1), "kwh_scenario": round(s_, 1),
                  "kwh_saved": round(b - s_, 1), "eur_saved": round((b - s_) * price, 2),
                  "schedule": BASIS_LABELS[sch.basis]}
        st, col = _band(b)
        base_feats.append(_point(lamp, {**common, "state": st, "color": col, "value": round(b, 1)}))
        st, col = _band(s_)
        scen_feats.append(_point(lamp, {**common, "state": st, "color": col, "value": round(s_, 1)}))
        st, col = _saving_band(b, s_)
        change_feats.append(_point(lamp, {**common, "state": st, "color": col, "value": round(b - s_, 1),
                                          "popup": f"{lamp.label} ({lid})\n{b:,.0f} kWh a year today, "
                                                   f"{s_:,.0f} in the scenario"}))
        if h["reasons"]:
            st, col = SAFETY_STATES[h["reasons"][0]]
            flag_feats.append(_point(lamp, {"state": st, "color": col, "value": round(h["reduced_h"]),
                                            "reasons": [SAFETY_STATES[r][0] for r in h["reasons"]],
                                            "lamp_hours": round(h["reduced_h"])}))
    by_street: dict[str, list[str]] = defaultdict(list)
    for lid in flagged:
        by_street[data.lamps[lid].street].append(lid)
    street_feats = []
    for street, ids in sorted(by_street.items()):
        if len(ids) >= 2 and street != "An unnamed street":
            street_feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": _street_line(
                [(data.lamps[i].lat, data.lamps[i].lon) for i in ids])},
                "properties": {"id": f"street:{street}", "label": street, "state": "Flagged street",
                               "color": STREET_COLOR, "lamps": len(ids)}})
    legend_energy = {label: color for _, label, color in ENERGY_BANDS}
    layers = {
        "baseline": {"type": "FeatureCollection", "features": base_feats,
                     "meta": {"title": "Today: energy a year per lamp", "legend": legend_energy,
                              "variant": "baseline", "value_field": "value", "unit": "kWh", "geometry": "point"}},
        "scenario": {"type": "FeatureCollection", "features": scen_feats,
                     "meta": {"title": "Scenario: energy a year per lamp", "legend": legend_energy,
                              "variant": "scenario", "value_field": "value", "unit": "kWh", "geometry": "point"}},
        "change": {"type": "FeatureCollection", "features": change_feats,
                   "meta": {"title": "What each lamp saves a year", "legend": dict(SAVING_BANDS),
                            "variant": None, "value_field": "kwh_saved", "unit": "kWh", "geometry": "point"}},
        "safety": {"type": "FeatureCollection", "features": street_feats + flag_feats,
                   "meta": {"title": "Flagged by the safety check", "show": True,
                            "legend": {**{v[0]: v[1] for v in SAFETY_STATES.values()}, "Flagged street": STREET_COLOR},
                            "variant": None, "value_field": "lamp_hours", "unit": "lamp-hours", "geometry": "mixed"}},
    }

    # Series: a night quarter hour by quarter hour, the year by month, and the backtest by day.
    series = []
    for t, row in zip(ph["slots"], ph["profile"]):
        when = datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%dT%H:%M")
        base_w = row[0] + row[1]
        scen_w = row[2] + row[3] * led + (row[4] + row[5] * led) * share + row[6]
        series += [{"metric": "night_kw", "time": when, "variant": "baseline", "value": round(base_w / 1000, 3)},
                   {"metric": "night_kw", "time": when, "variant": "scenario", "value": round(scen_w / 1000, 3)}]
    for m, a in ph["months"].items():
        series += [{"metric": "monthly_mwh", "time": m, "variant": "baseline", "value": round((a[0] + a[1]) / 1e6, 3)},
                   {"metric": "monthly_mwh", "time": m, "variant": "scenario",
                    "value": round((a[2] + a[3] * led + (a[4] + a[5] * led) * share + a[6]) / 1e6, 3)}]
    for d, (meas, mod) in data.backtest["full"]["days"].items():
        series += [{"metric": "backtest_kwh", "time": d, "variant": "measured", "value": round(meas, 2)},
                   {"metric": "backtest_kwh", "time": d, "variant": "modelled", "value": round(mod, 2)}]
    charts = [
        {"metric": "night_kw", "title": f"All lamps' power through the night of {data.profile.day:%d %b}",
         "unit": "kW", "x": "time", "kind": "line"},
        {"metric": "monthly_mwh", "title": "Energy by month over a year", "unit": "MWh", "x": "month", "kind": "bar"},
        {"metric": "backtest_kwh", "title": "Backtest: what the meters counted and the model, by night",
         "unit": "kWh", "x": "date", "kind": "line"},
    ]

    sch = [data.schedules[k] for k in ph["lamps"]]
    basis = Counter(s.basis for s in sch)
    streets: dict[str, dict] = {}
    for lid, h in ph["lamps"].items():
        lamp = data.lamps[lid]
        if lamp.street not in streets:
            cams = data.street_cameras.get(lamp.street) or []
            people = data.street_people(lamp.street, params["off_from"], params["off_to"])
            streets[lamp.street] = {"street": lamp.street, "lamps": 0,
                                    "cameras": [c["name"] for c in cams],
                                    "people_off_window": None if people is None else round(people, 1),
                                    "quiet": people is not None and people < params["quiet_max_people"],
                                    "dark": 0}
        st = streets[lamp.street]
        st["lamps"] += 1
        if h["off_h"] > 0:
            st["dark"] += 1
    safety_rows = sorted(({
        "id": lid, "street": data.lamps[lid].street, "label": data.lamps[lid].label,
        "reasons": [SAFETY_STATES[r][0] for r in h["reasons"]], "lamp_hours": round(h["reduced_h"]),
        "people_an_hour": None if h["people"] is None else round(h["people"], 1),
        "bus_stop": data.lamps[lid].stop[0] if "bus_stop" in h["reasons"] else None,
        "building": data.lamps[lid].building if "dangerous_building" in h["reasons"] else None,
    } for lid, h in flagged.items()), key=lambda r: (-len(r["reasons"]), -r["lamp_hours"]))
    summary = {
        "lamps": len(data.lamps), "modelled": len(per_lamp), "without_power": len(data.no_power),
        "schedule": {"on_min_after_sunset": round(statistics.median(s.on_min for s in sch), 1),
                     "off_min_after_sunrise": round(statistics.median(s.off_min for s in sch), 1),
                     "lit_hours_a_night_now": round(data.lit_hours_now, 2),
                     "basis": dict(basis)},
        "retrofit_candidates": len(cands), "flagged_lamps": len(flagged),
        "dimmed_or_dark_without_camera": len(unknown),
        "quiet_streets": sorted(s["street"] for s in streets.values() if s["quiet"]),
        "streets_without_camera": sorted(s["street"] for s in streets.values() if not s["cameras"]),
    }
    watts = Counter(round(s.watts / 5) * 5 for s in sch)
    details = {
        "summary": summary,
        "safety": safety_rows,
        "unmeasured": sorted(unknown),
        "streets": sorted(streets.values(), key=lambda s: -s["lamps"]),
        "retrofit": [{"id": lid, "street": data.lamps[lid].street, "watts": round(data.schedules[lid].watts, 1)}
                     for lid in cands],
        "wattage_classes": [{"watts": w, "lamps": n} for w, n in sorted(watts.items())],
        "city_pattern": data.city,
    }
    notes = _notes(data, params, ph, cands, summary)
    return Result(kpis=kpis, layers=layers, series=series, charts=charts, details=details,
                  sources=list(LIGHTING.sources), notes=notes)


def _notes(data: Data, params: dict, ph: dict, cands: list[str], summary: dict) -> list[str]:
    nights = data.period_nights
    month = data.period.end.astimezone(TZ).strftime("%B")
    notes = [f"Each lamp's schedule and power were learnt from {nights} nights ({data.period.label.lower()}, "
             f"in {month}) and replayed on every night of the year with that date's sunset and sunrise; "
             "switching may differ in summer, which these nights can't show."]
    if data.no_power:
        notes.append(f"{len(data.no_power)} of {len(data.lamps)} lamps reported no power in the period, "
                     "so the figures cover the other "
                     f"{len(data.lamps) - len(data.no_power)}.")
    notes += data.notes
    if params["dim"]:
        notes.append("No lamp has ever dimmed in the data (every level is 0 or 100), so what a dimmed lamp "
                     "draws rests on the assumption 'Power kept when dimmed'.")
    if params["switch_by_sun"]:
        notes.append("Civil twilight still has daylight to see by, so the safety check doesn't count the "
                     "minutes lost at dusk and dawn; it counts the hours dimmed or dark.")
    if params["part_night"]:
        quiet = summary["quiet_streets"]
        notes.append(f"Streets dark in the window: {', '.join(quiet) if quiet else 'none'}, judged by "
                     f"cameras' counts over the last {FOOTFALL_DAYS} days. "
                     + (f"{len(summary['streets_without_camera'])} streets have no camera and "
                        + ("were switched off as asked." if params["include_unmeasured"] else "stay lit.")
                        if summary["streets_without_camera"] else ""))
    if params["retrofit"] and not cands:
        heavy = [s.watts for s in data.schedules.values() if s.watts and s.watts >= 75]
        notes.append(f"No lamp draws {params['retrofit_min_w']} W or more at full light, so the data show "
                     "none to replace."
                     + (f" {len(heavy)} lamps draw {min(heavy):.0f} to {max(heavy):.0f} W, which fits an LED "
                        "luminaire or a 70 W sodium lamp on an electronic ballast; lower the threshold to "
                        "test them, and check on the street before buying." if heavy else ""))
    return notes


def backtest(data: Data) -> dict:
    full, hold = data.backtest["full"], data.backtest["holdout"]
    mid = datetime.fromtimestamp(data.backtest["mid"], TZ)
    med = lm.median_lamp_error(full["lamps"])
    return {
        "metric": "Energy the lamps drew", "unit": "kWh",
        "measured": round(full["measured_kwh"], 1), "modelled": round(full["modelled_kwh"], 1),
        "error_pct": None if full["error_pct"] is None else round(full["error_pct"], 1),
        "period": data.period.label, "lamps": len(full["lamps"]),
        "median_lamp_error_pct": None if med is None else round(med, 1),
        "holdout": {"learnt_until": mid.strftime("%d %b %H:%M"), "measured": round(hold["measured_kwh"], 1),
                    "modelled": round(hold["modelled_kwh"], 1),
                    "error_pct": None if hold["error_pct"] is None else round(hold["error_pct"], 1)},
        "note": ("Today's schedule and power, replayed over the period, against what the lamps' own energy "
                 f"counters measured ({len(full['lamps'])} lamps with a counter). The schedule was learnt from "
                 "the same nights, so the holdout is the fairer test: learnt on the first half, replayed on the "
                 "second."),
    }


class Lighting(Scenario):
    id = "lighting"
    title = "Street lighting policy"
    summary = ("What switching by the sun, dimming, switching off quiet streets or an LED retrofit "
               "would save on the lamps the twin measures, against what they draw today.")
    question = ("How much energy, money and CO2 would a different street lighting policy save, and "
                "where would it leave streets darker?")
    params = PARAMS
    main_kpi = "cost"
    sources = ("smart_nicosia", "twin_memory", "bus_timetable", "registers", "grid_co2", "led_retrofit")
    method = ("Each lamp's power at full light and its standby come from its own power readings; its "
              "switching times, as minutes after sunset and sunrise, from the readings either side of "
              "each switch, else from its energy counter across the silent day, else from the lamps "
              "read every 10 minutes. Today's schedule and the policy are both replayed on every night "
              "of the year before the period ends, with that date's sunset, sunrise and civil twilight. "
              "A dimmed lamp draws a fixed share of full power plus the rest in step with its light. "
              "Night footfall is the hour-of-day average of the city's people-counting cameras over 30 "
              "days. Every KPI's range spans every combination of the assumptions' plausible values.")

    def validated(self, params: dict | None) -> dict:
        out = super().validated(params)
        for a, b, what in (("dim_from", "dim_to", "dimming"), ("off_from", "off_to", "switch-off")):
            if TIMES.index(out[b]) <= TIMES.index(out[a]):
                raise ScenarioError(f"the {what} must end after it starts (night times run 18:00 to 07:30)")
        return out

    def label(self, params: dict) -> str:
        parts = []
        if params["switch_by_sun"]:
            parts.append(f"By the sun ({params['dusk_offset_min']:+d} min at dusk, "
                         f"{params['dawn_offset_min']:+d} at dawn)")
        if params["dim"]:
            parts.append(f"Dim to {params['dim_level']}% {params['dim_from']} to {params['dim_to']}")
        if params["part_night"]:
            parts.append(f"Quiet streets off {params['off_from']} to {params['off_to']}")
        if params["retrofit"]:
            parts.append(f"LED from {params['retrofit_min_w']} W")
        return ", ".join(parts) if parts else "Today's policy"

    def prepare(self, ctx: ScenarioContext, period: Period) -> Data:
        return prepare(ctx, period)

    def evaluate(self, data: Data, params: dict) -> Result:
        return evaluate(data, params)

    def backtest(self, ctx: ScenarioContext, data: Data, params: dict, period: Period) -> dict:
        return backtest(data)


LIGHTING = register(Lighting())

"""What's here: the everyday sections of a spot (chatbot/environment/spot.py), for a click on
the map and for the assistant.

A click on any point of the map opens a spot. Besides what it is like there (sun, shade, air
and wind, the spot's own record), the inspector reads six sections about the point, each built
from what the twin already holds and each able to fail on its own:

- address: the street and number (Nominatim's reverse lookup through `Places.reverse`, which
  throttles it; kept here per 10 m for a day), else the nearest named place from the index;
- buses: the nearest one or two stops with their next departures and live delays;
- parking: free on-street bays within 200 m, read by collector/interpret.py;
- registers: dangerous or needs-repair register buildings within 150 m;
- building: the DLS 3D building under the point (or the nearest), with its height and floors;
- lamps: after dark, how many smart street lights around it are lit.

None of them asks the city's API. Parking bays and lamps are read from the last snapshot the
live strip fetched (or the twin's memory when there is none), stops from the timetable, the
registers and the DLS tiles from their caches (the DLS tile under the point is fetched once
when it isn't cached yet). At a past moment of the city clock, parking and lamps read the twin's
memory and say "unknown" where it holds nothing, never a guess; departures are known only live,
so buses list the stops and say so.

Each section is a dict: {key, title, status, summary, message, rows, facts, sources, as_of}.
`status` is "ok" (something to say), "none" (nothing of the kind here, which is an answer),
"unknown" (the twin can't say for that moment, `message` says why) or "error" (a source failed,
`message` says what to try). `rows` name things with their entity id, so each is a click away.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from collector import sun as collector_sun

from chatbot.city import DeviceState
from chatbot.dls import bbox_of, centroid, contains, distance_to_boundary_m, lonlat_to_tile
from chatbot.geo import haversine_m
from chatbot.settings import TIMEZONE
from chatbot.store import STATE_COLORS

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

SECTIONS = ("address", "buses", "parking", "registers", "building", "lamps")
TITLES = {"address": "Address", "buses": "Buses", "parking": "Parking",
          "registers": "Dangerous buildings", "building": "Building", "lamps": "Street lights"}

BUS_RADIUS_M = 400
BUS_STOPS = 2
BUS_WINDOW_MIN = 60
BUS_DEPARTURES = 3
PARKING_RADIUS_M = 200
PARKING_ROWS = 5
REGISTER_RADIUS_M = 150
REGISTER_ROWS = 5
BUILDING_NEAR_M = 40  # past this, a point is open ground, not "next to" a building
BESIDE_M = 3  # closer than this to a wall is at it (footprints are a metre or two off)
LAMP_RADIUS_M = 150
LAMP_ROWS = 3
PLACE_NEAR_M = 80  # the fallback for an address: a named place this close

# How long a section is kept (seconds), live and at a past moment (the memory doesn't change).
TTL = {"address": 86400, "buses": 20, "parking": 60, "registers": 600, "building": 600,
       "lamps": 60}
TTL_PAST_S = 600
TTL_FAILED_S = 10
SECTION_TIMEOUT_S = 15  # sections() gives up waiting on one after this (the model's call)

SMART_NICOSIA = "https://nokia.smartnicosia.eu/api-docs"
# Each names its entry in chatbot/sources.py by `id` (the reading rules are not a source).
SRC = {
    "geocoding": {"id": "geocoding", "name": "Nominatim (OpenStreetMap)", "detail": "the address at the point",
                  "url": "https://nominatim.org"},
    "places": {"id": "openstreetmap", "name": "OpenStreetMap", "detail": "the places index",
               "url": "https://www.openstreetmap.org/copyright"},
    "timetable": {"id": "bus_timetable", "name": "Bus timetables (GTFS)", "detail": "stops and departures",
                  "url": "https://traffic4cyprus.org.cy/dataset/publictransportstatic"},
    "bus_live": {"id": "bus_live", "name": "Live bus positions (GTFS-RT)", "detail": "the live delays",
                 "url": "https://traffic4cyprus.org.cy/dataset/publictransportrealtime_gtfs_rt"},
    "smart_nicosia": {"id": "smart_nicosia", "name": "Smart Nicosia platform", "detail": "the last snapshot",
                      "url": SMART_NICOSIA},
    "memory": {"id": "twin_memory", "name": "The twin's memory", "detail": "readings as they arrived"},
    "rules": {"name": "The twin's reading rules", "detail": "collector/interpret.py"},
    "registers": {"id": "registers", "name": "Dangerous-building registers (ΕΟΑ)",
                  "url": "https://ndlgo.org.cy/en/hazardousbldslist/"},
    "dls": {"id": "dls", "name": "DLS 3D buildings (INSPIRE)", "url": "https://eservices.dls.moi.gov.cy/inspire/"},
}


class SectionError(Exception):
    """A section can't be built; the message says what happened and what to try, plainly."""


@dataclass
class Spot:
    ents: object  # chatbot.entities.Entities
    lat: float
    lon: float
    at: datetime | None  # a past moment of the city clock, or None for now

    @property
    def when(self) -> datetime:
        return self.at or self.ents.now()


def section(key: str, status: str, summary: str | None = None, *, message: str | None = None,
            rows: list[dict] | None = None, facts: list[dict] | None = None,
            sources: list[dict] | None = None, as_of: str | None = None, **extra) -> dict:
    return {"key": key, "title": TITLES[key], "status": status, "summary": summary,
            "message": message, "rows": rows or [], "facts": [f for f in facts or [] if f],
            "sources": sources or [], "as_of": as_of, **extra}


def fact(label: str, value: object) -> dict | None:
    if value is None or value == "":
        return None
    return {"label": label, "value": value}


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(TZ).isoformat(timespec="minutes") if dt else None


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _moments(ents):
    m = getattr(ents, "moments", None)
    if m is None and getattr(ents, "environment", None) is not None:
        m = getattr(ents.environment, "moments", None)  # the Agent shares one either way
    return m


def _moment_label(at: datetime) -> str:
    from chatbot.moments import label
    return label(at)


# -- address -------------------------------------------------------------------------------------

_reverse_cache: dict[str, tuple[float, dict | None]] = {}
_reverse_lock = threading.Lock()


def reverse(places, lat: float, lon: float) -> dict | None:
    """Nominatim's address at a point, through Places.reverse (throttled to one request a
    second), kept for a day per 10 m so clicking around a street asks once. None when the
    lookup failed (not kept, so it is tried again)."""
    key = f"{lat:.4f},{lon:.4f}"
    with _reverse_lock:
        hit = _reverse_cache.get(key)
        if hit and time.time() - hit[0] < TTL["address"]:
            return hit[1]
    found = places.reverse(lat, lon)
    if found is not None:
        with _reverse_lock:
            _reverse_cache[key] = (time.time(), found)
            if len(_reverse_cache) > 2000:
                _reverse_cache.pop(next(iter(_reverse_cache)))
    return found


def address(s: Spot) -> dict:
    places = s.ents.places
    if places is None:
        return section("address", "unknown", message="Address lookup isn't connected in this app.")
    found = reverse(places, s.lat, s.lon)
    near = s.ents.places_within(s.lat, s.lon, PLACE_NEAR_M, limit=1)
    if found:
        street = found.get("street")
        line = " ".join(x for x in (street, found.get("house_number")) if x) or found.get("name") \
            or (found.get("address") or "").split(",")[0].strip() or None
        area = ", ".join(dict.fromkeys(x for x in (found.get("neighbourhood"), found.get("municipality"),
                                                   found.get("postcode")) if x)) or None
        return section(
            "address", "ok", line, detail=area, street=street,
            facts=[fact("Street", street), fact("Number", found.get("house_number")),
                   fact("Building or place", found.get("name") if found.get("name") != street else None),
                   fact("Area", found.get("neighbourhood")), fact("Municipality", found.get("municipality")),
                   fact("Postcode", found.get("postcode"))],
            rows=near, sources=[SRC["geocoding"]])
    if near:
        p = near[0]
        return section("address", "unknown", f"Near {p['name']}", detail=f"{p['distance_m']} m away",
                       message="The street address couldn't be looked up just now; try again in a minute.",
                       rows=near, sources=[SRC["places"]])
    raise SectionError("The street address couldn't be looked up just now; try again in a minute.")


# -- buses ---------------------------------------------------------------------------------------


def buses(s: Spot) -> dict:
    from chatbot.mobility import planner

    m = getattr(s.ents, "mobility", None)
    if m is None:
        return section("buses", "unknown", message="Buses aren't connected in this app.")
    tt = m.timetable()  # never waits: the first load runs in the background
    if tt is None:
        if getattr(m.source, "error", None):
            raise SectionError("The bus timetable couldn't be downloaded. Try again later.")
        return section("buses", "unknown",
                       message="The bus timetable is still loading. Try again in a minute.")
    near = tt.near(s.lat, s.lon, BUS_RADIUS_M, limit=BUS_STOPS)
    sources = [SRC["timetable"]]
    if not near:
        return section("buses", "none", f"No bus stop within {BUS_RADIUS_M} m", sources=sources)
    rows = []
    for dist, stop in near:
        lines = tt.lines_at(stop)
        rows.append({"id": f"stop-{stop.id}", "kind": "bus_stop", "name": stop.name,
                     "detail": ("Lines " + ", ".join(ln.short for ln in lines[:6])) if lines else None,
                     "distance_m": round(dist), "lon": stop.lon, "lat": stop.lat,
                     "lines": [ln.short for ln in lines]})
    if s.at is not None:
        return section("buses", "ok", f"{_plural(len(rows), 'stop')} within {BUS_RADIUS_M} m", rows=rows,
                       live_only=True, sources=sources,
                       message="Departures are known only live. Set the clock to now to see the next buses.")
    try:
        snap = m.snapshot()
    except Exception as e:  # noqa: BLE001 - the timetable still answers
        log.info("bus feed for a spot failed: %s", e)
        snap = None
    live = snap is not None and snap.status != "unavailable"
    now = m.now()
    soonest = None
    for row, (_, stop) in zip(rows, near):
        deps = planner.departures(tt, [stop], now, BUS_WINDOW_MIN, BUS_DEPARTURES, snap=snap)
        row["departures"] = planner.public(deps)
        if deps and (soonest is None or deps[0]["_at"] < soonest[0]["_at"]):
            soonest = (deps[0], row)
    if live:
        sources.append({**SRC["bus_live"], "as_of": _iso(datetime.fromtimestamp(snap.fetched_at, timezone.utc))})
    note = None if live else "The live bus feed isn't answering, so these are timetable times."
    if soonest is None:
        return section("buses", "ok", f"No buses in the next {BUS_WINDOW_MIN} minutes", rows=rows,
                       message=note, sources=sources, as_of=_iso(now))
    d, row = soonest
    when = "now" if d["in_min"] == 0 else f"in {d['in_min']} min"
    return section("buses", "ok", f"Next bus: {d['line']} to {d['headsign']} {when}, from {row['name']}",
                   rows=rows, message=note, sources=sources, as_of=_iso(now))


# -- device snapshots (parking and lamps) --------------------------------------------------------


@dataclass
class Reading:
    state: str
    stale: bool
    label: str
    street: str | None
    lat: float
    lon: float
    name: str
    detail: dict
    as_of: datetime | None


def device_readings(s: Spot, device_type: str, kind: str) -> tuple[list[Reading], str]:
    """Every device of a type as the twin reads it at the spot's moment, and where that comes
    from ("live" or "memory"). Live: the last snapshot of the type (no request); when there is
    none yet, the twin's memory at the last quarter hour. Past: the twin's memory then.
    Raises SectionError when neither can say."""
    city = s.ents.city
    if s.at is None and city is not None:
        states: dict[str, DeviceState] = city.cached_states(device_type)
        if states:
            return [Reading(st.interp.state, st.interp.stale, st.label, st.street, st.device.lat,
                            st.device.lon, st.device.name, st.interp.detail, st.interp.as_of)
                    for st in states.values() if st.device.lat is not None], "live"
    moments = _moments(s.ents)
    if moments is None or moments.memory is None:
        if s.at is not None:
            raise SectionError("The twin's memory is off here, so this moment can't be read.")
        raise SectionError("The live readings haven't arrived yet. They come with the live strip "
                           "within a few minutes.")
    snap = moments.snapshot(kind, s.when)
    if snap.error:
        raise SectionError("The twin's memory couldn't be read just now; try again in a moment.")
    if not snap.known:
        if s.at is not None:
            raise _Unknown(f"The twin's memory holds nothing for {_moment_label(snap.at)}, so this "
                           "is unknown then.")
        raise SectionError("The live readings haven't arrived yet. They come with the live strip "
                           "within a few minutes.")
    out = []
    for d, st, vals in zip(snap.devices, snap.states, snap.values):
        out.append(Reading(st, st == "unknown", d.label, d.street, d.device.lat, d.device.lon,
                           d.device.name, vals, snap.at))
    return out, "memory"


class _Unknown(Exception):
    """The moment is outside what the twin's memory holds: an answer ("unknown"), not a failure."""


def _around(readings: list[Reading], lat: float, lon: float, radius_m: float) -> list[tuple[float, Reading]]:
    dlat = radius_m / 110_574
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    out = []
    for r in readings:
        if abs(r.lat - lat) > dlat or abs(r.lon - lon) > dlon:
            continue
        d = haversine_m(lat, lon, r.lat, r.lon)
        if d <= radius_m:
            out.append((d, r))
    out.sort(key=lambda t: t[0])
    return out


def _device_sources(origin: str, at: datetime | None) -> list[dict]:
    if origin == "live":
        return [SRC["smart_nicosia"], SRC["rules"]]
    detail = f"readings up to {_moment_label(at)}" if at else "the last quarter hour it holds"
    return [{**SRC["memory"], "detail": detail}, SRC["rules"]]


def parking(s: Spot) -> dict:
    try:
        readings, origin = device_readings(s, "Parking", "parking_sensor")
    except _Unknown as e:
        return section("parking", "unknown", message=str(e))
    around = _around(readings, s.lat, s.lon, PARKING_RADIUS_M)
    sources = _device_sources(origin, s.at)
    if not around:
        return section("parking", "none", f"No parking sensors within {PARKING_RADIUS_M} m",
                       message="Only some streets have bay sensors. Ask the assistant for car parks nearby.",
                       sources=sources)
    # Bays that stopped reporting are left out, as the live strip and the replays do.
    counts = Counter("no_reading" if r.stale or r.state not in ("free", "occupied") else r.state
                     for _, r in around)
    free = [(d, r) for d, r in around if r.state == "free" and not r.stale]
    known = counts["free"] + counts["occupied"]
    rows = [{"id": r.name, "kind": "parking_sensor", "name": f"Parking bay {r.name}",
             "detail": ", ".join(x for x in (r.street, "for disabled drivers" if r.detail.get("is_pwd") else None) if x)
             or None,
             "distance_m": round(d), "lon": r.lon, "lat": r.lat, "state": "free",
             "color": STATE_COLORS["free"]} for d, r in free[:PARKING_ROWS]]
    newest = max((r.as_of for _, r in around if r.as_of), default=None)
    facts = [fact("Free", counts["free"]), fact("Taken", counts["occupied"]),
             fact("No recent reading", counts["no_reading"] or None)]
    if not known:
        return section("parking", "unknown", f"{_plural(len(around), 'bay')} within {PARKING_RADIUS_M} m",
                       message="None of them has reported lately, so it isn't known which are free.",
                       facts=facts, sources=sources, as_of=_iso(newest))
    if known == 1:
        summary = f"The one bay within {PARKING_RADIUS_M} m is {'free' if counts['free'] else 'taken'}"
    elif counts["free"]:
        summary = f"{counts['free']} of {known} bays free within {PARKING_RADIUS_M} m"
    else:
        summary = f"All {known} bays within {PARKING_RADIUS_M} m are taken"
    return section("parking", "ok", summary, rows=rows, facts=facts, sources=sources,
                   as_of=_iso(newest), free=counts["free"], known=known)


def lamps(s: Spot) -> dict:
    when = s.when
    if not collector_sun.is_night(when, s.lat, s.lon):
        from chatbot.environment.sun import sun_times

        t = sun_times(when.astimezone(TZ).date(), s.lat, s.lon)
        return section("lamps", "none", "Daylight: street lights are off",
                       message=f"They come on after sunset, at {t['sunset']}." if t.get("sunset") else None,
                       night=False)
    try:
        readings, origin = device_readings(s, "Lighting", "street_light")
    except _Unknown as e:
        return section("lamps", "unknown", message=str(e), night=True)
    around = _around(readings, s.lat, s.lon, LAMP_RADIUS_M)
    sources = _device_sources(origin, s.at)
    if not around:
        return section("lamps", "none", f"No smart street lights within {LAMP_RADIUS_M} m",
                       message="Only some streets have lamps that report to the city.",
                       sources=sources, night=True)
    counts = Counter(r.state for _, r in around)
    lit = counts["on"]
    known = lit + counts["off"] + counts["offline"]  # "unknown": no report yet tonight
    if not known:
        return section("lamps", "unknown", f"{_plural(len(around), 'lamp')} within {LAMP_RADIUS_M} m",
                       message="None of them has reported tonight yet, so it isn't known which are lit.",
                       sources=sources, night=True)
    dark = [(d, r) for d, r in around if r.state in ("off", "offline")]
    rows = [{"id": r.name, "kind": "street_light", "name": r.label or r.name,
             "detail": "Offline at night" if r.state == "offline" else "Dark",
             "distance_m": round(d), "lon": r.lon, "lat": r.lat, "state": r.state,
             "color": STATE_COLORS.get(r.state, STATE_COLORS["unknown"])} for d, r in dark[:LAMP_ROWS]]
    if not rows:  # all lit: the nearest one, to open
        d, r = next((d, r) for d, r in around if r.state == "on")
        rows = [{"id": r.name, "kind": "street_light", "name": r.label or r.name, "detail": "Lit",
                 "distance_m": round(d), "lon": r.lon, "lat": r.lat, "state": "on",
                 "color": STATE_COLORS["on"]}]
    newest = max((r.as_of for _, r in around if r.as_of), default=None)
    facts = [fact("Lit", lit), fact("Dark", counts["off"] or None), fact("Offline", counts["offline"] or None),
             fact("No reading tonight", counts["unknown"] or None)]
    summary = (f"The one lamp within {LAMP_RADIUS_M} m is {'lit' if lit else 'not lit'}" if known == 1
               else f"{lit} of {known} lamps lit within {LAMP_RADIUS_M} m")
    return section("lamps", "ok", summary,
                   rows=rows, facts=facts, sources=sources, as_of=_iso(newest), night=True,
                   lit=lit, total=len(around))


# -- registers and buildings ---------------------------------------------------------------------


def registers(s: Spot) -> dict:
    reg = s.ents.registers
    if reg is None:
        return section("registers", "unknown", message="The registers aren't connected in this app.")
    if not reg.ready:
        return section("registers", "unknown",
                       message="The registers are still loading. Try again in a minute.")
    found = s.ents.nearby_registers(s.lat, s.lon, max_m=REGISTER_RADIUS_M, limit=100)
    sources = [SRC["registers"]]
    past = "The registers as published now; they keep no history." if s.at is not None else None
    if not found:
        return section("registers", "none", f"None on the registers within {REGISTER_RADIUS_M} m",
                       message=past, sources=sources)
    c = Counter(r["state"] for r in found)
    parts = [x for x in (f"{c['dangerous']} dangerous" if c["dangerous"] else None,
                         f"{c['needs_repair']} needing repair" if c["needs_repair"] else None) if x]
    rows = [{**r, "detail": {"dangerous": "Dangerous", "needs_repair": "Needs repair"}.get(r["state"])}
            for r in found[:REGISTER_ROWS]]
    return section("registers", "ok", f"{' and '.join(parts)} within {REGISTER_RADIUS_M} m",
                   message=past, rows=rows, sources=sources,
                   facts=[fact("Dangerous", c["dangerous"] or None), fact("Needs repair", c["needs_repair"] or None)],
                   note="A listing is a case on a public register, not an engineering verdict.")


def building_at(dls, lat: float, lon: float) -> tuple[dict | None, float]:
    """The DLS 3D building under a point, else the nearest within BUILDING_NEAR_M, with the
    metres to it (0 under it). The tile under the point is fetched when it isn't cached (once:
    DLS tiles are kept on disk for a month); the tiles around it are read only from the cache."""
    x, y = lonlat_to_tile(lon, lat)
    feats = list(dls.tile(x, y))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                feats += dls.cached_tile(x + dx, y + dy) or []
    dlat = BUILDING_NEAR_M / 110_574
    dlon = BUILDING_NEAR_M / (111_320 * math.cos(math.radians(lat)))
    best: tuple[float, dict | None] = (math.inf, None)
    for b in feats:
        bb = bbox_of(b["geometry"])
        if not (bb[0] - dlon <= lon <= bb[2] + dlon and bb[1] - dlat <= lat <= bb[3] + dlat):
            continue
        if contains(b["geometry"], lon, lat):
            return b, 0.0
        d = distance_to_boundary_m(b["geometry"], lon, lat)
        if d < best[0]:
            best = (d, b)
    return (best[1], best[0]) if best[0] <= BUILDING_NEAR_M else (None, math.inf)


def building(s: Spot) -> dict:
    dls = s.ents.dls
    if dls is None:
        return section("building", "unknown", message="The DLS 3D buildings aren't connected in this app.")
    try:
        b, dist = building_at(dls, s.lat, s.lon)
    except Exception as e:  # noqa: BLE001 - WebError from DLS, or a broken tile
        log.info("building at %s,%s failed: %s", s.lat, s.lon, e)
        raise SectionError("The DLS 3D buildings didn't answer. Try again in a moment.") from None
    sources = [SRC["dls"]]
    if b is None:
        return section("building", "none", "Open ground: no building here",
                       message=f"The DLS 3D city has no building within {BUILDING_NEAR_M} m.",
                       sources=sources)
    p = b["properties"]
    use = p.get("t") or "Building"
    floors = p.get("f")
    size = ", ".join(x for x in (f"{p['h']:g} m to the roof" if p.get("h") is not None else None,
                                 _plural(int(floors), "floor") if floors else None) if x)
    c = centroid(b["geometry"])
    row = {"id": f"B{b['id']}", "kind": "building_3d", "name": use, "detail": "Open it in 3D",
           "distance_m": round(dist), **({"lon": c[0], "lat": c[1]} if c else {})}
    rows = [row]
    for r in s.ents.registers_on_building(b["id"]):
        rows.append({"id": r["id"], "kind": "register_building", "name": r.get("name") or r.get("address") or r["id"],
                     "detail": "On the register: " + {"dangerous": "dangerous", "needs_repair": "needs repair"}[r["status"]],
                     "state": r["status"], "color": STATE_COLORS[r["status"]]})
    where = "Under the pin" if dist == 0 else "Right by the pin" if dist < BESIDE_M else f"{round(dist)} m away"
    what = "a building" if use == "Building" else f"a {use.lower()}" if use[0].lower() not in "aeiou" else f"an {use.lower()}"
    return section("building", "ok", f"{where}: {what}" + (f", {size}" if size else ""), rows=rows, sources=sources,
                   facts=[fact("Height to the roof", f"{p['h']:g} m" if p.get("h") is not None else None),
                          fact("Floors", int(floors) if floors else None), fact("Use", p.get("t")),
                          fact("Roof", p.get("r")),
                          fact("Footprint", f"{round(p['a'])} m²" if p.get("a") else None)],
                   under=dist == 0, distance_m=round(dist))


# -- running them --------------------------------------------------------------------------------

BUILDERS: dict[str, Callable[[Spot], dict]] = {
    "address": address, "buses": buses, "parking": parking, "registers": registers,
    "building": building, "lamps": lamps,
}

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_pool = ThreadPoolExecutor(len(SECTIONS), thread_name_prefix="here")


def _key(key: str, s: Spot) -> tuple:
    frame = int(s.at.timestamp()) // 900 if s.at is not None else None
    return key, round(s.lat, 5), round(s.lon, 5), frame


def build(ents, key: str, lat: float, lon: float, at: datetime | None = None) -> dict:
    """One section at a point and a moment, cached briefly. Never raises: a failure is a
    section with status "error" and a plain message."""
    if key not in BUILDERS:
        raise KeyError(key)
    s = Spot(ents, lat, lon, at)
    k = _key(key, s)
    with _cache_lock:
        hit = _cache.get(k)
        if hit and time.time() < hit[0]:
            return hit[1]
    try:
        out = BUILDERS[key](s)
        # A live "unknown" (a timetable still loading) is asked again soon; a past moment the
        # memory doesn't hold stays unknown.
        ttl = TTL_PAST_S if at is not None else TTL_FAILED_S if out["status"] == "unknown" else TTL[key]
    except SectionError as e:
        out = section(key, "error", message=str(e))
        ttl = TTL_FAILED_S
    except Exception as e:  # noqa: BLE001 - one section failing leaves the others
        log.warning("what's here: %s at %s,%s failed: %s", key, lat, lon, e, exc_info=True)
        out = section(key, "error", message=f"This couldn't be read just now ({_short(e)}). Try again in a moment.")
        ttl = TTL_FAILED_S
    with _cache_lock:
        _cache[k] = (time.time() + ttl, out)
        if len(_cache) > 600:
            for old in [x for x, (exp, _) in _cache.items() if exp < time.time()][:300] or [next(iter(_cache))]:
                _cache.pop(old, None)
    return out


def _short(e: Exception) -> str:
    text = str(e).strip().split("\n")[0]
    return (text[:120] + "...") if len(text) > 120 else (text or type(e).__name__)


def sections(ents, lat: float, lon: float, at: datetime | None = None,
             keys: tuple[str, ...] | list[str] = SECTIONS, timeout_s: float = SECTION_TIMEOUT_S) -> list[dict]:
    """Every section at once, in parallel, in order. One that takes longer than timeout_s is
    an error that says so (it carries on and is kept for the next ask)."""
    futures = {k: _pool.submit(build, ents, k, lat, lon, at) for k in keys}
    deadline = time.monotonic() + timeout_s
    out = []
    for k, f in futures.items():
        try:
            out.append(f.result(timeout=max(0.0, deadline - time.monotonic())))
        except FutureTimeout:
            out.append(section(k, "error", message="This took too long to read. Try again in a moment."))
    return out


# -- for the assistant ---------------------------------------------------------------------------

RELATION_LABELS = {"buses": "Bus stop", "parking": "Free parking bay", "registers": "Register building nearby",
                   "building": "Building here", "lamps": "Street light", "address": "Nearby place"}


def for_model(secs: list[dict]) -> dict:
    """The sections as compact JSON for the model: what each says, with its rows by id."""
    out = {}
    for s in secs:
        item: dict = {"status": s["status"]}
        for k in ("summary", "detail", "message", "note"):
            if s.get(k):
                item[k] = s[k]
        if s.get("live_only"):
            item["departures"] = "known only live"
        if s["facts"]:
            item["facts"] = {f["label"]: f["value"] for f in s["facts"]}
        rows = []
        for r in s["rows"]:
            row = {k: r[k] for k in ("id", "name", "detail", "distance_m", "state") if r.get(k) is not None}
            if r.get("departures") is not None:
                row["next_departures"] = [
                    {k: d[k] for k in ("line", "headsign", "scheduled", "expected", "in_min") if d.get(k) is not None}
                    | ({"late_min": round(d["delay_s"] / 60)} if d.get("delay_s") else {})
                    for d in r["departures"]]
            rows.append(row)
        if rows:
            item["rows"] = rows
        if s.get("sources"):
            item["sources"] = [x["name"] for x in s["sources"]]
        out[s["key"]] = item
    return out


def relations(secs: list[dict]) -> list[dict]:
    """The sections' rows that have a place, as relations (for the assistant's map layer)."""
    out = []
    seen = set()
    for s in secs:
        for r in s["rows"]:
            if not r.get("id") or r.get("lon") is None or r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append({"relation": f"here:{s['key']}", "label": RELATION_LABELS[s["key"]], "id": r["id"],
                        "kind": r.get("kind"), "name": r.get("name"), "detail": r.get("detail"),
                        "distance_m": r.get("distance_m"), "lon": r["lon"], "lat": r["lat"],
                        **({"state": r["state"], "color": r.get("color")} if r.get("state") else {})})
    return out


def source_ids(secs: list[dict]) -> list[str]:
    """The ids in chatbot/sources.py of what the sections read, once each, in order."""
    return list(dict.fromkeys(x["id"] for s in secs for x in s["sources"] if x.get("id")))

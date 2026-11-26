"""The twin's entities: everything on the map, of any kind, in one shape.

    {id, kind, kind_label, name, subtitle, geometry, center, footprints, state, readings,
     attributes, relations, sources, as_of}

Kinds: street_light, parking_sensor, waste_bin, air_station, info_screen and camera (the Smart
Nicosia devices, read live through CityData and interpreted by collector/interpret.py, so a lamp
here is "on" by the same rule as everywhere else), register_building (an EOA dangerous-building
register entry, NIC-409), building_3d (a DLS 3D building, B255563), place (an OpenStreetMap
place from the index, n219329628) and web_place (a place an answer found on the web and put on the
map, web-1a2b3c4d; chatbot/webplaces.py).

- `state` is the interpreted verdict: {value, label, color, tone, stale, reason, as_of, age_s}.
- `readings` are the numbers worth reading out, each with its unit and, for live kinds, its age.
- `attributes` are labelled facts; `relations` point to other things (its street, the nearest
  devices of other kinds, the register entry on a building, the buildings on a plot), with an id
  where the other thing is an entity too.
- `series(id)` is the last 24 hours of the headline reading, where the city's API keeps history.

Ids say their kind: NIC-/LIM-/LAR-/FAM- are register entries, B and digits a DLS building,
n/w/r and digits an OSM place, web- and 8 hex digits a web place, anything else a device (or a
traffic camera) by its name. A later
feature adds a kind with `register_kind`, its search items with `register_search` and its 24-hour
series with `register_series` (chatbot/mobility/entities.py adds buses, stops, lines and roads).

Nothing here asks a public service for more than the thing asked about: relations and search
read what is already cached (the device inventory, the attribute cache, the last snapshot of
each device type, the places index, the registers, DLS tiles on disk).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from collector import parse
from collector.interpret import Interpretation

from chatbot import context, eoa, history
from chatbot.city import KINDS, TYPE_TO_KIND, CityData, Device, DeviceState
from chatbot.dls import DLS, centroid, contains, lonlat_to_tile
from chatbot.geo import Places, haversine_m, loose, norm
from chatbot.settings import NICOSIA_BBOX, TIMEZONE
from chatbot.store import STATE_COLORS

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

KIND_LABELS = {
    "street_light": "Street light", "parking_sensor": "Parking bay", "waste_bin": "Waste bin",
    "air_station": "Air station", "info_screen": "Info screen", "camera": "Camera",
    "register_building": "Dangerous building", "building_3d": "Building", "place": "Place",
    "web_place": "From the web",
}
DEVICE_KINDS = tuple(KINDS)

STATE_LABELS = {
    "on": "On", "off": "Off", "offline": "Offline", "unknown": "Unknown",
    "free": "Free", "occupied": "Occupied",
    "low": "Low", "medium": "Medium", "high": "High", "full": "Full",
    "very_low": "Very good", "connected": "Connected", "disconnected": "Disconnected",
    "ok": "Working", "no_heartbeat": "No heartbeat", "not_reported": "Not reporting",
    "dangerous": "Dangerous", "needs_repair": "Needs repair",
    "started": "Counting", "stopped": "Not counting",
}
# Air bands read as air quality, not as "low pollution".
AIR_LABELS = {"very_low": "Very good", "low": "Good", "medium": "Moderate", "high": "Poor",
              "very_high": "Very poor"}
# How loud a state is on the page: alarm (signal red), warn, live (sodium), ok or muted.
TONES = {
    "on": "live", "off": "muted", "offline": "alarm", "unknown": "muted",
    "free": "ok", "occupied": "muted",
    "low": "ok", "medium": "muted", "high": "warn", "full": "alarm",
    "connected": "ok", "disconnected": "alarm",
    "ok": "ok", "no_heartbeat": "warn", "not_reported": "alarm",
    "dangerous": "alarm", "needs_repair": "warn",
    "started": "ok", "stopped": "warn",
}
AIR_TONES = {"very_low": "ok", "low": "ok", "medium": "muted", "high": "warn", "very_high": "alarm"}

# The numbers worth reading out, per device type: (detail key, label, unit).
READINGS: dict[str, list[tuple[str, str, str]]] = {
    "Lighting": [("level", "Dimming level", "%"), ("power_w", "Power", "W"),
                 ("voltage_v", "Voltage", "V"), ("power_factor_pct", "Power factor", "%")],
    "Parking": [("vbatt", "Battery", "V"), ("temperature_c", "Sensor temperature", "°C")],
    "Wastebin": [("level_pct", "Fill", "%"), ("temperature_c", "Temperature", "°C"),
                 ("vbatt", "Battery", "V")],
    "smartsense": [("caqi", "CAQI", ""), ("pm2_5", "PM2.5", "µg/m³"), ("pm10", "PM10", "µg/m³"),
                   ("no2", "NO₂", "µg/m³"), ("o3", "O₃", "µg/m³"), ("noise_db", "Noise", "dB"),
                   ("temperature_c", "Temperature", "°C"), ("humidity_pct", "Humidity", "%")],
    "InfoPoints": [],
    "Camera": [],
}
# The 24-hour series per device type: (metric, label, unit, how to draw it).
SERIES = {
    "Lighting": ("power_w", "Power drawn", "W", "line"),
    "Parking": ("occupied", "Occupied", "", "states"),
    "Wastebin": ("level_pct", "Fill", "%", "line"),
    "smartsense": ("caqi", "CAQI", "", "line"),
}
SERIES_STEP_MIN = 15
NEAR_DEVICE_M = 600  # how far to look for the nearest device of each other kind
NEAR_REGISTER_M = 250
LIVE_TTL_S = 60  # a live entity is read again after a minute
STATIC_TTL_S = 600
SERIES_TTL_S = 600
CACHE_SIZE = 256
SMART_NICOSIA = "https://nokia.smartnicosia.eu/api-docs"
OSM_TYPES = {"n": "node", "w": "way", "r": "relation"}
DLS_3D_URL = "https://eservices.dls.moi.gov.cy/inspire/"
DLS_CADASTRE_URL = "https://eservices.dls.moi.gov.cy/"

REGISTER_ID = re.compile(r"^(NIC|LIM|LAR|FAM)-", re.IGNORECASE)
BUILDING_ID = re.compile(r"^B(\d{1,10})$", re.IGNORECASE)
PLACE_ID = re.compile(r"^[nwr]\d{1,12}$")


class EntityNotFound(KeyError):
    """No entity has that id. The message says what ids look like."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "not found"


# -- small helpers -------------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(TZ).isoformat(timespec="minutes") if dt else None


def _from_ms(ms: object) -> datetime | None:
    try:
        v = float(ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(v / 1000, tz=timezone.utc) if v > 0 else None


def _local_text(iso_or_dt: object) -> str | None:
    """'2026-11-26 21:40' from an ISO string or a datetime."""
    if not iso_or_dt:
        return None
    dt = iso_or_dt if isinstance(iso_or_dt, datetime) else datetime.fromisoformat(str(iso_or_dt))
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def _round(v: float) -> float | int:
    return int(v) if float(v).is_integer() else round(v, 1)


def attr(label: str, value: object) -> dict | None:
    if value is None or value == "" or (isinstance(value, float) and not math.isfinite(value)):
        return None
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    return {"label": label, "value": value}


def street_of(address: str | None) -> str | None:
    """The street in an address ('ΣΤΑΣΙΝΟΥ 44' -> 'ΣΤΑΣΙΝΟΥ', 'Mnasiadou 2' -> 'Mnasiadou')."""
    if not address:
        return None
    s = re.sub(r"[\s,]*\d+[A-Za-zΑ-Ωα-ω]?(\s*[-&/,]\s*\d+[A-Za-zΑ-Ωα-ω]?)*\s*$", "", address).strip(" ,")
    return s or None


def streets_in(address: str | None) -> list[str]:
    """The streets an address names: a corner ('ΛΗΔΡΑΣ & ΟΝΑΣΑΓΟΡΟΥ ΓΩΝΙΑ') names two."""
    parts = re.split(r"\s*&\s*|\s+(?:και|ΚΑΙ)\s+", street_of(address) or "")
    out = [street_of(re.sub(r"\b(ΓΩΝΙΑ|γωνία|corner)\b", "", x, flags=re.IGNORECASE).strip()) for x in parts]
    return [x for x in out if x]


def where_of(r: dict) -> str:
    """A register entry's community and municipality, once each."""
    return ", ".join(dict.fromkeys(x for x in (r.get("community"), r.get("municipality")) if x)) or "?"


def state_block(kind: str, value: str | None, *, stale: bool = False, reason: str | None = None,
                as_of: datetime | None = None, now: datetime | None = None,
                local: str | None = None) -> dict | None:
    if not value:
        return None
    air = kind == "air_station"
    age = (now - as_of).total_seconds() if as_of and now else None
    return {"value": value,
            "label": (AIR_LABELS if air else STATE_LABELS).get(value, value.replace("_", " ").capitalize()),
            "color": STATE_COLORS.get(value, STATE_COLORS["unknown"]),
            "tone": (AIR_TONES if air else TONES).get(value, "muted"),
            "stale": stale, "reason": reason, "local": local,
            "as_of": _iso(as_of), "age_s": round(age) if age is not None else None}


def point_geom(lon: float | None, lat: float | None) -> dict | None:
    if lon is None or lat is None:
        return None
    return {"type": "Point", "coordinates": [lon, lat]}


def new_entity(eid: str, kind: str, name: str, **kw) -> dict:
    e = {"id": eid, "kind": kind, "kind_label": KIND_LABELS.get(kind, kind.replace("_", " ")),
         "name": name, "subtitle": None, "geometry": None, "center": None, "footprints": [],
         "state": None, "readings": [], "attributes": [], "relations": [], "sources": [],
         "as_of": None, "has_series": False}
    e.update(kw)
    e["attributes"] = [a for a in e["attributes"] if a]
    return e


# -- kinds ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    """How to recognise a kind's ids and build its entity. `build` returns None for "not
    mine", so the next kind can try."""
    name: str
    matches: Callable[[str], bool]
    build: Callable[[Entities, str, tuple[float, float] | None], dict | None]
    ttl_s: float = STATIC_TTL_S


KIND_RESOLVERS: list[Kind] = []
# More things to search: each takes the Entities and returns index_item()s (quickly: it runs on
# every keystroke, so keep its own cache). Groups listed in SEARCH_CAPS are capped there.
SEARCH_SOURCES: list[Callable[[Entities], list[dict]]] = []
SEARCH_CAPS = {"Devices": 8, "Dangerous buildings": 6, "Places": 6, "Streets": 4, "Buildings": 1}
# The 24-hour series of a kind the devices' history doesn't cover: kind -> fn(ents, entity, end),
# `end` the moment it ends at (None: now).
SERIES_RESOLVERS: dict[str, Callable[[Entities, dict, datetime | None], dict | None]] = {}


def register_kind(kind: Kind, first: bool = False) -> Kind:
    """Add a kind. Kinds are tried in order; the device kinds come last, since a device's id is
    any name the city gave it."""
    KIND_RESOLVERS.insert(0 if first else max(0, len(KIND_RESOLVERS) - 1), kind)
    return kind


def register_search(source: Callable[[Entities], list[dict]], caps: dict[str, int] | None = None):
    SEARCH_SOURCES.append(source)
    SEARCH_CAPS.update(caps or {})
    return source


def register_series(kind: str, fn: Callable[[Entities, dict, datetime | None], dict | None]):
    SERIES_RESOLVERS[kind] = fn
    return fn


# A kind's own reading of a past moment of the city clock (the devices read theirs through
# chatbot/moments.py): kind -> fn(ents, entity, at) -> the entity as it was, with its `moment`.
AT_RESOLVERS: dict[str, Callable[[Entities, dict, datetime], dict]] = {}


def register_at(kind: str, fn: Callable[[Entities, dict, datetime], dict]):
    AT_RESOLVERS[kind] = fn
    return fn


class Entities:
    """Builds entities from the twin's sources, with short caches, and searches them."""

    def __init__(self, city: CityData | None, places: Places | None = None,
                 dls: DLS | None = None, registers: eoa.Registry | None = None,
                 memory=None, mobility=None, web_places=None):
        self.city = city
        self.web_places = web_places  # places found on the web (chatbot/webplaces.py)
        self.memory = memory  # the twin's memory: the 24-hour series reads it first
        self.mobility = mobility  # buses, stops and road speeds (chatbot/mobility)
        self.places = places
        self.dls = dls
        self.registers = registers
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._series: OrderedDict[str, tuple[float, dict | None]] = OrderedDict()
        self._index: tuple[float, tuple, list[dict]] | None = None

    # -- lookups -------------------------------------------------------------------------------

    def get(self, eid: str, near: tuple[float, float] | None = None) -> dict:
        """The entity with this id. `near` (lon, lat) is a hint for where it is (where it was
        clicked), which saves a DLS lookup for a 3D building. Raises EntityNotFound."""
        eid = (eid or "").strip()
        if not eid or len(eid) > 120:
            raise EntityNotFound("give an id, e.g. YL1015, NIC-409, B255563 or n219329628")
        hit = self._cached(self._cache, eid.casefold())
        if hit is not None:
            return hit
        for kind in KIND_RESOLVERS:
            if not kind.matches(eid):
                continue
            entity = kind.build(self, eid, near)
            if entity is not None:
                for key in {entity["id"].casefold(), eid.casefold()}:
                    self._remember(self._cache, key, entity, kind.ttl_s)
                return entity
        raise EntityNotFound(
            f"nothing in the twin has the id {eid!r}. Ids look like YL1015 (street light), "
            "YP974 (parking bay), YW1394 (bin), NIC-409 (register building), B255563 (DLS 3D "
            "building) or n219329628 (place); find_devices, find_dangerous_buildings, "
            "get_buildings_3d and find_places return them")

    def series(self, eid: str, near: tuple[float, float] | None = None,
               end: datetime | None = None) -> dict | None:
        """The 24 hours up to `end` (default now) of the entity's headline reading, or None
        where the API keeps no history for its kind."""
        entity = self.get(eid, near)
        key = entity["id"].casefold() + (f"@{int(end.timestamp()) // 900}" if end else "")
        with self._lock:
            hit = self._series.get(key)
            if hit and time.time() - hit[0] < SERIES_TTL_S:
                return hit[1]
        out = None
        if entity["kind"] in ("street_light", "parking_sensor", "waste_bin", "air_station"):
            out = self._device_series(entity, end)
        elif entity["kind"] == "camera" and entity.get("_feeds"):
            out = self._traffic_series(entity, end)
        elif entity["kind"] in SERIES_RESOLVERS:
            out = SERIES_RESOLVERS[entity["kind"]](self, entity, end)
        with self._lock:
            self._series[key] = (time.time(), out)
            while len(self._series) > CACHE_SIZE:
                self._series.popitem(last=False)
        return out

    def _cached(self, cache: OrderedDict, key: str):
        with self._lock:
            hit = cache.get(key)
            if hit and time.time() < hit[0]:
                cache.move_to_end(key)
                return hit[1]
        return None

    def _remember(self, cache: OrderedDict, key: str, value, ttl: float) -> None:
        with self._lock:
            cache[key] = (time.time() + ttl, value)
            cache.move_to_end(key)
            while len(cache) > CACHE_SIZE:
                cache.popitem(last=False)

    def now(self) -> datetime:
        return self.city.now() if self.city is not None else datetime.now(timezone.utc)

    # -- relations (from cached data only) -----------------------------------------------------

    def nearest_devices(self, lat: float, lon: float, *, skip: str | None = None,
                        skip_kind: str | None = None, max_m: float = NEAR_DEVICE_M) -> list[dict]:
        """The nearest device of each kind (other than `skip_kind`) within `max_m`, from the
        inventory, with its state where the last snapshot of its kind has it."""
        if self.city is None:
            return []
        best: dict[str, tuple[float, Device]] = {}
        for d in self.city.cached_devices():
            kind = TYPE_TO_KIND.get(d.type)
            if kind is None or kind == skip_kind or d.name == skip or d.lat is None:
                continue
            dist = haversine_m(lat, lon, d.lat, d.lon)
            if dist <= max_m and (kind not in best or dist < best[kind][0]):
                best[kind] = (dist, d)
        out = []
        for kind in DEVICE_KINDS:
            if kind not in best:
                continue
            dist, d = best[kind]
            label, _ = self.city.cached_label(d)
            st = self.city.cached_states(d.type).get(d.name)
            out.append({"relation": "nearest", "label": f"Nearest {KIND_LABELS[kind].lower()}",
                        "id": d.name, "kind": kind, "name": device_name(kind, d, label),
                        "distance_m": round(dist), "lon": d.lon, "lat": d.lat,
                        **({"state": st.interp.state,
                            "color": STATE_COLORS.get(st.interp.state, STATE_COLORS["unknown"])}
                           if st else {})})
        return out

    def nearby_registers(self, lat: float, lon: float, *, skip: str | None = None,
                         max_m: float = NEAR_REGISTER_M, limit: int = 3) -> list[dict]:
        if self.registers is None or not self.registers.ready:
            return []
        found = []
        for r in self.registers.records():
            spot = eoa.anchor(r)
            if not spot or r["id"] == skip:
                continue
            if abs(spot[1] - lat) > 0.01 or abs(spot[0] - lon) > 0.012:
                continue
            d = haversine_m(lat, lon, spot[1], spot[0])
            if d <= max_m:
                found.append((d, r, spot))
        found.sort(key=lambda t: t[0])
        return [{"relation": "register", "label": "Register building nearby", "id": r["id"],
                 "kind": "register_building", "name": register_name(r),
                 "distance_m": round(d), "lon": spot[0], "lat": spot[1], "state": r["status"],
                 "color": STATE_COLORS[r["status"]]} for d, r, spot in found[:limit]]

    def places_within(self, lat: float, lon: float, max_m: float, limit: int = 4,
                      polygon: dict | None = None) -> list[dict]:
        """Named places inside `polygon` (a footprint), or else within `max_m`, nearest first."""
        if self.places is None or not self.places.ready:
            return []
        found = []
        for p in self.places.all():
            if not p.get("name") or abs(p["lat"] - lat) > 0.004 or abs(p["lon"] - lon) > 0.005:
                continue
            inside = polygon is not None and contains(polygon, p["lon"], p["lat"])
            d = haversine_m(lat, lon, p["lat"], p["lon"])
            if inside or (polygon is None and d <= max_m):
                found.append((d, p))
        found.sort(key=lambda t: t[0])
        return [{"relation": "place", "label": "In this building" if polygon else "Nearby place",
                 "id": p["id"], "kind": "place", "name": p["name"],
                 "detail": p["category"].replace("_", " "), "distance_m": round(d),
                 "lon": p["lon"], "lat": p["lat"]} for d, p in found[:limit]]

    def building_at(self, lat: float, lon: float) -> dict | None:
        """The DLS 3D building standing at a point, from tiles already cached (no request)."""
        if self.dls is None:
            return None
        x, y = lonlat_to_tile(lon, lat)
        for tx, ty in [(x + dx, y + dy) for dx in (0, -1, 1) for dy in (0, -1, 1)]:
            for b in self.dls.cached_tile(tx, ty) or []:
                if contains(b["geometry"], lon, lat):
                    return b
        return None

    def registers_on_building(self, bid: int) -> list[dict]:
        if self.registers is None or not self.registers.ready:
            return []
        return [r for r in self.registers.records()
                if any(fp.get("bid") == bid for fp in r.get("footprints") or [])]

    # -- search --------------------------------------------------------------------------------

    def search(self, q: str, limit: int = 24) -> list[dict]:
        """Devices, register buildings, places and streets matching `q`, best first. Reads the
        caches only, so it is quick enough to run on every keystroke."""
        qn = norm(q)
        if not qn:
            return []
        words = qn.split()
        ql = loose(q)
        out: list[tuple[float, dict]] = []
        if (m := BUILDING_ID.match(q.strip())):
            out.append((100, {"id": f"B{m.group(1)}", "kind": "building_3d",
                              "name": f"DLS 3D building {m.group(1)}", "group": "Buildings",
                              "detail": "Open it in the inspector"}))
        extra = []
        for source in SEARCH_SOURCES:
            try:
                extra += source(self)
            except Exception as e:  # noqa: BLE001 - one source failing leaves the others
                log.warning("search source failed: %s", e)
        for item in self._search_index() + extra:
            s = score(item, qn, words, ql)
            if not s and item["kind"] == "register_building" and any(c.isdigit() for c in q):
                r = item["_record"]
                s = 90 if eoa.file_matches(q, r) else 70 if eoa.plot_matches(q, r) else 0
            if s:
                out.append((s, item))
        out.sort(key=lambda t: (-t[0], t[1]["name"]))
        caps = SEARCH_CAPS
        taken: Counter = Counter()
        results = []
        for s, item in out:
            g = item["group"]
            if taken[g] >= caps.get(g, 6):
                continue
            taken[g] += 1
            results.append({k: v for k, v in item.items() if not k.startswith("_")})
            if len(results) >= limit:
                break
        return results

    def _search_index(self) -> list[dict]:
        city = self.city
        devices = city.cached_devices() if city is not None else []
        records = self.registers.records() if self.registers is not None else []
        places = self.places.all() if self.places is not None else []
        cams = city.cached_traffic_cameras() if city is not None else []
        attrs_at = tuple(sorted((getattr(city, "_attrs_at", {}) or {}).values()))
        key = tuple((id(x) if x else 0, len(x)) for x in (devices, records, places, cams)) + attrs_at
        with self._lock:
            if self._index and self._index[1] == key and time.time() - self._index[0] < 300:
                return self._index[2]
        items: list[dict] = []
        streets: dict[str, dict] = {}

        def add_street(name: str | None, lat: float | None, lon: float | None, what: str) -> None:
            if not name or lat is None or not (NICOSIA_BBOX[1] <= lat <= NICOSIA_BBOX[3]
                                               and NICOSIA_BBOX[0] <= lon <= NICOSIA_BBOX[2]):
                return
            clean = re.sub(r"\s+(Avenue|Ave|Street|St|Nicosia|Λευκωσία)\.?$", "", name.strip(),
                           flags=re.IGNORECASE).strip()
            k = norm(clean)
            if len(k) < 3:
                return
            s = streets.setdefault(k, {"names": Counter(), "pts": [], "what": Counter()})
            s["names"][clean] += 1
            s["pts"].append((lon, lat))
            s["what"][what] += 1

        for d in devices:
            kind = TYPE_TO_KIND.get(d.type)
            if kind is None:
                continue
            label, street = city.cached_label(d)
            name = device_name(kind, d, label)
            items.append(index_item(d.name, kind, name, "Devices", street or KIND_LABELS[kind],
                                    d.lat, d.lon, [d.name, name, street, KIND_LABELS[kind]]))
            add_street(street, d.lat, d.lon, KIND_LABELS[kind].lower())
        known = {d.name for d in devices}
        for c in cams:
            if c.get("name") and c["name"] not in known and c.get("device_name") not in known:
                items.append(index_item(c["name"], "camera", f"Camera {c['name']}", "Devices",
                                        "Traffic counting camera", c.get("lat"), c.get("lon"),
                                        [c["name"], c.get("stream_name"), "camera traffic"]))
        for r in records:
            spot = eoa.anchor(r)
            items.append(index_item(
                r["id"], "register_building", register_name(r), "Dangerous buildings",
                f"{STATE_LABELS[r['status']]} · {where_of(r)}",
                spot[1] if spot else None, spot[0] if spot else None,
                [r["id"], r.get("address"), r.get("name"), r.get("community"),
                 r.get("municipality"), r.get("parish"), r.get("file_number")],
                state=r["status"], record=r))
            for part in streets_in(r.get("address")):
                add_street(part, spot[1] if spot else None, spot[0] if spot else None,
                           "register building")
        for p in places:
            if not p.get("name"):
                continue
            items.append(index_item(p["id"], "place", p["name"], "Places",
                                    " · ".join(x for x in (p["category"].replace("_", " ").capitalize(),
                                                          p.get("street")) if x),
                                    p["lat"], p["lon"],
                                    [p["name"], p.get("name_local"), p["category"].replace("_", " ")]))
            add_street(p.get("street"), p["lat"], p["lon"], "place")
        for s in streets.values():
            name = s["names"].most_common(1)[0][0]
            lons, lats = [p[0] for p in s["pts"]], [p[1] for p in s["pts"]]
            what = ", ".join(f"{n} {w}{'s' if n > 1 else ''}" for w, n in s["what"].most_common(3))
            item = index_item(None, "street", name, "Streets", what,
                              sum(lats) / len(lats), sum(lons) / len(lons), list(s["names"]))
            item["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
            items.append(item)
        with self._lock:
            self._index = (time.time(), key, items)
        return items

    # -- series --------------------------------------------------------------------------------

    def _device_series(self, entity: dict, end: datetime | None = None) -> dict | None:
        device: Device = entity["_device"]
        metric, label, unit, draw = SERIES[device.type]
        now = end or self.now()
        times = history.frame_times(now, 24, SERIES_STEP_MIN)
        # From the twin's memory, the city's API filling only what it lacks.
        try:
            rd = history.readings(
                self.city, self.memory, device.type, [device.name],
                history.HISTORY_KEYS[device.type], int(times[0].timestamp() * 1000),
                int(now.timestamp() * 1000),
                lookback_ms=int(history.LOOKBACK[device.type].total_seconds() * 1000))
        except history.NoHistory:
            return None
        if device.name not in rd.records and not rd.covered.get(device.name):
            return None
        blank = DeviceState(device, "", None, Interpretation(device.type, "unknown", True, None,
                                                             None, ""))
        states, values = history.replay(device.type, [blank], rd.records, times, rd.covered)
        points = []
        for k, t in enumerate(times):
            st = states[k][0]
            v = (values.get(metric) or [[None]] * len(times))[k][0]
            if device.type == "Lighting":  # a dark lamp draws nothing, whatever it last reported
                v = (v or 0) if st == "on" else (0 if st in ("off", "offline") else None)
            points.append({"t": t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"), "v": v, "s": st})
        vals = [p["v"] for p in points if isinstance(p["v"], (int, float))]
        if not vals and draw != "states":
            return None
        out = {"metric": metric, "label": label, "unit": unit, "draw": draw,
               "step_min": SERIES_STEP_MIN, "points": points,
               "from": points[0]["t"], "to": points[-1]["t"],
               "state_colors": {s: STATE_COLORS.get(s, STATE_COLORS["unknown"])
                                for s in dict.fromkeys(p["s"] for p in points)},
               "summary": summary(vals), "source": rd.source}
        if device.type == "Wastebin":
            out["threshold"] = {"value": 80, "label": "Full from 80%"}
        elif device.type == "smartsense":
            out["threshold"] = {"value": 75, "label": "Poor from 75"}
        elif device.type == "Parking":
            known = [p for p in points if p["s"] in ("free", "occupied")]
            if known:
                share = 100 * sum(p["s"] == "occupied" for p in known) / len(known)
                out["summary"] = {"occupied_pct": round(share)}
        return out

    def _traffic_series(self, entity: dict, until: datetime | None = None) -> dict | None:
        """People (or, at a camera that counts none, everything) per hour over the 24 hours up
        to `until` (default now): the day before and that day, one small request each (an
        hourly report can't cross midnight)."""
        feeds = entity["_feeds"]
        va_ids = json.dumps([f["analytics_id"] for f in feeds], separators=(",", ":"))
        now = until or self.now()
        by_hour: dict[str, Counter] = {}
        for period in ("yesterday", "today"):
            start, end, days = context._period_bounds(period, now)
            payload = self.city.client.get_json(
                "getTrafficReports", group_by="hour", start_date=int(start.timestamp() * 1000),
                end_date=int(end.timestamp() * 1000), va_ids=va_ids)
            for r in parse.parse_traffic_buckets(payload, TZ, day=days[0]):
                c = by_hour.setdefault(r.bucket_start.strftime("%Y-%m-%dT%H:%M"), Counter())
                c[context.OBJECT_TYPES.get(r.object_type, str(r.object_type))] += r.total or 0
                c["total"] += r.total or 0
        last = now.astimezone(TZ).replace(minute=0, second=0, microsecond=0)
        hours = [(last - timedelta(hours=23 - k)).strftime("%Y-%m-%dT%H:%M") for k in range(24)]
        window = {h: by_hour[h] for h in hours if h in by_hour}
        if not window:
            return None
        field = "people" if any(c.get("people") for c in window.values()) else "total"
        # A camera that counts reads 0 in a quiet hour.
        points = [{"t": h, "v": window.get(h, Counter()).get(field, 0)} for h in hours]
        vals = [p["v"] for p in points]
        totals: Counter = Counter()
        for c in window.values():
            totals.update(c)
        return {"metric": field, "label": "People per hour" if field == "people" else
                "Counted per hour", "unit": "", "draw": "bars", "step_min": 60, "points": points,
                "from": points[0]["t"], "to": points[-1]["t"], "summary": summary(vals),
                "totals_24h": dict(totals.most_common())}


def summary(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {"min": _round(min(vals)), "max": _round(max(vals)), "last": _round(vals[-1]),
            "mean": _round(sum(vals) / len(vals))}


def index_item(eid, kind, name, group, detail, lat, lon, texts, state=None, record=None) -> dict:
    text = " ".join(norm(t) for t in texts if t)
    item = {"id": eid, "kind": kind, "name": name, "group": group, "detail": detail,
            "lat": lat, "lon": lon, "_text": text, "_name": norm(name),
            "_loose": " ".join(loose(t) for t in texts if t) + " "
                      + " ".join(loose(t, eta="e") for t in texts if t),
            "_id": (eid or "").lower()}
    if state:
        item["state"] = state
        item["color"] = STATE_COLORS.get(state)
    if record is not None:
        item["_record"] = record
    return item


def score(item: dict, qn: str, words: list[str], ql: str) -> float:
    if item["_id"] and item["_id"] == qn or qn in item.get("_aliases", ()):
        return 100
    if item["_id"] and len(qn) >= 2 and item["_id"].startswith(qn):
        return 85
    name = item["_name"]
    if name.startswith(qn):
        return 70
    text = item["_text"]
    if re.search(r"(^|[\s(])" + re.escape(qn), text):
        return 60
    if qn in text:
        return 45
    if len(words) > 1 and all(w in text for w in words):
        return 40
    if len(ql) >= 3 and ql in item["_loose"]:
        return 35
    return 0


# -- devices -------------------------------------------------------------------------------------


def device_name(kind: str, d: Device, label: str) -> str:
    if kind == "parking_sensor":
        return f"Parking bay {d.name}"
    if kind == "info_screen":
        return f"Info screen {d.name.split('__')[0]}"
    return label


def _meta_extra(attrs: dict) -> dict:
    try:
        return (json.loads(attrs.get("metadata") or "{}") or {}).get("extra") or {}
    except (ValueError, AttributeError):
        return {}


def _find_device(city: CityData, name: str) -> Device | None:
    devices = city.devices()
    return (next((d for d in devices if d.name == name), None)
            or next((d for d in devices if d.name.lower() == name.lower()), None))


def build_device(ents: Entities, eid: str, near) -> dict | None:
    city = ents.city
    if city is None:
        return None
    device = _find_device(city, eid)
    feeds: list[dict] = []
    if device is None or device.type == "Camera":
        try:
            cams = city.traffic_cameras()
        except Exception as e:  # noqa: BLE001 - the camera still opens, without its counts
            log.info("no traffic cameras for %s: %s", eid, e)
            cams = []
        if device is None:
            feed = next((c for c in cams if c.get("name") == eid), None)
            if feed is None:
                return None
            if feed.get("device_name"):
                device = _find_device(city, feed["device_name"])
            if device is None:
                return feed_entity(ents, feed)
        feeds = [c for c in cams if c.get("device_name") == device.name]
    kind = TYPE_TO_KIND.get(device.type)
    if kind is None:
        return None
    state, attrs = city.device_state(device.name)
    i = state.interp
    d = i.detail
    now = ents.now()
    readings = []
    for key, label, unit in READINGS[device.type]:
        v = _num(d.get(key))
        if v is not None:
            readings.append({"key": key, "label": label, "value": _round(v), "unit": unit,
                             "as_of": _iso(i.as_of), "stale": i.stale})
    extra = _meta_extra(attrs)
    activity = _from_ms(attrs.get("lastActivityTime"))
    attributes = [attr("Street", state.street)]
    if device.type == "Lighting":
        attributes += [attr("Lamp", attrs.get("name"))]
    elif device.type == "Parking":
        attributes += [attr("Reserved for disabled drivers", d.get("is_pwd")),
                       attr("Status since", _local_text(d.get("status_since")))]
    elif device.type == "Wastebin":
        attributes += [attr("Waste", extra.get("trashtype")),
                       attr("Container volume", f"{extra['volume']} l" if extra.get("volume") else None),
                       attr("Last pickup", _local_text(d.get("last_pickup"))),
                       attr("Fill before pickup", f"{_round(d['level_before_pickup'])}%"
                            if _num(d.get("level_before_pickup")) is not None else None),
                       attr("Postcode", attrs.get("postalcode"))]
    elif device.type == "smartsense":
        attributes += [attr("City", attrs.get("city")),
                       attr("Dominant pollutant", d.get("dominant_pollutant")),
                       attr("Index", attrs.get("caqi-type"))]
    elif device.type == "InfoPoints":
        attributes += [attr("Player", d.get("state_raw")), attr("Plan", d.get("plan_state")),
                       attr("Last reported", attrs.get("lastReportedTimestamp"))]
    elif device.type == "Camera":
        attributes += [attr("Location", attrs.get("location")), attr("Make", attrs.get("make")),
                       attr("Model", attrs.get("model")),
                       attr("Counts traffic", ", ".join(f["name"] for f in feeds) or None)]
    attributes += [attr("Last sign of life", _local_text(activity)), attr("Device id", device.name)]
    relations = []
    if state.street:
        relations.append({"relation": "street", "label": "Street", "name": state.street})
    if device.lat is not None:
        relations += ents.nearest_devices(device.lat, device.lon, skip=device.name, skip_kind=kind)
        relations += ents.nearby_registers(device.lat, device.lon, limit=2)
        relations += ents.places_within(device.lat, device.lon, 120, limit=2)
    entity = new_entity(
        device.name, kind, device_name(kind, device, state.label),
        subtitle=state.street or KIND_LABELS[kind],
        geometry=point_geom(device.lon, device.lat),
        center=[device.lon, device.lat] if device.lat is not None else None,
        state=state_block(kind, i.state, stale=i.stale, reason=i.reason, as_of=i.as_of, now=now),
        readings=readings, attributes=attributes, relations=relations,
        sources=[{"source": "smart_nicosia", "name": "Smart Nicosia platform", "detail": "latest telemetry and attributes",
                  "url": SMART_NICOSIA, "as_of": _iso(i.as_of)},
                 {"name": "The twin's reading rules", "detail": i.reason}],
        as_of=_iso(i.as_of), has_series=device.type in SERIES or bool(feeds))
    entity["_device"] = device
    entity["_feeds"] = feeds
    return entity


def feed_entity(ents: Entities, feed: dict) -> dict:
    """A traffic counting camera that is not a device in the inventory (placed by hand)."""
    lat, lon = feed.get("lat"), feed.get("lon")
    relations = ents.nearest_devices(lat, lon) if lat is not None else []
    e = new_entity(
        feed["name"], "camera", f"Camera {feed['name']}", subtitle="Traffic counting camera",
        geometry=point_geom(lon, lat), center=[lon, lat] if lat is not None else None,
        state=state_block("camera", (feed.get("status") or "unknown").lower()),
        attributes=[attr("Stream", feed.get("stream_name")), attr("Analytics id", feed.get("analytics_id")),
                    attr("Position", {"device": "from the camera device", "manual": "placed by hand"}
                         .get(feed.get("location_source") or "", None))],
        relations=relations,
        sources=[{"source": "smart_nicosia", "name": "Smart Nicosia video analytics", "url": SMART_NICOSIA}],
        has_series=True)
    e["_feeds"] = [feed]
    return e


# -- register buildings --------------------------------------------------------------------------


def register_name(r: dict) -> str:
    return r.get("name") or r.get("address") or where_of(r)


def build_register(ents: Entities, eid: str, near) -> dict | None:
    reg = ents.registers
    if reg is None:
        return None
    r = next((x for x in reg.records() if x["id"].upper() == eid.upper()), None)
    if r is None:
        return None
    spot = eoa.anchor(r)
    src = reg.sources.get(r["eoa"], {})
    plot = f"{r.get('sheet')}/{r.get('plan')}" + (f", block {r['block']}" if r.get("block") else "") \
        + f", parcel {r.get('parcel')}"
    readings = []
    if r.get("height_m") is not None:
        readings.append({"key": "height_m", "label": "Height" + (" (about)" if r.get("height_estimated") else ""),
                         "value": r["height_m"], "unit": "m"})
    for key, label, unit in (("floors", "Floors", ""), ("footprint_m2", "Footprint", "m²"),
                             ("volume_m3", "Volume", "m³")):
        if r.get(key) is not None:
            readings.append({"key": key, "label": label, "value": r[key], "unit": unit})
    zone = {"urban": "Urban area", "rural": "Rural community"}.get(r.get("setting") or "")
    attributes = [
        attr("Register", eoa.EOAS[r["eoa"]]["name"]),
        attr("Status", f"{eoa.STATUS_GR[r['status']]} (article {'15B' if r['status'] == 'dangerous' else '15A'})"),
        attr("File number", r.get("file_number")), attr("Address", r.get("address")),
        attr("Building name", r.get("name")), attr("Municipality", r.get("municipality")),
        attr("Community", r.get("community")),
        attr("Parish", r.get("parish") if r.get("parish") not in (None, "None") else None),
        attr("Zone", zone), attr("Plot", plot), attr("Declared", r.get("declared")),
        attr("Danger to", r.get("danger_to")), attr("Listed building", r.get("listed")),
        attr("Description", r.get("description")), attr("Instructions", r.get("instructions")),
        attr("Building type", r.get("building_type")),
        attr("Placed by", {"published": "the register's own marker",
                           "parcel": "the centre of its plot"}.get(r.get("position") or "")),
        attr("3D match", eoa.MATCH_NOTES.get(r.get("match_3d") or "")),
        attr("Plot note", r.get("parcel_note")),
    ]
    relations = []
    street = street_of(r.get("address"))
    if street:
        relations.append({"relation": "street", "label": "Street", "name": street})
    relations.append({"relation": "parcel", "label": "Land parcel", "name": f"Plot {plot}",
                      "detail": "DLS cadastre" if r.get("parcel_geometry") else "not found in the cadastre"})
    for fp in r.get("footprints") or []:
        relations.append({"relation": "building", "label": "DLS 3D building on the plot"
                          if r.get("match_3d") != "block" else "DLS 3D block around the plot",
                          "id": f"B{fp['bid']}", "kind": "building_3d",
                          "name": fp.get("type") or "Building",
                          "detail": f"{fp['height_m']} m" + (f", {fp['floors']} floors" if fp.get("floors") else "")})
    if spot:
        relations += ents.nearby_registers(spot[1], spot[0], skip=r["id"], max_m=150)
        relations += ents.nearest_devices(spot[1], spot[0], max_m=400)
    footprints = [{"geometry": fp["geometry"], "height_m": fp["height_m"]}
                  for fp in r.get("footprints") or []]
    sources = [{"source": "registers", "name": eoa.EOAS[r["eoa"]]["name"], "detail": {"live": "live copy", "cached": "saved copy",
                                                             "seed": "copy shipped with the app"}
                .get(src.get("copy") or "", None), "url": eoa.EOAS[r["eoa"]]["page"],
                "as_of": src.get("as_of")}]
    if r.get("parcel_geometry"):
        sources.append({"name": "DLS cadastre", "detail": "the plot outline", "url": DLS_CADASTRE_URL})
    if footprints:
        sources.append({"name": "DLS 3D buildings", "detail": "heights and floors", "url": DLS_3D_URL})
    return new_entity(
        r["id"], "register_building", register_name(r), subtitle=where_of(r),
        geometry=r.get("parcel_geometry") or point_geom(r.get("lon"), r.get("lat")),
        center=list(spot) if spot else None, footprints=footprints,
        state=state_block("register_building", r["status"], local=eoa.STATUS_GR[r["status"]],
                          reason="on the public register; not an engineering verdict"),
        readings=readings, attributes=attributes, relations=relations, sources=sources,
        as_of=src.get("as_of"))


# -- DLS 3D buildings ----------------------------------------------------------------------------


def build_building(ents: Entities, eid: str, near) -> dict | None:
    m = BUILDING_ID.match(eid)
    if not m or ents.dls is None:
        return None
    oid = int(m.group(1))
    b = ents.dls.building_by_id(oid, near)
    if b is None:
        return None
    p = b["properties"]
    c = centroid(b["geometry"])
    on = ents.registers_on_building(oid)
    rec = next((r for r in on if r.get("match_3d") != "block"), None)
    readings = [{"key": "height_m", "label": "Height to the roof", "value": p["h"], "unit": "m"},
                {"key": "eave_m", "label": "Eave", "value": p["e"], "unit": "m"}]
    for key, label, unit in (("f", "Floors", ""), ("a", "Footprint", "m²"), ("v", "Volume", "m³"),
                             ("g", "Ground above sea level", "m")):
        if p.get(key) is not None:
            readings.append({"key": {"f": "floors", "a": "footprint_m2", "v": "volume_m3",
                                     "g": "ground_m"}[key], "label": label,
                             "value": _round(p[key]), "unit": unit})
    relations = [{"relation": "register", "label": "On the register" if r.get("match_3d") != "block"
                  else "Register plot inside this block", "id": r["id"],
                  "kind": "register_building", "name": register_name(r), "state": r["status"],
                  "color": STATE_COLORS[r["status"]]} for r in on]
    if c:
        relations += ents.places_within(c[1], c[0], 0, polygon=b["geometry"])
        relations += ents.nearest_devices(c[1], c[0], max_m=300)
    return new_entity(
        f"B{oid}", "building_3d", p.get("t") or "Building", subtitle=f"DLS 3D building {oid}",
        geometry=b["geometry"], center=list(c) if c else None,
        footprints=[{"geometry": b["geometry"], "height_m": p["h"]}],
        state=state_block("register_building", rec["status"], local=eoa.STATUS_GR[rec["status"]],
                          reason=f"on the register as {rec['id']}") if rec else None,
        readings=readings,
        attributes=[attr("Use", p.get("t")), attr("Roof", p.get("r")), attr("DLS id", oid)],
        relations=relations,
        sources=[{"source": "dls", "name": "DLS 3D buildings (INSPIRE BU3)", "detail": "Department of Lands and Surveys",
                  "url": DLS_3D_URL}])


# -- places --------------------------------------------------------------------------------------

PLACE_ATTRS = (("opening_hours", "Opening hours"), ("street", "Street"), ("cuisine", "Cuisine"),
               ("wheelchair", "Wheelchair"), ("fee", "Fee"), ("phone", "Phone"),
               ("website", "Website"), ("description", "About"), ("wikipedia", "Wikipedia"))


def build_place(ents: Entities, eid: str, near) -> dict | None:
    if ents.places is None:
        return None
    p = ents.places.by_id(eid)
    if p is None:
        return None
    category = p["category"].replace("_", " ").capitalize()
    attributes = [attr("Category", category)]
    if p.get("name_local") and p["name_local"] != p.get("name"):
        attributes.append(attr("Local name", p["name_local"]))
    attributes += [attr(label, p.get(key)) for key, label in PLACE_ATTRS]
    relations = []
    if p.get("street"):
        relations.append({"relation": "street", "label": "Street", "name": p["street"]})
    b = ents.building_at(p["lat"], p["lon"])
    if b:
        relations.append({"relation": "building", "label": "In the DLS building", "id": f"B{b['id']}",
                          "kind": "building_3d", "name": b["properties"].get("t") or "Building",
                          "detail": f"{b['properties']['h']} m"})
    relations += ents.nearby_registers(p["lat"], p["lon"], max_m=150, limit=2)
    relations += ents.nearest_devices(p["lat"], p["lon"], max_m=400)
    fetched = ents.places.fetched_at
    return new_entity(
        p["id"], "place", p.get("name") or category,
        subtitle=" · ".join(x for x in (category, p.get("street")) if x),
        geometry=point_geom(p["lon"], p["lat"]), center=[p["lon"], p["lat"]],
        attributes=attributes, relations=relations,
        sources=[{"source": "openstreetmap", "name": "OpenStreetMap", "detail": "places index, refreshed weekly",
                  "url": f"https://www.openstreetmap.org/{OSM_TYPES.get(p['id'][0], 'node')}/{p['id'][1:]}",
                  "as_of": datetime.fromtimestamp(fetched, tz=timezone.utc).astimezone(TZ)
                  .strftime("%Y-%m-%d") if fetched else None}])


# -- places found on the web (chatbot/webplaces.py) ----------------------------------------------

WEB_ID = re.compile(r"^web-[0-9a-f]{8}$", re.IGNORECASE)


def nearest_stop(ents: Entities, lat: float, lon: float, max_m: float = 500) -> list[dict]:
    """The nearest bus stop a bus leaves from, if the timetable is loaded (never waits for it)."""
    tt = ents.mobility.timetable() if ents.mobility is not None else None
    if tt is None:
        return []
    return [{"relation": "stop", "label": "Nearest bus stop", "id": f"stop-{s.id}",
             "kind": "bus_stop", "name": s.name, "distance_m": round(d), "lon": s.lon, "lat": s.lat}
            for d, s in tt.near(lat, lon, max_m, limit=1)]


def build_web_place(ents: Entities, eid: str, near) -> dict | None:
    w = ents.web_places.get(eid) if ents.web_places is not None else None
    if w is None:
        return None
    exact = w["precision"] == "exact"
    found = datetime.fromtimestamp(w.get("found_at") or 0, tz=timezone.utc).astimezone(TZ)
    attributes = [attr("Offers", w.get("note")), attr("Address", w.get("address")),
                  attr("Phone", w.get("phone")), attr("Website", w.get("website")),
                  attr("On the map", f"At the door, found by {w['located_by']}" if exact else
                       "On its street" + (f", {w['matched']}," if w.get("matched") else "")
                       + " not at the door: the map knows the street but not the number")]
    relations = []
    if w.get("osm_id") and ents.places is not None and ents.places.by_id(w["osm_id"]):
        relations.append({"relation": "place", "label": "On OpenStreetMap", "id": w["osm_id"],
                          "kind": "place", "name": w.get("matched") or w["name"]})
    if w.get("overture_id"):
        relations.append({"relation": "place", "label": "In Overture Maps", "id": w["overture_id"],
                          "kind": "overture_place", "name": w.get("matched") or w["name"]})
    b = ents.building_at(w["lat"], w["lon"]) if exact else None
    if b:
        relations.append({"relation": "building", "label": "In the DLS building", "id": f"B{b['id']}",
                          "kind": "building_3d", "name": b["properties"].get("t") or "Building",
                          "detail": f"{b['properties']['h']} m"})
    relations += nearest_stop(ents, w["lat"], w["lon"])
    relations += ents.nearest_devices(w["lat"], w["lon"], max_m=400)
    sources = [{"source": "web_search", "name": re.sub(r"^www\.", "", urlsplit(u).netloc),
                "detail": "web search result", "url": u, "as_of": f"{found:%Y-%m-%d}"}
               for u in w.get("sources") or []]
    osm, ov = bool(w.get("osm_id")), bool(w.get("overture_id"))
    sources.append({"source": "openstreetmap" if osm else "overture" if ov else "geocoding",
                    "name": "OpenStreetMap" if osm else "Overture Maps" if ov else "Nominatim and Photon",
                    "detail": "where it stands on the map", "url": None, "as_of": f"{found:%Y-%m-%d}"})
    return new_entity(
        w["id"], "web_place", w["name"],
        subtitle=" · ".join(x for x in (", ".join(w.get("topics") or []) or "Found on the web",
                                        w.get("address")) if x),
        geometry=point_geom(w["lon"], w["lat"]), center=[w["lon"], w["lat"]],
        attributes=attributes, relations=relations, sources=sources)


def search_web_places(ents: Entities) -> list[dict]:
    if ents.web_places is None:
        return []
    return [index_item(w["id"], "web_place", w["name"], "From the web",
                       " · ".join(x for x in (", ".join(w.get("topics") or []), w.get("address")) if x),
                       w["lat"], w["lon"], [w["name"], w.get("address"), w.get("note"),
                                            *(w.get("topics") or [])])
            for w in ents.web_places.all()]


KIND_RESOLVERS.extend([
    Kind("register_building", lambda i: bool(REGISTER_ID.match(i)), build_register),
    Kind("building_3d", lambda i: bool(BUILDING_ID.match(i)), build_building),
    Kind("place", lambda i: bool(PLACE_ID.match(i)), build_place),
    Kind("web_place", lambda i: bool(WEB_ID.match(i)), build_web_place, ttl_s=60),
    Kind("device", lambda i: True, build_device, ttl_s=LIVE_TTL_S),
])
register_search(search_web_places, {"From the web": 4})


HISTORY_KINDS = ("street_light", "parking_sensor", "waste_bin", "air_station")


def at_moment(entity: dict, interp: Interpretation | None, known: bool, at: datetime,
              moment_label: str) -> dict:
    """The entity as it was at a past moment (the city clock): its state and readings from the
    twin's memory then (unknown where the memory holds nothing, never the live value), the rest
    of its record as it is. Things with no history (buildings, places) only say so."""
    e = dict(entity)
    e["moment"] = {"at": _iso(at), "label": moment_label, "known": known,
                   "applies": interp is not None}
    if interp is None:
        return e
    # The neighbours' state dots are live; at another moment they would mislead.
    e["relations"] = [{k: v for k, v in r.items() if k not in ("state", "color")}
                      for r in entity["relations"]]
    kind = entity["kind"]
    e["state"] = state_block(kind, interp.state if known else "unknown", stale=interp.stale,
                             reason=interp.reason, as_of=interp.as_of, now=at)
    d = interp.detail
    device: Device = entity["_device"]
    e["readings"] = []
    if known:
        for key, lab, unit in READINGS[device.type]:
            v = _num(d.get(key))
            if v is not None:
                e["readings"].append({"key": key, "label": lab, "value": _round(v), "unit": unit,
                                      "as_of": _iso(interp.as_of), "stale": interp.stale})
    e["as_of"] = _iso(interp.as_of)
    # Facts read from the live readings are told as they were then, or left out.
    then = {"Status since": _local_text(d.get("status_since")) if known else None,
            "Last pickup": _local_text(d.get("last_pickup")) if known else None,
            "Fill before pickup": f"{_round(d['level_before_pickup'])}%"
            if known and _num(d.get("level_before_pickup")) is not None else None,
            "Dominant pollutant": d.get("dominant_pollutant") if known else None}
    live_only = {"Last sign of life", "Player", "Plan", "Last reported"}
    e["attributes"] = [a for a in (
        attr(x["label"], then[x["label"]]) if x["label"] in then else x
        for x in entity["attributes"] if x["label"] not in live_only) if a]
    e["sources"] = [{"source": "twin_memory", "name": "The twin's memory",
                     "detail": f"readings up to {moment_label}" if known else
                     "no record of this moment", "as_of": _iso(interp.as_of)},
                    {"name": "The twin's reading rules", "detail": interp.reason},
                    *[s for s in entity["sources"]
                      if s["name"] not in ("Smart Nicosia platform", "The twin's reading rules")]]
    return e


def public(entity: dict) -> dict:
    """An entity without its private fields, for JSON."""
    return {k: v for k, v in entity.items() if not k.startswith("_")}


def for_model(entity: dict) -> dict:
    """An entity as compact JSON for the model: labels to values, relations by id."""
    st = entity.get("state")
    out = {"id": entity["id"], "kind": entity["kind"], "name": entity["name"],
           "where": entity.get("subtitle")}
    if entity.get("center"):
        out["lon"], out["lat"] = (round(v, 6) for v in entity["center"])
    if st:
        out["state"] = st["value"]
        out["state_label"] = st["label"]
        for k in ("stale", "reason", "local"):
            if st.get(k):
                out[f"state_{k}"] = st[k]
        if st.get("as_of"):
            out["as_of"] = _local_text(st["as_of"])
        if st.get("age_s") is not None:
            out["age_min"] = round(st["age_s"] / 60)
    if entity["readings"]:
        out["readings"] = {r["label"]: f"{r['value']} {r['unit']}".strip() for r in entity["readings"]}
    if entity["attributes"]:
        out["attributes"] = {a["label"]: a["value"] for a in entity["attributes"]}
    rel = []
    for r in entity["relations"]:
        item = {"relation": r["label"], "name": r.get("name")}
        for k in ("id", "kind", "distance_m", "state", "detail"):
            if r.get(k) is not None:
                item[k] = r[k]
        rel.append(item)
    if rel:
        out["relations"] = rel
    out["sources"] = [" · ".join(str(x) for x in (s["name"], s.get("detail"), s.get("as_of")) if x)
                      for s in entity["sources"]]
    out.update(entity.get("_for_model") or {})  # what a kind adds for the model (departures)
    return out


def series_rows(entity: dict, s: dict) -> list[dict]:
    """A 24-hour series as chart rows: time (local, temporal), the metric, the state."""
    rows = []
    for p in s["points"]:
        row = {"id": entity["id"], "time": p["t"], s["metric"]: p["v"]}
        if p.get("s"):
            row["state"] = p["s"]
        rows.append(row)
    return rows


def layer_features(entity: dict) -> list[dict]:
    """The entity (its footprints too, for 'buildings_3d') and the related things that have a
    place, as map features sharing ids with the page's inspector."""
    st = entity.get("state") or {}
    color = st.get("color") or "#ffae45"
    base = {"id": entity["id"], "label": entity["name"], "state": st.get("value") or entity["kind"],
            "color": color, "entity_kind": entity["kind"]}
    # `entity_kind`, not `kind`: the map treats a layer with kind street_light as the city's
    # lamps, which would switch the night lamps off for one thing's layer.
    numbers = {r["key"]: r["value"] for r in entity["readings"] if isinstance(r["value"], (int, float))}
    feats = []
    if entity.get("center"):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": entity["center"]},
                      "properties": {**base, **numbers,
                                     "popup": f"{entity['name']}\n{entity['kind_label']}"}})
    for fp in entity.get("footprints") or []:
        feats.append({"type": "Feature", "geometry": fp["geometry"],
                      "properties": {**base, "_kind": "footprint", "height_m": fp["height_m"]}})
    for r in entity["relations"]:
        if r.get("id") and r.get("lon") is not None:
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                          "properties": {"id": r["id"], "label": r["name"], "state": r.get("state") or r.get("kind"),
                                         "color": r.get("color") or "#8798b0",
                                         "entity_kind": r.get("kind")}})
    return feats

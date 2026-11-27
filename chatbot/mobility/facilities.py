"""Where people park, charge and hire: car parks, park and ride, electric vehicle chargers,
Nextbike stations, Bolt scooter zones and bicycle racks.

All six are inventories on the Cyprus National Access Point (traffic4cyprus.org.cy, a CKAN
catalogue, CC BY 4.0), served as DATEX II XML from fixcyprus.cy and regenerated every night:

- `parking_areas` (Public Works Department): 651 car parks, each a point with a name in English
  and Greek. 552 of them are named only "OSM Origin": they came from OpenStreetMap with no name.
  No capacity, no price, no live occupancy;
- `parkandride` (Public Works Department): 9 park and ride sites, each one or more polygons;
- `electric_vehicle_chargers` (Department of Electrical and Mechanical Services): 171 charging
  points with their power, connectors, operator, hours, and a status the inventory gives each
  night (operational or unavailable). That status is the listing's, not live availability;
- `nextbikestations` (Nextbike Cyprus): 343 stations, a capacity on some;
- `boltstations` (Bolt): 255 scooter parking zones in Nicosia, each a small polygon with a
  capacity;
- `bicycle_racks` (Public Works Department): 28 racks with their address.

`bike_stations_rental` (Public Works Department) is read too, only to fold its names into the
Nextbike stations it repeats (within 40 m). Its other Nicosia entries are numbered stations of an
earlier scheme that Nextbike's live feed no longer lists, so they are not drawn.

Each inventory is read once a day at most, one request for every page, tool and inspector, kept
on disk in data/chatbot_cache/nap_facilities/, and parsed defensively: a record without a usable
position is skipped and counted, a copy that doesn't parse is never kept, and when a refresh
fails yesterday's copy stands in (and says so). The catalogue's own record of each dataset
(when it last changed, who publishes it) is read alongside, as the inventory's date.

Nextbike publishes its own live feed for Cyprus (GBFS 2.3, CC0 1.0, listed by MobilityData):
the bikes standing at each station now, how many are electric, and the free docks. It is read at
most every two minutes, and only while something asks for it. Nothing else here is live: on the
city clock the inventories exist at every moment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from chatbot.geo import haversine_m
from chatbot.settings import CACHE_DIR

log = logging.getLogger(__name__)

NAP_DATA = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/{}/"
CATALOGUE_URL = "https://traffic4cyprus.org.cy/api/3/action/package_search?rows=1000"
DATASET_PAGE = "https://traffic4cyprus.org.cy/dataset/{}"
GBFS = "https://gbfs.nextbike.net/maps/gbfs/v2/nextbike_cy/en/{}.json"
GBFS_PAGE = "https://gbfs.nextbike.net/maps/gbfs/v2/nextbike_cy/gbfs.json"
LICENCE = "CC BY 4.0, Cyprus National Access Point (traffic4cyprus.org.cy)"
GBFS_LICENCE = "CC0 1.0, nextbike GmbH (GBFS)"

INVENTORY_S = 24 * 3600
RETRY_S = 3600  # after a failed refresh, the old copy stands in for an hour before trying again
LIVE_TTL_S = 120.0  # Nextbike's feed says 60 s; twice that is plenty for a map
LIVE_STALE_S = 15 * 60  # an older copy of the live bikes than this is no reading
# Greater Nicosia (lon_min, lat_min, lon_max, lat_max), as the road speeds use it.
AREA = (33.24, 35.07, 33.48, 35.23)
# Cyprus, to tell a position from a typing error (and to undo swapped coordinates).
CYPRUS = (32.2, 34.5, 34.7, 35.75)
FOLD_M = 40  # a rental station this close to a Nextbike station is the same one
FAST_KW = 43  # 43 kW and up charges a car in about an hour or less: "fast"

KINDS = ("car_park", "park_and_ride", "charger", "bike_station", "scooter_zone", "bike_rack")
LABELS = {"car_park": "Car park", "park_and_ride": "Park and ride", "charger": "EV charger",
          "bike_station": "Nextbike station", "scooter_zone": "Bolt scooter zone",
          "bike_rack": "Bicycle rack"}
PLURALS = {"car_park": "car parks", "park_and_ride": "park and ride sites", "charger": "EV chargers",
           "bike_station": "Nextbike stations", "scooter_zone": "Bolt scooter zones",
           "bike_rack": "bicycle racks"}
# The colours they are drawn in on the night map (and in legends): five hues apart, each with
# its own glyph on the page, so the colour never carries the kind alone. Sodium and signal stay
# free for "now" and "alarm".
COLORS = {"car_park": "#4c8dff", "park_and_ride": "#4c8dff", "charger": "#34d399",
          "bike_station": "#b48cff", "scooter_zone": "#f472b6", "bike_rack": "#9fb3c8"}
ID_PREFIX = {"car_park": "carpark-", "park_and_ride": "pnr-", "charger": "charger-",
             "bike_station": "nextbike-", "scooter_zone": "bolt-", "bike_rack": "rack-"}
ID_RE = re.compile(r"^(carpark|pnr|charger|nextbike|bolt|rack)-[0-9a-z]{1,16}$", re.IGNORECASE)
CONNECTORS = {"type2": "Type 2", "comboType2": "CCS (combo 2)", "chademo": "CHAdeMO",
              "type1": "Type 1", "comboType1": "CCS (combo 1)", "domestic": "Household socket",
              "teslaConnectorEurope": "Tesla"}


@dataclass(frozen=True)
class Feed:
    kind: str
    dataset: str  # its name under fixcyprus.cy/gnosis/open/api/nap/datasets/
    package: str  # its name in the catalogue (traffic4cyprus.org.cy/dataset/<package>)
    source: str  # its entry in chatbot/sources.py

    @property
    def url(self) -> str:
        return NAP_DATA.format(self.dataset)

    @property
    def page(self) -> str:
        return DATASET_PAGE.format(self.package)


FEEDS: dict[str, Feed] = {
    "car_park": Feed("car_park", "parking_areas", "parking_areas", "nap_public_works"),
    "park_and_ride": Feed("park_and_ride", "parkandride", "park_and_ride", "nap_public_works"),
    "charger": Feed("charger", "electric_vehicle_chargers", "electricvehiclecharges", "nap_chargers"),
    "bike_station": Feed("bike_station", "nextbikestations", "nextbike", "nap_nextbike"),
    "scooter_zone": Feed("scooter_zone", "boltstations", "bolt", "nap_bolt"),
    "bike_rack": Feed("bike_rack", "bicycle_racks", "bicycle_racks", "nap_public_works"),
}
RENTAL = Feed("bike_rental", "bike_stations_rental", "bike_stations_rental", "nap_public_works")


@dataclass
class Facility:
    id: str
    kind: str
    name: str
    lon: float
    lat: float
    name_el: str | None = None
    named: bool = True  # False for a car park mapped on OpenStreetMap with no name
    capacity: int | None = None
    address: str | None = None
    rings: list[list[tuple[float, float]]] = field(default_factory=list)  # polygons, (lon, lat)
    info: dict = field(default_factory=dict)  # what the kind adds (a charger's power, ...)

    @property
    def in_nicosia(self) -> bool:
        return AREA[0] <= self.lon <= AREA[2] and AREA[1] <= self.lat <= AREA[3]

    def search_words(self) -> list[str]:
        out = [self.name, self.name_el, self.address, LABELS[self.kind]]
        out += [self.info.get(k) for k in ("operator", "owner")]
        out += self.info.get("also_listed") or []
        return [x for x in out if x]


@dataclass
class Inventory:
    kind: str
    items: list[Facility]
    published: datetime | None  # the feed's publicationTime (it is regenerated every night)
    read_at: float  # when the twin read this copy
    total: int  # records in the feed
    skipped: int  # records left out: no usable position, or a test entry
    error: str | None = None  # the last refresh failed: this is an older copy
    updated: datetime | None = None  # when the catalogue last saw the dataset change
    publisher: str | None = None

    def public(self) -> dict:
        def iso(dt):
            return dt.isoformat(timespec="minutes") if dt else None
        return {"kind": self.kind, "count": len(self.items), "total": self.total,
                "skipped": self.skipped, "published": iso(self.published),
                "updated": iso(self.updated), "publisher": self.publisher,
                "read_at": iso(datetime.fromtimestamp(self.read_at, timezone.utc)),
                "error": self.error, "in_nicosia": sum(1 for f in self.items if f.in_nicosia)}


class FeedBroken(ValueError):
    """A copy of a feed that can't be used (not XML, or no usable records)."""


# -- reading DATEX II by local names, so a namespace prefix moving never breaks a parse ---------


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _kids(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _find(el: ET.Element, name: str) -> ET.Element | None:
    for x in el.iter():
        if x is not el and _local(x.tag) == name:
            return x
    return None


def _findall(el: ET.Element, name: str) -> list[ET.Element]:
    return [x for x in el.iter() if x is not el and _local(x.tag) == name]


def _text(el: ET.Element | None, name: str | None = None) -> str | None:
    if el is not None and name is not None:
        el = _find(el, name)
    if el is None or el.text is None:
        return None
    t = re.sub(r"\s+", " ", el.text).strip()
    return t or None


def _multilingual(el: ET.Element, name: str) -> dict[str, str]:
    """<name><value>..</value><lang>en</lang></name> (as often as there are languages) ->
    {lang: value}. A value without a language counts as English."""
    out: dict[str, str] = {}
    for n in _findall(el, name):
        v = _text(n, "value")
        if v:
            out.setdefault(_text(n, "lang") or "en", v)
    return out


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        v = float(text.replace(",", "."))
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _int(text: str | None) -> int | None:
    v = _number(text)
    return int(v) if v is not None and v >= 0 and v == int(v) else None


def _coords(pc: ET.Element) -> tuple[float, float] | None:
    """A pointCoordinates element -> (lon, lat) in Cyprus, or None. Swapped values are put back."""
    lat, lon = _number(_text(pc, "latitude")), _number(_text(pc, "longitude"))
    if lat is None or lon is None:
        return None
    w, s, e, n = CYPRUS
    if not (w <= lon <= e and s <= lat <= n) and (w <= lat <= e and s <= lon <= n):
        lat, lon = lon, lat
    if not (w <= lon <= e and s <= lat <= n):
        return None
    return round(lon, 6), round(lat, 6)


def _point(el: ET.Element) -> tuple[float, float] | None:
    """The record's point: its pointByCoordinates, not a polygon's corners."""
    pb = _find(el, "pointByCoordinates")
    pc = _find(pb, "pointCoordinates") if pb is not None else None
    return _coords(pc) if pc is not None else None


def _rings(el: ET.Element) -> list[list[tuple[float, float]]]:
    """Every polygon of the record as a closed ring of (lon, lat)."""
    out = []
    for poly in _findall(el, "polygon"):
        ring = [c for c in (_coords(pc) for pc in _findall(poly, "pointCoordinates")) if c]
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            out.append(ring)
    return out


def ring_centre(rings: list[list[tuple[float, float]]]) -> tuple[float, float] | None:
    """The middle of the biggest ring (its area centroid; the mean of its corners when flat)."""
    best = None
    for ring in rings:
        a = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if abs(a) > 1e-12:
            c = (cx / (3 * a), cy / (3 * a))
        else:
            c = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
        if best is None or abs(a) > best[0]:
            best = (abs(a), c)
    return (round(best[1][0], 6), round(best[1][1], 6)) if best else None


def _root(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise FeedBroken(f"not XML: {e}") from None


def published_at(root: ET.Element) -> datetime | None:
    t = _text(root, "publicationTime")
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _records(root: ET.Element, name: str) -> list[ET.Element]:
    return [x for x in root.iter() if _local(x.tag) == name]


def _ident(el: ET.Element) -> str | None:
    vi = _find(el, "versionedIdentifier")
    t = _text(vi, "identifier") if vi is not None else None
    if t is None:
        t = next((_text(c) for c in el if _local(c.tag) in ("id", "identifier")), None)
    return t


def _title(name: str) -> str:
    """Names come in either case ("OLD PORT"): read the shouted ones in title case."""
    return name.title() if name.isupper() and len(name) > 3 else name


def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.lower())[:16]


# -- one parser per feed: each returns (facilities, records, skipped) --------------------------


def parse_car_parks(xml_text: str) -> tuple[list[Facility], int, int, ET.Element]:
    root = _root(xml_text)
    recs = _records(root, "parkingFacility")
    out, skipped = [], 0
    for r in recs:
        ident, pt = _ident(r), _point(r)
        if not ident or pt is None:
            skipped += 1
            continue
        names = _multilingual(r, "parkingFacilityName")
        en = names.get("en") or names.get("el")
        named = bool(en) and en.strip().lower() != "osm origin"
        el_name = names.get("el")
        if el_name and el_name.strip().lower() == "osm origin":
            el_name = None
        out.append(Facility(f"carpark-{_slug(ident)}", "car_park", en if named else "Car park",
                            pt[0], pt[1], name_el=el_name if named else None, named=named,
                            info={"from_osm": not named, "nap_id": ident}))
    return out, len(recs), skipped, root


def parse_park_and_ride(xml_text: str) -> tuple[list[Facility], int, int, ET.Element]:
    root = _root(xml_text)
    recs = _records(root, "parkingFacility")
    out, skipped = [], 0
    for r in recs:
        ident = _ident(r)
        rings = _rings(r)
        pt = ring_centre(rings) if rings else _point(r)
        if ident is None or pt is None:
            skipped += 1
            continue
        names = _multilingual(r, "parkingFacilityName")
        name = names.get("en") or names.get("el") or "Park and ride"
        out.append(Facility(f"pnr-{_slug(ident)}", "park_and_ride", name, pt[0], pt[1],
                            name_el=names.get("el"), rings=rings, info={"nap_id": ident}))
    return out, len(recs), skipped, root


def _charger_id(name: str, taken: set[str]) -> str:
    base = "charger-" + hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:8]
    fid, n = base, 2
    while fid in taken:
        fid, n = f"{base[:-1]}{n}", n + 1
    taken.add(fid)
    return fid


def charger_speed(kw: float | None) -> str:
    return "fast" if kw is not None and kw >= FAST_KW else "standard"


def parse_chargers(xml_text: str) -> tuple[list[Facility], int, int, ET.Element]:
    root = _root(xml_text)
    recs = _records(root, "chargingPoint")
    out, skipped, taken = [], 0, set()
    for r in recs:
        name, pt = _text(r, "chargingPointIdentification"), _point(r)
        if not name or pt is None:
            skipped += 1
            continue
        contacts = []
        for c in _findall(r, "contact"):
            entry = {"type": _text(c, "contactType"), "phone": _text(c, "phoneNumber"),
                     "email": _text(c, "emailAddress")}
            if entry["phone"] or entry["email"]:
                contacts.append({k: v for k, v in entry.items() if v})
        connector_kw = _number(_text(r, "connectorPower"))
        max_kw = _number(_text(r, "maximumPower"))
        # The power one car gets: the connector's, else the point's maximum.
        kw = connector_kw if connector_kw else max_kw
        types = [t for t in (_text(x) for x in _findall(r, "connectorType")) if t]
        status = (_text(_find(r, "chargingPointStatus"), "value")
                  if _find(r, "chargingPointStatus") is not None else None)
        info = {
            "owner": _text(r, "chargingPointOwner"), "operator": _text(r, "chargingPointOperator"),
            "connectors": _int(_text(r, "numberOfConnectors")), "kw": kw, "max_kw": max_kw,
            "speed": charger_speed(kw), "mode": _text(r, "chargingMode"),
            "power_type": (_text(r, "powerType") or "").lower() or None,
            "connector_types": list(dict.fromkeys(types)),
            "status": status.lower() if status else None,
            "hours": _text(_find(r, "operatingTime"), "value") if _find(r, "operatingTime") is not None else None,
            "access": _text(_find(r, "accessInformation"), "value") if _find(r, "accessInformation") is not None else None,
            "spaces": _int(_text(r, "numberOfParkingSpaces")),
            "accessible_spaces": _int(_text(r, "numberOfAccessibleParkingSpaces")),
            "vehicles": [v for v in (_text(x) for x in _findall(r, "vehicleType")) if v],
            "since": _text(r, "creationDate"), "contacts": contacts,
        }
        hours = info["hours"] or ""
        if hours.startswith("{"):  # a schedule written out as code: say only that it has one
            info["hours"] = "Set hours each day"
        addr = _text(_find(r, "chargingPointAddress"), "value") if _find(r, "chargingPointAddress") is not None else None
        out.append(Facility(_charger_id(name, taken), "charger", name, pt[0], pt[1],
                            address=addr, capacity=info["connectors"], info=info))
    return out, len(recs), skipped, root


def parse_bike_stations(xml_text: str) -> tuple[list[Facility], int, int, ET.Element]:
    root = _root(xml_text)
    recs = _records(root, "sharedMobility")
    out, skipped = [], 0
    for r in recs:
        ident, pt = _ident(r), _point(r)
        if not ident or pt is None:
            skipped += 1
            continue
        names = _multilingual(r, "sharedMobilityFacilityName")
        name = _title((names.get("en") or names.get("el") or f"Station {ident}").strip())
        out.append(Facility(f"nextbike-{_slug(ident)}", "bike_station", name, pt[0], pt[1],
                            capacity=_int(_text(r, "capacity")), info={"station_id": ident}))
    return out, len(recs), skipped, root


def parse_scooter_zones(xml_text: str) -> tuple[list[Facility], int, int, ET.Element]:
    root = _root(xml_text)
    recs = _records(root, "sharedMobility")
    out, skipped, taken = [], 0, set()
    for r in recs:
        ident = _ident(r)
        rings = _rings(r)
        pt = _point(r) or (ring_centre(rings) if rings else None)
        if not ident or pt is None:
            skipped += 1
            continue
        names = _multilingual(r, "sharedMobilityFacilityName")
        raw = names.get("en") or names.get("el") or "Scooter zone"
        # "6- Rigenis": the number in front is Bolt's own code, not the street.
        name = re.sub(r"^\s*\d+\s*-\s*", "", raw).replace(" _ ", ", ").strip() or raw
        slug = _slug(ident)
        fid, n = f"bolt-{slug[:8]}", 12
        while fid in taken and n <= 16:
            fid, n = f"bolt-{slug[:n]}", n + 4
        taken.add(fid)
        out.append(Facility(fid, "scooter_zone", name, pt[0], pt[1], rings=rings,
                            capacity=_int(_text(r, "capacity")), info={"zone_id": ident}))
    return out, len(recs), skipped, root


def parse_bike_racks(xml_text: str) -> tuple[list[Facility], int, int, ET.Element]:
    root = _root(xml_text)
    recs = _records(root, "parkingFacility")
    out, skipped = [], 0
    for r in recs:
        ident, pt = _ident(r), _point(r)
        names = _multilingual(r, "parkingFacilityName")
        name = names.get("en") or names.get("el")
        if not ident or pt is None or not name or name.strip().lower() in ("test", "δοκιμή"):
            skipped += 1
            continue
        lines: dict[str, list[str]] = {}
        for line in _findall(r, "addressLineText"):
            v, lang = _text(line, "value"), _text(line, "lang") or "en"
            if v and v not in lines.setdefault(lang, []):
                lines[lang].append(v)
        address = ", ".join(lines.get("en") or lines.get("el") or []) or None
        out.append(Facility(f"rack-{_slug(ident)}", "bike_rack", name, pt[0], pt[1],
                            name_el=names.get("el"), address=address,
                            info={"address_el": ", ".join(lines.get("el") or []) or None,
                                  "reference": _text(r, "parkingFacilityReference")}))
    return out, len(recs), skipped, root


def parse_rental(xml_text: str) -> list[dict]:
    """bike_stations_rental: [{name, provider, lon, lat}] (named entries only)."""
    root = _root(xml_text)
    out = []
    for r in _records(root, "sharedMobility"):
        pt = _point(r)
        names = _multilingual(r, "sharedMobilityFacilityName")
        name = names.get("en") or names.get("el")
        if pt is None:
            continue
        out.append({"name": name, "provider": _text(r, "organisationName"), "lon": pt[0], "lat": pt[1]})
    return out


PARSERS = {"car_park": parse_car_parks, "park_and_ride": parse_park_and_ride,
           "charger": parse_chargers, "bike_station": parse_bike_stations,
           "scooter_zone": parse_scooter_zones, "bike_rack": parse_bike_racks}


def fold_rental(stations: list[Facility], rental: list[dict]) -> None:
    """Name each Nextbike station's twin in the rental list ("15. Solomou Square Station")."""
    for f in stations:
        also = []
        for r in rental:
            if abs(r["lat"] - f.lat) > 0.001 or abs(r["lon"] - f.lon) > 0.0012:
                continue
            if haversine_m(f.lat, f.lon, r["lat"], r["lon"]) > FOLD_M:
                continue
            name = re.sub(r"^\d+\.\s*", "", r.get("name") or "").strip()
            if name and name.lower() not in ("nextbike", f.name.lower()) and name not in also:
                also.append(name)
        if also:
            f.info["also_listed"] = also


def parse_catalogue(body: str) -> dict[str, dict]:
    """The catalogue's search -> {package name: {updated, publisher, licence}}."""
    try:
        data = json.loads(body)
        results = data["result"]["results"]
    except (ValueError, KeyError, TypeError) as e:
        raise FeedBroken(f"the catalogue's answer isn't what it should be: {e}") from None
    out = {}
    for p in results:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        out[p["name"]] = {"updated": p.get("metadata_modified"),
                          "publisher": (p.get("organization") or {}).get("title"),
                          "licence": p.get("license_id")}
    return out


# -- live bikes (GBFS) ------------------------------------------------------------------------


@dataclass
class Bikes:
    """Nextbike's live station status: station id -> {bikes, ebikes, docks, renting, ...}."""
    status: str  # live | stale | unavailable
    fetched_at: float
    stations: dict[str, dict]
    error: str | None = None
    feed_time: float | None = None  # when Nextbike last updated it

    def at(self, station_id: str) -> dict | None:
        return self.stations.get(station_id) if self.status != "unavailable" else None


def parse_status(body: str, electric: set[str]) -> tuple[dict[str, dict], float | None]:
    try:
        data = json.loads(body)
        stations = data["data"]["stations"]
    except (ValueError, KeyError, TypeError) as e:
        raise FeedBroken(f"Nextbike's station status isn't what it should be: {e}") from None
    out = {}
    for s in stations:
        if not isinstance(s, dict) or s.get("station_id") is None:
            continue
        bikes = _int(str(s.get("num_bikes_available"))) or 0
        ebikes = sum(int(v.get("count") or 0) for v in s.get("vehicle_types_available") or []
                     if str(v.get("vehicle_type_id")) in electric)
        docks = s.get("num_docks_available")
        out[str(s["station_id"])] = {
            "bikes": bikes, "ebikes": min(ebikes, bikes),
            "docks": int(docks) if isinstance(docks, (int, float)) else None,
            "installed": bool(s.get("is_installed", True)), "renting": bool(s.get("is_renting", True)),
            "returning": bool(s.get("is_returning", True)), "reported": s.get("last_reported")}
    updated = data.get("last_updated")
    return out, float(updated) if isinstance(updated, (int, float)) else None


def parse_vehicle_types(body: str) -> set[str]:
    """The vehicle types with a motor (e-bikes)."""
    try:
        types = json.loads(body)["data"]["vehicle_types"]
    except (ValueError, KeyError, TypeError):
        return set()
    return {str(v.get("vehicle_type_id")) for v in types
            if isinstance(v, dict) and str(v.get("propulsion_type", "")).startswith("electric")}


def parse_station_information(body: str) -> dict[str, dict]:
    try:
        stations = json.loads(body)["data"]["stations"]
    except (ValueError, KeyError, TypeError):
        return {}
    out = {}
    for s in stations:
        if isinstance(s, dict) and s.get("station_id") is not None:
            out[str(s["station_id"])] = {"capacity": s.get("capacity"), "virtual": bool(s.get("is_virtual_station")),
                                         "short_name": s.get("short_name"),
                                         "web": (s.get("rental_uris") or {}).get("web")}
    return out


# -- the inventories, shared by every page and tool ------------------------------------------------


class Facilities:
    """The six inventories (read once a day at most, kept on disk) and Nextbike's live bikes."""

    def __init__(self, fetch_text, *, now=None, cache_dir: Path | None = None,
                 live_ttl_s: float = LIVE_TTL_S, background: bool = True):
        self.fetch_text = fetch_text  # fetch_text(url) -> str
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.dir = (cache_dir or CACHE_DIR) / "nap_facilities"
        self.live_ttl_s = live_ttl_s
        self.background = background  # refresh a stale copy in the background (tests: inline)
        self._inv: dict[str, Inventory] = {}
        self._locks = {k: threading.RLock() for k in (*KINDS, "bike_rental", "catalogue", "gbfs_info")}
        self._refreshing: set[str] = set()
        self._retry_at: dict[str, float] = {}
        self._rental: tuple[float, list[dict]] | None = None
        self._catalogue: dict[str, dict] | None = None
        self._by_id: dict[str, Facility] = {}
        self._bikes: Bikes | None = None
        self._bikes_lock = threading.Lock()
        self._gbfs_info: tuple[float, set[str], dict[str, dict]] | None = None
        self.search_cache: dict = {}

    # -- files on disk ---------------------------------------------------------------------

    def _path(self, name: str, ext: str = "xml") -> Path:
        return self.dir / f"{name}.{ext}"

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _fresh(self, path: Path, max_age: float = INVENTORY_S) -> bool:
        return path.exists() and time.time() - path.stat().st_mtime < max_age

    # -- one inventory ---------------------------------------------------------------------

    def cached(self, kind: str) -> Inventory | None:
        """The inventory as last read, without reading anything (None before the first read)."""
        return self._inv.get(kind)

    def inventory(self, kind: str) -> Inventory:
        """The inventory of a kind. The first read waits for the feed (a few seconds); after
        that a copy older than a day is served while a fresh one is read in the background."""
        if kind not in FEEDS:
            raise KeyError(kind)
        hit = self._inv.get(kind)
        if hit is not None:
            if self._due(kind, hit):
                self._refresh_later(kind)
            return hit
        with self._locks[kind]:
            hit = self._inv.get(kind)
            if hit is not None:
                return hit
            path = self._path(FEEDS[kind].dataset)
            if path.exists():
                try:
                    inv = self._load(kind, path.read_text(encoding="utf-8"), path.stat().st_mtime)
                except FeedBroken as e:  # a broken file on disk: read the feed again
                    log.warning("cached %s inventory unreadable: %s", kind, e)
                    inv = None
                if inv is not None:
                    self._set(kind, inv)
                    if self._due(kind, inv):
                        self._refresh_later(kind)
                    return inv
            inv = self._read_feed(kind)  # raises when the feed fails and there is no copy
            self._set(kind, inv)
            return inv

    def _due(self, kind: str, inv: Inventory) -> bool:
        if time.time() < self._retry_at.get(kind, 0):
            return False
        return time.time() - inv.read_at >= INVENTORY_S

    def _refresh_later(self, kind: str) -> None:
        if kind in self._refreshing:
            return
        self._refreshing.add(kind)

        def run() -> None:
            try:
                self.refresh(kind)
            finally:
                self._refreshing.discard(kind)
        if self.background:
            threading.Thread(target=run, daemon=True, name=f"nap-{kind}").start()
        else:
            run()

    def refresh(self, kind: str) -> Inventory | None:
        """Read the feed again now. A failure keeps the copy there is, and says so."""
        with self._locks[kind]:
            try:
                inv = self._read_feed(kind)
            except Exception as e:  # noqa: BLE001 - yesterday's copy is fine for an inventory
                old = self._inv.get(kind)
                self._retry_at[kind] = time.time() + RETRY_S
                log.warning("%s inventory not refreshed: %s", kind, e)
                if old is not None:
                    old.error = f"the national feed didn't answer ({str(e)[:140]}); this is the copy of {_day(old.read_at)}"
                return old
            self._set(kind, inv)
            return inv

    def _read_feed(self, kind: str) -> Inventory:
        feed = FEEDS[kind]
        text = self.fetch_text(feed.url)
        inv = self._load(kind, text, time.time())  # raises FeedBroken: never keep a broken copy
        self._write(self._path(feed.dataset), text)
        self._retry_at.pop(kind, None)
        self._catalogue_later()
        return inv

    def _load(self, kind: str, text: str, read_at: float) -> Inventory:
        items, total, skipped, root = PARSERS[kind](text)
        if not items:
            raise FeedBroken(f"no usable records in {total}")
        if kind == "bike_station":
            fold_rental(items, self.rental())
        inv = Inventory(kind, items, published_at(root), read_at, total, skipped)
        self._date(inv)
        return inv

    def _set(self, kind: str, inv: Inventory) -> None:
        self._date(inv)
        self._inv[kind] = inv
        self._by_id = {f.id: f for k in KINDS if (i := self._inv.get(k)) for f in i.items}
        self.search_cache.clear()

    # -- the rental list, folded into the Nextbike stations ----------------------------------------

    def rental(self) -> list[dict]:
        hit = self._rental
        if hit is not None and time.time() - hit[0] < INVENTORY_S:
            return hit[1]
        path = self._path(RENTAL.dataset)
        rows: list[dict] = []
        try:
            if self._fresh(path):
                rows = parse_rental(path.read_text(encoding="utf-8"))
            else:
                text = self.fetch_text(RENTAL.url)
                rows = parse_rental(text)
                self._write(path, text)
        except Exception as e:  # noqa: BLE001 - the stations stand without their other names
            log.info("bike rental list not read: %s", e)
            if path.exists():
                try:
                    rows = parse_rental(path.read_text(encoding="utf-8"))
                except FeedBroken:
                    rows = []
        self._rental = (time.time(), rows)
        return rows

    # -- the catalogue: when each dataset last changed ----------------------------------------------

    def catalogue(self) -> dict[str, dict]:
        """{package: {updated, publisher, licence}} from the copy on disk (never waits)."""
        if self._catalogue is None:
            try:
                self._catalogue = parse_catalogue(self._path("catalogue", "json").read_text(encoding="utf-8"))
            except (OSError, FeedBroken):
                self._catalogue = {}
        return self._catalogue

    def _catalogue_later(self) -> None:
        path = self._path("catalogue", "json")
        if self._fresh(path) or "catalogue" in self._refreshing:
            return
        self._refreshing.add("catalogue")

        def run() -> None:
            try:
                body = self.fetch_text(CATALOGUE_URL)
                cat = parse_catalogue(body)
                self._write(path, json.dumps({"result": {"results": [
                    {"name": k, "metadata_modified": v["updated"], "organization": {"title": v["publisher"]},
                     "license_id": v["licence"]} for k, v in cat.items()]}}, ensure_ascii=False))
                self._catalogue = cat
                for inv in self._inv.values():
                    self._date(inv)
            except Exception as e:  # noqa: BLE001 - the inventory's own date stands in
                log.info("catalogue not read: %s", e)
            finally:
                self._refreshing.discard("catalogue")
        if self.background:
            threading.Thread(target=run, daemon=True, name="nap-catalogue").start()
        else:
            run()

    def _date(self, inv: Inventory) -> None:
        entry = self.catalogue().get(FEEDS[inv.kind].package) or {}
        updated = entry.get("updated")
        try:
            dt = datetime.fromisoformat(updated) if updated else None
        except ValueError:
            dt = None
        inv.updated = dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt
        inv.publisher = entry.get("publisher")

    # -- everything, by id and by place ------------------------------------------------------

    def all(self, kinds: tuple[str, ...] | list[str] = KINDS, strict: bool = False) -> list[Facility]:
        """Every facility of these kinds. A kind whose feed fails is left out (strict: raises)."""
        out = []
        for k in kinds:
            try:
                out += self.inventory(k).items
            except Exception as e:  # noqa: BLE001 - one feed failing leaves the others
                if strict:
                    raise
                log.warning("%s inventory unavailable: %s", k, e)
        return out

    @staticmethod
    def kind_of(fid: str) -> str | None:
        low = fid.strip().lower()
        return next((k for k, p in ID_PREFIX.items() if low.startswith(p)), None)

    def get(self, fid: str) -> Facility | None:
        fid = fid.strip().lower()
        hit = self._by_id.get(fid)
        if hit is not None:
            return hit
        kind = self.kind_of(fid)
        if kind is None:
            return None
        return next((f for f in self.inventory(kind).items if f.id == fid), None)

    def near(self, lat: float, lon: float, radius_m: float, kinds=KINDS,
             limit: int = 50, items: list[Facility] | None = None) -> list[tuple[float, Facility]]:
        dlat = radius_m / 110_574
        dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
        found = []
        for f in items if items is not None else self.all(kinds):
            if f.kind not in kinds or abs(f.lat - lat) > dlat or abs(f.lon - lon) > dlon:
                continue
            d = haversine_m(lat, lon, f.lat, f.lon)
            if d <= radius_m:
                found.append((d, f))
        found.sort(key=lambda t: t[0])
        return found[:limit]

    def status(self) -> dict[str, dict]:
        """Each kind's inventory as last read (nothing is read here)."""
        return {k: (i.public() if (i := self._inv.get(k)) else None) for k in KINDS}

    # -- Nextbike's live bikes -----------------------------------------------------------------

    def _info(self) -> tuple[set[str], dict[str, dict]]:
        """Which vehicle types are electric and each station's capacity, read daily."""
        hit = self._gbfs_info
        if hit is not None and time.time() - hit[0] < INVENTORY_S:
            return hit[1], hit[2]
        with self._locks["gbfs_info"]:
            texts = {}
            for name in ("vehicle_types", "station_information"):
                path = self._path(f"gbfs_{name}", "json")
                try:
                    if self._fresh(path):
                        texts[name] = path.read_text(encoding="utf-8")
                    else:
                        texts[name] = self.fetch_text(GBFS.format(name))
                        json.loads(texts[name])
                        self._write(path, texts[name])
                except Exception as e:  # noqa: BLE001 - counts still work without it
                    log.info("Nextbike %s not read: %s", name, e)
                    texts[name] = path.read_text(encoding="utf-8") if path.exists() else "{}"
            electric = parse_vehicle_types(texts["vehicle_types"])
            info = parse_station_information(texts["station_information"])
            self._gbfs_info = (time.time(), electric, info)
            return electric, info

    def station_info(self, station_id: str) -> dict | None:
        hit = self._gbfs_info
        return hit[2].get(station_id) if hit else None

    def last_bikes(self) -> Bikes | None:
        """The live bikes as last read, without reading (None before the first read)."""
        return self._bikes

    def bikes(self) -> Bikes:
        """The live bikes at every station: one request every live_ttl_s for everyone. When it
        fails the last copy stands in for 15 minutes (status stale), then there is none."""
        now = time.time()
        hit = self._bikes
        if hit and now - hit.fetched_at < self.live_ttl_s:
            return hit
        with self._bikes_lock:
            hit = self._bikes
            if hit and now - hit.fetched_at < self.live_ttl_s:
                return hit
            try:
                electric, _ = self._info()
                stations, feed_time = parse_status(self.fetch_text(GBFS.format("station_status")), electric)
                self._bikes = Bikes("live", now, stations, None, feed_time)
            except Exception as e:  # noqa: BLE001 - keep the last copy a while, say why
                err = f"Nextbike's live feed didn't answer: {str(e)[:160]}"
                log.warning("%s", err)
                if hit and hit.status != "unavailable" and now - (hit.feed_time or hit.fetched_at) < LIVE_STALE_S:
                    self._bikes = Bikes("stale", now - self.live_ttl_s + 60, hit.stations, err, hit.feed_time)
                else:
                    self._bikes = Bikes("unavailable", now - self.live_ttl_s + 60, {}, err)
            return self._bikes


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%d %b %Y").lstrip("0")


__all__ = ["Facilities", "Facility", "Inventory", "Bikes", "FEEDS", "KINDS", "LABELS", "PLURALS",
           "COLORS", "ID_RE", "CONNECTORS", "AREA", "LICENCE", "GBFS_LICENCE", "charger_speed"]

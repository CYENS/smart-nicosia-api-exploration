"""The street inventories of the Cyprus National Access Point, read once a day.

Seven DATEX II files on fixcyprus.cy (CC BY 4.0, regenerated nightly), published by the Public
Works Department, except the enforcement cameras (Elovate, for the Ministry of Transport):
speed limit signs, 20 and 30 km/h zones with their entrance points, traffic signals, pedestrian
crossings, cycleways, bus lanes and enforcement cameras. They are inventories, not readings: a
sign stands at every moment of the city clock.

Each file is fetched at most once a day for every page and tool, kept on disk, and yesterday's
copy stands in when a fetch fails. The files are written in several ways (namespaces numbered
per element in the bus lanes, latitude and longitude swapped in the zones, some cameras placed
to two decimals), so everything is read by local tag name and checked against Cyprus.

How old the inventory is: the catalogue (traffic4cyprus.org.cy, CKAN) says when the publisher
last changed each dataset, and each file says when it was written. Both are kept.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

from chatbot.settings import CACHE_DIR

log = logging.getLogger(__name__)

BASE = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/"
CATALOGUE = "https://traffic4cyprus.org.cy/api/3/action/package_search"
DATASET_PAGE = "https://traffic4cyprus.org.cy/dataset/"
LICENCE = "CC BY 4.0, Cyprus National Access Point (traffic4cyprus.org.cy)"
INVENTORY_S = 24 * 3600
# Greater Nicosia (lon_min, lat_min, lon_max, lat_max), as the road speeds use it.
AREA = (33.24, 35.07, 33.48, 35.23)
# Cyprus, to tell a real position from a swapped or broken one.
CYPRUS = (32.2, 34.5, 34.65, 35.75)
PWD = "Public Works Department"


@dataclass(frozen=True)
class Feed:
    key: str  # how the twin names it
    path: str  # under BASE
    dataset: str  # its page on the catalogue
    title: str
    publisher: str = PWD


FEEDS: dict[str, Feed] = {f.key: f for f in (
    Feed("speed_limits", "speed_limits", "speed_limits", "Speed limit signs"),
    Feed("zones", "low_speed_emission_zones", "lez", "20 and 30 km/h zones"),
    Feed("zone_entrances", "low_speed_emission_entrance_point", "lez_entrance_points", "Zone entrance points"),
    Feed("signals", "traffic_signals", "traffic_signals", "Traffic signals"),
    Feed("crossings", "pedestrian_crossings", "pedestrian_crossings", "Pedestrian crossings"),
    Feed("cycleways", "cycleways", "cycleways", "Cycleways"),
    Feed("bus_lanes", "bus_lanes", "bus_lanes", "Bus lanes"),
    Feed("cameras", "traffic_enforcement_cameras", "traffic_enforcement_cameras", "Enforcement cameras",
         publisher="Elovate, for the Ministry of Transport, Communications and Works"),
)}


# -- reading XML whatever its namespaces -----------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _records(root: ET.Element, tag: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == tag]


def _texts(el: ET.Element, tag: str) -> list[str]:
    return [x.text.strip() for x in el.iter() if _local(x.tag) == tag and x.text and x.text.strip()]


def _first(el: ET.Element, tag: str) -> str | None:
    t = _texts(el, tag)
    return t[0] if t else None


def _lang_values(el: ET.Element, tag: str) -> dict[str, str]:
    """{lang: value} of the multilingual strings under `tag` (common:value + common:lang)."""
    out: dict[str, str] = {}
    for x in el.iter():
        if _local(x.tag) != tag:
            continue
        value = _first(x, "value")
        lang = (_first(x, "lang") or "en").lower()
        if value and lang not in out:
            out[lang] = value
    return out


def _float(s: str | None) -> float | None:
    try:
        v = float(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _points(el: ET.Element) -> list[tuple[float, float, int]]:
    """Every pointCoordinates under `el` as (lon, lat, decimals): decimals is the fewer digits
    after the point of the two, so a position written to 0.01 degrees can say so."""
    out = []
    for p in el.iter():
        if _local(p.tag) != "pointCoordinates":
            continue
        la = next((c.text for c in p if _local(c.tag) == "latitude"), None)
        lo = next((c.text for c in p if _local(c.tag) == "longitude"), None)
        lat, lon = _float(la), _float(lo)
        if lat is None or lon is None:
            continue
        dec = min(len((la or "").strip().partition(".")[2]), len((lo or "").strip().partition(".")[2]))
        out.append((lon, lat, dec))
    return out


def _in_cyprus(lon: float, lat: float) -> bool:
    return CYPRUS[0] <= lon <= CYPRUS[2] and CYPRUS[1] <= lat <= CYPRUS[3]


def fix_order(points: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Points with latitude and longitude the right way round. In Cyprus the latitude (34.5 to
    35.7) is always the larger number, so a geometry whose 'latitudes' are mostly the smaller
    is written swapped (the zone files are)."""
    if not points:
        return points
    swapped = sum(1 for lon, lat, _ in points if lat < lon)
    if swapped * 2 > len(points):
        points = [(lat, lon, d) for lon, lat, d in points]
    return [p for p in points if _in_cyprus(p[0], p[1])]


def in_area(lon: float, lat: float, area=AREA) -> bool:
    return area[0] <= lon <= area[2] and area[1] <= lat <= area[3]


def _touches_area(coords) -> bool:
    return any(in_area(x, y) for x, y in coords)


def publication_time(root: ET.Element) -> str | None:
    t = next((x.text.strip() for x in root.iter() if _local(x.tag) == "publicationTime" and x.text), None)
    return t


# -- names --------------------------------------------------------------------------------------

GREEK = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i",
    "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s",
    "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}
PAIRS = (("ου", "ou"), ("αι", "ai"), ("ει", "ei"), ("οι", "oi"), ("γγ", "ng"), ("γκ", "gk"),
         ("ντ", "nt"), ("μπ", "mp"), ("αυ", "av"), ("ευ", "ev"))
# Places and councils the inventories name, as they are written in English.
PLACE_NAMES = {
    "λευκωσια": "Nicosia", "λεμεσος": "Limassol", "λαρνακα": "Larnaca", "αγλαντζια": "Aglantzia",
    "αγλατζια": "Aglantzia", "καιμακλι": "Kaimakli", "παλλουριωτισσα": "Pallouriotissa",
    "κατω πολεμιδια": "Kato Polemidia", "μεσα γειτονια": "Mesa Geitonia",
    "δημος λευκωσιας": "Nicosia Municipality", "δημος στροβολου": "Strovolos Municipality",
    "δημος αγλαντζιας": "Aglantzia Municipality", "δημος εγκωμης": "Engomi Municipality",
    "δημος λακαταμιας": "Lakatamia Municipality", "δημος λατσιων": "Latsia Municipality",
    "δημος αγιου δομετιου": "Agios Dometios Municipality", "δημος ιδαλιου": "Idalion Municipality",
    "δημος γεριου": "Geri Municipality", "δημος τσεριου": "Tseri Municipality",
}


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def latin(text: str | None) -> str | None:
    """A Greek name in Latin letters (ELOT 743, simplified): 'Αγλατζιά' -> 'Aglatzia'. Names the
    inventories use often are given as they are usually written in English."""
    if not text:
        return text
    key = re.sub(r"\s+", " ", _plain(text))
    if key in PLACE_NAMES:
        return PLACE_NAMES[key]
    if not re.search(r"[α-ωΑ-Ω]", text):
        return text

    def word(w: str) -> str:
        if not re.search(r"[α-ωΑ-Ωά-ώΆ-Ώ]", w):
            return w
        p = _plain(w)
        for a, b in PAIRS:
            p = p.replace(a, b)
        out = "".join(GREEK.get(c, c) for c in p)
        return out[:1].upper() + out[1:] if w[:1].isupper() else out
    return re.sub(r"[^\W\d_]+", lambda m: word(m.group(0)), text)


# Greek capitals typed inside Latin names ('Μakariou', 'ΤRAΜ'), read as the letters they look like.
LOOKALIKE = str.maketrans("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEZHIKMNOPTYX")
PHRASES = ((re.compile(r"\(\s*Diadromos\s+TRAM\s*\)", re.I), "(tram corridor)"),
           (re.compile(r"\s*-\s*Rythmisi\s+me\s+ITS", re.I), " (managed by ITS)"),
           (re.compile(r"\s+[–-]\s+"), " to "))


def english(name: str | None) -> str | None:
    """An English name from the feeds tidied: lookalike Greek capitals read as Latin, 'Leof. X'
    as 'X Avenue', the tram corridor and ITS notes in words, 'A - B' as 'A to B'."""
    if not name:
        return name
    t = re.sub(r"\s+", " ", name.translate(LOOKALIKE)).strip()
    t = re.sub(r"\bLeof\.\s*([^(/,]+?)(?=\s+(?:to|[-–])\s|\s*[(/,]|$)", r"\1 Avenue", t)
    for rx, sub in PHRASES:
        t = rx.sub(sub, t)
    return t


def hyphens(text: str | None) -> str | None:
    """The publisher's own names keep their words, with plain hyphens for their dashes."""
    return text.replace("–", "-").replace("—", "-") if text else text


def smart_title(text: str) -> str:
    """'MAKARIOU III - TECH. SCHOOL' -> 'Makariou III - Tech. School' (Roman numerals kept)."""
    if not text.isupper():
        return text
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0) if re.fullmatch(r"[IVX]{1,4}", m.group(0))
                  else m.group(0).capitalize(), text)


CYCLE_PREFIX = re.compile(r"^\s*Ποδηλατική\s+Υποδομή\s*[-–]?\s*", re.IGNORECASE)
BUILD_PREFIX = re.compile(r"^\s*Κατασκευή\s+Πεζόδρομου\s*/\s*Ποδηλατόδρομου\s*", re.IGNORECASE)


PLANNING_NOTE = re.compile(r"\(\s*Περιλαμβάνεται\s+σε\s+Πολεοδομικό\s+Έργο\s*\)", re.I)
LINKING = re.compile(r"^που\s+συνδέει\s+το\s+(?P<a>.+?)\s+με\s+τον?\s+(?:Δήμο\s+)?(?P<b>.+)$", re.I)
GREEK_WORDS = {"παν.κύπρου": "the University of Cyprus", "γερίου": "Geri"}


def _greek_part(t: str) -> str:
    key = _plain(t)
    for k, v in GREEK_WORDS.items():
        if _plain(k) == key:
            return v
    return english(latin(t.replace("Λεωφ.", "Leof.")) or t) or t


def cycle_name(el_name: str | None) -> str:
    """'Ποδηλατική Υποδομή Λεωφ. Αλεξανδρουπόλεως' -> 'Cycle route, Alexandroupoleos Avenue'.
    A section of the network named by its letter is 'Cycle network, section D'."""
    if not el_name or el_name.strip().upper() == "NO NAME":
        return "Cycle route (no name)"
    text = PLANNING_NOTE.sub("", el_name).strip()
    if BUILD_PREFIX.match(text):
        rest = re.sub(r"^(στη|στην|στο|στον)\s+", "", BUILD_PREFIX.sub("", text).strip(), flags=re.I)
        m = LINKING.match(rest)
        if m:
            return f"Footpath and cycleway, {_greek_part(m.group('a'))} to {_greek_part(m.group('b'))}"
        return f"Footpath and cycleway, {_greek_part(rest)}" if rest else "Footpath and cycleway"
    rest = CYCLE_PREFIX.sub("", text).strip()
    if re.fullmatch(r"[Α-Ω]['΄’]?", rest):
        return f"Cycle network, section {latin(rest[0])}"
    return f"Cycle route, {_greek_part(rest)}" if rest else "Cycle route"


# -- the parsers: XML text -> records ---------------------------------------------------------------


def parse_speed_limits(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "trafficRegulation"):
        ident = _first(r, "identifier")
        kmh = _float(_first(r, "speedLimitValue"))
        pts = fix_order(_points(r))
        if not ident or kmh is None or not (5 <= kmh <= 130) or not pts:
            continue
        lon, lat, _ = pts[0]
        out.append({"id": f"limit-{ident}", "n": ident, "kmh": int(kmh),
                    "road_ref": _first(r, "trafficRegulationReference"),
                    "lon": round(lon, 6), "lat": round(lat, 6)})
    return out


ZONE_KIND = re.compile(r"(ζώνη|ζωνη|zone)\s*(\d+)", re.IGNORECASE)


def _zone_names(r: ET.Element) -> tuple[str | None, int | None, str | None, str | None]:
    """(kind 'Zone 30', limit, area name, municipality) from the three regulatedAreaName strings
    ('Ζώνη 30', 'Αγλατζιά', 'Λευκωσία'), whatever their lang says."""
    names = []
    for x in r.iter():
        if _local(x.tag) == "regulatedAreaName":
            v = _first(x, "value")
            if v:
                names.append(v)
    kind = kmh = None
    rest = []
    for n in names:
        m = ZONE_KIND.search(n)
        if m and kmh is None:
            kmh = int(m.group(2))
            kind = f"Zone {kmh}"
        else:
            rest.append(n)
    area = rest[0] if rest else None
    if area:  # one entrance repeats its name ('ΠαλλουριώτισσαΠαλλουριώτισσα')
        half = len(area) // 2
        if len(area) % 2 == 0 and area[:half] == area[half:]:
            area = area[:half]
    return kind, kmh, area, rest[1] if len(rest) > 1 else None


def parse_zones(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "regulation"):
        ident = _first(r, "identifier")
        kind, kmh, area, town = _zone_names(r)
        ring = fix_order(_points(r))
        if not ident or len(ring) < 4:
            continue
        coords = [[round(x, 6), round(y, 6)] for x, y, _ in ring]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        out.append({"id": f"zone-{ident}", "n": ident, "kind": kind or "Slow zone", "kmh": kmh,
                    "name": latin(area) or "Unnamed zone", "name_el": area, "town": latin(town),
                    "status": (_first(r, "regulationStatus") or "unknown").lower(),
                    "ring": coords})
    return out


def parse_zone_entrances(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "regulation"):
        ident = _first(r, "identifier")
        kind, kmh, area, town = _zone_names(r)
        pts = fix_order(_points(r))
        if not ident or not pts:
            continue
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        out.append({"id": f"zentry-{ident}", "n": ident, "kind": kind or "Slow zone", "kmh": kmh,
                    "zone_name": latin(area), "town": latin(town),
                    "status": (_first(r, "regulationStatus") or "unknown").lower(),
                    "lon": round(lon, 6), "lat": round(lat, 6)})
    return out


JUNCTION_CODE = re.compile(r"\(([A-Z]{3}-\d{2,4})[^)]*\)")


def junction_streets(text: str | None) -> list[str]:
    """The streets a junction's comment names: 'GR.DHIGENIS / SEVERI' -> ['Gr.Dhigenis', 'Severi']."""
    if not text:
        return []
    t = JUNCTION_CODE.sub("", text)
    parts = [p.strip(" .-") for p in re.split(r"\s*/\s*|\s*&\s*", t)]
    return [p.title() if p.isupper() else p for p in parts if len(p) >= 3]


def parse_signals(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "managementElement"):
        ident = _first(r, "id")
        pts = fix_order(_points(r))
        if ident is None or not pts:
            continue
        comment = _first(r, "value")
        org = _first(r, "organisationName")
        code = JUNCTION_CODE.search(comment or "")
        lon, lat, _ = pts[0]
        out.append({"id": f"signal-{ident}", "n": ident, "junction": (comment or "").strip() or None,
                    "streets": junction_streets(comment), "code": code.group(1) if code else None,
                    "operator": latin(org) if org else None, "operator_el": org,
                    "lon": round(lon, 6), "lat": round(lat, 6)})
    return out


CROSSING = re.compile(r"^\s*(?P<street>[^(]+?)\s*(?:\((?P<near>[^)]*)\))?\s*$")


def parse_crossings(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "trafficElement"):
        ident = _first(r, "id")
        pts = fix_order(_points(r))
        if ident is None or not pts:
            continue
        comment = smart_title((_first(r, "value") or "").strip()) or None
        m = CROSSING.match(comment or "")
        street = m.group("street").strip() if m and comment else None
        near = ((m.group("near") or "").strip() or None) if m else None
        if street and not near:  # 'Lemesou / K. Palama', 'Makariou III - Tech. School'
            parts = re.split(r"\s+[-/]\s+|\s*/\s*", street, maxsplit=1)
            if len(parts) == 2:
                street, near = parts[0].strip(), parts[1].strip()
        lon, lat, _ = pts[0]
        out.append({"id": f"xing-{ident}", "n": ident, "name": comment or "Pedestrian crossing",
                    "street": street, "near": near, "lon": round(lon, 6), "lat": round(lat, 6)})
    return out


def _lines(r: ET.Element) -> list[list[list[float]]]:
    lines = []
    for pl in r.iter():
        if _local(pl.tag) != "polyline":
            continue
        pts = fix_order(_points(pl))
        coords = [[round(x, 6), round(y, 6)] for x, y, _ in pts]
        coords = [c for k, c in enumerate(coords) if k == 0 or c != coords[k - 1]]
        if len(coords) >= 2:
            lines.append(coords)
    return lines


def parse_cycleways(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "linearElement"):
        ident = _first(r, "id")
        lines = _lines(r)
        if ident is None or not lines:
            continue
        names = _lang_values(r, "name")
        el = names.get("el") or next(iter(names.values()), None)
        out.append({"id": f"cycle-{ident}", "n": ident, "name": cycle_name(el), "name_el": hyphens(el),
                    "lines": lines})
    return out


def parse_bus_lanes(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "linearElement"):
        ident = _first(r, "id")
        lines = _lines(r)
        if ident is None or not lines:
            continue
        names = _lang_values(r, "name")
        status = _lang_values(r, "status")
        en = (status.get("en") or "").strip().lower()
        out.append({"id": f"buslane-{ident}", "n": ident,
                    "name": english(names.get("en")) or latin(names.get("el")) or "Bus lane",
                    "name_el": hyphens(names.get("el")),
                    "status": "active" if en == "active" else "future" if en == "future" else en or "unknown",
                    "lines": lines})
    return out


CAMERA = re.compile(r"^\s*(?P<code>\d{3,6})\s*[-–]\s*(?P<name>.+?)\s*$")
BOUND = {"NB": "northbound", "SB": "southbound", "EB": "eastbound", "WB": "westbound"}


def parse_cameras(xml_text: str) -> list[dict]:
    out = []
    for r in _records(ET.fromstring(xml_text), "trafficEquipment"):
        ident = _first(r, "identifier")
        pts = fix_order(_points(r))
        if ident is None or not pts:
            continue
        names = _lang_values(r, "equipmentName")
        en = (names.get("en") or "").replace("NULL", "").strip()
        el = (names.get("el") or "").replace("NULL", "").strip()
        m = CAMERA.match(en) or CAMERA.match(el)
        name = m.group("name") if m and CAMERA.match(en) else en or (m.group("name") if m else "Camera")
        where = re.split(r"\s+@\s+|\s+and\s+", name, maxsplit=1)
        bound = next((BOUND[w] for w in re.findall(r"\b([NSEW]B)\b", name)), None)
        street = re.sub(r"\s+\b[NSEW]B\b", "", where[0]).strip() if where else None
        address = _lang_values(r, "addressLineText")
        unit = re.search(r"\s+(FC\d+)$", name)
        if unit:
            name = name[:unit.start()]
        where = re.split(r"\s+@\s+|\s+and\s+", name, maxsplit=1)
        street = re.sub(r"\s+\b[NSEW]B\b", "", where[0]).strip() if where else None
        lon, lat, dec = pts[0]
        out.append({"id": f"ecam-{ident}", "n": ident, "code": m.group("code") if m else None,
                    "name": name, "name_el": hyphens(CAMERA.sub(r"\g<name>", el)) if el else None,
                    "street": street, "at": where[1].strip() if len(where) > 1 else None,
                    "direction": bound, "type": _first(r, "equipmentType"),
                    "unit": unit.group(1) if unit else None,
                    "town": "Nicosia" if (address.get("en") or "").lower() in ("lefkosia", "nicosia")
                    else address.get("en") or latin(address.get("el")),
                    # A position written to two decimals is only good to about a kilometre.
                    "precise": dec >= 4, "lon": round(lon, 6), "lat": round(lat, 6)})
    return out


PARSERS = {
    "speed_limits": parse_speed_limits, "zones": parse_zones, "zone_entrances": parse_zone_entrances,
    "signals": parse_signals, "crossings": parse_crossings, "cycleways": parse_cycleways,
    "bus_lanes": parse_bus_lanes, "cameras": parse_cameras,
}


def _where(rec: dict) -> bool:
    """Whether a record lies in Greater Nicosia (a line or area that enters it does)."""
    if "lon" in rec:
        return in_area(rec["lon"], rec["lat"])
    if "ring" in rec:
        return _touches_area(rec["ring"])
    return any(_touches_area(line) for line in rec.get("lines", []))


# -- the inventory ------------------------------------------------------------------------------


@dataclass
class Inventory:
    """One feed as the twin holds it: Greater Nicosia's records, how many the island has, and
    how old it is."""
    key: str
    records: list[dict] = field(default_factory=list)
    island: int = 0
    published: str | None = None  # when the file was written (its publicationTime)
    updated: str | None = None  # when the publisher last changed the dataset (the catalogue)
    read_at: float = 0.0  # when the twin last read it (its fetch, or the copy on disk)
    copy: str = "none"  # live | saved | none
    error: str | None = None

    def public(self) -> dict:
        f = FEEDS[self.key]
        return {"key": self.key, "title": f.title, "count": len(self.records), "island": self.island,
                "published": self.published, "updated": self.updated, "copy": self.copy,
                "read_at": datetime.fromtimestamp(self.read_at, timezone.utc).isoformat(timespec="seconds")
                if self.read_at else None,
                "error": self.error, "publisher": f.publisher, "url": DATASET_PAGE + f.dataset}


class Feeds:
    """Every street inventory, each fetched at most once a day for everyone."""

    def __init__(self, fetch_text, cache_dir=None, ttl_s: float = INVENTORY_S):
        self.fetch_text = fetch_text  # fetch_text(url) -> str
        self.cache_dir = (cache_dir or CACHE_DIR) / "streets"
        self.ttl_s = ttl_s
        self._inv: dict[str, Inventory] = {}
        self._checked: dict[str, float] = {}
        self._locks = {k: threading.Lock() for k in FEEDS}
        self._catalogue: tuple[float, dict[str, str]] | None = None
        self._cat_lock = threading.Lock()
        self.version = 0  # goes up whenever an inventory is read anew

    def _path(self, key: str):
        return self.cache_dir / f"{FEEDS[key].path}.xml"

    def get(self, key: str) -> Inventory:
        """The feed's inventory, read from disk or fetched when a day old."""
        hit = self._inv.get(key)
        if hit is not None and time.time() - self._checked.get(key, 0) < self.ttl_s:
            return hit
        with self._locks[key]:
            hit = self._inv.get(key)
            if hit is not None and time.time() - self._checked.get(key, 0) < self.ttl_s:
                return hit
            inv = self._read(key, hit)
            self._inv[key] = inv
            self._checked[key] = time.time() if inv.copy != "none" else time.time() - self.ttl_s + 600
            self.version += 1
            return inv

    def peek(self, key: str) -> Inventory | None:
        """The inventory if it has been read, without reading it."""
        return self._inv.get(key)

    def _read(self, key: str, old: Inventory | None) -> Inventory:
        path = self._path(key)
        fresh = path.exists() and time.time() - path.stat().st_mtime < self.ttl_s
        text, copy, error = None, "none", None
        if fresh:
            text, copy = path.read_text(encoding="utf-8"), "saved"
        else:
            try:
                text = self.fetch_text(BASE + FEEDS[key].path + "/")
                ET.fromstring(text)  # never keep a broken copy
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                copy = "live"
            except Exception as e:  # noqa: BLE001 - yesterday's copy is fine for an inventory
                error = f"{FEEDS[key].title} couldn't be read: {str(e)[:160]}"
                log.warning("%s", error)
                if path.exists():
                    text, copy = path.read_text(encoding="utf-8"), "saved"
                    path.touch()
                elif old is not None:
                    old.error = error
                    return old
        inv = Inventory(key, copy=copy, error=error)
        if text is None:
            return inv
        try:
            root = ET.fromstring(text)
            records = PARSERS[key](text)
        except Exception as e:  # noqa: BLE001 - a broken file reads as nothing, and says so
            inv.error = f"{FEEDS[key].title} couldn't be parsed: {str(e)[:160]}"
            return old or inv
        inv.island = len(records)
        inv.records = [r for r in records if _where(r)]
        inv.published = publication_time(root)
        inv.read_at = path.stat().st_mtime if path.exists() else time.time()
        inv.updated = self.updated().get(FEEDS[key].dataset)
        return inv

    def updated(self) -> dict[str, str]:
        """Dataset name -> the day the publisher last changed it, from the catalogue (one request
        a day for every feed; nothing when the catalogue doesn't answer)."""
        hit = self._catalogue
        if hit is not None and time.time() - hit[0] < self.ttl_s:
            return hit[1]
        with self._cat_lock:
            hit = self._catalogue
            if hit is not None and time.time() - hit[0] < self.ttl_s:
                return hit[1]
            path = self.cache_dir / "catalogue.json"
            out: dict[str, str] = {}
            try:
                if path.exists() and time.time() - path.stat().st_mtime < self.ttl_s:
                    out = json.loads(path.read_text(encoding="utf-8"))
                else:
                    data = json.loads(self.fetch_text(f"{CATALOGUE}?rows=1000"))
                    wanted = {f.dataset for f in FEEDS.values()}
                    out = {p["name"]: (p.get("metadata_modified") or "")[:10]
                           for p in data.get("result", {}).get("results", []) if p.get("name") in wanted}
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(out), encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - the files' own dates still say something
                log.info("street inventories' catalogue unavailable: %s", e)
                if path.exists():
                    try:
                        out = json.loads(path.read_text(encoding="utf-8"))
                    except ValueError:
                        out = {}
            self._catalogue = (time.time(), out)
            return out

    def status(self) -> dict[str, dict]:
        """Every feed's state without reading any (for /api/sources and the Layers sheet)."""
        return {k: (inv.public() if (inv := self._inv.get(k)) else
                    {"key": k, "title": FEEDS[k].title, "count": None, "copy": "none"}) for k in FEEDS}


__all__ = ["Feeds", "FEEDS", "Inventory", "AREA", "in_area", "latin", "LICENCE", "DATASET_PAGE"]

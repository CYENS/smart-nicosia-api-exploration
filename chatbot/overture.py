"""Places from Overture Maps: the twin's second places index, each with a point at its door.

The OpenStreetMap index (chatbot/geo.py) lists the kinds of place people tour: museums, cafés,
pharmacies, parks. Overture's places theme adds the businesses and services it lacks, such as
dance studios, tailors, clinics, clubs and offices: about 9,500 in greater Nicosia, nearly all
with an address and a postcode, most with a phone, many with a website.

The index is cut from Overture's latest monthly release with DuckDB, straight from its public
bucket: only the rows whose bbox falls in NICOSIA_BBOX, which skips almost every row group (a
minute or two). It is checked at most monthly, in the background, and kept in
data/chatbot_cache/overture_places.json with its release name. A refresh that fails keeps the
last good copy, and no question ever waits for Overture.

What is kept (curate), from a look at release 2026-11-18.0 (17,801 rows in the bbox):
- Confidence of 0.6 or more. Overture's confidence says how sure it is that a place exists, not
  where it stands. Against the OSM index, the share of Overture places OSM also has stays flat
  at 5 to 10% up to 0.65 and climbs from 0.7 (14%) to 47% above 0.95, so below 0.7 confidence
  says little; a hand check of 0.6 to 0.7 still found mostly real, current places the twin lacks
  (Level Up Dance Academy 0.69, Caliente Dance Studio 0.67, Altius Performing Arts 0.61, a
  pharmacy), and below 0.6 more events, pages and repeats ("Santa Run", a "disco barda"). 0.6
  keeps 10,516 rows before the rules below, 0.7 would keep 8,016.
- Open, or of unknown status: operating_status is empty for 99.5% of rows.
- Not on a placeholder point. Some sources put a place they can't find at a town's centre: one
  point holds 448 places on 369 different streets. A point shared by 5 places or more on 3
  streets or more is not a door, so all its places are left out.
- One place per source record. Overture repeats about 700 Meta records under two ids, often
  kilometres apart (Froutaria Ithaki, at 28 Grigori Afxentiou, twice 700 m apart); the copy its
  source updated last stands.
- One place per name within 50 m: the surest stays, taking the other's phone or website if it
  lacks them.
- And against the OSM index, when a search runs: an Overture place with the name of an OSM
  place within 50 m is that place, and OSM wins (`twins`).

Addresses are kept as given, so north Nicosia's read the Turkish way.
"""

from __future__ import annotations

import json
import logging
import re
import struct
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from chatbot.geo import CATEGORIES, haversine_m, loose, norm, text_score
from chatbot.settings import CACHE_DIR, NICOSIA_BBOX

log = logging.getLogger(__name__)

BUCKET = "s3://overturemaps-us-west-2/release"
S3_REGION = "us-west-2"
ATTRIBUTION = "https://docs.overturemaps.org/attribution/"
MIN_CONFIDENCE = 0.6  # see the module's docstring for why
SAME_PLACE_M = 50  # places of one name this close together are one place
PLACEHOLDER_PLACES = 5  # a point shared by this many places...
PLACEHOLDER_STREETS = 3  # ...on this many streets is a placeholder, not a door
CHECK_EVERY_S = 30 * 86400  # a new release comes out monthly
RETRY_AFTER_S = 86400  # after a failed refresh, try again the next day
WATCH_EVERY_S = 6 * 3600
OV_ID = re.compile(r"^ov-[0-9a-f]{12}$", re.IGNORECASE)

# Overture's taxonomy onto the twin's categories (chatbot/geo.py), where they meet. A place takes
# the first of its hierarchy, most specific first, that is listed here.
TAXONOMY = {
    "museum": "museum", "art_gallery": "gallery", "monument": "monument",
    "memorial_site": "monument", "historic_site": "historic_site",
    "place_of_worship": "place_of_worship", "theatre_venue": "theatre",
    "movie_theater": "theatre", "performing_arts_venue": "theatre", "cafe": "cafe",
    "coffee_shop": "cafe", "ice_cream_shop": "ice_cream", "gelato_shop": "ice_cream",
    "fast_food_restaurant": "fast_food", "restaurant": "restaurant", "bar": "bar",
    "beer_garden": "bar", "pharmacy": "pharmacy", "hospital": "hospital",
    "public_toilet": "toilets", "atm": "atm", "bank_or_credit_union": "atm",
    "playground": "playground", "park": "park", "garden": "park", "hotel": "hotel",
    "hostel": "hotel", "guest_house": "hotel", "bed_and_breakfast": "hotel", "motel": "hotel",
    "supermarket": "supermarket", "grocery_store": "supermarket", "parking": "parking_lot",
    "ev_charging_station": "ev_charging", "bike_rental": "bicycle_rental",
    "bus_station": "bus_station",
}
assert set(TAXONOMY.values()) <= set(CATEGORIES)
DATASETS = {"meta": "Meta"}  # how a source dataset is named for a person; the rest as given

# The columns the twin keeps, from the schema of 2026 (no `categories` any more: `taxonomy`).
COLUMNS = """id, names.primary AS name, taxonomy.primary AS kind, taxonomy.hierarchy AS hierarchy,
    basic_category, operating_status, confidence, geometry, bbox,
    addresses[1].freeform AS address, addresses[1].locality AS locality,
    addresses[1].postcode AS postcode, addresses[1].country AS country,
    websites[1] AS website, socials[1] AS social, phones[1] AS phone,
    list_filter(sources, lambda s: s.dataset <> 'Overture') AS sources"""


# -- reading a release --------------------------------------------------------------------------


def release_glob(release: str) -> str:
    return f"{BUCKET}/{release}/theme=places/type=place/*"


def extraction_sql(source: str) -> str:
    """The query that cuts Nicosia out of `source` (a release's files, or one local file). The
    filter is on bbox.xmin and bbox.ymin, which Overture sorts its row groups by, so DuckDB reads
    only the few that can hold Nicosia."""
    path = source.replace("'", "''")
    return (f"SELECT {COLUMNS}\nFROM read_parquet('{path}')\n"
            "WHERE bbox.xmin BETWEEN $lon_min AND $lon_max "
            "AND bbox.ymin BETWEEN $lat_min AND $lat_max")


def _connect(remote: bool):
    import duckdb

    con = duckdb.connect()
    if remote:  # INSTALL is a no-op once the extension is on the machine
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"SET s3_region='{S3_REGION}'")
    return con


def extract(source: str, bbox=NICOSIA_BBOX) -> list[dict]:
    """Every row of `source` inside `bbox`, as dicts of COLUMNS."""
    con = _connect(source.startswith("s3://"))
    try:
        lon_min, lat_min, lon_max, lat_max = bbox
        cur = con.execute(extraction_sql(source), {"lon_min": lon_min, "lon_max": lon_max,
                                                   "lat_min": lat_min, "lat_max": lat_max})
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def latest_release() -> str:
    """The newest release with a places theme, by listing the bucket ('2026-11-18.0')."""
    con = _connect(True)
    try:
        rows = con.execute(
            "SELECT DISTINCT regexp_extract(file, 'release/([^/]+)/', 1) FROM glob(?)",
            [release_glob("*")]).fetchall()
    finally:
        con.close()
    names = [r[0] for r in rows if r[0] and re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$", r[0])]
    if not names:
        raise ValueError("no release of the places theme found")
    return max(names, key=lambda n: (n.split(".")[0], int(n.split(".")[1])))


def point_of(geometry: object, bbox: dict | None = None) -> tuple[float, float] | None:
    """(lat, lon) of a WKB point; else the middle of its bbox (which Overture stores as floats,
    so to about half a metre)."""
    if isinstance(geometry, (bytes, bytearray, memoryview)):
        g = bytes(geometry)
        if len(g) >= 21:
            order = "<" if g[0] == 1 else ">"
            (kind,) = struct.unpack(order + "I", g[1:5])
            if (kind & 0xFFFF) % 1000 == 1:  # a point, in plain, ISO or EWKB form
                x, y = struct.unpack(order + "dd", g[-16:] if kind & 0x20000000 else g[5:21])
                return y, x
    if bbox and bbox.get("xmin") is not None:
        return (bbox["ymin"] + bbox["ymax"]) / 2, (bbox["xmin"] + bbox["xmax"]) / 2
    return None


# -- the quality rules -------------------------------------------------------------------------


def street_key(address: str | None) -> str:
    """An address without numbers or punctuation, to count the streets at one point."""
    return re.sub(r"[\d\W_]+", " ", norm(address)).strip()


def same_name(a: str | None, b: str | None) -> bool:
    """Whether two names name one place: equal once spelling is set aside, or one inside the
    other ('Ithaki' and 'Ithaki Venue') when the shorter is long enough to mean something."""
    la, lb = loose(a), loose(b)
    if not la or not lb:
        return False
    return la == lb or (min(len(la), len(lb)) >= 5 and (la in lb or lb in la))


def category_of(kind: str | None, hierarchy: Iterable[str] | None) -> str | None:
    for k in [*(reversed(list(hierarchy or []))), kind]:
        if k in TAXONOMY:
            return TAXONOMY[k]
    return None


def short_id(gers: str) -> str:
    """'d5564327-fe43-4e04-aafe-559207dd70e2' -> 'ov-d5564327fe43'. Overture's ids are stable
    from one release to the next, and so are these."""
    return "ov-" + re.sub(r"[^0-9a-f]", "", gers.lower())[:12]


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return int(lat * 1000), int(lon * 1000)  # about 110 by 90 m


def _around(grid: dict, lat: float, lon: float) -> Iterable[dict]:
    y, x = _cell(lat, lon)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            yield from grid.get((y + dy, x + dx), ())


def _url(url: str | None) -> str | None:
    """A website without its tracking parameters."""
    if not url:
        return None
    url = re.sub(r"([?&])(utm_[a-z]+|fbclid|igshid)=[^&#]*", r"\1", url.strip())
    return re.sub(r"[?&]+$", "", url.replace("?&", "?"))


def _place(r: dict, lat: float, lon: float, name: str) -> dict:
    """A row as the copy on disk keeps it, compactly: the id is made from `gers` when loaded,
    `kinds` holds the taxonomy above `kind`, and `licence` is there only when it isn't
    Overture's own CDLA-Permissive-2.0."""
    sources = r.get("sources") or []
    updated = max((str(s.get("update_time") or "")[:10] for s in sources), default="") or None
    licences = sorted({s["license"] for s in sources if s.get("license")} - {"CDLA-Permissive-2.0"})
    kind = r.get("kind") or r.get("basic_category")
    p = {"gers": r["id"], "name": name, "kind": kind,
         "kinds": "/".join(k for k in r.get("hierarchy") or [] if k != kind) or None,
         "category": category_of(r.get("kind"), r.get("hierarchy")),
         "lat": round(lat, 6), "lon": round(lon, 6),
         "address": (r.get("address") or "").strip() or None, "locality": r.get("locality"),
         "postcode": r.get("postcode"), "phone": r.get("phone"),
         "website": _url(r.get("website") or r.get("social")),
         "confidence": round(float(r.get("confidence") or 0), 2),
         "via": ", ".join(sorted({s["dataset"] for s in sources if s.get("dataset")})) or None,
         "licence": ", ".join(licences) or None, "updated": updated}
    return {k: v for k, v in p.items() if v is not None}


def curate(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """The rows worth showing, as compact places, and how many each rule left out."""
    dropped: Counter = Counter()
    items = []
    for r in rows:
        pt = point_of(r.get("geometry"), r.get("bbox"))
        name = re.sub(r"\s+", " ", str(r.get("name") or "")).strip()
        if pt is None or not name or not r.get("id"):
            dropped["no name or point"] += 1
            continue
        items.append((r, pt, name))
    # Placeholders and repeated records are judged on every row, whatever its confidence.
    at: dict[tuple, list[dict]] = defaultdict(list)
    for r, (lat, lon), _ in items:
        at[(round(lat, 5), round(lon, 5))].append(r)
    placeholders = {k for k, rs in at.items() if len(rs) >= PLACEHOLDER_PLACES
                    and len({street_key(r.get("address")) for r in rs} - {""}) >= PLACEHOLDER_STREETS}
    newest: dict[tuple, tuple] = {}
    for r, _, _ in items:
        for s in r.get("sources") or []:
            if s.get("record_id"):
                key = (s.get("dataset"), s["record_id"])
                rank = (str(s.get("update_time") or ""), r.get("confidence") or 0, r["id"])
                if key not in newest or rank > newest[key]:
                    newest[key] = rank
    kept = []
    for r, (lat, lon), name in items:
        older = any(newest[(s.get("dataset"), s["record_id"])][2] != r["id"]
                    for s in r.get("sources") or [] if s.get("record_id"))
        why = ("on a placeholder point" if (round(lat, 5), round(lon, 5)) in placeholders else
               "an older copy of a record" if older else
               "closed" if (r.get("operating_status") or "open") != "open" else
               "below the confidence threshold" if (r.get("confidence") or 0) < MIN_CONFIDENCE else
               None)
        if why:
            dropped[why] += 1
        else:
            kept.append(_place(r, lat, lon, name))
    kept.sort(key=lambda p: (-p["confidence"], p["gers"]))
    out: list[dict] = []
    grid: dict[tuple, list[dict]] = defaultdict(list)
    ids: set[str] = set()
    for p in kept:
        p["id"] = short_id(p["gers"])
        twin = next((q for q in _around(grid, p["lat"], p["lon"]) if same_name(p["name"], q["name"])
                     and haversine_m(p["lat"], p["lon"], q["lat"], q["lon"]) <= SAME_PLACE_M), None)
        if twin is not None:
            for k in ("phone", "website", "address", "postcode", "locality"):
                if k in p and k not in twin:
                    twin[k] = p[k]
            dropped["the same place twice"] += 1
            continue
        if p["id"] in ids:  # two ids alike in their first 12 digits: never seen, but kept apart
            dropped["an id clash"] += 1
            continue
        ids.add(p["id"])
        grid[_cell(p["lat"], p["lon"])].append(p)
        out.append(p)
    return out, dict(dropped)


# -- for people and the model ------------------------------------------------------------------


def kind_label(p: dict) -> str:
    return (p.get("kind") or "place").replace("_", " ").capitalize()


def full_address(p: dict) -> str | None:
    """The address as Overture gives it, with its postcode and town: 'Nikiforou Foka 33, 1016
    Λευκωσία'."""
    tail = " ".join(x for x in (p.get("postcode"), p.get("locality")) if x)
    if p.get("address") and tail and norm(tail) not in norm(p["address"]):
        return f"{p['address']}, {tail}"
    return p.get("address") or tail or None


def confidence_band(c: float) -> str:
    return "high" if c >= 0.9 else "good" if c >= 0.75 else "fair"


def confidence_text(c: float) -> str:
    """For the inspector: a word and Overture's score to two places, never more."""
    return {"high": "High", "good": "Good", "fair": "Fair, worth checking before you go"}[
        confidence_band(c)] + f" ({c:.2f} of 1)"


def sources_text(p: dict) -> str:
    """Where Overture took the place from: 'Meta', 'Foursquare'."""
    return ", ".join(DATASETS.get(d, d) for d in (p.get("via") or "").split(", ") if d) or "Overture"


# -- the index ---------------------------------------------------------------------------------


class Overture:
    def __init__(self, path: Path | None = None):
        self.path = path or CACHE_DIR / "overture_places.json"
        self.release: str | None = None
        self.fetched_at = 0.0  # when the copy was cut
        self.checked_at = 0.0  # when the bucket was last asked for a newer release
        self.dropped: dict[str, int] = {}
        self.error: str | None = None  # why the last refresh failed, while it stands
        self._failed_at = 0.0
        self._places: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._loose: list[tuple[str, str]] = []
        self._busy = threading.Lock()
        self._twins: tuple[tuple, dict[str, str]] | None = None
        self.search_cache: tuple[tuple, list[dict]] | None = None  # Ctrl K's items, built once
        self._load()

    # -- the copy on disk ------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._use(data["places"], data["release"], data["fetched_at"],
                      data.get("checked_at") or data["fetched_at"], data.get("dropped") or {})
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _use(self, places, release, fetched_at, checked_at, dropped) -> None:
        for p in places:
            p.setdefault("id", short_id(p["gers"]))
        by_id = {p["id"]: p for p in places}
        keys = [(loose(p["name"]), loose(p["name"], eta="e")) for p in places]
        self._places, self._by_id, self._loose = places, by_id, keys
        self.release, self.fetched_at, self.checked_at = release, fetched_at, checked_at
        self.dropped = dropped

    def _save(self) -> None:
        data = {"release": self.release, "fetched_at": self.fetched_at,
                "checked_at": self.checked_at, "dropped": self.dropped,
                "licence": "CDLA-Permissive-2.0; rows from Foursquare Apache-2.0, from "
                           "AllThePlaces CC0-1.0. Overture Maps Foundation, " + ATTRIBUTION,
                "places": [{k: v for k, v in p.items() if k != "id"} for p in self._places]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(self.path)

    # -- refreshing --------------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return bool(self._places)

    @property
    def release_date(self) -> datetime | None:
        try:
            return datetime.strptime((self.release or "").split(".")[0], "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None

    def refresh_if_needed(self) -> None:
        """Cut a new copy if a newer release is out, asking at most monthly (blocking; a minute
        or two when there is a new release). A failure keeps the last good copy."""
        now = time.time()
        if self._places and now - self.checked_at < CHECK_EVERY_S:
            return
        if self.error and now - self._failed_at < RETRY_AFTER_S:
            return
        if not self._busy.acquire(blocking=False):
            return
        try:
            release = latest_release()
            if release != self.release or not self._places:
                started = time.monotonic()
                places, dropped = curate(extract(release_glob(release)))
                if not places:
                    raise ValueError(f"release {release} has no places in Nicosia")
                self._use(places, release, time.time(), time.time(), dropped)
                log.info("overture places: %d from release %s in %.0f s (left out: %s)",
                         len(places), release, time.monotonic() - started, dropped)
            else:
                self.checked_at = time.time()
            self.error = None
            self._save()
        except Exception as e:  # noqa: BLE001 - S3, DuckDB or a new schema: keep the last copy
            self.error = str(e).split("\n")[0][:200] or type(e).__name__
            self._failed_at = time.time()
            log.warning("overture places refresh failed (%s): %s", "keeping release "
                        f"{self.release}" if self._places else "no copy yet", self.error)
        finally:
            self._busy.release()

    def watch(self) -> None:
        """Keep the copy current while the server runs (a daemon thread's target)."""
        while True:
            self.refresh_if_needed()
            time.sleep(WATCH_EVERY_S)

    # -- reading ---------------------------------------------------------------------------------

    def all(self) -> list[dict]:
        return self._places

    def get(self, pid: str) -> dict | None:
        return self._by_id.get((pid or "").strip().lower())

    def twins(self, osm: list[dict]) -> dict[str, str]:
        """Overture places that are an OSM place, by id: the same name within SAME_PLACE_M. OSM
        wins, so these are left out of searches."""
        key = (id(osm), len(osm), id(self._places))
        if self._twins and self._twins[0] == key:
            return self._twins[1]
        grid: dict[tuple, list[dict]] = defaultdict(list)
        for p in osm:
            if p.get("name"):
                grid[_cell(p["lat"], p["lon"])].append(p)
        out = {}
        for p in self._places:
            for q in _around(grid, p["lat"], p["lon"]):
                if (same_name(p["name"], q["name"]) or same_name(p["name"], q.get("name_local"))) \
                        and haversine_m(p["lat"], p["lon"], q["lat"], q["lon"]) <= SAME_PLACE_M:
                    out[p["id"]] = q["id"]
                    break
        self._twins = (key, out)
        return out

    def named(self, text: str, near: tuple[float, float] | None = None) -> list[dict]:
        """Places called exactly `text` (or with that id), the nearest to `near` first, else the
        surest."""
        if OV_ID.match((text or "").strip()):
            p = self.get(text)
            return [p] if p else []
        key = norm(text)
        same = [p for p in self._places if key and norm(p["name"]) == key]
        if near:
            return sorted(same, key=lambda p: haversine_m(near[0], near[1], p["lat"], p["lon"]))
        return sorted(same, key=lambda p: -p["confidence"])

    def named_loosely(self, name: str) -> list[dict]:
        """Places whose name holds `name` or is held by it, whatever the script or spelling."""
        q = loose(name)
        if not q:
            return []
        out = []
        for p, (a, b) in zip(self._places, self._loose):
            if q == a or q == b or (len(q) >= 3 and (q in a or q in b)) \
                    or (len(a) >= 5 and a in q):
                out.append(p)
        return out

    def find(self, category: str | None = None, query: str | None = None,
             skip: dict | set | None = None) -> list[tuple[int, float, dict]]:
        """(score, confidence, row) for every place of the category whose name, kind or address
        matches the query, leaving out the ids in `skip` (OSM's twins)."""
        out = []
        for p in self._places:
            if category and p.get("category") != category:
                continue
            if skip and p["id"] in skip:
                continue
            s = text_score(query, [p["name"]],
                           [kind_label(p), p.get("kinds"), p.get("address"), p.get("locality")])
            if s:
                out.append((s, p["confidence"], row_of(p)))
        return out


def row_of(p: dict) -> dict:
    """A place as find_places gives it to the model and the page."""
    row = {"id": p["id"], "category": p.get("category") or p.get("kind") or "place",
           "name": p["name"], "kind": kind_label(p), "lat": p["lat"], "lon": p["lon"],
           "address": full_address(p), "phone": p.get("phone"), "website": p.get("website"),
           "confidence": confidence_band(p["confidence"]), "from": "Overture Maps"}
    return {k: v for k, v in row.items() if v is not None}


# -- the twin's entity kind: an Overture place in the inspector, search and chips ---------------

from chatbot import entities as E  # down here: entities never imports this module

E.KIND_LABELS.setdefault("overture_place", "Place")


def _overture_of(ents: E.Entities) -> Overture | None:
    ov = getattr(ents.places, "overture", None) if ents.places is not None else None
    return ov if isinstance(ov, Overture) and ov.ready else None


def build_overture_place(ents: E.Entities, eid: str, near) -> dict | None:
    ov = _overture_of(ents)
    p = ov.get(eid) if ov is not None else None
    if p is None:
        return None
    kind = kind_label(p)
    address = full_address(p)
    attributes = [E.attr("Kind", kind), E.attr("Address", address), E.attr("Phone", p.get("phone")),
                  E.attr("Website", p.get("website")),
                  E.attr("Confidence", confidence_text(p["confidence"])),
                  E.attr("Last confirmed by its source", p.get("updated")),
                  E.attr("Overture id", p["gers"])]
    relations = []
    osm_id = ov.twins(ents.places.all()).get(p["id"])
    osm = ents.places.by_id(osm_id) if osm_id else None
    if osm is not None:
        relations.append({"relation": "place", "label": "On OpenStreetMap", "id": osm["id"],
                          "kind": "place", "name": osm.get("name"), "lon": osm["lon"],
                          "lat": osm["lat"]})
    if (street := E.street_of(p.get("address"))):
        relations.append({"relation": "street", "label": "Street", "name": street})
    b = ents.building_at(p["lat"], p["lon"])
    if b:
        relations.append({"relation": "building", "label": "In the DLS building",
                          "id": f"B{b['id']}", "kind": "building_3d",
                          "name": b["properties"].get("t") or "Building",
                          "detail": f"{b['properties']['h']} m"})
    relations += E.nearest_stop(ents, p["lat"], p["lon"])
    relations += ents.nearby_registers(p["lat"], p["lon"], max_m=150, limit=2)
    relations += ents.nearest_devices(p["lat"], p["lon"], max_m=400)
    licence = "CDLA-Permissive-2.0" + (f", {p['licence']}" if p.get("licence") else "")
    return E.new_entity(
        p["id"], "overture_place", p["name"], kind_label="Place",
        subtitle=" · ".join(x for x in (kind, address) if x),
        geometry=E.point_geom(p["lon"], p["lat"]), center=[p["lon"], p["lat"]],
        attributes=attributes, relations=relations,
        sources=[{"source": "overture", "name": "Overture Maps",
                  "detail": f"release {ov.release}, from {sources_text(p)}; {licence}",
                  "url": ATTRIBUTION, "as_of": p.get("updated")}])


def search_overture(ents: E.Entities) -> list[dict]:
    """Overture's places for Ctrl K, less those OSM has too (built once per copy)."""
    ov = _overture_of(ents)
    if ov is None:
        return []
    osm = ents.places.all()
    twins = ov.twins(osm)
    key = (id(ov.all()), id(twins))
    if ov.search_cache and ov.search_cache[0] == key:
        return ov.search_cache[1]
    items = [E.index_item(p["id"], "overture_place", p["name"], "Places",
                          " · ".join(x for x in (kind_label(p), p.get("address")) if x),
                          p["lat"], p["lon"], [p["name"], kind_label(p), p.get("address")])
             for p in ov.all() if p["id"] not in twins]
    ov.search_cache = (key, items)
    return items


E.register_kind(E.Kind("overture_place", lambda i: bool(OV_ID.match(i)), build_overture_place))
E.register_search(search_overture)

"""Geocoding duty pharmacies and event venues with official DLS INSPIRE AD_Addresses,
OSM pharmacy places, Overture places, and Nominatim/Photon fallbacks.

Every returned placement declares its match precision cleanly:
- "exact": matched to a verified OSM/Overture POI or door-level address hit
- "street": placed along the official street from DLS INSPIRE AD_Addresses (or geocoder street)
- "area": municipality/town centre fallback when neither door nor street could be resolved
- "unplaced": could not be located inside Nicosia
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from pathlib import Path

from chatbot.geo import haversine_m, in_bbox, loose, loose_match, norm
from chatbot.settings import CACHE_DIR, Settings
from chatbot.web import Throttle, WebError, get_json
from chatbot.webplaces import by_postcode, on_street, precision_of, respell, street_only, streets_apart

log = logging.getLogger(__name__)

DLS_BBOX = (33.24, 35.07, 33.48, 35.23)
DLS_BBOX_STR = "33.24,35.07,33.48,35.23"
DLS_MAPSERVER = "https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE/AD_Addresses/MapServer"
DLS_ADDRESSES_QUERY = f"{DLS_MAPSERVER}/0/query"
DLS_STREETS_QUERY = f"{DLS_MAPSERVER}/4/query"

GEOCODE_VERSION = 1
DLS_TTL_S = 30 * 86400
PLACE_TTL_S = 14 * 86400
SAME_MUNI_MAX_M = 3500
DOOR_NEAR_DLS_M = 1800

PRECISION_LABELS = {
    "exact": "Exact location",
    "street": "On its street (exact number not matched)",
    "area": "Approximate municipality centre",
    "unplaced": "Not placed on the map",
}

# Known Greater Nicosia municipality & suburb centres for disambiguating namesake streets.
MUNICIPALITY_ANCHORS: dict[str, tuple[float, float]] = {
    "λευκωσια": (35.1695, 33.3645),
    "nicosia": (35.1695, 33.3645),
    "lefkosia": (35.1695, 33.3645),
    "στροβολος": (35.1430, 33.3450),
    "strovolos": (35.1430, 33.3450),
    "εγκωμη": (35.1615, 33.3195),
    "egkomi": (35.1615, 33.3195),
    "engomi": (35.1615, 33.3195),
    "αγλαντζια": (35.1500, 33.3920),
    "aglantzia": (35.1500, 33.3920),
    "λακαταμεια": (35.1180, 33.3060),
    "lakatamia": (35.1180, 33.3060),
    "lakatameia": (35.1180, 33.3060),
    "λατσια": (35.1020, 33.3790),
    "latsia": (35.1020, 33.3790),
    "γερι": (35.1045, 33.4180),
    "geri": (35.1045, 33.4180),
    "αγιος δομετιος": (35.1780, 33.3370),
    "agios dometios": (35.1780, 33.3370),
    "τσερι": (35.0730, 33.3220),
    "tseri": (35.0730, 33.3220),
    "ανθουπολη": (35.1140, 33.2890),
    "anthoupoli": (35.1140, 33.2890),
    "παλλουριωτισσα": (35.1765, 33.3820),
    "pallouriotissa": (35.1765, 33.3820),
    "καιμακλι": (35.1845, 33.3785),
    "kaimakli": (35.1845, 33.3785),
}

STREET_PREFIX = re.compile(
    r"^\s*(?:λεωφόρος|λεωφόρο|λεωφ\.?|οδός|οδού|πλατεία|leoforos|leof\.?|avenue|ave\.?|street|str\.?)\s+",
    re.IGNORECASE,
)
TRAIL_PARISH = re.compile(r",\s*(?:ενορία|ενορια|parish)\b.*$", re.IGNORECASE)
TRAIL_NUM = re.compile(r"[\s,]+\d+[A-Za-zΑ-Ωα-ω]?(?:\s*[-&/,]\s*\d+[A-Za-zΑ-Ωα-ω]?)*\s*$")
FORMER_STREET = re.compile(
    r"πρώην\s+(?:λεωφ\.?|λεωφόρος|οδός)?\s*([^,.;()]+)",
    re.IGNORECASE,
)

# DLS INSPIRE/AD_Addresses uses uppercase Romanized Greek (e.g. "DIGENI AKRITA", "28IS OKTOVRIOU").
_DLS_DIGRAPHS = (
    ("ου", "OU"), ("αυ", "AV"), ("ευ", "EV"), ("ει", "I"), ("οι", "I"), ("αι", "AI"),
    ("μπ", "MP"), ("ντ", "NT"), ("γγ", "NG"), ("γκ", "GK"), ("τσ", "TS"), ("τζ", "TZ"),
    ("θ", "TH"), ("χ", "CH"), ("ψ", "PS"),
)
_DLS_CHAR = str.maketrans({
    "α": "A", "β": "V", "γ": "G", "δ": "D", "ε": "E", "ζ": "Z", "η": "I",
    "ι": "I", "κ": "K", "λ": "L", "μ": "M", "ν": "N", "ξ": "X", "ο": "O",
    "π": "P", "ρ": "R", "σ": "S", "ς": "S", "τ": "T", "υ": "Y", "φ": "F",
    "ω": "O",
})


def digits_8(phone: object) -> str:
    d = re.sub(r"\D", "", str(phone or ""))
    return d[-8:] if len(d) >= 8 else ""


def primary_surname(surname: str | None) -> str:
    s = re.sub(r"\([^)]*\)", "", str(surname or "")).strip()
    return s.split()[0] if s else ""


def municipality_anchor(municipality: str | None) -> tuple[float, float] | None:
    key = norm(municipality)
    if not key:
        return None
    if key in MUNICIPALITY_ANCHORS:
        return MUNICIPALITY_ANCHORS[key]
    for k, pt in MUNICIPALITY_ANCHORS.items():
        if k in key or key in k:
            return pt
    return None


def clean_street(address: str | None) -> str:
    s = TRAIL_PARISH.sub("", str(address or "")).strip()
    # Strip trailing house numbers (keeping leading numbers like "28η Οκτωβρίου")
    s = TRAIL_NUM.sub("", s).strip(" ,.-")
    s = STREET_PREFIX.sub("", s).strip(" ,.-")
    # Common CSV typos like "Χρστόφια" -> "Χριστόφια"
    s = re.sub(r"χρστ[οό]φια", "Χριστόφια", s, flags=re.IGNORECASE)
    return s


def to_dls_latin(text: str) -> str:
    """Transliterate a Greek street name into uppercase DLS AD_Addresses Latin form."""
    t = norm(text)
    # Ordinal "28η" / "28ης" -> "28IS"
    t = re.sub(r"\b(\d+)\s*[ηης]{1,2}\b", r"\1IS", t)
    for a, b in _DLS_DIGRAPHS:
        t = t.replace(a, b)
    t = t.translate(_DLS_CHAR).upper()
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def dls_street_patterns(address: str | None, directions: str | None = None) -> list[str]:
    """Safe uppercase ASCII patterns for querying DLS AD_Addresses Table 4."""
    raw_streets = []
    if main := clean_street(address):
        raw_streets.append(main)
    if directions:
        if m := FORMER_STREET.search(directions):
            if alt := clean_street(m.group(1)):
                raw_streets.append(alt)
    patterns: list[str] = []
    for st in raw_streets:
        lat = to_dls_latin(st)
        if len(lat) >= 3:
            patterns.append(lat)
        # Also include a second variant with Y->I for names like Christofia / Argyrou
        alt = lat.replace("Y", "I")
        if alt != lat and len(alt) >= 3:
            patterns.append(alt)
        # If multi-word, add the longest word if >= 5 chars as a looser fallback
        words = [w for w in lat.split() if len(w) >= 5 and w not in ("AGIOU", "AGIAS", "LEOFOROS", "ARCHIEPISKOPOU")]
        if len(lat.split()) > 1 and words:
            patterns.append(max(words, key=len))
    return list(dict.fromkeys(patterns))


class DLSAddressClient:
    """Throttled, disk-cached client for DLS INSPIRE/AD_Addresses MapServer/0 & /4."""

    def __init__(self, settings: Settings, cache_dir: Path | None = None, *, post_json=None):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "tonight"
        self.cache_path = self.dir / "dls_addresses.json"
        self._post_json = post_json or (
            lambda url, body: get_json(url, user_agent=self.s.user_agent, data=body, timeout=25)
        )
        self._throttle = Throttle(0.25)
        self._lock = threading.Lock()
        self._streets: dict[str, dict] = {}
        self.last_read_at: float = 0.0
        self.last_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._streets = data.get("streets") or {}
            self.last_read_at = float(data.get("last_read_at") or 0.0)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps({"last_read_at": self.last_read_at, "streets": self._streets}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _query(self, url: str, params: dict) -> dict:
        self._throttle.wait()
        body = urllib.parse.urlencode(params).encode()
        data = self._post_json(url, body)
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            raise WebError(f"DLS AD_Addresses failed: {err.get('code')} {err.get('message')}")
        return data

    def query_street_points(self, pattern: str) -> list[dict]:
        """Points in Greater Nicosia bbox matching street name `pattern` via Table 4 + Layer 0."""
        safe = re.sub(r"[^A-Z0-9 ]", "", pattern.upper()).strip()
        if len(safe) < 3:
            return []
        with self._lock:
            hit = self._streets.get(safe)
            if hit and time.time() - hit.get("fetched_at", 0) < DLS_TTL_S:
                return hit.get("points", [])
        try:
            t4 = self._query(DLS_STREETS_QUERY, {
                "where": f"UPPER(name) LIKE '%{safe}%'",
                "outFields": "OBJECTID,IFCID,RID,name",
                "resultRecordCount": 200,
                "f": "json",
            })
            feats4 = t4.get("features") or []
            names_by_rid = {
                int(f["attributes"]["RID"]): str(f["attributes"].get("name") or safe)
                for f in feats4
                if (f.get("attributes") or {}).get("RID") is not None
            }
            points: list[dict] = []
            rids = sorted(names_by_rid)
            for i in range(0, len(rids), 150):
                chunk = rids[i:i + 150]
                l0 = self._query(DLS_ADDRESSES_QUERY, {
                    "where": f"RID IN ({','.join(map(str, chunk))})",
                    "geometry": DLS_BBOX_STR,
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "OBJECTID,IFCID,RID,specification",
                    "outSR": 4326,
                    "f": "geojson",
                })
                for feat in l0.get("features") or []:
                    coords = (feat.get("geometry") or {}).get("coordinates") or []
                    props = feat.get("properties") or {}
                    rid = props.get("RID")
                    if len(coords) >= 2:
                        lon, lat = float(coords[0]), float(coords[1])
                        if in_bbox(lat, lon, DLS_BBOX):
                            points.append({
                                "lat": round(lat, 6),
                                "lon": round(lon, 6),
                                "rid": rid,
                                "name": names_by_rid.get(int(rid), safe) if rid is not None else safe,
                            })
            now = time.time()
            with self._lock:
                self._streets[safe] = {"fetched_at": now, "points": points}
                self.last_read_at = now
                self.last_error = None
                self._save()
            return points
        except (WebError, OSError, ValueError, TypeError) as e:
            self.last_error = str(e)
            log.info("DLS AD_Addresses lookup failed for %r: %s", safe, e)
            return (hit or {}).get("points", [])

    def find_street_cluster(
        self,
        patterns: list[str],
        anchor: tuple[float, float] | None = None,
    ) -> dict | None:
        """Best street cluster from DLS AD_Addresses for candidate patterns."""
        for pat in patterns:
            pts = self.query_street_points(pat)
            if not pts:
                continue
            groups = streets_apart(pts)
            if anchor:
                # Score each cluster by distance to municipality anchor, favouring matching clusters.
                def cluster_center(g: list[dict]) -> tuple[float, float]:
                    lats = sorted(x["lat"] for x in g)
                    lons = sorted(x["lon"] for x in g)
                    return lats[len(lats) // 2], lons[len(lons) // 2]

                scored = [
                    (haversine_m(*anchor, *cluster_center(g)), -len(g), g, cluster_center(g))
                    for g in groups
                ]
                scored.sort(key=lambda t: (t[0], t[1]))
                dist, _, best_group, center = scored[0]
                if dist <= 5500 or len(groups) == 1:
                    return {
                        "lat": center[0],
                        "lon": center[1],
                        "street": best_group[0]["name"],
                        "points": len(best_group),
                        "distance_to_muni_m": round(dist),
                    }
            else:
                best_group = max(groups, key=len)
                lats = sorted(x["lat"] for x in best_group)
                lons = sorted(x["lon"] for x in best_group)
                return {
                    "lat": lats[len(lats) // 2],
                    "lon": lons[len(lons) // 2],
                    "street": best_group[0]["name"],
                    "points": len(best_group),
                }
        return None


def _within_dls_or_muni(
    lat: float, lon: float, dls_hit: dict | None, anchor: tuple[float, float] | None
) -> bool:
    if not in_bbox(lat, lon, DLS_BBOX):
        return False
    if dls_hit and haversine_m(lat, lon, dls_hit["lat"], dls_hit["lon"]) <= DOOR_NEAR_DLS_M:
        return True
    if anchor and haversine_m(lat, lon, anchor[0], anchor[1]) <= SAME_MUNI_MAX_M:
        return True
    return dls_hit is None and anchor is None


def _match_poi_list(
    candidates: list[dict],
    tel: str,
    surname: str,
    street_clean: str,
    anchor: tuple[float, float] | None,
) -> dict | None:
    # 1. Phone match is decisive when inside Greater Nicosia.
    if tel:
        for p in candidates:
            if not in_bbox(p["lat"], p["lon"], DLS_BBOX):
                continue
            cand_phones = [p.get("phone")] + list(p.get("phones") or ())
            if any(digits_8(ph) == tel for ph in cand_phones if ph):
                return p
    # 2. Surname match requires street or municipality agreement so namesakes in other suburbs don't collide.
    if surname:
        for p in candidates:
            if not in_bbox(p["lat"], p["lon"], DLS_BBOX):
                continue
            name_ok = loose_match(surname, p.get("name")) or loose_match(surname, p.get("name_local"))
            if not name_ok:
                continue
            p_street = p.get("street") or p.get("address") or ""
            street_ok = bool(street_clean and p_street and loose_match(street_clean, p_street))
            muni_ok = bool(anchor and haversine_m(*anchor, p["lat"], p["lon"]) <= 3000)
            if street_ok or muni_ok:
                return p
    return None


class TonightGeocoder:
    """Geocodes duty pharmacies and event venues, persisting non-personal results on disk."""

    def __init__(
        self,
        settings: Settings,
        places,
        overture=None,
        dls_addr: DLSAddressClient | None = None,
        cache_dir: Path | None = None,
    ):
        self.s = settings
        self.places = places
        self.overture = overture
        self.dls = dls_addr or DLSAddressClient(settings, cache_dir=cache_dir)
        self.dir = (cache_dir or CACHE_DIR) / "tonight"
        self.cache_path = self.dir / "geocode_cache.json"
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("version") == GEOCODE_VERSION:
                self._cache = data.get("items") or {}
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps({"version": GEOCODE_VERSION, "items": self._cache}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _cached(self, key: str) -> dict | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit.get("cached_at", 0) < PLACE_TTL_S:
                return dict(hit)
        return None

    def _remember(self, key: str, result: dict) -> dict:
        rec = {**result, "cached_at": time.time()}
        with self._lock:
            self._cache[key] = rec
            self._save()
        return dict(rec)

    def locate_pharmacy(self, row: dict, *, allow_network: bool = True) -> dict:
        """Locate one duty pharmacy using OSM/Overture POI matching, DLS AD_Addresses, and Nominatim/Photon."""
        tel = digits_8(row.get("pharmacy_tel"))
        surname = primary_surname(row.get("surname") or row.get("pharmacist"))
        address = row.get("address") or ""
        directions = row.get("directions") or ""
        municipality = row.get("municipality") or ""
        street_clean = clean_street(address)
        anchor = municipality_anchor(municipality)
        cache_key = f"ph:{row.get('reg_no')}:{norm(address)}:{norm(municipality)}"

        # 1. Exact POI match against OSM pharmacy places (fast, in-memory, always checked first)
        osm_ph = [p for p in (self.places.all() if self.places else []) if p.get("category") == "pharmacy"]
        if m := _match_poi_list(osm_ph, tel, surname, street_clean, anchor):
            return self._make_place("exact", m["lat"], m["lon"], "OpenStreetMap pharmacy", osm_id=m["id"])

        # 2. Exact POI match against Overture pharmacy places
        ov = self.overture if (self.overture is not None and self.overture.ready) else None
        if ov is not None:
            ov_ph = [p for p in ov.all() if p.get("category") == "pharmacy"]
            if m := _match_poi_list(ov_ph, tel, surname, street_clean, anchor):
                return self._make_place("exact", m["lat"], m["lon"], "Overture Maps pharmacy", overture_id=m["id"])

        if hit := self._cached(cache_key):
            return hit

        if not allow_network:
            if anchor:
                return self._make_place("area", anchor[0], anchor[1], "Municipality centre")
            return self._make_place("unplaced", None, None, "Unplaced")

        # 3. Official DLS INSPIRE/AD_Addresses street clusters inside Greater Nicosia
        patterns = dls_street_patterns(address, directions)
        dls_hit = self.dls.find_street_cluster(patterns, anchor) if patterns else None

        # 4. Try door-level / street-level Nominatim + Photon via Places.geocode
        door_hit, street_hit = self._geocode_address(address, municipality, dls_hit, anchor)
        if door_hit:
            located_by = "DLS AD_Addresses & geocoder" if dls_hit else "Address geocoder"
            return self._remember(cache_key, self._make_place("exact", door_hit["lat"], door_hit["lon"], located_by))

        if dls_hit:
            return self._remember(
                cache_key,
                self._make_place(
                    "street",
                    dls_hit["lat"],
                    dls_hit["lon"],
                    f"DLS INSPIRE AD_Addresses ({dls_hit['street']})",
                    dls_street=dls_hit["street"],
                    dls_points=dls_hit["points"],
                ),
            )

        if street_hit:
            return self._remember(cache_key, self._make_place("street", street_hit["lat"], street_hit["lon"], "Address geocoder"))

        if anchor:
            return self._remember(cache_key, self._make_place("area", anchor[0], anchor[1], "Municipality centre"))
        return self._make_place("unplaced", None, None, "Unplaced")

    def locate_venue(self, name: str, address: str | None, city: str | None, *, allow_network: bool = True) -> dict:
        """Locate an event venue using OSM, Overture, DLS AD_Addresses, and Nominatim/Photon."""
        cache_key = f"venue:{norm(name)}:{norm(address)}:{norm(city)}"
        if self.places and self.places.ready:
            hits = self.places.named(name)
            if hits and in_bbox(hits[0]["lat"], hits[0]["lon"], DLS_BBOX):
                return self._make_place("exact", hits[0]["lat"], hits[0]["lon"], "OpenStreetMap", osm_id=hits[0]["id"])
        ov = self.overture if (self.overture is not None and self.overture.ready) else None
        if ov is not None:
            hits = ov.named(name)
            if hits and in_bbox(hits[0]["lat"], hits[0]["lon"], DLS_BBOX):
                return self._make_place("exact", hits[0]["lat"], hits[0]["lon"], "Overture Maps", overture_id=hits[0]["id"])

        if hit := self._cached(cache_key):
            return hit
        if not allow_network:
            anchor = municipality_anchor(city) or municipality_anchor("nicosia")
            return self._make_place("area", anchor[0], anchor[1], "Municipality centre") if anchor else self._make_place("unplaced", None, None, "Unplaced")

        anchor = municipality_anchor(city) or municipality_anchor("nicosia")
        full_addr = ", ".join(x for x in (address, city) if x)
        patterns = dls_street_patterns(address or name)
        dls_hit = self.dls.find_street_cluster(patterns, anchor) if patterns else None
        door_hit, street_hit = self._geocode_address(full_addr or name, city or "Nicosia", dls_hit, anchor)
        if door_hit:
            return self._remember(cache_key, self._make_place("exact", door_hit["lat"], door_hit["lon"], "Address geocoder"))
        if dls_hit:
            return self._remember(
                cache_key,
                self._make_place(
                    "street",
                    dls_hit["lat"],
                    dls_hit["lon"],
                    f"DLS INSPIRE AD_Addresses ({dls_hit['street']})",
                    dls_street=dls_hit["street"],
                ),
            )
        if street_hit:
            return self._remember(cache_key, self._make_place("street", street_hit["lat"], street_hit["lon"], "Address geocoder"))
        if anchor:
            return self._remember(cache_key, self._make_place("area", anchor[0], anchor[1], "Municipality centre"))
        return self._make_place("unplaced", None, None, "Unplaced")

    def _geocode_address(
        self,
        address: str,
        municipality: str,
        dls_hit: dict | None,
        anchor: tuple[float, float] | None,
    ) -> tuple[dict | None, dict | None]:
        if self.places is None or not address:
            return None, None
        clean_addr = TRAIL_PARISH.sub("", address).strip()
        street_part = clean_street(clean_addr)
        latin_addr = to_dls_latin(clean_addr) if re.search(r"[α-ωΑ-Ω]", clean_addr) else clean_addr
        muni_en = {
            "λευκωσια": "Nicosia", "στροβολος": "Strovolos", "εγκωμη": "Engomi",
            "αγλαντζια": "Aglantzia", "λακαταμεια": "Lakatamia", "λατσια": "Latsia",
            "γερι": "Geri", "αγιος δομετιος": "Agios Dometios", "τσερι": "Tseri",
        }.get(norm(municipality), municipality or "Nicosia")

        queries = []
        for q in (
            f"{clean_addr}, {municipality}" if municipality else clean_addr,
            f"{latin_addr}, {muni_en}" if muni_en else latin_addr,
            f"{respell(latin_addr)}, {muni_en}" if muni_en else respell(latin_addr),
            street_only(f"{latin_addr}, {muni_en}"),
        ):
            if q and q not in queries:
                queries.append(q)

        street_words = [x for x in (street_part, to_dls_latin(street_part)) if x and len(x) >= 3]
        best_street: dict | None = None
        for q in queries[:3]:
            try:
                hits = self.places.geocode(q, limit=4)
            except (WebError, OSError, ValueError):
                continue
            valid = [
                h for h in hits
                if _within_dls_or_muni(h["lat"], h["lon"], dls_hit, anchor)
                and (not street_words or on_street(h, street_words))
            ]
            for h in by_postcode(valid, None):
                prec = precision_of(h)
                if prec == "exact":
                    return h, None
                if prec == "street" and best_street is None:
                    best_street = h
        return None, best_street

    @staticmethod
    def _make_place(
        precision: str,
        lat: float | None,
        lon: float | None,
        located_by: str,
        **extra,
    ) -> dict:
        return {
            "precision": precision,
            "precision_label": PRECISION_LABELS.get(precision, precision),
            "lat": round(lat, 6) if lat is not None else None,
            "lon": round(lon, 6) if lon is not None else None,
            "located_by": located_by,
            **{k: v for k, v in extra.items() if v is not None},
        }

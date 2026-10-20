"""Places and geocoding: an OSM places index cached from Overpass, plus Nominatim and Photon.

Overpass takes ~45 s and its mirrors fail often, so the index is fetched once in the background
and refreshed weekly; until it exists, `find_places` falls back to Photon (fast, fuzzy).
Nominatim needs every query word to match, so street-type words are stripped first
("Ledras Street" finds nothing, "Ledras" works).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import unicodedata
import urllib.parse

from chatbot.settings import CACHE_DIR, NICOSIA_BBOX, Settings
from chatbot.web import Throttle, WebError, get_json

log = logging.getLogger(__name__)

# category -> OSM tags (key, value)
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "museum": [("tourism", "museum")],
    "gallery": [("tourism", "gallery")],
    "attraction": [("tourism", "attraction")],
    "viewpoint": [("tourism", "viewpoint")],
    "monument": [("historic", "monument"), ("historic", "memorial")],
    "historic_site": [("historic", "castle"), ("historic", "city_gate"),
                      ("historic", "archaeological_site"), ("historic", "fort")],
    "place_of_worship": [("amenity", "place_of_worship")],
    "theatre": [("amenity", "theatre"), ("amenity", "cinema"), ("amenity", "arts_centre")],
    "cafe": [("amenity", "cafe")],
    "restaurant": [("amenity", "restaurant")],
    "bar": [("amenity", "bar"), ("amenity", "pub")],
    "fast_food": [("amenity", "fast_food")],
    "ice_cream": [("amenity", "ice_cream")],
    "pharmacy": [("amenity", "pharmacy")],
    "hospital": [("amenity", "hospital"), ("amenity", "clinic")],
    "toilets": [("amenity", "toilets")],
    "drinking_water": [("amenity", "drinking_water")],
    "bench": [("amenity", "bench")],
    "atm": [("amenity", "atm"), ("amenity", "bank")],
    "park": [("leisure", "park"), ("leisure", "garden")],
    "playground": [("leisure", "playground")],
    "hotel": [("tourism", "hotel"), ("tourism", "hostel"), ("tourism", "guest_house")],
    "supermarket": [("shop", "supermarket")],
    "parking_lot": [("amenity", "parking")],
    "ev_charging": [("amenity", "charging_station")],
    "bicycle_rental": [("amenity", "bicycle_rental")],
    "bus_station": [("amenity", "bus_station")],
}
KEEP_TAGS = ("opening_hours", "website", "phone", "wheelchair", "cuisine", "fee", "addr:street",
             "description", "wikipedia")
PLACES_TTL_S = 7 * 86400
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
STREET_WORDS = re.compile(
    r"\b(street|str\.?|st\.?|road|rd\.?|οδός|οδος|odos)\b", re.IGNORECASE
)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def norm(text: str | None) -> str:
    """Lower-case, accent-free, single-spaced (so 'Λήδρας' matches 'ληδρας')."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def in_bbox(lat: float, lon: float, bbox=NICOSIA_BBOX) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


class Places:
    def __init__(self, settings: Settings):
        self.s = settings
        self.path = CACHE_DIR / "places.json"
        self._places: list[dict] = []
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._loading = threading.Event()
        self._nominatim = Throttle(1.1)
        self._geocode_cache: dict[str, list[dict]] = {}
        self._load()

    # -- index -----------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._places, self._fetched_at = data["places"], data["fetched_at"]
        except (OSError, ValueError, KeyError):
            pass

    @property
    def ready(self) -> bool:
        return bool(self._places)

    def refresh_if_needed(self) -> None:
        """Fetch the index from Overpass if missing or a week old (blocking; ~45 s)."""
        if self._places and time.time() - self._fetched_at < PLACES_TTL_S:
            return
        if self._loading.is_set():
            return
        self._loading.set()
        try:
            self._fetch()
        except WebError as e:
            log.warning("places index refresh failed: %s", e)
        finally:
            self._loading.clear()

    def _fetch(self) -> None:
        lon_min, lat_min, lon_max, lat_max = NICOSIA_BBOX
        bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"
        parts = [f'nwr["{k}"="{v}"]({bbox});' for tags in CATEGORIES.values() for k, v in tags]
        query = f"[out:json][timeout:120];({''.join(parts)});out center tags;"
        body = urllib.parse.urlencode({"data": query}).encode()
        errors = []
        for url in dict.fromkeys([self.s.overpass_url, *OVERPASS_MIRRORS]):
            try:
                data = get_json(url, user_agent=self.s.user_agent, data=body, timeout=150)
                break
            except WebError as e:  # busy servers answer 429/502/504; try the next mirror
                errors.append(str(e).split("\n")[0])
        else:
            raise WebError("; ".join(errors))
        lookup = {(k, v): cat for cat, tags in CATEGORIES.items() for k, v in tags}
        places = []
        for e in data.get("elements", []):
            tags = e.get("tags", {})
            cat = next((c for (k, v), c in lookup.items() if tags.get(k) == v), None)
            lat = e.get("lat", e.get("center", {}).get("lat"))
            lon = e.get("lon", e.get("center", {}).get("lon"))
            if cat is None or lat is None:
                continue
            places.append({
                "id": f"{e['type'][0]}{e['id']}",
                "category": cat,
                "name": tags.get("name:en") or tags.get("name"),
                "name_local": tags.get("name"),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                **{k.replace("addr:", ""): tags[k] for k in KEEP_TAGS if k in tags},
            })
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"fetched_at": time.time(), "places": places}, ensure_ascii=False),
            encoding="utf-8",
        )
        with self._lock:
            self._places, self._fetched_at = places, time.time()
        log.info("places index: %d places", len(places))

    # -- search ----------------------------------------------------------------------------

    def find(
        self,
        category: str | None = None,
        query: str | None = None,
        near: tuple[float, float] | None = None,
        radius_m: float | None = None,
        limit: int = 20,
        named_only: bool = True,
    ) -> tuple[list[dict], str]:
        """Places matching a category and/or name, nearest first. Returns (places, source)."""
        if not self._places:
            return self._photon_find(category, query, near, limit), "photon (index still loading)"
        q = norm(query)
        out = []
        for p in self._places:
            if category and p["category"] != category:
                continue
            if named_only and not p.get("name"):
                continue
            if q and q not in norm(p.get("name")) and q not in norm(p.get("name_local")):
                continue
            item = dict(p)
            if near:
                item["distance_m"] = round(haversine_m(near[0], near[1], p["lat"], p["lon"]))
                if radius_m and item["distance_m"] > radius_m:
                    continue
            out.append(item)
        if near:
            out.sort(key=lambda p: p["distance_m"])
        return out[:limit], "OpenStreetMap (cached)"

    def _photon_find(self, category, query, near, limit) -> list[dict]:
        params = {"q": query or (category or "").replace("_", " "), "limit": limit,
                  "bbox": ",".join(map(str, NICOSIA_BBOX)), "lang": "en"}
        if category and category in CATEGORIES:
            k, v = CATEGORIES[category][0]
            params["osm_tag"] = f"{k}:{v}"
        if near:
            params["lat"], params["lon"] = near
        data = get_json(f"{self.s.photon_url}/api/", user_agent=self.s.user_agent,
                        params=params, timeout=20)
        out = []
        for f in data.get("features", []):
            lon, lat = f["geometry"]["coordinates"]
            pr = f.get("properties", {})
            item = {"id": f"{pr.get('osm_type', '?')[0].lower()}{pr.get('osm_id')}",
                    "category": category or pr.get("osm_value"), "name": pr.get("name"),
                    "lat": lat, "lon": lon, "street": pr.get("street")}
            if near:
                item["distance_m"] = round(haversine_m(near[0], near[1], lat, lon))
            out.append(item)
        return out

    # -- geocoding -------------------------------------------------------------------------

    def geocode(self, text: str, limit: int = 3) -> list[dict]:
        """Best matches inside Nicosia: the places index, then Nominatim, then Photon."""
        key = norm(text)
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        results: list[dict] = []
        if self._places:
            exact = [p for p in self._places
                     if key in (norm(p.get("name")), norm(p.get("name_local")))]
            results = [{"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                        "kind": p["category"], "source": "places index"} for p in exact[:limit]]
        if not results:
            results = self._nominatim_search(text, limit)
        if not results:
            results = self._photon_search(text, limit)
        self._geocode_cache[key] = results
        return results

    def _nominatim_search(self, text: str, limit: int) -> list[dict]:
        cleaned = re.sub(r"\s+", " ", STREET_WORDS.sub(" ", text)).strip(" ,") or text
        lon_min, lat_min, lon_max, lat_max = NICOSIA_BBOX
        params = {"q": cleaned, "format": "jsonv2", "limit": limit, "countrycodes": "cy",
                  "viewbox": f"{lon_min},{lat_max},{lon_max},{lat_min}", "bounded": 1,
                  "accept-language": "en"}
        self._nominatim.wait()
        try:
            data = get_json(f"{self.s.nominatim_url}/search", user_agent=self.s.user_agent,
                            params=params, timeout=20)
        except WebError as e:
            log.warning("nominatim failed: %s", e)
            return []
        return [{"name": d.get("name") or d.get("display_name", "").split(",")[0],
                 "address": d.get("display_name"), "lat": float(d["lat"]), "lon": float(d["lon"]),
                 "kind": d.get("type"), "source": "Nominatim"} for d in data]

    def _photon_search(self, text: str, limit: int) -> list[dict]:
        try:
            data = get_json(f"{self.s.photon_url}/api/", user_agent=self.s.user_agent,
                            params={"q": text, "limit": limit, "lang": "en",
                                    "bbox": ",".join(map(str, NICOSIA_BBOX))}, timeout=20)
        except WebError as e:
            log.warning("photon failed: %s", e)
            return []
        out = []
        for f in data.get("features", []):
            lon, lat = f["geometry"]["coordinates"]
            pr = f.get("properties", {})
            out.append({"name": pr.get("name"), "lat": lat, "lon": lon,
                        "kind": pr.get("osm_value"), "street": pr.get("street"),
                        "source": "Photon"})
        return out

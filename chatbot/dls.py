"""Cyprus Department of Lands and Surveys (DLS): cadastral parcels and the 3D buildings model.

Both are public ArcGIS REST layers that answer anonymous queries:

- Parcels (National/CadastralMap_EN, layer 0): every plot with its sheet, plan, block and
  parcel number (1.6 million plots).
- 3D buildings (INSPIRE/BU_Buildings, layer 6 "BU3.Building_All_Footprints"): 564,454
  footprints island-wide with the roof and eave heights above ground (m), the ground height
  above sea level, floors, roof type, use and volume.

Buildings reach the map in z16 web-mercator tiles (about 500 m across here), fetched on first
use and cached on disk. A building belongs to the tile holding its centre, so tiles never
overlap. Only the view/query services are used, never the INSPIRE download (WFS) services,
which are the paid ones. Requests are throttled and identify the app.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from chatbot.settings import CACHE_DIR, Settings
from chatbot.web import Throttle, WebError, get_json

log = logging.getLogger(__name__)

PARCELS_URL = ("https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/"
               "CadastralMap_EN/MapServer/0")
BUILDINGS_URL = ("https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE/"
                 "BU_Buildings/MapServer/6")
TILE_ZOOM = 16
PAGE = 1000  # the server's maxRecordCount
TILE_TTL_S = 30 * 86400
MEMORY_TILES = 400
# The 3D model's full extent (lon_min, lat_min, lon_max, lat_max): no queries outside it.
CYPRUS_BBOX = (32.25, 34.55, 34.62, 35.72)
DISTRICT_CODES = {"nicosia": 1, "famagusta": 3, "larnaca": 4, "limassol": 5, "paphos": 6}
PARCEL_FIELDS = "OBJECTID,DIST_CODE,VIL_CODE,QRTR_CODE,BLCK_CODE,SHEET,PLAN_NBR,PARCEL_NBR"
BUILDING_FIELDS = ("OBJECTID,H_ROOF_A_1,H_EAVE_B_1,H_MSL_m_1,FLOOR_QTY,GND_FLOOR,ROOF_TYPE,"
                   "B_TYPE,VOL_CubicM,AREA_sqm")
# Greek capitals that registers type where DLS stores the Latin letter ("62Ε2" for "62E2").
GREEK_TO_LATIN = str.maketrans("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEZHIKMNOPTYX")
SAFE_CODE = re.compile(r"^[0-9A-Z\-]+$")


# -- geometry helpers (small polygons in degrees, so metres come from a local projection) ------


def lonlat_to_tile(lon: float, lat: float, z: int = TILE_ZOOM) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds(x: int, y: int, z: int = TILE_ZOOM) -> tuple[float, float, float, float]:
    """(lon_min, lat_min, lon_max, lat_max) of a web-mercator tile."""
    n = 2 ** z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def tiles_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    x0, y1 = lonlat_to_tile(bbox[0], bbox[1])
    x1, y0 = lonlat_to_tile(bbox[2], bbox[3])
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def rings_of(geom: dict | None) -> list[list[list[float]]]:
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    if geom["type"] == "MultiPolygon":
        return [r for poly in geom["coordinates"] for r in poly]
    return []


def bbox_of(geom: dict) -> tuple[float, float, float, float]:
    pts = [p for r in rings_of(geom) for p in r]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _local(lat0: float):
    kx = 111320 * math.cos(math.radians(lat0))
    return lambda p: (p[0] * kx, p[1] * 110574)


def centroid(geom: dict) -> tuple[float, float] | None:
    """Area-weighted centre (lon, lat) of a polygon's outer rings, computed in local metres
    around the first vertex (cross products in raw degrees lose all precision at this size)."""
    polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"] if geom["type"] == "MultiPolygon" else [])
    if not polys or not polys[0] or not polys[0][0]:
        return None
    ox, oy = polys[0][0][0][:2]
    to_m = _local(oy)
    kx, ky = to_m((1, 1))
    total = sx = sy = 0.0
    for poly in polys:
        ring = [((p[0] - ox) * kx, (p[1] - oy) * ky) for p in poly[0]]
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            c = x1 * y2 - x2 * y1
            total += c
            sx += (x1 + x2) * c
            sy += (y1 + y2) * c
    if abs(total) < 1e-9:  # degenerate: fall back to the vertex mean
        pts = [p for poly in polys for p in poly[0]]
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return ox + sx / (3 * total) / kx, oy + sy / (3 * total) / ky


def area_m2(geom: dict) -> float:
    """Area of a (multi)polygon in square metres (outer rings minus holes)."""
    total = 0.0
    polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"] if geom["type"] == "MultiPolygon" else [])
    for poly in polys:
        for k, ring in enumerate(poly):
            if not ring:
                continue
            to_m = _local(ring[0][1])
            pts = [to_m(p) for p in ring]
            a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:]))) / 2
            total += a if k == 0 else -a
    return total


def contains(geom: dict, lon: float, lat: float) -> bool:
    """Point in (multi)polygon, even-odd over every ring, so holes count as outside."""
    polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"] if geom["type"] == "MultiPolygon" else [])
    for poly in polys:
        inside = False
        for ring in poly:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                    inside = not inside
        if inside:
            return True
    return False


def distance_to_boundary_m(geom: dict, lon: float, lat: float) -> float:
    """Metres from a point to the nearest edge of a polygon."""
    to_m = _local(lat)
    px, py = to_m((lon, lat))
    best = math.inf
    for ring in rings_of(geom):
        pts = [to_m(p) for p in ring]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            dx, dy = x2 - x1, y2 - y1
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy)
                                                        / (dx * dx + dy * dy)))
            best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def _round_geom(geom: dict) -> dict:
    def r(c):
        return [round(c[0], 6), round(c[1], 6)] if isinstance(c[0], (int, float)) else [r(x) for x in c]
    return {"type": geom["type"], "coordinates": r(geom["coordinates"])}


# -- parcels -----------------------------------------------------------------------------------


def plan_code(text: object) -> str | None:
    """A sheet or plan code as DLS stores it: upper case, Latin letters, no spaces."""
    s = re.sub(r"\s+", "", str(text or "")).upper().translate(GREEK_TO_LATIN)
    return s if s and SAFE_CODE.match(s) else None


def as_int(text: object) -> int | None:
    m = re.fullmatch(r"\s*0*(\d{1,6})\s*", str(text or ""))
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class ParcelRef:
    """A plot in the cadastre: sheet/plan/parcel, plus block and district where known."""
    sheet: str
    plan: str
    parcel: int
    block: int | None = None
    district: int | None = None

    def where(self) -> str:
        parts = [f"SHEET='{self.sheet}'", f"PLAN_NBR='{self.plan}'", f"PARCEL_NBR={self.parcel}"]
        if self.block is not None:
            parts.append(f"BLCK_CODE={self.block}")
        if self.district is not None:
            parts.append(f"DIST_CODE={self.district}")
        return "(" + " AND ".join(parts) + ")"

    def matches(self, props: dict) -> bool:
        return (str(props.get("SHEET")) == self.sheet and str(props.get("PLAN_NBR")) == self.plan
                and props.get("PARCEL_NBR") == self.parcel
                and (self.block is None or props.get("BLCK_CODE") == self.block)
                and (self.district is None or props.get("DIST_CODE") == self.district))


def parcel_refs(sheet: object, plan: object, parcels: object, block: object = None,
                district: int | None = None) -> list[ParcelRef]:
    """Refs for a register row. A parcel cell can hold several plots ('249, 250', '2080&2079');
    a resurvey plan ('2-290-379') always has sheet 0."""
    sheet_c, plan_c = plan_code(sheet), plan_code(plan)
    if plan_c and "-" in plan_c:
        sheet_c = "0"
    if not sheet_c or not plan_c:
        return []
    blk = as_int(block)
    return [ParcelRef(sheet_c, plan_c, n, blk, district)
            for n in (as_int(p) for p in re.split(r"[,&+/]|\s+και\s+|\s+", str(parcels or "")))
            if n is not None]


# -- buildings ---------------------------------------------------------------------------------


def _num(v: object) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def building(feature: dict) -> dict | None:
    """A DLS 3D building as a compact GeoJSON feature: id = OBJECTID; h roof height and e eave
    height above ground (m), g ground above sea level, f floors, t use, r roof type, v volume
    (m³), a footprint (m²). Implausible heights fall back to the floor count."""
    p = feature.get("properties") or {}
    geom = feature.get("geometry")
    oid = p.get("OBJECTID", feature.get("id"))
    if not geom or oid is None or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    floors = _num(p.get("FLOOR_QTY"))
    roof, eave = _num(p.get("H_ROOF_A_1")), _num(p.get("H_EAVE_B_1"))
    if roof is None or not 0.5 <= roof <= 300:
        roof = max(3.0, (floors or 1) * 3.2)
    if eave is None or not 0 < eave <= roof:
        eave = roof
    props = {"h": round(roof, 1), "e": round(eave, 1)}
    for key, src in (("g", "H_MSL_m_1"), ("v", "VOL_CubicM"), ("a", "AREA_sqm")):
        v = _num(p.get(src))
        if v is not None:
            props[key] = round(v, 1)
    if floors is not None:
        props["f"] = int(floors)
    for key, src in (("t", "B_TYPE"), ("r", "ROOF_TYPE")):
        if p.get(src) and str(p[src]).strip():
            props[key] = str(p[src]).strip()
    return {"type": "Feature", "id": int(oid), "geometry": _round_geom(geom), "properties": props}


class DLS:
    """Throttled, cached access to the DLS parcel and 3D building layers."""

    def __init__(self, settings: Settings, cache_dir=None, interval_s: float = 0.25,
                 workers: int = 4):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "dls3d"
        self._throttle = Throttle(interval_s)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dls")
        self._mem: OrderedDict[tuple[int, int], list[dict]] = OrderedDict()
        self._mem_lock = threading.Lock()
        self._tile_locks: dict[tuple[int, int], threading.Lock] = {}

    # -- requests ------------------------------------------------------------------------------

    def query(self, layer_url: str, params: dict) -> dict:
        """POST a query (polygons and long WHERE clauses exceed URL limits)."""
        self._throttle.wait()
        body = urllib.parse.urlencode({**params, "f": params.get("f", "geojson")}).encode()
        data = get_json(f"{layer_url}/query", user_agent=self.s.user_agent, data=body, timeout=60)
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            raise WebError(f"DLS query failed: {err.get('code')} {err.get('message')}")
        return data

    def _paged(self, layer_url: str, params: dict) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.query(layer_url, {**params, "orderByFields": "OBJECTID",
                                          "resultOffset": offset, "resultRecordCount": PAGE})
            feats = page.get("features") or []
            out.extend(feats)
            more = (page.get("properties") or {}).get("exceededTransferLimit") \
                or page.get("exceededTransferLimit")
            if not feats or (len(feats) < PAGE and not more):
                return out
            offset += len(feats)

    # -- parcels -------------------------------------------------------------------------------

    def parcels_by_objectid(self, ids: Iterable[int]) -> dict[int, dict]:
        ids = sorted({int(i) for i in ids})
        out: dict[int, dict] = {}
        for k in range(0, len(ids), 150):
            chunk = ids[k:k + 150]
            for f in self.query(PARCELS_URL, {
                    "where": f"OBJECTID IN ({','.join(map(str, chunk))})",
                    "outFields": PARCEL_FIELDS, "outSR": 4326}).get("features", []):
                if f.get("geometry"):
                    f["geometry"] = _round_geom(f["geometry"])  # also drops the Z values
                    out[f["properties"]["OBJECTID"]] = f
        return out

    def parcels_by_ref(self, refs: Iterable[ParcelRef]) -> dict[ParcelRef, list[dict]]:
        """Every DLS parcel matching each ref (usually one; more when the block is unknown)."""
        refs = list(dict.fromkeys(refs))
        out: dict[ParcelRef, list[dict]] = {r: [] for r in refs}
        for k in range(0, len(refs), 40):
            chunk = refs[k:k + 40]
            feats = self.query(PARCELS_URL, {"where": " OR ".join(r.where() for r in chunk),
                                             "outFields": PARCEL_FIELDS, "outSR": 4326})
            for f in feats.get("features", []):
                if not f.get("geometry"):
                    continue
                f["geometry"] = _round_geom(f["geometry"])  # also drops the Z values
                for r in chunk:
                    if r.matches(f["properties"]):
                        out[r].append(f)
        return out

    # -- 3D buildings --------------------------------------------------------------------------

    def _tile_path(self, x: int, y: int):
        return self.dir / str(TILE_ZOOM) / f"{x}_{y}.json"

    def tile(self, x: int, y: int) -> list[dict]:
        """The 3D buildings whose centre is in tile (x, y) at z16, from memory, disk or DLS."""
        key = (x, y)
        with self._mem_lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
            lock = self._tile_locks.setdefault(key, threading.Lock())
        with lock:  # one fetch per tile, however many callers ask at once
            with self._mem_lock:
                if key in self._mem:
                    return self._mem[key]
            feats = self._read_tile(x, y)
            if feats is None:
                feats = self._fetch_tile(x, y)
                path = self._tile_path(x, y)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fetched_at": time.time(), "features": feats},
                                           separators=(",", ":")), encoding="utf-8")
            with self._mem_lock:
                self._mem[key] = feats
                while len(self._mem) > MEMORY_TILES:
                    self._mem.popitem(last=False)
            return feats

    def _read_tile(self, x: int, y: int) -> list[dict] | None:
        try:
            data = json.loads(self._tile_path(x, y).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - data.get("fetched_at", 0) > TILE_TTL_S:
            return None
        return data["features"]

    def _fetch_tile(self, x: int, y: int) -> list[dict]:
        w, s, e, n = tile_bounds(x, y)
        if e < CYPRUS_BBOX[0] or w > CYPRUS_BBOX[2] or n < CYPRUS_BBOX[1] or s > CYPRUS_BBOX[3]:
            return []
        raw = self._paged(BUILDINGS_URL, {
            "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": BUILDING_FIELDS, "outSR": 4326})
        out, seen = [], set()
        for f in raw:
            b = building(f)
            if b is None or b["id"] in seen:
                continue
            c = centroid(b["geometry"])
            if c and w <= c[0] < e and s <= c[1] < n:
                seen.add(b["id"])
                out.append(b)
        return out

    def tiles(self, keys: Iterable[tuple[int, int]]) -> list[dict]:
        """Several tiles at once (fetched in parallel, still throttled)."""
        keys = list(dict.fromkeys(keys))
        return [b for feats in self._pool.map(lambda k: self.tile(*k), keys) for b in feats]

    def buildings_in_bbox(self, bbox: tuple[float, float, float, float]) -> list[dict]:
        return self.tiles(tiles_for_bbox(bbox))

    def buildings_near(self, lat: float, lon: float, radius_m: float) -> list[tuple[dict, float]]:
        """(building, distance from the point to its centre in m), nearest first."""
        dlat = radius_m / 110574
        dlon = radius_m / (111320 * math.cos(math.radians(lat)))
        to_m = _local(lat)
        px, py = to_m((lon, lat))
        out = []
        for b in self.buildings_in_bbox((lon - dlon, lat - dlat, lon + dlon, lat + dlat)):
            c = centroid(b["geometry"])
            if c is None:
                continue
            cx, cy = to_m(c)
            d = math.hypot(cx - px, cy - py)
            if d <= radius_m:
                out.append((b, d))
        out.sort(key=lambda t: t[1])
        return out

    def buildings_on(self, parcel: dict) -> tuple[list[dict], str]:
        """The 3D buildings standing on a parcel, and how they were matched:
        'on_parcel' (centre inside the plot), 'covers_parcel' (one footprint covers the plot's
        centre: in the walled city DLS models whole blocks as one building) or 'near_parcel'
        (centre within 2 m of the edge; plots and footprints are surveyed separately)."""
        inside, near = [], []
        w, s, e, n = bbox_of(parcel)
        pad = 0.00003  # about 3 m, so a centre just past the edge is still loaded
        for b in self.buildings_in_bbox((w - pad, s - pad, e + pad, n + pad)):
            c = centroid(b["geometry"])
            if c is None:
                continue
            if contains(parcel, *c):
                inside.append(b)
            elif distance_to_boundary_m(parcel, *c) <= 2:
                near.append(b)
        if inside:
            return inside, "on_parcel"
        mid = centroid(parcel)
        if mid:
            pad = 0.0008  # a block footprint's centre can lie ~80 m away, in another tile
            covering = [b for b in self.buildings_in_bbox((w - pad, s - pad, e + pad, n + pad))
                        if contains(b["geometry"], *mid)]
            if covering:
                return covering[:1], "covers_parcel"
        return near, "near_parcel" if near else "none"

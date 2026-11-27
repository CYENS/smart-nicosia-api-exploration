"""Trees for the 3D city, from OpenStreetMap.

One Overpass query for greater Nicosia, fetched in the background (never per request), kept on
disk and read again monthly. What it gives:

- mapped trees (`natural=tree`): each where it stands, with its height, crown and kind when
  tagged (`height`, `diameter_crown`, `leaf_type`, `genus`, `species`);
- tree rows (`natural=tree_row`): a tree every 8 m along the row;
- woods, orchards and parks (`natural=wood`, `landuse=forest`, `landuse=orchard`,
  `leisure=park`): planted on a loose grid (orchards in rows), sparser in parks, and sparser
  still in large woods so no area holds more than a few thousand. These are estimates: OSM
  says there are trees there, not where each one stands, and they are marked so.

Where a tag gives no height, a tree gets the usual size of its kind in Nicosia's streets (a
palm about 10 m, a pine 11 m, a broadleaf 7 m, an orchard tree 4 m). Copernicus's street tree
layer and tree cover density would add trees OSM lacks; their services
(image.discomap.eea.europa.eu, land.copernicus.eu) didn't answer from the twin's network when
this was built, so they aren't read.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from chatbot.dls import lonlat_to_tile
from chatbot.geo import OVERPASS_MIRRORS
from chatbot.settings import CACHE_DIR, NICOSIA_BBOX, Settings
from chatbot.web import WebError, get_json

log = logging.getLogger(__name__)

TTL_S = 30 * 86400
ROW_STEP_M = 8.0
MAX_PER_AREA = 4000
KINDS = ("mapped", "row", "wood", "orchard", "park")  # mapped and row: where OSM puts them
ESTIMATED = {"wood", "orchard", "park"}
LEAVES = ("broadleaved", "needleleaved", "palm")
PALMS = ("phoenix", "washingtonia", "palm", "chamaerops", "trachycarpus", "syagrus")
NEEDLES = ("pinus", "cupressus", "cedrus", "pine", "cypress", "juniperus", "araucaria", "casuarina")
# Typical size (height m, crown radius m) by leaf type, and for the kinds of area.
SIZE = {"broadleaved": (7.0, 2.8), "needleleaved": (11.0, 2.4), "palm": (10.0, 2.0)}
AREA = {  # spacing m, height m, crown m, share of grid points planted
    "wood": (7.0, 9.0, 3.0, 0.85),
    "orchard": (6.0, 4.0, 2.0, 1.0),
    "park": (13.0, 8.0, 3.2, 0.55),
}


def query(bbox: tuple[float, float, float, float] = NICOSIA_BBOX) -> str:
    lon0, lat0, lon1, lat1 = bbox
    b = f"({lat0},{lon0},{lat1},{lon1})"
    parts = [f'node["natural"="tree"]{b};', f'way["natural"="tree_row"]{b};',
             f'way["natural"="wood"]{b};', f'way["landuse"="forest"]{b};',
             f'way["landuse"="orchard"]{b};', f'way["leisure"="park"]{b};']
    return f"[out:json][timeout:120];({''.join(parts)});out tags geom qt;"


def _num(text: object) -> float | None:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*(m|metres?|meters?)?\s*$", str(text or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def leaf_of(tags: dict) -> str:
    lt = str(tags.get("leaf_type", "")).lower()
    name = " ".join(str(tags.get(k, "")) for k in ("genus", "species", "taxon", "species:en", "name")).lower()
    if lt == "palmleaved" or any(p in name for p in PALMS):
        return "palm"
    if lt == "needleleaved" or any(n in name for n in NEEDLES):
        return "needleleaved"
    return "broadleaved"


def _size(tags: dict, leaf: str, default: tuple[float, float]) -> tuple[float, float]:
    h = _num(tags.get("height"))
    d = _num(tags.get("diameter_crown"))
    height = h if h and 1 <= h <= 60 else default[0]
    crown = d / 2 if d and 0.5 <= d <= 40 else (default[1] if not h else max(1.2, min(default[1] * height / default[0], 8)))
    return round(height, 1), round(crown, 1)


def _genus(tags: dict) -> str | None:
    g = tags.get("genus") or (str(tags.get("species", "")).split(" ")[0] if tags.get("species") else None)
    return str(g).strip().capitalize() if g else None


def _local(lat0: float):
    kx = 111320 * math.cos(math.radians(lat0))
    return kx, 110574.0


def _hash(*parts: int) -> float:
    x = 0x811C9DC5
    for p in parts:
        x = ((x ^ (p & 0xFFFFFFFF)) * 0x01000193) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 0x5BD1E995) & 0xFFFFFFFF
    return (x ^ (x >> 15)) / 4294967296


def _inside(ring: list[tuple[float, float]], x: float, y: float) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def tree(lon: float, lat: float, h: float, r: float, kind: str, leaf: str, genus: str | None = None,
         osm: str | None = None) -> dict:
    t = {"lon": round(lon, 6), "lat": round(lat, 6), "h": h, "r": r, "kind": kind, "leaf": leaf}
    if genus:
        t["genus"] = genus
    if osm:
        t["osm"] = osm
    return t


def parse(elements: list[dict]) -> list[dict]:
    """Every tree the elements give, mapped and estimated."""
    out: list[dict] = []
    for e in elements:
        tags = e.get("tags") or {}
        if e.get("type") == "node" and tags.get("natural") == "tree" and "lat" in e:
            leaf = leaf_of(tags)
            h, r = _size(tags, leaf, SIZE[leaf])
            out.append(tree(e["lon"], e["lat"], h, r, "mapped", leaf, _genus(tags), f"n{e['id']}"))
        elif e.get("type") == "way" and e.get("geometry"):
            pts = [(g["lon"], g["lat"]) for g in e["geometry"] if g]
            if len(pts) < 2:
                continue
            if tags.get("natural") == "tree_row":
                out.extend(_row(e, pts, tags))
            else:
                kind = ("orchard" if tags.get("landuse") == "orchard" else "park"
                        if tags.get("leisure") == "park" else "wood")
                out.extend(_area(e, pts, tags, kind))
    return _thin(out)


def _row(e: dict, pts: list[tuple[float, float]], tags: dict) -> list[dict]:
    leaf = leaf_of(tags)
    h, r = _size(tags, leaf, (7.0, 2.5) if leaf == "broadleaved" else SIZE[leaf])
    kx, ky = _local(pts[0][1])
    out, carry = [], ROW_STEP_M / 2
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        seg = math.hypot((x2 - x1) * kx, (y2 - y1) * ky)
        d = carry
        while d <= seg:
            t = d / seg if seg else 0
            out.append(tree(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, h, r, "row", leaf, _genus(tags), f"w{e['id']}"))
            d += ROW_STEP_M
        carry = d - seg
    return out


def _area(e: dict, pts: list[tuple[float, float]], tags: dict, kind: str) -> list[dict]:
    if pts[0] != pts[-1] or len(pts) < 4:
        return []  # not a closed area
    spacing, h, r, share = AREA[kind]
    kx, ky = _local(pts[0][1])
    ring = [((lon - pts[0][0]) * kx, (lat - pts[0][1]) * ky) for lon, lat in pts[:-1]]
    area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2
    if area < spacing * spacing:
        return []
    spacing = max(spacing, math.sqrt(area / (MAX_PER_AREA * share)))  # big woods: sparser
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    leaf = leaf_of(tags) if (tags.get("leaf_type") or tags.get("genus") or tags.get("species")) else "broadleaved"
    out = []
    i = 0
    y = min(ys) + spacing / 2
    while y < max(ys):
        x = min(xs) + spacing / 2
        while x < max(xs):
            i += 1
            j = _hash(e["id"], i)
            if j < share:
                jitter = 0.0 if kind == "orchard" else spacing * 0.35
                px = x + (_hash(e["id"], i, 1) - 0.5) * 2 * jitter
                py = y + (_hash(e["id"], i, 2) - 0.5) * 2 * jitter
                if _inside(ring, px, py):
                    scale = 0.75 + 0.5 * _hash(e["id"], i, 3)  # trees of an area differ a little
                    out.append(tree(pts[0][0] + px / kx, pts[0][1] + py / ky, round(h * scale, 1),
                                    round(r * scale, 1), kind, leaf, None, f"w{e['id']}"))
            x += spacing
        y += spacing
    return out


def _thin(trees: list[dict]) -> list[dict]:
    """An estimated tree within 4 m of a mapped one is the mapped one."""
    cells: dict[tuple[int, int], list[dict]] = {}
    for t in trees:
        if t["kind"] not in ESTIMATED:
            cells.setdefault((int(t["lon"] * 25000), int(t["lat"] * 25000)), []).append(t)
    out = []
    for t in trees:
        if t["kind"] in ESTIMATED:
            cx, cy = int(t["lon"] * 25000), int(t["lat"] * 25000)
            kx, _ = _local(t["lat"])
            near = any(math.hypot((m["lon"] - t["lon"]) * kx, (m["lat"] - t["lat"]) * 110574) < 4
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) for m in cells.get((cx + dx, cy + dy), ()))
            if near:
                continue
        out.append(t)
    return out


class Trees:
    """OSM's trees around Nicosia, read once in the background and kept on disk."""

    def __init__(self, settings: Settings, cache_dir: Path | None = None,
                 fetch: Callable[[str], dict] | None = None):
        self.s = settings
        self.path = (cache_dir or CACHE_DIR) / "trees_osm.json"
        self._fetch = fetch or self._overpass
        self._lock = threading.Lock()
        self._loading = threading.Event()
        self.trees: list[dict] = []
        self.fetched_at = 0.0
        self.error: str | None = None
        self._tiles: dict[tuple[int, int], list[dict]] = {}
        self._load()

    @property
    def ready(self) -> bool:
        return bool(self.fetched_at)

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._use(data.get("elements") or [], data.get("fetched_at", 0))

    def _use(self, elements: list[dict], fetched_at: float) -> None:
        trees = parse(elements)
        tiles: dict[tuple[int, int], list[dict]] = {}
        for t in trees:
            tiles.setdefault(lonlat_to_tile(t["lon"], t["lat"]), []).append(t)
        with self._lock:
            self.trees, self._tiles, self.fetched_at = trees, tiles, fetched_at

    def _overpass(self, q: str) -> dict:
        body = urllib.parse.urlencode({"data": q}).encode()
        errors = []
        for url in dict.fromkeys([self.s.overpass_url, *OVERPASS_MIRRORS]):
            try:
                return get_json(url, user_agent=self.s.user_agent, data=body, timeout=150)
            except WebError as e:  # busy servers answer 429/502/504: the next mirror
                errors.append(str(e).split("\n")[0][:120])
        raise WebError("; ".join(errors))

    def refresh_if_needed(self) -> None:
        """Read OSM's trees if there are none yet or they are a month old (blocking, ~30 s)."""
        if self.ready and time.time() - self.fetched_at < TTL_S:
            return
        if self._loading.is_set():
            return
        self._loading.set()
        try:
            data = self._fetch(query())
            elements = data.get("elements") or []
            now = time.time()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"fetched_at": now, "elements": elements},
                                            separators=(",", ":")), encoding="utf-8")
            self._use(elements, now)
            self.error = None
            log.info("trees: %d from OpenStreetMap", len(self.trees))
        except WebError as e:
            self.error = str(e)
            log.warning("trees refresh failed: %s", e)
        finally:
            self._loading.clear()

    def tile(self, x: int, y: int) -> list[dict]:
        """The trees in a z16 tile (empty until the first read)."""
        return self._tiles.get((x, y), [])

    def near(self, lat: float, lon: float, radius_m: float) -> list[tuple[dict, float]]:
        """(tree, distance in m), nearest first."""
        kx, ky = _local(lat)
        dlat, dlon = radius_m / ky, radius_m / kx
        x0, y1 = lonlat_to_tile(lon - dlon, lat - dlat)
        x1, y0 = lonlat_to_tile(lon + dlon, lat + dlat)
        out = []
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for t in self._tiles.get((x, y), ()):
                    d = math.hypot((t["lon"] - lon) * kx, (t["lat"] - lat) * ky)
                    if d <= radius_m:
                        out.append((t, d))
        out.sort(key=lambda p: p[1])
        return out

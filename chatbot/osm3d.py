"""OpenStreetMap's buildings, for the 3D city wherever the DLS model has none: north of the buffer
zone, which the DLS doesn't survey, and the odd block it hasn't caught yet.

They come from the base map's own vector tiles (OpenFreeMap, built with OpenMapTiles from
OpenStreetMap), the z14 tiles the page already draws, so the city's 3D buildings and the base
map agree. A z14 tile is about 2.4 km across here; each is read once and kept on disk for 30
days, with only its buildings: every building as its own polygon (OpenMapTiles merges the
buildings of one height into one feature), cut to the tile's edge (the tiles overlap by a
margin), with its height and the height it starts at (a building part over a passage).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict

from chatbot import mvt
from chatbot.settings import CACHE_DIR, Settings
from chatbot.web import Throttle, WebError, get_bytes, get_json

log = logging.getLogger(__name__)

# Two addresses with the same tiles (chatbot/web/src/lib/map/basemap.ts says why there are two).
TILEJSON_URLS = ("https://tiles.openfreemap.org/planet", "https://tiles.openfreemap.org/backup_planet")
ZOOM = 14
TTL_S = 30 * 86400
TEMPLATE_TTL_S = 86400  # the tiles' address names the weekly build: asked again daily
MEMORY_TILES = 64
DEFAULT_HEIGHT = 5.0  # what OpenMapTiles draws a building with no height or levels at


class OsmBuildings:
    """OpenStreetMap buildings by z14 tile, read from OpenFreeMap and cached on disk."""

    def __init__(self, settings: Settings, cache_dir=None, interval_s: float = 0.3):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "osm3d" / str(ZOOM)
        self._throttle = Throttle(interval_s)
        self._template: str | None = None
        self._template_at = 0.0
        self._mem: OrderedDict[tuple[int, int], tuple[float, list[dict]]] = OrderedDict()
        self._lock = threading.Lock()

    def _path(self, x: int, y: int):
        return self.dir / f"{x}_{y}.json"

    def _load(self, x: int, y: int) -> tuple[float, list[dict]] | None:
        key = (x, y)
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        try:
            data = json.loads(self._path(x, y).read_text(encoding="utf-8"))
            entry = (float(data.get("fetched_at", 0)), data["buildings"])
        except (OSError, ValueError, KeyError):
            return None
        with self._lock:
            self._mem[key] = entry
            while len(self._mem) > MEMORY_TILES:
                self._mem.popitem(last=False)
        return entry

    def tile(self, x: int, y: int) -> list[dict] | None:
        """The buildings of z14 tile (x, y) however old (an old copy beats none), or None if it
        hasn't been read yet. Each is {"r": rings as flat [x, y, x, y, ...] lists in the tile's
        coordinates (0 to 4096, the outer ring first), "h": height, "b": base height,
        "c": [x, y] centre, "a": area in tile units}."""
        entry = self._load(x, y)
        return entry[1] if entry else None

    def fresh(self, x: int, y: int) -> bool:
        """Whether the tile was read in the last 30 days (only the file's head is read)."""
        with self._lock:
            entry = self._mem.get((x, y))
        if entry is None:
            try:
                with open(self._path(x, y), "rb") as f:
                    head = f.read(64).decode("ascii", "replace")
            except OSError:
                return False
            m = re.match(r'\{"fetched_at":\s*([0-9.eE+-]+)', head)
            entry = (float(m.group(1)), []) if m else None
        return entry is not None and time.time() - entry[0] < TTL_S

    def _tiles_url(self) -> str:
        if self._template and time.time() - self._template_at < TEMPLATE_TTL_S:
            return self._template
        last: Exception | None = None
        for url in TILEJSON_URLS:
            try:
                tiles = get_json(url, user_agent=self.s.user_agent, timeout=20).get("tiles") or []
            except (WebError, AttributeError) as e:
                last = e
                continue
            if tiles:
                self._template, self._template_at = tiles[0], time.time()
                return self._template
        raise WebError(f"OpenFreeMap's tile address couldn't be read: {last}")

    def fetch(self, x: int, y: int) -> list[dict]:
        """Read z14 tile (x, y) from OpenFreeMap now (throttled) and keep its buildings."""
        url = self._tiles_url().replace("{z}", str(ZOOM)).replace("{x}", str(x)).replace("{y}", str(y))
        self._throttle.wait()
        raw = get_bytes(url, user_agent=self.s.user_agent, timeout=30)
        layer = mvt.decode(raw, only=["building"]).get("building")
        found = buildings_of(layer) if layer else []
        path = self._path(x, y)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        path.write_text(json.dumps({"fetched_at": now, "buildings": found}, separators=(",", ":")),
                        encoding="utf-8")
        with self._lock:
            self._mem[(x, y)] = (now, found)
            self._mem.move_to_end((x, y))
            while len(self._mem) > MEMORY_TILES:
                self._mem.popitem(last=False)
        return found


def buildings_of(layer: mvt.Layer) -> list[dict]:
    """Every building polygon of an OpenMapTiles 'building' layer, cut to the tile (see
    OsmBuildings.tile for the shape). Outlines that only frame their parts (`hide_3d`) are left
    out, as the base map leaves them out of its 3D buildings."""
    k = mvt.EXTENT / layer.extent
    out = []
    for f in layer.features:
        if f.type != mvt.POLYGON or f.properties.get("hide_3d"):
            continue
        h = _height(f.properties.get("render_height"), DEFAULT_HEIGHT) or DEFAULT_HEIGHT
        b = min(_height(f.properties.get("render_min_height"), 0.0), max(0.0, h - 1))
        for poly in mvt.polygons(f.rings):
            rings = []
            for n, ring in enumerate(poly):
                cut = mvt.cleaned(mvt.clip([(x * k, y * k) for x, y in ring], 0, 0, mvt.EXTENT, mvt.EXTENT))
                if len(cut) < 3 or mvt.area2(cut) == 0:
                    if n == 0:
                        break  # the outer ring lies outside the tile: so do its holes
                    continue
                rings.append(mvt.oriented(cut, outer=n == 0))
            if not rings:
                continue
            area = mvt.area2(rings[0]) / 2 + sum(mvt.area2(r) / 2 for r in rings[1:])
            if area <= 0:
                continue
            cx, cy = mvt.centroid(rings[0])
            out.append({"r": [[v for p in r for v in p] for r in rings], "h": h, "b": b,
                        "c": [round(cx, 1), round(cy, 1)], "a": round(area, 1)})
    return out


def _height(v: object, default: float) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return round(f, 1) if 0 <= f <= 400 else default

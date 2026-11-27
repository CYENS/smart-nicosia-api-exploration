"""The 3D city as vector tiles, so the map has every building it can see, near and far.

The page draws the city from one vector source (chatbot/web/src/lib/map/engine.ts): MapLibre's
own tile engine picks the tiles a view needs (lower zooms in the distance of a tilted view) and
keeps the ones it has, as it does for the base map. The tiles, z12 to z16, are made here:

- A z16 tile (about 500 m across here) holds the DLS 3D buildings whose centre is in it
  (chatbot/dls.py) at their surveyed heights, with their DLS ids. Where the DLS has none, it holds
  OpenStreetMap's buildings instead (chatbot/osm3d.py, marked `src: "osm"`): north of the buffer
  zone, and wherever the DLS hasn't been read yet. Each z18 cell (about 125 m) is drawn from one
  or the other, never both, so no building stands twice. A cell is OpenStreetMap's when the DLS
  has nothing there, or far less than OpenStreetMap maps (the odd DLS building north of the line).
- z15 to z12 are made from the four tiles under them: outlines simplified and small buildings
  dropped as the tiles widen, so a view over the whole city stays light to draw.

A tile whose buildings are all known is kept on disk, gzipped as it is served, and sent with a
day's max-age. One still waiting for the DLS or OpenStreetMap is sent at once with what there is
and a max-age of a few seconds, so MapLibre asks again by itself and the tile fills in. Nothing
holds the map up.

The DLS is a throttled public service. Background threads read greater Nicosia's tiles
(NICOSIA_BBOX) through the DLS's own throttle: the ones being looked at first, the nearest the
camera first, then outward from the centre. Elsewhere in Cyprus the DLS is asked only for tiles
viewed close up (z15 and z16); further out those areas show OpenStreetMap's buildings.
"""

from __future__ import annotations

import gzip
import logging
import math
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace

from chatbot import mvt
from chatbot.dls import CYPRUS_BBOX, DLS, lonlat_to_tile, tile_bounds, tiles_for_bbox
from chatbot.osm3d import ZOOM as OSM_ZOOM
from chatbot.osm3d import OsmBuildings
from chatbot.settings import CACHE_DIR, NICOSIA_BBOX, NICOSIA_CENTER
from chatbot.web import WebError

log = logging.getLogger(__name__)

MIN_ZOOM, MAX_ZOOM = 12, 16
CLOSE_ZOOM = 15  # from here on the DLS is asked for any tile in Cyprus, not only Nicosia's
LAYER = "buildings"
VERSION = 1  # bump when what goes into a tile changes: old tiles on disk and in browsers go
CELL_BITS = 10  # z18 cells: a z16 tile's 4096 units in 4 x 4 cells of 1024
OSM_SHARE = 3.0  # a cell is OpenStreetMap's if it maps over 3x the DLS's footprint area there
PARTIAL_MAX_AGE_S = 5  # MapLibre asks again for a tile that is still filling in
FAILING_MAX_AGE_S = 60  # ... less often while a service is failing
FINAL_MAX_AGE_S = 86400
MEMORY_BYTES = 96 * 2 ** 20
RETRY_S = 600  # a tile that failed to load is tried again after this
DLS_THREADS = 2  # each waits on the DLS throttle, so together they stay within it
# While buildings arrive (the first fill), a wide tile is made again at most this often (s): a z12
# tile takes a second or two, and a new DLS tile under it comes every few tenths of a second.
REBUILD_AFTER_S = {12: 30.0, 13: 15.0, 14: 6.0, 15: 0.0, 16: 0.0}
EARTH_M = 40075016.686
# zoom: (smallest footprint kept in m², unless at least this tall in m, outline tolerance in m)
LEVELS: dict[int, tuple[float, float, float]] = {
    16: (0, 0, 0), 15: (0, 0, 0.5), 14: (25, 12, 1.5), 13: (60, 18, 3.5), 12: (150, 25, 7.0)}


@dataclass(frozen=True)
class Built:
    """A tile as it is served (a gzipped vector tile), and what it still waits for."""
    data: bytes
    count: int = -1  # buildings in it (-1: read from disk, not counted)
    missing_fill: bool = False  # DLS tiles of greater Nicosia not read yet
    missing_far: bool = False  # DLS tiles elsewhere in Cyprus not read yet
    missing_osm: bool = False  # OpenStreetMap tiles not read yet
    stale: bool = False  # buildings under it changed since it was made (it is made again soon)

    @property
    def final(self) -> bool:
        return not (self.missing_fill or self.missing_far or self.missing_osm or self.stale)

    def complete(self, z: int) -> bool:
        """Whether there is nothing more to wait for at zoom z (the DLS outside greater Nicosia
        is only asked for close up)."""
        return not (self.missing_fill or self.missing_osm or self.stale
                    or (z >= CLOSE_ZOOM and self.missing_far))


def _gzip(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=6, mtime=0)


EMPTY = Built(_gzip(mvt.encode_layer(LAYER, [])), 0)


def project(lon: float, lat: float, z: int, x: int, y: int) -> tuple[float, float]:
    """A point in tile (z, x, y)'s coordinates (0 to 4096 across, y down)."""
    n = 1 << z
    s = math.sin(math.radians(lat))
    wx = (lon + 180.0) / 360.0 * n
    wy = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return (wx - x) * mvt.EXTENT, (wy - y) * mvt.EXTENT


def units_per_m(z: int, y: int) -> float:
    """Tile units per metre on the ground at tile row y."""
    lat = math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / (1 << z))))
    return mvt.EXTENT * (1 << z) / (EARTH_M * math.cos(lat))


def cover(z: int, x: int, y: int, to: int) -> list[tuple[int, int]]:
    """The tiles at zoom `to` under (or over) tile (z, x, y)."""
    if to <= z:
        return [(x >> (z - to), y >> (z - to))]
    d = to - z
    return [(xx, yy) for yy in range(y << d, (y + 1) << d) for xx in range(x << d, (x + 1) << d)]


def in_cyprus(z: int, x: int, y: int) -> bool:
    w, s, e, n = tile_bounds(x, y, z)
    return not (e < CYPRUS_BBOX[0] or w > CYPRUS_BBOX[2] or n < CYPRUS_BBOX[1] or s > CYPRUS_BBOX[3])


def _cell(cx: float, cy: float) -> int:
    col = min(max(int(cx) >> CELL_BITS, 0), 3)
    row = min(max(int(cy) >> CELL_BITS, 0), 3)
    return row * 4 + col


def _rings_of(geom: dict, x: int, y: int) -> list[mvt.Ring]:
    """A DLS footprint (GeoJSON, lon/lat) in z16 tile (x, y)'s coordinates, outer rings positive."""
    polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"] if geom["type"] == "MultiPolygon" else [])
    out: list[mvt.Ring] = []
    for poly in polys:
        for n, ring in enumerate(poly):
            pts = mvt.cleaned([project(p[0], p[1], MAX_ZOOM, x, y) for p in ring])
            if len(pts) < 3 or mvt.area2(pts) == 0:
                if n == 0:
                    break
                continue
            out.append(mvt.oriented(pts, outer=n == 0))
    return out


def simplified(rings: list[mvt.Ring] | list[list[tuple[float, float]]], tol: float) -> list[mvt.Ring]:
    """Rings simplified within `tol` units, rounded; a polygon whose outline collapses goes, and
    so does a hole that does."""
    out: list[mvt.Ring] = []
    for poly in mvt.polygons(rings):  # type: ignore[arg-type]
        for n, ring in enumerate(poly):
            r = mvt.cleaned(mvt.simplify(list(ring), tol))
            a = mvt.area2(r) if len(r) >= 3 else 0
            if (a <= 0) if n == 0 else (a >= 0):
                if n == 0:
                    break
                continue
            out.append(r)
    return out


class CityTiles:
    """The 3D city's vector tiles, z12 to z16, and the background reading that fills them."""

    def __init__(self, dls: DLS, osm: OsmBuildings, cache_dir=None,
                 fill_bbox: tuple[float, float, float, float] = NICOSIA_BBOX,
                 center: tuple[float, float] = (NICOSIA_CENTER[1], NICOSIA_CENTER[0])):
        self.dls, self.osm = dls, osm
        self.root = (cache_dir or CACHE_DIR) / "city3d"
        self.dir = self.root / f"v{VERSION}"
        self.fill = frozenset(tiles_for_bbox(fill_bbox))
        cx, cy = lonlat_to_tile(*center)
        self._fill_order = sorted(self.fill, key=lambda k: (k[0] - cx) ** 2 + (k[1] - cy) ** 2)
        osm_fill = {(x >> (MAX_ZOOM - OSM_ZOOM), y >> (MAX_ZOOM - OSM_ZOOM)) for x, y in self.fill}
        self._osm_fill_order = sorted(osm_fill, key=lambda k: (k[0] * 4 + 2 - cx) ** 2 + (k[1] * 4 + 2 - cy) ** 2)
        self._lock = threading.Lock()
        self._mem: OrderedDict[tuple[int, int, int], Built] = OrderedDict()
        self._mem_bytes = 0
        self._gens: dict[tuple[int, int, int], int] = {}
        self._stale: dict[tuple[int, int, int], float] = {}  # tile: since when (kept meanwhile)
        self._changes = 0  # buildings arrived, all told
        self._warmed = -1  # ... when the pyramid was last made in full
        self._warm_lock = threading.Lock()
        self._build_locks: dict[tuple[int, int, int], threading.Lock] = {}
        # What the background readers know and are asked for.
        self._dls_known: set[tuple[int, int]] = set()  # fresh on disk
        self._osm_known: set[tuple[int, int]] = set()
        self._dls_wanted: dict[tuple[int, int], float] = {}  # key: priority (higher first)
        self._osm_wanted: dict[tuple[int, int], float] = {}
        self._busy: set[tuple[str, int, int]] = set()
        self._failed: dict[tuple[str, int, int], float] = {}
        self._scanned = False
        self._wake = {"dls": threading.Event(), "osm": threading.Event()}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.last_error: tuple[float, str] | None = None

    # -- serving -----------------------------------------------------------------------------

    def tile(self, z: int, x: int, y: int) -> tuple[bytes, bool]:
        """Tile (z, x, y) gzipped, and whether it is complete; an incomplete one asks the
        background readers for what it misses, first."""
        built = self.get(z, x, y)
        complete = built.complete(z)
        if not complete:
            self._ask(z, x, y, built)
        return built.data, complete

    def max_age(self, complete: bool) -> int:
        if complete:
            return FINAL_MAX_AGE_S
        failing = self.last_error is not None and time.time() - self.last_error[0] < 120
        return FAILING_MAX_AGE_S if failing else PARTIAL_MAX_AGE_S

    def _cached(self, key: tuple[int, int, int]) -> Built | None:
        """The tile in memory, unless it changed long enough ago to be made again (under the
        lock). One that changed lately is handed out marked stale, so it is asked for again."""
        hit = self._mem.get(key)
        if hit is None:
            return None
        since = self._stale.get(key)
        if since is None:
            self._mem.move_to_end(key)
            return hit
        if time.time() - since < REBUILD_AFTER_S[key[0]]:
            self._mem.move_to_end(key)
            return hit if hit.stale else replace(hit, stale=True)
        return None

    def get(self, z: int, x: int, y: int) -> Built:
        key = (z, x, y)
        with self._lock:
            hit = self._cached(key)
            if hit is not None:
                return hit
            lock = self._build_locks.setdefault(key, threading.Lock())
        with lock:  # one build per tile, however many ask at once
            with self._lock:
                hit = self._cached(key)
                gen = self._gens.get(key, 0)
                was_stale = key in self._stale
            if hit is not None:
                return hit
            built = None if was_stale else self._read(key)
            if built is None:
                built = self._build(z, x, y)
                if built.final:
                    self._write(key, built, gen)
            return self._remember(key, built, gen)

    def _path(self, key: tuple[int, int, int]):
        z, x, y = key
        return self.dir / str(z) / f"{x}_{y}.mvt.gz"

    def _read(self, key: tuple[int, int, int]) -> Built | None:
        try:
            return Built(self._path(key).read_bytes())
        except OSError:
            return None

    def _write(self, key: tuple[int, int, int], built: Built, gen: int) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(built.data)
            with self._lock:
                if self._gens.get(key, 0) != gen:  # it changed while being built
                    tmp.unlink(missing_ok=True)
                    return
                tmp.replace(path)
        except OSError as e:
            log.warning("couldn't keep 3D city tile %s: %s", key, e)

    def _remember(self, key: tuple[int, int, int], built: Built, gen: int) -> Built:
        """Keep a tile just made. If its buildings changed while it was being made, it is kept
        all the same (during the first fill they change every moment) but as stale."""
        with self._lock:
            if self._gens.get(key, 0) != gen and not built.stale:
                built = replace(built, stale=True)
            if built.stale:
                self._stale[key] = time.time()
            else:
                self._stale.pop(key, None)
            old = self._mem.pop(key, None)
            if old is not None:
                self._mem_bytes -= len(old.data)
            self._mem[key] = built
            self._mem_bytes += len(built.data)
            while self._mem_bytes > MEMORY_BYTES and len(self._mem) > 1:
                gone_key, gone = self._mem.popitem(last=False)
                self._mem_bytes -= len(gone.data)
                self._stale.pop(gone_key, None)
            return built

    def _forget(self, keys: set[tuple[int, int, int]]) -> None:
        """Tiles whose buildings changed: gone from disk, and stale in memory (still served
        until made again, at most every REBUILD_AFTER_S)."""
        now = time.time()
        with self._lock:
            self._changes += 1
            for key in keys:
                self._gens[key] = self._gens.get(key, 0) + 1
                if key in self._mem:
                    self._stale.setdefault(key, now)
                self._path(key).unlink(missing_ok=True)

    def _changed(self, z16: list[tuple[int, int]]) -> None:
        keys = {(z, x >> (MAX_ZOOM - z), y >> (MAX_ZOOM - z))
                for x, y in z16 for z in range(MIN_ZOOM, MAX_ZOOM + 1)}
        self._forget(keys)

    def warm(self) -> bool:
        """Make greater Nicosia's tiles, every zoom, so a first view need not wait for them (the
        fill does it when it has nothing to read). True once they are all made and complete."""
        if not self._warm_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                changes = self._changes
            done = True
            shift = MAX_ZOOM - MIN_ZOOM
            for x, y in sorted({(x >> shift, y >> shift) for x, y in self.fill}):
                done = self.get(MIN_ZOOM, x, y).complete(MIN_ZOOM) and done
            if done:
                with self._lock:
                    self._warmed = changes
            return done
        finally:
            self._warm_lock.release()

    # -- building ----------------------------------------------------------------------------

    def _build(self, z: int, x: int, y: int) -> Built:
        if not in_cyprus(z, x, y):
            return EMPTY
        if z >= MAX_ZOOM:
            return self._build_base(x, y)
        min_area, tall, tol_m = LEVELS[z]
        upm = units_per_m(z, y)
        tol = tol_m * upm
        feats: list[mvt.Feature] = []
        flags = [False, False, False, False]
        for dy in (0, 1):
            for dx in (0, 1):
                kid = self.get(z + 1, 2 * x + dx, 2 * y + dy)
                flags = [flags[0] or kid.missing_fill, flags[1] or kid.missing_far,
                         flags[2] or kid.missing_osm, flags[3] or kid.stale]
                if kid.count == 0:
                    continue
                layer = mvt.decode(gzip.decompress(kid.data), only=[LAYER]).get(LAYER)
                ox, oy = dx * mvt.EXTENT, dy * mvt.EXTENT
                for f in layer.features if layer else []:
                    h = float(f.properties.get("h") or 0)
                    rings = [[((px + ox) / 2, (py + oy) / 2) for px, py in r] for r in f.rings]
                    area_m2 = sum(mvt.area2(r) for r in rings) / 2 / (upm * upm)
                    if area_m2 < min_area and h < tall:
                        continue
                    kept = simplified(rings, tol)
                    if kept:
                        feats.append(mvt.Feature(kept, f.properties, f.id))
        return Built(_gzip(mvt.encode_layer(LAYER, feats)), len(feats), *flags)

    def _build_base(self, x: int, y: int) -> Built:
        """A z16 tile: the DLS buildings, and OpenStreetMap's in the cells the DLS leaves."""
        dls_feats = self.dls.stored_tile(x, y)
        osm_all = self.osm.tile(x >> (MAX_ZOOM - OSM_ZOOM), y >> (MAX_ZOOM - OSM_ZOOM))
        upm = units_per_m(MAX_ZOOM, y)
        dls_area, osm_area = [0.0] * 16, [0.0] * 16
        dls_b: list[tuple[mvt.Feature, int]] = []
        for b in dls_feats or []:
            rings = _rings_of(b["geometry"], x, y)
            if not rings:
                continue
            cell = _cell(*mvt.centroid(rings[0]))
            props = b.get("properties") or {}
            area = props.get("a") or sum(mvt.area2(r) for r in rings) / 2 / (upm * upm)
            dls_area[cell] += max(float(area), 1.0)
            dls_b.append((mvt.Feature(rings, {"h": float(props.get("h") or 3.0)}, int(b["id"])), cell))
        osm_b: list[tuple[mvt.Feature, int]] = []
        shift = MAX_ZOOM - OSM_ZOOM
        k = 1 << shift
        ox, oy = (x & (k - 1)) * mvt.EXTENT, (y & (k - 1)) * mvt.EXTENT
        for b in osm_all or []:
            cx, cy = b["c"][0] * k - ox, b["c"][1] * k - oy
            if not (0 <= cx < mvt.EXTENT and 0 <= cy < mvt.EXTENT):
                continue
            cell = _cell(cx, cy)
            osm_area[cell] += b["a"] * k * k / (upm * upm)
            rings = [[(r[i] * k - ox, r[i + 1] * k - oy) for i in range(0, len(r) - 1, 2)] for r in b["r"]]
            props: dict = {"h": float(b["h"]), "src": "osm"}
            if b.get("b"):
                props["b"] = float(b["b"])
            osm_b.append((mvt.Feature(rings, props), cell))
        use_osm = [dls_area[c] == 0 or osm_area[c] > OSM_SHARE * dls_area[c] for c in range(16)]
        feats = [f for f, c in dls_b if not use_osm[c]] + [f for f, c in osm_b if use_osm[c]]
        in_fill = (x, y) in self.fill
        return Built(_gzip(mvt.encode_layer(LAYER, feats)), len(feats),
                     missing_fill=dls_feats is None and in_fill,
                     missing_far=dls_feats is None and not in_fill,
                     missing_osm=osm_all is None)

    # -- asking for what's missing -----------------------------------------------------------

    def _ask(self, z: int, x: int, y: int, built: Built) -> None:
        """Note what a tile in view waits for. Nearer the camera (a higher zoom in a tilted view)
        and more recent goes first."""
        prio = time.time() + 2.0 * (z - MIN_ZOOM)
        with self._lock:
            if built.missing_osm:
                for k in cover(z, x, y, OSM_ZOOM):
                    if k not in self._osm_known and in_cyprus(OSM_ZOOM, *k):
                        self._osm_wanted[k] = max(self._osm_wanted.get(k, 0.0), prio)
            if built.missing_fill or (z >= CLOSE_ZOOM and built.missing_far):
                for k in cover(z, x, y, MAX_ZOOM):
                    if k not in self._dls_known and (k in self.fill or (z >= CLOSE_ZOOM and in_cyprus(MAX_ZOOM, *k))):
                        self._dls_wanted[k] = max(self._dls_wanted.get(k, 0.0), prio)
            for wanted in (self._dls_wanted, self._osm_wanted):
                if len(wanted) > 4000:  # keep the most recent wishes
                    for k, _ in sorted(wanted.items(), key=lambda kv: kv[1])[:len(wanted) - 3000]:
                        del wanted[k]
        for wake in self._wake.values():
            wake.set()

    def _pick(self, kind: str) -> tuple[int, int] | None:
        wanted = self._dls_wanted if kind == "dls" else self._osm_wanted
        known = self._dls_known if kind == "dls" else self._osm_known
        order = self._fill_order if kind == "dls" else self._osm_fill_order
        fresh = self.dls.tile_fresh if kind == "dls" else self.osm.fresh
        now = time.time()

        def usable(k: tuple[int, int]) -> bool:
            return ((kind, *k) not in self._busy and k not in known
                    and now - self._failed.get((kind, *k), 0.0) > RETRY_S)

        with self._lock:
            asked = sorted((p, k) for k, p in wanted.items() if usable(k))
        for _, k in reversed(asked[-64:]):
            if fresh(*k):  # read meanwhile, by another path
                with self._lock:
                    known.add(k)
                    wanted.pop(k, None)
                continue
            return k
        with self._lock:
            return next((k for k in order if usable(k)), None)

    def _scan(self) -> None:
        """What greater Nicosia already has on disk, so the fill starts where it left off."""
        dls = {k for k in self.fill if self.dls.tile_fresh(*k)}
        osm = {k for k in self._osm_fill_order if self.osm.fresh(*k)}
        with self._lock:
            self._dls_known |= dls
            self._osm_known |= osm
            self._scanned = True

    def _read_one(self, kind: str) -> bool:
        """Read the next tile of one kind; False if there was nothing to read."""
        key = self._pick(kind)
        if key is None:
            return False
        tag = (kind, *key)
        with self._lock:
            if tag in self._busy:
                return True
            self._busy.add(tag)
        try:
            if kind == "dls":
                self.dls.tile(*key)
                changed = [key]
            else:
                self.osm.fetch(*key)
                changed = cover(OSM_ZOOM, *key, MAX_ZOOM)
            with self._lock:
                (self._dls_known if kind == "dls" else self._osm_known).add(key)
                (self._dls_wanted if kind == "dls" else self._osm_wanted).pop(key, None)
            self._changed(changed)
        except WebError as e:
            with self._lock:
                self._failed[tag] = time.time()
            self.last_error = (time.time(), str(e))
            log.info("3D city: %s tile %s couldn't be read: %s", kind, key, e)
            self._stop.wait(5)
        except Exception:  # noqa: BLE001 - one bad tile must not stop the filling
            log.exception("3D city: %s tile %s failed", kind, key)
            with self._lock:
                self._failed[tag] = time.time()
        finally:
            with self._lock:
                self._busy.discard(tag)
        return True

    def _loop(self, kind: str) -> None:
        if kind == "osm":
            self._scan()
        while not self._stop.is_set():
            if not self._scanned:
                self._stop.wait(0.5)
                continue
            self._wake[kind].clear()
            if self._read_one(kind):
                continue
            with self._lock:
                behind = self._warmed != self._changes
            if kind == "dls" and behind:
                try:
                    self.warm()
                except Exception:  # noqa: BLE001 - try again on the next quiet moment
                    log.exception("3D city: making the tiles in advance failed")
            self._wake[kind].wait(5)

    def start(self) -> None:
        """Start filling in the background (once)."""
        if self._threads:
            return
        for old in self.root.glob("v*"):  # tiles of an earlier version
            if old.is_dir() and old.name != self.dir.name:
                shutil.rmtree(old, ignore_errors=True)
        kinds = ["osm"] + ["dls"] * DLS_THREADS
        for n, kind in enumerate(kinds):
            t = threading.Thread(target=self._loop, args=(kind,), name=f"city3d-{kind}-{n}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for wake in self._wake.values():
            wake.set()

    def status(self) -> dict:
        """How far the filling has come, for the page's status line."""
        with self._lock:
            done = len(self._dls_known & self.fill)
            waiting = sum(1 for k in self._dls_wanted if k not in self._dls_known)
            osm_done = len(self._osm_known & set(self._osm_fill_order))
            err = self.last_error
        return {
            "fill": {"done": done, "total": len(self.fill), "osm_done": osm_done,
                     "osm_total": len(self._osm_fill_order)},
            "waiting": waiting, "running": any(t.is_alive() for t in self._threads),
            "scanned": self._scanned,
            "problem": err[1] if err and time.time() - err[0] < 120 else None,
        }

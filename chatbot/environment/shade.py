"""Sun and shade on the ground, from the DLS 3D buildings.

The city is turned into a height grid (a surface model): Web Mercator pixels at zoom 16 with
256-pixel tiles, about 1.95 m across in Nicosia, the same tiling the DLS buildings come in, so a
tile's buildings fill exactly 256 by 256 cells. Each cell holds the roof height of the building
standing on it (0 on open ground). A cell of open ground is in shade when, walking from it
towards the sun, some building rises above the sun's ray: the classic ray march over a surface
model, done for every cell at once by shifting the grid one step at a time.

- `Shade.area(lat, lon, radius_m)` is a square of ground with a margin around it, so buildings
  just outside still cast their shadows in.
- `Area.shade(at)` is which cells are in shade at a moment, `Area.sun_hours(day)` the hours of
  direct sun each cell gets over a day (and the share of open ground in sun every 15 minutes).
- `Shade.along(path, start, speed)` is sun or shade at each point of a walk, at the moment the
  walker passes it, for shaded walks.

Only buildings cast shade here: no trees, awnings or arcades, and the ground is flat (the
terrain's own relief is not shading the city at these scales). Shadows longer than MARGIN_M
(very low sun) are cut there.
"""

from __future__ import annotations

import base64
import logging
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from chatbot.dls import DLS, TILE_ZOOM, rings_of
from chatbot.environment import sun as S

log = logging.getLogger(__name__)

TILE_PX = 256
WORLD_PX = TILE_PX * 2 ** TILE_ZOOM  # pixels around the world at zoom 16
EARTH_M = 2 * math.pi * 6378137  # Web Mercator's equator
MARGIN_M = 160  # buildings this far outside an area still shade it
MAX_RADIUS_M = 400
STEP_MIN = 15
NO_DATA = 255  # a roof in the sun hours grid (a building stands there)
TILE_CACHE = 160
AREA_CACHE = 12


def px_of(lon: float, lat: float) -> tuple[float, float]:
    """Global pixel coordinates (x east, y south) at zoom 16, 256-pixel tiles."""
    x = (lon + 180) / 360 * WORLD_PX
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * WORLD_PX
    return x, y


def lonlat_of(x: float, y: float) -> tuple[float, float]:
    lon = x / WORLD_PX * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / WORLD_PX))))
    return lon, lat


def cell_m(lat: float) -> float:
    """How many metres one cell is across at this latitude."""
    return EARTH_M * math.cos(math.radians(lat)) / WORLD_PX


# -- rasterising buildings ---------------------------------------------------------------------


@dataclass
class Cells:
    """The cells a tile's buildings cover: global pixel rows and columns, and the height."""
    rows: np.ndarray
    cols: np.ndarray
    heights: np.ndarray


def rasterise(buildings: list[dict]) -> Cells:
    """Every cell whose centre lies inside a building's footprint (even-odd over all its rings,
    so courtyards stay open), with the building's roof height."""
    rows, cols, hs = [], [], []
    for b in buildings:
        h = float((b.get("properties") or {}).get("h") or 0)
        rings = rings_of(b.get("geometry"))
        if h <= 0 or not rings:
            continue
        pts = [np.array([px_of(p[0], p[1]) for p in ring if len(p) >= 2], dtype=np.float64)
               for ring in rings if len(ring) >= 4]
        if not pts:
            continue
        allp = np.vstack(pts)
        c0, c1 = int(math.floor(allp[:, 0].min())), int(math.ceil(allp[:, 0].max()))
        r0, r1 = int(math.floor(allp[:, 1].min())), int(math.ceil(allp[:, 1].max()))
        if c1 - c0 > 600 or r1 - r0 > 600:  # a broken footprint; skip it
            continue
        xc = np.arange(c0, c1) + 0.5
        yc = np.arange(r0, r1) + 0.5
        if not len(xc) or not len(yc):
            continue
        edges = np.vstack([np.hstack([ring[:-1], ring[1:]]) for ring in pts])  # x1 y1 x2 y2
        x1, y1, x2, y2 = (edges[:, k][:, None] for k in range(4))
        spans = (y1 > yc) != (y2 > yc)  # [edge, row]
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = np.where(spans, x1 + (yc - y1) * (x2 - x1) / (y2 - y1), -np.inf)
        crossings = (xc[None, None, :] < xint[:, :, None]).sum(axis=0)  # [row, col]
        inside = (crossings % 2) == 1
        rr, cc = np.nonzero(inside)
        if not len(rr):  # smaller than a cell: its centre cell still stands
            cx, cy = allp[:, 0].mean(), allp[:, 1].mean()
            rr, cc = np.array([int(cy) - r0]), np.array([int(cx) - c0])
        rows.append(rr + r0)
        cols.append(cc + c0)
        hs.append(np.full(len(rr), h, dtype=np.float32))
    if not rows:
        empty = np.zeros(0, dtype=np.int64)
        return Cells(empty, empty, np.zeros(0, dtype=np.float32))
    return Cells(np.concatenate(rows), np.concatenate(cols), np.concatenate(hs))


# -- an area of ground -------------------------------------------------------------------------


@dataclass
class Area:
    """A square of ground (the window) inside a height grid with a margin around it."""
    heights: np.ndarray  # [rows, cols] metres, the whole grid, margin included
    origin: tuple[int, int]  # global pixel (row, col) of heights[0, 0]
    window: tuple[int, int, int, int]  # r0, r1, c0, c1 inside `heights`
    lat: float
    lon: float
    cell: float  # metres per cell
    tiles_missing: int = 0
    _cache: dict = field(default_factory=dict)

    @property
    def roofs(self) -> np.ndarray:
        r0, r1, c0, c1 = self.window
        return self.heights[r0:r1, c0:c1] > 0

    def bounds(self) -> list[list[float]]:
        """The window's corners (lon, lat): top left, top right, bottom right, bottom left, as
        an image is placed on the map."""
        r0, r1, c0, c1 = self.window
        gr, gc = self.origin
        tl = lonlat_of(gc + c0, gr + r0)
        br = lonlat_of(gc + c1, gr + r1)
        return [[round(tl[0], 7), round(tl[1], 7)], [round(br[0], 7), round(tl[1], 7)],
                [round(br[0], 7), round(br[1], 7)], [round(tl[0], 7), round(br[1], 7)]]

    def shade(self, at: datetime) -> np.ndarray | None:
        """True where open ground is in a building's shadow at `at` (roof cells are False).
        None while the sun is down: everything is in shade."""
        step = S.shadow_step(S.position(at, self.lat, self.lon))
        if step is None:
            return None
        return self._shade(step)

    def _shade(self, step: tuple[float, float, float]) -> np.ndarray:
        east, north, rise = step
        r0, r1, c0, c1 = self.window
        h = self.heights
        top = float(h.max(initial=0))
        out = np.zeros((r1 - r0, c1 - c0), dtype=bool)
        if top <= 0:
            return out
        limit = min(MARGIN_M, top / max(rise, 1e-6))
        rows_avail = min(r0, h.shape[0] - r1)
        cols_avail = min(c0, h.shape[1] - c1)
        k = 1
        last = None
        while k * self.cell <= limit:
            dc = int(round(k * east))
            dr = -int(round(k * north))  # rows run south
            if abs(dr) > rows_avail or abs(dc) > cols_avail:
                break
            if (dr, dc) != last:
                shifted = h[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
                out |= shifted > k * self.cell * rise
                last = (dr, dc)
            k += 1
        out &= ~self.roofs
        return out

    def sun_hours(self, day: date) -> dict:
        """Hours of direct sun per cell over a local day, in 15-minute steps (each step reads
        the sun at its middle), and the day's profile: the share of open ground in sun at each
        step, and the sun's altitude."""
        if ("hours", day) in self._cache:
            return self._cache[("hours", day)]
        r0, r1, c0, c1 = self.window
        quarters = np.zeros((r1 - r0, c1 - c0), dtype=np.int32)
        roofs = self.roofs
        open_cells = int((~roofs).sum())
        profile = []
        for t in S.day_moments(day, STEP_MIN):
            mid = t + timedelta(minutes=STEP_MIN / 2)
            sun = S.position(mid, self.lat, self.lon)
            step = S.shadow_step(sun)
            if step is None:
                profile.append({"time": t.strftime("%Y-%m-%dT%H:%M"), "sun_pct": 0.0,
                                "sun_altitude_deg": round(sun.altitude, 1)})
                continue
            lit = ~self._shade(step) & ~roofs
            quarters += lit
            share = 100 * float(lit.sum()) / open_cells if open_cells else 0.0
            profile.append({"time": t.strftime("%Y-%m-%dT%H:%M"), "sun_pct": round(share, 1),
                            "sun_altitude_deg": round(sun.altitude, 1)})
        hours = quarters * (STEP_MIN / 60)
        out = {"quarters": quarters, "hours": hours, "profile": profile, "roofs": roofs,
               "open_cells": open_cells}
        self._cache[("hours", day)] = out
        return out

    def lit(self, idx: tuple[int, int] | None, at: datetime) -> bool:
        """Whether one cell of the window (open ground) is in direct sun at `at`."""
        step = S.shadow_step(S.position(at, self.lat, self.lon))
        if step is None or idx is None:
            return False
        r, c = idx[0] + self.window[0], idx[1] + self.window[2]
        if self.heights[r, c] > 0:
            return True  # a roof is above the street's shade
        return not _ray_blocked(self.heights, r, c, step, self.cell, float(self.heights.max(initial=0)))

    def point_index(self, lon: float, lat: float) -> tuple[int, int] | None:
        """(row, col) inside the window of the cell under a point, or None outside it."""
        x, y = px_of(lon, lat)
        r = int(math.floor(y)) - self.origin[0] - self.window[0]
        c = int(math.floor(x)) - self.origin[1] - self.window[2]
        if 0 <= r < self.window[1] - self.window[0] and 0 <= c < self.window[3] - self.window[2]:
            return r, c
        return None


def encode_grid(values: np.ndarray) -> str:
    """A uint8 grid, row by row from the north, as base64 (what the page draws)."""
    return base64.b64encode(np.ascontiguousarray(values, dtype=np.uint8).tobytes()).decode()


def downsample(a: np.ndarray, k: int, how: str = "mean") -> np.ndarray:
    if k <= 1:
        return a
    r, c = (a.shape[0] // k) * k, (a.shape[1] // k) * k
    b = a[:r, :c].reshape(r // k, k, c // k, k)
    return b.mean(axis=(1, 3)) if how == "mean" else b.max(axis=(1, 3))


# -- the shade service ---------------------------------------------------------------------------


class Shade:
    """Builds height grids from the DLS tiles (each tile rasterised once and kept) and answers
    sun and shade for areas and walks."""

    def __init__(self, dls: DLS | None):
        self.dls = dls
        self._tiles: OrderedDict[tuple[int, int], Cells] = OrderedDict()
        self._areas: OrderedDict[tuple, Area] = OrderedDict()
        self._lock = threading.Lock()

    def _tile_cells(self, x: int, y: int, fetch: bool) -> Cells | None:
        with self._lock:
            hit = self._tiles.get((x, y))
            if hit is not None:
                self._tiles.move_to_end((x, y))
                return hit
        if self.dls is None:
            return None
        feats = self.dls.tile(x, y) if fetch else self.dls.cached_tile(x, y)
        if feats is None:
            return None
        cells = rasterise(feats)
        with self._lock:
            self._tiles[(x, y)] = cells
            while len(self._tiles) > TILE_CACHE:
                self._tiles.popitem(last=False)
        return cells

    def grid(self, x0: int, y0: int, x1: int, y1: int, fetch: bool = True) -> tuple[np.ndarray, int]:
        """The height grid over global pixels [x0, x1) by [y0, y1), and how many of the DLS
        tiles it needs could not be read."""
        h = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
        # A building belongs to the tile holding its centre and may reach into the next one.
        tx0, tx1 = (x0 - 64) // TILE_PX, (x1 + 64) // TILE_PX
        ty0, ty1 = (y0 - 64) // TILE_PX, (y1 + 64) // TILE_PX
        missing = 0
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                try:
                    cells = self._tile_cells(tx, ty, fetch)
                except Exception as e:  # noqa: BLE001 - a tile the DLS won't give: open ground
                    log.info("DLS tile %s,%s for shade failed: %s", tx, ty, e)
                    cells = None
                if cells is None:
                    missing += 1
                    continue
                if not len(cells.rows):
                    continue
                r = cells.rows - y0
                c = cells.cols - x0
                ok = (r >= 0) & (r < h.shape[0]) & (c >= 0) & (c < h.shape[1])
                np.maximum.at(h, (r[ok], c[ok]), cells.heights[ok])
        return h, missing

    def area(self, lat: float, lon: float, radius_m: float, fetch: bool = True) -> Area:
        """A square of ground `radius_m` around a point (half its side), on the cell grid."""
        radius_m = max(20.0, min(float(radius_m), MAX_RADIUS_M))
        cm = cell_m(lat)
        half = int(math.ceil(radius_m / cm))
        margin = int(math.ceil(MARGIN_M / cm))
        cx, cy = px_of(lon, lat)
        cx, cy = int(cx), int(cy)
        key = (cx, cy, half)
        with self._lock:
            if key in self._areas:
                self._areas.move_to_end(key)
                return self._areas[key]
        x0, y0 = cx - half - margin, cy - half - margin
        x1, y1 = cx + half + margin, cy + half + margin
        heights, missing = self.grid(x0, y0, x1, y1, fetch)
        n = 2 * half
        area = Area(heights, (y0, x0), (margin, margin + n, margin, margin + n), lat, lon, cm,
                    missing)
        with self._lock:
            self._areas[key] = area
            while len(self._areas) > AREA_CACHE:
                self._areas.popitem(last=False)
        return area

    def along(self, path: list[list[float]], start: datetime, speed_mps: float = 1.3,
              every_m: float = 8.0, fetch: bool = True) -> dict:
        """Sun or shade along a walk ([[lon, lat], ...]) started at `start`, each point at the
        moment the walker passes it: the shade share by length, and the stretches."""
        samples = _resample(path, every_m)
        if not samples:
            return {"shade_pct": None, "sun_up": False, "samples": []}
        lat0 = sum(p[1] for p in samples) / len(samples)
        cm = cell_m(lat0)
        pxs = [px_of(p[0], p[1]) for p in samples]
        margin = int(math.ceil(MARGIN_M / cm)) + 2
        x0 = int(min(p[0] for p in pxs)) - margin
        y0 = int(min(p[1] for p in pxs)) - margin
        x1 = int(max(p[0] for p in pxs)) + margin + 1
        y1 = int(max(p[1] for p in pxs)) + margin + 1
        if (x1 - x0) * (y1 - y0) > 4_000_000:  # about 3.9 km square: too far for one walk
            raise ValueError("the walk is too long to read its shade (keep it under about 3 km)")
        heights, missing = self.grid(x0, y0, x1, y1, fetch)
        top = float(heights.max(initial=0))
        shaded = []
        up = False
        for (lon, lat, along_m), (px, py) in zip(samples, pxs):
            t = start + timedelta(seconds=along_m / speed_mps)
            step = S.shadow_step(S.position(t, lat, lon))
            if step is None:
                shaded.append(True)
                continue
            up = True
            r, c = int(py) - y0, int(px) - x0
            if heights[r, c] > 0:  # through a passage or under a building's edge: covered
                shaded.append(True)
                continue
            shaded.append(_ray_blocked(heights, r, c, step, cm, top))
        n = len(shaded)
        shade_pct = round(100 * sum(shaded) / n, 1) if n else None
        return {"shade_pct": shade_pct, "sun_up": up, "tiles_missing": missing,
                "samples": [{"lon": s[0], "lat": s[1], "along_m": round(s[2]), "shade": sh}
                            for s, sh in zip(samples, shaded)]}

    def at_point(self, lat: float, lon: float, at: datetime, fetch: bool = True) -> bool | None:
        """Whether a point of open ground is in a building's shadow at `at`; None if the sun
        is down. A point on a roof reads as in the sun (it is above the street)."""
        area = self.area(lat, lon, 30, fetch)
        mask = area.shade(at)
        if mask is None:
            return None
        idx = area.point_index(lon, lat)
        return bool(mask[idx]) if idx else False


def _ray_blocked(h: np.ndarray, r: int, c: int, step, cm: float, top: float) -> bool:
    east, north, rise = step
    limit = min(MARGIN_M, top / max(rise, 1e-6))
    k = 1
    while k * cm <= limit:
        rr = r - int(round(k * north))
        cc = c + int(round(k * east))
        if not (0 <= rr < h.shape[0] and 0 <= cc < h.shape[1]):
            return False
        if h[rr, cc] > k * cm * rise:
            return True
        k += 1
    return False


def _resample(path: list[list[float]], every_m: float) -> list[tuple[float, float, float]]:
    """Points every `every_m` metres along a line, with how far along each is (lon, lat, m)."""
    if not path:
        return []
    out = [(path[0][0], path[0][1], 0.0)]
    along = 0.0
    carry = 0.0
    for (x1, y1), (x2, y2) in zip(path, path[1:]):
        kx = 111320 * math.cos(math.radians((y1 + y2) / 2))
        seg = math.hypot((x2 - x1) * kx, (y2 - y1) * 110574)
        if seg <= 0:
            continue
        d = every_m - carry
        while d <= seg:
            f = d / seg
            out.append((x1 + f * (x2 - x1), y1 + f * (y2 - y1), along + d))
            d += every_m
        carry = seg - (d - every_m)
        along += seg
    if along - out[-1][2] > every_m / 4:
        out.append((path[-1][0], path[-1][1], along))
    return out

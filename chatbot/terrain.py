"""The ground's height: the terrain model the Department of Lands and Surveys made from its
2019 LiDAR flight (a 1 m grid, CC BY 4.0), read as Mapterhorn's open terrain tiles (Terrarium
heights in lossless WebP, 512 px) and cached here, for the twin's views from high places and its
line-of-sight checks. The page draws the same tiles as its 3D terrain
(chatbot/web/src/lib/map/terrain.ts), so what the tools say and what the map shows agree.

The model stops at the buffer zone. North of it the tiles are filled from Copernicus GLO-30
(30 m), so the ground there is smoother than it really is and less sure.

A Terrarium pixel holds the height above sea level as (R * 256 + G + B / 256) - 32768 metres.
Two levels are read: ZOOM (about 16 m a pixel) for far views and for scanning the land for high
ground, and FINE_ZOOM (about 2 m a pixel) for the ground near where someone stands, where a
moat, a bastion or a river bank matters. Tiles are fetched once per area (throttled, with the
twin's user agent) and kept on disk as they came.
"""

from __future__ import annotations

import io
import logging
import math
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from array import array
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image

from chatbot.settings import CACHE_DIR, Settings
from chatbot.web import Throttle, WebError

log = logging.getLogger(__name__)

TILE_URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
ZOOM = 12  # about 16 m a pixel at Nicosia's latitude: far views, and scanning for high ground
FINE_ZOOM = 15  # about 2 m a pixel: the ground near where someone stands
FINE_STEP_M = 5.0  # a line of sight is sampled this often where it runs over the fine tiles
SIZE = 512
MEMORY_TILES = 96  # about 1 MB each
EARTH_R = 6371008.8
REFRACTION = 0.13  # light bends with the air: a line of sight curves a little with the earth


# -- PNG -----------------------------------------------------------------------------------------
# A small decoder of its own, for checking the PNGs the twin makes or is handed (the tests'
# tiles, an imagery tile): 8-bit RGB needs only zlib and the five PNG row filters.


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """An 8-bit RGB or RGBA PNG (not interlaced) -> (width, height, channels, pixel bytes)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat = 8, []
    width = height = channels = 0
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace:
                raise ValueError(f"unsupported PNG (depth {depth}, colour type {color})")
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if not width or not idat:
        raise ValueError("PNG without image data")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 255
        elif f != 0:
            raise ValueError(f"unknown PNG filter {f}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def terrarium(data: bytes) -> array:
    """A Terrarium tile (WebP as the tiles come, or PNG) -> its heights above sea level (m), row
    by row from the north-west. They are kept as 32-bit floats, which hold every Terrarium step
    (1/256 m) exactly, in a quarter of the memory a list would take."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            px = np.asarray(im.convert("RGB"), dtype=np.float32)
    except (OSError, ValueError, SyntaxError) as e:  # not an image, or a cut-off one
        raise ValueError(f"not a terrain tile ({e})") from None
    heights = px[..., 0] * 256 + px[..., 1] + px[..., 2] / 256 - 32768
    return array("f", heights.tobytes())


# -- tiles and points ----------------------------------------------------------------------------


def tile_xy(lat: float, lon: float, z: int = ZOOM) -> tuple[float, float]:
    """Fractional tile coordinates of a point (web mercator)."""
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The compass bearing from one point to another, 0 north, clockwise."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat: float, lon: float, bearing: float, dist: float) -> tuple[float, float]:
    """The point `dist` metres from (lat, lon) along `bearing`."""
    d = dist / EARTH_R
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def compass(bearing: float) -> str:
    return COMPASS[int(((bearing % 360) + 22.5) // 45) % 8]


def curvature_drop(d: float) -> float:
    """How far the ground falls below a level line of sight after d metres (with refraction)."""
    return d * d / (2 * EARTH_R) * (1 - REFRACTION)


def line_of_sight(profile: list[tuple[float, float]], eye_m: float, target_m: float,
                  obstacles: list[tuple[float, float, str]] = (), part_of_target_m: float = 0) -> dict:
    """Whether a target can be seen from an eye along a ground profile.

    `profile` is (distance from the eye in m, ground above sea level) up to the target, which
    stands at the last distance; `eye_m` and `target_m` are heights above sea level (the eye, and
    the top of what is looked at); `obstacles` are (distance, top above sea level, what) for
    buildings near the line; what stands within `part_of_target_m` of the target is the target
    itself (the near slope of a mountain is the mountain seen, the blocks of the centre are the
    centre). Returns visible, and otherwise
    what hides it first (nearest the eye), where and by how much (the metres it rises above the
    line of sight)."""
    if not profile:
        return {"visible": True}
    total = profile[-1][0]
    if total <= 0:
        return {"visible": True}
    points = [(d, g, "ground") for d, g in profile[:-1] if 0 < d < total - part_of_target_m]
    points += [(d, top, what) for d, top, what in obstacles if 0 < d < total - part_of_target_m]
    # Heights as the eye sees them, on the curved earth: everything sinks with distance, the
    # target too.
    seen_target = target_m - curvature_drop(total)
    for d, top, what in sorted(points):
        line = eye_m + (seen_target - eye_m) * d / total
        above = top - curvature_drop(d) - line
        if above > 0:
            return {"visible": False, "blocked_by": {"by_m": round(above, 1), "at_m": round(d),
                                                     "what": what, "top_m": round(top, 1)}}
    return {"visible": True}


class Terrain:
    """The ground's height anywhere, from cached Terrarium tiles."""

    def __init__(self, settings: Settings, cache_dir: Path | None = None,
                 fetch: Callable[[str], bytes] | None = None, interval_s: float = 0.2):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "terrain"
        self._fetch_bytes = fetch or self._download
        self._throttle = Throttle(interval_s)
        self._mem: OrderedDict[tuple[int, int, int], array] = OrderedDict()
        self._lock = threading.Lock()
        self._tile_locks: dict[tuple[int, int, int], threading.Lock] = {}
        self._failed: dict[tuple[int, int, int], float] = {}
        self.peaks: dict[tuple, tuple[float, float, float] | None] = {}  # chatbot/views.py

    def _download(self, url: str) -> bytes:
        self._throttle.wait()
        req = urllib.request.Request(url, headers={"User-Agent": self.s.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise WebError(f"terrain tiles answered HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise WebError(f"terrain tiles unreachable: {getattr(e, 'reason', e)}") from None

    def _path(self, z: int, x: int, y: int) -> Path:
        return self.dir / str(z) / f"{x}_{y}.webp"

    def tile(self, x: int, y: int, z: int = ZOOM) -> array:
        """One tile's heights, from memory, the disk or the terrain tiles (once per area)."""
        key = (z, x, y)
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
            if time.monotonic() - self._failed.get(key, -1e9) < 60:
                raise WebError("terrain tiles unreachable a moment ago; try again shortly")
            lock = self._tile_locks.setdefault(key, threading.Lock())
        with lock:  # one fetch per tile, however many ask at once
            with self._lock:
                if key in self._mem:
                    return self._mem[key]
            path = self._path(z, x, y)
            try:
                data = path.read_bytes()
            except OSError:
                try:
                    data = self._fetch_bytes(TILE_URL.format(z=z, x=x, y=y))
                except WebError:
                    with self._lock:
                        self._failed[key] = time.monotonic()
                    raise
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            try:
                heights = terrarium(data)
                if len(heights) != SIZE * SIZE:
                    raise ValueError(f"{len(heights)} heights, not {SIZE} by {SIZE}")
            except ValueError as e:
                path.unlink(missing_ok=True)
                raise WebError(f"a terrain tile could not be read: {e}") from None
            with self._lock:
                self._mem[key] = heights
                while len(self._mem) > MEMORY_TILES:
                    self._mem.popitem(last=False)
            return heights

    def elevation(self, lat: float, lon: float, z: int = ZOOM) -> float:
        """The ground above sea level at a point (m), between the four nearest pixels of the
        tiles at zoom `z`: ZOOM unless the point is where someone stands (FINE_ZOOM)."""
        fx, fy = tile_xy(lat, lon, z)
        px, py = fx * SIZE - 0.5, fy * SIZE - 0.5  # pixel centres
        x0, y0 = math.floor(px), math.floor(py)
        tx, ty = px - x0, py - y0

        def at(gx: int, gy: int) -> float:
            heights = self.tile(gx // SIZE, gy // SIZE, z)
            return heights[(gy % SIZE) * SIZE + gx % SIZE]

        top = at(x0, y0) * (1 - tx) + at(x0 + 1, y0) * tx
        bottom = at(x0, y0 + 1) * (1 - tx) + at(x0 + 1, y0 + 1) * tx
        return top * (1 - ty) + bottom * ty

    def profile(self, lat1: float, lon1: float, lat2: float, lon2: float,
                step_m: float = 30.0, fine_m: float = 0.0) -> list[tuple[float, float]]:
        """(distance from the first point in m, ground above sea level) along the straight line
        to the second point, both ends included: every `step_m`, and within `fine_m` of the first
        point (where the eye stands) every FINE_STEP_M from the fine tiles."""
        total = distance_m(lat1, lon1, lat2, lon2)
        near = min(max(fine_m, 0.0), total)
        marks: list[tuple[float, int]] = []
        if near > 0:
            k = math.ceil(near / FINE_STEP_M)
            marks += [(near * i / k, FINE_ZOOM) for i in range(k)]
        if total > near or not marks:
            n = max(1, math.ceil((total - near) / step_m))
            marks += [(near + (total - near) * i / n, ZOOM) for i in range(n + 1)]
        else:  # the whole line is near the eye
            marks.append((total, FINE_ZOOM))
        out = []
        for d, z in marks:
            f = d / total if total else 0.0
            lat, lon = lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f
            out.append((d, self.elevation(lat, lon, z)))
        return out

"""Aerial photos of Nicosia from the Department of Lands and Surveys (DLS), as map tiles.

Three of DLS's public base maps (ArcGIS REST, `BASEMAPS` folder), served to the page as Web
Mercator tiles and kept on disk:

- 2019: `Orthoimagery_2019_WebMercator`, a tile cache in Web Mercator (about 7.5 cm a pixel at
  its finest level). Its tiles are passed on as they are (JPEG, or PNG where the photo ends).
  Its own metadata calls it "Cached OI 2014"; the service is named 2019, and that is the year
  the twin gives it.
- 1993 and 1963: `Imagery_Orthophoto_1993` and `_1963`, black and white, in the Cyprus LTM grid
  with no tile cache. Each tile is one `export` of the tile's extent in Web Mercator, which the
  server reprojects.

DLS states no terms of use for these services (the copyright fields are empty): the twin names
DLS wherever a photo is drawn, and the terms should be confirmed with DLS before any public
deployment. Requests share the DLS throttle (4 a second at most), identify the app, stay inside
greater Nicosia and a band of zoom levels, and every tile is fetched once and kept: the photos
don't change.
"""

from __future__ import annotations

import logging
import math
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from chatbot.settings import CACHE_DIR, Settings
from chatbot.web import Throttle, WebError

log = logging.getLogger(__name__)

BASE = "https://eservices.dls.moi.gov.cy/arcgis/rest/services/BASEMAPS"
# Where the twin shows the photos: greater Nicosia and the hills around it (lon/lat).
COVER = (33.18, 35.04, 33.55, 35.27)
R = 20037508.342789244  # half the Web Mercator world, in metres


def _empty_png() -> bytes:
    """A transparent 1x1 PNG, for a tile with no photo (outside the cover, or past its edge)."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + chunk(b"IEND", b""))


EMPTY_PNG = _empty_png()


@dataclass(frozen=True)
class Photo:
    id: str
    year: int
    title: str
    service: str
    kind: str  # "tile": a Web Mercator tile cache; "export": drawn and reprojected per tile
    min_zoom: int
    max_zoom: int
    colour: bool

    @property
    def url(self) -> str:
        return f"{BASE}/{self.service}/MapServer"


PHOTOS: dict[str, Photo] = {p.id: p for p in (
    Photo("2019", 2019, "Aerial photo, 2019", "Orthoimagery_2019_WebMercator", "tile", 10, 19, True),
    Photo("1993", 1993, "Aerial photo, 1993", "Imagery_Orthophoto_1993", "export", 11, 18, False),
    Photo("1963", 1963, "Aerial photo, 1963", "Imagery_Orthophoto_1963", "export", 11, 18, False),
)}


def tile_lonlat_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** z

    def lat(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def tile_mercator_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) of a tile in Web Mercator metres."""
    s = 2 * R / 2 ** z
    return -R + x * s, R - (y + 1) * s, -R + (x + 1) * s, R - y * s


def in_cover(z: int, x: int, y: int) -> bool:
    w, s, e, n = tile_lonlat_bounds(z, x, y)
    return not (e < COVER[0] or w > COVER[2] or n < COVER[1] or s > COVER[3])


def sniff(data: bytes) -> str | None:
    """The image type of a tile's bytes, or None if they aren't an image (an error page)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


class Imagery:
    """DLS aerial photo tiles: from the disk, or fetched once (throttled) and kept."""

    def __init__(self, settings: Settings, cache_dir: Path | None = None,
                 throttle: Throttle | None = None,
                 fetch: Callable[[str], tuple[int, bytes]] | None = None):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "imagery"
        self._throttle = throttle or Throttle(0.25)
        self._fetch = fetch or self._download
        self._locks: dict[tuple, threading.Lock] = {}
        self._lock = threading.Lock()
        self.fetched = 0  # tiles asked of DLS since the start (for the data sources panel)
        self.failed = 0
        self.last_ok: float | None = None

    def _download(self, url: str) -> tuple[int, bytes]:
        self._throttle.wait()
        req = urllib.request.Request(url, headers={"User-Agent": self.s.user_agent,
                                                   "Accept": "image/*"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as e:
            raise WebError(f"DLS imagery unreachable: {getattr(e, 'reason', e)}") from None

    def source_url(self, photo: Photo, z: int, x: int, y: int) -> str:
        if photo.kind == "tile":
            return f"{photo.url}/tile/{z}/{y}/{x}"  # ArcGIS orders a tile's row before its column
        xmin, ymin, xmax, ymax = tile_mercator_bounds(z, x, y)
        return (f"{photo.url}/export?bbox={xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}"
                "&bboxSR=3857&imageSR=3857&size=256,256&format=jpg&transparent=false&f=image")

    def _path(self, photo: Photo, z: int, x: int, y: int) -> Path:
        return self.dir / photo.id / str(z) / str(x) / str(y)

    def cached(self, photo_id: str, z: int, x: int, y: int) -> tuple[bytes, str] | None:
        """A tile already on disk (the empty tile for one with no photo), never fetched here."""
        photo = PHOTOS.get(photo_id)
        if photo is None:
            return None
        if not (photo.min_zoom <= z <= photo.max_zoom) or not in_cover(z, x, y):
            return EMPTY_PNG, "image/png"
        path = self._path(photo, z, x, y)
        for ext, kind in ((".jpg", "image/jpeg"), (".png", "image/png")):
            try:
                return path.with_suffix(ext).read_bytes(), kind
            except OSError:
                continue
        if path.with_suffix(".none").exists():
            return EMPTY_PNG, "image/png"
        return None

    def tile(self, photo_id: str, z: int, x: int, y: int) -> tuple[bytes, str]:
        """(image bytes, content type) of one tile. Raises KeyError for an unknown photo and
        WebError when DLS can't be reached (nothing is cached then, so it is asked again)."""
        photo = PHOTOS[photo_id]
        if not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise KeyError(f"no tile {z}/{x}/{y}")
        hit = self.cached(photo_id, z, x, y)
        if hit is not None:
            return hit
        key = (photo_id, z, x, y)
        with self._lock:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # one fetch per tile, however many ask at once
            hit = self.cached(photo_id, z, x, y)
            if hit is not None:
                return hit
            try:
                status, data = self._fetch(self.source_url(photo, z, x, y))
            except WebError:
                self.failed += 1
                raise
            finally:
                with self._lock:
                    self._locks.pop(key, None)
            self.fetched += 1
            path = self._path(photo, z, x, y)
            path.parent.mkdir(parents=True, exist_ok=True)
            kind = sniff(data) if status == 200 else None
            if status == 404 or (status == 200 and kind is None and photo.kind == "tile"):
                path.with_suffix(".none").touch()  # past the photo's edge: nothing to draw
                return EMPTY_PNG, "image/png"
            if kind is None:
                self.failed += 1
                raise WebError(f"DLS imagery answered HTTP {status}")
            self.last_ok = time.time()
            path.with_suffix(".jpg" if kind == "image/jpeg" else ".png").write_bytes(data)
            return data, kind

    def public(self) -> list[dict]:
        """The photos, for the page: what each is and where its tiles are."""
        return [{"id": p.id, "year": p.year, "title": p.title, "colour": p.colour,
                 "tiles": f"/api/imagery/{p.id}/{{z}}/{{x}}/{{y}}", "min_zoom": p.min_zoom,
                 "max_zoom": p.max_zoom, "bounds": list(COVER)} for p in PHOTOS.values()]

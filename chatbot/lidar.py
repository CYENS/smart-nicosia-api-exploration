"""The city as the 2019 LiDAR flight measured it: the Department of Lands and Surveys' surface
model (everything the laser saw from above: roofs, trees, the ground between them) and terrain
model (the bare ground), and what the two say about each building.

DLS publishes both as GeoTIFF sheets of 2.5 km in its INSPIRE download folder, which the
national open data portal lists under CC BY 4.0
(https://www.data.gov.cy/el/dataset/ypsometria-inspire). They are plain files, not the WFS
download services the twin keeps away from. The sheets are heavy (greater Nicosia is 1.2 GB), so
they stay out of git, in `data/dls_lidar/` (or wherever DLS_LIDAR_DIR points):

    uv run python -m chatbot.lidar fetch          # the sheets for greater Nicosia, once
    uv run python -m chatbot.lidar measure        # every building's height, into data/

`measure` writes the small file the twin reads, so a deployment needs no sheet.

A sheet is named `ETRS89_<x>_<y>.tif` after its south-west corner on the national grid
(EPSG:6312, the Cyprus Local Transverse Mercator on ETRS89), in hundreds of metres.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from chatbot.settings import NICOSIA_BBOX, ROOT, Settings

FOLDER = "https://eservices.dls.moi.gov.cy/inspire_downloads/EL/rasters"
DATASET_URL = "https://www.data.gov.cy/el/dataset/ypsometria-inspire"  # the open data record
PRODUCTS = {"dsm": "2019_DSM", "dtm": "2019_DTM"}  # the surface, and the bare ground
SHEET_M = 2500
TIFF_MAGIC = (b"II*\x00", b"MM\x00*")


class LidarError(Exception):
    """The sheets could not be had or read. The message says what to do."""


def lidar_dir() -> Path:
    """Where the sheets are kept: DLS_LIDAR_DIR, or data/dls_lidar beside the code."""
    return Path(os.environ.get("DLS_LIDAR_DIR") or ROOT / "data" / "dls_lidar")


# -- the national grid ------------------------------------------------------------------------------

# EPSG:6312: transverse Mercator on GRS80, central meridian 33 E, scale 0.99995, false easting
# 200 km, false northing -3,500 km. ETRS89 and WGS84 are taken as one here, as DLS's own
# services do when they hand out longitudes and latitudes; they differ by well under a metre.
_A = 6378137.0
_F = 1 / 298.257222101
_K0 = 0.99995
_LON0 = math.radians(33.0)
_FE, _FN = 200_000.0, -3_500_000.0
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)


def to_grid(lat, lon):
    """(easting, northing) in metres on the national grid, for numbers or numpy arrays."""
    import numpy as np

    p, lam = np.radians(lat), np.radians(lon)
    sin_p, cos_p, tan_p = np.sin(p), np.cos(p), np.tan(p)
    n = _A / np.sqrt(1 - _E2 * sin_p ** 2)
    t, c, a = tan_p ** 2, _EP2 * cos_p ** 2, (lam - _LON0) * cos_p
    m = _A * ((1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256) * p
              - (3 * _E2 / 8 + 3 * _E2 ** 2 / 32 + 45 * _E2 ** 3 / 1024) * np.sin(2 * p)
              + (15 * _E2 ** 2 / 256 + 45 * _E2 ** 3 / 1024) * np.sin(4 * p)
              - (35 * _E2 ** 3 / 3072) * np.sin(6 * p))
    e = _FE + _K0 * n * (a + (1 - t + c) * a ** 3 / 6
                         + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120)
    north = _FN + _K0 * (m + n * tan_p * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                           + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    return e, north


def sheet_of(e: float, n: float) -> tuple[int, int]:
    """The sheet holding a point of the grid, as its name has it: the south-west corner in
    hundreds of metres."""
    return (int(e // SHEET_M) * (SHEET_M // 100), int(n // SHEET_M) * (SHEET_M // 100))


def sheet_name(x: int, y: int) -> str:
    return f"ETRS89_{x}_{y}.tif"


def sheets_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Every sheet a box of longitudes and latitudes touches (whether DLS has it or not)."""
    lon0, lat0, lon1, lat1 = bbox
    corners = [to_grid(lat, lon) for lat in (lat0, lat1) for lon in (lon0, lon1)]
    x0, y0 = sheet_of(min(float(c[0]) for c in corners), min(float(c[1]) for c in corners))
    x1, y1 = sheet_of(max(float(c[0]) for c in corners), max(float(c[1]) for c in corners))
    step = SHEET_M // 100
    return [(x, y) for y in range(y0, y1 + step, step) for x in range(x0, x1 + step, step)]


# -- fetching the sheets ----------------------------------------------------------------------------

_ROW = re.compile(r'\s(\d+)\s+<A HREF="[^"]+">(ETRS89_\d+_\d+\.tif)</A>', re.IGNORECASE)


def _open(url: str, user_agent: str, timeout: float = 120.0):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise LidarError(f"DLS answered HTTP {e.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LidarError(f"DLS could not be reached: {getattr(e, 'reason', e)}") from None


def listing(product: str, user_agent: str) -> dict[str, int]:
    """The sheets DLS lists for a product ('dsm' or 'dtm'): file name -> size in bytes."""
    with _open(f"{FOLDER}/{PRODUCTS[product]}/", user_agent) as resp:
        page = resp.read().decode("utf-8", "replace")
    found = {name: int(size) for size, name in _ROW.findall(page)}
    if not found:
        raise LidarError(f"the {PRODUCTS[product]} folder lists no sheets; DLS may be asking for "
                         "a person (a validation page). Stop and try again another day.")
    return found


def fetch_sheets(bbox: tuple[float, float, float, float] = NICOSIA_BBOX, out: Path | None = None,
                 products: tuple[str, ...] = ("dsm", "dtm"), pause_s: float = 1.0,
                 user_agent: str | None = None, say: Callable[[str], None] = print) -> dict:
    """Download the sheets a box needs, one at a time with a pause between them, skipping those
    already on disk at their listed size. Stops at the first answer that is not a sheet."""
    out = out or lidar_dir()
    user_agent = user_agent or Settings().user_agent
    wanted = sheets_for_bbox(bbox)
    done = {"fetched": 0, "kept": 0, "absent": 0, "bytes": 0}
    for product in products:
        listed = listing(product, user_agent)
        folder = out / PRODUCTS[product]
        folder.mkdir(parents=True, exist_ok=True)
        time.sleep(pause_s)
        for x, y in wanted:
            name = sheet_name(x, y)
            if name not in listed:
                done["absent"] += 1  # outside what the flight covered
                continue
            path = folder / name
            if path.exists() and path.stat().st_size == listed[name]:
                done["kept"] += 1
                continue
            part = path.with_suffix(".part")
            with _open(f"{FOLDER}/{PRODUCTS[product]}/{name}", user_agent) as resp:
                if "html" in (resp.headers.get("Content-Type") or "").lower():
                    raise LidarError(f"DLS answered a web page for {name}, not a sheet (a "
                                     "validation page?). Stopped; nothing more was asked.")
                with open(part, "wb") as f:
                    shutil.copyfileobj(resp, f, 1 << 20)
            with open(part, "rb") as f:
                magic = f.read(4)
            if part.stat().st_size != listed[name] or magic not in TIFF_MAGIC:
                part.unlink(missing_ok=True)
                raise LidarError(f"{name} came back as {magic!r}, not the {listed[name]} bytes of "
                                 "TIFF that were listed. Stopped.")
            os.replace(part, path)
            done["fetched"] += 1
            done["bytes"] += listed[name]
            say(f"{PRODUCTS[product]}/{name}  {listed[name] / 1e6:.1f} MB")
            time.sleep(pause_s)
    return done


# -- reading a sheet --------------------------------------------------------------------------------

TAG_SCALE, TAG_TIEPOINT, TAG_NODATA = 33550, 33922, 42113
EMPTY_BELOW = -1e30  # the sheets mark a cell nobody measured with the lowest float there is


class Sheet:
    """One sheet in memory: heights above sea level (m) on a grid of longitudes and latitudes
    (ETRS89), about half a metre a cell for the surface model and a metre for the terrain. The
    sheet's own tags say where its top-left corner is and how wide a cell is."""

    def __init__(self, path: Path):
        import numpy as np
        from PIL import Image

        try:
            with Image.open(path) as im:
                tags = im.tag_v2
                scale, tie = tags.get(TAG_SCALE), tags.get(TAG_TIEPOINT)
                if not scale or not tie or len(tie) < 6:
                    raise LidarError(f"{path.name} carries no position: not a DLS sheet")
                self.heights = np.asarray(im, dtype=np.float32)
        except (OSError, ValueError, SyntaxError) as e:
            raise LidarError(f"{path.name} could not be read: {e}") from None
        self.name = path.name
        self.dx, self.dy = float(scale[0]), float(scale[1])
        self.lon0 = float(tie[3]) - float(tie[0]) * self.dx  # the west edge of the first column
        self.lat0 = float(tie[4]) + float(tie[1]) * self.dy  # the north edge of the first row
        self.rows, self.cols = self.heights.shape

    def window(self, bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int] | None:
        """(row0, row1, col0, col1) of the cells whose centres fall in a box, or None when the
        box misses the sheet."""
        lon_min, lat_min, lon_max, lat_max = bbox
        c0 = max(0, math.ceil((lon_min - self.lon0) / self.dx - 0.5))
        c1 = min(self.cols, math.floor((lon_max - self.lon0) / self.dx - 0.5) + 1)
        r0 = max(0, math.ceil((self.lat0 - lat_max) / self.dy - 0.5))
        r1 = min(self.rows, math.floor((self.lat0 - lat_min) / self.dy - 0.5) + 1)
        return (r0, r1, c0, c1) if r1 > r0 and c1 > c0 else None

    def at(self, lon, lat):
        """The heights at arrays of points (nearest cell); empty outside the sheet."""
        import numpy as np

        c = np.floor((lon - self.lon0) / self.dx).astype(np.int64)
        r = np.floor((self.lat0 - lat) / self.dy).astype(np.int64)
        inside = (r >= 0) & (r < self.rows) & (c >= 0) & (c < self.cols)
        out = np.full(np.shape(lon), np.float32(-3.4e38), dtype=np.float32)
        out[inside] = self.heights[r[inside], c[inside]]
        return out


def _polygons(geom: dict) -> list[list[list[list[float]]]]:
    return ([geom["coordinates"]] if geom["type"] == "Polygon"
            else geom["coordinates"] if geom["type"] == "MultiPolygon" else [])


def cells_above_ground(geom: dict, surface: Sheet, ground: Sheet):
    """What stands on a footprint, as the two sheets have it: (heights above the ground of the
    cells whose centres are inside the footprint, the ground under them above sea level), both
    numpy arrays, with the cells either sheet has no value for left out."""
    import numpy as np

    empty = np.empty(0, dtype=np.float32)
    polys = _polygons(geom)
    pts = [p for poly in polys for p in poly[0]]
    if not pts:
        return empty, empty
    win = surface.window((min(p[0] for p in pts), min(p[1] for p in pts),
                          max(p[0] for p in pts), max(p[1] for p in pts)))
    if win is None:
        return empty, empty
    r0, r1, c0, c1 = win
    # A cell is in when its centre is: even-odd over every ring, so courtyards are out, as
    # chatbot.dls.contains has it. One pass per edge over the rows it crosses.
    lons = surface.lon0 + (np.arange(c0, c1) + 0.5) * surface.dx
    lats = surface.lat0 - (np.arange(r0, r1) + 0.5) * surface.dy
    inside = np.zeros((r1 - r0, c1 - c0), dtype=bool)
    for poly in polys:
        for ring in poly:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                rows = (y1 > lats) != (y2 > lats)
                if rows.any():
                    crossing = (x2 - x1) * (lats[rows] - y1) / (y2 - y1) + x1
                    inside[rows] ^= lons[None, :] < crossing[:, None]
    rr, cc = np.nonzero(inside)
    if not rr.size:
        return empty, empty
    top = surface.heights[r0 + rr, c0 + cc]
    lon, lat = lons[cc], lats[rr]
    base = ground.at(lon, lat)
    ok = (top > EMPTY_BELOW) & (base > EMPTY_BELOW)
    return top[ok] - base[ok], base[ok]


# -- measuring buildings ----------------------------------------------------------------------------

MIN_CELLS = 8  # two square metres of footprint: fewer cells say nothing
STANDS_M = 1.5  # a cell this far above the ground has something standing on it
STATS = ("ground", "eave", "mid", "upper", "top", "peak")  # metres, kept in decimetres
SEED = ROOT / "data" / "lidar_buildings_2019.json.gz"


def summarise(above, base, expected_cells: float) -> dict | None:
    """What the cells of one footprint say. Checked against the 2014 survey on 15,000 unchanged
    buildings, the 95th percentile is the survey's roof height (median difference 0.1 m, four in
    five within 2 m) and the 10th its eave height, so those are the measured top and eave; `mid`
    and `upper` (the median and the 75th percentile) say how much of the roof is up there, and
    `peak` is the single highest cell (a spire, a stair tower, a mast). `built` is the share of
    the footprint with something standing on it, and `cover` the share of the footprint the
    laser has a value for."""
    import numpy as np

    if above.size < MIN_CELLS:
        return None
    eave, mid, upper, top = (max(0.0, float(v)) for v in np.percentile(above, [10, 50, 75, 95]))
    return {"ground": round(float(np.median(base)), 1), "eave": round(eave, 1), "mid": round(mid, 1),
            "upper": round(upper, 1), "top": round(top, 1), "peak": round(max(0.0, float(above.max())), 1),
            "built": round(float((above > STANDS_M).mean()), 2),
            "cover": round(min(1.0, above.size / max(expected_cells, 1.0)), 2)}


def _sheets_touched(geom: dict) -> set[tuple[int, int]]:
    pts = [p for poly in _polygons(geom) for p in poly[0]]
    if not pts:
        return set()
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    out = set()
    for lat in (min(lats), max(lats)):
        for lon in (min(lons), max(lons)):
            e, n = to_grid(lat, lon)
            out.add(sheet_of(float(e), float(n)))
    return out


def measure_buildings(buildings: list[dict], sheets: Path | None = None,
                      say: Callable[[str], None] = lambda _: None) -> dict[int, dict]:
    """Every building the sheets on disk cover -> its measured heights (`summarise`). Works a
    sheet at a time, so only one pair of sheets is in memory; a building across a sheet's edge
    waits until every sheet it touches has been read."""
    import numpy as np

    from chatbot.dls import area_m2

    sheets = sheets or lidar_dir()
    by_sheet: dict[tuple[int, int], list[dict]] = {}
    waits: dict[int, set[tuple[int, int]]] = {}
    for b in buildings:
        touched = _sheets_touched(b["geometry"])
        waits[b["id"]] = set(touched)
        for key in touched:
            by_sheet.setdefault(key, []).append(b)
    parts: dict[int, list] = {}
    cell_deg2: dict[int, float] = {}  # a cell's size on the sheet a building was first read from
    out: dict[int, dict] = {}
    for key in sorted(by_sheet):
        name = sheet_name(*key)
        paths = [sheets / PRODUCTS[p] / name for p in ("dsm", "dtm")]
        pair = [Sheet(p) for p in paths] if all(p.exists() for p in paths) else None
        if pair is None:
            say(f"{name}: not on disk, its {len(by_sheet[key])} buildings stay unmeasured there")
        for b in by_sheet[key]:
            oid = b["id"]
            if pair is not None:
                parts.setdefault(oid, []).append(cells_above_ground(b["geometry"], *pair))
                cell_deg2.setdefault(oid, pair[0].dx * pair[0].dy)
            waits[oid].discard(key)
            if waits[oid]:
                continue
            got = parts.pop(oid, [])
            if not got:
                continue
            above = np.concatenate([a for a, _ in got])
            base = np.concatenate([g for _, g in got])
            lat = _polygons(b["geometry"])[0][0][0][1]
            cell_m2 = cell_deg2[oid] * 111320 * math.cos(math.radians(lat)) * 110574
            stats = summarise(above, base, area_m2(b["geometry"]) / cell_m2)
            if stats:
                out[oid] = stats
        if pair is not None:
            say(f"{name}: {len(by_sheet[key])} buildings")
    return out


def _write_gz(path: Path, data: dict) -> None:
    """Compact JSON, gzipped with no file name and no time in it: the same content is the same
    bytes, so measuring again changes the file only when a height changed."""
    import gzip
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps(data, separators=(",", ":")).encode("utf-8"))


def write_seed(measured: dict[int, dict], path: Path, about: dict) -> None:
    """The measured heights as one small file the twin reads: columns of whole numbers
    (decimetres and per cent), by building id."""
    ids = sorted(measured)
    data: dict = {"about": about, "ids": ids}
    for k in STATS:
        data[k] = [round(measured[i][k] * 10) for i in ids]
    for k in ("built", "cover"):
        data[k] = [round(measured[i][k] * 100) for i in ids]
    _write_gz(path, data)


class Measured:
    """The buildings' measured heights, read once from the seed file when first asked."""

    def __init__(self, path: Path | None = None):
        self.path = path or SEED
        self._rows: dict[int, tuple] | None = None
        self.about: dict = {}

    def _load(self) -> dict[int, tuple]:
        if self._rows is None:
            import gzip
            import json

            try:
                with gzip.open(self.path, "rb") as f:
                    data = json.loads(f.read().decode("utf-8"))
                cols = [data[k] for k in (*STATS, "built", "cover")]
                self._rows = dict(zip(data["ids"], zip(*cols)))
                self.about = data.get("about") or {}
            except (OSError, ValueError, KeyError):
                self._rows = {}  # no seed: nothing is measured, and nothing is guessed
        return self._rows

    def __len__(self) -> int:
        return len(self._load())

    def of(self, oid: int) -> dict | None:
        """One building's measured heights (m), or None when the flight did not measure it."""
        row = self._load().get(int(oid))
        if row is None:
            return None
        out = {k: v / 10 for k, v in zip(STATS, row)}
        out["built"], out["cover"] = row[-2] / 100, row[-1] / 100
        return out


# -- 2014 against 2019 ------------------------------------------------------------------------------

CHANGE_M = 3.0  # a floor: differences below it are within what the two surveys disagree by
GONE_BUILT = 0.15  # less of the footprint than this still stands
SURE_COVER = 0.6  # with less of the footprint measured, nothing is said
SMALL_M2 = 30.0  # a shed or a garage: too small to tell its roof from the tree over it
HOUSE_M2 = 50.0  # what the map and the answers show by default: no sheds, no garages


def compare(surveyed_m: float, m: dict, area_m2: float | None = None) -> dict:
    """The 2014 survey's roof height against what the 2019 flight measured on the same footprint.

    `state` is 'gone' (almost nothing stands there), 'taller' or 'lower' (by a floor or more),
    'same', or 'unknown' when too little of the footprint was measured. The rules are cautious,
    because the survey gives one height for a building and the laser thousands:

    - taller needs the measured top a floor above the survey and a quarter of the footprint
      above the old roof, so a tree leaning over a house is not an extra floor;
    - lower needs even the highest cell a floor below the survey, so a church is not lower
      for having a spire that is one part in a hundred of its footprint;
    - a footprint under SMALL_M2 can only be gone or the same: a tree over a shed and a
      taller shed look alike from above.

    `by_m` is the measured top minus the surveyed roof."""
    by = round(m["top"] - surveyed_m, 1)
    small = area_m2 is not None and area_m2 < SMALL_M2
    if m["cover"] < SURE_COVER:
        state = "unknown"
    elif m["built"] < GONE_BUILT:
        state = "gone"
    elif small:
        state = "same"
    elif by >= CHANGE_M and m["upper"] - surveyed_m >= 1.0:
        state = "taller"
    elif m["peak"] - surveyed_m <= -CHANGE_M:
        state = "lower"
    else:
        state = "same"
    return {"state": state, "by_m": by}


CHANGED = ("gone", "taller", "lower")
CHANGES_REF = "height_changes"  # the map layer of every changed building
CHANGES = ROOT / "data" / "lidar_changes_2019.json.gz"


def changes_of(buildings: list[dict], measured: dict[int, dict]) -> tuple[list[dict], dict[str, int]]:
    """The buildings that differ from the 2014 survey by a floor or more, each with its footprint
    (so a map can draw them without asking DLS again), and how many buildings fell in each
    state, the unchanged ones included."""
    from chatbot.dls import area_m2, centroid

    rows, counts = [], dict.fromkeys(("same", *CHANGED, "unknown"), 0)
    for b in buildings:
        m = measured.get(b["id"])
        if m is None:
            continue
        p = b["properties"]
        area = p.get("a") or area_m2(b["geometry"])
        c = compare(p["h"], m, area)
        counts[c["state"]] += 1
        centre = centroid(b["geometry"])
        if c["state"] not in CHANGED or centre is None:
            continue
        rows.append({"id": b["id"], "state": c["state"], "by_m": c["by_m"], "was_m": p["h"],
                     "now_m": m["top"], "built": m["built"], "use": p.get("t"), "floors": p.get("f"),
                     "area_m2": round(area),
                     "lon": round(centre[0], 6), "lat": round(centre[1], 6),
                     "geometry": b["geometry"]})
    rows.sort(key=lambda r: r["id"])
    return rows, counts


def write_changes(rows: list[dict], counts: dict[str, int], path: Path, about: dict) -> None:
    _write_gz(path, {"about": about, "counts": counts, "changes": rows})


class Changes:
    """The buildings that changed between the survey and the flight, read once when first asked."""

    def __init__(self, path: Path | None = None):
        self.path = path or CHANGES
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            import gzip
            import json

            try:
                with gzip.open(self.path, "rb") as f:
                    self._data = json.loads(f.read().decode("utf-8"))
            except (OSError, ValueError):
                self._data = {"about": {}, "counts": {}, "changes": []}
        return self._data

    @property
    def rows(self) -> list[dict]:
        return self._load()["changes"]

    @property
    def counts(self) -> dict[str, int]:
        return self._load()["counts"]

    @property
    def about(self) -> dict:
        return self._load()["about"]


class Flight:
    """What the twin holds of the 2019 flight: every building's measured heights, and the
    buildings that changed since the survey. Both come from small files in data/, so the twin
    never needs the sheets themselves."""

    def __init__(self, seed: Path | None = None, changes: Path | None = None):
        self.measured = Measured(seed)
        self.changes = Changes(changes)


def change_words(r: dict) -> str:
    """One changed building in a line: what the flight found against what the survey had."""
    if r["state"] == "gone":
        return (f"Nothing stands on most of it in 2019. The 2014 survey has a "
                f"{r['was_m']:g} m building here")
    word = "Taller" if r["state"] == "taller" else "Lower"
    return (f"{word} by {abs(r['by_m']):g} m: {r['now_m']:g} m at the top in 2019, "
            f"{r['was_m']:g} m in the 2014 survey")


def layer_features(rows: list[dict], colors: dict[str, str]) -> list[dict]:
    """Changed buildings as footprints for the map: drawn at their 2019 height, and the gone
    ones at the height they had, coloured by what changed."""
    out = []
    for r in rows:
        title = f"{r.get('use') or 'Building'} · DLS 3D #{r['id']}"
        detail = " · ".join(x for x in (r.get("floors") and f"{r['floors']} floors in the survey",
                                        f"{r['area_m2']:,} m² footprint") if x)
        out.append({"type": "Feature", "geometry": r["geometry"], "properties": {
            "id": f"B{r['id']}", "bid": r["id"], "_kind": "footprint",
            "label": r.get("use") or "building", "state": r["state"], "color": colors[r["state"]],
            "height_m": r["was_m"] if r["state"] == "gone" else r["now_m"],
            "height_2014_m": r["was_m"], "height_2019_m": r["now_m"], "by_m": r["by_m"],
            "popup": "\n".join((title, change_words(r), detail))}})
    return out


# -- command line -----------------------------------------------------------------------------------


def _bbox(text: str) -> tuple[float, float, float, float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("a box is lon_min,lat_min,lon_max,lat_max")
    return tuple(parts)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatbot.lidar", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="download the DLS sheets a box needs (kept out of git)")
    f.add_argument("--bbox", type=_bbox, default=NICOSIA_BBOX, help="lon_min,lat_min,lon_max,lat_max")
    f.add_argument("--out", type=Path, default=None, help="where to keep them (default: data/dls_lidar)")
    m = sub.add_parser("measure", help="measure every DLS building the sheets cover, into the seed file")
    m.add_argument("--bbox", type=_bbox, default=NICOSIA_BBOX, help="lon_min,lat_min,lon_max,lat_max")
    m.add_argument("--sheets", type=Path, default=None, help="where the sheets are (default: data/dls_lidar)")
    m.add_argument("--out", type=Path, default=SEED, help="the seed file to write")
    m.add_argument("--changes", type=Path, default=CHANGES, help="the file of changed buildings to write")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            done = fetch_sheets(args.bbox, args.out)
            print(f"{done['fetched']} fetched ({done['bytes'] / 1e9:.2f} GB), {done['kept']} already "
                  f"here, {done['absent']} not flown, in {args.out or lidar_dir()}")
        elif args.command == "measure":
            from chatbot.dls import DLS
            from chatbot.web import WebError

            try:
                buildings = DLS(Settings()).buildings_in_bbox(args.bbox)
            except WebError as e:
                raise LidarError(f"the DLS buildings could not be read: {e}") from None
            print(f"{len(buildings)} DLS buildings in the box")
            measured = measure_buildings(buildings, args.sheets, say=print)
            about = {"source": "Department of Lands and Surveys, surface and terrain models of 2019",
                     "licence": "CC BY 4.0", "url": "https://www.data.gov.cy/el/dataset/ypsometria-inspire",
                     "bbox": list(args.bbox), "buildings": len(buildings), "measured": len(measured)}
            write_seed(measured, args.out, {
                **about, "what": "DLS 3D buildings as the 2019 LiDAR flight measured them",
                "units": "ground, eave, mid, upper, top, peak in decimetres; built and cover in per cent"})
            print(f"{len(measured)} of {len(buildings)} measured, written to {args.out} "
                  f"({args.out.stat().st_size / 1e6:.2f} MB)")
            rows, counts = changes_of(buildings, measured)
            write_changes(rows, counts, args.changes, {
                **about, "what": "DLS 3D buildings that differ from the 2014 survey by a floor or more"})
            print(f"{counts}; {len(rows)} changed, written to {args.changes} "
                  f"({args.changes.stat().st_size / 1e6:.2f} MB)")
    except LidarError as e:
        print(f"Stopped: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Landsat 8/9 summer morning ground surface temperature via Planetary Computer STAC.

Composites clear ``landsat-c2-l2`` ``lwir11`` scenes (``eo:cloud_cover < 10``) over Greater
Nicosia (33.24, 35.07, 33.48, 35.23) using HTTP Range requests on Cloud-Optimized GeoTIFF
tiles, masked by ``qa_pixel``, into a compact median surface-temperature grid.

HONESTY CRITICAL: Landsat overpasses Nicosia around 10:30 local time, measuring ground skin
(surface) temperature rather than shaded air temperature. Every readout and label must state
this plainly.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import math
import struct
import threading
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2"
SAS_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
DEFAULT_SUMMER_RANGE = "2026-06-01T00:00:00Z/2026-09-30T23:59:59Z"
NICOSIA_BBOX = (33.24, 35.07, 33.48, 35.23)
NODATA_U16 = 65535
DEFAULT_NX = 220
DEFAULT_NY = 160
# Landsat C2 L2 Collection-2 ST_B10 standard calibration
DEFAULT_SCALE = 0.00341802
DEFAULT_OFFSET = 149.0
# QA_PIXEL bitmask: 0=Fill, 1=Dilated Cloud, 2=Cirrus, 3=Cloud, 4=Cloud Shadow, 7=Water
QA_EXCLUDE_MASK = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 7)

HEAT_BANDS = [
    {"to": 32.0, "label": "Cooler (<32 °C)"},
    {"to": 36.0, "label": "Mild (32 to 36 °C)"},
    {"to": 40.0, "label": "Warm (36 to 40 °C)"},
    {"to": 44.0, "label": "Hot (40 to 44 °C)"},
    {"to": None, "label": "Extreme (44+ °C)"},
]
HEAT_COLORS_NIGHT = ["#3b82f6", "#22c55e", "#eab308", "#f97316", "#ff5a4e"]
HEAT_COLORS_DAY = ["#2563eb", "#16a34a", "#ca8a04", "#ea580c", "#dc2626"]


def latlon_to_utm36n(lat, lon):
    """WGS84 (lat, lon) -> UTM zone 36N (easting, northing) in metres (scalars or ndarrays)."""
    a = 6378137.0
    f = 1.0 / 298.257223563
    k0 = 0.9996
    lon0 = math.radians(33.0)
    e2 = f * (2.0 - f)
    ep2 = e2 / (1.0 - e2)
    p = np.radians(lat)
    lam = np.radians(lon)
    sin_p, cos_p, tan_p = np.sin(p), np.cos(p), np.tan(p)
    n = a / np.sqrt(1.0 - e2 * sin_p * sin_p)
    t = tan_p * tan_p
    c = ep2 * cos_p * cos_p
    a_term = (lam - lon0) * cos_p
    m = a * (
        (1.0 - e2 / 4.0 - 3.0 * e2**2 / 64.0 - 5.0 * e2**3 / 256.0) * p
        - (3.0 * e2 / 8.0 + 3.0 * e2**2 / 32.0 + 45.0 * e2**3 / 1024.0) * np.sin(2.0 * p)
        + (15.0 * e2**2 / 256.0 + 45.0 * e2**3 / 1024.0) * np.sin(4.0 * p)
        - (35.0 * e2**3 / 3072.0) * np.sin(6.0 * p)
    )
    east = 500000.0 + k0 * n * (
        a_term
        + (1.0 - t + c) * a_term**3 / 6.0
        + (5.0 - 18.0 * t + t * t + 72.0 * c - 58.0 * ep2) * a_term**5 / 120.0
    )
    north = k0 * (
        m
        + n
        * tan_p
        * (
            a_term * a_term / 2.0
            + (5.0 - t + 9.0 * c + 4.0 * c * c) * a_term**4 / 24.0
            + (61.0 - 58.0 * t + t * t + 600.0 * c - 330.0 * ep2) * a_term**6 / 720.0
        )
    )
    return east, north


# -- Minimal pure-Python COG (Cloud-Optimized GeoTIFF) reader -----------------------------------


_TIFF_TYPES = {
    1: ("B", 1),
    2: ("s", 1),
    3: ("H", 2),
    4: ("I", 4),
    5: ("II", 8),
    8: ("h", 2),
    9: ("i", 4),
    12: ("d", 8),
    16: ("Q", 8),
}


_THREAD_CONNS = threading.local()


def _keepalive_read_range(url: str, start: int, end: int, user_agent: str = "smart-nicosia-chatbot/0.1") -> bytes:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    path = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
    pool: dict[str, http.client.HTTPSConnection] = getattr(_THREAD_CONNS, "pool", None)
    if pool is None:
        pool = {}
        _THREAD_CONNS.pool = pool
    for attempt in range(2):
        conn = pool.get(host)
        if conn is None:
            conn = http.client.HTTPSConnection(host, timeout=30)
            pool[host] = conn
        try:
            conn.request("GET", path, headers={"Range": f"bytes={start}-{end}", "User-Agent": user_agent})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status in (200, 206):
                return data
            raise OSError(f"HTTP {resp.status} for range {start}-{end}")
        except (http.client.HTTPException, OSError):
            pool.pop(host, None)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            if attempt == 1:
                break
    return http_read_range(url, start, end, user_agent=user_agent)


def http_read_range(url: str, start: int, end: int, user_agent: str = "smart-nicosia-chatbot/0.1") -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_tiff_tags(read_range: Callable[[int, int], bytes]) -> dict[int, Any]:
    """Read IFD0 tags of a tiled Classic TIFF (or BigTIFF) using minimal Range requests."""
    hdr = read_range(0, 65535)
    if len(hdr) < 8:
        raise ValueError("TIFF header too short")
    endian = "<" if hdr[:2] == b"II" else ">" if hdr[:2] == b"MM" else ""
    if not endian:
        raise ValueError(f"not a TIFF header: {hdr[:4]!r}")
    (magic,) = struct.unpack(endian + "H", hdr[2:4])
    if magic != 42:
        raise ValueError(f"unsupported TIFF magic {magic}")
    (ifd_off,) = struct.unpack(endian + "I", hdr[4:8])
    if ifd_off + 2 > len(hdr):
        hdr = read_range(0, ifd_off + 4096)
    (n_tags,) = struct.unpack(endian + "H", hdr[ifd_off : ifd_off + 2])
    end_ifd = ifd_off + 2 + n_tags * 12 + 4
    if end_ifd > len(hdr):
        hdr = read_range(0, end_ifd)

    tags: dict[int, Any] = {}
    for k in range(n_tags):
        off = ifd_off + 2 + k * 12
        tag, typ, cnt, val_raw = struct.unpack(endian + "HHII", hdr[off : off + 12])
        fmt, sz = _TIFF_TYPES.get(typ, ("B", 1))
        total = cnt * sz
        if total <= 4:
            raw_bytes = hdr[off + 8 : off + 8 + total]
        else:
            raw_bytes = (
                read_range(val_raw, val_raw + total - 1)
                if val_raw + total > len(hdr)
                else hdr[val_raw : val_raw + total]
            )
        if typ == 2:
            tags[tag] = raw_bytes.rstrip(b"\x00").decode("ascii", "replace")
        elif typ == 5:
            vals = struct.unpack(endian + "I" * (2 * cnt), raw_bytes)
            tags[tag] = tuple(vals[i] / vals[i + 1] if vals[i + 1] else 0.0 for i in range(0, len(vals), 2))
        else:
            vals = struct.unpack(endian + fmt * cnt, raw_bytes)
            tags[tag] = vals[0] if cnt == 1 else vals
    tags["_endian"] = endian
    return tags


def read_cog_tiles(
    read_range: Callable[[int, int], bytes],
    meta: dict[int, Any],
    tx0: int,
    tx1: int,
    ty0: int,
    ty1: int,
) -> np.ndarray:
    """Decode uint16/int16 COG tiles covering tile box [tx0..tx1, ty0..ty1]."""
    tw = int(meta[322])
    th = int(meta[323])
    width = int(meta[256])
    ntx = (width + tw - 1) // tw
    offsets = meta[324]
    counts = meta[325]
    if isinstance(offsets, int):
        offsets = (offsets,)
        counts = (counts,)
    compression = int(meta.get(259, 1))
    predictor = int(meta.get(317, 1))
    sample_fmt = int(meta.get(339, 1))
    endian = meta.get("_endian", "<")
    dtype = (f"{endian}i2" if sample_fmt == 2 else f"{endian}u2")

    out = np.zeros(((ty1 - ty0 + 1) * th, (tx1 - tx0 + 1) * tw), dtype=dtype)
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            idx = ty * ntx + tx
            if idx < 0 or idx >= len(offsets):
                continue
            off, cnt = int(offsets[idx]), int(counts[idx])
            if cnt <= 0:
                continue
            raw = read_range(off, off + cnt - 1)
            if compression == 8:
                raw = zlib.decompress(raw)
            elif compression != 1:
                raise ValueError(f"unsupported TIFF compression {compression}")
            arr = np.frombuffer(raw, dtype=dtype).reshape((th, tw)).copy()
            if predictor == 2:
                arr = np.cumsum(arr, axis=1, dtype=dtype)
            oy, ox = (ty - ty0) * th, (tx - tx0) * tw
            out[oy : oy + th, ox : ox + tw] = arr
    return out


def clear_ground_mask(lwir: np.ndarray, qa: np.ndarray) -> np.ndarray:
    """Boolean mask of valid clear land pixels (excluding fill, clouds, cirrus, shadow, water)."""
    return (lwir > 0) & ((qa & QA_EXCLUDE_MASK) == 0)


# -- Planetary Computer STAC + compositing ------------------------------------------------------


def search_landsat_scenes(
    bbox: tuple[float, float, float, float] = NICOSIA_BBOX,
    datetime_range: str = DEFAULT_SUMMER_RANGE,
    max_cloud_cover: float = 10.0,
    limit: int = 50,
    user_agent: str = "smart-nicosia-chatbot/0.1",
) -> list[dict]:
    body = json.dumps({
        "collections": ["landsat-c2-l2"],
        "bbox": list(bbox),
        "datetime": datetime_range,
        "query": {"eo:cloud_cover": {"lt": max_cloud_cover}},
        "limit": limit,
    }).encode("utf-8")
    req = urllib.request.Request(
        STAC_SEARCH_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("features") or []


def fetch_sas_token(user_agent: str = "smart-nicosia-chatbot/0.1") -> str:
    req = urllib.request.Request(SAS_TOKEN_URL, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))["token"]


def sign_asset(href: str, token: str | None = None, user_agent: str = "smart-nicosia-chatbot/0.1") -> str:
    if token:
        sep = "&" if "?" in href else "?"
        return f"{href}{sep}{token}"
    url = f"{SAS_SIGN_URL}?href={urllib.parse.quote(href, safe='')}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))["href"]


THERMAL_CONSTANTS = {
    "LC08": (774.8853, 1321.0789),
    "LC09": (799.0284, 1329.2405),
}


def _read_asset_tiles(
    href: str,
    east_grid: np.ndarray,
    north_grid: np.ndarray,
    range_reader: Callable[[str, int, int], bytes],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Read COG tiles for ``href`` over ``(east_grid, north_grid)``."""
    rr = lambda s, e: range_reader(href, s, e)
    meta = parse_tiff_tags(rr)
    sx, sy, _ = meta[33550]
    ox, oy = float(meta[33922][3]), float(meta[33922][4])
    width, height = int(meta[256]), int(meta[257])
    tw, th = int(meta[322]), int(meta[323])
    cols_i = np.floor((east_grid - ox) / sx).astype(np.int64)
    rows_i = np.floor((oy - north_grid) / sy).astype(np.int64)
    in_scene = (cols_i >= 0) & (cols_i < width) & (rows_i >= 0) & (rows_i < height)
    if not np.any(in_scene):
        return np.empty(0), in_scene, cols_i, rows_i
    c0, c1 = int(cols_i[in_scene].min()), int(cols_i[in_scene].max())
    r0, r1 = int(rows_i[in_scene].min()), int(rows_i[in_scene].max())
    tx0, tx1 = c0 // tw, c1 // tw
    ty0, ty1 = r0 // th, r1 // th
    tiles = read_cog_tiles(rr, meta, tx0, tx1, ty0, ty1)
    lc = cols_i - tx0 * tw
    lr = rows_i - ty0 * th
    valid_idx = in_scene & (lr >= 0) & (lr < tiles.shape[0]) & (lc >= 0) & (lc < tiles.shape[1])
    return tiles[lr[valid_idx], lc[valid_idx]], valid_idx, cols_i, rows_i


def sample_scene_onto_grid(
    scene: dict,
    east_grid: np.ndarray,
    north_grid: np.ndarray,
    range_reader: Callable[[str, int, int], bytes],
) -> tuple[np.ndarray, int]:
    """Read Greater Nicosia tiles for one Landsat scene and project onto (ny, nx) UTM grid.

    Uses USGS ``lwir11`` (``ST_B10``) directly wherever valid, and fills ASTER-GED emissivity gaps
    over Cyprus using USGS L2 single-channel RTE (``trad``, ``urad``, ``drad``, ``atran``, and
    NDVI-adjusted urban/vegetated surface emissivity where ``emis`` is nodata).
    """
    assets = scene.get("assets") or {}
    lwir_asset = assets.get("lwir11") or {}
    qa_asset = assets.get("qa_pixel") or {}
    lwir_href = lwir_asset.get("href")
    qa_href = qa_asset.get("href")
    if not lwir_href or not qa_href:
        return np.full(east_grid.shape, np.nan, dtype=np.float32), 0

    lw_vals, valid_idx, _, _ = _read_asset_tiles(lwir_href, east_grid, north_grid, range_reader)
    if not np.any(valid_idx):
        return np.full(east_grid.shape, np.nan, dtype=np.float32), 0
    qa_vals, qa_idx, _, _ = _read_asset_tiles(qa_href, east_grid, north_grid, range_reader)
    if qa_vals.shape != lw_vals.shape:
        return np.full(east_grid.shape, np.nan, dtype=np.float32), 0

    clear_ground = (qa_vals & QA_EXCLUDE_MASK) == 0
    if not np.any(clear_ground):
        return np.full(east_grid.shape, np.nan, dtype=np.float32), 0

    temps = np.full(lw_vals.shape, np.nan, dtype=np.float64)
    has_st = clear_ground & (lw_vals > 0)
    if np.any(has_st):
        rb = (lwir_asset.get("raster:bands") or [{}])[0]
        scale = float(rb.get("scale") or DEFAULT_SCALE)
        offset = float(rb.get("offset") or DEFAULT_OFFSET)
        temps[has_st] = lw_vals[has_st].astype(np.float64) * scale + offset - 273.15

    # Fill ASTER GED gap (lwir11 == 0 on clear land) via USGS L2 thermal radiance + atmospheric bands
    need_rte = clear_ground & (lw_vals == 0)
    rte_names = ("trad", "urad", "drad", "atran")
    if np.any(need_rte) and all((assets.get(k) or {}).get("href") for k in rte_names):
        bands: dict[str, np.ndarray] = {}
        for k in rte_names:
            vals, idx, _, _ = _read_asset_tiles(assets[k]["href"], east_grid, north_grid, range_reader)
            if vals.shape != lw_vals.shape:
                bands = {}
                break
            bands[k] = vals.astype(np.float64)
        if bands:
            eps = np.full(lw_vals.shape, 0.968, dtype=np.float64)
            if (assets.get("emis") or {}).get("href"):
                em_vals, _, _, _ = _read_asset_tiles(assets["emis"]["href"], east_grid, north_grid, range_reader)
                if em_vals.shape == lw_vals.shape:
                    em_ok = em_vals > 0
                    eps[em_ok] = em_vals[em_ok].astype(np.float64) * 0.0001
            # Refine missing-emissivity land pixels with NDVI vegetation fraction if red & nir08 exist
            if (assets.get("red") or {}).get("href") and (assets.get("nir08") or {}).get("href"):
                red_v, _, _, _ = _read_asset_tiles(assets["red"]["href"], east_grid, north_grid, range_reader)
                nir_v, _, _, _ = _read_asset_tiles(assets["nir08"]["href"], east_grid, north_grid, range_reader)
                if red_v.shape == lw_vals.shape and nir_v.shape == lw_vals.shape:
                    sr_ok = (red_v > 0) & (nir_v > 0)
                    r_sr = red_v.astype(np.float64) * 0.0000275 - 0.2
                    n_sr = nir_v.astype(np.float64) * 0.0000275 - 0.2
                    denom = n_sr + r_sr
                    ndvi = np.where(sr_ok & (denom > 0.01), (n_sr - r_sr) / np.maximum(denom, 1e-6), 0.0)
                    pv = np.clip((ndvi - 0.12) / 0.52, 0.0, 1.0) ** 2
                    eps_ndvi = 0.958 + 0.028 * pv
                    target = need_rte & sr_ok & (em_vals <= 0 if (assets.get("emis") or {}).get("href") and em_vals.shape == lw_vals.shape else True)
                    eps[target] = eps_ndvi[target]

            tau = bands["atran"] * 0.0001
            l_toa = bands["trad"] * 0.001
            l_up = bands["urad"] * 0.001
            l_dn = bands["drad"] * 0.001
            rte_ok = need_rte & (tau > 0.1) & (eps > 0.8) & (bands["trad"] > 0)
            l_t = np.where(
                rte_ok,
                (l_toa - l_up - tau * (1.0 - eps) * l_dn) / np.maximum(tau * eps, 1e-6),
                np.nan,
            )
            sat_key = "LC09" if "LC09" in str(scene.get("id") or "") else "LC08"
            k1, k2 = THERMAL_CONSTANTS[sat_key]
            valid_rad = rte_ok & np.isfinite(l_t) & (l_t > 0.1)
            temps[valid_rad] = k2 / np.log(k1 / l_t[valid_rad] + 1.0) - 273.15

    plausible = np.isfinite(temps) & (temps >= 5.0) & (temps <= 75.0)
    if not np.any(plausible):
        return np.full(east_grid.shape, np.nan, dtype=np.float32), 0

    out = np.full(east_grid.shape, np.nan, dtype=np.float32)
    flat = out[valid_idx]
    flat[plausible] = temps[plausible].astype(np.float32)
    out[valid_idx] = flat
    return out, int(plausible.sum())


def encode_heat_grid(arr_c: np.ndarray, bbox: tuple[float, float, float, float], meta: dict | None = None) -> dict:
    """Encode a (ny, nx) float Celsius grid into compact zlib+base64 uint16 (°C * 100)."""
    ny, nx = arr_c.shape
    finite = np.isfinite(arr_c)
    scaled = np.where(finite, np.clip(np.round(arr_c * 100.0), 0, 65534), NODATA_U16).astype("<u2")
    raw_b64 = base64.b64encode(zlib.compress(scaled.tobytes(), level=9)).decode("ascii")
    vals = arr_c[finite]
    return {
        "bbox": [round(x, 6) for x in bbox],
        "nx": int(nx),
        "ny": int(ny),
        "scale": 0.01,
        "nodata": NODATA_U16,
        "grid_b64": raw_b64,
        "min_c": round(float(vals.min()), 1) if vals.size else None,
        "median_c": round(float(np.median(vals)), 1) if vals.size else None,
        "p90_c": round(float(np.percentile(vals, 90)), 1) if vals.size else None,
        "max_c": round(float(vals.max()), 1) if vals.size else None,
        **(meta or {}),
    }


def decode_heat_grid(grid: dict) -> np.ndarray:
    """Decode compact heat grid dict back into a (ny, nx) float32 Celsius array (NaN for nodata)."""
    raw = zlib.decompress(base64.b64decode(grid["grid_b64"]))
    arr = np.frombuffer(raw, dtype="<u2").reshape((int(grid["ny"]), int(grid["nx"])))
    scale = float(grid.get("scale") or 0.01)
    nodata = int(grid.get("nodata") if grid.get("nodata") is not None else NODATA_U16)
    out = arr.astype(np.float32) * scale
    out[arr == nodata] = np.nan
    return out


def composite_summer_lst(
    bbox: tuple[float, float, float, float] = NICOSIA_BBOX,
    datetime_range: str = DEFAULT_SUMMER_RANGE,
    max_cloud_cover: float = 10.0,
    nx: int = DEFAULT_NX,
    ny: int = DEFAULT_NY,
    scenes: list[dict] | None = None,
    range_reader: Callable[[str, int, int], bytes] | None = None,
    user_agent: str = "smart-nicosia-chatbot/0.1",
    say: Callable[[str], None] = log.info,
) -> dict:
    """Composite clear summer Landsat 8/9 surface temperature scenes over ``bbox``."""
    if scenes is None:
        scenes = search_landsat_scenes(bbox, datetime_range, max_cloud_cover, user_agent=user_agent)
    token = fetch_sas_token(user_agent=user_agent) if range_reader is None and scenes else None
    reader = range_reader or (lambda url, s, e: _keepalive_read_range(url, s, e, user_agent=user_agent))
    sign_keys = ("lwir11", "qa_pixel", "trad", "urad", "drad", "atran", "emis", "red", "nir08")

    w, s, e, n = bbox
    lons = w + (np.arange(nx) + 0.5) * (e - w) / nx
    lats = n - (np.arange(ny) + 0.5) * (n - s) / ny
    lon_2d, lat_2d = np.meshgrid(lons, lats)
    east_grid, north_grid = latlon_to_utm36n(lat_2d, lon_2d)

    def _prepare(sc: dict) -> dict:
        if token is None:
            return sc
        return {
            **sc,
            "assets": {
                k: ({**v, "href": sign_asset(v["href"], token)} if k in sign_keys and "href" in v else v)
                for k, v in (sc.get("assets") or {}).items()
            },
        }

    def _run_one(sc: dict) -> tuple[dict, np.ndarray | None, int, str | None]:
        try:
            layer, n_px = sample_scene_onto_grid(_prepare(sc), east_grid, north_grid, reader)
            return sc, layer, n_px, None
        except Exception as exc:  # noqa: BLE001
            return sc, None, 0, str(exc)

    if range_reader is None and len(scenes) > 1:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_run_one, scenes))
    else:
        results = [_run_one(sc) for sc in scenes]

    stack: list[np.ndarray] = []
    used_ids: list[str] = []
    dates: list[str] = []
    for sc, layer, n_px, err in results:
        if err:
            say(f"skipped scene {sc.get('id')}: {err}")
            continue
        if n_px > 0 and layer is not None:
            stack.append(layer)
            used_ids.append(str(sc.get("id") or ""))
            dt = str((sc.get("properties") or {}).get("datetime") or "")[:10]
            if dt:
                dates.append(dt)
            say(f"scene {sc.get('id')}: {n_px} clear pixels")

    if not stack:
        raise ValueError("no clear Landsat thermal pixels found for Greater Nicosia")

    arr_stack = np.stack(stack, axis=0)
    median_c = np.full(east_grid.shape, np.nan, dtype=np.float32)
    any_valid = np.isfinite(arr_stack).any(axis=0)
    if np.any(any_valid):
        with np.errstate(all="ignore"):
            median_c[any_valid] = np.nanmedian(arr_stack[:, any_valid], axis=0).astype(np.float32)

    return encode_heat_grid(
        median_c,
        bbox,
        meta={
            "scenes_found": len(scenes),
            "scenes_used": len(used_ids),
            "scene_ids": used_ids,
            "date_from": min(dates) if dates else None,
            "date_to": max(dates) if dates else None,
            "overpass_local": "~10:30",
            "temperature_kind": "morning_ground_surface",
            "note": "Landsat 8/9 C2 L2 lwir11 morning ground surface temperature (~10:30 local), not air temperature",
        },
    )


def sample_heat_grid(grid: dict, arr: np.ndarray, lat: float, lon: float) -> float | None:
    """Nearest valid surface temperature (°C) in ``grid`` at (lat, lon)."""
    w, s, e, n = grid["bbox"]
    nx, ny = int(grid["nx"]), int(grid["ny"])
    if not (w <= lon <= e and s <= lat <= n):
        return None
    col = min(nx - 1, max(0, int((lon - w) / (e - w) * nx)))
    row = min(ny - 1, max(0, int((n - lat) / (n - s) * ny)))
    v = float(arr[row, col])
    if math.isfinite(v):
        return round(v, 1)
    # Tiny 3x3 fallback if single cell is masked
    sub = arr[max(0, row - 1) : min(ny, row + 2), max(0, col - 1) : min(nx, col + 2)]
    fin = sub[np.isfinite(sub)]
    return round(float(np.median(fin)), 1) if fin.size else None


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def hex_heat_stats(grid: dict, arr: np.ndarray, ring: list[list[float]], center: list[float]) -> dict:
    """Compute surface-temperature statistics for all grid cells inside a hexagon polygon."""
    w, s, e, n = grid["bbox"]
    nx, ny = int(grid["nx"]), int(grid["ny"])
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    c0 = max(0, int(math.floor((min(xs) - w) / (e - w) * nx)))
    c1 = min(nx - 1, int(math.ceil((max(xs) - w) / (e - w) * nx)))
    r0 = max(0, int(math.floor((n - max(ys)) / (n - s) * ny)))
    r1 = min(ny - 1, int(math.ceil((n - min(ys)) / (n - s) * ny)))

    vals: list[float] = []
    dx = (e - w) / nx
    dy = (n - s) / ny
    for r in range(r0, r1 + 1):
        lat = n - (r + 0.5) * dy
        for c in range(c0, c1 + 1):
            v = float(arr[r, c])
            if not math.isfinite(v):
                continue
            lon = w + (c + 0.5) * dx
            if _point_in_ring(lon, lat, ring):
                vals.append(v)

    if not vals:
        pt = sample_heat_grid(grid, arr, center[1], center[0])
        if pt is None:
            return {"lst_median_c": None, "lst_mean_c": None, "lst_min_c": None, "lst_max_c": None, "lst_p90_c": None, "lst_samples": 0}
        return {"lst_median_c": pt, "lst_mean_c": pt, "lst_min_c": pt, "lst_max_c": pt, "lst_p90_c": pt, "lst_samples": 1}

    a = np.asarray(vals, dtype=np.float32)
    return {
        "lst_median_c": round(float(np.median(a)), 1),
        "lst_mean_c": round(float(np.mean(a)), 1),
        "lst_min_c": round(float(a.min()), 1),
        "lst_max_c": round(float(a.max()), 1),
        "lst_p90_c": round(float(np.percentile(a, 90)), 1),
        "lst_samples": int(a.size),
    }

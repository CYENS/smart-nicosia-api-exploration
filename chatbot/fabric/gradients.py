"""CyNAP road network gradients (traffic4cyprus.org.cy, CC BY 4.0).

Consumes the National Access Point road gradients feed (100 m road points with slope %):
- Primary DATEX II v3 XML:
  https://fixcyprus.cy/gnosis/open/api/nap/datasets/road_network_gradients_hundred_m/
- GeoJSON mirror:
  https://fixcyprus.cy/gnosis/open/api/nap/map/road_network_gradients_hundred_m/

Cached daily in ``data/chatbot_cache/fabric/gradients.json``, clipped defensively to Greater
Nicosia (33.24, 35.07, 33.48, 35.23) to flag moderate (>5%) and steep (>8%) segments for
step-free/wheelchair accessibility and cycling effort.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from chatbot.settings import CACHE_DIR, Settings

log = logging.getLogger(__name__)

NICOSIA_FABRIC_BBOX = (33.24, 35.07, 33.48, 35.23)
XML_URL = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/road_network_gradients_hundred_m/"
GEOJSON_URL = "https://fixcyprus.cy/gnosis/open/api/nap/map/road_network_gradients_hundred_m/"
DATASET_URL = "https://traffic4cyprus.org.cy/dataset/road_network_gradients"

MODERATE_PCT = 5.0
STEEP_PCT = 8.0
DAILY_TTL_S = 24 * 3600
RETRY_AFTER_S = 1800

SLOPE_COLORS = {
    "gentle": "#22c55e",
    "moderate": "#f5a524",
    "steep": "#ff5a4e",
}


def classify_slope(slope_pct: float) -> str:
    if slope_pct > STEEP_PCT:
        return "steep"
    if slope_pct > MODERATE_PCT:
        return "moderate"
    return "gentle"


def _in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n


def _float(val: object) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        v = float(str(val).strip())
    except (ValueError, TypeError):
        return None
    return v if -100.0 <= v <= 100.0 else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    t = " ".join(el.text.split())
    return t or None


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def parse_gradients_xml(raw: bytes, bbox: tuple[float, float, float, float] = NICOSIA_FABRIC_BBOX) -> dict:
    """Stream-parse DATEX II RoadElementsPublication XML and clip to ``bbox``."""
    points: list[dict] = []
    pub_time: str | None = None
    total_island = 0
    skipped = 0
    ctx = ET.iterparse(io.BytesIO(raw), events=("end",))
    for _, elem in ctx:
        name = _local(elem.tag)
        if name == "publicationTime" and pub_time is None:
            pub_time = _text(elem)
            continue
        if name != "roadElement":
            continue
        total_island += 1
        try:
            ver = _child(elem, "versionedIdentifier")
            ident = _text(_child(ver, "identifier")) if ver is not None else None
            road_code = _text(_child(elem, "roadNumber"))
            road_cls = _text(_child(elem, "roadClassification"))
            name_en: str | None = None
            name_el: str | None = None
            for rn in _children(elem, "roadName"):
                val = _text(_child(rn, "value"))
                lang = (_text(_child(rn, "lang")) or "").lower()
                if not val:
                    continue
                if lang == "el" and name_el is None:
                    name_el = val
                elif name_en is None:
                    name_en = val
            km_ref: str | None = None
            junction: str | None = None
            for desc in _children(elem, "roadElementDescription"):
                val = _text(_child(desc, "value")) or ""
                if val.startswith("KM Reference:"):
                    km_ref = val.split(":", 1)[1].strip() or None
                elif val.startswith("Junction Name:"):
                    junction = val.split(":", 1)[1].strip() or None
            grad = _child(elem, "gradient")
            slope = _float(_text(_child(grad, "gradientPercentage"))) if grad is not None else None
            loc = _child(elem, "location")
            pt = _child(loc, "pointByCoordinates") if loc is not None else None
            coords = _child(pt, "pointCoordinates") if pt is not None else None
            lat = _float(_text(_child(coords, "latitude"))) if coords is not None else None
            lon = _float(_text(_child(coords, "longitude"))) if coords is not None else None
            if slope is None or lat is None or lon is None:
                skipped += 1
                continue
            if not _in_bbox(lon, lat, bbox):
                continue
            slope = abs(round(slope, 1))
            pid = f"grad-{ident or len(points)}"
            points.append({
                "id": pid,
                "road_code": road_code,
                "road_class": road_cls,
                "road_name": name_en or name_el or road_code or "Unnamed road",
                **({"road_name_el": name_el} if name_el and name_el != name_en else {}),
                **({"junction": junction} if junction else {}),
                **({"km_ref": km_ref} if km_ref is not None else {}),
                "slope_pct": slope,
                "band": classify_slope(slope),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            })
        finally:
            elem.clear()
    return {
        "source_format": "datex2_xml",
        "published_at": pub_time,
        "island_total": total_island,
        "skipped": skipped,
        "points": points,
    }


def parse_gradients_geojson(raw: bytes | dict, bbox: tuple[float, float, float, float] = NICOSIA_FABRIC_BBOX) -> dict:
    """Parse the GeoJSON mirror of CyNAP gradients and clip to ``bbox``."""
    data = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else raw
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError("gradients GeoJSON missing features list")
    points: list[dict] = []
    skipped = 0
    for idx, feat in enumerate(features):
        if not isinstance(feat, dict):
            skipped += 1
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") if geom.get("type") == "Point" else None
        if not isinstance(coords, list) or len(coords) < 2:
            skipped += 1
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            skipped += 1
            continue
        props = feat.get("properties") or {}
        slope = _float(props.get("Slope") if "Slope" in props else props.get("slope_pct"))
        if slope is None:
            skipped += 1
            continue
        if not _in_bbox(lon, lat, bbox):
            continue
        slope = abs(round(slope, 1))
        fid = feat.get("id") if feat.get("id") is not None else idx
        code = props.get("local_road_code") or props.get("road_code")
        km_ref = props.get("KM_Ref") or props.get("km_ref")
        points.append({
            "id": f"grad-{fid}",
            "road_code": str(code) if code else None,
            "road_class": str(code)[0] if code else None,
            "road_name": str(props.get("road_name") or code or "Unnamed road"),
            **({"km_ref": str(km_ref)} if km_ref is not None else {}),
            "slope_pct": slope,
            "band": classify_slope(slope),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        })
    return {
        "source_format": "geojson",
        "published_at": None,
        "island_total": len(features),
        "skipped": skipped,
        "points": points,
    }


def _default_fetch(url: str, user_agent: str, timeout: float = 45.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise OSError(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OSError(f"unreachable {url}: {getattr(e, 'reason', e)}") from None


def summarize_gradients(points: list[dict]) -> dict:
    if not points:
        return {"total": 0, "gentle": 0, "moderate_gt5": 0, "steep_gt8": 0, "max_slope_pct": None}
    gt5 = sum(1 for p in points if p["slope_pct"] > MODERATE_PCT)
    gt8 = sum(1 for p in points if p["slope_pct"] > STEEP_PCT)
    return {
        "total": len(points),
        "gentle": len(points) - gt5,
        "moderate_gt5": gt5 - gt8,
        "gt5": gt5,
        "steep_gt8": gt8,
        "max_slope_pct": max(p["slope_pct"] for p in points),
    }


class GradientsStore:
    """Daily cached CyNAP road network gradients for Greater Nicosia."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache_dir: Path | None = None,
        fetch: Callable[[str], bytes] | None = None,
        bbox: tuple[float, float, float, float] = NICOSIA_FABRIC_BBOX,
    ):
        s = settings or Settings()
        self.cache_path = (cache_dir or (CACHE_DIR / "fabric")) / "gradients.json"
        self.fetch = fetch or (lambda url: _default_fetch(url, s.user_agent))
        self.bbox = bbox
        self._lock = threading.Lock()
        self._points: list[dict] = []
        self.fetched_at: float = 0.0
        self.published_at: str | None = None
        self.source_mode: str | None = None
        self.island_total: int = 0
        self.error: str | None = None
        self._failed_at: float = 0.0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            pts = data.get("points")
            if isinstance(pts, list):
                self._points = pts
                self.fetched_at = float(data.get("fetched_at") or 0.0)
                self.published_at = data.get("published_at")
                self.source_mode = data.get("source_mode")
                self.island_total = int(data.get("island_total") or len(pts))
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": self.fetched_at,
            "published_at": self.published_at,
            "source_mode": self.source_mode,
            "island_total": self.island_total,
            "bbox": list(self.bbox),
            "licence": "CC BY 4.0, Ministry of Transport, Communications and Works (traffic4cyprus.org.cy)",
            "points": self._points,
        }
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _parse(self, raw: bytes) -> dict:
        stripped = raw.lstrip()
        if stripped.startswith(b"<"):
            return parse_gradients_xml(raw, self.bbox)
        return parse_gradients_geojson(raw, self.bbox)

    def refresh(self, force: bool = False) -> list[dict]:
        now = time.time()
        if not force and self._points and now - self.fetched_at < DAILY_TTL_S:
            return self._points
        if not force and self.error and now - self._failed_at < RETRY_AFTER_S:
            return self._points
        with self._lock:
            now = time.time()
            if not force and self._points and now - self.fetched_at < DAILY_TTL_S:
                return self._points
            errs: list[str] = []
            for url in (XML_URL, GEOJSON_URL):
                try:
                    raw = self.fetch(url)
                    parsed = self._parse(raw)
                    if not parsed["points"] and parsed["island_total"] == 0:
                        raise ValueError("gradients feed returned zero elements")
                    self._points = parsed["points"]
                    self.fetched_at = time.time()
                    self.published_at = parsed.get("published_at")
                    self.source_mode = parsed.get("source_format")
                    self.island_total = int(parsed.get("island_total") or len(self._points))
                    self.error = None
                    self._save()
                    log.info(
                        "cynap gradients: %d points in Greater Nicosia (%s)",
                        len(self._points),
                        self.source_mode,
                    )
                    return self._points
                except Exception as e:  # noqa: BLE001 - try fallback mirror, else keep stale copy
                    errs.append(str(e)[:120])
            self.error = "; ".join(errs) or "gradients feed unavailable"
            self._failed_at = time.time()
            log.warning("cynap gradients refresh failed: %s", self.error)
            return self._points

    def points(self, refresh_if_stale: bool = False) -> list[dict]:
        if refresh_if_stale and (not self._points or time.time() - self.fetched_at >= DAILY_TTL_S):
            return self.refresh()
        return self._points

    def seed(self, points: list[dict], *, source_mode: str = "fixture", fetched_at: float | None = None) -> None:
        self._points = list(points)
        self.source_mode = source_mode
        self.fetched_at = fetched_at or time.time()
        self.error = None

    def health(self) -> dict:
        """Read-only status for /api/sources (never fetches over the network)."""
        if not self._points and not self.error:
            return {"state": "waiting", "as_of": None, "note": "cached daily on first use"}
        summary = summarize_gradients(self._points)
        note = f"{summary['total']:,} points in Greater Nicosia ({summary['gt5']} over 5%, {summary['steep_gt8']} over 8%)"
        if self.error and self._points:
            return {"state": "stale", "as_of": self.fetched_at, "note": f"{note}; refresh failed ({self.error})"}
        if self.error:
            return {"state": "unavailable", "as_of": self._failed_at or None, "note": self.error}
        as_of_iso = None
        if self.published_at:
            try:
                as_of_iso = datetime.fromisoformat(self.published_at).astimezone(timezone.utc).timestamp()
            except ValueError:
                as_of_iso = None
        return {
            "state": "live",
            "as_of": self.fetched_at or as_of_iso,
            "note": note,
            "published_at": self.published_at,
        }

"""Remaining CyNAP ITS infrastructure from FixCyprus / traffic4cyprus.org.cy (CC BY 4.0):
- Static daily-cached site inventories for PWD CCTV cameras (13 in Nicosia) and PWD urban
  traffic loop and cycle detectors (39 in Nicosia).
- Lightweight health/freshness probe for Swarco's `MeasuredDataPublication`, which currently
  publishes 92 site references with no measured flow values.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from chatbot.settings import CACHE_DIR, NICOSIA_BBOX, TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

CCTV_URL = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/its_sensors/CCTV/"
DETECTORS_URL = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/its_sensors/Traffic_Detector/"
SWARCO_URL = "https://www.traffic4cyprus.org.cy/swarco3/api/Data/MeasuredDataPublication"
LICENCE = "CC BY 4.0, Ministry of Transport, Communications and Works (Cyprus National Access Point)"
INVENTORY_TTL_S = 24 * 3600
SWARCO_TTL_S = 15 * 60

CCTV_COLOR = "#0ea5e9"
DETECTOR_VEH_COLOR = "#f59e0b"
DETECTOR_CYCLE_COLOR = "#10b981"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _in_nicosia(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    w, s, e, n = NICOSIA_BBOX
    return w <= lon <= e and s <= lat <= n


def parse_sites_xml(xml_text: str, site_kind: str, *, nicosia_only: bool = True) -> tuple[list[dict], str | None]:
    """Parse a DATEX II MeasurementSiteTablePublication XML into site dicts.
    `site_kind` is 'cctv' or 'detector'. Returns (sites, publication_time_iso)."""
    root = ET.fromstring(xml_text)
    pub_time = None
    for el in root.iter():
        if _local(el.tag) == "publicationTime" and el.text:
            pub_time = el.text.strip()
            break
    prefix = "cctv" if site_kind == "cctv" else "det"
    sites: list[dict] = []
    for rec in root.iter():
        if _local(rec.tag) != "measurementSiteRecord":
            continue
        fields: dict[str, list[str]] = {}
        for el in rec.iter():
            t = _local(el.tag)
            if el.text and el.text.strip():
                fields.setdefault(t, []).append(el.text.strip())
        site_id = (fields.get("identifier") or [None])[0]
        if not site_id:
            continue
        try:
            lat = float(fields["latitude"][0])
            lon = float(fields["longitude"][0])
        except (KeyError, ValueError, IndexError):
            continue
        if nicosia_only and not _in_nicosia(lat, lon):
            continue
        name = (fields.get("value") or [f"Site {site_id}"])[0]
        subtype = (fields.get("systemSubtype") or [None])[0]
        eq_type = (fields.get("measurementEquipmentTypeEnum") or [None])[0]
        direction = (fields.get("directionBound") or [None])[0]
        sites.append({
            "id": f"{prefix}-{site_id}",
            "site_id": site_id,
            "kind": "its_cctv" if site_kind == "cctv" else "its_detector",
            "name": name,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "equipment_type": eq_type or ("CCTV" if site_kind == "cctv" else "Traffic Detector"),
            "subtype": subtype,
            "direction": direction,
            "publication_time": pub_time,
        })
    sites.sort(key=lambda s: int(s["site_id"]) if s["site_id"].isdigit() else 0)
    return sites, pub_time


_MEASURED_TAGS = {
    "measuredValue", "basicData", "trafficFlow", "vehicleFlowRate",
    "averageVehicleSpeed", "trafficSpeed", "trafficConcentration", "occupancy",
}


def parse_swarco_probe(xml_text: str) -> dict:
    """Inspect Swarco MeasuredDataPublication for whether any live measured flow values exist."""
    root = ET.fromstring(xml_text)
    pub_time = None
    site_refs = 0
    measured_values = 0
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "publicationTime" and pub_time is None and el.text:
            pub_time = el.text.strip()
        elif tag == "siteMeasurements":
            site_refs += 1
        elif tag in _MEASURED_TAGS:
            measured_values += 1
    return {
        "publication_time": pub_time,
        "site_refs": site_refs,
        "measured_count": measured_values,
        "has_values": measured_values > 0,
    }


class ITSInventory:
    """Daily-cached Nicosia CCTV and traffic detector inventories + Swarco measurement probe."""

    def __init__(self, fetch_text, *, cache_dir: Path | None = None,
                 inventory_ttl_s: float = INVENTORY_TTL_S, swarco_ttl_s: float = SWARCO_TTL_S):
        self.fetch_text = fetch_text
        self.cache_dir = cache_dir or CACHE_DIR
        self.inventory_ttl_s = inventory_ttl_s
        self.swarco_ttl_s = swarco_ttl_s
        self._lock = threading.Lock()
        self._sites: tuple[float, dict] | None = None
        self._swarco: tuple[float, dict] | None = None
        self._inv_read_at: float | None = None
        self._inv_ok_at: float | None = None
        self._inv_error: str | None = None
        self.fetches = 0

    def health(self) -> dict | None:
        """Last ITS inventory read metadata for /api/sources (never fetches)."""
        if self._inv_read_at is None:
            return None
        data = self._sites[1] if self._sites else {}
        return {
            "read_at": self._inv_read_at,
            "read_ok_at": self._inv_ok_at,
            "error": self._inv_error,
            "cctv": len(data.get("cctv") or []),
            "detectors": len(data.get("detectors") or []),
        }

    def swarco_health(self) -> dict | None:
        """Last Swarco MeasuredDataPublication probe result for /api/sources (never fetches)."""
        if self._swarco is None:
            return None
        return self._swarco[1]

    def _cached_xml(self, filename: str, url: str) -> tuple[str, bool]:
        """Return (xml_text, stale) using disk cache for inventory_ttl_s."""
        path = self.cache_dir / filename
        now_s = time.time()
        if path.exists() and now_s - path.stat().st_mtime < self.inventory_ttl_s:
            text = path.read_text(encoding="utf-8")
            ET.fromstring(text)
            return text, False
        try:
            self.fetches += 1
            text = self.fetch_text(url)
            ET.fromstring(text)
        except Exception as e:  # noqa: BLE001
            if path.exists():
                log.warning("ITS inventory %s refresh failed, using cached copy: %s", filename, e)
                return path.read_text(encoding="utf-8"), True
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text, False

    def inventories(self) -> dict:
        """Return Nicosia CCTV sites and traffic detector sites."""
        now_s = time.time()
        with self._lock:
            if self._sites and now_s - self._sites[0] < self.inventory_ttl_s:
                return self._sites[1]
            self._inv_read_at = now_s
            try:
                cctv_xml, cctv_stale = self._cached_xml("its_cctv.xml", CCTV_URL)
                det_xml, det_stale = self._cached_xml("its_detectors.xml", DETECTORS_URL)
                cctv, cctv_pub = parse_sites_xml(cctv_xml, "cctv", nicosia_only=True)
                detectors, det_pub = parse_sites_xml(det_xml, "detector", nicosia_only=True)
            except Exception as e:  # noqa: BLE001
                self._inv_error = str(e)[:200]
                if self._sites:
                    return self._sites[1]
                raise
            self._inv_ok_at = now_s
            self._inv_error = "showing saved copy: refresh failed" if (cctv_stale or det_stale) else None
            out = {
                "cctv": cctv,
                "detectors": detectors,
                "cctv_published": cctv_pub,
                "detectors_published": det_pub,
                "stale": cctv_stale or det_stale,
            }
            self._sites = (now_s, out)
            return out

    def probe_swarco(self) -> dict:
        """Non-intrusive probe of Swarco MeasuredDataPublication (cached 15 min)."""
        now_s = time.time()
        with self._lock:
            if self._swarco and now_s - self._swarco[0] < self.swarco_ttl_s:
                return self._swarco[1]
            try:
                self.fetches += 1
                xml_text = self.fetch_text(SWARCO_URL)
                parsed = parse_swarco_probe(xml_text)
                res = {
                    "read_at": now_s,
                    "error": None,
                    **parsed,
                }
            except Exception as e:  # noqa: BLE001
                res = {
                    "read_at": now_s,
                    "error": str(e)[:200],
                    "publication_time": None,
                    "site_refs": 0,
                    "measured_count": 0,
                    "has_values": False,
                }
            self._swarco = (now_s, res)
            return res

    def site(self, eid: str) -> dict | None:
        key = eid.strip().lower()
        inv = self.inventories()
        for row in inv["cctv"] + inv["detectors"]:
            if row["id"].lower() == key:
                return row
        return None

    def geojson(self, kind: str = "all", *, detector_mode: str = "all") -> dict:
        """GeoJSON FeatureCollection of Nicosia CCTV and/or traffic detector sites."""
        inv = self.inventories()
        swarco = self.probe_swarco()
        feats: list[dict] = []
        cctv_list = inv["cctv"] if kind in ("all", "cctv") else []
        det_list = inv["detectors"] if kind in ("all", "detectors") else []
        if detector_mode != "all":
            det_list = [d for d in det_list if (d.get("subtype") or "").lower() == detector_mode.lower()]

        for s in cctv_list:
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                "properties": {
                    "id": s["id"],
                    "entity_kind": "its_cctv",
                    "kind": "its_cctv",
                    "label": s["name"],
                    "state": "cctv",
                    "color": CCTV_COLOR,
                    "subtype": s.get("subtype") or "CCTV",
                    "popup": f"{s['name']}\nPWD CCTV ({s.get('subtype') or 'Camera'})\nSite inventory only",
                },
            })
        for s in det_list:
            sub = (s.get("subtype") or "Vehicles").lower()
            is_cycle = sub == "cycle"
            st = "detector_cycle" if is_cycle else "detector_vehicles"
            color = DETECTOR_CYCLE_COLOR if is_cycle else DETECTOR_VEH_COLOR
            dir_part = f", {s['direction']}" if s.get("direction") else ""
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                "properties": {
                    "id": s["id"],
                    "entity_kind": "its_detector",
                    "kind": "its_detector",
                    "label": s["name"],
                    "state": st,
                    "color": color,
                    "subtype": s.get("subtype") or "Vehicles",
                    "direction": s.get("direction"),
                    "popup": f"{s['name']}\nPWD {s.get('subtype') or 'Vehicle'} detector{dir_part}",
                },
            })
        veh_n = sum(1 for d in det_list if (d.get("subtype") or "").lower() != "cycle")
        cyc_n = sum(1 for d in det_list if (d.get("subtype") or "").lower() == "cycle")
        return {
            "type": "FeatureCollection",
            "features": feats,
            "meta": {
                "title": "PWD traffic detectors and CCTV",
                "count": len(feats),
                "counts": {
                    "cctv": len(cctv_list),
                    "detectors": len(det_list),
                    "vehicle_detectors": veh_n,
                    "cycle_detectors": cyc_n,
                },
                "stale": inv["stale"],
                "swarco": {
                    "has_values": swarco["has_values"],
                    "site_refs": swarco["site_refs"],
                    "measured_count": swarco["measured_count"],
                    "publication_time": swarco["publication_time"],
                    "error": swarco["error"],
                },
                "legend": {
                    "cctv": CCTV_COLOR,
                    "detector_vehicles": DETECTOR_VEH_COLOR,
                    "detector_cycle": DETECTOR_CYCLE_COLOR,
                },
                "sources": ["cynap_its", "swarco_traffic"],
            },
        }

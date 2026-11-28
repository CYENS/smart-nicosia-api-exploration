"""Runtime Urban Fabric, Heat & 15-Minute City service (shared via ``agent.fabric``)."""

from __future__ import annotations

import gzip
import json
import logging
import math
import threading
from pathlib import Path
from typing import Any

from chatbot.fabric.gradients import (
    NICOSIA_FABRIC_BBOX,
    SLOPE_COLORS,
    GradientsStore,
    classify_slope,
    summarize_gradients,
)
from chatbot.fabric.heat import (
    HEAT_BANDS,
    HEAT_COLORS_NIGHT,
    decode_heat_grid,
    sample_heat_grid,
)
from chatbot.fabric.scoring import (
    ACCESS_COLORS,
    HEAT_EXPOSURE_COLORS,
    SpatialBins,
    access_band,
    evaluate_accessibility,
    point_in_ring,
)
from chatbot.geo import haversine_m
from chatbot.settings import CACHE_DIR, ROOT, Settings

log = logging.getLogger(__name__)

DEFAULT_BUNDLE = ROOT / "data" / "fabric_nicosia.json.gz"

METRIC_META = {
    "heat_exposure": {
        "title": "Summer heat exposure (ground heat x people x shade)",
        "value_field": "heat_exposure_score",
        "legend": {
            "Low (<25)": HEAT_EXPOSURE_COLORS["low"],
            "Moderate (25-50)": HEAT_EXPOSURE_COLORS["moderate"],
            "High (50-75)": HEAT_EXPOSURE_COLORS["high"],
            "Very high (75+)": HEAT_EXPOSURE_COLORS["very_high"],
        },
    },
    "surface_heat": {
        "title": "Summer morning ground surface temperature (Landsat ~10:30 local)",
        "value_field": "lst_median_c",
        "legend": {b["label"]: c for b, c in zip(HEAT_BANDS, HEAT_COLORS_NIGHT)},
    },
    "people": {
        "title": "Modelled 2022 population (Kontur H3 res 8)",
        "value_field": "population",
        "legend": {
            "Under 500": "#1e3a5f",
            "500 to 2,000": "#2563eb",
            "2,000 to 5,000": "#38bdf8",
            "5,000+": "#ffae45",
        },
    },
    "access_15min": {
        "title": "15-minute walk access score",
        "value_field": "access_15min_score",
        "legend": {
            "High (75+)": ACCESS_COLORS["high"],
            "Good (50-74)": ACCESS_COLORS["good"],
            "Moderate (25-49)": ACCESS_COLORS["moderate"],
            "Low (<25)": ACCESS_COLORS["low"],
        },
    },
    "access_step_free": {
        "title": "Step-free 15-minute access (gentle slope)",
        "value_field": "access_step_free_score",
        "legend": {
            "High (75+)": ACCESS_COLORS["high"],
            "Good (50-74)": ACCESS_COLORS["good"],
            "Moderate (25-49)": ACCESS_COLORS["moderate"],
            "Low (<25)": ACCESS_COLORS["low"],
        },
    },
}


def _surface_heat_color(c: float | None) -> str:
    if c is None:
        return "#64748b"
    for b, color in zip(HEAT_BANDS, HEAT_COLORS_NIGHT):
        if b["to"] is None or c < b["to"]:
            return color
    return HEAT_COLORS_NIGHT[-1]


def _people_color(pop: float) -> str:
    if pop >= 5000:
        return "#ffae45"
    if pop >= 2000:
        return "#38bdf8"
    if pop >= 500:
        return "#2563eb"
    return "#1e3a5f"


def _hex_color(h: dict, metric: str) -> tuple[str, str]:
    if metric == "people":
        p = float(h.get("population") or 0)
        band = "5,000+" if p >= 5000 else "2,000 to 5,000" if p >= 2000 else "500 to 2,000" if p >= 500 else "Under 500"
        return _people_color(p), band
    if metric == "surface_heat":
        c = h.get("lst_median_c")
        band = next((b["label"] for b, _ in zip(HEAT_BANDS, HEAT_COLORS_NIGHT) if b["to"] is None or (c is not None and c < b["to"])), "Unknown")
        return _surface_heat_color(c), band
    if metric == "access_15min":
        s = h.get("access_15min_score")
        b = access_band(s)
        return ACCESS_COLORS[b], b
    if metric == "access_step_free":
        s = h.get("access_step_free_score")
        b = access_band(s)
        return ACCESS_COLORS[b], b
    # Default: heat_exposure
    band = h.get("heat_band") or "low"
    return HEAT_EXPOSURE_COLORS.get(band, HEAT_EXPOSURE_COLORS["low"]), h.get("heat_band_label") or "Low"


class Fabric:
    """Nicosia Urban Fabric, Heat Exposure & 15-Minute Walkability service."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bundle_path: Path | None = None,
        cache_dir: Path | None = None,
        gradients: GradientsStore | None = None,
        places=None,
        mobility=None,
    ):
        self.s = settings or Settings()
        self.bundle_path = bundle_path or DEFAULT_BUNDLE
        self.cache_dir = cache_dir or (CACHE_DIR / "fabric")
        self.gradients = gradients or GradientsStore(self.s, cache_dir=self.cache_dir)
        self.places = places
        self.mobility = mobility
        self._lock = threading.Lock()
        self._bundle: dict = {}
        self._hexagons: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._heat_grid: dict | None = None
        self._heat_arr = None
        self._amenities: list[dict] = []
        self._amenity_bins: SpatialBins | None = None
        self._service_bins: dict[str, SpatialBins] | None = None
        self._load_bundle()

    def _load_bundle(self) -> None:
        if not self.bundle_path.exists():
            return
        try:
            if self.bundle_path.suffix == ".gz":
                with gzip.open(self.bundle_path, "rt", encoding="utf-8") as fh:
                    data = json.load(fh)
            else:
                data = json.loads(self.bundle_path.read_text(encoding="utf-8"))
            self.load_data(data)
        except Exception as e:  # noqa: BLE001
            log.warning("could not load fabric bundle %s: %s", self.bundle_path, e)

    def load_data(self, data: dict) -> None:
        """Load an in-memory or fixture fabric bundle."""
        self._bundle = data
        self._hexagons = list(data.get("hexagons") or [])
        self._by_id = {h["id"].lower(): h for h in self._hexagons}
        self._heat_grid = data.get("heat_grid")
        self._heat_arr = decode_heat_grid(self._heat_grid) if self._heat_grid and self._heat_grid.get("grid_b64") else None
        self._amenities = list(data.get("amenities") or [])
        self._amenity_bins = SpatialBins(self._amenities) if self._amenities else None
        self._service_bins = None  # invalidate lazy bins

    @property
    def ready(self) -> bool:
        return bool(self._hexagons)

    def warm_up(self) -> None:
        """Background warm-up: refresh CyNAP gradients daily without delaying server startup."""
        try:
            self.gradients.refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("fabric warm-up gradients failed: %s", e)

    def hexagons(self) -> list[dict]:
        return self._hexagons

    def hex_by_id(self, eid: str) -> dict | None:
        return self._by_id.get((eid or "").strip().lower())

    def hex_at(self, lat: float, lon: float) -> dict | None:
        """Find the H3 hexagon containing (lat, lon), or nearest within 550 m."""
        for h in self._hexagons:
            ring = h.get("ring") or []
            if not ring:
                continue
            cx, cy = h["center"]
            if abs(cx - lon) > 0.012 or abs(cy - lat) > 0.01:
                continue
            if point_in_ring(lon, lat, ring):
                return h
        # Border fallback
        best = None
        best_d = 550.0
        for h in self._hexagons:
            d = haversine_m(lat, lon, h["center"][1], h["center"][0])
            if d < best_d:
                best_d = d
                best = h
        return best

    def _get_service_bins(self) -> dict[str, SpatialBins]:
        if self._service_bins is not None:
            return self._service_bins
        with self._lock:
            if self._service_bins is not None:
                return self._service_bins
            from collections import defaultdict
            by_cat: dict[str, list[dict]] = defaultdict(list)
            if self.places is not None and getattr(self.places, "ready", False):
                for p in self.places.all():
                    cat = p.get("category")
                    item = {"id": p["id"], "name": p.get("name") or cat, "lat": p["lat"], "lon": p["lon"], "source": "OpenStreetMap"}
                    if cat == "pharmacy":
                        by_cat["pharmacy"].append(item)
                    elif cat == "supermarket":
                        by_cat["grocery"].append(item)
                    elif cat == "park":
                        by_cat["park"].append(item)
                    elif cat in ("toilets", "drinking_water", "bench"):
                        by_cat["civic"].append(item)
                ov = getattr(self.places, "overture", None)
                if ov is not None and getattr(ov, "ready", False):
                    for p in ov.all():
                        cat = p.get("category")
                        kind = p.get("kind") or ""
                        item = {"id": p["id"], "name": p.get("name") or kind, "lat": p["lat"], "lon": p["lon"], "source": "Overture Maps"}
                        if cat == "pharmacy":
                            by_cat["pharmacy"].append(item)
                        elif cat == "supermarket" or "grocery" in kind or "convenience" in kind:
                            by_cat["grocery"].append(item)
                        elif cat == "park":
                            by_cat["park"].append(item)
                        elif "school" in kind or "kindergarten" in kind or "university" in kind:
                            by_cat["school"].append(item)
            if self.mobility is not None:
                tt = self.mobility.timetable()
                if tt is not None:
                    for s in tt.stops:
                        by_cat["bus_stop"].append({"id": f"stop-{s.id}", "name": s.name, "lat": s.lat, "lon": s.lon, "source": "GTFS"})
            for am in self._amenities:
                item = {"id": am["id"], "name": am.get("name") or am["kind"].replace("_", " ").capitalize(), "lat": am["lat"], "lon": am["lon"], "source": "Overture base"}
                if am["kind"] in ("recycling", "bench", "crossing"):
                    by_cat["civic"].append(item)
                elif am["kind"] in ("bus_stop", "stop"):
                    by_cat["bus_stop"].append(item)
            # Also add built land-use schools/parks if available in overture cache
            ov_base = self.cache_dir / "overture_fabric.json"
            if ov_base.exists():
                try:
                    raw_ov = json.loads(ov_base.read_text(encoding="utf-8"))
                    for lu in raw_ov.get("land_use_points") or []:
                        cls = lu.get("class")
                        item = {"id": lu["id"], "name": lu.get("name") or cls, "lat": lu["lat"], "lon": lu["lon"], "source": "Overture land use"}
                        if cls in ("park", "garden", "grass", "orchard", "playground", "recreation_ground", "forest"):
                            by_cat["park"].append(item)
                        elif cls in ("school", "kindergarten", "university", "college"):
                            by_cat["school"].append(item)
                except (OSError, ValueError):
                    pass
            self._service_bins = {k: SpatialBins(v) for k, v in by_cat.items()}
            return self._service_bins

    def summary(self) -> dict[str, Any]:
        grad_pts = self.gradients.points()
        grad_sum = summarize_gradients(grad_pts)
        total_pop = sum(int(h.get("population") or 0) for h in self._hexagons)
        lst_vals = [h["lst_median_c"] for h in self._hexagons if h.get("lst_median_c") is not None]
        acc_vals = [h["access_15min_score"] for h in self._hexagons if h.get("access_15min_score") is not None]
        sf_vals = [h["access_step_free_score"] for h in self._hexagons if h.get("access_step_free_score") is not None]
        hg = self._heat_grid or {}
        return {
            "ready": self.ready,
            "hex_count": len(self._hexagons),
            "population_total_2022": total_pop,
            "population_note": "Modelled 2022 population (Kontur H3 res-8, ~0.7 km² hexagons)",
            "heat": {
                "min_c": hg.get("min_c") or (min(lst_vals) if lst_vals else None),
                "median_c": hg.get("median_c") or (round(sorted(lst_vals)[len(lst_vals) // 2], 1) if lst_vals else None),
                "max_c": hg.get("max_c") or (max(lst_vals) if lst_vals else None),
                "scenes_used": hg.get("scenes_used") or 0,
                "date_from": hg.get("date_from"),
                "date_to": hg.get("date_to"),
                "overpass_local": hg.get("overpass_local") or "~10:30",
                "label": "Summer morning ground surface temperature (Landsat ~10:30 local, not air temperature)",
            },
            "access": {
                "mean_15min": round(sum(acc_vals) / len(acc_vals), 1) if acc_vals else None,
                "mean_step_free": round(sum(sf_vals) / len(sf_vals), 1) if sf_vals else None,
            },
            "slopes": {
                **grad_sum,
                "health": self.gradients.health(),
            },
            "overture_release": self._bundle.get("overture_release"),
            "infra_counts": self._bundle.get("infra_counts") or {},
            "caveats": self._bundle.get("caveats") or {},
        }

    def heat_grid_payload(self) -> dict[str, Any]:
        if not self._heat_grid or not self._heat_grid.get("grid_b64"):
            return {
                "known": False,
                "reason": "The Landsat summer ground surface temperature grid has not been built yet (run python -m chatbot.fabric build).",
            }
        hg = self._heat_grid
        return {
            "known": True,
            "bbox": hg["bbox"],
            "nx": hg["nx"],
            "ny": hg["ny"],
            "scale": hg.get("scale", 0.01),
            "nodata": hg.get("nodata", 65535),
            "grid_b64": hg["grid_b64"],
            "min_c": hg.get("min_c"),
            "median_c": hg.get("median_c"),
            "p90_c": hg.get("p90_c"),
            "max_c": hg.get("max_c"),
            "scenes_used": hg.get("scenes_used"),
            "date_from": hg.get("date_from"),
            "date_to": hg.get("date_to"),
            "overpass_local": hg.get("overpass_local", "~10:30"),
            "temperature_kind": "morning_ground_surface",
            "note": hg.get("note") or "Morning ground surface temperature (Landsat ~10:30 local), not air temperature",
            "bands": HEAT_BANDS,
            "colors": HEAT_COLORS_NIGHT,
            "sources": ["landsat_lst"],
        }

    def hexagons_geojson(
        self,
        metric: str = "heat_exposure",
        *,
        center: tuple[float, float] | None = None,
        radius_m: float | None = None,
        min_pop: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if metric not in METRIC_META:
            raise ValueError(f"unknown metric {metric!r}; one of {sorted(METRIC_META)}")
        meta = METRIC_META[metric]
        vf = meta["value_field"]
        items: list[tuple[float, dict]] = []
        for h in self._hexagons:
            if int(h.get("population") or 0) < min_pop:
                continue
            dist = 0.0
            if center is not None:
                dist = haversine_m(center[0], center[1], h["center"][1], h["center"][0])
                if radius_m is not None and dist > radius_m + 550.0:
                    continue
            val = h.get(vf)
            if val is None:
                continue
            items.append((float(val), h))
        items.sort(key=lambda t: (-t[0], t[1]["id"]))
        if limit is not None:
            items = items[:limit]

        features = []
        vals = []
        for val, h in items:
            vals.append(val)
            color, band_label = _hex_color(h, metric)
            popup = (
                f"{h['name']}\n"
                f"Modelled 2022 pop: {h['population']:,} | Morning ground heat (~10:30): {h.get('lst_median_c', '?')} °C\n"
                f"15-min walk: {h.get('access_15min_score', '?')}/100 (step-free: {h.get('access_step_free_score', '?')}/100)"
            )
            features.append({
                "type": "Feature",
                "id": h["id"],
                "geometry": {"type": "Polygon", "coordinates": [h["ring"]]},
                "properties": {
                    "id": h["id"],
                    "kind": "fabric_hex",
                    "name": h["name"],
                    "label": h["name"],
                    "state": band_label,
                    "color": color,
                    "population": h["population"],
                    "pop_density_km2": h.get("pop_density_km2"),
                    "lst_median_c": h.get("lst_median_c"),
                    "lst_max_c": h.get("lst_max_c"),
                    "green_pct": h.get("green_pct"),
                    "heat_exposure_score": h.get("heat_exposure_score"),
                    "heat_band_label": h.get("heat_band_label"),
                    "access_15min_score": h.get("access_15min_score"),
                    "access_step_free_score": h.get("access_step_free_score"),
                    "step_free_gap": h.get("step_free_gap"),
                    "grad_gt5": (h.get("slopes") or {}).get("gt5", 0),
                    "grad_gt8": (h.get("slopes") or {}).get("gt8", 0),
                    "popup": popup,
                },
            })

        sources = ["kontur_population"]
        if metric in ("heat_exposure", "surface_heat"):
            sources = ["landsat_lst", "kontur_population", "overture_base"]
        elif metric in ("access_15min", "access_step_free"):
            sources = ["openstreetmap", "overture", "overture_base", "bus_timetable", "cynap_gradients", "kontur_population"]

        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "title": meta["title"],
                "metric": metric,
                "value_field": vf,
                "count": len(features),
                "legend": meta["legend"],
                "stats": {"min": min(vals), "max": max(vals)} if vals else None,
                "sources": sources,
                "caveats": self._bundle.get("caveats") or {},
                "fabric": {"kind": "hexagons", "metric": metric, "value_field": vf},
            },
        }

    def gradients_geojson(self, min_slope: float = 0.0, refresh_if_stale: bool = False) -> dict[str, Any]:
        pts = self.gradients.points(refresh_if_stale=refresh_if_stale)
        kept = [p for p in pts if p["slope_pct"] >= min_slope]
        features = []
        for p in kept:
            band = p.get("band") or classify_slope(p["slope_pct"])
            color = SLOPE_COLORS.get(band, SLOPE_COLORS["gentle"])
            grade_note = (
                "Steep (>8%): exceeds common wheelchair ramp limits"
                if band == "steep"
                else "Moderate (>5%): noticeable climb for manual wheelchairs and bikes"
                if band == "moderate"
                else "Gentle (<=5%): step-free friendly grade"
            )
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": {
                    "id": p["id"],
                    "kind": "road_gradient",
                    "road_code": p.get("road_code"),
                    "road_name": p.get("road_name"),
                    "km_ref": p.get("km_ref"),
                    "slope_pct": p["slope_pct"],
                    "state": band,
                    "color": color,
                    "label": f"{p.get('road_name') or p.get('road_code')}: {p['slope_pct']}%",
                    "popup": f"{p.get('road_name') or p.get('road_code') or 'Road point'}\nSlope: {p['slope_pct']}% ({band})\n{grade_note}",
                },
            })
        summary = summarize_gradients(pts)
        health = self.gradients.health()
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "title": "Street and road slopes (CyNAP 100 m)",
                "count": len(features),
                "total_points": summary["total"],
                "summary": summary,
                "status": health["state"],
                "note": health["note"],
                "legend": {
                    "Gentle (<=5%)": SLOPE_COLORS["gentle"],
                    "Moderate (5 to 8%)": SLOPE_COLORS["moderate"],
                    "Steep (>8%)": SLOPE_COLORS["steep"],
                },
                "sources": ["cynap_gradients"],
                "fabric": {"kind": "gradients"},
            },
        }

    def spot_fabric(self, lat: float, lon: float) -> dict[str, Any]:
        """Compact urban fabric, summer ground heat, modelled population & 15-min access at (lat, lon)."""
        h = self.hex_at(lat, lon)
        point_lst = sample_heat_grid(self._heat_grid, self._heat_arr, lat, lon) if self._heat_grid and self._heat_arr is not None else None
        grad_pts = self.gradients.points()
        near_grads = [
            (round(haversine_m(lat, lon, p["lat"], p["lon"])), p)
            for p in grad_pts
            if abs(p["lat"] - lat) < 0.005 and abs(p["lon"] - lon) < 0.006
        ]
        near_grads = [(d, p) for d, p in near_grads if d <= 400]
        near_grads.sort(key=lambda x: (-x[1]["slope_pct"], x[0]))
        gt5 = sum(1 for _, p in near_grads if p["slope_pct"] > 5.0)
        gt8 = sum(1 for _, p in near_grads if p["slope_pct"] > 8.0)

        return {
            "known": h is not None or point_lst is not None,
            "hex_id": h["id"] if h else None,
            "hex_name": h["name"] if h else None,
            "population_2022": h["population"] if h else None,
            "pop_density_km2": h.get("pop_density_km2") if h else None,
            "population_note": "Modelled 2022 population (Kontur H3 res 8)",
            "surface_heat_c": point_lst if point_lst is not None else (h.get("lst_median_c") if h else None),
            "hex_lst_median_c": h.get("lst_median_c") if h else None,
            "overpass_local": (self._heat_grid or {}).get("overpass_local", "~10:30"),
            "temperature_note": "Morning ground surface temperature (Landsat ~10:30 local), not air temperature",
            "heat_exposure_score": h.get("heat_exposure_score") if h else None,
            "heat_band_label": h.get("heat_band_label") if h else None,
            "heat_parts": h.get("heat_parts") if h else {},
            "green_pct": h.get("green_pct") if h else None,
            "access_15min_score": h.get("access_15min_score") if h else None,
            "access_step_free_score": h.get("access_step_free_score") if h else None,
            "step_free_gap": h.get("step_free_gap") if h else None,
            "nearest_services": h.get("nearest_services") if h else {},
            "infra": h.get("infra") if h else {},
            "slopes_nearby": {
                "radius_m": 400,
                "points": len(near_grads),
                "gt5": gt5,
                "gt8": gt8,
                "max_pct": near_grads[0][1]["slope_pct"] if near_grads else None,
                "steepest": {
                    "road": near_grads[0][1].get("road_name") or near_grads[0][1].get("road_code"),
                    "slope_pct": near_grads[0][1]["slope_pct"],
                    "distance_m": near_grads[0][0],
                } if near_grads else None,
            },
        }

    def population_within(self, lat: float, lon: float, radius_m: float = 500.0) -> dict[str, Any]:
        """Sum modelled 2022 population inside/intersecting ``radius_m`` of (lat, lon)."""
        # H3 res-8 average radius is ~460 m. Weight overlapping edge hexes by smooth overlap fraction.
        hex_r = 460.0
        overlapping: list[tuple[float, float, dict]] = []
        centroid_pop = 0
        weighted_pop = 0.0
        for h in self._hexagons:
            d = haversine_m(lat, lon, h["center"][1], h["center"][0])
            if d <= radius_m + hex_r:
                if d <= radius_m:
                    centroid_pop += int(h["population"])
                # Smooth overlap estimate between circle(radius_m) and hex disc(hex_r)
                if d <= max(0.0, radius_m - hex_r):
                    frac = 1.0
                elif radius_m < hex_r and d <= hex_r - radius_m:
                    frac = (radius_m / hex_r) ** 2
                else:
                    frac = max(0.05, min(1.0, (radius_m + hex_r - d) / (2.0 * hex_r)))
                weighted_pop += float(h["population"]) * frac
                overlapping.append((d, frac, h))
        overlapping.sort(key=lambda t: t[0])
        return {
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "radius_m": round(radius_m),
            "estimated_population": round(weighted_pop),
            "centroid_population": centroid_pop,
            "hex_count": len(overlapping),
            "population_note": "Modelled 2022 population (Kontur H3 res-8 ~0.7 km² hexagons; area-weighted where hexagons straddle the radius)",
            "hexes": [
                {
                    "id": h["id"],
                    "name": h["name"],
                    "distance_m": round(d),
                    "overlap_pct": round(frac * 100),
                    "population_2022": h["population"],
                    "heat_exposure_score": h.get("heat_exposure_score"),
                    "lst_median_c": h.get("lst_median_c"),
                    "ring": h["ring"],
                    "center": h["center"],
                }
                for d, frac, h in overlapping
            ],
        }

    def access_at(self, lat: float, lon: float) -> dict[str, Any]:
        """Compute detailed 15-minute walkability (standard & step-free) at a specific point."""
        service_bins = self._get_service_bins()
        grad_pts = self.gradients.points()
        grad_bins = SpatialBins(grad_pts) if grad_pts else None
        h = self.hex_at(lat, lon)
        near_grad = grad_bins.within(lat, lon, 500.0) if grad_bins else []
        g_gt5 = sum(1 for _, p in near_grad if p["slope_pct"] > 5.0)
        g_gt8 = sum(1 for _, p in near_grad if p["slope_pct"] > 8.0)
        near_cross = len([a for _, a in (self._amenity_bins.within(lat, lon, 500.0) if self._amenity_bins else []) if a["kind"] == "crossing"])

        # Fallback to prebuilt hex summary if runtime service_bins are empty (e.g. unit test without places cache)
        if not any(b.items for b in service_bins.values()) and h is not None:
            return {
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "hex_id": h["id"],
                "hex_name": h["name"],
                "access_15min_score": h["access_15min_score"],
                "access_step_free_score": h["access_step_free_score"],
                "step_free_gap": h["step_free_gap"],
                "categories": [
                    {
                        "category": cat, "label": cat.replace("_", " ").capitalize(),
                        "reachable_15min": (info["walk_min"] <= 15.0),
                        "reachable_step_free": (info["step_free_min"] <= 15.0 and not info.get("slope_barrier")),
                        "walk_min": info["walk_min"], "step_free_min": info["step_free_min"],
                        "slope_barrier": info.get("slope_barrier", False),
                        "nearest": {"name": info["name"], "distance_m": info["distance_m"]},
                    }
                    for cat, info in (h.get("nearest_services") or {}).items()
                ],
                "slopes_nearby": h.get("slopes") or {"points": 0, "gt5": 0, "gt8": 0, "coverage": "unmeasured"},
                "infra": h.get("infra") or {},
            }

        acc = evaluate_accessibility(
            lat, lon, service_bins, grad_bins,
            local_grad_gt5=g_gt5, local_grad_gt8=g_gt8, local_crossings=near_cross,
        )
        return {
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "hex_id": h["id"] if h else None,
            "hex_name": h["name"] if h else None,
            **acc,
            "slopes_nearby": {
                "points": len(near_grad),
                "gt5": g_gt5,
                "gt8": g_gt8,
                "max_pct": max((p["slope_pct"] for _, p in near_grad), default=None),
                "coverage": "measured" if near_grad else "unmeasured",
            },
            "infra": h.get("infra") if h else {},
        }

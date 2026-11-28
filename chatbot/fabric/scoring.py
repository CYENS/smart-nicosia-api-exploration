"""15-minute walkability (standard & step-free/gentle-slope), heat exposure, and spatial helpers."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from chatbot.fabric.gradients import MODERATE_PCT, STEEP_PCT
from chatbot.geo import haversine_m

# 15-minute walking model:
# - Standard walk: 4.8 km/h = 80 m/min -> 1,200 m path (~960 m crow-flies with 1.25x street detour).
# - Step-free / gentle-slope walk: 3.6 km/h = 60 m/min -> 900 m path (~720 m crow-flies with 1.25x detour),
#   plus gradient penalty when CyNAP road points nearby or between origin and destination exceed 5% or 8%.
DETOUR_FACTOR = 1.25
WALK_SPEED_M_MIN = 80.0
STEP_FREE_SPEED_M_MIN = 60.0
WALK_15_PATH_M = 1200.0
STEP_FREE_15_PATH_M = 900.0
WALK_15_STRAIGHT_M = WALK_15_PATH_M / DETOUR_FACTOR  # 960 m
STEP_FREE_15_STRAIGHT_M = STEP_FREE_15_PATH_M / DETOUR_FACTOR  # 720 m

SERVICE_CATEGORIES: tuple[tuple[str, str, float], ...] = (
    ("bus_stop", "Bus stop", 0.22),
    ("grocery", "Grocery or supermarket", 0.20),
    ("pharmacy", "Pharmacy", 0.16),
    ("park", "Park or green space", 0.16),
    ("school", "School or university", 0.14),
    ("civic", "Civic amenity (recycling, bench, crossing)", 0.12),
)

HEAT_BANDS = [
    (25.0, "low", "Low"),
    (50.0, "moderate", "Moderate"),
    (75.0, "high", "High"),
    (math.inf, "very_high", "Very high"),
]
HEAT_EXPOSURE_COLORS = {
    "low": "#22c55e",
    "moderate": "#eab308",
    "high": "#f97316",
    "very_high": "#ff5a4e",
}
ACCESS_COLORS = {
    "high": "#22c55e",       # 75-100
    "good": "#84cc16",       # 50-74
    "moderate": "#eab308",   # 25-49
    "low": "#f97316",        # 0-24
}


def access_band(score: float | None) -> str:
    if score is None:
        return "low"
    if score >= 75:
        return "high"
    if score >= 50:
        return "good"
    if score >= 25:
        return "moderate"
    return "low"


def heat_band(score: float | None) -> tuple[str, str]:
    if score is None:
        return "low", "Low"
    for top, code, label in HEAT_BANDS:
        if score < top:
            return code, label
    return "very_high", "Very high"


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
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


class SpatialBins:
    """Fast ~500 m spatial grid for point lookup without third-party spatial index libs."""

    def __init__(self, items: list[dict], cell_deg: float = 0.005):
        self.cell = cell_deg
        self.bins: dict[tuple[int, int], list[dict]] = defaultdict(list)
        self.items = items
        for it in items:
            lat, lon = float(it["lat"]), float(it["lon"])
            self.bins[(int(lat / cell_deg), int(lon / cell_deg))].append(it)

    def within(self, lat: float, lon: float, radius_m: float) -> list[tuple[float, dict]]:
        dlat = radius_m / 110574.0
        dlon = radius_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
        r0 = int((lat - dlat) / self.cell) - 1
        r1 = int((lat + dlat) / self.cell) + 1
        c0 = int((lon - dlon) / self.cell) - 1
        c1 = int((lon + dlon) / self.cell) + 1
        out: list[tuple[float, dict]] = []
        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                for it in self.bins.get((rr, cc), ()):
                    d = haversine_m(lat, lon, it["lat"], it["lon"])
                    if d <= radius_m:
                        out.append((d, it))
        out.sort(key=lambda x: x[0])
        return out

    def nearest(self, lat: float, lon: float, max_m: float = 2500.0) -> tuple[float, dict] | None:
        hits = self.within(lat, lon, max_m)
        return hits[0] if hits else None


def _slope_penalty(
    lat: float,
    lon: float,
    dest_lat: float,
    dest_lon: float,
    grad_index: SpatialBins | None,
) -> tuple[float, int, int, float | None]:
    """Check CyNAP gradient points along the origin-destination corridor & nearby.

    Returns (penalty_factor, gt5_count, gt8_count, max_slope_pct).
    A >8% grade multiplies effective effort strongly (2.0x+), >5% multiplies 1.35x.
    """
    if grad_index is None or not grad_index.items:
        return 1.0, 0, 0, None
    mid_lat = 0.5 * (lat + dest_lat)
    mid_lon = 0.5 * (lon + dest_lon)
    half = max(250.0, 0.65 * haversine_m(lat, lon, dest_lat, dest_lon))
    near = grad_index.within(mid_lat, mid_lon, min(1100.0, half + 150.0))
    if not near:
        return 1.0, 0, 0, None
    gt5 = sum(1 for _, p in near if p["slope_pct"] > MODERATE_PCT)
    gt8 = sum(1 for _, p in near if p["slope_pct"] > STEEP_PCT)
    max_s = max(p["slope_pct"] for _, p in near)
    if gt8 > 0:
        return 1.85, gt5, gt8, max_s
    if gt5 > 0:
        return 1.35, gt5, gt8, max_s
    return 1.0, 0, 0, max_s


def evaluate_accessibility(
    lat: float,
    lon: float,
    services: dict[str, SpatialBins],
    grad_index: SpatialBins | None = None,
    *,
    local_grad_gt5: int = 0,
    local_grad_gt8: int = 0,
    local_crossings: int = 0,
) -> dict[str, Any]:
    """Compute standard 15-min walkability and step-free/gentle-slope 15-min scores at (lat, lon)."""
    std_total = 0.0
    sf_total = 0.0
    categories: list[dict] = []
    std_reached = 0
    sf_reached = 0

    for cat_id, label, weight in SERVICE_CATEGORIES:
        bins = services.get(cat_id)
        if bins is None:
            categories.append({
                "category": cat_id, "label": label, "reachable_15min": False,
                "reachable_step_free": False, "nearest": None,
            })
            continue
        within = bins.within(lat, lon, WALK_15_STRAIGHT_M)
        nearest_hit = within[0] if within else bins.nearest(lat, lon, max_m=2400.0)
        if nearest_hit is None:
            categories.append({
                "category": cat_id, "label": label, "count_15min": 0,
                "reachable_15min": False, "reachable_step_free": False, "nearest": None,
            })
            continue

        straight_m, item = nearest_hit
        path_m = straight_m * DETOUR_FACTOR
        walk_min = round(path_m / WALK_SPEED_M_MIN, 1)
        reach_std = path_m <= WALK_15_PATH_M

        penalty, corr_gt5, corr_gt8, corr_max_s = _slope_penalty(
            lat, lon, item["lat"], item["lon"], grad_index
        )
        # Local steep grades inside the hex also increase effort
        if local_grad_gt8 > 0:
            penalty = max(penalty, 1.6)
        elif local_grad_gt5 > 0:
            penalty = max(penalty, 1.25)

        eff_sf_m = path_m * penalty
        sf_min = round(eff_sf_m / STEP_FREE_SPEED_M_MIN, 1)
        reach_sf = eff_sf_m <= STEP_FREE_15_PATH_M and corr_gt8 == 0

        if reach_std:
            std_reached += 1
            proximity = max(0.25, 1.0 - (path_m / WALK_15_PATH_M) * 0.7)
            std_total += weight * proximity
        if reach_sf:
            sf_reached += 1
            sf_prox = max(0.2, 1.0 - (eff_sf_m / STEP_FREE_15_PATH_M) * 0.75)
            # Gentle bonus when pedestrian crossings exist in the neighbourhood
            crossing_factor = 1.0 if local_crossings > 0 or cat_id != "civic" else 0.92
            sf_total += weight * sf_prox * crossing_factor

        categories.append({
            "category": cat_id,
            "label": label,
            "count_15min": len(within),
            "reachable_15min": reach_std,
            "reachable_step_free": reach_sf,
            "walk_min": walk_min,
            "step_free_min": sf_min,
            "slope_barrier": corr_gt8 > 0 or local_grad_gt8 > 0,
            "slope_moderate": (corr_gt5 > 0 or local_grad_gt5 > 0) and not (corr_gt8 > 0 or local_grad_gt8 > 0),
            "max_corridor_slope_pct": corr_max_s,
            "nearest": {
                "id": item.get("id"),
                "name": item.get("name") or label,
                "distance_m": round(path_m),
                "straight_m": round(straight_m),
                "source": item.get("source"),
            },
        })

    # Combine proximity-weighted score (70%) + category completeness (30%) -> 0..100
    std_completeness = std_reached / len(SERVICE_CATEGORIES)
    sf_completeness = sf_reached / len(SERVICE_CATEGORIES)
    std_score = round(min(100.0, (std_total * 0.7 + std_completeness * 0.3) * 100.0), 1)
    sf_score = round(min(100.0, (sf_total * 0.7 + sf_completeness * 0.3) * 100.0), 1)
    return {
        "access_15min_score": std_score,
        "access_step_free_score": sf_score,
        "step_free_gap": round(max(0.0, std_score - sf_score), 1),
        "categories_reached_15min": std_reached,
        "categories_reached_step_free": sf_reached,
        "categories_total": len(SERVICE_CATEGORIES),
        "categories": categories,
    }


def compute_heat_exposure(
    hexagons: list[dict],
) -> None:
    """Populate ``heat_exposure_score``, ``heat_band``, and transparent ``heat_parts`` in-place.

    Components (always exposed separately):
    - surface_heat_norm: morning ground surface temp normalized between city p10 and p90
    - pop_norm: log-scaled modelled 2022 population density against city max
    - lack_of_cool_norm: 1 - cooling factor from green land-use % (and building shade if known)
    """
    temps = [h["lst_median_c"] for h in hexagons if h.get("lst_median_c") is not None]
    pops = [h["population"] for h in hexagons if h.get("population") and h["population"] > 0]
    if not temps:
        for h in hexagons:
            h["heat_exposure_score"] = None
            h["heat_band"] = "low"
            h["heat_band_label"] = "Unknown"
            h["heat_parts"] = {}
        return

    temps_sorted = sorted(temps)
    t_lo = temps_sorted[max(0, int(0.05 * len(temps_sorted)))]
    t_hi = temps_sorted[min(len(temps_sorted) - 1, int(0.95 * len(temps_sorted)))]
    t_span = max(1.0, t_hi - t_lo)
    max_log_pop = math.log1p(max(pops)) if pops else 1.0

    for h in hexagons:
        lst = h.get("lst_median_c")
        pop = float(h.get("population") or 0.0)
        green_pct = float(h.get("green_pct") or 0.0)
        shade_factor = float(h.get("building_shade_factor") or 0.0)
        if lst is None:
            h["heat_exposure_score"] = None
            h["heat_band"] = "low"
            h["heat_band_label"] = "Unknown"
            h["heat_parts"] = {}
            continue
        heat_norm = max(0.0, min(1.0, (lst - t_lo) / t_span))
        pop_norm = max(0.0, min(1.0, math.log1p(pop) / max_log_pop)) if pop > 0 else 0.0
        cool_factor = min(1.0, (green_pct / 35.0) * 0.7 + shade_factor * 0.3)
        lack_cool_norm = round(1.0 - cool_factor, 3)

        if pop <= 0:
            raw = 0.0
        else:
            raw = (0.45 * heat_norm + 0.35 * pop_norm + 0.20 * lack_cool_norm) * 100.0
        score = round(min(100.0, max(0.0, raw)), 1)
        band_code, band_label = heat_band(score)
        h["heat_exposure_score"] = score
        h["heat_band"] = band_code
        h["heat_band_label"] = band_label
        h["heat_parts"] = {
            "surface_heat_c": lst,
            "heat_norm": round(heat_norm, 3),
            "population_2022": round(pop),
            "pop_norm": round(pop_norm, 3),
            "green_pct": round(green_pct, 1),
            "cool_factor": round(cool_factor, 3),
            "lack_of_cool_norm": lack_cool_norm,
            "weights": {"surface_heat": 0.45, "population": 0.35, "shade_green_context": 0.20},
        }

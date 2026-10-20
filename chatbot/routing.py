"""Routes, tours and reachable areas from Valhalla (public FOSSGIS instance by default).

Tours: one Valhalla matrix request, then an exact solver for the visiting order (Held-Karp,
up to 12 stops), then one route request through the stops in that order.
"""

from __future__ import annotations

import itertools
import json

from chatbot.settings import Settings
from chatbot.web import WebError, get_json

MODES = {"walk": "pedestrian", "drive": "auto", "bike": "bicycle", "wheelchair": "pedestrian"}
PREFERENCES = ("fastest", "lit_streets", "quiet", "less_hills", "accessible")
MAX_TOUR_STOPS = 12


def costing_options(mode: str, prefer: list[str]) -> dict:
    costing = MODES[mode]
    o: dict = {}
    if costing == "pedestrian":
        if mode == "wheelchair" or "accessible" in prefer:
            o |= {"type": "wheelchair", "step_penalty": 600}
        if "lit_streets" in prefer:
            o["use_lit"] = 1.0
        if "quiet" in prefer:
            o |= {"walkway_factor": 0.5, "sidewalk_factor": 0.7, "use_living_streets": 0.9,
                  "alley_factor": 1.2}
        if "less_hills" in prefer:
            o["use_hills"] = 0.0
    elif costing == "bicycle":
        o["bicycle_type"] = "Hybrid"
        if "less_hills" in prefer:
            o["use_hills"] = 0.0
        if "quiet" in prefer:
            o["use_roads"] = 0.2
    return {costing: o} if o else {}


def decode_polyline6(encoded: str) -> list[list[float]]:
    """Valhalla's encoded shape (precision 6) -> [[lon, lat], ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append([round(lon / 1e6, 6), round(lat / 1e6, 6)])
    return coords


class Router:
    def __init__(self, settings: Settings):
        self.s = settings

    def _call(self, action: str, body: dict, timeout: float = 30.0) -> dict:
        return get_json(f"{self.s.valhalla_url}/{action}", user_agent=self.s.user_agent,
                        params={"json": json.dumps(body, separators=(",", ":"))}, timeout=timeout)

    def route(self, stops: list[tuple[float, float]], mode: str, prefer: list[str]) -> dict:
        body = {
            "locations": [{"lat": lat, "lon": lon, "type": "break"} for lat, lon in stops],
            "costing": MODES[mode],
            "costing_options": costing_options(mode, prefer),
            "directions_options": {"units": "kilometers", "language": "en-US"},
        }
        trip = self._call("route", body)["trip"]
        coords: list[list[float]] = []
        legs = []
        for leg in trip["legs"]:
            shape = decode_polyline6(leg["shape"])
            coords.extend(shape if not coords else shape[1:])
            steps = [m["instruction"] for m in leg.get("maneuvers", [])
                     if m.get("instruction")]
            legs.append({
                "distance_km": round(leg["summary"]["length"], 2),
                "duration_min": round(leg["summary"]["time"] / 60, 1),
                "directions": steps[:12] + (["..."] if len(steps) > 12 else []),
            })
        return {
            "distance_km": round(trip["summary"]["length"], 2),
            "duration_min": round(trip["summary"]["time"] / 60, 1),
            "legs": legs,
            "coordinates": coords,
        }

    def matrix(self, stops: list[tuple[float, float]], mode: str, prefer: list[str]) -> list[list[float]]:
        """Seconds between every pair of stops (inf where no route exists)."""
        locs = [{"lat": lat, "lon": lon} for lat, lon in stops]
        body = {"sources": locs, "targets": locs, "costing": MODES[mode],
                "costing_options": costing_options(mode, prefer)}
        data = self._call("sources_to_targets", body, timeout=45)
        n = len(stops)
        out = [[float("inf")] * n for _ in range(n)]
        for row in data["sources_to_targets"]:
            for cell in row:
                if cell.get("time") is not None:
                    out[cell["from_index"]][cell["to_index"]] = float(cell["time"])
        return out

    def isochrone(self, center: tuple[float, float], minutes: list[int], mode: str) -> dict:
        body = {"locations": [{"lat": center[0], "lon": center[1]}], "costing": MODES[mode],
                "contours": [{"time": m} for m in sorted(set(minutes))], "polygons": True,
                "costing_options": costing_options(mode, [])}
        return self._call("isochrone", body)


def best_order(seconds: list[list[float]], return_to_start: bool,
               fixed_end: bool = False) -> list[int]:
    """Exact shortest visiting order starting at stop 0 (Held-Karp).

    return_to_start: finish back at stop 0. fixed_end: the last stop stays last.
    Otherwise the tour may end at whichever stop is cheapest.
    """
    n = len(seconds)
    if n <= 2:
        return list(range(n))
    if n > MAX_TOUR_STOPS + 1:
        raise ValueError(f"at most {MAX_TOUR_STOPS} stops after the start")
    last = n - 1 if fixed_end else None
    middle = [i for i in range(1, n) if i != last]
    # dp[(subset, j)] = (cost, prev): start at 0, visit `subset` (bitmask over middle), end at j
    dp: dict[tuple[int, int], tuple[float, int]] = {}
    for k, j in enumerate(middle):
        dp[(1 << k, j)] = (seconds[0][j], 0)
    for size in range(2, len(middle) + 1):
        for combo in itertools.combinations(range(len(middle)), size):
            mask = sum(1 << k for k in combo)
            for k in combo:
                j = middle[k]
                prev_mask = mask & ~(1 << k)
                best = (float("inf"), -1)
                for k2 in combo:
                    if k2 == k:
                        continue
                    i = middle[k2]
                    c = dp[(prev_mask, i)][0] + seconds[i][j]
                    if c < best[0]:
                        best = (c, i)
                dp[(mask, j)] = best
    full = (1 << len(middle)) - 1

    def closing(j: int) -> float:
        if return_to_start:
            return seconds[j][0]
        if last is not None:
            return seconds[j][last]
        return 0.0

    if not middle:
        return [0, last] if last is not None else [0]
    end = min(middle, key=lambda j: dp[(full, j)][0] + closing(j))
    order, mask, j = [], full, end
    while j != 0:
        order.append(j)
        _, prev = dp[(mask, j)]
        mask &= ~(1 << middle.index(j))
        j = prev
    order = [0] + order[::-1]
    if last is not None:
        order.append(last)
    return order


__all__ = ["Router", "best_order", "decode_polyline6", "MODES", "PREFERENCES", "WebError"]

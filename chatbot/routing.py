"""Routes, tours and reachable areas from Valhalla (public FOSSGIS instance by default).

Tours: one Valhalla matrix request, then an exact solver for the visiting order (Held-Karp,
up to 12 stops), then one route request through the stops in that order.
"""

from __future__ import annotations

import itertools
import json
import math

from chatbot.geo import haversine_m
from chatbot.settings import Settings
from chatbot.web import WebError, get_json

MODES = {"walk": "pedestrian", "drive": "auto", "bike": "bicycle", "wheelchair": "pedestrian"}
PREFERENCES = ("fastest", "lit_streets", "quiet", "less_hills", "accessible",
               "avoid_dangerous_buildings")
MAX_TOUR_STOPS = 12
MAX_ROUTE_LOCATIONS = 10  # what the public Valhalla instance takes in one route request
# Points sit on one street when the walk between the two ends is about the straight line.
STREET_MAX_M = 3000
STREET_DETOUR = 1.5


def order_along(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Distinct (lat, lon) points in the order they sit along the line they roughly form (their
    principal axis), as cameras do along a street."""
    pts = list(dict.fromkeys(points))
    if len(pts) < 3:
        return sorted(pts)
    lat0 = sum(p[0] for p in pts) / len(pts)
    lon0 = sum(p[1] for p in pts) / len(pts)
    k = math.cos(math.radians(lat0))
    xy = [((lon - lon0) * k, lat - lat0) for lat, lon in pts]
    sxx = sum(x * x for x, _ in xy)
    syy = sum(y * y for _, y in xy)
    sxy = sum(x * y for x, y in xy)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)
    return [p for _, p in sorted(zip((x * ux + y * uy for x, y in xy), pts))]


def drop_spurs(coords: list[list[float]]) -> list[list[float]]:
    """A line without its out-and-back side trips (A, B, A becomes A): a route through a point
    just off a street steps out to it and straight back."""
    out: list[list[float]] = []
    for p in coords:
        if len(out) >= 2 and out[-2] == p:
            out.pop()
        elif not out or out[-1] != p:
            out.append(p)
    return out


def length_m(coords: list[list[float]]) -> float:
    return sum(haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))


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
        self._streets: dict[tuple, list[list[float]] | None] = {}

    def _call(self, action: str, body: dict, timeout: float = 30.0) -> dict:
        text = json.dumps(body, separators=(",", ":"))
        if len(text) > 1500:  # polygons to avoid outgrow a URL: post the body instead
            return get_json(f"{self.s.valhalla_url}/{action}", user_agent=self.s.user_agent,
                            data=text.encode(), timeout=timeout)
        return get_json(f"{self.s.valhalla_url}/{action}", user_agent=self.s.user_agent,
                        params={"json": text}, timeout=timeout)

    def route(self, stops: list[tuple[float, float]], mode: str, prefer: list[str],
              avoid: list[list[list[float]]] | None = None) -> dict:
        """`avoid`: rings ([[lon, lat], ...]) whose streets the route must not use."""
        body = {
            "locations": [{"lat": lat, "lon": lon, "type": "break"} for lat, lon in stops],
            "costing": MODES[mode],
            "costing_options": costing_options(mode, prefer),
            "directions_options": {"units": "kilometers", "language": "en-US"},
        }
        if avoid:
            body["exclude_polygons"] = avoid
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

    def street_through(self, points: list[tuple[float, float]]) -> list[list[float]] | None:
        """The stretch of street that (lat, lon) points sit along, as [[lon, lat], ...]: the walk
        through them from one end to the other. None when they aren't on one street (fewer than
        two places, too far apart, or the walk wanders well off the straight line). Remembered,
        since the same cameras are asked about again and again."""
        pts = order_along(points)
        key = tuple(pts)
        if key in self._streets:
            return self._streets[key]
        line = None
        straight = haversine_m(*pts[0], *pts[-1]) if len(pts) >= 2 else 0
        if 15 <= straight <= STREET_MAX_M:
            if len(pts) > MAX_ROUTE_LOCATIONS:  # keep both ends and an even spread between
                last = len(pts) - 1
                pts = [pts[round(i * last / (MAX_ROUTE_LOCATIONS - 1))]
                       for i in range(MAX_ROUTE_LOCATIONS)]
            coords = drop_spurs(self.route(pts, "walk", [])["coordinates"])
            if len(coords) >= 2 and length_m(coords) <= STREET_DETOUR * straight + 60:
                line = coords
        if len(self._streets) >= 64:
            self._streets.pop(next(iter(self._streets)))
        self._streets[key] = line
        return line

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


__all__ = ["Router", "best_order", "decode_polyline6", "drop_spurs", "order_along", "MODES",
           "PREFERENCES", "WebError"]

"""Collection rounds: the road distances between bins, and the routes a day's trucks drive.

- `Matrix` holds road kilometres and driving seconds between every pair of points (the depot
  first, then the bins), from Valhalla's `truck` costing, fetched a row at a time and kept on
  disk pair by pair (bins don't move), so a scenario run costs the public server nothing once
  the pairs are in. Pairs not fetched yet (or when Valhalla can't answer) are estimated from
  straight lines, and the matrix says how many.
- `solve` splits a day's bins into trips that fit the truck (Clarke and Wright's savings
  algorithm) and orders each trip with 2-opt. Small and transparent, and on small cases within a
  few per cent of the exact best (tests/test_waste_scenario.py checks it against Held-Karp).
- `plan_round` turns trips into a day's work: trucks within a shift, each stop's arrival time,
  kilometres, driving, lifting and unloading time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chatbot.geo import haversine_m
from chatbot.scenarios.waste_model import Bin, Stop, Trip
from chatbot.web import WebError, get_json

log = logging.getLogger(__name__)

MAX_PAIRS = 100  # what the public Valhalla instance takes in one matrix request (sources x targets)
REQUEST_GAP_S = 1.2  # between requests: be gentle with the public server
FETCH_PER_RUN = 15  # new matrix requests one run may make; the rest waits for the next runs
ROUTE_LOCATIONS = 10  # what the public Valhalla instance takes in one route request
# A crane truck that empties bell and underground containers: about 10 m and 26 t loaded.
TRUCK = {"truck": {"length": 10.0, "weight": 26.0, "height": 3.9, "width": 2.55}}
# Straight-line fallback: roads are about this much longer than the crow flies in a city ...
DETOUR = 1.35
# ... and a heavy truck averages about this speed between stops.
FALLBACK_KMH = 20.0


@dataclass
class Matrix:
    points: list[tuple[float, float]]  # (lat, lon); index 0 is the depot
    km: list[list[float]]
    seconds: list[list[float]]
    source: str  # "valhalla" | "partly estimated" | "straight lines"
    estimated: int = 0  # pairs estimated from straight lines

    def index(self) -> dict[tuple[float, float], int]:
        return {p: i for i, p in enumerate(self.points)}


def straight_matrix(points: Sequence[tuple[float, float]]) -> tuple[list[list[float]], list[list[float]]]:
    km = [[haversine_m(a[0], a[1], b[0], b[1]) / 1000 * DETOUR for b in points] for a in points]
    sec = [[d / FALLBACK_KMH * 3600 for d in row] for row in km]
    return km, sec


def _key(points: Sequence[tuple[float, float]]) -> str:
    text = json.dumps([[round(a, 6), round(b, 6)] for a, b in points])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _pt(p: tuple[float, float]) -> str:
    return f"{p[0]:.6f},{p[1]:.6f}"


class MatrixSource:
    """Road distances and times between points from Valhalla's truck profile, kept on disk pair
    by pair (bins don't move, so a pair is fetched once, ever). A run fetches at most
    FETCH_PER_RUN requests of the pairs it lacks, one source and up to MAX_PAIRS targets at a
    time, and estimates the rest from straight lines until a later run has them."""

    def __init__(self, valhalla_url: str, user_agent: str, cache_dir: Path,
                 fetch_per_run: int = FETCH_PER_RUN):
        self.url = valhalla_url
        self.user_agent = user_agent
        self.dir = cache_dir
        self.fetch_per_run = fetch_per_run
        self._pairs: dict[str, list[float]] | None = None

    @property
    def _pairs_path(self) -> Path:
        return self.dir / "waste_pairs.json"

    def _load(self) -> dict[str, list[float]]:
        if self._pairs is None:
            try:
                self._pairs = json.loads(self._pairs_path.read_text())
            except (OSError, ValueError):
                self._pairs = {}
        return self._pairs

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._pairs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._pairs, separators=(",", ":")))
        tmp.replace(self._pairs_path)

    def _row(self, source: tuple[float, float], targets: list[tuple[float, float]]) -> None:
        body = {"sources": [{"lat": source[0], "lon": source[1]}],
                "targets": [{"lat": a, "lon": b} for a, b in targets],
                "costing": "truck", "costing_options": TRUCK, "units": "kilometers"}
        data = get_json(f"{self.url}/sources_to_targets", user_agent=self.user_agent,
                        data=json.dumps(body, separators=(",", ":")).encode(), timeout=60)
        pairs = self._load()
        for row in data["sources_to_targets"]:
            for cell in row:
                t = targets[cell["to_index"]]
                if cell.get("time") is not None and cell.get("distance") is not None:
                    pairs[f"{_pt(source)}|{_pt(t)}"] = [round(float(cell["distance"]), 3),
                                                        round(float(cell["time"]), 1)]
                else:  # no road between them: remember that too, so it isn't asked again
                    pairs[f"{_pt(source)}|{_pt(t)}"] = [None, None]

    def matrix(self, depot: tuple[float, float], bins: Sequence[tuple[float, float]]) -> Matrix:
        """The depot and the bins (in this order): cached pairs, up to fetch_per_run new rows,
        and straight-line estimates for whatever is still missing."""
        points = [depot, *bins]
        pairs = self._load()
        requests = 0
        changed = False
        try:
            for a in points:
                missing = [b for b in points if b != a and f"{_pt(a)}|{_pt(b)}" not in pairs]
                for k in range(0, len(missing), MAX_PAIRS):
                    if requests >= self.fetch_per_run:
                        break
                    if requests:
                        time.sleep(REQUEST_GAP_S)
                    self._row(a, missing[k:k + MAX_PAIRS])
                    requests += 1
                    changed = True
        except (WebError, KeyError, ValueError, TypeError) as e:
            log.warning("waste rounds: Valhalla matrix unavailable (%s); estimating the rest", e)
        if changed:
            self._save()
        est_km, est_sec = straight_matrix(points)
        n = len(points)
        km = [[0.0] * n for _ in range(n)]
        sec = [[0.0] * n for _ in range(n)]
        estimated = 0
        for i, a in enumerate(points):
            for j, b in enumerate(points):
                if i == j or a == b:
                    continue
                got = pairs.get(f"{_pt(a)}|{_pt(b)}")
                if got and got[0] is not None:
                    km[i][j], sec[i][j] = got
                else:
                    km[i][j], sec[i][j] = est_km[i][j], est_sec[i][j]
                    estimated += 1
        source = ("valhalla" if not estimated else
                  "straight lines" if estimated >= n * (n - 1) else "partly estimated")
        return Matrix(points, km, sec, source, estimated)

    def cached_line(self, stops: Sequence[tuple[float, float]]) -> list[list[float]] | None:
        """The road line through the stops if an earlier run fetched it, else None (no request)."""
        path = self.dir / f"waste_line_{_key(list(stops))}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def route_line(self, stops: Sequence[tuple[float, float]]) -> list[list[float]]:
        """The road line through (lat, lon) stops in order, as [[lon, lat], ...], from Valhalla
        in pieces of ROUTE_LOCATIONS and kept on disk; straight segments when it can't answer."""
        from chatbot.routing import decode_polyline6

        if len(stops) < 2:
            return [[lon, lat] for lat, lon in stops]
        path = self.dir / f"waste_line_{_key(list(stops))}.json"
        if path.exists():
            return json.loads(path.read_text())
        coords: list[list[float]] = []
        try:
            for i in range(0, len(stops) - 1, ROUTE_LOCATIONS - 1):
                piece = stops[i:i + ROUTE_LOCATIONS]
                body = {"locations": [{"lat": a, "lon": b, "type": "break"} for a, b in piece],
                        "costing": "truck", "costing_options": TRUCK,
                        "directions_options": {"units": "kilometers", "directions_type": "none"}}
                data = get_json(f"{self.url}/route", user_agent=self.user_agent,
                                data=json.dumps(body, separators=(",", ":")).encode(), timeout=45)
                for leg in data["trip"]["legs"]:
                    shape = decode_polyline6(leg["shape"])
                    coords.extend(shape if not coords else shape[1:])
                time.sleep(REQUEST_GAP_S / 2)
        except (WebError, KeyError, ValueError, TypeError) as e:
            log.warning("waste rounds: Valhalla route unavailable (%s); straight lines", e)
            return [[lon, lat] for lat, lon in stops]
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(coords))
        return coords


# -- routes -------------------------------------------------------------------------------------


def route_cost(route: Sequence[int], cost: Sequence[Sequence[float]], depot: int = 0) -> float:
    path = [depot, *route, depot]
    return sum(cost[a][b] for a, b in zip(path, path[1:]))


def two_opt(route: list[int], cost: Sequence[Sequence[float]], depot: int = 0) -> list[int]:
    """Reverse stretches of a trip while that shortens it (on symmetric costs)."""
    path = [depot, *route, depot]
    improved = True
    while improved:
        improved = False
        for i in range(1, len(path) - 2):
            for j in range(i + 1, len(path) - 1):
                a, b, c, d = path[i - 1], path[i], path[j], path[j + 1]
                if cost[a][c] + cost[b][d] < cost[a][b] + cost[c][d] - 1e-9:
                    path[i:j + 1] = reversed(path[i:j + 1])
                    improved = True
    return path[1:-1]


def solve(nodes: Sequence[int], demand: dict[int, float], capacity: float,
          cost: Sequence[Sequence[float]], depot: int = 0) -> list[list[int]]:
    """Trips from the depot through `nodes` and back, none carrying more than `capacity`:
    Clarke and Wright's savings (parallel), then 2-opt on each trip. `cost` may be asymmetric;
    the trips are built on its symmetric mean."""
    sym = {}

    def c(i: int, j: int) -> float:
        k = (i, j) if i < j else (j, i)
        if k not in sym:
            sym[k] = (cost[i][j] + cost[j][i]) / 2
        return sym[k]

    nodes = list(dict.fromkeys(nodes))
    if not nodes:
        return []
    routes: dict[int, list[int]] = {i: [i] for i in nodes}
    load = {i: demand.get(i, 0.0) for i in nodes}
    owner = {i: i for i in nodes}
    savings = sorted(((c(depot, i) + c(depot, j) - c(i, j), i, j)
                      for a, i in enumerate(nodes) for j in nodes[a + 1:]), reverse=True)
    for s, i, j in savings:
        if s <= 0:
            break
        ri, rj = owner[i], owner[j]
        if ri == rj or load[ri] + load[rj] > capacity:
            continue
        a, b = routes[ri], routes[rj]
        if a[-1] == i and b[0] == j:
            new = a + b
        elif a[0] == i and b[-1] == j:
            new = b + a
        elif a[-1] == i and b[-1] == j:
            new = a + b[::-1]
        elif a[0] == i and b[0] == j:
            new = a[::-1] + b
        else:
            continue  # i or j sits inside its trip: joining there would break it
        routes[ri] = new
        load[ri] += load.pop(rj)
        del routes[rj]
        for k in new:
            owner[k] = ri
    symc = [[0.0] * len(cost) for _ in range(len(cost))]
    for k in [depot, *nodes]:
        for m in [depot, *nodes]:
            symc[k][m] = c(k, m) if k != m else 0.0
    out = [two_opt(r, symc, depot) for r in routes.values()]
    # each trip runs in whichever direction is shorter on the real (one-way aware) costs
    out = [r if route_cost(r, cost, depot) <= route_cost(r[::-1], cost, depot) else r[::-1]
           for r in out]
    return sorted(out, key=lambda r: -len(r))


@dataclass(frozen=True)
class Crew:
    truck_m3: float = 20.0
    service_min: float = 4.0  # per stop: position, lift, empty, lower
    unload_min: float = 20.0  # per trip, at the depot
    shift_h: float = 8.0


def plan_round(stream: str, stops: Sequence[tuple[Bin, float, float]], matrix: Matrix,
               crew: Crew) -> list[Trip]:
    """A day's trips for one stream: trips from `solve`, given to as few trucks as fit them in
    a shift (longest first), each stop timed from when the round leaves the depot."""
    idx = matrix.index()
    nodes, demand, by_node = [], {}, {}
    for b, level, load in stops:
        i = idx[(b.lat, b.lon)]
        nodes.append(i)
        demand[i] = demand.get(i, 0.0) + load
        by_node.setdefault(i, []).append((b, level, load))
    routes = solve(nodes, demand, crew.truck_m3, matrix.seconds)
    timed = []
    for r in routes:
        path = [0, *r, 0]
        drive_s = sum(matrix.seconds[a][b] for a, b in zip(path, path[1:]))
        km = sum(matrix.km[a][b] for a, b in zip(path, path[1:]))
        n = sum(len(by_node[i]) for i in r)
        timed.append((r, km, drive_s / 60, n * crew.service_min))
    # trucks: longest trip first onto the truck with the least work that still fits its shift
    busy: list[float] = []
    assigned: list[tuple[int, tuple]] = []
    for t in sorted(timed, key=lambda t: -(t[2] + t[3] + crew.unload_min)):
        work = t[2] + t[3] + crew.unload_min
        fits = [k for k, b in enumerate(busy) if b + work <= crew.shift_h * 60]
        k = min(fits, key=lambda k: busy[k]) if fits else len(busy)
        if k == len(busy):
            busy.append(0.0)
        assigned.append((k, t))
        busy[k] += work
    trips: list[Trip] = []
    clock = [0.0] * len(busy)
    for k, (r, km, drive_min, service_min) in sorted(assigned, key=lambda a: a[0]):
        start = clock[k]
        at = start
        prev = 0
        timed_stops = []
        for i in r:
            at += matrix.seconds[prev][i] / 60
            for b, level, load in by_node[i]:
                timed_stops.append(Stop(b.id, round(at, 1), level, load))
                at += crew.service_min
            prev = i
        at += matrix.seconds[prev][0] / 60 + crew.unload_min
        clock[k] = at
        trips.append(Trip(stream, k, timed_stops, round(start, 1), round(at, 1), round(km, 3),
                          round(drive_min, 1), service_min, crew.unload_min))
    return trips

"""What people ask of the street rules, worked out from the inventories and what the twin
already knows (its places, its bus stops, the street network):

- how a school or a bus stop is served for crossing: the crossings and signals within a short
  walk, the main road beside it and its posted limit, whether it lies in a slow zone;
- where the cycle network breaks: its connected pieces, their loose ends, and the short gaps
  between pieces that a link would close, each with the street it would run along;
- what lies inside a slow zone: its streets, the signs posted there, its crossings, signals
  and schools.

Distances are straight lines, not walks: a crossing 150 m away across a fenced avenue is
still 150 m here. Every result says so.
"""

from __future__ import annotations

import math
from collections import defaultdict

from chatbot.geo import haversine_m
from chatbot.streets.network import KX, KY, cumulative, dist_m, project

SCHOOL_WALK_M = 250.0  # a crossing this close to a school serves it
STOP_WALK_M = 120.0  # and this close to a bus stop
MAIN_ROAD_M = 60.0  # a school or stop this close to a main road is beside it
JOIN_M = 25.0  # two cycle lines this close are joined
GAP_M = 1200.0  # a gap between two pieces of the network shorter than this is worth closing
COVERED_M = 1000.0  # the inventories cover a place with one of their records this close


def inside(ring: list[list[float]], lon: float, lat: float) -> bool:
    """Ray casting: whether (lon, lat) lies inside a closed ring."""
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            hit = not hit
        j = i
    return hit


def ring_area_m2(ring: list[list[float]]) -> float:
    s = 0.0
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        s += (x1 * KX) * (y2 * KY) - (x2 * KX) * (y1 * KY)
    return abs(s) / 2


def nearest(points: list[dict], lat: float, lon: float, max_m: float) -> list[tuple[float, dict]]:
    out = []
    for p in points:
        if abs(p["lat"] - lat) > max_m / 100_000 or abs(p["lon"] - lon) > max_m / 85_000:
            continue
        d = haversine_m(lat, lon, p["lat"], p["lon"])
        if d <= max_m:
            out.append((d, p))
    return sorted(out, key=lambda t: t[0])


# -- crossing near a school or a bus stop ------------------------------------------------------------


def crossing_report(streets, lat: float, lon: float, walk_m: float) -> dict:
    """How a point is served for crossing: crossings and signals within `walk_m`, the main road
    beside it with its posted limit, and the slow zone it lies in."""
    xs = nearest(streets.records("crossings"), lat, lon, walk_m)
    sg = nearest(streets.records("signals"), lat, lon, walk_m)
    road = streets.main_road_near(lon, lat, MAIN_ROAD_M)
    limit = None
    if road is not None:
        link, _ = road
        mid = link.coords[len(link.coords) // 2]
        piece = streets.limit_at(lon, lat, MAIN_ROAD_M) or streets.limit_at(mid[0], mid[1], 30)
        limit = piece
    zone = streets.zone_at(lon, lat)
    fast = road is not None and (limit is None or limit["kmh"] >= 50)
    served = bool(xs or sg)
    if served:
        verdict = "served"
    elif road is None:
        verdict = "quiet"  # no main road beside it: side streets only
    else:
        verdict = "gap" if fast else "slow_road"
    return {
        "crossings": [{"id": x["id"], "name": x["name"], "distance_m": round(d)} for d, x in xs[:5]],
        "signals": [{"id": s["id"], "name": s["junction"], "distance_m": round(d)} for d, s in sg[:5]],
        "nearest_crossing_m": round(xs[0][0]) if xs else None,
        "nearest_signal_m": round(sg[0][0]) if sg else None,
        "main_road": road[0].label if road else None,
        "main_road_m": round(road[1]) if road else None,
        "main_road_limit_kmh": limit["kmh"] if limit else None,
        "limit_how": limit["tier"] if limit else None,
        "zone": f"{zone['kind']} {zone['name']} ({zone['status']})" if zone else None,
        "verdict": verdict,
    }


VERDICTS = {"served": "A crossing or signal within a short walk",
            "gap": "Beside a main road at 50 km/h or more with no crossing or signal near",
            "slow_road": "Beside a main road posted below 50, no crossing near",
            "quiet": "Side streets only, no main road beside it"}
VERDICT_COLORS = {"served": "#5fd3a0", "gap": "#ff5a4e", "slow_road": "#e8c26a", "quiet": "#8798b0"}


def covered(streets, lat: float, lon: float) -> bool:
    """Whether the national inventories cover a place: they hold the Republic's streets only,
    so north of the buffer zone a school has no crossing in them, whatever is on the ground."""
    pts = streets.records("crossings") + streets.records("signals") + streets.records("speed_limits")
    return bool(nearest(pts, lat, lon, COVERED_M))


def schools_report(streets, near: tuple[float, float] | None = None, radius_m: float = 1500,
                   name: str | None = None) -> list[dict]:
    from chatbot.geo import loose
    out = []
    for s in streets.schools():
        if not covered(streets, s["lat"], s["lon"]):
            continue
        if near is not None and haversine_m(near[0], near[1], s["lat"], s["lon"]) > radius_m:
            continue
        if name and loose(name) not in loose(s["name"]):
            continue
        rep = crossing_report(streets, s["lat"], s["lon"], SCHOOL_WALK_M)
        out.append({**s, **rep})
    order = {"gap": 0, "slow_road": 1, "quiet": 2, "served": 3}
    return sorted(out, key=lambda r: (order[r["verdict"]], -(r["main_road_limit_kmh"] or 0), r["name"]))


def stops_report(streets, tt, near: tuple[float, float] | None = None, radius_m: float = 1500) -> list[dict]:
    out = []
    for stop in tt.stops:
        if not tt.lines_at(stop):
            continue
        if near is not None and haversine_m(near[0], near[1], stop.lat, stop.lon) > radius_m:
            continue
        if near is None and not (33.24 <= stop.lon <= 33.48 and 35.07 <= stop.lat <= 35.23):
            continue
        if not covered(streets, stop.lat, stop.lon):
            continue
        rep = crossing_report(streets, stop.lat, stop.lon, STOP_WALK_M)
        if rep["verdict"] == "quiet":
            continue  # a stop on a side street is crossed anywhere
        out.append({"id": f"stop-{stop.id}", "name": stop.name, "lat": stop.lat, "lon": stop.lon,
                    "lines": ", ".join(ln.short for ln in tt.lines_at(stop)[:6]), **rep})
    order = {"gap": 0, "slow_road": 1, "served": 3}
    return sorted(out, key=lambda r: (order.get(r["verdict"], 2), -(r["main_road_limit_kmh"] or 0), r["name"]))


# -- the cycle network -------------------------------------------------------------------------------


def _point_line(p, line) -> float:
    return min(project(p, line[i], line[i + 1])[1] for i in range(len(line) - 1))


def cycle_network(streets) -> dict:
    """The cycleways as connected pieces, their loose ends, and the gaps worth closing."""
    lines = []  # (cycleway record, polyline)
    for rec in streets.records("cycleways"):
        for line in rec["lines"]:
            lines.append((rec, line))
    n = len(lines)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ends: list[tuple[int, list[float], bool]] = []  # (line, end point, joined to another line)
    for i, (_, line) in enumerate(lines):
        for end in (line[0], line[-1]):
            joined = False
            for j, (_, other) in enumerate(lines):
                if i == j:
                    continue
                if abs(other[0][0] - end[0]) > 0.05 and abs(other[-1][0] - end[0]) > 0.05:
                    continue
                if _point_line(end, other) <= JOIN_M:
                    parent[find(i)] = find(j)
                    joined = True
            # A line that turns back on itself at this end (a loop) is joined too.
            if not joined and dist_m(line[0], line[-1]) <= JOIN_M and len(line) > 2:
                joined = True
            ends.append((i, end, joined))
    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        comps[find(i)].append(i)
    comp_of = {i: c for c, members in comps.items() for i in members}
    pieces = []
    for k, (root, members) in enumerate(sorted(comps.items(), key=lambda t: -sum(cumulative(lines[i][1])[-1] for i in t[1]))):
        length = sum(cumulative(lines[i][1])[-1] for i in members)
        names = list(dict.fromkeys(lines[i][0]["name"] for i in members))
        pieces.append({"piece": k + 1, "root": root, "length_m": round(length), "routes": names,
                       "ids": list(dict.fromkeys(lines[i][0]["id"] for i in members))})
    piece_no = {p["root"]: p["piece"] for p in pieces}
    loose_ends = [(i, e) for i, e, joined in ends if not joined]
    # Gaps: for each pair of pieces, their nearest pair of loose ends, when short enough.
    best: dict[tuple[int, int], tuple[float, list, list]] = {}
    for a in range(len(loose_ends)):
        ia, ea = loose_ends[a]
        for b in range(a + 1, len(loose_ends)):
            ib, eb = loose_ends[b]
            ca, cb = comp_of[ia], comp_of[ib]
            if ca == cb:
                continue
            d = dist_m(ea, eb)
            key = (min(ca, cb), max(ca, cb))
            if d <= GAP_M and (key not in best or d < best[key][0]):
                best[key] = (d, ea, eb)
    gaps = []
    for (ca, cb), (d, ea, eb) in sorted(best.items(), key=lambda t: t[1][0]):
        mid = [(ea[0] + eb[0]) / 2, (ea[1] + eb[1]) / 2]
        street = streets.street_label_at(mid[0], mid[1], 60)
        piece = streets.limit_at(mid[0], mid[1], 40)
        gaps.append({"id": f"gap-{piece_no[ca]}-{piece_no[cb]}", "between": [piece_no[ca], piece_no[cb]],
                     "length_m": round(d), "from": [round(ea[0], 6), round(ea[1], 6)],
                     "to": [round(eb[0], 6), round(eb[1], 6)], "street": street,
                     "street_limit_kmh": piece["kmh"] if piece else None})
    dead = [{"piece": piece_no[comp_of[i]], "lon": round(e[0], 6), "lat": round(e[1], 6),
             "route": lines[i][0]["name"], "id": lines[i][0]["id"]} for i, e in loose_ends]
    return {"pieces": pieces, "gaps": gaps, "loose_ends": dead,
            "total_m": round(sum(p["length_m"] for p in pieces)), "lines": lines, "comp_of": comp_of,
            "piece_no": piece_no}


# -- inside a slow zone -------------------------------------------------------------------------------


def zone_contents(streets, zone: dict) -> dict:
    """The streets inside a zone (by length), the speed signs posted inside it, and its
    crossings, signals and schools."""
    ring = zone["ring"]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    box = (min(xs), min(ys), max(xs), max(ys))

    def within(lon, lat):
        return box[0] <= lon <= box[2] and box[1] <= lat <= box[3] and inside(ring, lon, lat)
    net = streets.network.current()
    by_street: dict[str, float] = defaultdict(float)
    links = []
    if net is not None:
        for link in net.links:
            mid = link.coords[len(link.coords) // 2]
            if within(mid[0], mid[1]):
                by_street[link.label or "unnamed street"] += link.length
                links.append(link)
    signs = [s for s in streets.records("speed_limits") if within(s["lon"], s["lat"])]
    by_value: dict[int, int] = defaultdict(int)
    for s in signs:
        by_value[s["kmh"]] += 1
    return {
        "area_km2": round(ring_area_m2(ring) / 1e6, 2),
        "streets": [{"street": k, "length_m": round(v)} for k, v in sorted(by_street.items(), key=lambda t: -t[1])],
        "street_km": round(sum(by_street.values()) / 1000, 1),
        "links": links,
        "signs": signs, "signs_by_kmh": dict(sorted(by_value.items())),
        "crossings": [x for x in streets.records("crossings") if within(x["lon"], x["lat"])],
        "signals": [s for s in streets.records("signals") if within(s["lon"], s["lat"])],
        "schools": [s for s in streets.schools() if within(s["lon"], s["lat"])],
        "entrances": [e for e in streets.records("zone_entrances") if e.get("zone_name") == zone["name"]],
    }


def bbox_of(coords) -> list[float]:
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def centre_of(coords) -> list[float]:
    cum = cumulative(coords)
    from chatbot.streets.network import point_at
    p = point_at(coords, cum, cum[-1] / 2)
    return [round(p[0], 6), round(p[1], 6)]


def length_of(lines) -> float:
    return sum(cumulative(line)[-1] for line in lines)


__all__ = ["inside", "crossing_report", "schools_report", "stops_report", "cycle_network",
           "zone_contents", "VERDICTS", "VERDICT_COLORS", "ring_area_m2", "math"]

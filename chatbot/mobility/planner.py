"""Departures from a stop and bus trips from A to B, over the timetable with the live feed.

Departures are the timetable's, with the live estimate where a bus on that trip is on the road
(or the feed has a believable delay for it). Trips are walk, ride, walk: every direct ride and
every ride with one change (at the same stop or a short walk away) that leaves within the next
90 minutes, ranked by when they get you there with a little weight against changes and
walking. Walking is timed at 1.25 m/s over 1.3 times the straight line here; the tool asks
Valhalla for the real walking legs of the trip it draws.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from chatbot.geo import loose, norm
from chatbot.mobility.gtfs import M_PER_DEG_LAT, TZ, Stop, Timetable, Trip, m_per_deg_lon
from chatbot.mobility.realtime import Snapshot, feed_delay

WALK_MPS = 1.25
DETOUR = 1.3
MAX_WALK_M = 800
TRANSFER_M = 200  # walk between stops to change buses
MIN_CHANGE_S = 120
MAX_WAIT_CHANGE_S = 45 * 60
WINDOW_MIN = 90
CHANGE_PENALTY_S = 300
WALK_WEIGHT = 0.5  # a minute of walking counts as 1.5 minutes of riding
SAME_PLACE_M = 400  # closer than this, just walk


def metres(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    return math.hypot((a_lon - b_lon) * m_per_deg_lon((a_lat + b_lat) / 2), (a_lat - b_lat) * M_PER_DEG_LAT)


def walk_s(m: float) -> float:
    return m * DETOUR / WALK_MPS


def hhmm(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


def stops_named(tt: Timetable, text: str, limit: int = 8) -> list[Stop]:
    """Stops by name or code, best first: "Solomos" finds the four Solomos Square stops."""
    q = norm(text)
    ql = loose(text)
    if not q:
        return []
    scored = []
    for s in tt.stops:
        if not tt._dep_s[s.idx] and not len(tt.trips_calling(s.idx)):
            continue
        name, el = norm(s.name), norm(s.name_el or "")
        if q in (s.id, s.code.lower()) or q == name:
            score = 100
        elif name.startswith(q) or el.startswith(q):
            score = 70
        elif q in name or (el and q in el):
            score = 50
        elif len(ql) >= 3 and (ql in loose(s.name) or ql in loose(s.name_el or "")):
            score = 35
        else:
            continue
        scored.append((-score, len(s.name), s.name, s))
    scored.sort(key=lambda x: x[:3])
    return [s for *_, s in scored[:limit]]


# -- realtime on a timetable trip -------------------------------------------------------------


def live_index(snap: Snapshot | None) -> dict[tuple, dict]:
    """Live buses by (route, direction, start time, service date)."""
    if snap is None:
        return {}
    return {(b["route_id"], b["direction"], b["start_time"], b["start_date"]): b
            for b in snap.vehicles if b.get("trip") is not None}


def _start_text(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def realtime_for(trip: Trip, day, pos: int, live: dict, snap: Snapshot | None,
                 stop_id: str | None = None) -> dict:
    """{bus, delay_s, source, passed} for this trip at stop position `pos`."""
    key = (trip.route_id, trip.direction, _start_text(trip.start), day.strftime("%Y%m%d"))
    bus = live.get(key)
    out: dict = {}
    if bus:
        out["bus"] = bus["id"]
        stop_now = (bus.get("progress") or {}).get("stop", 0)
        if bus.get("finished") or stop_now > pos:
            out["passed"] = True
            return out
        if bus.get("delay_s") is not None:
            out["delay_s"], out["source"] = bus["delay_s"], bus["delay_source"]
            return out
    d = feed_delay((snap.updates if snap else {}).get(key[:3]), pos, stop_id)
    if d is not None:
        out["delay_s"], out["source"] = d, "feed"
    return out


def departures(tt: Timetable, stops: list[Stop], start: datetime, minutes: float = 60,
               limit: int = 12, lines: set[str] | None = None,
               snap: Snapshot | None = None) -> list[dict]:
    """The next departures from these stops, soonest (expected) first."""
    live = live_index(snap)
    out = []
    # Look a little back too: a bus due two minutes ago that runs five late is still to come.
    for when, trip, pos in tt.departures([s.idx for s in stops], start - timedelta(minutes=10),
                                         minutes + 10, lines):
        day = (when - timedelta(seconds=trip.dep[pos])).date()
        rt = realtime_for(trip, day, pos, live, snap, tt.stops[trip.stops[pos]].id)
        if rt.get("passed"):
            continue
        expected = when + timedelta(seconds=rt["delay_s"]) if "delay_s" in rt else None
        effective = expected or when
        if effective < start - timedelta(seconds=30) or effective > start + timedelta(minutes=minutes):
            continue
        line = tt.lines[trip.line_id]
        stop = tt.stops[trip.stops[pos]]
        out.append({
            "line": line.short, "line_id": line.id, "operator": line.operator,
            "color": line.color, "text_color": line.text_color, "headsign": trip.headsign,
            "stop_id": f"stop-{stop.id}", "stop": stop.name,
            "scheduled": hhmm(when), "expected": hhmm(expected) if expected else None,
            "realtime": expected is not None, "delay_s": rt.get("delay_s"),
            "delay_source": rt.get("source"), "bus": rt.get("bus"),
            "in_min": max(0, round((effective - start).total_seconds() / 60)),
            "_at": effective, "_trip": trip.idx, "_pos": pos, "_day": day,
        })
    out.sort(key=lambda d: (d["_at"], d["line"]))
    return out[:limit]


def public(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


# -- trips from A to B --------------------------------------------------------------------------


@dataclass
class Ride:
    trip: Trip
    day: object
    board: int  # positions in the trip
    alight: int
    dep: datetime
    arr: datetime


def _base(tt: Timetable, day) -> datetime:
    return tt.midnight(day)


def plan(tt: Timetable, origin: tuple[float, float], dest: tuple[float, float], start: datetime,
         snap: Snapshot | None = None, max_walk_m: float = MAX_WALK_M,
         window_min: float = WINDOW_MIN, options: int = 3, arrive_by: datetime | None = None) -> dict:
    """Walk, ride (one or two buses), walk from origin to dest (lat, lon), leaving after start.
    Returns {"options": [...], "why_none": str | None, "straight_m"}."""
    straight = metres(*origin, *dest)
    if straight < SAME_PLACE_M:
        return {"options": [], "straight_m": round(straight),
                "why_none": f"the two places are {round(straight)} m apart: walking is quicker"}
    o_stops = tt.near(origin[0], origin[1], max_walk_m, limit=14)
    d_stops = tt.near(dest[0], dest[1], max_walk_m, limit=14, arriving=True)
    if not o_stops or not d_stops:
        where = "the start" if not o_stops else "the destination"
        return {"options": [], "straight_m": round(straight),
                "why_none": f"no bus stop within {max_walk_m} m of {where}"}
    to_dest = {s.idx: walk_s(d) for d, s in d_stops}
    # Trips that reach a destination stop, with the positions where they do.
    reach: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for s, w in to_dest.items():
        for t in tt.trips_calling(s):
            trip = tt.trips[t]
            for pos, x in enumerate(trip.stops):
                if x == s and pos > 0:
                    reach[t].append((pos, w))
    live = live_index(snap)
    best: dict[tuple, tuple[float, list[Ride], float, float]] = {}

    def offer(rides: list[Ride], walk_first: float, walk_last: float, walk_mid: float) -> None:
        arrive = rides[-1].arr + timedelta(seconds=walk_last)
        if arrive_by and arrive > arrive_by:
            return
        walking = walk_first + walk_last + walk_mid
        score = (arrive - start).total_seconds() + CHANGE_PENALTY_S * (len(rides) - 1) + WALK_WEIGHT * walking
        key = tuple(r.trip.line_id for r in rides)
        if key not in best or score < best[key][0]:
            best[key] = (score, rides, walk_first, walk_last + walk_mid)

    neighbours: dict[int, list[tuple[int, float]]] = {}

    def near_stop(s: int) -> list[tuple[int, float]]:
        if s not in neighbours:
            st = tt.stops[s]
            neighbours[s] = [(x.idx, walk_s(d)) for d, x in tt.near(st.lat, st.lon, TRANSFER_M, limit=5)]
            if not any(i == s for i, _ in neighbours[s]):
                neighbours[s].insert(0, (s, 0.0))
        return neighbours[s]

    for dist, o in o_stops:
        w0 = walk_s(dist)
        seen_lines: dict[tuple[str, int], int] = defaultdict(int)
        for when, trip, pos in tt.departures([o.idx], start + timedelta(seconds=w0), window_min):
            key = (trip.line_id, trip.direction)
            if seen_lines[key] >= 2:  # the next two of each line from each stop are enough
                continue
            seen_lines[key] += 1
            day = (when - timedelta(seconds=trip.dep[pos])).date()
            base = _base(tt, day)
            rt = realtime_for(trip, day, pos, live, snap, o.id)
            if rt.get("passed"):
                continue
            # Direct: the best place to get off this trip.
            for p2, w2 in reach.get(trip.idx, []):
                if p2 > pos:
                    offer([Ride(trip, day, pos, p2, when, base + timedelta(seconds=trip.arr[p2]))], w0, w2, 0)
            # One change, at any later stop of this trip.
            for k in range(pos + 1, len(trip.stops)):
                arr_k = base + timedelta(seconds=trip.arr[k])
                if (arr_k - start).total_seconds() > (window_min + 60) * 60:
                    break
                for s2, wt in near_stop(trip.stops[k]):
                    lo = arr_k + timedelta(seconds=max(MIN_CHANGE_S, wt + 60))
                    for when2, trip2, pos2 in tt.departures([s2], lo, MAX_WAIT_CHANGE_S / 60):
                        if trip2.line_id == trip.line_id or trip2.idx not in reach:
                            continue
                        day2 = (when2 - timedelta(seconds=trip2.dep[pos2])).date()
                        base2 = _base(tt, day2)
                        for p3, w3 in reach[trip2.idx]:
                            if p3 > pos2:
                                offer([Ride(trip, day, pos, k, when, arr_k),
                                       Ride(trip2, day2, pos2, p3, when2, base2 + timedelta(seconds=trip2.arr[p3]))],
                                      w0, w3, wt)
    ranked = sorted(best.values(), key=lambda x: x[0])
    # Drop options that a better one beats on every count (arrives no later, fewer changes).
    picked: list[tuple[float, list[Ride], float, float]] = []
    for cand in ranked:
        arr = cand[1][-1].arr
        if any(p[1][-1].arr <= arr and len(p[1]) <= len(cand[1]) and p[1][0].dep >= cand[1][0].dep
               for p in picked):
            continue
        picked.append(cand)
        if len(picked) >= options:
            break
    out = [describe(tt, rides, w_first, w_rest, origin, dest, live, snap)
           for _, rides, w_first, w_rest in picked]
    why = None
    if not out:
        why = (f"no bus links stops near the two places within the next {window_min} minutes "
               "with at most one change")
    walk_min = round(walk_s(straight) / 60)
    result = {"options": out, "straight_m": round(straight), "why_none": why, "walk_min": walk_min}
    if out and start + timedelta(minutes=walk_min) <= out[0]["_arrive"]:
        result["walking_is_quicker"] = (f"walking all the way takes about {walk_min} min and gets "
                                        f"there by {hhmm(start + timedelta(minutes=walk_min))}, "
                                        f"before the best bus option ({out[0]['arrive']})")
    return result


def describe(tt: Timetable, rides: list[Ride], w_first: float, w_rest: float,
             origin: tuple[float, float], dest: tuple[float, float], live: dict,
             snap: Snapshot | None) -> dict:
    legs = []
    first = rides[0]
    board0 = tt.stops[first.trip.stops[first.board]]
    leave = first.dep - timedelta(seconds=w_first)
    legs.append({"mode": "walk", "to": board0.name, "to_id": f"stop-{board0.id}",
                 "metres": round(metres(origin[0], origin[1], board0.lat, board0.lon) * DETOUR),
                 "minutes": max(1, round(w_first / 60))})
    for i, r in enumerate(rides):
        line = tt.lines[r.trip.line_id]
        a, b = tt.stops[r.trip.stops[r.board]], tt.stops[r.trip.stops[r.alight]]
        rt = realtime_for(r.trip, r.day, r.board, live, snap, a.id)
        leg = {"mode": "bus", "line": line.short, "line_id": line.id, "operator": line.operator,
               "color": line.color, "headsign": r.trip.headsign,
               "from": a.name, "from_id": f"stop-{a.id}", "to": b.name, "to_id": f"stop-{b.id}",
               "departs": hhmm(r.dep), "arrives": hhmm(r.arr), "stops": r.alight - r.board,
               "minutes": round((r.arr - r.dep).total_seconds() / 60),
               "_trip": r.trip.idx, "_board": r.board, "_alight": r.alight}
        if "delay_s" in rt:
            leg["expected_departure"] = hhmm(r.dep + timedelta(seconds=rt["delay_s"]))
            leg["delay_min"] = round(rt["delay_s"] / 60)
            leg["realtime"] = rt.get("source")
        if rt.get("bus"):
            leg["bus"] = rt["bus"]
        legs.append(leg)
        if i + 1 < len(rides):
            nxt = rides[i + 1]
            c = tt.stops[nxt.trip.stops[nxt.board]]
            gap = metres(b.lat, b.lon, c.lat, c.lon)
            wait = round((nxt.dep - r.arr).total_seconds() / 60)
            legs.append({"mode": "change", "at": b.name, "to": c.name, "to_id": f"stop-{c.id}",
                         "metres": round(gap * DETOUR), "wait_min": wait})
    last = rides[-1]
    end = tt.stops[last.trip.stops[last.alight]]
    end_walk = metres(end.lat, end.lon, dest[0], dest[1])
    legs.append({"mode": "walk", "from": end.name, "from_id": f"stop-{end.id}",
                 "metres": round(end_walk * DETOUR), "minutes": max(1, round(walk_s(end_walk) / 60))})
    arrive = last.arr + timedelta(seconds=walk_s(end_walk))
    return {"leave": hhmm(leave), "arrive": hhmm(arrive),
            "minutes": round((arrive - leave).total_seconds() / 60), "changes": len(rides) - 1,
            "walk_m": sum(leg.get("metres", 0) for leg in legs if leg["mode"] in ("walk", "change")),
            "lines": [tt.lines[r.trip.line_id].short for r in rides], "legs": legs,
            "_leave": leave, "_arrive": arrive}

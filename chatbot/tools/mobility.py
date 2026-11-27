"""Mobility tools: the buses on the road now, departures from a stop, bus trips from A to B,
and road speeds against free flow. They read chatbot/mobility, which keeps one fetch of each
feed for every page and answer."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from chatbot.geo import haversine_m
from chatbot.mobility import planner
from chatbot.mobility.gtfs import TZ, line_order, slice_shape
from chatbot.mobility.realtime import delay_text
from chatbot.mobility.roads import (LEGEND, STALLED_LEGEND, parse_moment, segment_feature, since_words,
                                    slowest, usually_slowest)
from chatbot.store import point
from chatbot.tools.base import (NEAR, LocationNotShared, ToolContext, ToolError, ToolSet, local_now,
                                my_location, obj, resolve_point)
from chatbot.web import WebError

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

WALK_COLOR = "#8798b0"
CITY = (33.25, 35.07, 33.47, 35.23)  # Greater Nicosia, lon_min, lat_min, lon_max, lat_max
STOP_COLOR = "#ffae45"
LINES = {"type": "array", "maxItems": 20, "items": {"type": "string"},
         "description": "Line numbers as on the bus, e.g. ['26', '42', 'GSP1'], or line ids "
                        "(line-npt-26)."}


def mobility_of(ctx: ToolContext):
    if ctx.mobility is None:
        raise ToolError("live mobility isn't available in this deployment")
    return ctx.mobility


def lines_matching(tt, wanted: list[str] | None) -> set[str] | None:
    """Line ids for line numbers or ids. A number two operators share (26) means both."""
    if not wanted:
        return None
    out: set[str] = set()
    unknown = []
    for w in wanted:
        w = w.strip()
        key = w.lower().removeprefix("line").strip(" -")
        hits = {ln.id for ln in tt.lines.values() if ln.id == w.lower() or ln.short.lower() == key}
        if hits:
            out |= hits
        else:
            unknown.append(w)
    if unknown and not out:
        shown = ", ".join(ln.short for ln in sorted(tt.lines.values(), key=line_order)[:40])
        raise ToolError(f"no bus line {', '.join(unknown)} in Nicosia's timetables; lines include {shown}")
    return out


def _near(ctx: ToolContext, a: dict, default_radius: float) -> tuple[float, float, str, float] | None:
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if pt is None:
        return None
    return pt[0], pt[1], pt[2], float(a.get("radius_m") or default_radius)


# -- buses -------------------------------------------------------------------------------------


@tool("get_buses",
      "The buses on Nicosia's roads right now, live from the national GTFS-RT feed joined to "
      "the bus timetables (NPT city buses, Intercity and Pame Express park and ride): each "
      "bus's line, where it is heading, its next stops and how late it runs. Call it for "
      "'where are the buses', 'is the 26 running', 'how late are the buses', 'which lines are "
      "busiest now', or anything about buses moving now. Filter by lines or near a place. "
      "The layer_ref draws the buses live on the map, gliding along their routes, coloured by "
      "line and pointing where they head (show it with style 'points'); the data_ref has one "
      "row per line (line, name, operator, buses, late = 3 min or more, mean_delay_min) for a "
      "sorted bar chart of buses per line. A bus's id (bus-5006) opens it in the inspector.",
      obj({"lines": LINES, **NEAR}))
def h_get_buses(ctx: ToolContext, a: dict) -> dict:
    m = mobility_of(ctx)
    tt = m.require_timetable()
    wanted = lines_matching(tt, a.get("lines"))
    snap, buses = m.buses(wanted)
    if snap.status == "unavailable":
        raise ToolError(f"the live bus feed isn't answering right now ({snap.error}). Say so "
                        "plainly; get_departures and plan_transit still work from the timetable")
    near = _near(ctx, a, 800)
    if near:
        lat, lon, _, radius = near
        buses = [b for b in buses if haversine_m(lat, lon, b["lat"], b["lon"]) <= radius]
    elif not wanted:  # the city's buses, not every intercity bus out on the motorways
        w, s, e, n = CITY
        buses = [b for b in buses if w <= b["lon"] <= e and s <= b["lat"] <= n]
    running = [b for b in buses if not b.get("finished")]
    rows = m.by_line(running)
    feats = []
    for b in running:
        feats.append(point(b["lon"], b["lat"], {
            "id": b["id"], "label": b["line"], "state": f"line {b['line']}", "color": b["color"],
            "text_color": b["text_color"], "heading": b.get("heading"), "line_id": b.get("line_id"),
            "entity_kind": "bus", "delay_min": round(b["delay_s"] / 60) if b.get("delay_s") is not None else None,
            "popup": f"Bus {b['line']} to {b.get('headsign') or '?'}\n" + (delay_text(b.get("delay_s")) or "on the road")}))
    legend = {f"Line {r['line']}": r["color"] for r in rows[:12]}
    where = f" near {near[2]}" if near else "" if wanted else " in Greater Nicosia"
    title = (f"Buses on line {', '.join(sorted({b['line'] for b in running}))}" if wanted and running
             else f"Buses running now{where}")
    ref = ctx.store.add_layer("buses_live", feats, geometry="point", title=title, legend=legend,
                              value_fields=["delay_min"])
    ctx.store.layers[ref]["meta"]["live"] = {"kind": "buses", "lines": sorted(wanted) if wanted else None,
                                             "near": {"lat": near[0], "lon": near[1], "radius_m": near[3]} if near else None}
    data = ctx.store.add_dataset("buses_by_line", rows, title=f"Buses running per line{where}")
    as_of = datetime.fromtimestamp(snap.fetched_at, tz=TZ).strftime("%H:%M:%S")
    sample = sorted(running, key=lambda b: -(b.get("delay_s") or 0))[:12]
    res = {
        "running": len(running), "lines": len(rows), "as_of": as_of, "feed": snap.status,
        "late_3min_or_more": sum(1 for b in running if (b.get("delay_s") or 0) >= 180),
        "by_line": [{k: r[k] for k in ("line", "name", "operator", "buses", "late", "mean_delay_min")} for r in rows[:15]],
        "buses": [{"id": b["id"], "line": b["line"], "to": b.get("headsign"),
                   "delay": delay_text(b.get("delay_s")), "delay_from": b.get("delay_source"),
                   "speed_kmh": b.get("speed_kmh"),
                   "next_stop": (b.get("next_stops") or [{}])[0].get("name"),
                   **({"waiting_until": b["waiting"]} if b.get("waiting") else {})} for b in sample],
        "layer_ref": ref, "data_ref": data,
        "notes": "delays are the operator's live figure where believable, else estimated from the "
                 "bus's position against the timetable (delay_from 'timetable')",
    }
    if snap.status == "stale":
        res["feed_note"] = f"the feed failed just now ({snap.error}); these positions are up to 5 minutes old"
    res["_summary"] = f"{len(running)} buses on {len(rows)} lines"
    return res


# -- departures --------------------------------------------------------------------------------


@tool("get_departures",
      "The next buses from a stop, from the timetable with the live estimate wherever that bus "
      "is on the road. Call it for 'when is the next bus from X', 'which buses stop at X', "
      "'departures from Solomos Square', or when the user has a bus stop open (its id, "
      "stop-1297). Give stop (an id or a stop name) or a place (near_place, stops within "
      "radius_m, default 300). Returns each stop with its departures (line, headsign, "
      "scheduled, expected when live, in_min) and a layer_ref of the stops; show it with "
      "'pulse' and answer as a short departure board.",
      obj({"stop": {"type": "string", "description": "A stop id (stop-1297) or name ('Solomos Square')."},
           **NEAR,
           "minutes": {"type": "integer", "minimum": 5, "maximum": 240,
                       "description": "How far ahead to look (default 60)."},
           "lines": LINES,
           "limit": {"type": "integer", "minimum": 1, "maximum": 30,
                     "description": "Departures per stop (default 8)."}}))
def h_get_departures(ctx: ToolContext, a: dict) -> dict:
    m = mobility_of(ctx)
    tt = m.require_timetable()
    stops = []
    if a.get("stop"):
        text = a["stop"].strip()
        if text.lower().startswith("stop-"):
            s = tt.stop(text.split("-", 1)[1])
            stops = [s] if s else []
        else:
            stops = planner.stops_named(tt, text, limit=6)
        if not stops:
            raise ToolError(f"no bus stop is called {text!r}; try near_place with a place nearby")
    else:
        near = _near(ctx, a, 300)
        if near is None:
            if not ctx.user_location:
                raise LocationNotShared("give a stop or a place (near_place): the user hasn't "
                                        "shared a location")
            loc = my_location(ctx)
            near = (loc["lat"], loc["lon"], loc["name"], float(a.get("radius_m") or 300))
        stops = [s for _, s in tt.near(near[0], near[1], near[3], limit=6)]
        if not stops:
            raise ToolError(f"no bus stop within {round(near[3])} m of {near[2]}; try a larger radius_m")
    wanted = lines_matching(tt, a.get("lines"))
    now = local_now(ctx)
    try:
        snap = m.snapshot()
    except Exception as e:  # noqa: BLE001 - the timetable alone still answers
        log.info("bus feed for departures failed: %s", e)
        snap = None
    minutes = a.get("minutes") or 60
    limit = a.get("limit") or 8
    out_stops = []
    feats = []
    rows = []
    for s in stops:
        deps = planner.public(planner.departures(tt, [s], now, minutes, limit, wanted, snap))
        lines = tt.lines_at(s)
        out_stops.append({"id": f"stop-{s.id}", "name": s.name, "code": s.code,
                          "lines": [ln.short for ln in lines],
                          "departures": [{k: d[k] for k in ("line", "headsign", "scheduled", "expected", "in_min", "bus")
                                          if d.get(k) is not None} for d in deps]})
        nxt = deps[0] if deps else None
        feats.append(point(s.lon, s.lat, {
            "id": f"stop-{s.id}", "label": s.name, "state": "bus stop", "color": STOP_COLOR,
            "entity_kind": "bus_stop",
            "popup": f"{s.name}\n" + (f"next: {nxt['line']} at {nxt['expected'] or nxt['scheduled']}" if nxt else
                                      f"no bus in the next {minutes} min")}))
        rows += [{"id": f"stop-{s.id}", "stop": s.name, **{k: d[k] for k in ("line", "headsign", "scheduled", "expected", "in_min", "realtime")}}
                 for d in deps]
    ref = ctx.store.add_layer("bus_stops", feats, geometry="point",
                              title="Bus stops" if len(stops) > 1 else stops[0].name,
                              legend={"bus stop": STOP_COLOR})
    data = ctx.store.add_dataset("departures", rows, title="Next departures", link={"key": "id"})
    res = {"now": now.strftime("%H:%M"), "stops": out_stops, "layer_ref": ref, "data_ref": data,
           "realtime": "unavailable, timetable only" if snap is None or snap.status == "unavailable"
           else "expected times come from buses on the road"}
    res["_summary"] = f"{sum(len(s['departures']) for s in out_stops)} departures from {len(stops)} stop{'s' if len(stops) > 1 else ''}"
    return res


# -- trips -------------------------------------------------------------------------------------


def _when(text: str | None, now: datetime) -> datetime:
    if not text:
        return now
    t = text.strip()
    if len(t) <= 5 and ":" in t:
        try:
            h, mnt = map(int, t.split(":"))
        except ValueError:
            raise ToolError("depart_at must be HH:MM or a local ISO time") from None
        at = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        return at if at >= now - timedelta(minutes=5) else at + timedelta(days=1)
    try:
        at = datetime.fromisoformat(t)
    except ValueError:
        raise ToolError("depart_at must be HH:MM or a local ISO time") from None
    return at if at.tzinfo else at.replace(tzinfo=TZ)


def _walk(ctx: ToolContext, a: tuple[float, float], b: tuple[float, float]) -> tuple[list[list[float]], float]:
    """The walking line and minutes between two (lat, lon), from Valhalla; a straight line
    when it doesn't answer."""
    if haversine_m(a[0], a[1], b[0], b[1]) < 25:
        return [[a[1], a[0]], [b[1], b[0]]], 0.0
    try:
        r = ctx.router.route([a, b], "walk", [])
        return r["coordinates"], r["duration_min"]
    except WebError as e:
        log.info("walking leg from Valhalla failed: %s", e)
        m = haversine_m(a[0], a[1], b[0], b[1])
        return [[a[1], a[0]], [b[1], b[0]]], planner.walk_s(m) / 60


@tool("plan_transit",
      "A bus trip from one place to another over the timetables: walk to a stop, ride "
      "directly or with one change, walk to the end, leaving now or at depart_at. Call it for "
      "'can I take the bus to X', 'how do I get to X by bus', 'which bus goes to the "
      "hospital', 'bus from A to B at 18:00'. from defaults to the user's shared location. "
      "Returns up to 3 options (leave, arrive, minutes, changes, walk_m, and legs: walks, "
      "rides with line, headsign, stops, departs and arrives, the live delay where the bus is "
      "on the road) and a layer_ref of the best option: the walks from Valhalla in grey, the "
      "ride along the bus's own route in the line's colour, the stops numbered (show it with "
      "style 'route'). When the places are close it says walking is quicker; then use "
      "plan_route.",
      obj({"from_place": {"type": "string", "description": "Where from: a place name or 'my location' (default)."},
           "from_lat": {"type": "number"}, "from_lon": {"type": "number"},
           "to_place": {"type": "string", "description": "Where to: a place name."},
           "to_lat": {"type": "number"}, "to_lon": {"type": "number"},
           "depart_at": {"type": "string", "description": "Local time to leave, HH:MM or ISO (default now)."},
           "max_walk_m": {"type": "integer", "minimum": 200, "maximum": 1500,
                          "description": "Longest walk to or from a stop (default 800)."}}))
def h_plan_transit(ctx: ToolContext, a: dict) -> dict:
    m = mobility_of(ctx)
    tt = m.require_timetable()
    if a.get("from_place") or a.get("from_lat") is not None:
        origin = resolve_point(ctx, a.get("from_place"), a.get("from_lat"), a.get("from_lon"))
    elif ctx.user_location:
        loc = my_location(ctx)
        origin = (loc["lat"], loc["lon"], loc["name"])
    else:
        raise LocationNotShared("give from_place: the user hasn't shared a location")
    dest = resolve_point(ctx, a.get("to_place"), a.get("to_lat"), a.get("to_lon"))
    if origin is None or dest is None:
        raise ToolError("give both ends: from_place and to_place (or coordinates)")
    now = local_now(ctx)
    start = _when(a.get("depart_at"), now)
    try:
        snap = m.snapshot() if start - now < timedelta(hours=2) else None
    except Exception:  # noqa: BLE001
        snap = None
    result = planner.plan(tt, (origin[0], origin[1]), (dest[0], dest[1]), start, snap,
                          max_walk_m=a.get("max_walk_m") or planner.MAX_WALK_M)
    res: dict = {"from": origin[2], "to": dest[2], "leaving_after": start.strftime("%a %H:%M"),
                 "straight_line_m": result["straight_m"]}
    if result.get("walk_min") is not None:
        res["walk_all_the_way_min"] = result["walk_min"]
    if result.get("walking_is_quicker"):
        res["walking_is_quicker"] = result["walking_is_quicker"]
    if not result["options"]:
        res["options"] = []
        res["why_none"] = result["why_none"]
        res["_summary"] = "no bus trip"
        return res
    best = result["options"][0]
    feats, legend = trip_features(ctx, tt, best, origin, dest)
    ref = ctx.store.add_layer("transit_trip", feats, geometry="route",
                              title=f"By bus: {origin[2]} to {dest[2]}", legend=legend)
    res["options"] = [{k: v for k, v in o.items() if not k.startswith("_")} for o in result["options"]]
    for o in res["options"]:
        o["legs"] = [{k: v for k, v in leg.items() if not k.startswith("_")} for leg in o["legs"]]
    res["layer_ref"] = ref
    res["timetable_note"] = "times are the timetable's; expected_departure is live where that bus is on the road"
    res["_summary"] = f"{best['leave']} to {best['arrive']} by line {' then '.join(best['lines'])}"
    return res


def trip_features(ctx: ToolContext, tt, option: dict, origin, dest) -> tuple[list[dict], dict]:
    """The best option as route features: walks (grey, from Valhalla), rides along the bus's
    shape (line colour), and numbered stops. Valhalla's walking time also sets when to leave."""
    feats: list[dict] = []
    legend = {"walk": WALK_COLOR}
    # (label, lat, lon, when, stop id or None)
    pts: list[tuple[str, float, float, str, str | None]] = []
    rides = [leg for leg in option["legs"] if leg["mode"] == "bus"]
    here = (origin[0], origin[1])
    first_walk_min = None
    for leg in rides:
        trip = tt.trips[leg["_trip"]]
        board, alight = tt.stops[trip.stops[leg["_board"]]], tt.stops[trip.stops[leg["_alight"]]]
        line, minutes = _walk(ctx, here, (board.lat, board.lon))
        if first_walk_min is None:
            first_walk_min = minutes
        feats.append(_line(line, "walk", WALK_COLOR, f"walk {max(1, round(minutes))} min"))
        shape = tt.shapes.get(trip.shape_id or "")
        along = tt.along(trip)
        if shape is not None and along is not None:
            coords = slice_shape(shape, along[leg["_board"]], along[leg["_alight"]])
        else:
            coords = [[tt.stops[x].lon, tt.stops[x].lat] for x in trip.stops[leg["_board"]:leg["_alight"] + 1]]
        feats.append(_line(coords, "bus", leg["color"], f"line {leg['line']}: {leg['departs']} to {leg['arrives']}"))
        legend[f"Line {leg['line']}"] = leg["color"]
        pts.append((f"Board {leg['line']} at {board.name}", board.lat, board.lon,
                    f"departs {leg.get('expected_departure') or leg['departs']}", f"stop-{board.id}"))
        pts.append((f"Get off at {alight.name}", alight.lat, alight.lon, f"arrives {leg['arrives']}",
                    f"stop-{alight.id}"))
        here = (alight.lat, alight.lon)
    line, minutes = _walk(ctx, here, (dest[0], dest[1]))
    feats.append(_line(line, "walk", WALK_COLOR, f"walk {max(1, round(minutes))} min"))
    if first_walk_min is not None and rides:  # leave in time for Valhalla's walk, a minute spare
        leave = option["_leave"].replace(second=0, microsecond=0)
        dep = leave.replace(hour=int(rides[0]["departs"][:2]), minute=int(rides[0]["departs"][3:]))
        if dep < leave - timedelta(hours=12):  # the bus leaves after midnight
            dep += timedelta(days=1)
        leave = dep - timedelta(minutes=round(first_walk_min) + 1)
        option["leave"] = leave.strftime("%H:%M")
        option["minutes"] = round((option["_arrive"] - leave).total_seconds() / 60)
    pts.insert(0, (f"Start: {origin[2]}", origin[0], origin[1], f"leave {option['leave']}", None))
    pts.append((f"End: {dest[2]}", dest[0], dest[1], f"arrive {option['arrive']}", None))
    for n, (label, lat, lon, when, sid) in enumerate(pts, start=1):
        feats.append(point(lon, lat, {"id": sid or f"stop{n}", "label": f"{n}. {label}", "n": n,
                                      "state": "stop", "color": STOP_COLOR if sid else "#0f766e",
                                      "entity_kind": "bus_stop" if sid else None,
                                      "popup": f"{n}. {label}\n{when}"}))
    return feats, legend


def _line(coords: list[list[float]], mode: str, color: str, label: str) -> dict:
    # 'walk' lines draw thin and still under the ride (as the usual route does in plan_route).
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"id": "route", "state": "walk" if mode == "walk" else "route",
                           "mode": mode, "color": color, "label": label}}


# -- road speeds -------------------------------------------------------------------------------


@tool("get_road_speeds",
      "Speeds on Nicosia's main roads from the national Bluetooth travel-time detectors (about "
      "a hundred one-way segments, updated every 5 minutes), each against its own free-flow "
      "speed: moving freely, busy, slow or very slow. Call it for 'which roads are slow', "
      "'how is the traffic', 'is Griva Digeni congested', 'how long to drive Athalassas "
      "now', or traffic at a past moment the twin recorded (at). Filter by road name or near a "
      "place. Returns the segments slowest first (id, name, road, speed_kmh, free_flow_kmh, "
      "share_of_free_flow, state, usual_kmh for this hour where the memory has weeks of it) "
      "and a layer_ref of every segment coloured by speed (show it with style 'route'); the "
      "data_ref is linked to the map by id, for a sorted bar of each road's speed against its "
      "free flow. Segment ids (road-12) open in the inspector. status 'stalled' means the feed "
      "answers but its detectors have sent nothing new (note, last_measured_at): no speed is "
      "live then, and the layer shows each road's usual speed for this hour instead.",
      obj({"road": {"type": "string", "description": "A road or site name, e.g. 'Griva', 'Makariou', 'Athalassas'."},
           **NEAR,
           "at": {"type": "string", "description": "A past local moment, ISO (2026-11-26T08:30): "
                                                    "speeds as the twin's memory recorded them."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                     "description": "Segments to list, slowest first (default 10; the layer has all)."}}))
def h_get_road_speeds(ctx: ToolContext, a: dict) -> dict:
    m = mobility_of(ctx)
    now = ctx.city.now()
    try:
        at = parse_moment(a.get("at"), now)
    except ValueError as e:
        raise ToolError(str(e)) from None
    out = m.roads.speeds(at)
    if out["status"] == "unavailable":
        if at is not None:
            raise ToolError(f"the twin's memory has no road speeds around {at.astimezone(TZ):%a %H:%M} "
                            f"({out.get('error') or 'nothing recorded then'}); it records them while "
                            "the road speeds layer or this tool is in use")
        raise ToolError(f"the national traffic feed isn't answering ({out.get('error')}). Say so plainly")
    rows = out["segments"]
    segs = {s.id: s for s in m.roads.segments()}
    chosen = rows
    if a.get("road"):
        q = a["road"].strip().lower()
        chosen = [r for r in rows if q in r["name"].lower() or q in (r["road"] or "").lower()]
        if not chosen:
            names = sorted({r["road"] for r in rows if r["road"]})
            raise ToolError(f"no measured road matches {a['road']!r}; roads include {', '.join(names)}")
    near = _near(ctx, a, 1500)
    if near:
        lat, lon, _, radius = near
        chosen = [r for r in chosen if min(haversine_m(lat, lon, y, x) for x, y in segs[r["id"]].coords) <= radius]
        if not chosen:
            raise ToolError(f"no measured road within {round(radius)} m of {near[2]}; the detectors "
                            "cover the main avenues only")
    picked = {r["id"] for r in chosen}
    feats = [segment_feature(segs[r["id"]], r) for r in rows if r["id"] in picked]
    stalled = out["status"] == "stalled"
    when = at.astimezone(TZ).strftime("%a %H:%M") if at else now.astimezone(TZ).strftime("%H:%M")
    title = (f"Usual road speeds for {when} (the detectors have stalled)" if stalled else f"Road speeds, {when}") \
        + (f": {a['road']}" if a.get("road") else "")
    ref = ctx.store.add_layer("road_speeds", feats, geometry="route", title=title,
                              legend=STALLED_LEGEND if stalled else LEGEND,
                              value_fields=["speed_kmh", "share"])
    ctx.store.layers[ref]["meta"]["live"] = {"kind": "roads", "at": at.isoformat() if at else None}
    ranked = slowest(chosen, len(chosen))
    posted = posted_limits(ctx, segs, chosen)  # the street rules: "limit 50, traffic at 34"
    data_rows = [{"id": r["id"], "segment": r["name"], "road": r["road"], "speed_kmh": r["speed_kmh"],
                  "free_flow_kmh": r["free_flow_kmh"], "share_of_free_flow": r["share_of_free_flow"],
                  "state": r["state_label"], "usual_kmh": r["usual_kmh"],
                  "limit_kmh": posted.get(r["id"])} for r in ranked]
    data = ctx.store.add_dataset("road_speeds", data_rows, title=title, link={"key": "id"})
    how = {r["free_flow"] for r in chosen}
    limit = a.get("limit") or 10
    counts: dict[str, int] = {}
    for r in chosen:
        counts[r["state_label"]] = counts.get(r["state_label"], 0) + 1
    res = {
        "as_of": when, "source": "live" if at is None else "the twin's memory",
        "measured": sum(1 for r in chosen if r["speed_kmh"] is not None), "segments": len(chosen),
        "by_state": counts,
        "slowest": [{k: r[k] for k in ("id", "name", "road", "speed_kmh", "free_flow_kmh",
                                       "share_of_free_flow", "state_label", "usual_kmh", "travel_time_s")}
                    | {"limit_kmh": posted.get(r["id"])} for r in ranked[:limit]],
        "free_flow_from": sorted(how),
        "free_flow_note": "measured: the 85th percentile of 4 weeks of the twin's readings; estimated: "
                          "the road network's typical speed (Valhalla), until a day of readings is "
                          "stored; posted: the road's posted speed limit while that estimate loads; "
                          "assumed: 50 km/h where no limit is known either",
        "limit_note": "limit_kmh is the posted speed limit along most of the segment, from the national "
                      "sign inventory or OpenStreetMap (get_speed_limits has the detail); null where neither says",
        "layer_ref": ref, "data_ref": data,
    }
    if not any(r["usual_kmh"] for r in chosen):
        res["usual_note"] = "the twin's memory has no earlier weeks of road speeds yet, so no usual"
    res["_summary"] = f"{res['measured']} roads measured, {counts.get('Very slow', 0) + counts.get('Slow', 0)} slow"
    if stalled:
        # The feed answers, but the detectors behind it have sent nothing new: no speed is now.
        usual_rows = usually_slowest(chosen, len(chosen))
        res |= {
            "status": "stalled", "source": "stalled: nothing live", "last_measured_at": out["last_measured_at"],
            "note": out["note"] + " Road speeds have no live reading until they send again.",
            "usual_shown": len(usual_rows),
            "usually_slowest": [{"id": r["id"], "name": r["name"], "road": r["road"], "usual_kmh": r["usual_kmh"],
                                 "usual_days": r["usual_days"], "free_flow_kmh": r["free_flow_kmh"],
                                 "usual": r["usual_label"]} for r in usual_rows[:limit]],
            "map_note": ("the map draws each road at its usual speed for this hour and weekday, dotted, "
                         "never as a reading" if usual_rows else
                         "the twin's memory knows too little of this hour on this weekday to show the "
                         "usual speeds, so every road reads no reading"),
            "_summary": f"detectors stalled since {since_words(datetime.fromisoformat(out['last_measured_at']), now)}",
        }
    return res


def posted_limits(ctx: ToolContext, segs: dict, rows: list[dict]) -> dict[str, int]:
    """Each road segment's posted limit from the street rules (chatbot/streets), where known."""
    st = getattr(ctx, "streets", None)
    if st is None:
        return {}
    out = {}
    for r in rows:
        got = st.segment_limit(segs[r["id"]]) if r["id"] in segs else None
        if got:
            out[r["id"]] = got["kmh"]
    return out

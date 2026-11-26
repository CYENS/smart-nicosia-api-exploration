"""Route tools: walks, drives and tours (with safer walks around the dangerous buildings), and
the area reachable in a given time."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from collector.http import ApiError

from chatbot import context, eoa
from chatbot import sources as data_sources
from chatbot.dls import distance_to_path_m
from chatbot.geo import haversine_m
from chatbot.routing import MAX_TOUR_STOPS, MODES, PREFERENCES, best_order
from chatbot.store import STATE_COLORS, point
from chatbot.tools.base import (ToolContext, ToolError, ToolSet, local_now, obj, resolve_place,
                                resolve_point)
from chatbot.tools.buildings import STATUSES
from chatbot.tools.environment import SHADE_COLORS as ENV_SHADE_COLORS
from chatbot.web import WebError

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

PASS_BY_M = 25  # a register building this close to a route's line is "passed"
MAX_AVOID_ZONES = 40  # Valhalla caps the total perimeter of polygons to avoid


def _hhmm(text: str | None, now: datetime) -> datetime:
    if not text:
        return now
    try:
        h, m = map(int, text.strip().split(":"))
    except ValueError:
        raise ToolError("start_time must be HH:MM")
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t >= now - timedelta(minutes=5) else t + timedelta(days=1)


@tool("plan_route",
      "Walking, driving, cycling or wheelchair route through 2 to 13 stops, drawn as a "
      "layer_ref. With optimize_order the stops after the first are reordered for the "
      "shortest total travel time (exact for up to 12 stops), which is how to plan a tour of "
      "museums or sights. Preferences tune the walking route: lit_streets (after dark), "
      "quiet (footpaths and living streets), less_hills, accessible, and "
      "avoid_dangerous_buildings (keeps off the streets in front of buildings on the "
      "dangerous registers; the reply compares it with the usual route, the one picked "
      "without avoiding them: minutes added, buildings passed by each and how many it "
      "avoids, and the layer shows both routes and the buildings), and shade (walking or "
      "cycling between two places: the real alternatives are read for the shade of the DLS "
      "3D buildings at the moment of the walk, from start_time, and the shadiest within "
      "about a third more time is taken; the reply compares them in `shade`). Every "
      "route reports the register buildings it passes within 25 m. Give each stop a place "
      "name, 'my location', or coordinates, and optionally minutes_here for a visit. Driving "
      "routes use typical speeds, not live per-street traffic; the reply adds the current "
      "city-wide speed against last week.",
      obj({
          "stops": {"type": "array", "minItems": 2, "maxItems": MAX_TOUR_STOPS + 1,
                    "items": obj({
                        "place": {"type": "string"},
                        "lat": {"type": "number"},
                        "lon": {"type": "number"},
                        "minutes_here": {"type": "number", "minimum": 0, "maximum": 600},
                    })},
          "mode": {"type": "string", "enum": list(MODES)},
          "prefer": {"type": "array", "items": {"type": "string", "enum": list(PREFERENCES)}},
          "optimize_order": {"type": "boolean"},
          "return_to_start": {"type": "boolean"},
          "keep_last_stop_last": {"type": "boolean"},
          "start_time": {"type": "string", "description": "Local start time HH:MM (default now)."},
      }, ["stops", "mode"]))
def h_plan_route(ctx: ToolContext, a: dict) -> dict:
    mode = a["mode"]
    prefer = a.get("prefer") or []
    stops = []
    for i, s in enumerate(a["stops"]):
        if s.get("lat") is not None and s.get("lon") is not None:
            info = {"name": s.get("place") or f"Stop {i + 1}", "lat": s["lat"], "lon": s["lon"]}
        elif s.get("place"):
            info = resolve_place(ctx, s["place"])
        else:
            raise ToolError(f"stop {i + 1} needs a place or lat/lon")
        stops.append({"label": info.get("name") or s.get("place"), "lat": info["lat"],
                      "lon": info["lon"], "opening_hours": info.get("opening_hours"),
                      "minutes_here": s.get("minutes_here") or 0})
    coords = [(s["lat"], s["lon"]) for s in stops]
    return_to_start = bool(a.get("return_to_start"))
    order = list(range(len(stops)))
    seconds = None
    if a.get("optimize_order") and len(stops) >= 3:
        seconds = ctx.router.matrix(coords, mode, prefer)
        order = best_order(seconds, return_to_start, bool(a.get("keep_last_stop_last")))
    path = [coords[i] for i in order] + ([coords[0]] if return_to_start else [])
    # In the shade: the real alternatives between two places, each read for its shade below.
    options = None
    if ("shade" in prefer and len(path) == 2 and mode != "drive"
            and "avoid_dangerous_buildings" not in prefer):
        options = ctx.router.alternatives(path, mode, prefer)
        route = options[0]
    else:
        route = ctx.router.route(path, mode, prefer)
    records = ctx.registers.records() if ctx.registers is not None and ctx.registers.ready else []
    # `usual` is the route picked without avoiding them (not always the shortest: other
    # preferences, like lit streets, weigh in too).
    usual, zones = None, []
    if "avoid_dangerous_buildings" in prefer and records:
        safer, zones = route_avoiding(ctx, path, mode, prefer, route, records)
        if safer is not route:
            usual, route = route, safer
    start = _hhmm(a.get("start_time"), local_now(ctx))
    shade = None
    if "shade" in prefer:
        route, shade = shaded_choice(ctx, options or [route], start, len(path), mode)
    passed = passed_by(route["coordinates"], records)
    usual_passed = passed_by(usual["coordinates"], records) if usual else []

    t = start
    plan = []
    for pos, i in enumerate(order):
        if pos:
            t += timedelta(minutes=route["legs"][pos - 1]["duration_min"])
        arrive = t
        t += timedelta(minutes=stops[i]["minutes_here"] if pos else 0)
        plan.append({"n": pos + 1, "label": stops[i]["label"], "arrive": arrive.strftime("%a %H:%M"),
                     "leave": t.strftime("%H:%M"), "lat": stops[i]["lat"], "lon": stops[i]["lon"],
                     **({"opening_hours": stops[i]["opening_hours"]}
                        if stops[i]["opening_hours"] else {})})
    if return_to_start:
        t += timedelta(minutes=route["legs"][-1]["duration_min"])

    feats = []
    if usual:  # drawn first, so the route to take sits on top of it
        feats.append({"type": "Feature", "geometry": {"type": "LineString",
                                                      "coordinates": usual["coordinates"]},
                      "properties": {"id": "route_usual", "state": "usual route",
                                     "color": STATE_COLORS["usual route"],
                                     "label": f"usual: {usual['distance_km']} km · "
                                              f"{usual['duration_min']:.0f} min"}})
    if shade and shade.get("stretches"):
        # The other ways first, thin and grey; then the walk, in shade and in sun.
        for k, other in enumerate(shade["others"]):
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": other["coordinates"]},
                          "properties": {"id": f"route_other{k + 1}", "state": "usual route",
                                         "color": STATE_COLORS["usual route"],
                                         "label": f"other way: {other['shade_pct']:.0f}% in shade · "
                                                  f"{other['min']:.0f} min"}})
        for k, (in_shade, line) in enumerate(shade["stretches"]):
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": line},
                          "properties": {"id": f"route_{k + 1}", "state": "in shade" if in_shade else "in sun",
                                         "color": SHADE_COLORS["in shade" if in_shade else "in sun"],
                                         "label": f"{route['distance_km']} km · {route['duration_min']:.0f} min · "
                                                  f"{shade['chosen']['shade_pct']:.0f}% in shade"}})
    else:
        feats.append({"type": "Feature", "geometry": {"type": "LineString",
                                                      "coordinates": route["coordinates"]},
                      "properties": {"id": "route", "state": "route", "color": STATE_COLORS["route"],
                                     "label": f"{route['distance_km']} km · {route['duration_min']:.0f} min"}})
    feats += [point(p["lon"], p["lat"], {"id": f"stop{p['n']}", "label": f"{p['n']}. {p['label']}",
                                         "n": p["n"], "state": "stop", "color": STATE_COLORS["stop"],
                                         "popup": f"{p['n']}. {p['label']}\narrive {p['arrive']}"})
              for p in plan]
    # register buildings along either route, as red "!" markers
    flagged = {r["id"]: r for r, _ in passed + usual_passed}
    for r in flagged.values():
        spot = eoa.anchor(r)
        feats.append(point(spot[0], spot[1], {
            "id": r["id"], "label": "", "n": "!", "state": r["status"],
            "color": STATE_COLORS[r["status"]], "popup": eoa.popup(r)}))
    legend = {}
    if flagged or usual:
        legend = {"route": STATE_COLORS["route"],
                  **({"usual route": STATE_COLORS["usual route"]} if usual else {}),
                  **{s: STATE_COLORS[s] for s in STATUSES
                     if any(r["status"] == s for r in flagged.values())}}
    if shade and shade.get("stretches"):
        legend = {**SHADE_COLORS, **({"other ways": STATE_COLORS["usual route"]} if shade["others"] else {}),
                  **{k: v for k, v in legend.items() if k not in ("route", "usual route")}}
    # A walk read for its shade draws on the DLS buildings too.
    layer = ctx.store.add_layer(f"route_{mode}", feats, geometry="route",
                                title=f"{mode.capitalize()} route", legend=legend,
                                sources=data_sources.current() + (["dls"] if shade and shade.get("stretches") else []))
    visit_min = sum(s["minutes_here"] for s in stops[1:])
    res = {
        "mode": mode, "prefer": prefer, "optimized": bool(seconds),
        "travel_km": route["distance_km"], "travel_min": route["duration_min"],
        "visits_min": visit_min, "finish": t.strftime("%a %H:%M"),
        "stops_in_order": plan,
        "legs": [{"from": plan[k]["label"],
                  "to": plan[k + 1]["label"] if k + 1 < len(plan) else plan[0]["label"],
                  **leg} for k, leg in enumerate(route["legs"])],
        "layer_ref": layer,
    }
    if records:
        res["dangerous_passed"] = len(passed)
        res["dangerous_along_route"] = [
            {"id": r["id"], "status": r["status"], "address": r.get("address") or eoa.place_of(r),
             "height_m": r.get("height_m"), "metres_from_route": round(d)}
            for r, d in sorted(passed, key=lambda t: t[1])][:12]
    if "avoid_dangerous_buildings" in prefer:
        if usual:
            ids = {r["id"] for r, _ in passed}
            res["avoidance"] = {
                "usual_route": {"km": usual["distance_km"], "min": usual["duration_min"],
                                "dangerous_passed": len(usual_passed)},
                "this_route": {"km": route["distance_km"], "min": route["duration_min"],
                               "dangerous_passed": len(passed)},
                "avoided": len({r["id"] for r, _ in usual_passed} - ids),
                "minutes_added": round(route["duration_min"] - usual["duration_min"], 1),
                "km_added": round(route["distance_km"] - usual["distance_km"], 2),
                "what_usual_means": "the route picked without avoiding them, with the same "
                                    "other preferences; not always the shortest",
                "still_passed_note": "buildings within 40 m of a stop can't be avoided"
                if passed else None}
        elif not records:
            res["avoidance"] = {"note": "the dangerous-building registers are still loading"}
        elif not zones:
            res["avoidance"] = {"note": "the usual route already passes no register buildings "
                                        "(or only ones within 40 m of a stop)"}
        else:
            res["avoidance"] = {"note": "no way round them passes fewer register buildings, so "
                                        "this is the usual route"}
    if shade:
        res["shade"] = {k: v for k, v in shade.items() if k not in ("stretches", "others")}
    if mode == "drive":
        try:
            sp = context.speed(ctx.city, 2)["rows"]
            if sp:
                res["city_traffic_now"] = sp[-1]
        except (ApiError, WebError) as e:
            log.info("speed lookup failed: %s", e)
    res["_summary"] = f"{route['distance_km']} km, {route['duration_min']:.0f} min {mode}"
    return res


@tool("reachable_area",
      "The area reachable within N minutes from a place, on foot, by bike or by car "
      "(an isochrone), as a layer_ref of nested polygons.",
      obj({
          "place": {"type": "string"},
          "lat": {"type": "number"},
          "lon": {"type": "number"},
          "minutes": {"type": "array", "minItems": 1, "maxItems": 4,
                      "items": {"type": "integer", "minimum": 1, "maximum": 60}},
          "mode": {"type": "string", "enum": ["walk", "bike", "drive"]},
      }, ["minutes", "mode"]))
def h_reachable_area(ctx: ToolContext, a: dict) -> dict:
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    if not center:
        raise ToolError("give a place or lat/lon")
    data = ctx.router.isochrone((center[0], center[1]), a["minutes"], a["mode"])
    palette = ["#16a34a", "#eab308", "#f97316", "#dc2626"]
    contours = sorted(data.get("features", []), key=lambda f: f["properties"].get("contour", 0))
    for k, f in enumerate(contours):
        m = f["properties"].get("contour")
        m = int(m) if m is not None and m == int(m) else m
        f["properties"] = {"id": f"iso{m}", "label": f"{m} min", "minutes": m,
                           "state": f"{m} min", "color": palette[min(k, 3)]}
    feats = contours[::-1]  # largest first, so the smaller areas draw on top
    feats.append(point(center[1], center[0], {"id": "origin", "label": center[2], "state": "place",
                                              "color": STATE_COLORS["place"], "popup": center[2]}))
    layer = ctx.store.add_layer("reachable", feats, geometry="area",
                                title=f"Reachable by {a['mode']} from {center[2]}",
                                legend={f["properties"]["label"]: f["properties"]["color"]
                                        for f in feats if "minutes" in f["properties"]})
    return {"origin": center[2], "minutes": a["minutes"], "mode": a["mode"], "layer_ref": layer,
            "_summary": f"{', '.join(map(str, a['minutes']))} min by {a['mode']}"}


SHADE_COLORS = ENV_SHADE_COLORS  # one blue and one amber for shade and sun, as on the shade map
SHADE_MAX_EXTRA = 1.35  # a shadier way may take this much longer than the quickest (plus a minute)
SHADE_METHOD = ("the shadows of the DLS 3D buildings at the moment the walker passes each point "
                "(every 8 m, at the route's own pace); trees, awnings and arcades are not in it")


def shaded_choice(ctx: ToolContext, options: list[dict], start: datetime, stops: int,
                  mode: str) -> tuple[dict, dict]:
    """The route with the most shade at the time of the walk, among the real alternatives
    (`options`, best first), and what each one offers. With the sun down every way is in shade,
    so the first one stays."""
    env = ctx.environment
    first = options[0]
    if env is None:
        return first, {"note": "shade isn't available on this server, so this is the usual route"}
    scored = []
    for r in options:
        pace = r["distance_km"] * 1000 / max(60.0, r["duration_min"] * 60)
        try:
            s = env.shade.along(r["coordinates"], start, pace)
        except (ValueError, WebError, ApiError) as e:
            log.info("shade along a route failed: %s", e)
            s = {"shade_pct": None, "sun_up": True, "samples": []}
        scored.append((r, s))
    quickest = min(r["duration_min"] for r in options)
    sun_up = any(s["sun_up"] for _, s in scored)
    fair = [(r, s) for r, s in scored
            if s["shade_pct"] is not None and r["duration_min"] <= quickest * SHADE_MAX_EXTRA + 1]
    best = (max(fair, key=lambda t: (t[1]["shade_pct"], -t[0]["duration_min"]))
            if fair and sun_up else scored[0])
    route, chosen = best
    info: dict = {
        "walk_starts": start.strftime("%a %H:%M"),
        "sun_up": sun_up,
        "chosen": {"km": route["distance_km"], "min": route["duration_min"],
                   "shade_pct": chosen["shade_pct"]},
        "options": [{"option": k + 1, "km": r["distance_km"], "min": r["duration_min"],
                     "shade_pct": s["shade_pct"], "chosen": r is route}
                    for k, (r, s) in enumerate(scored)],
        "minutes_added": round(route["duration_min"] - quickest, 1),
        "method": SHADE_METHOD,
    }
    if not sun_up:
        info["note"] = ("the sun is down for the whole walk: every way is in shade, so this is "
                        "the usual route")
    elif len(options) == 1:
        info["note"] = ("shade is read for walking and cycling, not driving" if mode == "drive" else
                        "one route only: alternatives come between two places, with no stops on "
                        "the way" if stops > 2 else
                        "with the dangerous buildings avoided too, this route is read for its "
                        "shade but not swapped")
    if any(s.get("tiles_missing") for _, s in scored):
        info["buildings_missing"] = ("some 3D buildings along the way couldn't be read, so shade "
                                     "may be missing there")
    info["stretches"] = _stretches(chosen["samples"]) if chosen["samples"] else []
    info["others"] = [{"coordinates": r["coordinates"], "min": r["duration_min"],
                       "shade_pct": s["shade_pct"] or 0.0}
                      for r, s in scored if r is not route]
    return route, info


def _stretches(samples: list[dict]) -> list[tuple[bool, list[list[float]]]]:
    """A walk's sample points as runs of shade and of sun, each a line touching the next."""
    out: list[tuple[bool, list[list[float]]]] = []
    for p in samples:
        xy = [round(p["lon"], 6), round(p["lat"], 6)]
        if out and out[-1][0] == p["shade"]:
            out[-1][1].append(xy)
        else:
            if out:
                out[-1][1].append(xy)  # meet the next run, so the line has no gaps
            out.append((p["shade"], [xy]))
    return [(sh, line) for sh, line in out if len(line) >= 2]


def passed_by(path: list[list[float]], records: list[dict],
              within: float = PASS_BY_M) -> list[tuple[dict, float]]:
    """Register buildings whose plot is within `within` m of a route, with the distance."""
    if not path:
        return []
    pad = 0.0005
    w, e = min(p[0] for p in path) - pad, max(p[0] for p in path) + pad
    s, n = min(p[1] for p in path) - pad, max(p[1] for p in path) + pad
    out = []
    for r in records:
        spot = eoa.anchor(r)
        if spot and w <= spot[0] <= e and s <= spot[1] <= n:
            d = distance_to_path_m(path, *spot)
            if d <= within:
                out.append((r, d))
    return out


def route_avoiding(ctx: ToolContext, path: list[tuple[float, float]], mode: str,
                   prefer: list[str], first: dict, records: list[dict]):
    """Re-route around the streets in front of the register buildings a route passes: exclude
    them, re-route, and repeat for any the new route passes (3 rounds at most). Buildings
    within 40 m of a stop can't be avoided and stay. Returns (route, avoided ids): the route
    passing the fewest of them, which stays `first` when no detour passes fewer (in a dense old
    town a detour can run past more than it dodges)."""
    stops = [(lon, lat) for lat, lon in path]
    zones: dict[str, list] = {}
    current = best = first
    fewest = len(passed_by(first["coordinates"], records))
    for _ in range(3):
        new = []
        for r, _d in passed_by(current["coordinates"], records):
            spot = eoa.anchor(r)
            if r["id"] in zones or any(haversine_m(spot[1], spot[0], y, x) < 40 for x, y in stops):
                continue
            new.append(r)
        if not new or len(zones) >= MAX_AVOID_ZONES:
            break
        for r in new[:MAX_AVOID_ZONES - len(zones)]:
            zone = eoa.avoid_zone(r)
            if zone and zone not in zones.values():
                zones[r["id"]] = zone
        try:
            current = ctx.router.route(path, mode, prefer, avoid=list(zones.values()))
        except WebError as e:  # the zones cut off every way through
            log.info("no way round the dangerous buildings: %s", e)
            break
        if (n := len(passed_by(current["coordinates"], records))) < fewest:
            best, fewest = current, n
    return best, list(zones)

"""Street rules tools: speed limits, 20 and 30 km/h zones, crossings and signals (and how
schools and bus stops are served by them), the cycle network and where it breaks, bus lanes
and enforcement cameras. They read chatbot/streets, which keeps one copy of each national
inventory for every page and answer. Every layer is drawn by the page's streets renderer
(meta.streets), in the same styles as the Layers sheet's Streets rows."""

from __future__ import annotations

from collections import defaultdict

from chatbot.geo import haversine_m, loose
from chatbot.streets import analysis as A
from chatbot.streets import layers as Ly
from chatbot.streets.entities import day
from chatbot.streets.network import bearing, cumulative, point_at
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool

SURE_NOTE = ("the national inventory gives each speed limit as a sign at a point; the twin carries it "
             "along its street (through junctions while the street keeps its name) up to 800 m or the "
             "next sign. tier signed: near a sign or between two that agree; likely: further along "
             "from one sign; uncertain: between signs that disagree, where it changes somewhere; "
             "mapped: no national sign reaches it, OpenStreetMap's own tag")
STRAIGHT = "distances are straight lines, not walking routes"


def streets_of(ctx: ToolContext):
    s = getattr(ctx, "streets", None)
    if s is None:
        raise ToolError("the street rules aren't available in this deployment")
    return s


def _near(ctx: ToolContext, a: dict, default: float):
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if pt is None:
        return None
    return pt[0], pt[1], pt[2], float(a.get("radius_m") or default)


def _layer(ctx, hint, feats, *, geometry, title, legend, kind, sources=None) -> str:
    ref = ctx.store.add_layer(hint, feats, geometry=geometry, title=title, legend=legend, sources=sources)
    ctx.store.layers[ref]["meta"]["streets"] = {"kind": kind}
    return ref


def _age(st, *keys) -> str:
    """'inventories updated 10 Mar 2025' from the catalogue's dates."""
    days = sorted({d for k in keys if (d := st.age(k)["updated"])})
    if not days:
        return "inventory date unknown"
    return f"inventory updated {day(days[-1])}" if len(days) == 1 else \
        f"inventories updated {day(days[0])} to {day(days[-1])}"


def _limits(st):
    lim = st.limits(wait=True)
    if lim is None:
        raise ToolError("the street network is still loading from OpenStreetMap (a minute after the "
                        f"first start{': ' + st.network.error if st.network.error else ''}); the "
                        "speed limit signs alone are on the map's Streets layers meanwhile")
    return lim


def _near_line(coords, lat, lon, radius) -> bool:
    return any(haversine_m(lat, lon, y, x) <= radius for x, y in coords[:: max(1, len(coords) // 8)] + [coords[-1]])


# -- speed limits ----------------------------------------------------------------------------------


@tool("get_speed_limits",
      "Posted speed limits on Nicosia's streets, from the national speed limit sign inventory "
      "(about 330 signs in Greater Nicosia, Public Works Department), each carried along its "
      "street with how sure it is (tier signed, likely, uncertain or mapped from OpenStreetMap "
      "where no sign reaches). Call it for 'what is the speed limit on Makariou Avenue', 'how "
      "fast can I drive here', 'which streets are 30', or a road segment's posted limit (road, "
      "e.g. 'Griva Digeni', or segment 'road-12'). Give road, a place (near_place, radius_m "
      "default 400) or neither for the whole city. Returns per street the limit by length and "
      "tier, the signs, and a layer_ref of the stretches as coloured street casings with the "
      "number at street zoom (show it with style 'route'); the data_ref has one row per street "
      "and limit (street, kmh, length_m, tier) linked to the map by id. Sign ids (limit-295) "
      "open in the inspector.",
      obj({"road": {"type": "string", "description": "A street's name in any script: 'Makariou', 'Λεωφόρος Λεμεσού'."},
           "segment": {"type": "string", "description": "A road speeds segment id, e.g. 'road-12'."},
           **NEAR,
           "include_mapped": {"type": "boolean", "description": "Also streets only OpenStreetMap gives a limit for (default true with a road or place, false city-wide)."}}))
def h_get_speed_limits(ctx: ToolContext, a: dict) -> dict:
    st = streets_of(ctx)
    lim = _limits(st)
    net = st.network.current()
    pieces = lim.pieces
    title = "Speed limits"
    seg_info = None
    if a.get("segment"):
        if ctx.mobility is None:
            raise ToolError("road segments aren't available in this deployment")
        seg = ctx.mobility.roads.segment(a["segment"])
        if seg is None:
            raise ToolError(f"no road segment {a['segment']!r}; ids look like road-12 (get_road_speeds lists them)")
        seg_info = st.segment_limit(seg)
        coords = [list(c) for c in seg.coords]
        cum = cumulative(coords)
        found: dict[str, dict] = {}
        for k in range(max(2, int(cum[-1] // 20))):
            at = cum[-1] * (k + 0.5) / max(2, int(cum[-1] // 20))
            p, q = point_at(coords, cum, max(0.0, at - 5)), point_at(coords, cum, min(cum[-1], at + 5))
            mid = point_at(coords, cum, at)
            hit = lim.index.at(mid[0], mid[1], 25, bearing(p, q))
            if hit is not None:
                found[hit["id"]] = hit
        pieces = list(found.values())
        title = f"Speed limits along {seg.name}"
    if a.get("road"):
        links = set(net.named(a["road"]))
        if not links:
            raise ToolError(f"no street called {a['road']!r} in Greater Nicosia; try a shorter part of its name")
        ways = {net.links[i].way for i in links}
        pieces = [p for p in pieces if p["way"] in ways]
        title = f"Speed limits on {a['road']}"
    near = _near(ctx, a, 400)
    if near:
        lat, lon, label, radius = near
        pieces = [p for p in pieces if _near_line(p["coords"], lat, lon, radius)]
        title = f"Speed limits near {label}"
    mapped = a.get("include_mapped")
    if mapped is None:
        mapped = bool(a.get("road") or near or a.get("segment"))
    if not mapped:
        pieces = [p for p in pieces if p["tier"] != "mapped"]
    if not pieces:
        raise ToolError("no posted limit there: no national sign reaches it and OpenStreetMap gives none. "
                        "In towns the default is 50 km/h unless signs say otherwise; say that it is the "
                        "default, not a posted limit")
    rows: dict[tuple, dict] = {}
    for p in pieces:
        k = (p["street"] or "unnamed street", p["kmh"], p["tier"])
        r = rows.setdefault(k, {"id": p["sign"] or f"limitway-{p['way']}", "street": k[0], "kmh": p["kmh"],
                                "tier": p["tier"], "length_m": 0, "signs": set(), "alt_kmh": set()})
        r["length_m"] += p["length_m"]
        if p["sign"]:
            r["signs"].add(p["sign"])
        if p.get("alt_kmh"):
            r["alt_kmh"].add(p["alt_kmh"])
    data_rows = sorted(({**r, "signs": sorted(r["signs"])[:6], "alt_kmh": sorted(r["alt_kmh"]) or None}
                        for r in rows.values()), key=lambda r: (-r["length_m"]))
    by_kmh: dict[int, int] = defaultdict(int)
    for p in pieces:
        if p["tier"] != "uncertain":
            by_kmh[p["kmh"]] += p["length_m"]
    signs = sorted({p["sign"] for p in pieces if p["sign"]})
    sign_list = [s for s in st.records("speed_limits") if s["id"] in set(signs)]
    feats = Ly.limit_features(pieces) + Ly.sign_features(sign_list, [st.sign_snap(s["id"]) for s in sign_list])
    ref = _layer(ctx, "speed_limits", feats, geometry="route", title=title, legend=Ly.limit_legend(), kind="limits",
                 sources=["speed_limits"] + (["openstreetmap"] if mapped else []))
    data = ctx.store.add_dataset("speed_limits", [{k: v for k, v in r.items() if k != "signs"} | {"signs": ", ".join(r["signs"])}
                                                  for r in data_rows], title=title, link={"key": "id"})
    total = sum(by_kmh.values()) or 1
    res = {
        "streets": [{k: r[k] for k in ("street", "kmh", "tier", "length_m", "alt_kmh", "signs")} for r in data_rows[:12]],
        "by_kmh_share": {f"{k} km/h": round(v / total, 2) for k, v in sorted(by_kmh.items())},
        "signs": len(signs), "stretches": len(pieces),
        "how_sure": SURE_NOTE, "inventory": _age(st, "speed_limits"),
        "layer_ref": ref, "data_ref": data,
    }
    if a.get("road") and not near:
        # A name several streets share (Makariou in Nicosia, Lakatamia and Latsia): say which, so
        # the answer can pick one or ask, and a place narrows it.
        where: dict[str, list] = defaultdict(list)
        for p in pieces:
            mid = p["coords"][len(p["coords"]) // 2]
            where[p["street"] or "unnamed street"].append((p["length_m"], mid))
        matched = []
        for name, parts in where.items():
            total_m = sum(m for m, _ in parts)
            lon = sum(c[0] * m for m, c in parts) / total_m
            lat = sum(c[1] * m for m, c in parts) / total_m
            matched.append({"street": name, "km": round(total_m / 1000, 1), "lat": round(lat, 4), "lon": round(lon, 4)})
        matched.sort(key=lambda r: -r["km"])
        if len(matched) > 1:
            far = max(haversine_m(x["lat"], x["lon"], y["lat"], y["lon"]) for x in matched for y in matched)
            res["matched_streets"] = matched[:8]
            if far > 3000:
                res["matched_note"] = ("several streets in different parts of Greater Nicosia share this name; the "
                                       "figures cover all of them. Pass near_place to narrow it to one")
    if seg_info is not None:
        res["segment_posted_limit"] = seg_info
    elif a.get("segment"):
        res["segment_posted_limit"] = "not known: no sign or mapped limit covers enough of it"
    res["_summary"] = ", ".join(f"{k} km/h" for k in sorted(by_kmh, key=by_kmh.get, reverse=True)[:3]) or "limits"
    return res


# -- zones -------------------------------------------------------------------------------------------


@tool("find_slow_zones",
      "The 20 and 30 km/h zones of the national inventory (Public Works Department): three in "
      "Nicosia, Aglantzia, Kaimakli and Pallouriotissa, all listed as planned, not yet in force. "
      "Call it for 'where are the 30 zones', 'is Kaimakli a 30 zone', 'which streets are inside "
      "the slow zone', 'is this street in a 30 zone'. Give zone (a name or id) for its streets, "
      "the speed signs posted inside it, its crossings, signals and schools. Returns each zone "
      "(status, area, street km, entrances, signs inside) and a layer_ref of the zones as soft "
      "areas with their entrances (style 'area'); with zone, the streets inside are drawn too. "
      "The data_ref has one row per zone, or per street inside the zone.",
      obj({"zone": {"type": "string", "description": "A zone's name or id: 'Kaimakli', 'zone-1'."}}))
def h_find_slow_zones(ctx: ToolContext, a: dict) -> dict:
    st = streets_of(ctx)
    zones = st.records("zones")
    if not zones:
        raise ToolError("the zones inventory couldn't be read just now" + (f" ({st.age('zones')['error']})" if st.age("zones")["error"] else ""))
    chosen = zones
    if a.get("zone"):
        q = a["zone"].strip()
        chosen = [z for z in zones if z["id"] == q.lower() or loose(q) in loose(z["name"]) or loose(q) in loose(z.get("name_el"))]
        if not chosen:
            raise ToolError(f"no slow zone called {q!r}; Nicosia's are {', '.join(z['name'] for z in zones)}")
    rows, feats = [], []
    for z in chosen:
        c = A.zone_contents(st, z)
        rows.append({"id": z["id"], "zone": f"{z['kind']}, {z['name']}", "kmh": z["kmh"], "status": z["status"],
                     "area_km2": c["area_km2"], "street_km": c["street_km"], "entrances": len(c["entrances"]),
                     "signs_inside": sum(c["signs_by_kmh"].values()), "crossings_inside": len(c["crossings"]),
                     "signals_inside": len(c["signals"]), "schools_inside": len(c["schools"]),
                     "_c": c})
        feats += Ly.zone_features([z], c["entrances"])
    one = rows[0] if len(rows) == 1 else None
    if one:
        c = one["_c"]
        feats += [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": link.coords},
                   "properties": {"id": f"street-{link.way}", "layer": "zone_streets", "label": link.label or "",
                                  "color": Ly.ZONE_COLOR, "state": "street inside"}} for link in c["links"]]
        feats += Ly.school_features([{**s, "verdict": "quiet"} for s in c["schools"]])
    title = f"{chosen[0]['kind']}, {chosen[0]['name']}" if one else "Slow zones"
    ref = _layer(ctx, "slow_zones", feats, geometry="area", title=title,
                 legend={"Zone 30, planned": Ly.ZONE_COLOR}, kind="zones",
                 sources=["slow_zones"] + (["openstreetmap"] if one else []))
    if one:
        data = ctx.store.add_dataset("zone_streets", [{"street": s["street"], "length_m": s["length_m"]} for s in one["_c"]["streets"]],
                                     title=f"Streets inside {title}")
    else:
        data = ctx.store.add_dataset("slow_zones", [{k: v for k, v in r.items() if k != "_c"} for r in rows],
                                     title="Slow zones", link={"key": "id"})
    res = {"zones": [{k: v for k, v in r.items() if k != "_c"} for r in rows],
           "status_note": "every zone in the inventory is listed as planned: not yet signed or in force",
           "inventory": _age(st, "zones", "zone_entrances"), "layer_ref": ref, "data_ref": data}
    if one:
        c = one["_c"]
        res["streets_inside"] = [s["street"] for s in c["streets"][:25]]
        res["signs_inside_by_kmh"] = c["signs_by_kmh"] or "none: no speed sign in the inventory stands inside it yet"
        res["schools_inside"] = [{"id": s["id"], "name": s["name"]} for s in c["schools"][:10]]
    res["_summary"] = f"{len(rows)} zone{'s' if len(rows) != 1 else ''}, planned"
    return res


# -- crossings, and how schools and stops are served --------------------------------------------------


def rank_schools(schools: list[dict], q: str) -> list[dict]:
    """Schools by how well their name fits: its id or exactly the name ('English School' finds
    'English School' and 'The English School' before 'The English Nursery School'), then names
    that start with it, then names holding its words, shorter names first."""
    from chatbot.geo import norm
    qn = norm(q).removeprefix("the ").strip()
    ql = loose(qn)
    ranked = []
    for s in schools:
        if s["id"].lower() == q.strip().lower():
            return [s]
        for name in (s["name"], s.get("name_local")):
            n = norm(name).removeprefix("the ").strip()
            if not n:
                continue
            if n == qn:
                rank = 0
            elif n.startswith(qn):
                rank = 1
            elif all(w in n.split() for w in qn.split()):
                rank = 2
            elif ql and ql in loose(n):
                rank = 3
            else:
                continue
            ranked.append((rank, len(n), s))
            break
    return [s for _, _, s in sorted(ranked, key=lambda t: (t[0], t[1]))]


@tool("find_crossings",
      "Pedestrian crossings and traffic signals from the national inventories (Public Works "
      "Department), and how schools and bus stops are served by them. Call it for 'is there a "
      "safe crossing near this school', 'where can I cross Larnakos Avenue', 'crossings near "
      "me', 'which schools have no crossing nearby', 'which bus stops on fast roads have no "
      "crossing'. Near a place (near_place or a school's name, radius_m default 300): the "
      "crossings and signals there with distances, the schools there and the main road's posted "
      "limit. With around 'schools' or 'bus_stops' (city-wide, or near a place with radius_m "
      "default 1500): each one's nearest crossing and signal, the main road beside it and its "
      "limit, and a verdict (served, gap: beside a main road at 50 km/h or more with nothing "
      "within 250 m of a school or 120 m of a stop, slow_road, quiet), gaps first. The "
      "layer_ref shows crossings, signals and the schools or stops coloured by verdict (style "
      "'points'); the data_ref has a row per crossing or per school or stop, linked by id.",
      obj({**NEAR,
           "school": {"type": "string", "description": "A school's name, or its id (school-w123)."},
           "around": {"type": "string", "enum": ["schools", "bus_stops"],
                      "description": "Check every school or bus stop (city-wide, or near the place)."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Rows to list (default 12)."}}))
def h_find_crossings(ctx: ToolContext, a: dict) -> dict:
    st = streets_of(ctx)
    limit = a.get("limit") or 12
    center = None
    hits: list[dict] = []
    others: list[dict] = []
    if a.get("school"):
        q = a["school"].strip()
        hits = rank_schools(st.schools(), q)
        if not hits:
            raise ToolError(f"no school called {q!r} in the twin's school list (OpenStreetMap and Overture); "
                            "try near_place with its address")
        s = hits[0]
        others = [{"id": h["id"], "name": h["name"]} for h in hits[1:5]]
        center = (s["lat"], s["lon"], s["name"], float(a.get("radius_m") or 300))
    else:
        near = _near(ctx, a, 1500 if a.get("around") else 300)
        center = near
    if a.get("around"):
        return _around(ctx, st, a["around"], center, limit)
    if center is None:
        raise ToolError("give a place (near_place), a school, or around 'schools' or 'bus_stops'")
    lat, lon, label, radius = center
    xs = A.nearest(st.records("crossings"), lat, lon, radius)
    sg = A.nearest(st.records("signals"), lat, lon, radius)
    schools = A.nearest(st.schools(), lat, lon, radius)
    rep = A.crossing_report(st, lat, lon, radius)
    rows = ([{"id": x["id"], "what": "crossing", "name": x["name"], "distance_m": round(d)} for d, x in xs]
            + [{"id": s["id"], "what": "signals", "name": s["junction"], "distance_m": round(d)} for d, s in sg])
    rows.sort(key=lambda r: r["distance_m"])
    feats = Ly.crossing_features([x for _, x in xs]) + Ly.signal_features([s for _, s in sg])
    school_rows = [{**s, **A.crossing_report(st, s["lat"], s["lon"], A.SCHOOL_WALK_M)} for _, s in schools[:8]]
    feats += Ly.school_features(school_rows)
    ref = _layer(ctx, "crossings", feats, geometry="point", title=f"Crossings and signals near {label}",
                 legend={"crossing": Ly.CROSSING_COLOR, "signals": Ly.SIGNAL_COLOR}, kind="crossings",
                 sources=["pedestrian_crossings", "traffic_signals", "openstreetmap"])
    data = ctx.store.add_dataset("crossings", rows, title=f"Crossings and signals near {label}", link={"key": "id"})
    res = {"place": label, "radius_m": round(radius), "crossings": len(xs), "signals": len(sg),
           "nearest": rows[:limit], "main_road": rep["main_road"], "main_road_limit_kmh": rep["main_road_limit_kmh"],
           "zone": rep["zone"],
           "schools_here": [{"id": s["id"], "name": s["name"], "verdict": s["verdict"],
                             "nearest_crossing_m": s["nearest_crossing_m"], "nearest_signal_m": s["nearest_signal_m"]}
                            for s in school_rows],
           "note": f"{STRAIGHT}; the inventories hold marked crossings and signalled junctions the Public "
                   "Works Department lists, not every zebra the municipalities paint",
           "inventory": _age(st, "crossings", "signals"), "layer_ref": ref, "data_ref": data}
    if not A.covered(st, lat, lon):
        res["coverage"] = "the national inventories hold nothing near here (they cover the Republic's streets only)"
    if a.get("school"):
        res["school"] = {"id": hits[0]["id"], "name": hits[0]["name"]}
        if others:
            res["other_schools_matching"] = others
    res["_summary"] = f"{len(xs)} crossings, {len(sg)} signals within {round(radius)} m"
    return res


def _around(ctx, st, what: str, center, limit: int) -> dict:
    near = (center[0], center[1]) if center else None
    radius = center[3] if center else 1500
    where = f" near {center[2]}" if center else ""
    if what == "schools":
        rows = A.schools_report(st, near, radius)
        noun = "schools"
    else:
        if ctx.mobility is None:
            raise ToolError("bus stops aren't available in this deployment")
        rows = A.stops_report(st, ctx.mobility.require_timetable(), near, radius)
        noun = "bus stops on main roads"
    if not rows:
        raise ToolError(f"no {noun}{where} inside the national inventories' coverage")
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["verdict"]] += 1
    shown = rows if center else [r for r in rows if r["verdict"] != "served"] + [r for r in rows if r["verdict"] == "served"][:40]
    feats = (Ly.school_features(shown) if what == "schools" else
             [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
               "properties": {"id": r["id"], "layer": "stops", "label": r["name"], "color": A.VERDICT_COLORS[r["verdict"]],
                              "state": r["verdict"], "entity_kind": "bus_stop",
                              "popup": f"{r['name']}\n{A.VERDICTS[r['verdict']]}"}} for r in shown])
    if center:
        feats += Ly.crossing_features([x for _, x in A.nearest(st.records("crossings"), near[0], near[1], radius)])
        feats += Ly.signal_features([s for _, s in A.nearest(st.records("signals"), near[0], near[1], radius)])
    legend = {A.VERDICTS[v]: c for v, c in A.VERDICT_COLORS.items() if counts.get(v)}
    title = f"How {noun} are served for crossing{where}"
    ref = _layer(ctx, f"{what}_crossing", feats, geometry="point", title=title, legend=legend, kind="safety",
                 sources=["pedestrian_crossings", "traffic_signals", "speed_limits", "openstreetmap"]
                 + (["overture"] if what == "schools" else ["bus_timetable"]))
    keys = ("id", "name", "verdict", "nearest_crossing_m", "nearest_signal_m", "main_road", "main_road_limit_kmh", "zone")
    data = ctx.store.add_dataset(f"{what}_crossing", [{k: r.get(k) for k in keys} for r in rows], title=title, link={"key": "id"})
    res = {"checked": len(rows), "by_verdict": dict(counts), "verdicts": A.VERDICTS,
           "worst": [{k: r.get(k) for k in keys} for r in rows[:limit]],
           "rules": f"served: a crossing or signals within {round(A.SCHOOL_WALK_M if what == 'schools' else A.STOP_WALK_M)} m; "
                    f"gap: beside a main road (within {round(A.MAIN_ROAD_M)} m) posted 50 km/h or more with neither; {STRAIGHT}",
           "coverage": "places north of the buffer zone are left out: the national inventories don't cover them",
           "inventory": _age(st, "crossings", "signals", "speed_limits"), "layer_ref": ref, "data_ref": data}
    res["_summary"] = f"{counts.get('gap', 0)} of {len(rows)} {noun} with a gap"
    return res


# -- the cycle network ---------------------------------------------------------------------------------


@tool("get_cycle_network",
      "Nicosia's cycleways from the national inventory (Public Works Department), joined into "
      "connected pieces, with their loose ends and the gaps between pieces (shorter than 1.2 km) "
      "that a link would close, each with the street it would run along and that street's posted "
      "limit. Call it for 'show the cycle network', 'where does the cycle network break', 'can I "
      "cycle from A to B on cycle lanes', 'how long are the cycle lanes'. Returns the pieces "
      "(length, routes), the gaps shortest first, the loose ends, and a layer_ref of the cycleways "
      "in green with the gaps dashed and the loose ends marked (style 'route'); the data_ref has "
      "one row per gap (between, length_m, street, street_limit_kmh), or per piece when there is "
      "no gap.",
      obj({}))
def h_get_cycle_network(ctx: ToolContext, a: dict) -> dict:
    st = streets_of(ctx)
    cyc = st.records("cycleways")
    if not cyc:
        raise ToolError("the cycleways inventory couldn't be read just now")
    st.limits(wait=True)
    net = A.cycle_network(st)
    feats = Ly.cycle_features(cyc, net["gaps"], net["loose_ends"])
    ref = _layer(ctx, "cycle_network", feats, geometry="route", title="The cycle network and where it breaks",
                 legend={"cycleway": Ly.CYCLE_COLOR, "gap or loose end": Ly.GAP_COLOR}, kind="cycleways",
                 sources=["cycleways", "openstreetmap", "speed_limits"])
    if net["gaps"]:
        data = ctx.store.add_dataset("cycle_gaps", [{k: g[k] for k in ("id", "length_m", "street", "street_limit_kmh")}
                                                    | {"between": f"pieces {g['between'][0]} and {g['between'][1]}"}
                                                    for g in net["gaps"]], title="Gaps in the cycle network", link={"key": "id"})
    else:
        data = ctx.store.add_dataset("cycle_pieces", [{"piece": p["piece"], "length_m": p["length_m"],
                                                       "routes": ", ".join(p["routes"])} for p in net["pieces"]],
                                     title="Pieces of the cycle network")
    res = {"total_km": round(net["total_m"] / 1000, 1), "pieces": [{k: p[k] for k in ("piece", "length_m", "routes", "ids")} for p in net["pieces"]],
           "gaps": [{k: g[k] for k in ("id", "between", "length_m", "street", "street_limit_kmh")} for g in net["gaps"]],
           "loose_ends": len(net["loose_ends"]),
           "how": "lines whose ends come within 25 m of another line are one piece; a gap joins the nearest loose "
                  "ends of two pieces, in a straight line",
           "inventory": _age(st, "cycleways"), "layer_ref": ref, "data_ref": data}
    res["_summary"] = f"{len(net['pieces'])} pieces, {len(net['gaps'])} gaps"
    return res


# -- bus lanes and cameras ------------------------------------------------------------------------------


@tool("get_bus_lanes",
      "Bus lanes from the national inventory (Public Works Department): those in use and those "
      "planned (the tram corridors). Call it for 'where are the bus lanes', 'is there a bus lane "
      "on Makariou', 'which bus lanes are planned'. Returns each lane (name, status, length) and a "
      "layer_ref of the lanes, in use solid and planned dashed (style 'route'); the data_ref has "
      "one row per lane, linked by id.",
      obj({"status": {"type": "string", "enum": ["active", "future", "all"], "description": "Default all."}}))
def h_get_bus_lanes(ctx: ToolContext, a: dict) -> dict:
    st = streets_of(ctx)
    lanes = st.records("bus_lanes")
    want = a.get("status") or "all"
    if want != "all":
        lanes = [b for b in lanes if b["status"] == want]
    if not lanes:
        raise ToolError("no bus lane of that status in Greater Nicosia's inventory")
    rows = [{"id": b["id"], "lane": b["name"], "status": "in use" if b["status"] == "active" else "planned",
             "length_m": round(A.length_of(b["lines"]))} for b in lanes]
    ref = _layer(ctx, "bus_lanes", Ly.bus_lane_features(lanes), geometry="route", title="Bus lanes",
                 legend={"bus lane": Ly.BUS_LANE_COLOR}, kind="bus_lanes", sources=["bus_lanes"])
    data = ctx.store.add_dataset("bus_lanes", rows, title="Bus lanes", link={"key": "id"})
    active = [r for r in rows if r["status"] == "in use"]
    res = {"lanes": rows, "in_use_km": round(sum(r["length_m"] for r in active) / 1000, 1),
           "planned_km": round(sum(r["length_m"] for r in rows if r["status"] == "planned") / 1000, 1),
           "inventory": _age(st, "bus_lanes"), "layer_ref": ref, "data_ref": data}
    res["_summary"] = f"{len(active)} in use, {len(rows) - len(active)} planned"
    return res


@tool("find_enforcement_cameras",
      "Traffic enforcement cameras from the national inventory (89 island-wide, about 20 in "
      "Nicosia, mostly at junctions on the main avenues). Call it for 'where are the speed "
      "cameras', 'is there a camera on Griva Digeni', 'cameras near me'. The inventory doesn't say "
      "what each camera checks (the fixed cameras check speed and red lights). A few positions "
      "come to two decimals only; the twin moves those to the signalled junction their name gives "
      "and says so (placed). Returns each camera (name, street, direction, placed, the street's "
      "posted limit) and a layer_ref (style 'points'); the data_ref has one row per camera, "
      "linked by id.",
      obj({"road": {"type": "string", "description": "A street's name: 'Griva Digeni'."}, **NEAR}))
def h_find_enforcement_cameras(ctx: ToolContext, a: dict) -> dict:
    st = streets_of(ctx)
    cams = st.cameras()
    if a.get("road"):
        cams = [c for c in cams if loose(a["road"]) in loose(c["name"]) or loose(a["road"]) in loose(c.get("name_el"))]
    near = _near(ctx, a, 1500)
    if near:
        cams = [c for c in cams if haversine_m(near[0], near[1], c["lat"], c["lon"]) <= near[3]]
    if not cams:
        raise ToolError("no enforcement camera there in the national inventory")
    st.limits(wait=True)
    rows = []
    for c in cams:
        piece = st.limit_at(c["lon"], c["lat"], 50)
        rows.append({"id": c["id"], "camera": c["name"], "code": c.get("code"), "street": c.get("street"),
                     "direction": c.get("direction"), "placed": c["placed"], "street_limit_kmh": piece["kmh"] if piece else None})
    title = "Enforcement cameras" + (f" on {a['road']}" if a.get("road") else f" near {near[2]}" if near else "")
    ref = _layer(ctx, "cameras", Ly.camera_features(cams), geometry="point", title=title,
                 legend={"enforcement camera": Ly.CAMERA_COLOR}, kind="cameras", sources=["enforcement_cameras"])
    data = ctx.store.add_dataset("cameras", rows, title=title, link={"key": "id"})
    res = {"cameras": rows, "placed_note": "feed: as the inventory places it; junction: moved to the signalled junction "
                                           "its name gives (the feed's position is rounded to about a kilometre); rough: "
                                           "only to about a kilometre",
           "checks": "the inventory doesn't say whether a camera checks speed, red lights or both",
           "inventory": _age(st, "cameras"), "layer_ref": ref, "data_ref": data}
    res["_summary"] = f"{len(rows)} cameras"
    return res

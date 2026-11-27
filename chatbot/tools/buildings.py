"""Building tools: the EOA dangerous-building registers, where the user is among them, and the
DLS 3D buildings model."""

from __future__ import annotations

import math
from collections import Counter

from chatbot import eoa, lidar
from chatbot.dls import area_m2
from chatbot.geo import haversine_m, loose_match, norm
from chatbot.store import STATE_COLORS, legend_for, point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, my_location, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool

STATUSES = ["dangerous", "needs_repair"]
ZONES = ["urban", "rural"]
SORTS = ["height", "floors", "volume", "distance", "declared"]
# DLS 3D buildings coloured by height (upper bound in m, label, colour); dangerous ones stay red.
HEIGHT_BANDS = [(6, "under 6 m", "#cbd5e1"), (12, "6-12 m", "#93c5fd"), (20, "12-20 m", "#3b82f6"),
                (35, "20-35 m", "#1d4ed8"), (float("inf"), "35 m and up", "#312e81")]
MAX_BUILDINGS = 4000
# The walled city, where DLS models whole blocks as one building.
WALLED_CITY = (35.1745, 33.3635, 750)


def registers_ready(ctx: ToolContext) -> list[dict]:
    if ctx.registers is None:
        raise ToolError("the dangerous-building registers aren't connected in this app")
    records = ctx.registers.records(wait_s=90)
    if not records:
        raise ToolError("the registers are still loading (the first load takes about a minute); "
                        "try again shortly")
    return records


def distance_to(center: tuple[float, float], r: dict) -> float | None:
    """Metres from a point to a register building: to its plot's centre when known, as
    where_am_i measures, so the two tools agree (hand-placed markers can be 10-20 m off)."""
    spot = eoa.anchor(r)
    return haversine_m(center[0], center[1], spot[1], spot[0]) if spot else None


@tool("find_dangerous_buildings",
      "Buildings on the public dangerous-building registers of the District Local Government "
      "Organisations (ΕΟΑ) of Nicosia, Limassol, Larnaca and Famagusta (Paphos publishes "
      "none): 'dangerous' (article 15B, επικίνδυνη) or 'needs_repair' (15A, χρήζει "
      "επισκευής). Each is tied to its DLS land parcel and to the DLS 3D buildings on it, so "
      "most have a real height_m and floors. Filter by eoa, status, area (municipality, "
      "community or parish; Greek or Latin spelling), zone (Nicosia's urban areas or rural "
      "communities), street, file number, plot, distance from a place, or size. Returns "
      "counts by EOA, status, zone and area, height and floor statistics, the tallest "
      "matches (or the nearest, with a place), a layer_ref and a data_ref (one row per "
      "building: eoa, status, municipality, community, parish, zone, file_number, address, "
      "plot, height_m, floors, building_type, footprint_m2, volume_m3, declared). Show the "
      "layer with style 'buildings_3d' to extrude the buildings at their real heights (red "
      "dangerous, amber needing repair), or 'pulse', 'heatmap', or 'columns_3d' with "
      "value_field floors or height_m.",
      obj({
          "eoa": {"type": "string", "enum": list(eoa.EOAS),
                  "description": "Which district's register (default all four)."},
          "status": {"type": "string", "enum": STATUSES},
          "area": {"type": "string",
                   "description": "Municipality, community or parish, e.g. 'Strovolos', "
                                  "'ΑΓΛΑΝΤΖΙΑ', 'Paralimni'."},
          "street": {"type": "string", "description": "Street name in the address."},
          "zone": {"type": "string", "enum": ZONES,
                   "description": "'urban' (urban areas) or 'rural' (communities), as "
                                  "Nicosia's register groups them; the other registers "
                                  "don't say, so this keeps to Nicosia."},
          "file_number": {"type": "string",
                          "description": "Register file number (ΑΕΟ), e.g. '540/2024'. Only "
                                         "Nicosia's register publishes them."},
          "plot": {"type": "string",
                   "description": "Plot reference: sheet/plan and parcel, e.g. "
                                  "'21/460512 130', or just a plan or parcel number. Every "
                                  "number given must match."},
          **NEAR,
          "min_floors": {"type": "integer", "minimum": 1, "maximum": 60},
          "min_height_m": {"type": "number", "minimum": 0, "maximum": 300},
          "sort": {"type": "string", "enum": SORTS,
                   "description": "Order of the listed items: 'height' (tallest first, the "
                                  "default without a place), 'floors', 'volume' (the most "
                                  "building that could fail), 'distance' (nearest first, the "
                                  "default with a place) or 'declared' (newest first; only "
                                  "Larnaca dates its declarations)."},
          "ids": {"type": "array", "maxItems": 50, "items": {"type": "string"},
                  "description": "Only these register ids, e.g. ['NIC-409'], for buildings "
                                 "an earlier answer named (to show or describe them)."},
          "top": {"type": "integer", "minimum": 1, "maximum": 200,
                  "description": "Keep only the first N matches after sorting, in the layer "
                                 "and data_ref too: top 1 with near_place 'my location' is "
                                 "the closest one, top 5 with sort 'height' the five tallest."},
          "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                    "description": "How many buildings to list in the reply (default 12). "
                                   "The layer and data_ref hold every match (or the top N)."},
      }))
def h_find_dangerous_buildings(ctx: ToolContext, a: dict) -> dict:
    records = registers_ready(ctx)
    wanted = {i.strip().upper() for i in a.get("ids") or []}
    sel = [r for r in records
           if (not wanted or r["id"].upper() in wanted)
           and (not a.get("eoa") or r["eoa"] == a["eoa"])
           and (not a.get("status") or r["status"] == a["status"])
           and (not a.get("area") or any(loose_match(a["area"], r.get(k))
                                         for k in ("municipality", "community", "parish")))
           and (not a.get("street") or loose_match(a["street"], r.get("address"))
                or loose_match(a["street"], r.get("name")))
           and (not a.get("zone") or r.get("setting") == a["zone"])
           and (not a.get("file_number") or eoa.file_matches(a["file_number"], r))
           and (not a.get("plot") or eoa.plot_matches(a["plot"], r))
           and (r.get("floors") or 0) >= (a.get("min_floors") or 0)
           and (r.get("height_m") or 0) >= (a.get("min_height_m") or 0)]
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    dist: dict[str, float] = {}
    beyond: list[dict] = []
    if center:
        radius = a.get("radius_m") or 1000
        dist = {r["id"]: d for r in sel if (d := distance_to(center, r)) is not None}
        inside = [r for r in sel if dist.get(r["id"], 1e12) <= radius]
        if not inside:  # say how far the nearest ones are, so a retry needn't guess a radius
            beyond = [eoa.row(r, dist[r["id"]])
                      for r in sorted((r for r in sel if r["id"] in dist),
                                      key=lambda r: dist[r["id"]])[:3]]
        sel = inside
    order = a.get("sort") or ("distance" if center else "height")
    if order == "distance" and not center:
        raise ToolError("sort 'distance' needs near_place (or 'my location') or near_lat/near_lon")
    if order == "declared":  # newest first; undated ("") last
        sel = sorted(sel, key=lambda r: r.get("declared") or "", reverse=True)
    else:
        sel = sorted(sel, key={"distance": lambda r: dist[r["id"]],
                               "height": lambda r: -(r.get("height_m") or 0),
                               "floors": lambda r: -(r.get("floors") or 0),
                               "volume": lambda r: -(r.get("volume_m3") or 0)}[order])
    matched = len(sel)
    if a.get("top"):
        sel = sel[:a["top"]]

    parts = [eoa.EOAS[a["eoa"]]["name_en"] if a.get("eoa") else "EOA registers"]
    if wanted:
        parts.append(", ".join(sorted(wanted)))
    parts += [a[k] for k in ("status", "area", "zone", "street") if a.get(k)]
    parts += [f"{k.replace('_', ' ')} {a[k]}" for k in ("file_number", "plot") if a.get(k)]
    title = "Dangerous buildings: " + ", ".join(p.replace("_", " ") for p in parts)
    if center:
        title += f" near {center[2]}"
    rows = [eoa.row(r, dist.get(r["id"])) for r in sel]
    features = eoa.layer_features(sel, STATE_COLORS)
    layer = ctx.store.add_layer("dangerous", features, geometry="buildings", title=title,
                                legend=legend_for([s for s in STATUSES
                                                   if any(r["status"] == s for r in sel)]),
                                value_fields=["height_m", "floors", "volume_m3"],
                                count=len(sel))
    data = ctx.store.add_dataset("dangerous", rows, title=title, link={"key": "id"})
    heights = [r["height_m"] for r in sel if r.get("height_m") and not r.get("height_estimated")]
    floors = [r["floors"] for r in sel if r.get("floors")]
    limit = a.get("limit") or 12
    res = {
        "registers": {k: {"name": eoa.EOAS[k]["name"], "records": v.get("count"),
                          "copy": v.get("copy"), "as_of": v.get("as_of"),
                          "page": eoa.EOAS[k]["page"]}
                      for k, v in ctx.registers.sources.items()},
        "total_on_registers": len(records), "matched": matched,
        **({"kept_top": len(sel)} if a.get("top") else {}),
        "by_eoa": dict(Counter(r["eoa"] for r in sel).most_common()),
        "by_status": dict(Counter(r["status"] for r in sel).most_common()),
        "by_zone": dict(Counter(r["setting"] for r in sel if r.get("setting")).most_common()),
        "top_areas": dict(Counter(eoa.place_of(r) for r in sel).most_common(12)),
        "with_3d_building": sum(1 for r in sel if r.get("footprints")),
        "not_on_map": sum(1 for r in sel if r.get("lat") is None),
        "height_m": eoa.summary(heights), "floors": eoa.summary(floors),
        "five_floors_or_more": sum(1 for f in floors if f >= 5),
        "layer_ref": layer, "data_ref": data,
        "sorted_by": order, "items": rows[:limit],
    }
    if not ctx.registers.linked_3d:
        res["note"] = "3D heights are still being linked; ask again in a few minutes for them"
    if center:
        res["near"] = {"name": center[2], "lat": center[0], "lon": center[1],
                       "radius_m": a.get("radius_m") or 1000,
                       "distances_to": "each plot's centre (its marker where no plot is known)"}
    if beyond:
        res["nearest_beyond_radius"] = beyond
    if wanted - {r["id"].upper() for r in records}:
        res["unknown_ids"] = sorted(wanted - {r["id"].upper() for r in records})
    res["_summary"] = f"{len(sel)} of {len(records)} register buildings"
    return res


@tool("where_am_i",
      "Where the user is, from the position they shared (the locate button, or 'I'm here' "
      "on the map): the address, the nearest places, and the dangerous-register buildings "
      "around them with distances. Returns a layer_ref (the user and those buildings; show "
      "it with 'buildings_3d') and a data_ref. Give lat/lon to describe another point.",
      obj({
          "lat": {"type": "number"},
          "lon": {"type": "number"},
          "radius_m": {"type": "number", "minimum": 50, "maximum": 2000,
                       "description": "How far to look for dangerous buildings (default 300)."},
      }))
def h_where_am_i(ctx: ToolContext, a: dict) -> dict:
    if a.get("lat") is not None and a.get("lon") is not None:
        lat, lon, source, accuracy = a["lat"], a["lon"], "given point", None
    else:
        my_location(ctx)  # says how to share a location if there is none
        loc = ctx.user_location
        lat, lon = loc["lat"], loc["lon"]
        source, accuracy = loc.get("source"), loc.get("accuracy_m")
    radius = a.get("radius_m") or 300
    address = ctx.places.reverse(lat, lon) if ctx.places is not None else None
    places = []
    if ctx.places is not None and ctx.places.ready:
        found, _ = ctx.places.find(near=(lat, lon), radius_m=250, limit=5)
        places = [{"name": p.get("name"), "category": p["category"].replace("_", " "),
                   "distance_m": p["distance_m"]} for p in found]
    near = []
    if ctx.registers is not None and ctx.registers.ready:
        for r in ctx.registers.records():
            if (d := distance_to((lat, lon), r)) is not None and d <= radius:
                near.append((r, d))
    near.sort(key=lambda t: t[1])
    you = point(lon, lat, {"id": "you", "label": "You are here", "state": "you",
                           "color": STATE_COLORS["you"],
                           "popup": "You are here" + (f" (±{accuracy:.0f} m)" if accuracy else "")})
    records = [r for r, _ in near]
    layer = ctx.store.add_layer("where_am_i", [you, *eoa.layer_features(records, STATE_COLORS)],
                                geometry="buildings", title="Around you",
                                legend=legend_for(["you", *[s for s in STATUSES
                                                            if any(r["status"] == s for r in records)]]),
                                value_fields=["height_m", "floors"], count=len(records))
    rows = [eoa.row(r, d) for r, d in near]
    data = ctx.store.add_dataset("around_you", rows, title="Dangerous buildings around you",
                                 link={"key": "id"})
    in_walls = haversine_m(lat, lon, WALLED_CITY[0], WALLED_CITY[1]) < WALLED_CITY[2]
    return {
        "you_are_at": {"lat": lat, "lon": lon, "accuracy_m": accuracy, "source": source},
        "address": address, "inside_the_walled_city": in_walls, "nearest_places": places,
        "dangerous_within_m": radius, "dangerous_count": len(near),
        "by_status": dict(Counter(r["status"] for r in records).most_common()),
        "nearest_dangerous": rows[:8], "layer_ref": layer, "data_ref": data,
        "_summary": f"{len(near)} register buildings within {radius:g} m",
    }


def height_band(h: float) -> tuple[str, str]:
    return next((label, color) for top, label, color in HEIGHT_BANDS if h < top)


@tool("get_buildings_3d",
      "The Department of Lands and Surveys (DLS) 3D buildings model around a place: every "
      "building's roof and eave height above ground (m), floors, use (e.g. 'High "
      "residential building'), roof type, volume, footprint and ground height above sea "
      "level. Returns height and floor statistics, counts by use, roof type and height "
      "band, built-up share of the circle, the tallest buildings, any dangerous-register "
      "buildings among them, a layer_ref (show it with style 'buildings_3d': footprints "
      "extruded at real heights and coloured by height, dangerous ones red) and a data_ref "
      "(one row per building) for charts; where the 2019 LiDAR flight measured the building, "
      "its row also has height_2019_m (the measured top) and change_since_2014 ('taller', "
      "'lower' or 'gone') when the two differ by a floor or more. In the walled old town DLS "
      "models whole blocks as one building.",
      obj({
          **{k: v for k, v in NEAR.items() if k != "radius_m"},
          "radius_m": {"type": "number", "minimum": 20, "maximum": 800,
                       "description": "Radius in metres (default 300)."},
          "min_height_m": {"type": "number", "minimum": 0, "maximum": 300},
          "min_floors": {"type": "integer", "minimum": 1, "maximum": 60},
          "use": {"type": "string",
                  "description": "Only buildings whose use contains this, e.g. 'residential', "
                                 "'commercial', 'church', 'industrial'."},
          "limit": {"type": "integer", "minimum": 1, "maximum": 30,
                    "description": "How many of the tallest to list (default 10)."},
      }))
def h_get_buildings_3d(ctx: ToolContext, a: dict) -> dict:
    if ctx.dls is None:
        raise ToolError("the DLS 3D buildings model isn't connected in this app")
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if not center:
        raise ToolError("give near_place or near_lat and near_lon")
    radius = a.get("radius_m") or 300
    near = ctx.dls.buildings_near(center[0], center[1], radius)
    near = [(b, d) for b, d in near
            if b["properties"]["h"] >= (a.get("min_height_m") or 0)
            and (b["properties"].get("f") or 0) >= (a.get("min_floors") or 0)
            and (not a.get("use") or norm(a["use"]) in norm(b["properties"].get("t")))]
    capped = len(near) > MAX_BUILDINGS
    near = near[:MAX_BUILDINGS]
    on_register: dict[int, dict] = {}
    if ctx.registers is not None:
        for r in ctx.registers.records():
            for fp in r.get("footprints") or []:
                if r.get("match_3d") != "block":
                    on_register[fp["bid"]] = r

    features, rows = [], []
    for b, d in near:
        p = b["properties"]
        band, color = height_band(p["h"])
        rec = on_register.get(b["id"])
        state = rec["status"] if rec else band
        popup = [f"{p.get('t') or 'Building'} · DLS 3D #{b['id']}",
                 f"{p['h']} m to the roof, eave {p['e']} m"
                 + (f", {p['f']} floors" if p.get("f") else ""),
                 " · ".join(x for x in (p.get("r"), p.get("v") and f"{p['v']:,.0f} m³",
                                        p.get("a") and f"{p['a']:,.0f} m² footprint") if x)]
        if p.get("g") is not None:
            popup.append(f"ground {p['g']} m above sea level")
        measured = ctx.lidar.measured.of(b["id"]) if ctx.lidar is not None else None
        change = lidar.compare(p["h"], measured, p.get("a"))["state"] if measured else None
        if measured:
            popup.append(f"measured in 2019: {measured['top']:g} m at the top")
        if rec:
            popup.append(f"On the {eoa.EOAS[rec['eoa']]['name']} register: "
                         f"{eoa.STATUS_GR[rec['status']]} ({rec['id']})")
        features.append({"type": "Feature", "geometry": b["geometry"], "properties": {
            "id": f"B{b['id']}", "bid": b["id"], "_kind": "footprint",
            "label": p.get("t") or "building", "state": state,
            "color": STATE_COLORS[rec["status"]] if rec else color,
            "height_m": p["h"], "eave_m": p["e"], "floors": p.get("f"),
            "popup": "\n".join(x for x in popup if x)}})
        rows.append({k: v for k, v in {
            "id": f"B{b['id']}", "height_m": p["h"], "eave_m": p["e"], "floors": p.get("f"),
            "use": p.get("t"), "roof": p.get("r"), "volume_m3": p.get("v"),
            "footprint_m2": p.get("a"), "ground_m": p.get("g"), "distance_m": round(d),
            "height_2019_m": measured["top"] if measured else None,
            "change_since_2014": change if change in lidar.CHANGED else None,
            "height_band": band, "register": rec["status"] if rec else None,
            "register_id": rec["id"] if rec else None}.items() if v is not None})

    title = f"DLS 3D buildings within {radius:g} m of {center[2]}"
    legend = {label: color for _, label, color in HEIGHT_BANDS
              if any(r["height_band"] == label for r in rows)}
    legend.update({s: STATE_COLORS[s] for s in STATUSES if any(r.get("register") == s for r in rows)})
    layer = ctx.store.add_layer("buildings_3d", features, geometry="buildings", title=title,
                                legend=legend, value_fields=["height_m", "floors", "eave_m"])
    data = ctx.store.add_dataset("buildings_3d", rows, title=title, link={"key": "id"})
    footprint = sum(b["properties"].get("a") or area_m2(b["geometry"]) for b, _ in near)
    limit = a.get("limit") or 10
    res = {
        "center": {"name": center[2], "lat": center[0], "lon": center[1]}, "radius_m": radius,
        "buildings": len(near), "capped_at": MAX_BUILDINGS if capped else None,
        "height_m": eoa.summary([r["height_m"] for r in rows]),
        "floors": eoa.summary([r["floors"] for r in rows if r.get("floors")]),
        "by_height_band": dict(Counter(r["height_band"] for r in rows).most_common()),
        "by_use": dict(Counter(r.get("use") or "?" for r in rows).most_common(8)),
        "by_roof": dict(Counter(r.get("roof") or "?" for r in rows).most_common(6)),
        "built_up_pct": round(100 * footprint / (math.pi * radius ** 2), 1),
        "total_volume_m3": round(sum(r.get("volume_m3") or 0 for r in rows)),
        "tallest": sorted(rows, key=lambda r: -r["height_m"])[:limit],
        "on_dangerous_registers": [{"id": r["register_id"], "status": r["register"],
                                    "height_m": r["height_m"], "building": r["id"]}
                                   for r in rows if r.get("register_id")][:20],
        "layer_ref": layer, "data_ref": data,
        "source": "DLS INSPIRE BU3 3D buildings (Department of Lands and Surveys, Cyprus)",
    }
    if haversine_m(center[0], center[1], WALLED_CITY[0], WALLED_CITY[1]) < WALLED_CITY[2] + radius:
        res["note"] = ("inside the walls DLS models whole blocks as one building, so a block's "
                       "height is its tallest part")
    res["_summary"] = f"{len(near)} buildings, tallest {res['height_m']['max'] if rows else 0} m"
    return res


MAX_CHANGED = 4000


@tool("find_height_changes",
      "What changed between the 2014 building survey and the 2019 LiDAR flight, in greater "
      "Nicosia: buildings of the DLS 3D model whose top, as the laser measured it in 2019, "
      "differs from the surveyed roof height by a floor (3 m) or more. 'taller': a floor or "
      "more was added, or it was rebuilt taller; 'lower': it lost height; 'gone': almost "
      "nothing stands on the footprint any more (demolished or cleared). Everywhere the flight "
      "covers, or near a place; filter by change, by how much, by footprint. Returns how many "
      "buildings were measured and how many fell in each state, the matches counted by change, "
      "the biggest ones, a layer_ref (show it with style 'buildings_3d': footprints at their "
      "2019 height, the gone ones at the height they had, green taller, amber lower, red gone) "
      "and a data_ref (one row per changed building: id, change, by_m, height_2014_m, "
      "height_2019_m, standing_pct, use, floors, footprint_m2). It cannot see a building put up "
      "after 2014 on empty ground (there is no footprint to measure), a measured top counts "
      "whatever stands on the roof, and nothing north of the buffer zone was flown.",
      obj({
          **{k: v for k, v in NEAR.items() if k != "radius_m"},
          "radius_m": {"type": "number", "minimum": 50, "maximum": 10000,
                       "description": "Radius in metres around the place (default 1000)."},
          "change": {"type": "string", "enum": list(lidar.CHANGED)},
          "min_change_m": {"type": "number", "minimum": 3, "maximum": 100,
                           "description": "Only buildings taller or lower by at least this."},
          "min_footprint_m2": {"type": "number", "minimum": 0, "maximum": 100000,
                               "description": "Leave out footprints smaller than this (default "
                                              "50, which skips the sheds and garages that "
                                              "went; 0 for every footprint)."},
          "limit": {"type": "integer", "minimum": 1, "maximum": 30,
                    "description": "How many of the biggest changes to list (default 10)."},
      }))
def h_find_height_changes(ctx: ToolContext, a: dict) -> dict:
    flight = ctx.lidar
    if flight is None or not flight.changes.rows:
        raise ToolError("the 2019 LiDAR measurements aren't in this copy of the twin")
    center = None
    if a.get("near_place") or a.get("near_lat") is not None:
        center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
        if not center:
            raise ToolError("that place wasn't found; give near_lat and near_lon, or leave the "
                            "place out for everywhere the flight covers")
    radius = a.get("radius_m") or 1000
    hits = []
    for r in flight.changes.rows:
        if a.get("change") and r["state"] != a["change"]:
            continue
        if r["state"] != "gone" and abs(r["by_m"]) < (a.get("min_change_m") or 0):
            continue
        if r["area_m2"] < a.get("min_footprint_m2", lidar.HOUSE_M2):
            continue
        d = haversine_m(center[0], center[1], r["lat"], r["lon"]) if center else None
        if d is not None and d > radius:
            continue
        hits.append((r, d))
    # The biggest first: what went by the height it had, the rest by how much they changed.
    hits.sort(key=lambda h: -(h[0]["was_m"] if h[0]["state"] == "gone" else abs(h[0]["by_m"])))
    capped = len(hits) > MAX_CHANGED
    hits = hits[:MAX_CHANGED]
    rows = [{k: v for k, v in {
        "id": f"B{r['id']}", "change": r["state"], "by_m": r["by_m"], "height_2014_m": r["was_m"],
        "height_2019_m": r["now_m"], "standing_pct": round(100 * r["built"]), "use": r.get("use"),
        "floors": r.get("floors"), "footprint_m2": r["area_m2"],
        "distance_m": round(d) if d is not None else None}.items() if v is not None}
        for r, d in hits]
    where = f"within {radius:g} m of {center[2]}" if center else "in greater Nicosia"
    title = f"Buildings changed between 2014 and 2019 {where}"
    by_change = Counter(r["change"] for r in rows)
    layer = ctx.store.add_layer(
        "height_changes", lidar.layer_features([r for r, _ in hits], STATE_COLORS),
        geometry="buildings", title=title, legend=legend_for(list(by_change)),
        value_fields=["height_m", "by_m"])
    data = ctx.store.add_dataset("height_changes", rows, title=title, link={"key": "id"})
    counts = flight.changes.counts
    res = {
        "area": ({"name": center[2], "lat": center[0], "lon": center[1], "radius_m": radius}
                 if center else "greater Nicosia, south of the buffer zone"),
        "measured_in_nicosia": {"buildings": sum(counts.values()), **counts},
        "matching": len(rows), "capped_at": MAX_CHANGED if capped else None,
        "by_change": dict(by_change.most_common()),
        "biggest": rows[:a.get("limit") or 10],
        "layer_ref": layer, "data_ref": data,
        "source": "DLS surface and terrain models of the 2019 LiDAR flight (CC BY 4.0), against "
                  "the DLS 3D buildings surveyed in 2014",
        "note": "a change is a difference of 3 m or more between the two surveys on the same "
                "footprint; buildings put up after 2014 on empty ground are not in it",
    }
    res["_summary"] = (f"{len(rows)} changed buildings {where}: " + ", ".join(
        f"{n} {k}" for k, n in by_change.most_common())) if rows else f"No changed buildings {where}"
    return res

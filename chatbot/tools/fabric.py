"""Urban fabric tools: heat_exposure, accessibility_15min, and population_reach."""

from __future__ import annotations

from chatbot.fabric.gradients import SLOPE_COLORS, summarize_gradients
from chatbot.store import STATE_COLORS, point
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool


def _fabric(ctx: ToolContext):
    fab = getattr(ctx, "fabric", None)
    if fab is None or not fab.ready:
        raise ToolError("the urban fabric dataset isn't loaded yet; run python -m chatbot.fabric build")
    return fab


@tool(
    "heat_exposure",
    "Summer ground surface temperature and human heat exposure across Greater Nicosia's H3 "
    "resolution-8 hexagons, combining Landsat 8/9 morning ground surface temperature (~10:30 "
    "local, NOT air temperature), Kontur modelled 2022 population, and Overture green/shade "
    "context. Always state clearly that Landsat measures morning ground surface temperature "
    "(~10:30 local), never air temperature, and show the heat-exposure score's three component "
    "parts (surface_temp_c, population_2022, green_pct). Returns a layer_ref (show with style "
    "'area') and a data_ref linked by id.",
    obj({
        "place": {"type": "string", "description": "Optional place name (omit for all of Greater Nicosia)."},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "radius_m": {"type": "number", "minimum": 300, "maximum": 12000,
                     "description": "Search radius in metres when a place/point is given (default 2000)."},
        "metric": {"type": "string", "enum": ["exposure", "surface_heat"],
                   "description": "'exposure' ranks heat x people x lack of green/shade (default); 'surface_heat' ranks raw ground surface temperature."},
        "top": {"type": "integer", "minimum": 1, "maximum": 50,
                "description": "How many top hexagons to include in the reply and chart rows (default 12)."},
    }),
)
def h_heat_exposure(ctx: ToolContext, a: dict) -> dict:
    fab = _fabric(ctx)
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    radius = float(a.get("radius_m") or 2000.0) if center else None
    mode = a.get("metric") or "exposure"
    layer_metric = "surface_heat" if mode == "surface_heat" else "heat_exposure"
    top_n = int(a.get("top") or 12)

    fc = fab.hexagons_geojson(
        metric=layer_metric,
        center=(center[0], center[1]) if center else None,
        radius_m=radius,
        min_pop=1 if mode == "exposure" else 0,
    )
    feats = fc["features"]
    if not feats:
        raise ToolError("no Greater Nicosia hexagons matched that area")

    # Sort features by chosen metric (already sorted descending by hexagons_geojson)
    rows = []
    top_items = []
    by_id = {h["id"]: h for h in fab.hexagons()}
    for f in feats[: max(top_n, 25)]:
        p = f["properties"]
        h = by_id.get(p["id"]) or {}
        parts = h.get("heat_parts") or {}
        row = {
            "id": p["id"],
            "name": p["name"],
            "heat_exposure_score": p["heat_exposure_score"],
            "heat_band": p["heat_band_label"],
            "surface_temp_c": p["lst_median_c"],
            "surface_temp_max_c": p.get("lst_max_c"),
            "population_2022": p["population"],
            "green_pct": p["green_pct"],
            "heat_component": round((parts.get("heat_norm") or 0) * 45, 1),
            "people_component": round((parts.get("pop_norm") or 0) * 35, 1),
            "shade_gap_component": round((parts.get("lack_of_cool_norm") or 0) * 20, 1),
            "access_15min_score": p.get("access_15min_score"),
        }
        rows.append(row)
        if len(top_items) < top_n:
            top_items.append({**row, "parts": parts})

    title = (
        f"Summer morning ground heat near {center[2]}" if center and mode == "surface_heat"
        else f"Summer heat exposure near {center[2]}" if center
        else fc["meta"]["title"]
    )
    layer = ctx.store.add_layer(
        "heat_exposure",
        feats,
        geometry="area",
        title=title,
        legend=fc["meta"]["legend"],
        value_fields=[fc["meta"]["value_field"], "lst_median_c", "population", "heat_exposure_score"],
        sources=["landsat_lst", "kontur_population", "overture_base", "dls"],
    )
    ctx.store.layers[layer]["meta"]["fabric"] = fc["meta"]["fabric"]
    data = ctx.store.add_dataset(
        "heat_exposure",
        rows,
        title=title,
        link={"key": "id"},
        sources=["landsat_lst", "kontur_population", "overture_base"],
    )
    hg = fab.heat_grid_payload()
    return {
        "metric": mode,
        "area": center[2] if center else "Greater Nicosia",
        "temperature_kind": "morning ground surface temperature (Landsat 8/9, ~10:30 local, summer median), NOT air temperature",
        "population_note": "Kontur modelled 2022 population (H3 res-8 ~0.7 km² hexagons)",
        "composite_weights": "45% morning ground surface heat + 35% modelled 2022 population + 20% lack of green/shade cooling",
        "city_heat_summary": {
            "min_c": hg.get("min_c"),
            "median_c": hg.get("median_c"),
            "p90_c": hg.get("p90_c"),
            "max_c": hg.get("max_c"),
            "scenes_used": hg.get("scenes_used"),
            "date_from": hg.get("date_from"),
            "date_to": hg.get("date_to"),
            "overpass_local": hg.get("overpass_local"),
        },
        "hexagons_matched": len(feats),
        "top_hexagons": top_items,
        "layer_ref": layer,
        "data_ref": data,
        "_summary": f"{len(feats)} hexagons, top {top_items[0]['surface_temp_c']} °C ground (~10:30)",
    }


@tool(
    "accessibility_15min",
    "15-minute walkability and step-free/gentle-slope accessibility across Greater Nicosia, "
    "combining OSM places, Overture places & base infrastructure (parks, recycling, benches, "
    "crossings), GTFS bus stops, Kontur 2022 population, and CyNAP road gradients (>5% moderate, "
    ">8% steep). Pass a place or coordinates for a point-level breakdown across 6 everyday "
    "service categories (standard walk vs step-free/gentle-slope variant), or leave out for a "
    "city-wide ranking. Returns a layer_ref (show with 'area') and data_ref linked by id.",
    obj({
        "place": {"type": "string", "description": "Place or neighbourhood to evaluate (omit for city-wide)."},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "mode": {"type": "string", "enum": ["both", "standard", "step_free"],
                 "description": "Highlight 'standard' walk, 'step_free' (gentle slope <=5%/<=8% & shorter reach), or 'both' (default)."},
        "radius_m": {"type": "number", "minimum": 400, "maximum": 12000,
                     "description": "Area radius in metres when a point is given (default 1800)."},
        "top": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Rows in reply (default 12)."},
    }),
)
def h_accessibility_15min(ctx: ToolContext, a: dict) -> dict:
    fab = _fabric(ctx)
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    mode = a.get("mode") or "both"
    top_n = int(a.get("top") or 12)
    layer_metric = "access_step_free" if mode == "step_free" else "access_15min"

    fc = fab.hexagons_geojson(
        metric=layer_metric,
        center=(center[0], center[1]) if center else None,
        radius_m=float(a.get("radius_m") or 1800.0) if center else None,
    )
    feats = list(fc["features"])
    if not feats:
        raise ToolError("no Greater Nicosia hexagons matched that area")

    # Include nearby steep/moderate CyNAP gradient points so barriers appear on the map
    grad_pts = fab.gradients.points()
    steep_added = 0
    for p in grad_pts:
        if p["slope_pct"] <= 5.0:
            continue
        if center is not None:
            from chatbot.geo import haversine_m
            if haversine_m(center[0], center[1], p["lat"], p["lon"]) > float(a.get("radius_m") or 1800.0):
                continue
        elif steep_added >= 220:
            break
        band = p["band"]
        feats.append(
            point(
                p["lon"],
                p["lat"],
                {
                    "id": p["id"],
                    "kind": "road_gradient",
                    "label": f"{p.get('road_name') or p.get('road_code')}: {p['slope_pct']}%",
                    "state": f"slope {band}",
                    "color": SLOPE_COLORS[band],
                    "slope_pct": p["slope_pct"],
                    "popup": f"{p.get('road_name') or p.get('road_code')}\nSlope: {p['slope_pct']}% ({band})",
                },
            )
        )
        steep_added += 1

    if center is not None:
        feats.append(
            point(
                center[1],
                center[0],
                {
                    "id": "origin",
                    "label": center[2],
                    "state": "origin",
                    "color": STATE_COLORS["you"],
                    "popup": f"{center[2]}\n15-minute walkability origin",
                },
            )
        )

    hex_rows = [
        {
            "id": f["properties"]["id"],
            "name": f["properties"]["name"],
            "access_15min_score": f["properties"]["access_15min_score"],
            "access_step_free_score": f["properties"]["access_step_free_score"],
            "step_free_gap": f["properties"]["step_free_gap"],
            "population_2022": f["properties"]["population"],
            "slopes_gt5": f["properties"]["grad_gt5"],
            "slopes_gt8": f["properties"]["grad_gt8"],
        }
        for f in fc["features"][: max(top_n, 25)]
    ]

    legend = {
        **fc["meta"]["legend"],
        **(
            {"Slope 5 to 8%": SLOPE_COLORS["moderate"], "Slope >8%": SLOPE_COLORS["steep"]}
            if steep_added
            else {}
        ),
    }
    title = (
        f"15-min {'step-free ' if mode == 'step_free' else ''}access around {center[2]}"
        if center
        else fc["meta"]["title"]
    )
    layer = ctx.store.add_layer(
        "access_15min",
        feats,
        geometry="area",
        title=title,
        legend=legend,
        value_fields=[fc["meta"]["value_field"], "access_15min_score", "access_step_free_score"],
        count=len(fc["features"]),
        sources=["openstreetmap", "overture", "overture_base", "bus_timetable", "cynap_gradients", "kontur_population"],
    )
    ctx.store.layers[layer]["meta"]["fabric"] = fc["meta"]["fabric"]
    data = ctx.store.add_dataset(
        "access_15min",
        hex_rows,
        title=title,
        link={"key": "id"},
        sources=["openstreetmap", "overture", "overture_base", "bus_timetable", "cynap_gradients"],
    )

    grad_summary = summarize_gradients(grad_pts)
    pop_total = sum(int(h["population"]) for h in fab.hexagons())
    pop_high_std = sum(int(h["population"]) for h in fab.hexagons() if (h.get("access_15min_score") or 0) >= 60)
    pop_high_sf = sum(int(h["population"]) for h in fab.hexagons() if (h.get("access_step_free_score") or 0) >= 60)

    res: dict = {
        "mode": mode,
        "area": center[2] if center else "Greater Nicosia",
        "city_summary": {
            "total_modelled_2022_pop": pop_total,
            "pop_with_good_15min_access_60plus": pop_high_std,
            "pop_with_good_step_free_60plus": pop_high_sf,
            "cynap_gradients_in_city": grad_summary,
        },
        "caveats": (
            "15-min reach uses crow-flies x1.25 street detour at 4.8 km/h (standard) and 3.6 km/h "
            "with CyNAP grade penalties (>5% moderate, >8% steep barrier). OSM/Overture completeness "
            "varies by suburb, and CyNAP gradients cover classified roads mostly south of the buffer zone."
        ),
        "hexagons": hex_rows[:top_n],
        "layer_ref": layer,
        "data_ref": data,
    }
    if center is not None:
        pt_acc = fab.access_at(center[0], center[1])
        cat_rows = [
            {
                "category": c["category"],
                "service": c["label"],
                "walk_min": c.get("walk_min"),
                "step_free_min": c.get("step_free_min"),
                "reachable_15min": c["reachable_15min"],
                "reachable_step_free": c["reachable_step_free"],
                "slope_barrier": c.get("slope_barrier", False),
                "nearest": c["nearest"]["name"] if c.get("nearest") else None,
                "distance_m": c["nearest"]["distance_m"] if c.get("nearest") else None,
            }
            for c in pt_acc["categories"]
        ]
        cat_data = ctx.store.add_dataset(
            "access_categories",
            cat_rows,
            title=f"Everyday services within 15 min of {center[2]}",
            sources=["openstreetmap", "overture", "overture_base", "bus_timetable", "cynap_gradients"],
        )
        res["point_access"] = pt_acc
        res["categories_data_ref"] = cat_data
        res["_summary"] = (
            f"{center[2]}: {pt_acc['access_15min_score']}/100 walk, "
            f"{pt_acc['access_step_free_score']}/100 step-free"
        )
    else:
        res["_summary"] = f"{len(fc['features'])} hexagons, {grad_summary['gt5']} slopes >5%"
    return res


@tool(
    "population_reach",
    "Estimate how many people live within a walking/impact radius of a point (or across Greater "
    "Nicosia) using Kontur H3 resolution-8 hexagons (~0.7 km²). Always state clearly that these "
    "are modelled 2022 population figures (Kontur, CC BY 4.0) built for visualisation across both "
    "sides of the buffer zone. Returns a layer_ref (show with 'area') and a data_ref linked by id.",
    obj({
        "place": {"type": "string", "description": "Place name or 'my location' (omit for city-wide distribution)."},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "radius_m": {"type": "number", "minimum": 100, "maximum": 10000,
                     "description": "Radius in metres around the point (default 500)."},
        "top": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Hexagons to list (default 12)."},
    }),
)
def h_population_reach(ctx: ToolContext, a: dict) -> dict:
    fab = _fabric(ctx)
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    top_n = int(a.get("top") or 12)

    if center is not None:
        radius = float(a.get("radius_m") or 500.0)
        reach = fab.population_within(center[0], center[1], radius)
        hex_ids = {h["id"] for h in reach["hexes"]}
        fc = fab.hexagons_geojson(metric="people")
        feats = [f for f in fc["features"] if f["properties"]["id"] in hex_ids]
        feats.append(
            point(
                center[1],
                center[0],
                {
                    "id": "origin",
                    "label": center[2],
                    "state": "origin",
                    "color": STATE_COLORS["you"],
                    "popup": f"{center[2]}\n~{reach['estimated_population']:,} modelled 2022 residents within {round(radius)} m",
                },
            )
        )
        title = f"People within {round(radius)} m of {center[2]} (Kontur 2022)"
        rows = [
            {
                "id": h["id"],
                "name": h["name"],
                "population_2022": h["population_2022"],
                "overlap_pct": h["overlap_pct"],
                "distance_m": h["distance_m"],
                "surface_temp_c": h.get("lst_median_c"),
                "heat_exposure_score": h.get("heat_exposure_score"),
            }
            for h in reach["hexes"]
        ]
        layer = ctx.store.add_layer(
            "population_reach",
            feats,
            geometry="area",
            title=title,
            legend=fc["meta"]["legend"],
            value_fields=["population"],
            count=len(reach["hexes"]),
            sources=["kontur_population", "overture_base"],
        )
        ctx.store.layers[layer]["meta"]["fabric"] = fc["meta"]["fabric"]
        data = ctx.store.add_dataset(
            "population_reach",
            rows,
            title=title,
            link={"key": "id"},
            sources=["kontur_population"],
        )
        return {
            "area": center[2],
            "radius_m": round(radius),
            "estimated_population_2022": reach["estimated_population"],
            "centroid_population_2022": reach["centroid_population"],
            "hex_count": reach["hex_count"],
            "population_note": reach["population_note"],
            "hexagons": rows[:top_n],
            "layer_ref": layer,
            "data_ref": data,
            "_summary": f"~{reach['estimated_population']:,} people (2022 model) within {round(radius)} m",
        }

    fc = fab.hexagons_geojson(metric="people")
    total_pop = sum(int(h["population"]) for h in fab.hexagons())
    rows = [
        {
            "id": f["properties"]["id"],
            "name": f["properties"]["name"],
            "population_2022": f["properties"]["population"],
            "pop_density_km2": f["properties"]["pop_density_km2"],
            "surface_temp_c": f["properties"]["lst_median_c"],
            "access_15min_score": f["properties"]["access_15min_score"],
        }
        for f in fc["features"][: max(top_n, 25)]
    ]
    layer = ctx.store.add_layer(
        "population_city",
        fc["features"],
        geometry="area",
        title=fc["meta"]["title"],
        legend=fc["meta"]["legend"],
        value_fields=["population"],
        sources=["kontur_population"],
    )
    ctx.store.layers[layer]["meta"]["fabric"] = fc["meta"]["fabric"]
    data = ctx.store.add_dataset(
        "population_city",
        rows,
        title="Most populated Greater Nicosia hexagons (Kontur 2022)",
        link={"key": "id"},
        sources=["kontur_population"],
    )
    return {
        "area": "Greater Nicosia",
        "total_population_2022": total_pop,
        "hex_count": len(fc["features"]),
        "population_note": "Kontur modelled 2022 population (H3 res-8 ~0.7 km² hexagons) across north and south Nicosia",
        "top_hexagons": rows[:top_n],
        "layer_ref": layer,
        "data_ref": data,
        "_summary": f"{total_pop:,} modelled 2022 residents across {len(fc['features'])} hexagons",
    }

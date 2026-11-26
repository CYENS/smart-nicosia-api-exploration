"""Place tools: geocoding and points of interest from the OpenStreetMap index."""

from __future__ import annotations

from chatbot.geo import CATEGORIES
from chatbot.store import STATE_COLORS, point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, local_now, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool


@tool("geocode",
      "Find the coordinates of a place, street or address in Nicosia. Use plain names "
      "('Ledras', 'Cyprus Museum', 'Eleftheria Square').",
      obj({"query": {"type": "string"}}, ["query"]))
def h_geocode(ctx: ToolContext, a: dict) -> dict:
    hits = ctx.places.geocode(a["query"])
    if not hits:
        raise ToolError(f"nothing found for {a['query']!r}; try another spelling or a nearby landmark")
    return {"results": hits, "_summary": hits[0].get("name") or a["query"]}


@tool("find_places",
      "Points of interest from OpenStreetMap: museums, cafés, pharmacies, toilets, drinking "
      "water, parks and more, by category, name and/or distance. Returns places with opening "
      "hours where mapped (raw OSM opening_hours strings; judge them against the local time "
      "in <context>) and a layer_ref.",
      obj({
          "category": {"type": "string", "enum": list(CATEGORIES)},
          "query": {"type": "string", "description": "Name or part of a name."},
          **NEAR,
          "limit": {"type": "integer", "minimum": 1, "maximum": 50},
      }))
def h_find_places(ctx: ToolContext, a: dict) -> dict:
    if not a.get("category") and not a.get("query"):
        raise ToolError("give a category or a query")
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    near = (center[0], center[1]) if center else None
    radius = (a.get("radius_m") or 1000) if near else None
    places, source = ctx.places.find(a.get("category"), a.get("query"), near, radius, limit=200)
    feats = [point(p["lon"], p["lat"], {
        "id": p["id"], "label": p.get("name") or p["category"], "state": "place",
        "color": STATE_COLORS["place"],
        "popup": "\n".join(x for x in (p.get("name"), p["category"].replace("_", " "),
                                       p.get("opening_hours")) if x),
    }) for p in places]
    title = (a.get("category") or a.get("query") or "places").replace("_", " ")
    layer = ctx.store.add_layer(title, feats, geometry="point", title=title.capitalize(),
                                legend={"place": STATE_COLORS["place"]})
    limit = a.get("limit") or 15
    return {"source": source, "matched": len(places), "places": places[:limit],
            "layer_ref": layer, "local_time_now": local_now(ctx).strftime("%a %H:%M"),
            "_summary": f"{len(places)} {title}"}

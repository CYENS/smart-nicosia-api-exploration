"""Place tools: geocoding, points of interest from the OpenStreetMap index, and the places a web
search found, put on the map."""

from __future__ import annotations

import logging

from chatbot.geo import CATEGORIES, haversine_m
from chatbot.store import STATE_COLORS, point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, local_now, obj, resolve_point

log = logging.getLogger(__name__)
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


WEB_PLACE = obj({
    "name": {"type": "string", "minLength": 2, "maxLength": 120},
    "address": {"type": "string", "maxLength": 200,
                "description": "As the result gives it, with its postcode and area when it "
                               "gives them: 'Nikiforou Foka 33, 1016 Nicosia', 'Yiannaki "
                               "Talioti 7, Strovolos'. Nicosia has several streets of one "
                               "name, and the postcode tells them apart."},
    "phone": {"type": "string", "maxLength": 40},
    "website": {"type": "string", "maxLength": 300},
    "source_url": {"type": "string", "maxLength": 500,
                   "description": "The page the place was found on."},
    "note": {"type": "string", "maxLength": 200,
             "description": "A few words from the result on what it offers."},
}, ["name"])


@tool("locate_web_places",
      "Put places found with web_search on the map: schools, studios, teachers, shops, clubs "
      "and services that find_places doesn't list. Pass each place as the results give it: its "
      "name and address, and its phone, website, the page it came from and a few words on what "
      "it offers. Each is placed from OpenStreetMap or its address, never guessed: precision "
      "'exact' is at the door, 'street' only somewhere on its street; places that can't be "
      "found in Nicosia come back in not_placed. Returns an id per place (web-1a2b3c4d) that "
      "opens it in the inspector and works as a place in plan_route and plan_transit, a "
      "layer_ref and a data_ref. Takes a few seconds per new address (the geocoder's limit).",
      obj({
          "places": {"type": "array", "minItems": 1, "maxItems": 12, "items": WEB_PLACE},
          "topic": {"type": "string", "maxLength": 60,
                    "description": "What they are, as the legend title: 'Singing lessons'."},
      }, ["places"]))
def h_locate_web_places(ctx: ToolContext, a: dict) -> dict:
    if ctx.web_places is None:
        raise ToolError("placing web results isn't available here; list them without a map")
    topic = " ".join((a.get("topic") or "").split()) or "Found on the web"
    loc = ctx.user_location
    placed, missing = [], []
    for item in a["places"]:
        place, why = ctx.web_places.locate(item, topic)
        if place is None:
            log.info("web place not placed: %r at %r: %s", item["name"], item.get("address"), why)
            missing.append({"name": item["name"], "address": item.get("address"), "reason": why})
            continue
        row = {k: place[k] for k in ("id", "name", "precision", "located_by", "address", "phone",
                                     "website", "note") if place.get(k)}
        if loc:
            row["distance_m"] = round(haversine_m(loc["lat"], loc["lon"], place["lat"], place["lon"]))
        placed.append((row, place))
    if loc:
        placed.sort(key=lambda t: t[0]["distance_m"])
    res: dict = {"topic": topic, "placed": len(placed), "of": len(a["places"]),
                 "places": [row for row, _ in placed], "not_placed": missing}
    if missing:
        res["hint"] = ("list the places not placed without an id; for one with no address, a "
                       "web_search for its address may let it be placed; for one whose street "
                       "name Nicosia has more than once, pass it again with the postcode the "
                       "page gives")
    if not placed:
        res["_summary"] = f"none of {len(a['places'])} found on the map"
        return res
    feats = []
    for row, p in placed:
        state = "from the web" if p["precision"] == "exact" else "on its street"
        lines = [p["name"], p.get("note"), p.get("address"), p.get("phone"),
                 "Found on the web" + ("; pinned on its street, not at the door"
                                       if state == "on its street" else "")]
        feats.append(point(p["lon"], p["lat"], {
            "id": p["id"], "label": p["name"], "state": state, "color": STATE_COLORS[state],
            **({"distance_m": row["distance_m"]} if "distance_m" in row else {}),
            "popup": "\n".join(x for x in lines if x)}))
    states = {f["properties"]["state"] for f in feats}
    if loc:  # the distances are from here, and the camera fits both
        states.add("you")
        feats.append(point(loc["lon"], loc["lat"], {
            "id": "you", "label": "You are here", "state": "you", "color": STATE_COLORS["you"],
            "popup": "You are here\nThe distances are from this point"}))
    res["layer_ref"] = ctx.store.add_layer(
        topic, feats, geometry="point", title=topic, count=len(placed),
        legend={s: STATE_COLORS[s] for s in ("from the web", "on its street", "you") if s in states})
    res["data_ref"] = ctx.store.add_dataset(topic, [row for row, _ in placed], title=topic,
                                            link={"key": "id"})
    res["_summary"] = f"{len(placed)} of {len(a['places'])} on the map"
    return res

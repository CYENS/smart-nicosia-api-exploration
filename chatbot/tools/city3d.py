"""The 3D city as it looks: OpenStreetMap's trees around a place (chatbot/trees.py), and the DLS
aerial photos on the ground or in the clock's compare swipe (chatbot/imagery.py)."""

from __future__ import annotations

import math
from collections import Counter

from chatbot import eoa
from chatbot.store import point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool

TREE_COLORS = {"mapped tree": "#22c55e", "tree row": "#14b8a6", "estimated in a park": "#86efac",
               "estimated in a wood": "#4d7c0f", "estimated in an orchard": "#bef264"}
LABEL = {"mapped": "mapped tree", "row": "tree row", "park": "estimated in a park",
         "wood": "estimated in a wood", "orchard": "estimated in an orchard"}
MAX_ROWS = 3000


@tool("find_trees",
      "Trees around a place, from OpenStreetMap: trees mapped one by one (with their height and "
      "kind when tagged), trees along mapped tree rows (one every 8 m), and trees estimated in "
      "mapped parks, woods and orchards (OSM says there are trees there, not where each stands; "
      "they are counted apart). Returns counts by kind, mapped against estimated, leaf types "
      "and genera where tagged, heights, the share of the circle under tree crowns (an "
      "estimate), a layer_ref (a point per tree, coloured by kind; value fields height_m and "
      "crown_m) and a data_ref (one row per tree: id, kind, estimated, leaf, genus, height_m, "
      "crown_m, distance_m), linked by id. The 3D city draws the same trees.",
      obj({
          **{k: v for k, v in NEAR.items() if k != "radius_m"},
          "radius_m": {"type": "number", "minimum": 50, "maximum": 1500,
                       "description": "Radius in metres (default 300)."},
          "mapped_only": {"type": "boolean",
                          "description": "Only trees OSM maps one by one or in rows."},
      }))
def h_find_trees(ctx: ToolContext, a: dict) -> dict:
    trees = getattr(ctx, "trees", None)
    if trees is None:
        raise ToolError("the trees aren't connected in this app")
    if not trees.ready:
        raise ToolError("OpenStreetMap's trees are still being read (Overpass can be busy); "
                        "say so and try again in a few minutes")
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if not center:
        raise ToolError("give near_place or near_lat and near_lon")
    radius = a.get("radius_m") or 300
    near = trees.near(center[0], center[1], radius)
    if a.get("mapped_only"):
        near = [(t, d) for t, d in near if t["kind"] in ("mapped", "row")]
    feats, rows = [], []
    for i, (t, d) in enumerate(near):
        state = LABEL[t["kind"]]
        tid = f"tree-{t.get('osm', 'x')}-{i}"
        estimated = t["kind"] in ("park", "wood", "orchard")
        if len(rows) < MAX_ROWS:
            feats.append(point(t["lon"], t["lat"], {
                "id": tid, "label": t.get("genus") or "tree", "state": state, "color": TREE_COLORS[state],
                "height_m": t["h"], "crown_m": round(t["r"] * 2, 1),
                "popup": "\n".join(x for x in (
                    (t.get("genus") or t["leaf"].replace("leaved", "-leaved")).capitalize(),
                    f"About {t['h']:g} m tall, crown {t['r'] * 2:g} m across",
                    state.capitalize() + (" (its place is an estimate)" if estimated else ""),
                    f"OpenStreetMap {t['osm']}" if t.get("osm") else None) if x)}))
            rows.append({k: v for k, v in {
                "id": tid, "kind": t["kind"], "estimated": estimated, "leaf": t["leaf"],
                "genus": t.get("genus"), "height_m": t["h"], "crown_m": round(t["r"] * 2, 1),
                "distance_m": round(d)}.items() if v is not None})
    kinds = Counter(t["kind"] for t, _ in near)
    mapped = kinds["mapped"] + kinds["row"]
    canopy = sum(math.pi * t["r"] ** 2 for t, _ in near)
    title = f"Trees within {radius:g} m of {center[2]}"
    states = [s for s in TREE_COLORS if any(f["properties"]["state"] == s for f in feats)]
    layer = ctx.store.add_layer("trees", feats, geometry="point", title=title,
                                legend={s: TREE_COLORS[s] for s in states},
                                value_fields=["height_m", "crown_m"]) if feats else None
    data = ctx.store.add_dataset("trees", rows, title=title, link={"key": "id"}) if rows else None
    return {
        "center": {"name": center[2], "lat": center[0], "lon": center[1]}, "radius_m": radius,
        "trees": len(near), "mapped": mapped, "estimated": len(near) - mapped,
        "by_kind": {LABEL[k]: n for k, n in kinds.most_common()},
        "by_leaf": dict(Counter(t["leaf"] for t, _ in near).most_common()),
        "by_genus": dict(Counter(t["genus"] for t, _ in near if t.get("genus")).most_common(8)),
        "height_m": eoa.summary([t["h"] for t, _ in near]),
        "crown_cover_pct": round(min(100.0, 100 * canopy / (math.pi * radius ** 2)), 1),
        "listed": len(rows), "layer_ref": layer, "data_ref": data,
        "note": ("OSM maps few of Nicosia's street trees one by one, so counts are a floor, not a "
                 "census; estimated trees stand where OSM maps a park, wood or orchard. Heights "
                 "without a tag are typical sizes for the kind of tree. Crown cover is the crowns' "
                 "area over the circle's, overlaps counted twice."),
        "_summary": f"{len(near)} trees ({mapped} mapped) within {radius:g} m",
    }


PHOTO_YEARS = {"2019": "on the ground, under the 3D city", "1993": "left of the compare swipe",
               "1963": "left of the compare swipe"}


@tool("show_aerial_photo",
      "Show the Department of Lands and Surveys' aerial photos of Nicosia. '2019' lays the 2019 "
      "photo (about 7.5 cm a pixel, in colour) on the ground under the 3D city, which then "
      "shows its buildings as they look (plaster, stone and tiled roofs); it stops at the Green "
      "Line. '1993' and '1963' (black and white) open the city clock's compare swipe: the old "
      "photo left of the line, the twin as it is right of it, so the user can drag the line "
      "across streets to see what changed. 'map' puts the base map back on the ground and "
      "closes a photo comparison. With near_place the map flies there first.",
      obj({
          "photo": {"type": "string", "enum": ["2019", "1993", "1963", "map"],
                    "description": "Which photo, or 'map' to go back to the base map."},
          "near_place": {"type": "string", "description": "Fly the map to this place first."},
          "near_lat": {"type": "number"},
          "near_lon": {"type": "number"},
          "city_3d": {"type": "boolean",
                      "description": "Also turn on the 3D city (default true with the 2019 photo)."},
      }, ["photo"]))
def h_show_aerial_photo(ctx: ToolContext, a: dict) -> dict:
    photo = a["photo"]
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    ev: dict = {"type": "ui", "action": "photo"}
    if photo == "map":
        ev.update(ground="map", compare="off")
    elif photo == "2019":
        ev.update(ground="photo", city_3d=a.get("city_3d", True))
    else:
        ev.update(compare=photo, city_3d=bool(a.get("city_3d", False)))
    if center:
        ev["fly"] = {"lat": center[0], "lon": center[1], "zoom": 16.5 if photo == "2019" else 16}
    ctx.emit(ev)
    facts = {
        "map": "The base map is back on the ground; any photo comparison is closed.",
        "2019": "The 2019 aerial photo is on the ground, the 3D city shows as it looks, and the "
                "photo stops at the Green Line (DLS has none north of it).",
        "1993": "The compare swipe shows the 1993 photo left of the line and the twin today "
                "right of it; dragging the line or its arrow keys moves it.",
        "1963": "The compare swipe shows the 1963 photo left of the line and the twin today "
                "right of it; dragging the line or its arrow keys moves it. 1963 is before the "
                "division: the photo covers north Nicosia too.",
    }[photo]
    return {"ok": True, "photo": photo, "shown": PHOTO_YEARS.get(photo, "base map"), "facts": facts,
            "place": center[2] if center else None,
            "_summary": "base map on the ground" if photo == "map" else f"{photo} aerial photo"}

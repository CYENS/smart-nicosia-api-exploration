"""Map tools: draw a layer, move the camera, pin callouts and clear the map. They push commands to
the page through `ctx.emit`."""

from __future__ import annotations

from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool

POINT_STYLES = ["points", "pulse", "heatmap", "columns_3d"]
STYLES_FOR = {"point": POINT_STYLES, "route": ["route"], "area": ["area"],
              "buildings": [*POINT_STYLES, "buildings_3d", "area"]}


@tool("show_on_map",
      "Draw a layer from a data tool (its layer_ref) on the user's map. Styles for point "
      "layers: 'points' (coloured by state), 'pulse' (animated halo, to highlight a few "
      "important features), 'heatmap' (density weighted by value_field) and 'columns_3d' (3D "
      "bars whose height is value_field; tilts the map). Route layers use 'route', area "
      "layers use 'area'. Building layers (find_dangerous_buildings, get_buildings_3d) also "
      "take 'buildings_3d': the real footprints extruded at their DLS heights, with plot "
      "outlines; it tilts the map. Use fit to zoom the map to the layer. Show a layer "
      "whenever something has a location; the map is half of the answer.",
      obj({
          "layer_ref": {"type": "string"},
          "style": {"type": "string", "enum": POINT_STYLES + ["buildings_3d", "route", "area"]},
          "value_field": {"type": "string",
                          "description": "Numeric property for heatmap/columns_3d, e.g. "
                                         "level_pct, noise_db, caqi, power_w, people, total."},
          "title": {"type": "string", "description": "Legend title."},
          "fit": {"type": "boolean", "description": "Zoom to the layer (default true)."},
          "labels": {"type": "boolean", "description": "Show text labels (good for < 20 features)."},
      }, ["layer_ref", "style"]))
def h_show_on_map(ctx: ToolContext, a: dict) -> dict:
    ref = a["layer_ref"]
    layer = ctx.store.layers.get(ref)
    if layer is None:
        raise ToolError(f"unknown layer_ref {ref!r}; known: {list(ctx.store.layers)[-10:]}")
    meta = layer["meta"]
    style = a["style"]
    allowed = STYLES_FOR[meta["geometry"]]
    if style not in allowed:
        raise ToolError(f"layer {ref} is a {meta['geometry']} layer; use style {allowed}")
    vf = a.get("value_field")
    if style in ("heatmap", "columns_3d") and vf and meta["value_fields"] and vf not in meta["value_fields"]:
        raise ToolError(f"value_field must be one of {meta['value_fields']}")
    if style == "columns_3d" and not vf:
        raise ToolError(f"columns_3d needs value_field, one of {meta['value_fields']}")
    ctx.emit({"type": "ui", "action": "show_layer", "layer": {
        "ref": ref, "url": f"/api/layers/{ctx.conversation_id}/{ref}", "style": style,
        "value_field": vf, "title": a.get("title") or meta["title"],
        "fit": a.get("fit", True), "labels": a.get("labels", meta["count"] <= 20),
        "legend": meta["legend"], "count": meta["count"], "timeseries": bool(meta["frames"]),
        "sources": meta.get("sources", []),
    }})
    res = {"ok": True, "shown": ref, "features": meta["count"], "_summary": f"map: {ref}"}
    if meta["frames"]:
        res["timelapse"] = (f"{meta['frames']} frames, playing as a range on the city clock; "
                            "the lamps, the strip and the charts follow it")
    return res


@tool("map_fly_to",
      "Move the map camera: to a place name or coordinates, with zoom (11 city, 14 district, "
      "16 street, 18 building), pitch (0-70 degrees, tilt for 3D) and bearing. city_3d true "
      "swaps the base map's buildings for the DLS 3D city model (real heights for every "
      "building in Cyprus, coloured by height, dangerous-register buildings in red); it "
      "draws from zoom 15, so pair it with zoom 16-17 and pitch 55-65.",
      obj({
          "place": {"type": "string"},
          "lat": {"type": "number"},
          "lon": {"type": "number"},
          "zoom": {"type": "number", "minimum": 3, "maximum": 20},
          "pitch": {"type": "number", "minimum": 0, "maximum": 70},
          "bearing": {"type": "number", "minimum": -180, "maximum": 180},
          "city_3d": {"type": "boolean",
                      "description": "Turn the DLS 3D city model on (true) or off (false)."},
      }))
def h_map_fly_to(ctx: ToolContext, a: dict) -> dict:
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    if a.get("city_3d") is not None:
        ctx.emit({"type": "ui", "action": "city_3d", "on": a["city_3d"]})
    cmd = {"type": "ui", "action": "fly_to"}
    if center:
        cmd["center"] = [center[1], center[0]]
    for k in ("zoom", "pitch", "bearing"):
        if a.get(k) is not None:
            cmd[k] = a[k]
    if len(cmd) > 2:
        ctx.emit(cmd)
    res = {"ok": True, **({"center": center[2]} if center else {}), "_summary": "map: fly"}
    if a.get("city_3d") is not None:
        res["city_3d"] = "on" if a["city_3d"] else "off"
        if a["city_3d"]:
            res["note"] = "the 3D city draws from zoom 15; buildings stream in as the view moves"
    return res


@tool("map_callouts",
      "Pin short labelled callouts on the map (up to 12), e.g. to point out sights on a tour "
      "or explain what a highlight means.",
      obj({
          "items": {"type": "array", "minItems": 1, "maxItems": 12, "items": obj({
              "lat": {"type": "number"}, "lon": {"type": "number"},
              "title": {"type": "string"}, "text": {"type": "string"},
          }, ["lat", "lon", "title"])},
          "clear_previous": {"type": "boolean"},
      }, ["items"]))
def h_map_callouts(ctx: ToolContext, a: dict) -> dict:
    ctx.emit({"type": "ui", "action": "callouts", "items": a["items"],
              "clear_previous": a.get("clear_previous", True)})
    return {"ok": True, "callouts": len(a["items"]), "_summary": f"{len(a['items'])} callouts"}


@tool("map_clear",
      "Remove one layer (layer_ref) or, without arguments, every layer and callout.",
      obj({"layer_ref": {"type": "string"}}))
def h_map_clear(ctx: ToolContext, a: dict) -> dict:
    ctx.emit({"type": "ui", "action": "clear", "ref": a.get("layer_ref")})
    return {"ok": True, "_summary": "map: clear"}

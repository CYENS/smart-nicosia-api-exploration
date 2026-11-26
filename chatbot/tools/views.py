"""View tools: the high places with a view of the city, ranked by real height and sight, and a
view of Nicosia from any place at eye level, on a roof or higher up, by day or at night (the
page flies there and looks out in 3D). The work is in chatbot/views.py."""

from __future__ import annotations

from chatbot import views as V
from chatbot.store import STATE_COLORS, point
from chatbot.terrain import Terrain
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj, resolve_point
from chatbot.web import WebError

TOOLS = ToolSet()
tool = TOOLS.tool

KIND_LABEL = {"viewpoint": "viewpoint", "lookout": "public lookout", "high_ground": "high ground"}


def terrain_of(ctx: ToolContext) -> Terrain:
    if ctx.terrain is None:
        ctx.terrain = Terrain(ctx.settings)
    return ctx.terrain


@tool("find_high_places",
      "High places with a view of the city, ranked by how good the view is: OpenStreetMap "
      "viewpoints, public lookouts in tall buildings (observatories, rooftop bars and "
      "terraces, measured with the DLS 3D model; Shacolas Tower's observatory on Ledras Street "
      "is one) and the high ground of the terrain itself. Each comes with a plain name (what "
      "and in which area: never only a raw map label), its height above sea level and above "
      "the city centre, how far it stands above its surroundings, the distance and direction "
      "to the centre, and whether the centre is in line of sight over the terrain (or what "
      "hides it). Ranked by height above the centre and the surroundings, the centre in sight, "
      "and distance. Returns a layer_ref (a point per place, value fields above_centre_m, "
      "elevation_m and score for 'columns_3d') and a data_ref, linked by id. Pass an id to "
      "view_from to show the view from there.",
      obj({
          "near_place": {"type": "string",
                         "description": "Search around this place (default the city centre), "
                                        "or 'my location'."},
          "near_lat": {"type": "number"},
          "near_lon": {"type": "number"},
          "radius_m": {"type": "number", "minimum": 500, "maximum": 15000,
                       "description": "How far to look, in metres (default 6000)."},
          "limit": {"type": "integer", "minimum": 1, "maximum": 12,
                    "description": "How many to return (default 6)."},
      }))
def h_find_high_places(ctx: ToolContext, a: dict) -> dict:
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    near = (center[0], center[1]) if center else V.CENTRE
    radius = a.get("radius_m") or 6000
    limit = a.get("limit") or 6
    try:
        rows = V.find_high(ctx, terrain_of(ctx), near, radius, limit)
    except WebError as e:
        raise ToolError(f"the terrain tiles could not be read: {e}. Say so; don't guess heights") from None
    if not rows:
        raise ToolError(f"no high places within {radius / 1000:.1f} km; try a larger radius_m")
    feats = []
    for r in rows:
        state = KIND_LABEL[r["kind"]]
        lines = [r["name"], r["how_high"], r["where"], r["view"][0].upper() + r["view"][1:]]
        if r.get("mapped_as"):
            lines.append(f"Mapped as \"{r['mapped_as']}\"")
        if r.get("opening_hours"):
            lines.append(f"Open {r['opening_hours']}")
        feats.append(point(r["lon"], r["lat"], {
            "id": r["id"], "label": f"{r['rank']}. {r['name']}", "state": state,
            "color": STATE_COLORS[state], "elevation_m": r["elevation_m"],
            "above_centre_m": r["above_centre_m"], "score": r["score"],
            "popup": "\n".join(lines)}))
    states = list(dict.fromkeys(f["properties"]["state"] for f in feats))
    title = "High places with a view"
    layer = ctx.store.add_layer("high_places", feats, geometry="point", title=title,
                                legend={s: STATE_COLORS[s] for s in states},
                                value_fields=["above_centre_m", "elevation_m", "score"])
    data = ctx.store.add_dataset("high_places", rows, title=title, link={"key": "id"})
    ctx.store.datasets[data]["high_places"] = True  # view_from finds them by id
    best = rows[0]
    return {
        "places": rows, "layer_ref": layer, "data_ref": data,
        "centre_ground_m": round(terrain_of(ctx).elevation(*V.CENTRE)),
        "note": ("heights are metres above sea level from the terrain tiles (about 30 m "
                 "detail) plus DLS building heights; Nicosia is fairly flat, so tens of metres "
                 "above the centre is a real view. High ground is from the terrain alone: say "
                 "the data doesn't tell whether it is open to the public."),
        "_summary": f"{len(rows)} high places, best: {best['name']}",
    }


HEIGHT = {"anyOf": [{"type": "string", "enum": ["eye", "roof"]},
                    {"type": "number", "minimum": 0, "maximum": 300}],
          "description": "'eye' (eye level, 1.7 m: the default, except a building from "
                         "find_high_places, which defaults to its top), 'roof' (on the roof of "
                         "the DLS building at that spot) or metres above the ground (0 to 300)."}


@tool("view_from",
      "Fly the user to a place and show Nicosia from there at eye level in 3D, as they would "
      "see it: the page lowers the camera to the eye, with the real terrain, the buildings, the "
      "sky and, at night, the city's lamps as they were. `from` is any place: a find_high_places "
      "id, an OpenStreetMap place (n219329628), a web place (web-1a2b3c4d), a DLS building "
      "(B255563), any entity with a location, a name, or 'my location'. `toward` is a place, a "
      "compass direction ('north-east'), or the city centre by default. `when` is 'now' "
      "(default), a past local moment ('2026-11-21T21:00') or a night ('at night', 'last "
      "night 22:00'): the clock shows only the past, so at night in daylight it is last "
      "night's, and says so. Reads the ground's height at both ends and checks the line of "
      "sight over the terrain and the DLS roofs near the eye. Returns facts in words: how "
      "high the eye is (above sea level and the centre), the distance and direction, whether "
      "the target is in sight or what hides it (a ridge, buildings), the landmarks in sight "
      "(the centre, Shacolas Tower, tall buildings, the Kyrenia mountains) and the moment. "
      "The page shows a card with look left and right, look around, the next view (the other "
      "places of the same find_high_places answer) and back to the map.",
      obj({
          "from": {"type": "string", "description": "Where to stand."},
          "toward": {"type": "string",
                     "description": "What to look at: a place or id, a compass direction, or "
                                    "'the city centre' (default)."},
          "height": HEIGHT,
          "when": {"type": "string",
                   "description": "'now' (default), a past local moment, or a night: 'at "
                                  "night', 'last night 22:00'."},
          "look_around": {"type": "boolean",
                          "description": "Turn slowly all the way round once there."},
      }, ["from"]))
def h_view_from(ctx: ToolContext, a: dict) -> dict:
    terrain = terrain_of(ctx)
    try:
        hit = V.known_high_place(ctx, a["from"])
        frm, tour_rows = hit if hit else (V.resolve_spot(ctx, a["from"]), [])
        toward, bearing = None, None
        if a.get("toward"):
            bearing = V.direction_of(a["toward"])
            if bearing is None:
                toward = V.resolve_spot(ctx, a["toward"])
        when = V.resolve_when(ctx, a.get("when"))
        v = V.view(ctx, terrain, frm, toward=toward, bearing=bearing, height=a.get("height"),
                   when=when)
    except V.ViewError as e:
        raise ToolError(str(e)) from None
    except WebError as e:
        raise ToolError(f"the terrain tiles could not be read: {e}. Say so; don't guess") from None
    cmd = V.camera(v, when, look_around=bool(a.get("look_around")))
    # The other places of the same answer, for the card's Next view: their cameras, without
    # their landmarks (those come with the view when the page moves on to it).
    tour = []
    if tour_rows and not a.get("toward") and a.get("height") is None:
        for row in tour_rows:
            if row["id"] == frm.id:
                tour.append(cmd)
                continue
            try:
                other = V.view(ctx, terrain, V.spot_of_row(row), when=when, with_landmarks=False)
            except (V.ViewError, WebError):
                continue
            tour.append(V.camera(other, when) | {"rank": row["rank"]})
    cmd["tour"] = [{k: c[k] for k in ("id", "name", "lat", "lon", "ground_m", "eye_m", "height_m",
                                      "above_centre_m", "bearing", "building_id", "toward") if k in c}
                   for c in tour]
    ctx.emit({"type": "ui", "action": "view", "view": cmd})
    out = {k: v[k] for k in ("from", "looking", "toward", "facts", "when", "dark") if k in v}
    out["landmarks"] = [{k: m[k] for k in ("title", "distance_m", "visible", "hidden_by") if k in m}
                        for m in v.get("landmarks", [])]
    if when.at is not None:
        out["clock"] = f"the page's clock moves to {cmd['at']} (the lamps as they were then)"
    out["page"] = ("the page is flying there and lowering the camera to the eye; the card lets "
                   "the user look around, go to the next view and go back to the map (Esc)")
    if len(cmd["tour"]) > 1:
        out["next_views"] = len(cmd["tour"]) - 1
    out["_summary"] = f"view from {frm.name}"
    return out

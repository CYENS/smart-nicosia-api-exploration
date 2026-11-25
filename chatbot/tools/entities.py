"""Entity tools: anything in the twin by its id, and opening it in the page's inspector."""

from __future__ import annotations

from chatbot.entities import Entities, EntityNotFound, for_model, layer_features, series_rows
from chatbot.store import legend_for
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj

TOOLS = ToolSet()
tool = TOOLS.tool

ID = {"type": "string",
      "description": "The thing's id as the tools give it: a device (YL1015 street light, YP974 "
                     "parking bay, YW1394 bin, an air station's or camera's name), a register "
                     "building (NIC-409, LIM-12, LAR-120, FAM-15B-3), a DLS 3D building (B255563) "
                     "or an OpenStreetMap place (n219329628)."}


def entities_of(ctx: ToolContext) -> Entities:
    if ctx.entities is None:
        ctx.entities = Entities(ctx.city, ctx.places, ctx.dls, ctx.registers, ctx.memory)
    return ctx.entities


def lookup(ctx: ToolContext, eid: str) -> dict:
    try:
        return entities_of(ctx).get(eid)
    except EntityNotFound as e:
        raise ToolError(str(e)) from None


@tool("get_entity",
      "Everything the twin knows about one thing, of any kind, by its id: a street light, "
      "parking bay, bin, air station, camera, info screen, dangerous-register building, DLS 3D "
      "building or OpenStreetMap place. Returns its interpreted state with the reading's age, "
      "its readings, attributes, relations (its street, the nearest device of each other kind "
      "with distances, the register entry on a building, the 3D buildings on a register plot, "
      "places inside a building) with their ids, and its sources. For a lamp, parking bay, bin "
      "or air station it also returns the last 24 hours (series_24h: min, max, last, and a "
      "data_ref with one row per 15 minutes: time, the metric, state) and for a counting "
      "camera people per hour. The layer_ref holds the thing and its related things "
      "('pulse' for one thing, 'buildings_3d' for a building). Covers everything get_device "
      "returns.",
      obj({"id": ID,
           "history": {"type": "boolean",
                       "description": "Include the last 24 hours where there are any (default "
                                      "true; one request to the city's API, cached)."}},
          ["id"]))
def h_get_entity(ctx: ToolContext, a: dict) -> dict:
    entity = lookup(ctx, a["id"])
    out = for_model(entity)
    feats = layer_features(entity)
    if feats:
        states = [f["properties"]["state"] for f in feats]
        out["layer_ref"] = ctx.store.add_layer(
            f"entity_{entity['id']}", feats,
            geometry="buildings" if entity.get("footprints") else "point",
            title=entity["name"], legend=legend_for(states),
            value_fields=[r["key"] for r in entity["readings"] if isinstance(r["value"], (int, float))],
            count=1 + sum(1 for r in entity["relations"] if r.get("id") and r.get("lon") is not None))
    if a.get("history", True) and entity.get("has_series"):
        s = entities_of(ctx).series(entity["id"])
        if s:
            out["series_24h"] = {"what": s["label"], "metric": s["metric"], "unit": s["unit"],
                                 "from": s["from"], "to": s["to"], "points": len(s["points"]),
                                 "step_min": s["step_min"], "summary": s.get("summary")}
            for k in ("threshold", "totals_24h"):
                if s.get(k):
                    out["series_24h"][k] = s[k]
            out["data_ref"] = ctx.store.add_dataset(
                f"{entity['id']}_24h", series_rows(entity, s),
                title=f"{entity['name']}: {s['label'].lower()}, last 24 hours", link={"key": "id"})
        else:
            out["series_24h"] = "no history from the city's API for the last 24 hours"
    out["_summary"] = f"{entity['kind_label']}: {entity['name']}"
    return out


@tool("open_inspector",
      "Open one thing in the inspector beside the map: its name, status, live readings with "
      "their age, a 24-hour sparkline, attributes, relations and actions. Use it when the user "
      "asks to see, open or inspect something, or after answering about one particular thing. "
      "fly_to (default true) also moves the map to it.",
      obj({"id": ID, "fly_to": {"type": "boolean"}}, ["id"]))
def h_open_inspector(ctx: ToolContext, a: dict) -> dict:
    entity = lookup(ctx, a["id"])
    ctx.emit({"type": "ui", "action": "inspect", "id": entity["id"], "at": entity.get("center"),
              "fly": a.get("fly_to", True)})
    return {"ok": True, "opened": entity["id"], "name": entity["name"], "kind": entity["kind"],
            "_summary": f"inspector: {entity['id']}"}

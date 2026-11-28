"""Hazard tools: earthquakes, official weather warnings and fire danger around Nicosia, as the
twin read and recorded them from the official sources (chatbot/hazards), with the incidents the
watch raised for them."""

from __future__ import annotations

from datetime import timedelta

from chatbot.hazards import feeds as F
from chatbot.hazards import layer as L
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj
from chatbot.tools.entities import moment_of

TOOLS = ToolSet()
tool = TOOLS.tool
KINDS = ("all", "earthquakes", "warnings", "fire")


def hazards_of(ctx: ToolContext):
    hz = getattr(ctx.watcher, "hazards", None)
    if hz is None:
        raise ToolError("the twin's hazard feeds aren't running in this deployment")
    return hz


def quake_row(q: dict, incidents: dict[str, str]) -> dict:
    felt = q.get("felt") or {}
    sev = L.quake_severity(q)
    return {"id": q["id"], "time": q["start"], "magnitude": q.get("value"), "magnitude_type": q.get("magtype"),
            "depth_km": q.get("depth_km"), "distance_km": q.get("distance_km"), "direction": q.get("direction"),
            "where": q.get("where"), "located_by": q.get("located_by"),
            "felt_reports": felt.get("count"), "felt_near_nicosia": felt.get("near_nicosia"),
            "felt_near_described": (f"{F.INTENSITY_WORDS.get(felt['near_intensity'])} (intensity "
                                    f"{F.ROMAN.get(felt['near_intensity'])}, the median report)"
                                    if felt.get("near_intensity") else None),
            "raises_incident": sev != "minor", "severity_here": sev,
            "incident": incidents.get(q["id"]), "lat": q.get("lat"), "lon": q.get("lon")}


def warning_row(w: dict, incidents: dict[str, str]) -> dict:
    return {"id": w["id"], "title": w["title"], "level": w.get("level"), "kind": w.get("awareness"),
            "area": w.get("area"), "covers_nicosia": w.get("covers_nicosia"), "from": w["start"],
            "until": w.get("end"), "in_force": w.get("in_force"), "issued": w.get("issued"),
            "headline": w.get("headline"), "description": w.get("description"),
            "instruction": w.get("instruction"), "withdrawn_at": w.get("withdrawn_at"),
            "incident": incidents.get(w["id"])}


def fire_row(f: dict) -> dict:
    shares = f.get("shares") or {}
    level = f.get("level") or "low"
    return {"id": f["id"], "day": f.get("day"), "level": F.FIRE_WORDS.get(level, level),
            "level_index": F.FIRE_CLASSES.index(level) if level in F.FIRE_CLASSES else None,
            "fire_weather_index": f.get("fwi"), "forecast": bool(f.get("forecast")),
            "very_high_or_worse_pct": round(100 * sum(v for k, v in shares.items()
                                                      if F.FIRE_CLASSES.index(k) >= 3)),
            "at_nicosia_centre": F.FIRE_WORDS.get(f.get("city_level") or "", None)}


@tool("get_hazards",
      "Natural hazards around Nicosia from the official sources, as the twin read and recorded "
      "them (they replay with the city clock): earthquakes (EMSC, carrying the Cyprus Geological "
      "Survey Department's own solutions, with how many people reported feeling each one to EMSC "
      "and how many of them within 25 km of Nicosia, and how they described it), the Department "
      "of Meteorology's weather warnings for Cyprus through Meteoalarm (yellow, orange or red; "
      "the kind; the hours; the warning's own words, issued for the island as one area, so "
      "Nicosia included), and fire danger in the Nicosia district from EFFIS's fire weather "
      "index (today, past days and the next two days' forecast). kind picks one (default all). "
      "Earthquakes: the last `days` (default 7, up to 62) at or above min_magnitude; "
      "notable_only keeps those near and strong enough that the twin's watch raises an incident "
      "(M3 within 100 km, M4.5 within 250 km, M2.5 within 20 km, or felt near Nicosia). `at` "
      "reads a past moment of the city clock. The layer_ref draws quakes as rings sized by "
      "magnitude, coloured by how strongly they reached Nicosia, their felt reports as cells, a "
      "warning's area and the district's fire danger over the island (show it with 'points'; it "
      "draws its own way). The data_ref has one row per earthquake (id, time, magnitude, "
      "depth_km, distance_km, direction, where, felt_reports, felt_near_nicosia, "
      "raises_incident, severity_here), linked to the map by id, for a chart by magnitude over "
      "time; with kind 'warnings' one row per warning, with kind 'fire' one row per day (day, "
      "level, level_index 0 to 5, very_high_or_worse_pct, forecast). `incident` names the "
      "watch's incident for a hazard (INC-12).",
      obj({"kind": {"type": "string", "enum": list(KINDS)},
           "days": {"type": "integer", "minimum": 1, "maximum": 62,
                    "description": "Earthquakes of the last this many days (default 7)."},
           "min_magnitude": {"type": "number", "minimum": 0, "maximum": 9},
           "notable_only": {"type": "boolean",
                            "description": "Only the quakes the watch would raise (near and strong enough)."},
           "at": {"type": "string", "description": "A past local moment, ISO (2026-11-26T21:00): "
                                                   "the hazards as the twin knew them then."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                     "description": "Earthquakes to list, strongest first (default 15; the layer and data have all)."}}))
def h_get_hazards(ctx: ToolContext, a: dict) -> dict:
    hz = hazards_of(ctx)
    kind = a.get("kind") or "all"
    at = moment_of(ctx, a.get("at"))
    days = a.get("days") or 7
    m = hz.moment(at, days=days, read_fire=kind in ("all", "fire"))
    incidents = L.incident_ids(ctx.watcher)
    quakes = [q for q in m["earthquakes"] if (q.get("value") or 0) >= (a.get("min_magnitude") or 0)]
    if a.get("notable_only"):
        quakes = [q for q in quakes if L.quake_severity(q) != "minor"]
    m["earthquakes"] = quakes if kind in ("all", "earthquakes") else []
    if kind not in ("all", "warnings"):
        m["warnings"] = []
    if kind not in ("all", "fire"):
        m["fire"], m["fire_ahead"] = None, []
    when = (at or hz.now()).astimezone(F.TZ)
    res: dict = {"at": m["at"], "live": m["live"], "feeds": m["status"]}
    q_rows = [quake_row(q, incidents) for q in quakes]
    if kind in ("all", "earthquakes"):
        strongest = sorted(q_rows, key=lambda r: -(r["magnitude"] or 0))
        res["earthquakes"] = {
            "days": days, "count": len(q_rows),
            "raising_incidents": sum(1 for r in q_rows if r["raises_incident"]),
            "strongest_first": strongest[:a.get("limit") or 15]}
    if kind in ("all", "warnings"):
        res["warnings"] = [warning_row(w, incidents) for w in m["warnings"]]
        if not res["warnings"]:
            res["warnings_note"] = ("no weather warning for Cyprus was issued and still running at "
                                    f"{when:%a %d %b %H:%M} (Meteoalarm, Department of Meteorology)")
    fire_rows: list[dict] = []
    if kind in ("all", "fire"):
        today = when.date()
        past = [hz.fire_day(today - timedelta(days=k)) for k in range(6 if kind == "fire" else 0, 0, -1)]
        days_ = [h.public() for h in past if h is not None] + ([m["fire"]] if m["fire"] else []) + m.get("fire_ahead", [])
        fire_rows = [fire_row(f) for f in days_]
        res["fire_danger"] = fire_rows or None
        if not fire_rows:
            res["fire_note"] = "EFFIS's fire danger for the Nicosia district isn't read yet or has no data for that day"
    feats = L.features(m, incidents=incidents)
    title_kind = {"all": "Hazards", "earthquakes": f"Earthquakes, last {days} days",
                  "warnings": "Weather warnings", "fire": "Fire danger"}[kind]
    title = f"{title_kind}, {when:%a %d %b %H:%M}" if at else title_kind
    if feats or m.get("fire"):
        ref = ctx.store.add_layer("hazards", feats, geometry="point", title=title,
                                  legend=L.legend(feats, m.get("fire")),
                                  count=sum(1 for f in feats if f["properties"].get("kind") != "felt"))
        ctx.store.layers[ref]["meta"]["hazards"] = L.meta(m)
        res["layer_ref"] = ref
    if kind == "warnings":
        rows = res["warnings"]
        if rows:
            res["data_ref"] = ctx.store.add_dataset("warnings", rows, title=title, link={"key": "id"})
    elif kind == "fire":
        if fire_rows:
            res["data_ref"] = ctx.store.add_dataset("fire_danger", fire_rows, title="Fire danger in the Nicosia district")
    elif q_rows:
        res["data_ref"] = ctx.store.add_dataset("earthquakes", q_rows, title=f"Earthquakes around Cyprus, last {days} days",
                                                link={"key": "id"})
    res["note"] = ("Report only what the sources say: EMSC and the Geological Survey Department for quakes, the "
                   "Department of Meteorology's warning text for warnings, EFFIS's index for fire danger. "
                   "Felt reports are what people told EMSC, not a measurement.")
    parts = []
    if kind in ("all", "earthquakes"):
        parts.append(f"{len(q_rows)} quakes")
    if kind in ("all", "warnings"):
        parts.append(f"{len(res['warnings'])} warnings")
    if kind in ("all", "fire") and m.get("fire"):
        parts.append(f"fire danger {F.FIRE_WORDS.get(m['fire'].get('level'), '?').lower()}")
    res["_summary"] = ", ".join(parts) or "nothing"
    return res

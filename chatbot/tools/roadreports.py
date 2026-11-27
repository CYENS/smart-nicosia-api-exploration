"""Road reports: what is happening on Nicosia's roads besides speed (closures, jams, accidents,
hazards, roadworks and diversions), now or at a moment the twin recorded. They read
chatbot/mobility/reports.py, which keeps one fetch of each feed for every page and answer."""

from __future__ import annotations

import logging
from datetime import timedelta

from chatbot.geo import haversine_m
from chatbot.mobility.gtfs import TZ
from chatbot.mobility.reports import (KIND_COLORS, KINDS, LEGEND, SOURCE_NAMES, RoadReport, age_words,
                                      count_words, counts, distance_to_line, features, local_text, sort_key,
                                      when_text)
from chatbot.moments import OutOfRange, parse_at
from chatbot.tools.base import (NEAR, LocationNotShared, ToolContext, ToolError, ToolSet, my_location, obj,
                                resolve_point)
from chatbot.web import WebError

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

CENTRE = (35.1700, 33.3610)  # Eleftheria Square: the walled city and the shops around Makariou
CENTRE_M = 1600.0
ROUTE_M = 150.0  # a report this close to the drive is on the way
PLANNED_DAYS = 30
TEXT_MAX = 240
ROUTE_COLOR = "#8798b0"


def _mobility(ctx: ToolContext):
    if ctx.mobility is None or getattr(ctx.mobility, "reports", None) is None:
        raise ToolError("road reports aren't available in this deployment")
    return ctx.mobility


def _pts(r: RoadReport) -> list[tuple[float, float]]:
    return r.line or r.points or [(r.lon, r.lat)]


def _drive(ctx: ToolContext, a: dict) -> tuple[list[list[float]], str, float] | None:
    """The drive the question is about, from Valhalla: (line, words, km), or None."""
    if not (a.get("to_place") or a.get("to_lat") is not None):
        return None
    if a.get("from_place") or a.get("from_lat") is not None:
        origin = resolve_point(ctx, a.get("from_place"), a.get("from_lat"), a.get("from_lon"))
    elif ctx.user_location:
        loc = my_location(ctx)
        origin = (loc["lat"], loc["lon"], loc["name"])
    else:
        raise LocationNotShared("give from_place for the drive: the user hasn't shared a location")
    dest = resolve_point(ctx, a.get("to_place"), a.get("to_lat"), a.get("to_lon"))
    if origin is None or dest is None:
        raise ToolError("give both ends of the drive: from_place and to_place")
    try:
        r = ctx.router.route([(origin[0], origin[1]), (dest[0], dest[1])], "drive", [])
        line = r["coordinates"]
        km = r.get("distance_km") or 0.0
    except (WebError, KeyError) as e:
        log.info("no drive for road reports: %s", e)
        line = [[origin[1], origin[0]], [dest[1], dest[0]]]
        km = haversine_m(origin[0], origin[1], dest[0], dest[1]) / 1000
    return line, f"{origin[2]} to {dest[2]}", km


def _row(r: RoadReport, now) -> dict:
    status = r.status(now)
    when = when_text(r, status, now) if r.source == "traffic_events" else age_words(r.since, now)
    out = {"id": r.id, "kind": r.kind, "label": r.label, "street": r.street,
           "since": local_text(r.since), "age": when, "source": SOURCE_NAMES.get(r.source, r.source)}
    if r.until:
        out["until"] = local_text(r.until)
    if status != "active":
        out["status"] = status
    if r.text and r.source == "traffic_events":
        out["text"] = r.text if len(r.text) <= TEXT_MAX else r.text[:TEXT_MAX - 1] + "…"
        if r.details.get("text_en") and r.details["text_en"] != r.text:
            out["text_en"] = r.details["text_en"][:TEXT_MAX]
    for k in ("points", "length_m", "level", "delay_s", "speed_kmh", "affectedLanes"):
        if r.details.get(k) is not None:
            out[k] = r.details[k]
    return out


@tool("get_road_reports",
      "What is happening on Nicosia's roads besides speed, from the Cyprus National Access "
      "Point: closures, jams, accidents and hazards (potholes, a stopped car, something on the "
      "road) that drivers report in Waze, and roadworks and diversions the Public Works "
      "Department enters with their planned dates. Call it for 'what is closed in the centre', "
      "'any roadworks on the way to the general hospital', 'where are jams reported right now', "
      "'is anything blocking Makariou', 'why is traffic slow there', or at a past moment the "
      "twin recorded (at). Narrow it by kinds, a street, an area ('centre'), near a place, or "
      "along a drive (to_place, from the user's location or from_place: reports within 150 m "
      "of the driving route). Returns counts by kind, the reports loudest and newest first (id, "
      "kind, label, street, since, age, until for works, source, the operators' own words), "
      "works planned to start soon, a layer_ref that marks each kind with its own shape and "
      "colour (closed stretches and the drive drawn as lines; show it with style 'points') and "
      "a data_ref of one row per report linked to the map by id. Many closures were reported "
      "weeks or months ago: they are long works, not news, so say their age. Report ids "
      "(report-waze-1a2b3c4d, report-works-639274141179895317) open in the inspector.",
      obj({"kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)},
                     "description": "Only these kinds (default all)."},
           "street": {"type": "string", "description": "A street name, e.g. 'Makariou', 'Strovolos'."},
           "area": {"type": "string", "enum": ["centre", "city"],
                    "description": "'centre': the walled city and the commercial centre (1.6 km "
                                   "around Eleftheria Square); 'city': Greater Nicosia (default)."},
           **NEAR,
           "to_place": {"type": "string", "description": "Where a drive goes: reports on the way there."},
           "to_lat": {"type": "number"}, "to_lon": {"type": "number"},
           "from_place": {"type": "string", "description": "Where the drive starts (default the user's location)."},
           "from_lat": {"type": "number"}, "from_lon": {"type": "number"},
           "recent_minutes": {"type": "integer", "minimum": 5, "maximum": 1440,
                              "description": "Only reports made in the last this many minutes (jams now: 60)."},
           "at": {"type": "string", "description": "A past local moment, ISO (2026-11-26T08:30): the "
                                                    "reports as the twin's memory recorded them."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 60,
                     "description": "Reports to list (default 15; the layer has them all)."}}))
def h_get_road_reports(ctx: ToolContext, a: dict) -> dict:
    m = _mobility(ctx)
    now = ctx.city.now()
    try:
        at = parse_at(a.get("at"), now)
    except OutOfRange as e:
        raise ToolError(str(e)) from None
    got = m.reports.at(at) if at is not None else m.reports.current()
    if got["status"] == "unknown":
        raise ToolError(f"the twin's memory holds no road reports for {at.astimezone(TZ):%a %d %b %H:%M}: it "
                        "records them only while a page or a tool reads them. Say so plainly")
    if got["status"] == "unavailable":
        why = got.get("error") or "; ".join(f"{s}: {f.get('error')}" for s, f in got["feeds"].items() if f.get("error"))
        raise ToolError(f"the road report feeds aren't answering ({why}). Say so plainly; get_road_speeds "
                        "may still answer")
    moment = at or now
    reports: list[RoadReport] = got["reports"]
    where = "Greater Nicosia"
    if a.get("kinds"):
        reports = [r for r in reports if r.kind in set(a["kinds"])]
    if a.get("street"):
        q = a["street"].strip().lower()
        reports = [r for r in reports if q in (r.street or "").lower() or q in (r.text or "").lower()
                   or q in (r.details.get("text_en") or "").lower()]
        where = f"on {a['street']}"
    if a.get("area") == "centre":
        reports = [r for r in reports if min(haversine_m(CENTRE[0], CENTRE[1], y, x) for x, y in _pts(r)) <= CENTRE_M]
        where = "in the centre (1.6 km around Eleftheria Square)"
    near = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if near:
        radius = float(a.get("radius_m") or 800)
        reports = [r for r in reports if min(haversine_m(near[0], near[1], y, x) for x, y in _pts(r)) <= radius]
        where = f"within {round(radius)} m of {near[2]}"
    drive = _drive(ctx, a)
    if drive:
        line = [tuple(c) for c in drive[0]]
        reports = [r for r in reports if min(distance_to_line(p, line) for p in _pts(r)) <= ROUTE_M]
        where = f"within {round(ROUTE_M)} m of the drive from {drive[1]} ({drive[2]:.1f} km)"
    if a.get("recent_minutes"):
        since = moment - timedelta(minutes=a["recent_minutes"])
        reports = [r for r in reports if r.since and r.since >= since and r.source != "traffic_events"]
    active = [r for r in reports if r.status(moment) == "active"]
    planned = sorted((r for r in reports if r.status(moment) == "planned" and r.since
                      and r.since - moment <= timedelta(days=PLANNED_DAYS)), key=lambda r: r.since)
    active.sort(key=lambda r: sort_key(r, moment))
    by = counts(active)
    feats = features(active + planned, moment)
    if drive:
        feats.insert(0, {"type": "Feature", "geometry": {"type": "LineString", "coordinates": drive[0]},
                         "properties": {"id": "drive", "part": "route", "color": ROUTE_COLOR,
                                        "label": f"Drive: {drive[1]}"}})
    when = moment.astimezone(TZ).strftime("%a %d %b %H:%M") if at else moment.astimezone(TZ).strftime("%H:%M")
    title = f"Road reports {where}, {when}"
    legend = {KINDS[k][0]: KIND_COLORS[k] for k in KINDS if by.get(k)} or LEGEND
    ref = ctx.store.add_layer("road_reports", feats, geometry="point", title=title, legend=legend,
                              count=len(active) + len(planned))
    ctx.store.layers[ref]["meta"]["live"] = {"kind": "road_reports", "at": at.isoformat() if at else None}
    rows = [{"id": r.id, "kind": r.kind_label, "label": r.label, "street": r.street,
             "since": local_text(r.since), "age_days": round((moment - r.since).total_seconds() / 86400, 1) if r.since else None,
             "until": local_text(r.until), "source": SOURCE_NAMES.get(r.source, r.source)} for r in active]
    data = ctx.store.add_dataset("road_reports", rows, title=title, link={"key": "id"})
    recent = [r for r in active if r.since and timedelta(0) <= moment - r.since <= timedelta(minutes=30)
              and r.source != "traffic_events"]
    old = [r for r in active if r.since and moment - r.since > timedelta(days=7)]
    limit = a.get("limit") or 15
    res = {
        "as_of": when, "source": "live" if at is None else "the twin's memory", "where": where,
        "counts": by, "in_words": count_words(by),
        "reported_last_30_min": count_words(counts(recent)) if recent else "nothing",
        "older_than_a_week": len(old),
        "reports": [_row(r, moment) for r in active[:limit]],
        "layer_ref": ref, "data_ref": data,
        "feeds": {s: {k: v for k, v in f.items() if k in ("state", "error", "note", "count") and v is not None}
                  for s, f in got["feeds"].items()},
        "notes": "Waze reports are what drivers report, not official; the Public Works Department's are "
                 "planned works with dates. A closure reported weeks ago is a long closure, not news.",
    }
    if planned:
        res["planned"] = [_row(r, moment) for r in planned[:8]]
    if len(active) > limit:
        res["more"] = f"{len(active) - limit} more on the layer"
    if any(f.get("state") == "stale" for f in got["feeds"].values()):
        res["feed_note"] = "a feed failed just now; its reports are from its last good read"
    if got["feeds"].get("waze_traffic", {}).get("state") == "unavailable":
        res["jam_lines"] = "Waze's jam lines aren't answering, so jams are the ones drivers reported"
    res["_summary"] = f"{count_words(by)} {where}" if by else f"no road reports {where}"
    return res

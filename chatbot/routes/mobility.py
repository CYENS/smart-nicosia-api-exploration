"""Live mobility for the page: the buses on the road, the stops and route shapes they glide
along, road speeds now or at a moment (`?at=`, for the city clock), and the Mobility
workspace's summary. Every page shares one fetch of each feed (chatbot/mobility)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot import context
from chatbot.mobility.gtfs import simplify
from chatbot.mobility.roads import LEGEND, STALLED_LEGEND, parse_moment, segment_feature
from chatbot.overview import cached
from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment
from chatbot.sky.bus_archive import buses_at

router = APIRouter()

BUS_FIELDS = ("id", "line", "line_id", "color", "text_color", "headsign", "lon", "lat", "heading",
              "speed_kmh", "reported", "along_m", "shape_id", "delay_s", "delay_source", "finished",
              "waiting")


@router.get("/api/buses")
def buses(request: Request, lines: str | None = Query(None, max_length=400),
          at: str | None = AT) -> JSONResponse:
    """The buses on the road now (live every 30 s) or recorded in `bus_positions` at `?at=`."""
    agent = agent_of(request)
    m = agent.mobility
    when = moment(request, at)
    wanted = {x.strip().lower() for x in lines.split(",") if x.strip()} if lines else None
    if when is not None:
        tt = m.timetable(0)
        body = buses_at(agent.memory, when, lines=wanted, timetable_state="ready" if tt else "loading")
        return JSONResponse(body, headers={"Cache-Control": "max-age=300"})
    tt = m.timetable(20)
    snap = m.snapshot()
    rows = [{k: b.get(k) for k in BUS_FIELDS if b.get(k) is not None}
            for b in snap.vehicles if not wanted or (b.get("line_id") or "") in wanted]
    body = {"status": snap.status, "error": snap.error, "count": len(rows),
            "fetched_at": int(snap.fetched_at * 1000), "feed_time": int(snap.feed_time * 1000) if snap.feed_time else None,
            "timetable": "ready" if tt else "loading", "buses": rows}
    return JSONResponse(body, headers={"Cache-Control": "max-age=10"})


@router.get("/api/transit/shapes/{shape_id}")
def shape(shape_id: str, request: Request) -> JSONResponse:
    """A route's line, simplified, for gliding buses along it (cached by the browser a day)."""
    tt = agent_of(request).mobility.timetable(20)
    s = tt.shapes.get(shape_id) if tt else None
    if s is None:
        raise HTTPException(404, f"no route shape {shape_id!r}")
    coords = [[round(x, 6), round(y, 6)] for x, y in simplify(s.coords, 1.5)]
    return JSONResponse({"id": shape_id, "coordinates": coords},
                        headers={"Cache-Control": "max-age=86400"})


@router.get("/api/transit/stops")
def stops(request: Request) -> JSONResponse:
    """Every served stop in Greater Nicosia, as GeoJSON (cached an hour)."""
    m = agent_of(request).mobility

    def build():
        tt = m.timetable(20)
        if tt is None:
            return None
        feats = []
        for s in tt.stops:
            if not (33.0 <= s.lon <= 33.75 and 34.95 <= s.lat <= 35.3):
                continue
            lines = tt.lines_at(s)
            if not lines:
                continue
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]},
                          "properties": {"id": f"stop-{s.id}", "name": s.name, "code": s.code,
                                         "lines": " ".join(ln.short for ln in lines[:10])}})
        return {"type": "FeatureCollection", "features": feats}

    body = cached("transit_stops", 3600, build, keep=lambda v: v is not None)
    if body is None:
        raise HTTPException(503, "the bus timetable is still loading; try again in a minute")
    return JSONResponse(body, headers={"Cache-Control": "max-age=3600"})


@router.get("/api/roads")
def roads(request: Request, at: str | None = Query(None, max_length=40)) -> JSONResponse:
    """Road segments coloured by speed against free flow, now or at a moment the twin's memory
    recorded (`at`: ISO local time or epoch ms, for the city clock)."""
    agent = agent_of(request)
    m = agent.mobility
    try:
        moment = parse_moment(at, agent.city.now())
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    try:
        segs = {s.id: s for s in m.roads.segments()}
    except Exception as e:  # noqa: BLE001 - the inventory never came
        raise HTTPException(502, f"the road inventory couldn't be read: {e}") from None
    out = m.roads.speeds(moment)
    feats = [segment_feature(segs[r["id"]], r) for r in out["segments"] if r["id"] in segs]
    stalled = out["status"] == "stalled"  # the detectors' last time, and roads at their usual
    body = {"type": "FeatureCollection", "features": feats,
            "meta": {"status": out["status"], "error": out.get("error"), "measured": out.get("measured", 0),
                     "total": out.get("total", len(feats)), "legend": STALLED_LEGEND if stalled else LEGEND,
                     "at": out["at"].isoformat() if isinstance(out.get("at"), datetime) else None,
                     "free_flow": sorted({r["free_flow"] for r in out["segments"]}),
                     "last_measured_at": out.get("last_measured_at"), "usual": out.get("usual", 0)}}
    return JSONResponse(body, headers={"Cache-Control": "max-age=60" if moment is None else "max-age=600"})


@router.get("/api/mobility")
def mobility(request: Request) -> JSONResponse:
    """What is moving now, for the Mobility workspace and the strip: buses per line, the
    slowest roads and the busiest counting cameras today (cached 30 s; cameras 10 min)."""
    agent = agent_of(request)
    body = cached("mobility_summary", 30, agent.mobility.summary) or {}

    def cams():
        rows = context.traffic(agent.city, "today", "camera", None).get("rows") or []
        return [{k: r.get(k) for k in ("camera", "lat", "lon", "people", "car", "bus", "bike", "total")}
                for r in rows[:6]]

    body = {**body, "cameras": cached("mobility_cameras", 600, cams)}  # None if they failed
    body["as_of"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse(body, headers={"Cache-Control": "max-age=30"})

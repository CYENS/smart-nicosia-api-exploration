"""The island's grid for the page: `/api/grid` (now) and `/api/grid?at=` (a moment of the city
clock, from the twin's memory only), and the generating stations as a map layer."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot.energy import plants
from chatbot.mobility.roads import parse_moment
from chatbot.routes import agent_of

router = APIRouter()


@router.get("/api/grid")
def grid(request: Request, at: str | None = Query(None, max_length=40)) -> JSONResponse:
    agent = agent_of(request)
    try:
        moment = parse_moment(at, agent.city.now())
    except ValueError as e:
        raise HTTPException(400, str(e).replace("road speeds have", "the grid has")) from None
    body = agent.grid.snapshot(moment)
    # Live, the next row comes every 15 minutes; a past moment never changes.
    cache = "max-age=60" if body["live"] else "max-age=600"
    return JSONResponse(body, headers={"Cache-Control": cache})


@router.get("/api/grid/plants")
def grid_plants(request: Request) -> JSONResponse:
    snap = agent_of(request).grid.snapshot(None)
    n = snap["now"] or {}
    feats = plants.features({"oil": n.get("conventional_mw"), "wind": n.get("wind_mw")})
    return JSONResponse({"type": "FeatureCollection", "features": feats,
                         "meta": {"title": "Where the island's power is made", "legend": plants.legend(),
                                  "note": plants.SOLAR_NOTE}},
                        headers={"Cache-Control": "max-age=300"})

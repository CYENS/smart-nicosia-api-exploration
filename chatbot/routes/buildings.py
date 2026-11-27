"""Buildings for the map: the DLS 3D city in z16 tiles, and every register building as a layer."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot import lidar
from chatbot.dls import TILE_ZOOM
from chatbot.eoa import layer_features
from chatbot.routes import agent_of
from chatbot.store import STATE_COLORS, legend_for
from chatbot.web import WebError

router = APIRouter()


@router.get("/api/buildings3d/{z}/{x}/{y}")
def buildings_3d(z: int, x: int, y: int, request: Request) -> JSONResponse:
    """DLS 3D buildings in one z16 tile, for the map's 3D city layer."""
    if z != TILE_ZOOM or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, f"tiles are z{TILE_ZOOM} only")
    try:
        feats = agent_of(request).dls.tile(x, y)
    except WebError as e:
        raise HTTPException(502, str(e)) from None
    return JSONResponse({"type": "FeatureCollection", "features": feats},
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/buildings/changes")
def changes(request: Request) -> JSONResponse:
    """Every building the 2019 flight found a floor or more off the 2014 survey, as a map layer
    (footprints at their 2019 height, coloured by what changed)."""
    flight = agent_of(request).lidar
    if not flight.changes.rows:
        raise HTTPException(503, "the 2019 measurements aren't in this copy of the twin")
    rows = [r for r in flight.changes.rows if r["area_m2"] >= lidar.HOUSE_M2]  # no sheds
    counts = flight.changes.counts
    return JSONResponse({
        "type": "FeatureCollection", "features": lidar.layer_features(rows, STATE_COLORS),
        "meta": {"ref": lidar.CHANGES_REF, "title": "Buildings changed between 2014 and 2019",
                 "geometry": "buildings", "count": len(rows), "legend": legend_for(lidar.CHANGED),
                 "sources": ["dls_lidar", "dls"],
                 "by_change": {s: sum(r["state"] == s for r in rows) for s in lidar.CHANGED},
                 "measured": sum(counts.values()), "min_footprint_m2": lidar.HOUSE_M2}},
        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/api/dangerous")
def dangerous(request: Request) -> JSONResponse:
    """Every register building as a map layer (markers, plot outlines and 3D footprints)."""
    registers = agent_of(request).registers
    records = registers.records(wait_s=90)
    if not records:
        raise HTTPException(503, "the registers are still loading; try again in a minute")
    features = layer_features(records, STATE_COLORS)
    return JSONResponse({
        "type": "FeatureCollection", "features": features,
        "meta": {"ref": "eoa_registers", "title": "Dangerous buildings (EOA registers)",
                 "geometry": "buildings", "count": len(records),
                 "legend": legend_for(["dangerous", "needs_repair"]),
                 "sources": registers.sources, "linked_3d": registers.linked_3d}})

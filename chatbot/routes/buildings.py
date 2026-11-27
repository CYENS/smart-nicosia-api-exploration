"""Buildings for the map: the DLS 3D city in z16 tiles, and every register building as a layer."""

from __future__ import annotations

import gzip

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from chatbot import citytiles, lidar
from chatbot.citytiles import CityTiles
from chatbot.dls import CYPRUS_BBOX, TILE_ZOOM
from chatbot.eoa import layer_features
from chatbot.osm3d import OsmBuildings
from chatbot.routes import agent_of
from chatbot.store import STATE_COLORS, legend_for
from chatbot.web import WebError

router = APIRouter()

# The 3D city is drawn from vector tiles, z12 to z16 (chatbot/citytiles.py): DLS buildings, and
# OpenStreetMap's where the DLS has none. The z16 GeoJSON tiles further down carry every DLS
# field, for what the page works out from the buildings near the eye (their shadows).


def city_tiles_of(request: Request) -> CityTiles:
    """The app's 3D city tiles (made on first use where the server didn't start them)."""
    state = request.app.state
    tiles = getattr(state, "city_tiles", None)
    if tiles is None:
        agent = agent_of(request)
        tiles = state.city_tiles = CityTiles(agent.dls, OsmBuildings(agent.settings))
    return tiles


@router.get("/api/buildings3d.json")
def buildings_3d_tilejson(request: Request) -> JSONResponse:
    """The 3D city's vector tiles, as a TileJSON. The page names the tiles' address itself, with
    its own origin (behind a proxy this server doesn't know it)."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "tilejson": "3.0.0", "name": "Nicosia 3D city",
        "tiles": [f"{base}/api/buildings3d/{{z}}/{{x}}/{{y}}.mvt?v={citytiles.VERSION}"],
        "minzoom": citytiles.MIN_ZOOM, "maxzoom": citytiles.MAX_ZOOM, "bounds": list(CYPRUS_BBOX),
        "vector_layers": [{"id": citytiles.LAYER, "minzoom": citytiles.MIN_ZOOM,
                           "maxzoom": citytiles.MAX_ZOOM,
                           "fields": {"h": "Number", "b": "Number", "src": "String"}}],
    }, headers={"Cache-Control": "public, max-age=300"})


@router.get("/api/buildings3d/status")
def buildings_3d_status(request: Request) -> JSONResponse:
    """How far the 3D city has been read in the background (greater Nicosia's tiles)."""
    return JSONResponse(city_tiles_of(request).status(), headers={"Cache-Control": "no-store"})


@router.get("/api/buildings3d/{z}/{x}/{y}.mvt")
def buildings_3d_tile(z: int, x: int, y: int, request: Request) -> Response:
    """One vector tile of the 3D city. A tile still filling in comes back at once, with what
    there is and a max-age of a few seconds, so MapLibre asks again."""
    if not (citytiles.MIN_ZOOM <= z <= citytiles.MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, f"tiles are z{citytiles.MIN_ZOOM} to z{citytiles.MAX_ZOOM}")
    tiles = city_tiles_of(request)
    data, complete = tiles.tile(z, x, y)
    headers = {"Cache-Control": f"public, max-age={tiles.max_age(complete)}", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
    else:
        data = gzip.decompress(data)
    return Response(data, media_type="application/vnd.mapbox-vector-tile", headers=headers)


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

"""Trees for the 3D city (chatbot/trees.py), one z16 tile at a time, as compact rows:
[lon, lat, height m, crown radius m, kind, leaf] with kind and leaf as indexes into KINDS and
LEAVES. Until OpenStreetMap's trees are first read the tile says so ("loading"), and the page
asks again later."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot.dls import TILE_ZOOM
from chatbot.routes import agent_of
from chatbot.trees import KINDS, LEAVES

router = APIRouter()


@router.get("/api/trees/{z}/{x}/{y}")
def trees_tile(z: int, x: int, y: int, request: Request) -> JSONResponse:
    if z != TILE_ZOOM or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, f"tiles are z{TILE_ZOOM} only")
    trees = getattr(agent_of(request), "trees", None)
    if trees is None or not trees.ready:
        status = "unavailable" if trees is None or trees.error else "loading"
        return JSONResponse({"status": status, "trees": [], "kinds": KINDS, "leaves": LEAVES},
                            headers={"Cache-Control": "no-store"})
    rows = [[t["lon"], t["lat"], t["h"], t["r"], KINDS.index(t["kind"]), LEAVES.index(t["leaf"])]
            for t in trees.tile(x, y)]
    return JSONResponse({"status": "ready", "trees": rows, "kinds": KINDS, "leaves": LEAVES},
                        headers={"Cache-Control": "public, max-age=3600"})

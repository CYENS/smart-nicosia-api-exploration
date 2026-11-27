"""Aerial photo tiles for the map (chatbot/imagery.py): the 2019 photo on the ground, and the
1993 and 1963 photos in the city clock's compare swipe.

A tile on disk goes straight back. One that isn't waits its turn for DLS in a small pool of its
own (so a screenful of new tiles never ties up the server's other requests), and is dropped
before its turn if the page stopped wanting it (the map moved on): DLS is a throttled public
service, and the tiles already fetched for a place stay on disk."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from chatbot.imagery import PHOTOS
from chatbot.routes import agent_of
from chatbot.web import WebError

router = APIRouter()
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="imagery")
CACHE = {"Cache-Control": "public, max-age=2592000"}  # the photos don't change: a month


@router.get("/api/imagery")
def photos(request: Request) -> JSONResponse:
    """The aerial photos the map can show, with where their tiles are."""
    return JSONResponse({"photos": agent_of(request).imagery.public()})


@router.get("/api/imagery/{photo}/{z}/{x}/{y}")
async def photo_tile(photo: str, z: int, x: int, y: int, request: Request) -> Response:
    imagery = agent_of(request).imagery
    if photo not in PHOTOS or not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "no such photo tile")
    hit = imagery.cached(photo, z, x, y)
    if hit is None:
        gone = threading.Event()

        def fetch():
            return None if gone.is_set() else imagery.tile(photo, z, x, y)

        job = asyncio.wrap_future(_pool.submit(fetch))
        while True:
            done, _ = await asyncio.wait({job}, timeout=0.4)
            if done:
                break
            if await request.is_disconnected():
                gone.set()  # its turn comes to nothing; the job finishes quietly
                return Response(status_code=204)
        try:
            hit = job.result()
        except WebError as e:
            raise HTTPException(502, str(e)) from None
        if hit is None:
            return Response(status_code=204)
    data, kind = hit
    return Response(content=data, media_type=kind, headers=CACHE)

"""FastAPI app: serves the map + chat page and streams each turn as Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from chatbot.agent import Agent, describe_api_error
from chatbot.city import CityData
from chatbot.dls import DLS, TILE_ZOOM
from chatbot.eoa import Registry, layer_features
from chatbot.geo import Places
from chatbot.overview import live_lamps, live_strip
from chatbot.routing import Router
from chatbot.settings import ROOT, STATIC_DIR, load_settings
from chatbot.store import STATE_COLORS, legend_for
from chatbot.tools import vega_lite_validator
from chatbot.web import WebError

log = logging.getLogger("chatbot")


def _warm_up(agent: Agent) -> None:
    """Load the slow, cacheable things in the background so the first question is quick."""
    for step in (agent.city.overview, lambda: vega_lite_validator(agent.settings)):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - best effort
            log.warning("warm-up step failed: %s", e)
    # Overpass is often at capacity: retry with growing pauses (Photon covers place search meanwhile).
    for pause in (0, 60, 180, 600, 1800):
        time.sleep(pause)
        agent.places.refresh_if_needed()
        if agent.places.ready:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(ROOT / ".env")
    settings = load_settings()
    dls = DLS(settings)
    registers = Registry(settings, dls)
    agent = Agent(settings, CityData(settings), Places(settings), Router(settings), dls=dls,
                  registers=registers)
    app.state.agent = agent
    threading.Thread(target=_warm_up, args=(agent,), daemon=True).start()
    # The registers refresh daily; the first build (parcels, then 3D heights) takes ~2 minutes.
    threading.Thread(target=registers.refresh_if_needed, daemon=True).start()
    yield


app = FastAPI(title="Nicosia digital twin", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=4096)  # the register layer and 3D tiles are big
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    context: dict | None = None


@app.get("/")
def index() -> FileResponse:
    # Always revalidated: it names the hashed bundles of the current build.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> dict:
    agent: Agent = app.state.agent
    return {
        "ok": True,
        "model": agent.settings.model,
        "has_credentials": bool(os.environ.get("ANTHROPIC_API_KEY")
                                or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "places_ready": agent.places.ready,
        "registers_ready": agent.registers.ready,
        "registers_3d_linked": agent.registers.linked_3d,
    }


@app.get("/api/overview")
def overview() -> dict:
    """The live strip: lamps lit, free parking, full bins, air, weather and the registers."""
    return live_strip(app.state.agent)


@app.get("/api/lamps")
def lamps() -> JSONResponse:
    """The smart street lamps and whether each is lit, for the map's night lighting."""
    return JSONResponse(live_lamps(app.state.agent), headers={"Cache-Control": "max-age=60"})


@app.get("/api/buildings3d/{z}/{x}/{y}")
def buildings_3d(z: int, x: int, y: int) -> JSONResponse:
    """DLS 3D buildings in one z16 tile, for the map's 3D city layer."""
    if z != TILE_ZOOM or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, f"tiles are z{TILE_ZOOM} only")
    try:
        feats = app.state.agent.dls.tile(x, y)
    except WebError as e:
        raise HTTPException(502, str(e)) from None
    return JSONResponse({"type": "FeatureCollection", "features": feats},
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/dangerous")
def dangerous() -> JSONResponse:
    """Every register building as a map layer (markers, plot outlines and 3D footprints)."""
    registers: Registry = app.state.agent.registers
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


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    agent: Agent = app.state.agent
    conv = agent.conversation(req.conversation_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        try:
            async with conv.lock:
                await agent.run_turn(conv, req.message, req.context, queue.put_nowait)
        except Exception as e:  # noqa: BLE001 - every failure becomes a message in the chat
            log.exception("turn failed")
            queue.put_nowait({"type": "error", "message": describe_api_error(e)})
        finally:
            queue.put_nowait({"type": "done"})

    task = asyncio.create_task(run())

    async def events():
        yield f"data: {json.dumps({'type': 'conversation', 'id': conv.id})}\n\n"
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            if event["type"] == "done":
                break
        await task

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/layers/{cid}/{ref}")
def layer(cid: str, ref: str) -> JSONResponse:
    agent: Agent = app.state.agent
    conv = agent.conversations.get(cid)
    if conv is None or ref not in conv.store.layers:
        raise HTTPException(404, "unknown layer")
    return JSONResponse(conv.store.layers[ref])


@app.post("/api/reset/{cid}")
def reset(cid: str) -> dict:
    app.state.agent.conversations.pop(cid, None)
    return {"ok": True}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    settings = load_settings()
    import uvicorn

    print(f"Nicosia digital twin: http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()

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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from chatbot.agent import Agent, describe_api_error
from chatbot.city import CityData
from chatbot.geo import Places
from chatbot.routing import Router
from chatbot.settings import ROOT, STATIC_DIR, load_settings
from chatbot.tools import vega_lite_validator

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
    agent = Agent(settings, CityData(settings), Places(settings), Router(settings))
    app.state.agent = agent
    threading.Thread(target=_warm_up, args=(agent,), daemon=True).start()
    yield


app = FastAPI(title="Nicosia digital twin", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    context: dict | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    agent: Agent = app.state.agent
    return {
        "ok": True,
        "model": agent.settings.model,
        "has_credentials": bool(os.environ.get("ANTHROPIC_API_KEY")
                                or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "places_ready": agent.places.ready,
    }


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

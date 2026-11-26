"""The conversation: each turn streamed as Server-Sent Events, the layers it drew, and a reset."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from chatbot.agent import describe_api_error
from chatbot.routes import agent_of

log = logging.getLogger("chatbot")
router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    context: dict | None = None


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    agent = agent_of(request)
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


@router.get("/api/layers/{cid}/{ref}")
def layer(cid: str, ref: str, request: Request) -> JSONResponse:
    conv = agent_of(request).conversations.get(cid)
    if conv is None or ref not in conv.store.layers:
        raise HTTPException(404, "unknown layer")
    return JSONResponse(conv.store.layers[ref])


@router.post("/api/reset/{cid}")
def reset(cid: str, request: Request) -> dict:
    agent_of(request).conversations.pop(cid, None)
    return {"ok": True}

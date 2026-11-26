"""The conversation: each turn streamed as Server-Sent Events, the layers it drew, Stop, and a
reset.

A turn runs in its own task. It ends early when the page calls Stop or when the page goes away
(the stream is closed); either way the agent takes the turn out of the history (agent.run_turn).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

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
    # Ask in place of this turn and every turn after it (Retry, Edit): the turn number the
    # "turn" event gave.
    replace_turn: int | None = Field(default=None, ge=0)


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    agent = agent_of(request)
    conv = agent.conversation(req.conversation_id)
    queue: asyncio.Queue = asyncio.Queue()
    run_id = uuid.uuid4().hex[:12]

    async def run() -> None:
        try:
            async with conv.lock:
                await agent.run_turn(conv, req.message, req.context, queue.put_nowait,
                                     replace=req.replace_turn)
        except asyncio.CancelledError:  # Stop, or the page went away; this task ends here
            queue.put_nowait({"type": "stopped"})
        except Exception as e:  # noqa: BLE001 - every failure becomes a message in the chat
            log.exception("turn failed")
            queue.put_nowait({"type": "error", "message": describe_api_error(e)})
        finally:
            conv.runs.pop(run_id, None)
            queue.put_nowait({"type": "done"})

    task = asyncio.create_task(run())
    conv.runs[run_id] = task

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'conversation', 'id': conv.id, 'run': run_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                if event["type"] == "done":
                    break
            await task
        finally:
            if not task.done():  # the page closed the stream: nobody is reading the answer
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/stop/{cid}/{run}")
async def stop(cid: str, run: str, request: Request) -> dict:
    """Stop a turn (the run id from its "conversation" event). Async on purpose: a task is
    cancelled from the event loop's own thread."""
    conv = agent_of(request).conversations.get(cid)
    task = conv.runs.get(run) if conv else None
    if task is None or task.done():
        return {"stopped": False}
    task.cancel()
    return {"stopped": True}


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

"""The twin's watch for the page: the incident inbox, what people do about an incident, the
watches, and one live stream of everything that changes (server-sent events)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from chatbot.moments import TZ
from chatbot.routes import agent_of
from chatbot.watch.incidents import ActionError
from chatbot.watch.service import Watcher
from chatbot.watch.watches import WatchError

router = APIRouter()

STATUSES = ("unresolved", "open", "acknowledged", "snoozed", "resolved", "all")
FAMILIES = ("outage", "lighting", "power", "parking", "bins", "air", "other", "feed")


def watcher_of(request: Request) -> Watcher:
    return agent_of(request).watcher


@router.get("/api/incidents")
def incidents(request: Request, status: str = Query("unresolved", max_length=20),
              severity: str | None = Query(None, max_length=10),
              family: str | None = Query(None, max_length=20),
              entity: str | None = Query(None, max_length=80),
              since_hours: int | None = Query(None, ge=1, le=24 * 62),
              limit: int = Query(100, ge=1, le=300)) -> dict:
    """The inbox: incidents newest first by status and severity, with the counts per status
    and severity for the strip and the filters."""
    if status not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    if severity not in (None, "", "high", "medium", "low"):
        raise HTTPException(400, "severity must be high, medium or low")
    if family not in (None, "", *FAMILIES):
        raise HTTPException(400, f"family must be one of {', '.join(FAMILIES)}")
    w = watcher_of(request)
    now = w.now()
    rows = w.book.query(status=status, severity=severity or None, family=family or None,
                        entity=entity or None,
                        since=now - timedelta(hours=since_hours) if since_hours else None,
                        limit=limit)
    return {"incidents": rows, "counts": w.book.counts(), "watch": w.status()}


@router.get("/api/incidents/marks")
def marks(request: Request, days: int = Query(31, ge=1, le=62)) -> dict:
    """Every incident that began in the last `days`, for the city clock's track."""
    w = watcher_of(request)
    since = w.now() - timedelta(days=days)
    rows = w.book.query(status="all", since=since, limit=1000)
    return {"marks": [{"id": r["id"], "t": r["start"], "severity": r["severity"],
                       "title": r["title"], "status": r["status"]}
                      for r in rows if r["start_ms"] >= since.timestamp() * 1000]}


@router.get("/api/incidents/{iid}")
def incident(iid: str, request: Request) -> dict:
    w = watcher_of(request)
    x = w.book.get(iid)
    if x is None or not x["members"]:
        raise HTTPException(404, f"no incident {iid}")
    return w.book.detail(x, w.now())


class Action(BaseModel):
    action: str = Field(max_length=20)
    by: str | None = Field(None, max_length=60)
    until: str | None = Field(None, max_length=40)  # local wall time or ISO with offset
    note: str | None = Field(None, max_length=300)


def _until(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "until must be a time like 2026-11-26T08:00") from None
    return t.replace(tzinfo=TZ) if t.tzinfo is None else t


@router.post("/api/incidents/{iid}/actions")
def act(iid: str, body: Action, request: Request) -> dict:
    """Acknowledge, snooze (until), unsnooze, resolve or reopen, with an optional name and note."""
    w = watcher_of(request)
    try:
        out = w.book.act(iid, body.action, by=body.by, until=_until(body.until), note=body.note)
    except ActionError as e:
        raise HTTPException(404 if "no incident" in str(e) else 400, str(e)) from None
    w.book.save()
    w.acted(w.book.summary(w.book.get(iid), w.now()))
    return out


@router.get("/api/watches")
def watches(request: Request) -> dict:
    return {"watches": watcher_of(request).watches.list()}


class Confirm(BaseModel):
    proposal_id: str = Field(max_length=40)
    by: str | None = Field(None, max_length=60)


@router.post("/api/watches/confirm")
def confirm(body: Confirm, request: Request) -> dict:
    """A person's click saves a watch the chatbot proposed (it never saves one itself)."""
    w = watcher_of(request)
    try:
        saved = w.watches.confirm(body.proposal_id, body.by)
    except WatchError as e:
        raise HTTPException(409, str(e)) from None
    w.hub.publish({"type": "watches"})
    return saved


@router.delete("/api/watches/{wid}")
def delete(wid: str, request: Request) -> dict:
    w = watcher_of(request)
    if not w.watches.delete(wid):
        raise HTTPException(404, "no such watch")
    w.hub.publish({"type": "watches"})
    return {"ok": True}


@router.get("/api/watch/stream", response_class=EventSourceResponse)
async def stream(request: Request) -> AsyncIterator[ServerSentEvent]:
    """Everything the watch raises, live: new and changed incidents, actions people take,
    watches that fire. FastAPI sends a keep-alive comment every 15 s."""
    w = watcher_of(request)
    q = w.hub.subscribe(asyncio.get_running_loop())
    try:
        yield ServerSentEvent(data={"type": "hello", "counts": w.book.counts()}, retry=5000)
        while True:
            event = await q.get()
            yield ServerSentEvent(data=event)
    finally:
        w.hub.unsubscribe(q)

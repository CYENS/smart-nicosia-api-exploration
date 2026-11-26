"""What the page reads without being asked: health, the live strip and the lamps that glow at night.
The strip and the lamps take `?at=` (epoch ms or local time) for the city clock: then they read
as they were at that moment, from the twin's memory."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot.moments import OutOfRange, parse_at
from chatbot.overview import live_lamps, live_strip, moments_of, strip_at
from chatbot.routes import agent_of

router = APIRouter()
AT = Query(None, max_length=40, description="A past moment: epoch ms or local time; empty is now.")


def moment(request: Request, at: str | None):
    """The moment a request asks about (None: now), or a 400 the page can show."""
    if not isinstance(at, str) or not at:  # also when a test calls the route directly
        return None
    try:
        return parse_at(at, agent_of(request).city.now())
    except OutOfRange as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/health")
def health(request: Request) -> dict:
    agent = agent_of(request)
    return {
        "ok": True,
        "model": agent.settings.model,
        "has_credentials": bool(os.environ.get("ANTHROPIC_API_KEY")
                                or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "places_ready": agent.places.ready,
        "registers_ready": agent.registers.ready,
        "registers_3d_linked": agent.registers.linked_3d,
    }


@router.get("/api/overview")
def overview(request: Request, at: str | None = AT) -> dict:
    """The live strip: lamps lit, free parking, full bins, air, weather and the registers; with
    `at`, as they were then."""
    when = moment(request, at)
    if when is None:
        return {**live_strip(agent_of(request)), "live": True}
    return strip_at(agent_of(request), when)


@router.get("/api/lamps")
def lamps(request: Request, at: str | None = AT) -> JSONResponse:
    """The smart street lamps and whether each is lit, for the map's night lighting; with `at`,
    as they were then (unknown where the memory holds nothing)."""
    when = moment(request, at)
    if when is None:
        return JSONResponse(live_lamps(agent_of(request)), headers={"Cache-Control": "max-age=60"})
    return JSONResponse(moments_of(agent_of(request)).lamps_at(when),
                        headers={"Cache-Control": "max-age=300"})

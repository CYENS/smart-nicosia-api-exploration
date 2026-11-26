"""What the page reads without being asked: health, the live strip and the lamps that glow at night."""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chatbot.overview import live_lamps, live_strip
from chatbot.routes import agent_of

router = APIRouter()


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
def overview(request: Request) -> dict:
    """The live strip: lamps lit, free parking, full bins, air, weather and the registers."""
    return live_strip(agent_of(request))


@router.get("/api/lamps")
def lamps(request: Request) -> JSONResponse:
    """The smart street lamps and whether each is lit, for the map's night lighting."""
    return JSONResponse(live_lamps(agent_of(request)), headers={"Cache-Control": "max-age=60"})

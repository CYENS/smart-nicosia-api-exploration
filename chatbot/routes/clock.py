"""The city clock: what its track draws (how much of the city the memory covers, hour by hour,
and the events it logged) and every device of a kind at a moment, for restating the map's
layers when the clock is moved."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot.moments import CLOCK_KINDS, MAX_DAYS
from chatbot.overview import moments_of
from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment

router = APIRouter()


@router.get("/api/clock")
def clock(request: Request, days: int = Query(31, ge=1, le=MAX_DAYS)) -> JSONResponse:
    """The track: coverage per kind and in all, per hour, the oldest moment the memory holds,
    and the events per hour (cached for 5 minutes)."""
    return JSONResponse(moments_of(agent_of(request)).track(days),
                        headers={"Cache-Control": "max-age=120"})


@router.get("/api/clock/states")
def states(request: Request, kind: str = Query(..., max_length=40), at: str | None = AT) -> JSONResponse:
    """Every device of `kind` at the moment `at`: {devices: {id: {s: state, ...values}}}."""
    if kind not in CLOCK_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(CLOCK_KINDS)}")
    when = moment(request, at)
    if when is None:
        raise HTTPException(400, "give a past moment as `at`; the layers already show now")
    return JSONResponse(moments_of(agent_of(request)).states_at(kind, when),
                        headers={"Cache-Control": "max-age=300"})

"""The inspector's endpoints: search everything, one entity, and its last 24 hours. With `?at=`
(the city clock) an entity reads as it was at that moment, and its series covers the 24 hours
around it."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from collector.http import ApiError

from chatbot.entities import AT_RESOLVERS, HISTORY_KINDS, EntityNotFound, at_moment, public
from chatbot.moments import frame_of, label
from chatbot.overview import cached, moments_of
from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment
from chatbot.web import WebError

router = APIRouter()


def _near(lat: float | None, lon: float | None) -> tuple[float, float] | None:
    return (lon, lat) if lat is not None and lon is not None else None


@router.get("/api/entities/search")
def search(request: Request, q: str = Query("", max_length=120),
           limit: int = Query(24, ge=1, le=60)) -> dict:
    """Devices, register buildings, places and streets matching `q`, from the caches."""
    agent = agent_of(request)
    return {"query": q, "results": agent.entities.search(q, limit),
            "ready": {"places": bool(agent.places and agent.places.ready),
                      "registers": bool(agent.registers and agent.registers.ready)}}


def _get(request: Request, eid: str, lat: float | None, lon: float | None):
    try:
        return agent_of(request).entities.get(eid, _near(lat, lon))
    except EntityNotFound as e:
        raise HTTPException(404, str(e)) from None
    except (ApiError, WebError) as e:
        raise HTTPException(502, f"a data source didn't answer: {e}") from None


def series_end(at: datetime, now: datetime) -> datetime:
    """Where the 24 hours shown around a past moment end: 12 hours after it (on the hour, so
    moving the clock within the hour keeps the same window), and never after now."""
    return min(now, at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=12))


def _at(request: Request, entity: dict, when: datetime) -> dict:
    """The entity at a past moment, cached per quarter hour (the memory doesn't change)."""
    if entity["kind"] in AT_RESOLVERS:  # a kind that reads its own past (road speeds)
        return AT_RESOLVERS[entity["kind"]](agent_of(request).entities, entity, when)
    if entity["kind"] not in HISTORY_KINDS or "_device" not in entity:
        return at_moment(entity, None, False, when, label(frame_of(when)))
    moments = moments_of(agent_of(request))
    key = f"entity_at:{entity['id'].lower()}:{int(frame_of(when).timestamp())}"
    interp, known = cached(key, 600, lambda: moments.device_at(entity["_device"], when),
                           keep=lambda v: v is not None) or (None, False)
    if interp is None:
        raise HTTPException(503, "the twin's memory could not be read; try again in a moment")
    return at_moment(entity, interp, known, when, label(when))


@router.get("/api/entities/{eid}")
def entity(eid: str, request: Request, lat: float | None = None, lon: float | None = None,
           series: bool = True, at: str | None = AT) -> dict:
    """One entity. `lat`/`lon` say where it was clicked (it saves a lookup for a 3D building);
    `series=false` leaves out the last 24 hours, which /series then fetches; `at` gives it as
    it was at a past moment."""
    when = moment(request, at)
    record = _get(request, eid, lat, lon)
    if when is not None:
        record = _at(request, record, when)
    out = public(record)
    if series:
        out["series"] = _series(request, eid, lat, lon, when)
    return out


@router.get("/api/entities/{eid}/series")
def entity_series(eid: str, request: Request, lat: float | None = None,
                  lon: float | None = None, at: str | None = AT) -> dict:
    when = moment(request, at)
    _get(request, eid, lat, lon)
    return {"id": eid, "series": _series(request, eid, lat, lon, when)}


def _series(request: Request, eid: str, lat: float | None, lon: float | None,
            when: datetime | None = None) -> dict | None:
    agent = agent_of(request)
    end = series_end(when, agent.city.now()) if when is not None else None
    try:
        return agent.entities.series(eid, _near(lat, lon), end)
    except (ApiError, WebError) as e:
        return {"error": f"the city's API didn't send the history: {e}"}

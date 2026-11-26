"""The inspector's endpoints: search everything, one entity, and its last 24 hours."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from collector.http import ApiError

from chatbot.entities import EntityNotFound, public
from chatbot.routes import agent_of
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


@router.get("/api/entities/{eid}")
def entity(eid: str, request: Request, lat: float | None = None, lon: float | None = None,
           series: bool = True) -> dict:
    """One entity. `lat`/`lon` say where it was clicked (it saves a lookup for a 3D building);
    `series=false` leaves out the last 24 hours, which /series then fetches."""
    out = public(_get(request, eid, lat, lon))
    if series:
        out["series"] = _series(request, eid, lat, lon)
    return out


@router.get("/api/entities/{eid}/series")
def entity_series(eid: str, request: Request, lat: float | None = None,
                  lon: float | None = None) -> dict:
    _get(request, eid, lat, lon)
    return {"id": eid, "series": _series(request, eid, lat, lon)}


def _series(request: Request, eid: str, lat: float | None, lon: float | None) -> dict | None:
    try:
        return agent_of(request).entities.series(eid, _near(lat, lon))
    except (ApiError, WebError) as e:
        return {"error": f"the city's API didn't send the history: {e}"}

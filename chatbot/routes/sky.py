"""Endpoints for Living Sky (aircraft, ISS passes) and CyNAP ITS sites (PWD CCTV & detectors)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment
from chatbot.sky.bus_archive import archive_status
from chatbot.web import WebError

router = APIRouter()


@router.get("/api/sky/aircraft")
def sky_aircraft(request: Request, at: str | None = AT) -> JSONResponse:
    """Live aircraft within 40 nm of Nicosia (cached 30 s), or recorded positions at `?at=`."""
    agent = agent_of(request)
    when = moment(request, at)
    if when is not None:
        body = agent.sky.aircraft.at(when)
        return JSONResponse(body, headers={"Cache-Control": "max-age=300"})
    body = agent.sky.aircraft.snapshot()
    return JSONResponse({k: v for k, v in body.items() if k != "retry_at"},
                        headers={"Cache-Control": "max-age=15"})


@router.get("/api/sky/iss")
def sky_iss(request: Request, at: str | None = AT,
            days: int = Query(4, ge=1, le=7)) -> JSONResponse:
    """ISS current position (at `?at=` or now), upcoming visible passes and recent track marks."""
    agent = agent_of(request)
    when = moment(request, at)
    body = agent.sky.iss.summary(at=when, look_ahead_days=days)
    return JSONResponse(body, headers={"Cache-Control": "max-age=300" if when is None else "max-age=600"})


@router.get("/api/sky/its")
def sky_its(request: Request,
            kind: str = Query("all", pattern="^(all|cctv|detectors)$"),
            detector_mode: str = Query("all", pattern="^(all|vehicles|cycle)$")) -> JSONResponse:
    """PWD CCTV cameras (13) and traffic detectors (39) in Nicosia, plus Swarco feed status."""
    agent = agent_of(request)
    try:
        body = agent.sky.its.geojson(kind=kind, detector_mode=detector_mode)
    except WebError as e:
        raise HTTPException(502, f"CyNAP ITS inventory couldn't be read: {e}") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"CyNAP ITS inventory failed: {e}") from None
    return JSONResponse(body, headers={"Cache-Control": "max-age=3600"})


@router.get("/api/sky/bus-archive")
def sky_bus_archive(request: Request) -> JSONResponse:
    """Status of recorded `bus_positions` rows for historical bus replay."""
    agent = agent_of(request)
    status = archive_status(agent.memory)
    arch = agent.sky.bus_archiver
    status["recording"] = arch is not None
    if arch is not None:
        status["rows_written"] = arch.rows_written
        status["flushes"] = arch.flushes
        status["last_error"] = arch.last_error
    return JSONResponse(status, headers={"Cache-Control": "no-store"})

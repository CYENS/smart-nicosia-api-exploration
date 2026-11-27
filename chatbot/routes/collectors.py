"""The admin page's API (`/admin`): every collector described, and its controls.

Every change answers with the whole report again, so the page redraws from one shape. A change
applies on the collector's next turn, within seconds, with no restart (chatbot/collectors.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot.collectors import Collectors, UnknownJob

router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}


def collectors_of(request: Request) -> Collectors:
    c = getattr(request.app.state, "collectors", None)
    if c is None:
        raise HTTPException(503, "the collectors are not set up on this server")
    return c


def _report(c: Collectors, status: int = 200) -> JSONResponse:
    return JSONResponse(c.report(), status_code=status, headers=NO_STORE)


def _on(body: Any) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        raise HTTPException(400, 'send {"on": true} or {"on": false}')
    return body["on"]


@router.get("/api/admin/collectors")
def collectors_report(request: Request) -> JSONResponse:
    return _report(collectors_of(request))


@router.put("/api/admin/collectors/collecting")
def set_collecting(request: Request, body: Any = Body(None)) -> JSONResponse:
    """Start or pause all collection on this database (Task Scheduler's passes too)."""
    c = collectors_of(request)
    c.set_collecting(_on(body))
    return _report(c)


@router.put("/api/admin/collectors/recording")
def set_recording(request: Request, body: Any = Body(None)) -> JSONResponse:
    """Keep, or stop keeping, what the twin's pages fetch."""
    c = collectors_of(request)
    try:
        c.set_recording(_on(body))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _report(c)


@router.patch("/api/admin/collectors/jobs/{name}")
def update_job(name: str, request: Request, body: Any = Body(None)) -> JSONResponse:
    """Switch a job on or off, or change how often it runs: {"enabled", "every_minutes",
    "night_only"}, any of them."""
    c = collectors_of(request)
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, "send the settings to change, such as {\"every_minutes\": 15}")
    try:
        c.update_job(name, body)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return _report(c)


@router.delete("/api/admin/collectors/jobs/{name}/changes")
def reset_job(name: str, request: Request) -> JSONResponse:
    """Put a job back to its defaults (collector.toml, then the built-in ones)."""
    c = collectors_of(request)
    try:
        c.reset_job(name)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    return _report(c)


@router.post("/api/admin/collectors/jobs/{name}/run")
def run_job_now(name: str, request: Request) -> JSONResponse:
    """Run a job straight away, whether or not it is due or switched on."""
    c = collectors_of(request)
    try:
        c.run_now(name)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    return _report(c, 202)

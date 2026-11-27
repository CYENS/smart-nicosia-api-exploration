"""The admin page's API (`/admin`): every collector described, and its controls.

Reading is open to whoever can open the twin. Changing anything needs the admin password
(CHATBOT_ADMIN_PASSWORD) in the X-Admin-Password header; without one set on the server, the
page is read-only. A browser only sends that header from the twin's own page (any other site
would need a CORS preflight, which this server never grants), and a request a browser marks as
coming from another site is refused before anything else, so no other page can change the
collectors on someone's behalf.

Every change answers with the whole report again, so the page redraws from one shape. A change
applies on the collector's next turn, within seconds, with no restart (chatbot/collectors.py).
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot.collectors import Collectors, TooSoon, UnknownJob

router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}
ADMIN_HEADER = "X-Admin-Password"
WRONG_PASSWORD_PAUSE_S = 1.0  # slows down guessing


def collectors_of(request: Request) -> Collectors:
    c = getattr(request.app.state, "collectors", None)
    if c is None:
        raise HTTPException(503, "the collectors are not set up on this server")
    return c


def admin_state(request: Request) -> dict:
    """Whether changes are possible on this server, and whether this request may make them."""
    expected = os.environ.get("CHATBOT_ADMIN_PASSWORD") or ""
    if not expected:
        return {"configured": False, "unlocked": False}
    given = request.headers.get(ADMIN_HEADER) or ""
    ok = bool(given) and secrets.compare_digest(given.encode(), expected.encode())
    return {"configured": True, "unlocked": ok}


def guard_change(request: Request) -> None:
    """Refuse a change from another site, one without a JSON body, or one without the admin
    password. Called first by every route that changes something."""
    if (request.headers.get("sec-fetch-site") or "") in ("cross-site", "same-site"):
        raise HTTPException(403, "changes are only taken from the twin's own admin page")
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        raise HTTPException(415, "send changes as JSON")
    state = admin_state(request)
    if not state["configured"]:
        raise HTTPException(403, "changes are switched off on this server: set "
                                 "CHATBOT_ADMIN_PASSWORD there to allow them")
    if not state["unlocked"]:
        if request.headers.get(ADMIN_HEADER):
            time.sleep(WRONG_PASSWORD_PAUSE_S)
            raise HTTPException(403, "that admin password isn't right")
        raise HTTPException(403, "changes need the admin password")


def _report(c: Collectors, request: Request, status: int = 200) -> JSONResponse:
    body = c.report()
    body["admin"] = admin_state(request)
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def _on(body: Any) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        raise HTTPException(400, 'send {"on": true} or {"on": false}')
    return body["on"]


@router.get("/api/admin/collectors")
def collectors_report(request: Request) -> JSONResponse:
    return _report(collectors_of(request), request)


@router.get("/api/admin/collectors/jobs/{name}/how")
def how_it_is_collected(name: str, request: Request) -> JSONResponse:
    """One job in depth: how it works in plain words, then what the code, the settings and the
    database say about it right now (chatbot/collectors.py, Collectors.how)."""
    c = collectors_of(request)
    try:
        body = c.how(name)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    return JSONResponse(body, headers=NO_STORE)


@router.put("/api/admin/collectors/collecting")
def set_collecting(request: Request, body: Any = Body(None)) -> JSONResponse:
    """Start or pause all collection on this database (Task Scheduler's passes too)."""
    guard_change(request)
    c = collectors_of(request)
    c.set_collecting(_on(body))
    return _report(c, request)


@router.put("/api/admin/collectors/recording")
def set_recording(request: Request, body: Any = Body(None)) -> JSONResponse:
    """Keep, or stop keeping, what the twin's pages fetch."""
    guard_change(request)
    c = collectors_of(request)
    try:
        c.set_recording(_on(body))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _report(c, request)


@router.patch("/api/admin/collectors/jobs/{name}")
def update_job(name: str, request: Request, body: Any = Body(None)) -> JSONResponse:
    """Switch a job on or off, or change how often it runs: {"enabled", "every_minutes",
    "night_only"}, any of them."""
    guard_change(request)
    c = collectors_of(request)
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, "send the settings to change, such as {\"every_minutes\": 15}")
    try:
        c.update_job(name, body)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return _report(c, request)


@router.delete("/api/admin/collectors/jobs/{name}/changes")
def reset_job(name: str, request: Request) -> JSONResponse:
    """Put a job back to its defaults (collector.toml, then the built-in ones)."""
    guard_change(request)
    c = collectors_of(request)
    try:
        c.reset_job(name)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    return _report(c, request)


@router.post("/api/admin/collectors/jobs/{name}/run")
def run_job_now(name: str, request: Request) -> JSONResponse:
    """Run a job straight away, whether or not it is due or switched on; once a minute at most."""
    guard_change(request)
    c = collectors_of(request)
    try:
        c.run_now(name)
    except UnknownJob:
        raise HTTPException(404, f"there is no collector job called {name!r}") from None
    except TooSoon as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(e.seconds)}) from None
    return _report(c, request, 202)

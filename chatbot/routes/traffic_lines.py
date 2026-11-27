"""The camera walkthrough's API: every counting camera with its lines, their roles in the
counting-lines register (chatbot/traffic_lines.py) and what each line counted yesterday, hour by
hour; and saving a camera once a person has looked at it.

Reading is open to whoever can open the twin. Saving changes the register every traffic answer
is built from, so it needs the admin password and is refused from another site, exactly as the
collectors' controls are (chatbot/routes/collectors.py, guard_change).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot import context, traffic_lines
from chatbot.routes import agent_of
from chatbot.routes.collectors import NO_STORE, admin_state, guard_change

router = APIRouter()


def _yesterday(city) -> tuple[list, dict[tuple[int, str], dict]]:
    """Yesterday's hours, and per line: its crossings by kind, by way across and by hour."""
    start, end, _ = context._period_bounds("yesterday", city.now())
    hours = traffic_lines.day_hours(start, min(end, city.now()))
    got = traffic_lines.line_hours(city, context.TZ).get(hours)
    index = {h: i for i, h in enumerate(hours)}
    per: dict[tuple[int, str], dict] = defaultdict(lambda: {
        "kinds": Counter(), "way_1": 0, "way_2": 0, "hours": [0] * len(hours)})
    for h, rows in got.items():
        for r in rows or []:
            p = per[(r.analytics_id, (r.line_name or "").strip())]
            p["kinds"][context.OBJECT_TYPES.get(r.object_type, str(r.object_type))] += r.total or 0
            p["way_1"] += r.dir1 or 0
            p["way_2"] += r.dir2 or 0
            p["hours"][index[h]] += r.total or 0
    missed = [h for h, rows in got.items() if rows is None]
    return [h.strftime("%H:%M") for h in hours], {"per": per, "missed": len(missed)}


def _walkthrough(request: Request) -> dict:
    city = agent_of(request).city
    cams = city.traffic_cameras()
    hours, y = _yesterday(city)
    out = traffic_lines.walkthrough(cams)
    for cam in out:
        for line in cam["lines"]:
            p = y["per"].get((cam["analytics_id"], line["line_name"]))
            line["yesterday"] = ({"kinds": dict(p["kinds"]), "way_1": p["way_1"],
                                  "way_2": p["way_2"], "hours": p["hours"]} if p else None)
    return {
        "cameras": out,
        "reviewed": sum(1 for c in out if c["reviewed"]),
        "hours": hours,
        "yesterday_missed_hours": y["missed"],
        "options": {"measures": list(traffic_lines.MEASURES),
                    "direction": list(traffic_lines.DIRECTIONS),
                    "adds_up": list(traffic_lines.ADDS_UP)},
        "admin": admin_state(request),
    }


@router.get("/api/traffic-lines")
def traffic_lines_walkthrough(request: Request) -> JSONResponse:
    return JSONResponse(_walkthrough(request), headers=NO_STORE)


@router.put("/api/traffic-lines/{analytics_id}")
def save_camera(analytics_id: int, request: Request, body: Any = Body(None)) -> JSONResponse:
    """Confirm one camera: every line of it, with its measures, direction, street and whether
    it adds up ({"lines": [{"line_name", "measures", "direction", "street", "adds_up",
    "note"}]})."""
    guard_change(request)
    if not isinstance(body, dict) or not isinstance(body.get("lines"), list) or not body["lines"]:
        raise HTTPException(400, 'send {"lines": [...]} with every line of the camera')
    city = agent_of(request).city
    today = city.now().astimezone(context.TZ).date().isoformat()
    try:
        traffic_lines.save_camera(city.traffic_cameras(), analytics_id, body["lines"], today)
    except KeyError:
        raise HTTPException(404, f"there is no counting camera {analytics_id}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return JSONResponse(_walkthrough(request), headers=NO_STORE)

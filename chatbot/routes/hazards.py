"""Natural hazards for the page: the Hazards layer at the city clock's moment, the hazards of one
incident (its extent on the map while it is open in the inbox), and marks for the clock's track.
Everything comes from what the twin already read and recorded (chatbot/hazards); a past day's
fire danger is read from EFFIS once and kept."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from chatbot.hazards import feeds as F
from chatbot.hazards import layer as L
from chatbot.moments import OutOfRange, parse_at
from chatbot.routes import agent_of

router = APIRouter()


def _layer(agent, m: dict) -> dict:
    incidents = L.incident_ids(agent.watcher)
    feats = L.features(m, incidents=incidents)
    return {**m, "incidents": incidents, "features": feats, "legend": L.legend(feats, m.get("fire")),
            "meta": L.meta(m)}


@router.get("/api/hazards")
def hazards(request: Request, at: str | None = Query(None, max_length=40),
            days: int = Query(30, ge=1, le=62)) -> dict:
    """The hazards at the clock's moment (now by default): the quakes of the `days` before it, the
    warnings issued and not over, and that day's fire danger in the Nicosia district (with the
    next two days' forecast, live), with the map features and legend."""
    agent = agent_of(request)
    try:
        moment = parse_at(at, agent.city.now())
    except OutOfRange as e:
        raise HTTPException(400, str(e)) from None
    return _layer(agent, agent.hazards.moment(moment, days=days))


@router.get("/api/hazards/items")
def items(request: Request, ids: str = Query(..., max_length=2000)) -> dict:
    """Some hazards by id (an incident's members), as the layer draws them."""
    agent = agent_of(request)
    hz = agent.hazards
    wanted = [i.strip() for i in ids.split(",") if i.strip()][:60]
    found = []
    for i in wanted:
        h = hz.get(i) if i != "fire-nicosia" else hz.fire_day(hz.now().astimezone(F.TZ).date())
        if h is not None:
            found.append(h)
    now = hz.now()
    m = {"at": F.wall(now), "live": True,
         "earthquakes": [h.public() for h in found if h.kind == "earthquake"],
         "warnings": [h.public(now) for h in found if h.kind == "warning"],
         "fire": next((h.public() for h in sorted(found, key=lambda h: h.value or 0, reverse=True)
                       if h.kind == "fire"), None)}
    return _layer(agent, m)


@router.get("/api/hazards/marks")
def marks(request: Request, days: int = Query(31, ge=1, le=62)) -> dict:
    """Hazards worth a flag on the clock's track: quakes the watch would raise, warnings for
    Nicosia (where they come into force) and days of very high fire danger or worse."""
    hz = agent_of(request).hazards
    now = hz.now()
    since = now - timedelta(days=days)
    out = []
    for h in hz.all():
        if not since <= h.start <= now:
            continue
        if h.kind == "earthquake":
            raises, severity, _ = F.quake_rules(h)
            if raises:
                out.append({"id": h.id, "t": F.wall(h.start), "kind": "earthquake", "severity": severity,
                            "title": h.title})
        elif h.kind == "warning" and h.details.get("covers_nicosia"):
            out.append({"id": h.id, "t": F.wall(h.start), "kind": "warning", "level": h.level,
                        "severity": F.WARNING_SEVERITY.get(h.level or "", "low"), "title": h.title})
        elif h.kind == "fire" and F.fire_index(h) >= 3 and not h.details.get("forecast"):
            out.append({"id": h.id, "t": F.wall(h.start), "kind": "fire", "level": h.level,
                        "severity": F.FIRE_SEVERITY.get(h.level or "", "low"), "title": h.title})
    return {"marks": sorted(out, key=lambda m: m["t"])}


@router.get("/api/hazards/status")
def status(request: Request) -> dict:
    return agent_of(request).hazards.status()

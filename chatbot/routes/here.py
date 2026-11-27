"""What's here: the everyday sections of a spot (chatbot/here.py), one at a time so the page
shows each as soon as it is read and each can fail on its own. With `?at=` (the city clock)
they read that moment: parking and lamps from the twin's memory, buses as known only live."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from chatbot import here
from chatbot.environment.spot import parse, spot_id
from chatbot.moments import local_iso
from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment

router = APIRouter()


@router.get("/api/entities/{eid}/here")
def whats_here(eid: str, request: Request,
               section: str | None = Query(None, description="One of " + ", ".join(here.SECTIONS)),
               at: str | None = AT) -> dict:
    """The sections of "What's here?" at a spot (all of them, or the one `section` names)."""
    where = parse(eid)
    if where is None:
        raise HTTPException(404, f"{eid!r} isn't a point of the map. Spot ids look like "
                                 "spot-35.17000-33.36060 (latitude, longitude).")
    if section is not None and section not in here.SECTIONS:
        raise HTTPException(404, f"no section {section!r}; one of {', '.join(here.SECTIONS)}")
    when = moment(request, at)
    keys = (section,) if section else here.SECTIONS
    ents = agent_of(request).entities
    if len(keys) == 1:
        secs = [here.build(ents, keys[0], where[0], where[1], when)]
    else:
        secs = here.sections(ents, where[0], where[1], when, keys)
    return {"id": spot_id(*where), "at": local_iso(when) if when else None, "sections": secs}

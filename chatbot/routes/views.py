"""Views for the page: the same view the chat's view_from describes, for a shared link to a view
and for "View from here" on anything with a place."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from chatbot import views as V
from chatbot.routes import agent_of
from chatbot.store import SessionStore
from chatbot.tools.base import ToolContext, ToolError
from chatbot.web import WebError

router = APIRouter()


@router.get("/api/view")
def view(request: Request, frm: str = Query(alias="from", max_length=120),
         toward: str | None = Query(None, max_length=120),
         bearing: float | None = Query(None, ge=-360, le=360),
         height: str | None = Query(None, max_length=12),
         name: str | None = Query(None, max_length=120)) -> dict:
    """The camera, the facts and the labels of the view from a place (an id, a name, or
    "lat,lon"), toward a place or a bearing (the centre by default), at a height ('eye',
    'roof' or metres)."""
    agent = agent_of(request)
    ctx = ToolContext(agent.settings, agent.city, agent.places, agent.router, SessionStore(),
                      lambda _e: None, "view", agent.dls, agent.registers, None,
                      memory=agent.memory, entities=agent.entities, moments=agent.moments,
                      mobility=agent.mobility, web_places=agent.web_places, terrain=agent.terrain)
    try:
        spot = V.resolve_spot(ctx, frm)
        if name:
            spot.name = name
        target = None
        if bearing is None and toward:
            bearing = V.direction_of(toward)
            if bearing is None:
                target = V.resolve_spot(ctx, toward)
        h: str | float | None = height
        if height and height.replace(".", "", 1).isdigit():
            h = float(height)
        v = V.view(ctx, agent.terrain, spot, toward=target,
                   bearing=None if bearing is None else bearing % 360, height=h)
    except (V.ViewError, ToolError) as e:
        raise HTTPException(400, str(e)) from None
    except WebError as e:
        raise HTTPException(502, f"the terrain tiles could not be read: {e}") from None
    return {"view": V.camera(v, None), "facts": v["facts"]}

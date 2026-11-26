"""The twin's memory for the page: how fresh each source is, and one device's history with its
typical-day band (the inspector's sparkline can read it)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot.memory import Unavailable
from chatbot.memory_tools import EntityNotFound, entity_history
from chatbot.moments import OutOfRange, parse_at
from chatbot.overview import cached
from chatbot.routes import agent_of
from chatbot.tools import ToolContext, ToolError
from chatbot.web import WebError

router = APIRouter()


@router.get("/api/health/data")
def health_data(request: Request) -> JSONResponse:
    """How fresh and complete the twin's memory is, per source (cached for 30 s)."""
    agent = agent_of(request)
    if agent.memory is None:
        return JSONResponse({"store": {"status": "off"}})
    body = cached("health_data", 30, lambda: agent.memory.health(agent.city.now()))
    return JSONResponse(body or {"store": {"status": "error"}},
                        headers={"Cache-Control": "max-age=30"})


@router.get("/api/history/{entity_id}")
def history_series(entity_id: str, request: Request, hours: int = Query(24, ge=1, le=168),
                   metric: str | None = Query(None, max_length=40),
                   until: str | None = Query(None, max_length=40)) -> JSONResponse:
    """One device's metric over the last hours (up to `until`, epoch ms or local time, for the
    city clock) with its typical-day band, for the inspector's sparkline: from the twin's memory,
    the city API filling what it lacks (cached for 2 min)."""
    agent = agent_of(request)
    try:
        end = parse_at(until if isinstance(until, str) else None, agent.city.now())
    except OutOfRange as e:
        raise HTTPException(400, str(e)) from None
    ctx = ToolContext(agent.settings, agent.city, agent.places, agent.router, None,
                      lambda e: None, "", memory=agent.memory)

    def build():
        try:
            return {"ok": entity_history(ctx, entity_id, hours, metric, end)}
        except EntityNotFound:
            return {"status": 404, "detail": f"no device named {entity_id!r}"}
        except ValueError as e:
            return {"status": 400, "detail": str(e)}
        except (ToolError, Unavailable) as e:
            return {"status": 503, "detail": str(e)}
        except WebError as e:
            return {"status": 502, "detail": str(e)}

    stamp = int(end.timestamp()) // 900 if end else "now"
    out = cached(f"history:{entity_id.lower()}:{hours}:{metric}:{stamp}", 120, build,
                 keep=lambda v: bool(v) and ("ok" in v or v.get("status") in (400, 404)))
    if out is None:
        raise HTTPException(502, "the history could not be read; try again in a minute")
    if "ok" not in out:
        raise HTTPException(out["status"], out["detail"])
    return JSONResponse(out["ok"], headers={"Cache-Control": "max-age=120"})

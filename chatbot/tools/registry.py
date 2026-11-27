"""Every tool, in the order the model sees them, and the one way they are run.

Each domain module keeps a `TOOLS` list, filled by `@tool(...)` on its handlers in the order they
are written. DOMAINS puts the modules in order. The tool list is part of the prompt cache key,
so it must come out the same on every request: it is fixed here, never by import order or by
whatever registers first. To add a tool, write it in its domain's module; to add a domain, add
its module to the end of DOMAINS.
"""

from __future__ import annotations

import logging

import jsonschema

from collector.http import ApiError

from chatbot import sources
from chatbot.settings import TIMEZONE
from chatbot.tools import (buildings, charts, city, clock, entities, environment, maps, memory,
                           mobility, places, routes, scenarios, views, watch)
from chatbot.tools import streets
from chatbot.tools import facilities
from chatbot.tools import energy
from chatbot.tools import hazards
from chatbot.tools import tonight
from chatbot.tools import sky
from chatbot.tools.base import Handler, LocationNotShared, Tool, ToolContext, ToolError
from chatbot.web import WebError

log = logging.getLogger(__name__)

DOMAINS = (city, places, routes, buildings, maps, charts, memory, entities, clock, mobility,
           watch, environment, views, scenarios, streets)
# Road reports (closures, jams, roadworks): a domain of its own, at the end.
from chatbot.tools import roadreports  # noqa: E402
DOMAINS += (roadreports,)
# Park, charge and hire (car parks, chargers, bikes and scooters), after it.
DOMAINS += (facilities,)
# The island's power (demand, wind, solar and the grid's carbon), after that.
DOMAINS += (energy,)
# Hazards (earthquakes, weather warnings and fire danger), after that.
DOMAINS += (hazards,)
# Tonight in Nicosia (duty pharmacies, Visit Cyprus events, open-now places), after that.
DOMAINS += (tonight,)
# The living sky (aircraft, the ISS) and the PWD's ITS sites, last.
DOMAINS += (sky,)

TOOLS: list[Tool] = [t for domain in DOMAINS for t in domain.TOOLS]
TOOL_DEFS: list[dict] = [t.definition for t in TOOLS]
HANDLERS: dict[str, Handler] = {t.name: t.handler for t in TOOLS}
SCHEMAS: dict[str, dict] = {t.name: t.definition["input_schema"] for t in TOOLS}
if len(HANDLERS) != len(TOOLS):
    raise RuntimeError("two tools share a name")

WEB_SEARCH = {
    "type": "web_search_20260209", "name": "web_search", "max_uses": 3,
    # No "country": the API rejects "CY" as an unsupported country code for web search.
    "user_location": {"type": "approximate", "city": "Nicosia", "timezone": TIMEZONE},
}

_LOWER = {name.lower(): name for name in HANDLERS}


def run_tool(ctx: ToolContext, name: str, args: object) -> tuple[dict, bool]:
    """Validate and run one tool. Returns (result, is_error)."""
    real = HANDLERS.get(name) and name or _LOWER.get(name.lower())
    if real is None:
        return {"error": f"unknown tool {name!r}; tools: {sorted(HANDLERS)}"}, True
    if not isinstance(args, dict):
        return {"error": "INVALID_JSON: tool input must be a JSON object"}, True
    try:
        jsonschema.validate(args, SCHEMAS[real])
    except jsonschema.ValidationError as e:
        where = "/".join(map(str, e.path)) or "input"
        return {"error": f"invalid input at {where}: {e.message[:300]}"}, True
    try:
        with sources.during(real):  # what it stores says where it comes from
            return HANDLERS[real](ctx, args), False
    except LocationNotShared as e:  # the page offers what only the user can do
        return {"error": str(e), "_actions": list(e.ACTIONS),
                "_summary": "Your location isn't shared yet"}, True
    except ToolError as e:
        return {"error": str(e)}, True
    except (ApiError, WebError) as e:
        return {"error": f"data source failed: {e}. Say so plainly; don't guess values."}, True
    except Exception as e:  # noqa: BLE001 - report, don't crash the conversation
        log.exception("tool %s failed", real)
        return {"error": f"internal error in {real}: {type(e).__name__}: {e}"}, True

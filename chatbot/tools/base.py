"""What every tool module shares: the context a handler gets, the error the model can fix, the
schema helpers, the decorator that registers a tool, and how places, points and "my location"
are resolved."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from chatbot import eoa
from chatbot.city import CityData
from chatbot.dls import DLS
from chatbot.geo import Places, norm
from chatbot.memory import Memory
from chatbot.routing import Router
from chatbot.settings import TIMEZONE, Settings
from chatbot.store import SessionStore

if TYPE_CHECKING:
    from chatbot.entities import Entities
    from chatbot.moments import Moments

TZ = ZoneInfo(TIMEZONE)


class ToolError(Exception):
    """A problem the model can fix (bad name, nothing found). Sent back with is_error."""


@dataclass
class ToolContext:
    settings: Settings
    city: CityData
    places: Places
    router: Router
    store: SessionStore
    emit: Callable[[dict], None]
    conversation_id: str
    dls: DLS | None = None
    registers: eoa.Registry | None = None
    # The user's position from the page (device GPS or set on the map), if they shared it:
    # {"lat", "lon", "accuracy_m", "source"}.
    user_location: dict | None = None
    memory: Memory | None = None  # the twin's memory (chatbot/memory.py), if it is on
    # The entity model with its caches, shared with the inspector's endpoints. Built from the
    # fields above when a caller doesn't pass one.
    entities: Entities | None = None
    # The city at past moments (chatbot/moments.py), shared with the city clock's endpoints.
    moments: Moments | None = None


# -- schema helpers -----------------------------------------------------------------------------

NEAR = {
    "near_place": {"type": "string",
                   "description": "Place name to search around, e.g. 'Eleftheria Square', or "
                                  "'my location' for the user's shared position."},
    "near_lat": {"type": "number"},
    "near_lon": {"type": "number"},
    "radius_m": {"type": "number", "minimum": 20, "maximum": 10000,
                 "description": "Search radius in metres (default 500 when a 'near' is given)."},
}


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [],
            "additionalProperties": False}


def tool_def(name: str, description: str, schema: dict) -> dict:
    return {"name": name, "description": description, "input_schema": schema,
            "eager_input_streaming": True}


Handler = Callable[["ToolContext", dict], dict]


@dataclass(frozen=True)
class Tool:
    definition: dict
    handler: Handler

    @property
    def name(self) -> str:
        return self.definition["name"]


class ToolSet(list[Tool]):
    """A domain module's tools, in the order they are written. `@TOOLS.tool(name, description,
    schema)` on a handler adds it; chatbot/tools/registry.py puts the domains in order."""

    def tool(self, name: str, description: str, schema: dict) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.append(Tool(tool_def(name, description, schema), handler))
            return handler
        return register


# -- places and points --------------------------------------------------------------------------


ME = {norm(w) for w in ("me", "my location", "my position", "here", "where i am",
                         "current location", "my current location", "you are here", "εδώ",
                         "εγώ", "η θέση μου")}


def my_location(ctx: ToolContext) -> dict:
    loc = ctx.user_location
    if not loc:
        raise ToolError("the user hasn't shared a location: ask them to press the locate button "
                        "(top left of the map) or right-click the map and choose 'I'm here'")
    return {"name": "your location", "lat": loc["lat"], "lon": loc["lon"]}


def resolve_place(ctx: ToolContext, text: str) -> dict:
    """A place name -> {name, lat, lon, ...}, preferring the places index (it has opening hours).
    'me', 'my location' or 'here' is the user's shared location."""
    if norm(text) in ME:
        return my_location(ctx)
    if ctx.places.ready:
        loc = ctx.user_location
        exact = ctx.places.named(text, (loc["lat"], loc["lon"]) if loc else None)
        if exact:
            return exact[0]
    hits = ctx.places.geocode(text)
    if hits:
        return hits[0]
    if ctx.places.ready:
        near, _ = ctx.places.find(query=text, limit=1)
        if near:
            return near[0]
    raise ToolError(f"could not find {text!r} in Nicosia; try a simpler or different name")


def resolve_point(ctx: ToolContext, place: str | None, lat: float | None,
                  lon: float | None) -> tuple[float, float, str] | None:
    if lat is not None and lon is not None:
        return lat, lon, f"{lat:.5f}, {lon:.5f}"
    if place:
        p = resolve_place(ctx, place)
        return p["lat"], p["lon"], p.get("name") or place
    return None


def local_now(ctx: ToolContext) -> datetime:
    return ctx.city.now().astimezone(TZ)

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
from chatbot.overture import OV_ID
from chatbot.routing import Router
from chatbot.settings import TIMEZONE, Settings
from chatbot.store import SessionStore

if TYPE_CHECKING:
    from chatbot.entities import Entities
    from chatbot.environment.service import Environment
    from chatbot.lidar import Flight
    from chatbot.mobility import Mobility
    from chatbot.moments import Moments
    from chatbot.scenarios.lab import Lab
    from chatbot.streets import Streets
    from chatbot.sky.service import SkyService
    from chatbot.terrain import Terrain
    from chatbot.tonight.service import Tonight
    from chatbot.watch import Watcher
    from chatbot.webplaces import WebPlaces

TZ = ZoneInfo(TIMEZONE)


class ToolError(Exception):
    """A problem the model can fix (bad name, nothing found). Sent back with is_error."""


class LocationNotShared(ToolError):
    """The tool needs the user's position and they haven't shared it. Only the user can fix
    this, so the page offers them the actions that share it (run_tool adds them)."""

    ACTIONS = ("share_location", "pick_on_map")

    def __init__(self, what: str = "the user hasn't shared a location") -> None:
        super().__init__(f"{what}. The page now offers them buttons to share it or to pick it on "
                         "the map, and asks again by itself once it is shared: say so in one "
                         "short line, and don't explain how to share it")


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
    mobility: Mobility | None = None  # buses, stops and road speeds (chatbot/mobility)
    web_places: WebPlaces | None = None  # places found on the web, placed (chatbot/webplaces.py)
    watcher: Watcher | None = None  # incidents and watches (chatbot/watch)
    # Sun and shade, air and noise surfaces and the weather (chatbot/environment).
    environment: Environment | None = None
    terrain: Terrain | None = None  # the ground's height (chatbot/terrain.py)
    scenarios: Lab | None = None  # the scenario lab: runs and saved runs (chatbot/scenarios)
    lidar: Flight | None = None  # buildings as the 2019 LiDAR flight measured them (chatbot/lidar.py)
    streets: Streets | None = None  # the street rules: limits, zones, crossings (chatbot/streets)
    tonight: Tonight | None = None  # duty pharmacies, city events, open-now places (chatbot/tonight)
    sky: SkyService | None = None  # aircraft, ISS passes, CyNAP ITS sites, bus archive (chatbot/sky)


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
        raise LocationNotShared()
    return {"name": "your location", "lat": loc["lat"], "lon": loc["lon"]}


def resolve_place(ctx: ToolContext, text: str) -> dict:
    """A place name -> {name, lat, lon, ...}, preferring the places index (it has opening hours).
    'me', 'my location' or 'here' is the user's shared location; a web place's id (web-1a2b3c4d)
    or exact name is where locate_web_places put it."""
    if norm(text) in ME:
        return my_location(ctx)
    web = ctx.web_places.named(text) if ctx.web_places is not None else None
    if web:
        return {"name": web["name"], "lat": web["lat"], "lon": web["lon"], "id": web["id"]}
    loc = ctx.user_location
    near = (loc["lat"], loc["lon"]) if loc else None
    # An Overture place by its id (ov-d5564327fe43); by its name only after OSM's places.
    ov = getattr(ctx.places, "overture", None)
    ov = ov if ov is not None and ov.ready else None
    if ov is not None and OV_ID.match(text.strip()):
        hit = ov.get(text)
        if hit:
            return {"name": hit["name"], "lat": hit["lat"], "lon": hit["lon"], "id": hit["id"]}
    if ctx.places.ready:
        exact = ctx.places.named(text, near)
        if exact:
            return exact[0]
    if ov is not None and (same := ov.named(text, near)):
        return {"name": same[0]["name"], "lat": same[0]["lat"], "lon": same[0]["lon"],
                "id": same[0]["id"]}
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

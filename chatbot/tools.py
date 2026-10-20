"""The tools Claude can call: city data, places, routing, weather, map control and charts.

Data tools return compact JSON and park big results in the session store (`layer_ref`,
`data_ref`). UI tools push a command to the browser through `ctx.emit` and return "ok".
Every input is validated against its schema before the handler runs.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jsonschema
from jsonschema.exceptions import best_match

from collector.http import ApiError

from chatbot import context, eoa, history
from chatbot.city import KINDS, CityData
from chatbot.dls import DLS, area_m2, distance_to_path_m
from chatbot.geo import CATEGORIES, Places, haversine_m, loose_match, norm
from chatbot.routing import MAX_TOUR_STOPS, MODES, PREFERENCES, Router, best_order
from chatbot.settings import CACHE_DIR, TIMEZONE, Settings
from chatbot.store import STATE_COLORS, SessionStore, legend_for, point
from chatbot.web import WebError, get_json

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

KIND_STATES = {
    "street_light": ["on", "off", "offline", "unknown"],
    "parking_sensor": ["free", "occupied", "unknown"],
    "waste_bin": ["low", "medium", "high", "full", "unknown"],
    "air_station": ["very_low", "low", "medium", "high", "very_high", "unknown"],
    "info_screen": ["ok", "no_heartbeat", "not_reported", "unknown"],
    "camera": ["connected", "disconnected", "unknown"],
}
POINT_STYLES = ["points", "pulse", "heatmap", "columns_3d"]
STYLES_FOR = {"point": POINT_STYLES, "route": ["route"], "area": ["area"],
              "buildings": [*POINT_STYLES, "buildings_3d", "area"]}
HISTORY_KINDS = ["street_light", "parking_sensor", "waste_bin", "air_station"]
VISUAL_LIBRARIES = ["d3", "plot", "echarts", "three"]
MAX_VISUAL_DATA_CHARS = 3_000_000
STATUSES = ["dangerous", "needs_repair"]
SORTS = ["height", "floors", "volume", "distance", "declared"]
PASS_BY_M = 25  # a register building this close to a route's line is "passed"
MAX_AVOID_ZONES = 40  # Valhalla caps the total perimeter of polygons to avoid
# DLS 3D buildings coloured by height (upper bound in m, label, colour); dangerous ones stay red.
HEIGHT_BANDS = [(6, "under 6 m", "#cbd5e1"), (12, "6-12 m", "#93c5fd"), (20, "12-20 m", "#3b82f6"),
                (35, "20-35 m", "#1d4ed8"), (float("inf"), "35 m and up", "#312e81")]
MAX_BUILDINGS = 4000
# The walled city, where DLS models whole blocks as one building.
WALLED_CITY = (35.1745, 33.3635, 750)


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


def tool(name: str, description: str, schema: dict) -> dict:
    return {"name": name, "description": description, "input_schema": schema,
            "eager_input_streaming": True}


TOOL_DEFS: list[dict] = [
    tool("city_overview",
         "One-glance status of the whole city right now: street lights on/off/offline and the "
         "power they draw, parking sensors free/occupied, waste bins by fill level, air and noise "
         "at every station, info screens and cameras. Use it for 'how is the city doing' and as "
         "a first look before drilling down.",
         obj({})),
    tool("find_devices",
         "Find smart-city devices by kind, state, street or distance from a place, with their "
         "live interpreted state. Returns counts, the nearest or first matches, a `layer_ref` "
         "(every match, ready for show_on_map) and a `data_ref` (rows for show_chart).\n"
         "Kinds and states: street_light on|off|offline|unknown (a lamp is 'on' only if it "
         "reported in the last 70 minutes; lamps are unpowered and silent by day); "
         "parking_sensor free|occupied (on-street bays; disabled_bay marks reserved bays); "
         "waste_bin low|medium|high|full (fill %, full is 80%+); air_station very_low..very_high "
         "(CAQI band) with pm2_5, pm10, no2, o3, noise_db, temperature_c; info_screen; camera.",
         obj({
             "kind": {"type": "string", "enum": list(KINDS)},
             "state": {"type": "string", "description": "Only devices in this state."},
             "street": {"type": "string",
                        "description": "Street name to match, e.g. 'Mnasiadou' (street lights, "
                                       "air stations and cameras carry street names)."},
             **NEAR,
             "include_stale": {"type": "boolean",
                               "description": "Keep sensors that have stopped reporting (default "
                                              "false: their state can't be trusted). Street "
                                              "lights are always kept, since they are silent by "
                                              "day."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                       "description": "How many matches to list in the reply (default 12). The "
                                      "map layer always holds every match."},
         }, ["kind"])),
    tool("get_history",
         "Replay how devices changed over the last hours as a map time-lapse: street lights "
         "switching on at dusk, parking filling up, bins filling, air and noise through the day. "
         "Every frame interprets the past readings with the same rules as find_devices. Returns "
         "a layer_ref (show it with show_on_map: 'points' coloured by state, or 'heatmap' / "
         "'columns_3d' with a value_field; a time slider with play appears on the map) and a "
         "data_ref with one row per frame: frame, time, label, a count per state, plus "
         "lit_power_kw (lights), occupancy_pct (parking), avg_level_pct (bins) or avg_caqi and "
         "avg_noise_db (air). A chart of that data_ref with x = time stays in sync with the "
         "slider. Filter by street or near a place like find_devices.",
         obj({
             "kind": {"type": "string", "enum": HISTORY_KINDS},
             "hours": {"type": "integer", "minimum": 1, "maximum": 72,
                       "description": "How far back to replay (default 24)."},
             "step_minutes": {"type": "integer", "enum": [5, 10, 15, 30, 60],
                              "description": "Minutes between frames (default 15 up to 24 h, "
                                             "30 up to 48 h, else 60)."},
             "street": {"type": "string", "description": "Street name to match."},
             **NEAR,
         }, ["kind"])),
    tool("get_device",
         "Everything about one device by its id (e.g. 'YL1015', 'YW1394'): fresh readings, "
         "interpretation, attributes and location.",
         obj({"id": {"type": "string"}}, ["id"])),
    tool("get_alarms",
         "The newest active, unacknowledged alarms in the city (lost connections, full bins, "
         "lamp faults, air-quality warnings), with locations and a layer_ref.",
         obj({"limit": {"type": "integer", "minimum": 1, "maximum": 20}})),
    tool("get_traffic",
         "Counts of people, bikes, cars, motorcycles, buses and trucks from the city's video "
         "analytics cameras. group_by 'day' (one row per day), 'hour_of_day' (per hour; over "
         "several days it is a summed hour-of-day profile) or 'camera' (totals per camera with "
         "coordinates and a layer_ref, good for a heatmap of where people walk). Optionally "
         "filter to cameras whose name contains `camera` (e.g. 'Ledras', 'Makariou').",
         obj({
             "period": {"type": "string", "enum": list(context.PERIODS)},
             "group_by": {"type": "string", "enum": ["day", "hour_of_day", "camera"]},
             "camera": {"type": "string"},
         }, ["period", "group_by"])),
    tool("get_speed",
         "City-wide average traffic speed (km/h) per hour for the last N hours, next to the "
         "same hours one week earlier. There is no per-street speed in this data.",
         obj({"hours": {"type": "integer", "minimum": 1, "maximum": 48}})),
    tool("get_weather",
         "Weather now and for the next 12 hours in Nicosia (temperature, feels-like, rain "
         "chance, UV, sky), sunrise and sunset, and the regional air-quality model including "
         "Saharan dust.",
         obj({})),
    tool("geocode",
         "Find the coordinates of a place, street or address in Nicosia. Use plain names "
         "('Ledras', 'Cyprus Museum', 'Eleftheria Square').",
         obj({"query": {"type": "string"}}, ["query"])),
    tool("find_places",
         "Points of interest from OpenStreetMap: museums, cafés, pharmacies, toilets, drinking "
         "water, parks and more, by category, name and/or distance. Returns places with opening "
         "hours where mapped (raw OSM opening_hours strings; judge them against the local time "
         "in <context>) and a layer_ref.",
         obj({
             "category": {"type": "string", "enum": list(CATEGORIES)},
             "query": {"type": "string", "description": "Name or part of a name."},
             **NEAR,
             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
         })),
    tool("plan_route",
         "Walking, driving, cycling or wheelchair route through 2 to 13 stops, drawn as a "
         "layer_ref. With optimize_order the stops after the first are reordered for the "
         "shortest total travel time (exact for up to 12 stops), which is how to plan a tour of "
         "museums or sights. Preferences tune the walking route: lit_streets (after dark), "
         "quiet (footpaths and living streets), less_hills, accessible, and "
         "avoid_dangerous_buildings (keeps off the streets in front of buildings on the "
         "dangerous registers; the reply compares it with the usual route, the one picked "
         "without avoiding them: minutes added, buildings passed by each and how many it "
         "avoids, and the layer shows both routes and the buildings). Every "
         "route reports the register buildings it passes within 25 m. Give each stop a place "
         "name, 'my location', or coordinates, and optionally minutes_here for a visit. Driving "
         "routes use typical speeds, not live per-street traffic; the reply adds the current "
         "city-wide speed against last week.",
         obj({
             "stops": {"type": "array", "minItems": 2, "maxItems": MAX_TOUR_STOPS + 1,
                       "items": obj({
                           "place": {"type": "string"},
                           "lat": {"type": "number"},
                           "lon": {"type": "number"},
                           "minutes_here": {"type": "number", "minimum": 0, "maximum": 600},
                       })},
             "mode": {"type": "string", "enum": list(MODES)},
             "prefer": {"type": "array", "items": {"type": "string", "enum": list(PREFERENCES)}},
             "optimize_order": {"type": "boolean"},
             "return_to_start": {"type": "boolean"},
             "keep_last_stop_last": {"type": "boolean"},
             "start_time": {"type": "string", "description": "Local start time HH:MM (default now)."},
         }, ["stops", "mode"])),
    tool("reachable_area",
         "The area reachable within N minutes from a place, on foot, by bike or by car "
         "(an isochrone), as a layer_ref of nested polygons.",
         obj({
             "place": {"type": "string"},
             "lat": {"type": "number"},
             "lon": {"type": "number"},
             "minutes": {"type": "array", "minItems": 1, "maxItems": 4,
                         "items": {"type": "integer", "minimum": 1, "maximum": 60}},
             "mode": {"type": "string", "enum": ["walk", "bike", "drive"]},
         }, ["minutes", "mode"])),
    tool("find_dangerous_buildings",
         "Buildings on the public dangerous-building registers of the District Local Government "
         "Organisations (ΕΟΑ) of Nicosia, Limassol, Larnaca and Famagusta (Paphos publishes "
         "none): 'dangerous' (article 15B, επικίνδυνη) or 'needs_repair' (15A, χρήζει "
         "επισκευής). Each is tied to its DLS land parcel and to the DLS 3D buildings on it, so "
         "most have a real height_m and floors. Filter by eoa, status, area (municipality, "
         "community or parish; Greek or Latin spelling), street, distance from a place, or "
         "size. Returns counts by EOA, status and area, height and floor statistics, the "
         "tallest matches (or the nearest, with a place), a layer_ref and a data_ref (one row "
         "per building: eoa, status, municipality, community, parish, address, height_m, "
         "floors, building_type, footprint_m2, volume_m3, declared). Show the layer with "
         "style 'buildings_3d' to extrude the buildings at their real heights (red dangerous, "
         "amber needing repair), or 'pulse', 'heatmap', or 'columns_3d' with value_field "
         "floors or height_m.",
         obj({
             "eoa": {"type": "string", "enum": list(eoa.EOAS),
                     "description": "Which district's register (default all four)."},
             "status": {"type": "string", "enum": STATUSES},
             "area": {"type": "string",
                      "description": "Municipality, community or parish, e.g. 'Strovolos', "
                                     "'ΑΓΛΑΝΤΖΙΑ', 'Paralimni'."},
             "street": {"type": "string", "description": "Street name in the address."},
             **NEAR,
             "min_floors": {"type": "integer", "minimum": 1, "maximum": 60},
             "min_height_m": {"type": "number", "minimum": 0, "maximum": 300},
             "sort": {"type": "string", "enum": SORTS,
                      "description": "Order of the listed items: 'height' (tallest first, the "
                                     "default without a place), 'floors', 'volume' (the most "
                                     "building that could fail), 'distance' (nearest first, the "
                                     "default with a place) or 'declared' (newest first; only "
                                     "Larnaca dates its declarations)."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                       "description": "How many buildings to list in the reply (default 12). "
                                      "The layer and data_ref always hold every match."},
         })),
    tool("where_am_i",
         "Where the user is, from the position they shared (the locate button, or 'I'm here' "
         "on the map): the address, the nearest places, and the dangerous-register buildings "
         "around them with distances. Returns a layer_ref (the user and those buildings; show "
         "it with 'buildings_3d') and a data_ref. Give lat/lon to describe another point.",
         obj({
             "lat": {"type": "number"},
             "lon": {"type": "number"},
             "radius_m": {"type": "number", "minimum": 50, "maximum": 2000,
                          "description": "How far to look for dangerous buildings (default 300)."},
         })),
    tool("get_buildings_3d",
         "The Department of Lands and Surveys (DLS) 3D buildings model around a place: every "
         "building's roof and eave height above ground (m), floors, use (e.g. 'High "
         "residential building'), roof type, volume, footprint and ground height above sea "
         "level. Returns height and floor statistics, counts by use, roof type and height "
         "band, built-up share of the circle, the tallest buildings, any dangerous-register "
         "buildings among them, a layer_ref (show it with style 'buildings_3d': footprints "
         "extruded at real heights and coloured by height, dangerous ones red) and a data_ref "
         "(one row per building) for charts. In the walled old town DLS models whole blocks "
         "as one building.",
         obj({
             **{k: v for k, v in NEAR.items() if k != "radius_m"},
             "radius_m": {"type": "number", "minimum": 20, "maximum": 800,
                          "description": "Radius in metres (default 300)."},
             "min_height_m": {"type": "number", "minimum": 0, "maximum": 300},
             "min_floors": {"type": "integer", "minimum": 1, "maximum": 60},
             "use": {"type": "string",
                     "description": "Only buildings whose use contains this, e.g. 'residential', "
                                    "'commercial', 'church', 'industrial'."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 30,
                       "description": "How many of the tallest to list (default 10)."},
         })),
    tool("show_on_map",
         "Draw a layer from a data tool (its layer_ref) on the user's map. Styles for point "
         "layers: 'points' (coloured by state), 'pulse' (animated halo, to highlight a few "
         "important features), 'heatmap' (density weighted by value_field) and 'columns_3d' (3D "
         "bars whose height is value_field; tilts the map). Route layers use 'route', area "
         "layers use 'area'. Building layers (find_dangerous_buildings, get_buildings_3d) also "
         "take 'buildings_3d': the real footprints extruded at their DLS heights, with plot "
         "outlines; it tilts the map. Use fit to zoom the map to the layer. Show a layer "
         "whenever something has a location; the map is half of the answer.",
         obj({
             "layer_ref": {"type": "string"},
             "style": {"type": "string", "enum": POINT_STYLES + ["buildings_3d", "route", "area"]},
             "value_field": {"type": "string",
                             "description": "Numeric property for heatmap/columns_3d, e.g. "
                                            "level_pct, noise_db, caqi, power_w, people, total."},
             "title": {"type": "string", "description": "Legend title."},
             "fit": {"type": "boolean", "description": "Zoom to the layer (default true)."},
             "labels": {"type": "boolean", "description": "Show text labels (good for < 20 features)."},
         }, ["layer_ref", "style"])),
    tool("map_fly_to",
         "Move the map camera: to a place name or coordinates, with zoom (11 city, 14 district, "
         "16 street, 18 building), pitch (0-70 degrees, tilt for 3D) and bearing. city_3d true "
         "swaps the base map's buildings for the DLS 3D city model (real heights for every "
         "building in Cyprus, coloured by height, dangerous-register buildings in red); it "
         "draws from zoom 15, so pair it with zoom 16-17 and pitch 55-65.",
         obj({
             "place": {"type": "string"},
             "lat": {"type": "number"},
             "lon": {"type": "number"},
             "zoom": {"type": "number", "minimum": 3, "maximum": 20},
             "pitch": {"type": "number", "minimum": 0, "maximum": 70},
             "bearing": {"type": "number", "minimum": -180, "maximum": 180},
             "city_3d": {"type": "boolean",
                         "description": "Turn the DLS 3D city model on (true) or off (false)."},
         })),
    tool("map_callouts",
         "Pin short labelled callouts on the map (up to 12), e.g. to point out sights on a tour "
         "or explain what a highlight means.",
         obj({
             "items": {"type": "array", "minItems": 1, "maxItems": 12, "items": obj({
                 "lat": {"type": "number"}, "lon": {"type": "number"},
                 "title": {"type": "string"}, "text": {"type": "string"},
             }, ["lat", "lon", "title"])},
             "clear_previous": {"type": "boolean"},
         }, ["items"])),
    tool("map_clear",
         "Remove one layer (layer_ref) or, without arguments, every layer and callout.",
         obj({"layer_ref": {"type": "string"}})),
    tool("show_chart",
         "Render a chart in the chat from a Vega-Lite v6 spec. Pass data_ref from a data tool "
         "and leave `data` out of the spec: the server fills it from the dataset, so every "
         "number comes from the tools. Use any Vega-Lite feature (layer, facet, concat, "
         "transform, timeUnit, selections). Keep axis titles readable and put units in titles. "
         "Single-view and layered charts are linked to the map automatically: rows that carry a "
         "device id or camera highlight on the map when hovered (and the other way round). Any "
         "chart whose x is temporal (type temporal, e.g. a `time` or `hour` field) gets a red "
         "line at the time-lapse's moment while one plays, and at the current time otherwise; "
         "hovering it moves the time-lapse. Keep the id or camera field unaggregated in the "
         "linked view for this to work.",
         obj({
             "title": {"type": "string"},
             "spec": {"type": "object", "description": "Vega-Lite spec without data."},
             "data_ref": {"type": "string"},
         }, ["title", "spec"])),
    tool("show_visual",
         "Draw a custom visual in the chat by writing JavaScript, for what Vega-Lite can't "
         "express well: radial clocks, sankeys, chord or network graphs, calendars, ridgelines, "
         "3D scenes, animations, bespoke SVG. Prefer show_chart for ordinary charts. The code "
         "runs in a locked sandbox (no network, no access to the page) as the body of an async "
         "function, with these globals: root (an empty div to draw in), WIDTH and HEIGHT (px), "
         "DATA[data_ref] (the rows of each data_refs entry; DATA_LIST holds them in order), "
         "LAYERS[layer_ref] (GeoJSON of each layer_refs entry; a get_history layer also has "
         "`frames`), COLORS (state colours, e.g. COLORS.on, COLORS.full), THEME {text, muted, "
         "grid, accent, categorical[], ramp[]}, highlight(value) (highlights the device or "
         "camera with that id on the map and in linked charts, or jumps the time-lapse to that "
         "frame number; null clears) and onHighlight(fn) (fn(value) runs when the user hovers "
         "something on the map or in a chart). Libraries load as globals: d3 (v7), Plot "
         "(Observable Plot 0.6), echarts (5.6), THREE (r159). The card is white. Read every "
         "number from DATA or LAYERS, never type data into the code. Runtime errors come back in "
         "the next turn's <context> as visual_errors.",
         obj({
             "title": {"type": "string"},
             "code": {"type": "string", "minLength": 10, "maxLength": 30000},
             "data_refs": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
             "layer_refs": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
             "libraries": {"type": "array", "uniqueItems": True,
                           "items": {"type": "string", "enum": VISUAL_LIBRARIES}},
             "height": {"type": "integer", "minimum": 120, "maximum": 900,
                        "description": "Initial height in px (default 380); the card grows to "
                                       "fit what you draw."},
         }, ["title", "code"])),
]

WEB_SEARCH = {
    "type": "web_search_20260209", "name": "web_search", "max_uses": 3,
    # No "country": the API rejects "CY" as an unsupported country code for web search.
    "user_location": {"type": "approximate", "city": "Nicosia", "timezone": TIMEZONE},
}

SCHEMAS = {t["name"]: t["input_schema"] for t in TOOL_DEFS}


# -- shared helpers -----------------------------------------------------------------------------


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


# -- data tools ---------------------------------------------------------------------------------


def h_city_overview(ctx: ToolContext, a: dict) -> dict:
    return {"as_of": local_now(ctx).strftime("%Y-%m-%d %H:%M"), **ctx.city.overview(),
            "_summary": "city overview"}


def filter_by_place(ctx: ToolContext, states: list, a: dict):
    """Apply the `street` and near_* filters. Returns (states, center or None, distance by id),
    nearest first when a center is given."""
    if a.get("street"):
        q = norm(a["street"])
        states = [s for s in states if q in norm(s.street) or q in norm(s.label)]
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    dist: dict[str, float] = {}
    if center:
        radius = a.get("radius_m") or 500
        for s in states:
            if s.device.lat is not None:
                dist[s.device.name] = haversine_m(center[0], center[1], s.device.lat, s.device.lon)
        states = [s for s in states if dist.get(s.device.name, 1e12) <= radius]
        states.sort(key=lambda s: dist[s.device.name])
    return states, center, dist


def h_find_devices(ctx: ToolContext, a: dict) -> dict:
    kind = a["kind"]
    states = ctx.city.states(KINDS[kind])
    total = len(states)
    stale_dropped = 0
    if kind != "street_light" and not a.get("include_stale"):
        fresh = [s for s in states if not s.interp.stale]
        stale_dropped = len(states) - len(fresh)
        states = fresh
    if a.get("state"):
        want = a["state"].strip().lower()
        if want not in KIND_STATES[kind]:
            raise ToolError(f"state for {kind} must be one of {KIND_STATES[kind]}")
        states = [s for s in states if s.interp.state == want]
    states, center, dist = filter_by_place(ctx, states, a)

    features, rows = [], []
    for s in states:
        summ = s.summary()
        if center:
            summ["distance_m"] = round(dist[s.device.name])
        rows.append(summ)
        if s.device.lat is None:
            continue
        popup = f"{s.label}\n{s.interp.state} · {s.interp.reason}"
        if summ.get("as_of"):
            popup += f"\nas of {summ['as_of']}"
        features.append(point(s.device.lon, s.device.lat, {
            "id": s.device.name, "label": s.street or s.label, "state": s.interp.state,
            "color": STATE_COLORS.get(s.interp.state, STATE_COLORS["unknown"]), "popup": popup,
            **{k: v for k, v in s.values().items() if v is not None},
        }))
    by_state = Counter(s.interp.state for s in states)
    title = kind.replace("_", " ") + "s" + (f": {a['state']}" if a.get("state") else "")
    if center:
        title += f" near {center[2]}"
    layer = ctx.store.add_layer(f"{kind}_{a.get('state', 'all')}", features, geometry="point",
                                title=title, legend=legend_for(list(by_state)),
                                value_fields=sorted({k for s in states for k in s.values()}))
    data = ctx.store.add_dataset(kind, rows, title=title, link={"key": "id"})
    limit = a.get("limit") or 12
    age = ctx.city.snapshot_age_s(KINDS[kind])
    result = {
        "kind": kind, "devices_of_this_kind": total, "matched": len(states),
        "by_state": dict(by_state.most_common()),
        "left_out_stale": stale_dropped,
        "readings_fetched_min_ago": round(age / 60, 1) if age is not None else None,
        "layer_ref": layer, "data_ref": data,
        "items": rows[:limit],
    }
    if center:
        result["near"] = {"name": center[2], "lat": center[0], "lon": center[1],
                          "radius_m": a.get("radius_m") or 500}
    result["_summary"] = f"{len(states)} of {total} {kind.replace('_', ' ')}s"
    return result


def h_get_history(ctx: ToolContext, a: dict) -> dict:
    kind = a["kind"]
    device_type = KINDS[kind]
    hours = a.get("hours") or 24
    step = a.get("step_minutes") or (15 if hours <= 24 else 30 if hours <= 48 else 60)
    if hours * 60 // step > history.MAX_FRAMES:
        raise ToolError(f"{hours} h every {step} min is more than {history.MAX_FRAMES} frames; "
                        "use a larger step_minutes")
    states, center, _ = filter_by_place(ctx, ctx.city.states(device_type), a)
    states = [s for s in states if s.device.lat is not None]
    if not states:
        raise ToolError(f"no {kind.replace('_', ' ')}s match those filters")
    now = ctx.city.now()
    times = history.frame_times(now, hours, step)
    start_ms = int((times[0] - history.LOOKBACK[device_type]).timestamp() * 1000)
    records = ctx.city.history(device_type, [s.device.name for s in states],
                               history.HISTORY_KEYS[device_type], start_ms,
                               int(now.timestamp() * 1000))
    states = [s for s in states if s.device.name in records]
    frame_states, frame_values = history.replay(device_type, states, records, times)
    fields = [f for f, rows in frame_values.items()
              if any(v is not None for row in rows for v in row)]
    names = sorted({st for row in frame_states for st in row})
    index = {n: i for i, n in enumerate(names)}
    frames = {
        "times": [t.astimezone(TZ).strftime("%a %d %b %H:%M") for t in times],
        "iso": [t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") for t in times],
        "state_names": names,
        "states": [[index[st] for st in row] for row in frame_states],
        "values": {f: frame_values[f] for f in fields},
    }
    last = frame_states[-1]
    features = [point(s.device.lon, s.device.lat, {
        "id": s.device.name, "label": s.street or s.label, "name": s.label, "state": last[j],
        "color": STATE_COLORS.get(last[j], STATE_COLORS["unknown"]),
        **{f: frame_values[f][-1][j] for f in fields if frame_values[f][-1][j] is not None},
    }) for j, s in enumerate(states)]
    title = f"{kind.replace('_', ' ')}s, last {hours} h" + (f" near {center[2]}" if center else "")
    layer = ctx.store.add_layer(f"{kind}_history", features, geometry="point", title=title,
                                legend=legend_for(names), value_fields=fields, frames=frames)
    rows = history.frame_rows(device_type, times, frame_states, frame_values)
    data = ctx.store.add_dataset(f"{kind}_history", rows, title=title,
                                 link={"key": "frame", "layer": layer})
    every = max(1, len(rows) // 24)
    sampled = rows[::every] + ([rows[-1]] if (len(rows) - 1) % every else [])
    return {
        "kind": kind, "devices": len(states), "hours": hours, "step_minutes": step,
        "frames": len(times), "from": rows[0]["label"], "to": rows[-1]["label"],
        "value_fields": fields, "layer_ref": layer, "data_ref": data,
        "rows_sampled": sampled,
        "_summary": f"{len(states)} {kind.replace('_', ' ')}s × {len(times)} frames",
    }


def h_get_device(ctx: ToolContext, a: dict) -> dict:
    try:
        state, attrs = ctx.city.device_state(a["id"].strip())
    except KeyError:
        raise ToolError(f"no device named {a['id']!r} (ids look like YL1015, YP974, YW1394)")
    keep = ("name", "street", "city", "location", "postalcode", "lastActivityTime",
            "lastConnectTime", "lastDisconnectTime", "metadata", "make", "model")
    return {**state.summary(), "detail": state.interp.detail,
            "attributes": {k: v for k, v in attrs.items() if k in keep},
            "_summary": state.device.name}


def h_get_alarms(ctx: ToolContext, a: dict) -> dict:
    res = context.alarms(ctx.city, a.get("limit") or 20)
    feats = [point(x["lon"], x["lat"], {
        "id": x["device"], "label": x["alarm"], "state": "alarm", "color": STATE_COLORS["alarm"],
        "popup": f"{x['device']} ({x['device_type']})\n{x['alarm']} · {x['severity']}\nsince {x['since']}",
    }) for x in res["alarms"] if x["lat"] is not None]
    res["layer_ref"] = ctx.store.add_layer("alarms", feats, geometry="point", title="Active alarms",
                                           legend={"alarm": STATE_COLORS["alarm"]})
    res["_summary"] = f"{len(res['alarms'])} alarms"
    return res


def h_get_traffic(ctx: ToolContext, a: dict) -> dict:
    res = context.traffic(ctx.city, a["period"], a["group_by"], a.get("camera"))
    if "error" in res:
        raise ToolError(f"{res['error']}; cameras: {', '.join(res['cameras'][:40])}")
    res["data_ref"] = ctx.store.add_dataset(
        f"traffic_{a['group_by']}", res["rows"], title=f"Traffic {a['period']} by {a['group_by']}",
        link={"key": "camera"} if a["group_by"] == "camera" else None)
    if a["group_by"] == "camera":
        feats = [point(r["lon"], r["lat"], {
            "id": r["camera"], "label": r["camera"], "state": "camera",
            "color": STATE_COLORS["camera"],
            "popup": f"{r['camera']}\npeople {r.get('people', 0):,} · cars {r.get('car', 0):,}",
            **{k: v for k, v in r.items() if isinstance(v, int)},
        }) for r in res["rows"] if r.get("lat") is not None]
        res["layer_ref"] = ctx.store.add_layer(
            "traffic_cameras", feats, geometry="point", title=f"Traffic counts, {a['period']}",
            legend={"camera": STATE_COLORS["camera"]},
            value_fields=["people", "bike", "car", "motorcycle", "bus", "truck", "total"])
        res["rows"] = res["rows"][:15]
    res["_summary"] = f"traffic {a['period']} by {a['group_by']}"
    return res


def h_get_speed(ctx: ToolContext, a: dict) -> dict:
    res = context.speed(ctx.city, a.get("hours") or 12)
    res["data_ref"] = ctx.store.add_dataset("speed", res["rows"], title="City average speed")
    res["_summary"] = f"{len(res['rows'])} hours of speed"
    return res


def h_get_weather(ctx: ToolContext, a: dict) -> dict:
    res = context.weather(ctx.settings)
    res["data_ref"] = ctx.store.add_dataset("weather", res["next_12_hours"], title="Next 12 hours")
    res["_summary"] = f"{res['now']['temp_c']} °C, {res['now']['sky']}"
    return res


def h_geocode(ctx: ToolContext, a: dict) -> dict:
    hits = ctx.places.geocode(a["query"])
    if not hits:
        raise ToolError(f"nothing found for {a['query']!r}; try another spelling or a nearby landmark")
    return {"results": hits, "_summary": hits[0].get("name") or a["query"]}


def h_find_places(ctx: ToolContext, a: dict) -> dict:
    if not a.get("category") and not a.get("query"):
        raise ToolError("give a category or a query")
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    near = (center[0], center[1]) if center else None
    radius = (a.get("radius_m") or 1000) if near else None
    places, source = ctx.places.find(a.get("category"), a.get("query"), near, radius, limit=200)
    feats = [point(p["lon"], p["lat"], {
        "id": p["id"], "label": p.get("name") or p["category"], "state": "place",
        "color": STATE_COLORS["place"],
        "popup": "\n".join(x for x in (p.get("name"), p["category"].replace("_", " "),
                                       p.get("opening_hours")) if x),
    }) for p in places]
    title = (a.get("category") or a.get("query") or "places").replace("_", " ")
    layer = ctx.store.add_layer(title, feats, geometry="point", title=title.capitalize(),
                                legend={"place": STATE_COLORS["place"]})
    limit = a.get("limit") or 15
    return {"source": source, "matched": len(places), "places": places[:limit],
            "layer_ref": layer, "local_time_now": local_now(ctx).strftime("%a %H:%M"),
            "_summary": f"{len(places)} {title}"}


def _hhmm(text: str | None, now: datetime) -> datetime:
    if not text:
        return now
    try:
        h, m = map(int, text.strip().split(":"))
    except ValueError:
        raise ToolError("start_time must be HH:MM")
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t >= now - timedelta(minutes=5) else t + timedelta(days=1)


def h_plan_route(ctx: ToolContext, a: dict) -> dict:
    mode = a["mode"]
    prefer = a.get("prefer") or []
    stops = []
    for i, s in enumerate(a["stops"]):
        if s.get("lat") is not None and s.get("lon") is not None:
            info = {"name": s.get("place") or f"Stop {i + 1}", "lat": s["lat"], "lon": s["lon"]}
        elif s.get("place"):
            info = resolve_place(ctx, s["place"])
        else:
            raise ToolError(f"stop {i + 1} needs a place or lat/lon")
        stops.append({"label": info.get("name") or s.get("place"), "lat": info["lat"],
                      "lon": info["lon"], "opening_hours": info.get("opening_hours"),
                      "minutes_here": s.get("minutes_here") or 0})
    coords = [(s["lat"], s["lon"]) for s in stops]
    return_to_start = bool(a.get("return_to_start"))
    order = list(range(len(stops)))
    seconds = None
    if a.get("optimize_order") and len(stops) >= 3:
        seconds = ctx.router.matrix(coords, mode, prefer)
        order = best_order(seconds, return_to_start, bool(a.get("keep_last_stop_last")))
    path = [coords[i] for i in order] + ([coords[0]] if return_to_start else [])
    route = ctx.router.route(path, mode, prefer)
    records = ctx.registers.records() if ctx.registers is not None and ctx.registers.ready else []
    # `usual` is the route picked without avoiding them (not always the shortest: other
    # preferences, like lit streets, weigh in too).
    usual, zones = None, []
    if "avoid_dangerous_buildings" in prefer and records:
        safer, zones = route_avoiding(ctx, path, mode, prefer, route, records)
        if safer is not route:
            usual, route = route, safer
    passed = passed_by(route["coordinates"], records)
    usual_passed = passed_by(usual["coordinates"], records) if usual else []

    start = _hhmm(a.get("start_time"), local_now(ctx))
    t = start
    plan = []
    for pos, i in enumerate(order):
        if pos:
            t += timedelta(minutes=route["legs"][pos - 1]["duration_min"])
        arrive = t
        t += timedelta(minutes=stops[i]["minutes_here"] if pos else 0)
        plan.append({"n": pos + 1, "label": stops[i]["label"], "arrive": arrive.strftime("%a %H:%M"),
                     "leave": t.strftime("%H:%M"), "lat": stops[i]["lat"], "lon": stops[i]["lon"],
                     **({"opening_hours": stops[i]["opening_hours"]}
                        if stops[i]["opening_hours"] else {})})
    if return_to_start:
        t += timedelta(minutes=route["legs"][-1]["duration_min"])

    feats = []
    if usual:  # drawn first, so the route to take sits on top of it
        feats.append({"type": "Feature", "geometry": {"type": "LineString",
                                                      "coordinates": usual["coordinates"]},
                      "properties": {"id": "route_usual", "state": "usual route",
                                     "color": STATE_COLORS["usual route"],
                                     "label": f"usual: {usual['distance_km']} km · "
                                              f"{usual['duration_min']:.0f} min"}})
    feats.append({"type": "Feature", "geometry": {"type": "LineString",
                                                  "coordinates": route["coordinates"]},
                  "properties": {"id": "route", "state": "route", "color": STATE_COLORS["route"],
                                 "label": f"{route['distance_km']} km · {route['duration_min']:.0f} min"}})
    feats += [point(p["lon"], p["lat"], {"id": f"stop{p['n']}", "label": f"{p['n']}. {p['label']}",
                                         "n": p["n"], "state": "stop", "color": STATE_COLORS["stop"],
                                         "popup": f"{p['n']}. {p['label']}\narrive {p['arrive']}"})
              for p in plan]
    # register buildings along either route, as red "!" markers
    flagged = {r["id"]: r for r, _ in passed + usual_passed}
    for r in flagged.values():
        spot = eoa.anchor(r)
        feats.append(point(spot[0], spot[1], {
            "id": r["id"], "label": "", "n": "!", "state": r["status"],
            "color": STATE_COLORS[r["status"]], "popup": eoa.popup(r)}))
    legend = {}
    if flagged or usual:
        legend = {"route": STATE_COLORS["route"],
                  **({"usual route": STATE_COLORS["usual route"]} if usual else {}),
                  **{s: STATE_COLORS[s] for s in STATUSES
                     if any(r["status"] == s for r in flagged.values())}}
    layer = ctx.store.add_layer(f"route_{mode}", feats, geometry="route",
                                title=f"{mode.capitalize()} route", legend=legend)
    visit_min = sum(s["minutes_here"] for s in stops[1:])
    res = {
        "mode": mode, "prefer": prefer, "optimized": bool(seconds),
        "travel_km": route["distance_km"], "travel_min": route["duration_min"],
        "visits_min": visit_min, "finish": t.strftime("%a %H:%M"),
        "stops_in_order": plan,
        "legs": [{"from": plan[k]["label"],
                  "to": plan[k + 1]["label"] if k + 1 < len(plan) else plan[0]["label"],
                  **leg} for k, leg in enumerate(route["legs"])],
        "layer_ref": layer,
    }
    if records:
        res["dangerous_passed"] = len(passed)
        res["dangerous_along_route"] = [
            {"id": r["id"], "status": r["status"], "address": r.get("address") or eoa.place_of(r),
             "height_m": r.get("height_m"), "metres_from_route": round(d)}
            for r, d in sorted(passed, key=lambda t: t[1])][:12]
    if "avoid_dangerous_buildings" in prefer:
        if usual:
            ids = {r["id"] for r, _ in passed}
            res["avoidance"] = {
                "usual_route": {"km": usual["distance_km"], "min": usual["duration_min"],
                                "dangerous_passed": len(usual_passed)},
                "this_route": {"km": route["distance_km"], "min": route["duration_min"],
                               "dangerous_passed": len(passed)},
                "avoided": len({r["id"] for r, _ in usual_passed} - ids),
                "minutes_added": round(route["duration_min"] - usual["duration_min"], 1),
                "km_added": round(route["distance_km"] - usual["distance_km"], 2),
                "what_usual_means": "the route picked without avoiding them, with the same "
                                    "other preferences; not always the shortest",
                "still_passed_note": "buildings within 40 m of a stop can't be avoided"
                if passed else None}
        elif not records:
            res["avoidance"] = {"note": "the dangerous-building registers are still loading"}
        elif not zones:
            res["avoidance"] = {"note": "the usual route already passes no register buildings "
                                        "(or only ones within 40 m of a stop)"}
        else:
            res["avoidance"] = {"note": "no way round them was found; this is the usual route"}
    if mode == "drive":
        try:
            sp = context.speed(ctx.city, 2)["rows"]
            if sp:
                res["city_traffic_now"] = sp[-1]
        except (ApiError, WebError) as e:
            log.info("speed lookup failed: %s", e)
    res["_summary"] = f"{route['distance_km']} km, {route['duration_min']:.0f} min {mode}"
    return res


def h_reachable_area(ctx: ToolContext, a: dict) -> dict:
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    if not center:
        raise ToolError("give a place or lat/lon")
    data = ctx.router.isochrone((center[0], center[1]), a["minutes"], a["mode"])
    palette = ["#16a34a", "#eab308", "#f97316", "#dc2626"]
    contours = sorted(data.get("features", []), key=lambda f: f["properties"].get("contour", 0))
    for k, f in enumerate(contours):
        m = f["properties"].get("contour")
        m = int(m) if m is not None and m == int(m) else m
        f["properties"] = {"id": f"iso{m}", "label": f"{m} min", "minutes": m,
                           "state": f"{m} min", "color": palette[min(k, 3)]}
    feats = contours[::-1]  # largest first, so the smaller areas draw on top
    feats.append(point(center[1], center[0], {"id": "origin", "label": center[2], "state": "place",
                                              "color": STATE_COLORS["place"], "popup": center[2]}))
    layer = ctx.store.add_layer("reachable", feats, geometry="area",
                                title=f"Reachable by {a['mode']} from {center[2]}",
                                legend={f["properties"]["label"]: f["properties"]["color"]
                                        for f in feats if "minutes" in f["properties"]})
    return {"origin": center[2], "minutes": a["minutes"], "mode": a["mode"], "layer_ref": layer,
            "_summary": f"{', '.join(map(str, a['minutes']))} min by {a['mode']}"}


# -- dangerous buildings and the DLS 3D model -----------------------------------------------------


def registers_ready(ctx: ToolContext) -> list[dict]:
    if ctx.registers is None:
        raise ToolError("the dangerous-building registers aren't connected in this app")
    records = ctx.registers.records(wait_s=90)
    if not records:
        raise ToolError("the registers are still loading (the first load takes about a minute); "
                        "try again shortly")
    return records


def h_find_dangerous_buildings(ctx: ToolContext, a: dict) -> dict:
    records = registers_ready(ctx)
    sel = [r for r in records
           if (not a.get("eoa") or r["eoa"] == a["eoa"])
           and (not a.get("status") or r["status"] == a["status"])
           and (not a.get("area") or any(loose_match(a["area"], r.get(k))
                                         for k in ("municipality", "community", "parish")))
           and (not a.get("street") or loose_match(a["street"], r.get("address"))
                or loose_match(a["street"], r.get("name")))
           and (r.get("floors") or 0) >= (a.get("min_floors") or 0)
           and (r.get("height_m") or 0) >= (a.get("min_height_m") or 0)]
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    dist: dict[str, float] = {}
    if center:
        radius = a.get("radius_m") or 1000
        dist = {r["id"]: haversine_m(center[0], center[1], r["lat"], r["lon"])
                for r in sel if r.get("lat") is not None}
        sel = [r for r in sel if dist.get(r["id"], 1e12) <= radius]
    order = a.get("sort") or ("distance" if center else "height")
    if order == "distance" and not center:
        raise ToolError("sort 'distance' needs near_place (or 'my location') or near_lat/near_lon")
    if order == "declared":  # newest first; undated ("") last
        sel = sorted(sel, key=lambda r: r.get("declared") or "", reverse=True)
    else:
        sel = sorted(sel, key={"distance": lambda r: dist[r["id"]],
                               "height": lambda r: -(r.get("height_m") or 0),
                               "floors": lambda r: -(r.get("floors") or 0),
                               "volume": lambda r: -(r.get("volume_m3") or 0)}[order])

    parts = [eoa.EOAS[a["eoa"]]["name_en"] if a.get("eoa") else "EOA registers"]
    parts += [a[k] for k in ("status", "area", "street") if a.get(k)]
    title = "Dangerous buildings: " + ", ".join(p.replace("_", " ") for p in parts)
    if center:
        title += f" near {center[2]}"
    rows = [eoa.row(r, dist.get(r["id"])) for r in sel]
    features = eoa.layer_features(sel, STATE_COLORS)
    layer = ctx.store.add_layer("dangerous", features, geometry="buildings", title=title,
                                legend=legend_for([s for s in STATUSES
                                                   if any(r["status"] == s for r in sel)]),
                                value_fields=["height_m", "floors", "volume_m3"],
                                count=len(sel))
    data = ctx.store.add_dataset("dangerous", rows, title=title, link={"key": "id"})
    heights = [r["height_m"] for r in sel if r.get("height_m") and not r.get("height_estimated")]
    floors = [r["floors"] for r in sel if r.get("floors")]
    limit = a.get("limit") or 12
    res = {
        "registers": {k: {"name": eoa.EOAS[k]["name"], "records": v.get("count"),
                          "copy": v.get("copy"), "as_of": v.get("as_of"),
                          "page": eoa.EOAS[k]["page"]}
                      for k, v in ctx.registers.sources.items()},
        "total_on_registers": len(records), "matched": len(sel),
        "by_eoa": dict(Counter(r["eoa"] for r in sel).most_common()),
        "by_status": dict(Counter(r["status"] for r in sel).most_common()),
        "top_areas": dict(Counter(eoa.place_of(r) for r in sel).most_common(12)),
        "with_3d_building": sum(1 for r in sel if r.get("footprints")),
        "not_on_map": sum(1 for r in sel if r.get("lat") is None),
        "height_m": eoa.summary(heights), "floors": eoa.summary(floors),
        "five_floors_or_more": sum(1 for f in floors if f >= 5),
        "layer_ref": layer, "data_ref": data,
        "sorted_by": order, "items": rows[:limit],
    }
    if not ctx.registers.linked_3d:
        res["note"] = "3D heights are still being linked; ask again in a few minutes for them"
    if center:
        res["near"] = {"name": center[2], "lat": center[0], "lon": center[1],
                       "radius_m": a.get("radius_m") or 1000}
    res["_summary"] = f"{len(sel)} of {len(records)} register buildings"
    return res


def h_where_am_i(ctx: ToolContext, a: dict) -> dict:
    if a.get("lat") is not None and a.get("lon") is not None:
        lat, lon, source, accuracy = a["lat"], a["lon"], "given point", None
    else:
        my_location(ctx)  # says how to share a location if there is none
        loc = ctx.user_location
        lat, lon = loc["lat"], loc["lon"]
        source, accuracy = loc.get("source"), loc.get("accuracy_m")
    radius = a.get("radius_m") or 300
    address = ctx.places.reverse(lat, lon) if ctx.places is not None else None
    places = []
    if ctx.places is not None and ctx.places.ready:
        found, _ = ctx.places.find(near=(lat, lon), radius_m=250, limit=5)
        places = [{"name": p.get("name"), "category": p["category"].replace("_", " "),
                   "distance_m": p["distance_m"]} for p in found]
    near = []
    if ctx.registers is not None and ctx.registers.ready:
        for r in ctx.registers.records():
            spot = eoa.anchor(r)
            if spot and (d := haversine_m(lat, lon, spot[1], spot[0])) <= radius:
                near.append((r, d))
    near.sort(key=lambda t: t[1])
    you = point(lon, lat, {"id": "you", "label": "You are here", "state": "you",
                           "color": STATE_COLORS["you"],
                           "popup": "You are here" + (f" (±{accuracy:.0f} m)" if accuracy else "")})
    records = [r for r, _ in near]
    layer = ctx.store.add_layer("where_am_i", [you, *eoa.layer_features(records, STATE_COLORS)],
                                geometry="buildings", title="Around you",
                                legend=legend_for(["you", *[s for s in STATUSES
                                                            if any(r["status"] == s for r in records)]]),
                                value_fields=["height_m", "floors"], count=len(records))
    rows = [eoa.row(r, d) for r, d in near]
    data = ctx.store.add_dataset("around_you", rows, title="Dangerous buildings around you",
                                 link={"key": "id"})
    in_walls = haversine_m(lat, lon, WALLED_CITY[0], WALLED_CITY[1]) < WALLED_CITY[2]
    return {
        "you_are_at": {"lat": lat, "lon": lon, "accuracy_m": accuracy, "source": source},
        "address": address, "inside_the_walled_city": in_walls, "nearest_places": places,
        "dangerous_within_m": radius, "dangerous_count": len(near),
        "by_status": dict(Counter(r["status"] for r in records).most_common()),
        "nearest_dangerous": rows[:8], "layer_ref": layer, "data_ref": data,
        "_summary": f"{len(near)} register buildings within {radius:g} m",
    }


def passed_by(path: list[list[float]], records: list[dict],
              within: float = PASS_BY_M) -> list[tuple[dict, float]]:
    """Register buildings whose plot is within `within` m of a route, with the distance."""
    if not path:
        return []
    pad = 0.0005
    w, e = min(p[0] for p in path) - pad, max(p[0] for p in path) + pad
    s, n = min(p[1] for p in path) - pad, max(p[1] for p in path) + pad
    out = []
    for r in records:
        spot = eoa.anchor(r)
        if spot and w <= spot[0] <= e and s <= spot[1] <= n:
            d = distance_to_path_m(path, *spot)
            if d <= within:
                out.append((r, d))
    return out


def route_avoiding(ctx: ToolContext, path: list[tuple[float, float]], mode: str,
                   prefer: list[str], first: dict, records: list[dict]):
    """Re-route around the streets in front of the register buildings a route passes: exclude
    them, re-route, and repeat for any the new route passes (3 rounds at most). Buildings
    within 40 m of a stop can't be avoided and stay. Returns (route, avoided ids)."""
    stops = [(lon, lat) for lat, lon in path]
    zones: dict[str, list] = {}
    current = first
    for _ in range(3):
        new = []
        for r, _d in passed_by(current["coordinates"], records):
            spot = eoa.anchor(r)
            if r["id"] in zones or any(haversine_m(spot[1], spot[0], y, x) < 40 for x, y in stops):
                continue
            new.append(r)
        if not new or len(zones) >= MAX_AVOID_ZONES:
            break
        for r in new[:MAX_AVOID_ZONES - len(zones)]:
            zone = eoa.avoid_zone(r)
            if zone and zone not in zones.values():
                zones[r["id"]] = zone
        try:
            current = ctx.router.route(path, mode, prefer, avoid=list(zones.values()))
        except WebError as e:  # the zones cut off every way through
            log.info("no way round the dangerous buildings: %s", e)
            break
    return current, list(zones)


def height_band(h: float) -> tuple[str, str]:
    return next((label, color) for top, label, color in HEIGHT_BANDS if h < top)


def h_get_buildings_3d(ctx: ToolContext, a: dict) -> dict:
    if ctx.dls is None:
        raise ToolError("the DLS 3D buildings model isn't connected in this app")
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if not center:
        raise ToolError("give near_place or near_lat and near_lon")
    radius = a.get("radius_m") or 300
    near = ctx.dls.buildings_near(center[0], center[1], radius)
    near = [(b, d) for b, d in near
            if b["properties"]["h"] >= (a.get("min_height_m") or 0)
            and (b["properties"].get("f") or 0) >= (a.get("min_floors") or 0)
            and (not a.get("use") or norm(a["use"]) in norm(b["properties"].get("t")))]
    capped = len(near) > MAX_BUILDINGS
    near = near[:MAX_BUILDINGS]
    on_register: dict[int, dict] = {}
    if ctx.registers is not None:
        for r in ctx.registers.records():
            for fp in r.get("footprints") or []:
                if r.get("match_3d") != "block":
                    on_register[fp["bid"]] = r

    features, rows = [], []
    for b, d in near:
        p = b["properties"]
        band, color = height_band(p["h"])
        rec = on_register.get(b["id"])
        state = rec["status"] if rec else band
        popup = [f"{p.get('t') or 'Building'} · DLS 3D #{b['id']}",
                 f"{p['h']} m to the roof, eave {p['e']} m"
                 + (f", {p['f']} floors" if p.get("f") else ""),
                 " · ".join(x for x in (p.get("r"), p.get("v") and f"{p['v']:,.0f} m³",
                                        p.get("a") and f"{p['a']:,.0f} m² footprint") if x)]
        if p.get("g") is not None:
            popup.append(f"ground {p['g']} m above sea level")
        if rec:
            popup.append(f"On the {eoa.EOAS[rec['eoa']]['name']} register: "
                         f"{eoa.STATUS_GR[rec['status']]} ({rec['id']})")
        features.append({"type": "Feature", "geometry": b["geometry"], "properties": {
            "id": f"B{b['id']}", "bid": b["id"], "_kind": "footprint",
            "label": p.get("t") or "building", "state": state,
            "color": STATE_COLORS[rec["status"]] if rec else color,
            "height_m": p["h"], "eave_m": p["e"], "floors": p.get("f"),
            "popup": "\n".join(x for x in popup if x)}})
        rows.append({k: v for k, v in {
            "id": f"B{b['id']}", "height_m": p["h"], "eave_m": p["e"], "floors": p.get("f"),
            "use": p.get("t"), "roof": p.get("r"), "volume_m3": p.get("v"),
            "footprint_m2": p.get("a"), "ground_m": p.get("g"), "distance_m": round(d),
            "height_band": band, "register": rec["status"] if rec else None,
            "register_id": rec["id"] if rec else None}.items() if v is not None})

    title = f"DLS 3D buildings within {radius:g} m of {center[2]}"
    legend = {label: color for _, label, color in HEIGHT_BANDS
              if any(r["height_band"] == label for r in rows)}
    legend.update({s: STATE_COLORS[s] for s in STATUSES if any(r.get("register") == s for r in rows)})
    layer = ctx.store.add_layer("buildings_3d", features, geometry="buildings", title=title,
                                legend=legend, value_fields=["height_m", "floors", "eave_m"])
    data = ctx.store.add_dataset("buildings_3d", rows, title=title, link={"key": "id"})
    footprint = sum(b["properties"].get("a") or area_m2(b["geometry"]) for b, _ in near)
    limit = a.get("limit") or 10
    res = {
        "center": {"name": center[2], "lat": center[0], "lon": center[1]}, "radius_m": radius,
        "buildings": len(near), "capped_at": MAX_BUILDINGS if capped else None,
        "height_m": eoa.summary([r["height_m"] for r in rows]),
        "floors": eoa.summary([r["floors"] for r in rows if r.get("floors")]),
        "by_height_band": dict(Counter(r["height_band"] for r in rows).most_common()),
        "by_use": dict(Counter(r.get("use") or "?" for r in rows).most_common(8)),
        "by_roof": dict(Counter(r.get("roof") or "?" for r in rows).most_common(6)),
        "built_up_pct": round(100 * footprint / (math.pi * radius ** 2), 1),
        "total_volume_m3": round(sum(r.get("volume_m3") or 0 for r in rows)),
        "tallest": sorted(rows, key=lambda r: -r["height_m"])[:limit],
        "on_dangerous_registers": [{"id": r["register_id"], "status": r["register"],
                                    "height_m": r["height_m"], "building": r["id"]}
                                   for r in rows if r.get("register_id")][:20],
        "layer_ref": layer, "data_ref": data,
        "source": "DLS INSPIRE BU3 3D buildings (Department of Lands and Surveys, Cyprus)",
    }
    if haversine_m(center[0], center[1], WALLED_CITY[0], WALLED_CITY[1]) < WALLED_CITY[2] + radius:
        res["note"] = ("inside the walls DLS models whole blocks as one building, so a block's "
                       "height is its tallest part")
    res["_summary"] = f"{len(near)} buildings, tallest {res['height_m']['max'] if rows else 0} m"
    return res


# -- UI tools ------------------------------------------------------------------------------------


def h_show_on_map(ctx: ToolContext, a: dict) -> dict:
    ref = a["layer_ref"]
    layer = ctx.store.layers.get(ref)
    if layer is None:
        raise ToolError(f"unknown layer_ref {ref!r}; known: {list(ctx.store.layers)[-10:]}")
    meta = layer["meta"]
    style = a["style"]
    allowed = STYLES_FOR[meta["geometry"]]
    if style not in allowed:
        raise ToolError(f"layer {ref} is a {meta['geometry']} layer; use style {allowed}")
    vf = a.get("value_field")
    if style in ("heatmap", "columns_3d") and vf and meta["value_fields"] and vf not in meta["value_fields"]:
        raise ToolError(f"value_field must be one of {meta['value_fields']}")
    if style == "columns_3d" and not vf:
        raise ToolError(f"columns_3d needs value_field, one of {meta['value_fields']}")
    ctx.emit({"type": "ui", "action": "show_layer", "layer": {
        "ref": ref, "url": f"/api/layers/{ctx.conversation_id}/{ref}", "style": style,
        "value_field": vf, "title": a.get("title") or meta["title"],
        "fit": a.get("fit", True), "labels": a.get("labels", meta["count"] <= 20),
        "legend": meta["legend"], "count": meta["count"], "timeseries": bool(meta["frames"]),
    }})
    res = {"ok": True, "shown": ref, "features": meta["count"], "_summary": f"map: {ref}"}
    if meta["frames"]:
        res["timelapse"] = f"{meta['frames']} frames, playing with a time slider on the map"
    return res


def h_map_fly_to(ctx: ToolContext, a: dict) -> dict:
    center = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    if a.get("city_3d") is not None:
        ctx.emit({"type": "ui", "action": "city_3d", "on": a["city_3d"]})
    cmd = {"type": "ui", "action": "fly_to"}
    if center:
        cmd["center"] = [center[1], center[0]]
    for k in ("zoom", "pitch", "bearing"):
        if a.get(k) is not None:
            cmd[k] = a[k]
    if len(cmd) > 2:
        ctx.emit(cmd)
    res = {"ok": True, **({"center": center[2]} if center else {}), "_summary": "map: fly"}
    if a.get("city_3d") is not None:
        res["city_3d"] = "on" if a["city_3d"] else "off"
        if a["city_3d"]:
            res["note"] = "the 3D city draws from zoom 15; buildings stream in as the view moves"
    return res


def h_map_callouts(ctx: ToolContext, a: dict) -> dict:
    ctx.emit({"type": "ui", "action": "callouts", "items": a["items"],
              "clear_previous": a.get("clear_previous", True)})
    return {"ok": True, "callouts": len(a["items"]), "_summary": f"{len(a['items'])} callouts"}


def h_map_clear(ctx: ToolContext, a: dict) -> dict:
    ctx.emit({"type": "ui", "action": "clear", "ref": a.get("layer_ref")})
    return {"ok": True, "_summary": "map: clear"}


# -- charts --------------------------------------------------------------------------------------

VEGA_LITE_SCHEMA_URL = "https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/build/vega-lite-schema.json"
_vl_validator = None
_vl_lock = threading.Lock()


def vega_lite_validator(settings: Settings):
    """The Vega-Lite JSON Schema validator, downloaded once and cached (None if unavailable)."""
    global _vl_validator
    with _vl_lock:
        if _vl_validator is not None:
            return _vl_validator or None
        path = CACHE_DIR / "vega-lite-6.5.0-schema.json"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(get_json(VEGA_LITE_SCHEMA_URL,
                                                    user_agent=settings.user_agent, timeout=30)),
                                encoding="utf-8")
            schema = json.loads(path.read_text(encoding="utf-8"))
            _vl_validator = jsonschema.Draft7Validator(schema)
        except (OSError, ValueError, WebError) as e:
            log.warning("Vega-Lite schema unavailable, charts get light checks only: %s", e)
            _vl_validator = False
        return _vl_validator or None


COMPOSITE = ("mark", "layer", "concat", "hconcat", "vconcat", "facet", "repeat", "spec")
# Keys that belong to a single view when a unit spec is turned into a layer.
UNIT_KEYS = ("mark", "encoding", "params", "transform", "projection")
FRAME_FIELDS = {"frame", "label"}  # non-temporal x fields of get_history rows
NOW_RULE = {"type": "rule", "color": "#dc2626", "strokeWidth": 2}


def spec_error(validator, spec: dict, has_data_ref: bool) -> str | None:
    """The most specific Vega-Lite schema error, or None if the spec is valid."""
    # Validate with a named placeholder instead of the rows, so errors quote the spec only.
    probe = {**spec, "data": {"name": "rows"}} if has_data_ref or "data" not in spec else spec
    err = best_match(validator.iter_errors(probe))
    if err is None:
        return None
    while err.context:
        deeper = best_match(err.context)
        if deeper is None:
            break
        err = deeper
    where = "/".join(map(str, err.absolute_path)) or "(root)"
    return f"invalid Vega-Lite spec at {where}: {err.message[:240]}"


def first_unit(spec: dict) -> dict | None:
    """The view to wire: the spec itself if it is a single view, else its first layer with an
    encoding. Facet and concat specs aren't wired."""
    if "layer" in spec:
        layers = spec["layer"] if isinstance(spec["layer"], list) else []
        return next((u for u in layers if isinstance(u, dict) and "mark" in u
                     and isinstance(u.get("encoding"), dict)), None)
    return spec if "mark" in spec and isinstance(spec.get("encoding"), dict) else None


def param_names(spec: dict, unit: dict) -> set:
    return {p.get("name") for p in spec.get("params", []) + unit.get("params", [])
            if isinstance(p, dict)}


def add_layer(spec: dict, unit: dict, layer: dict) -> None:
    """Append a layer, turning a single-view spec into [the view, layer] first. Selection
    params go with the view; variable params (like `_hl`) must stay at the top of a layer spec."""
    if unit is spec:
        view = {k: spec.pop(k) for k in UNIT_KEYS if k in spec}
        params = view.pop("params", [])
        selections = [p for p in params if isinstance(p, dict) and "select" in p]
        if selections:
            view["params"] = selections
        if rest := [p for p in params if p not in selections]:
            spec["params"] = rest
        spec["layer"] = [view]
    spec["layer"].append(layer)


def is_temporal(x: object) -> bool:
    return isinstance(x, dict) and bool(x.get("field")) and (
        x.get("type") == "temporal" or "timeUnit" in x)


def link_chart(spec: dict, key: str) -> bool:
    """Wire a chart to the map, in place: the page sets the `_hl` param to the value hovered on
    the map (or the time-lapse frame), and the other marks fade, or, for a time-lapse chart
    over frames or frame labels, a rule marks the frame. (Charts over real time follow the
    time-lapse through add_time_rule instead; the page reads hovers from Vega's pointer events.)
    Only single-view and layered specs are wired; returns whether this one was."""
    unit = first_unit(spec)
    if unit is None or "_hl" in param_names(spec, unit):
        return False
    x = unit["encoding"].get("x")
    if key == "frame" and is_temporal(x):
        return False
    if key == "frame" and isinstance(x, dict) and x.get("field") in FRAME_FIELDS:
        add_layer(spec, unit, {
            "mark": NOW_RULE, "transform": [{"filter": "_hl != null && datum.frame === _hl"}],
            "encoding": {"x": {k: x[k] for k in ("field", "type", "sort") if k in x}}})
    elif "opacity" not in unit["encoding"]:
        k = json.dumps(key)
        unit["encoding"]["opacity"] = {
            "condition": {"test": f"_hl == null || datum[{k}] == null || datum[{k}] === _hl",
                          "value": 1},
            "value": 0.2}
    spec.setdefault("params", []).append({"name": "_hl", "value": None})
    return True


def add_time_rule(spec: dict) -> str | None:
    """Give a chart over time a vertical rule at the `_t` param (epoch ms), in place. The page
    sets `_t` to the time-lapse's moment while one plays and to the current time otherwise, or
    to null when that moment is outside the chart. The rule reuses the x field's name, type,
    timeUnit and title, so the axis and its title stay as they were. Returns the x field."""
    unit = first_unit(spec)
    if unit is None or "_t" in param_names(spec, unit):
        return None
    x = unit["encoding"].get("x")
    if not is_temporal(x):
        return None
    field = x["field"]
    add_layer(spec, unit, {
        "data": {"values": [{}]},
        "transform": [{"filter": "_t != null"}, {"calculate": "_t", "as": field}],
        "mark": NOW_RULE,
        "encoding": {"x": {k: x[k] for k in ("field", "type", "timeUnit", "title") if k in x}}})
    spec.setdefault("params", []).append({"name": "_t", "value": None})
    return field


BAND_COLORS = {label: color for _, label, color in HEIGHT_BANDS}
NAMED_COLORS = {**STATE_COLORS, **BAND_COLORS}
COLOR_ORDERS = [*KIND_STATES.values(), STATUSES, list(BAND_COLORS)]
COLOR_FIELDS = ("state", "status", "register", "height_band")


def color_states(spec: dict, rows: list[dict]) -> None:
    """Colour a state-like field (state, status, register, height_band) with the map's colours
    (in place), so a full bin or a dangerous building is the same red in the chart as on the
    map. Leaves any scale the model chose alone."""
    for unit in [spec, *[u for u in spec.get("layer", []) if isinstance(u, dict)]]:
        color = (unit.get("encoding") or {}).get("color")
        if not (isinstance(color, dict) and color.get("field") in COLOR_FIELDS
                and "scale" not in color):
            continue
        field = color["field"]
        present = [s for s in dict.fromkeys(r.get(field) for r in rows) if s in NAMED_COLORS]
        if not present:
            continue
        order = next((v for v in COLOR_ORDERS if set(present) <= set(v)), present)
        values = [s for s in order if s in present]
        color["scale"] = {"domain": values, "range": [NAMED_COLORS[s] for s in values]}


def h_show_chart(ctx: ToolContext, a: dict) -> dict:
    spec = copy.deepcopy(a["spec"])  # never touch the tool input: it stays in the history
    if not any(k in spec for k in COMPOSITE):
        raise ToolError(f"spec needs one of {COMPOSITE}")
    spec.setdefault("$schema", "https://vega.github.io/schema/vega-lite/v6.json")
    if "mark" in spec or "layer" in spec:  # facet/concat/repeat specs don't take a width
        spec.setdefault("width", "container")
    validator = vega_lite_validator(ctx.settings)
    has_ref = bool(a.get("data_ref"))
    if validator is not None and (err := spec_error(validator, spec, has_ref)):
        raise ToolError(err)
    def wired(spec: dict, wire: Callable[[dict], object]) -> tuple[dict, object]:
        """Apply `wire` to a copy; keep it only if it did something and the spec stays valid."""
        trial = copy.deepcopy(spec)
        result = wire(trial)
        if result and (validator is None or not spec_error(validator, trial, True)):
            return trial, result
        return spec, None

    rows = link = time_field = None
    if has_ref:
        ds = ctx.store.datasets.get(a["data_ref"])
        if ds is None:
            raise ToolError(f"unknown data_ref {a['data_ref']!r}; known: {list(ctx.store.datasets)[-10:]}")
        rows = ds["rows"]
        color_states(spec, rows)
        if ds.get("link") and rows and ds["link"]["key"] in rows[0]:
            spec, ok = wired(spec, lambda s: link_chart(s, ds["link"]["key"]))
            link = ds["link"] if ok else None
        spec, time_field = wired(spec, add_time_rule)
        spec["data"] = {"values": rows}
    elif "data" not in spec and "datasets" not in spec:
        raise ToolError("pass a data_ref (preferred) or include data in the spec")
    ctx.emit({"type": "ui", "action": "chart", "title": a["title"], "spec": spec, "link": link,
              "time_field": time_field})
    return {"ok": True, "rows_plotted": len(rows) if rows is not None else None,
            "linked_to_map": bool(link or time_field), "_summary": f"chart: {a['title']}"}


def h_show_visual(ctx: ToolContext, a: dict) -> dict:
    data, layers = {}, {}
    for ref in a.get("data_refs") or []:
        if ref not in ctx.store.datasets:
            raise ToolError(f"unknown data_ref {ref!r}; known: {list(ctx.store.datasets)[-10:]}")
        data[ref] = ctx.store.datasets[ref]["rows"]
    for ref in a.get("layer_refs") or []:
        if ref not in ctx.store.layers:
            raise ToolError(f"unknown layer_ref {ref!r}; known: {list(ctx.store.layers)[-10:]}")
        layers[ref] = {k: v for k, v in ctx.store.layers[ref].items() if k != "meta"}
    size = len(json.dumps(data, default=str)) + len(json.dumps(layers, default=str))
    if size > MAX_VISUAL_DATA_CHARS:
        raise ToolError(f"the data is too big for a visual ({size:,} characters); pass fewer refs")
    libraries = a.get("libraries") or []
    ctx.emit({"type": "ui", "action": "visual", "title": a["title"], "code": a["code"],
              "libraries": libraries, "height": a.get("height") or 380, "data": data,
              "data_order": list(data), "layers": layers, "colors": STATE_COLORS})
    return {"ok": True, "libraries": libraries, "rows": {k: len(v) for k, v in data.items()},
            "note": "rendered in the sandbox; any runtime error shows in the next <context>",
            "_summary": f"visual: {a['title']}"}


HANDLERS: dict[str, Callable[[ToolContext, dict], dict]] = {
    "city_overview": h_city_overview,
    "find_devices": h_find_devices,
    "get_history": h_get_history,
    "get_device": h_get_device,
    "get_alarms": h_get_alarms,
    "get_traffic": h_get_traffic,
    "get_speed": h_get_speed,
    "get_weather": h_get_weather,
    "geocode": h_geocode,
    "find_places": h_find_places,
    "plan_route": h_plan_route,
    "reachable_area": h_reachable_area,
    "find_dangerous_buildings": h_find_dangerous_buildings,
    "where_am_i": h_where_am_i,
    "get_buildings_3d": h_get_buildings_3d,
    "show_on_map": h_show_on_map,
    "map_fly_to": h_map_fly_to,
    "map_callouts": h_map_callouts,
    "map_clear": h_map_clear,
    "show_chart": h_show_chart,
    "show_visual": h_show_visual,
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
        return HANDLERS[real](ctx, args), False
    except ToolError as e:
        return {"error": str(e)}, True
    except (ApiError, WebError) as e:
        return {"error": f"data source failed: {e}. Say so plainly; don't guess values."}, True
    except Exception as e:  # noqa: BLE001 - report, don't crash the conversation
        log.exception("tool %s failed", real)
        return {"error": f"internal error in {real}: {type(e).__name__}: {e}"}, True

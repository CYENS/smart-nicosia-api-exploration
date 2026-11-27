"""Where the twin's data comes from: one list of its sources, for the page and the tools.

Each source says who publishes it, what it gives the twin, under which licence, where to read
more, how often the twin reads it, and a short credit for the map's corner. The page shows the
list as "Data sources" (`/api/sources`, with how fresh each live feed is) and names a source
wherever its data appears: a map layer, a chart, a readout of the strip, the inspector.

Layers and datasets learn their sources from the tool that made them: `run_tool` runs each
handler inside `during(tool)`, and the session store tags what it holds with `current()`. A
handler that knows better passes `sources=` itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

CC_BY = "CC BY 4.0"
ODBL = "ODbL"
MINISTRY = "Ministry of Transport, Communications and Works, through the Cyprus National Access Point"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    short: str  # how a "From ..." line names it
    publisher: str
    gives: str  # what it gives the twin, in the user's words
    group: str
    url: str | None = None  # where to read more: the dataset page, the API docs
    licence: str | None = None  # None: the publisher's own terms
    licence_url: str | None = None
    refresh: str | None = None  # how often the twin reads it
    credit: str | None = None  # the few words the map's corner shows while its data is drawn

    def public(self) -> dict:
        return asdict(self)


GROUPS = ("City sensors", "Mobility", "Weather", "Map, places and routes", "Buildings", "This twin",
          "Figures the scenarios assume")

SOURCES: tuple[Source, ...] = (
    Source(
        "smart_nicosia", "Smart Nicosia platform", "Smart Nicosia", "Municipality of Nicosia, on Nokia's city platform",
        "Street lights, parking bays, bins, air and noise sensors, info screens, traffic cameras "
        "and their counts, the city's speed and alarms",
        "City sensors", url="https://nokia.smartnicosia.eu/api-docs",
        refresh="Every 5 minutes while a page is open",
        credit="Smart Nicosia"),
    Source(
        "bus_live", "Live bus positions (GTFS-RT)", "the live bus feed", MINISTRY,
        "Where each bus is now, and how late it runs",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/publictransportrealtime_gtfs_rt",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 30 seconds at most while a page is open",
        credit="Buses: Ministry of Transport, CC BY 4.0"),
    Source(
        "bus_timetable", "Bus timetables (GTFS)", "the bus timetables", MINISTRY,
        "Lines, stops, timetables and route shapes of NPT, Intercity and Pame Express",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/publictransportstatic",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most"),
    Source(
        "road_speeds", "Road speeds (Bluetooth travel times)", "the road detectors", "Cyprus National Access Point",
        "Speeds on the main avenues, timed between Bluetooth detectors",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/bluetooth",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 5 minutes",
        credit="Road speeds: traffic4cyprus, CC BY 4.0"),
    Source(
        "open_meteo", "Open-Meteo", "Open-Meteo", "Open-Meteo",
        "The weather now and by the hour, sunrise and sunset, modelled air quality and dust (CAMS "
        "Europe, under the air surfaces between the stations), the wind over the city, and the "
        "weather at past moments of the city clock",
        "Weather", url="https://open-meteo.com",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 15 minutes",
        credit="Weather: Open-Meteo"),
    Source(
        "rainviewer", "RainViewer rain radar", "RainViewer", "RainViewer",
        "Where it rains: the radar of the last two hours, ten minutes apart",
        "Weather", url="https://www.rainviewer.com/api.html",
        refresh="Every 5 minutes while the rain radar is on",
        credit="Radar: RainViewer"),
    Source(
        "openstreetmap", "OpenStreetMap", "OpenStreetMap", "OpenStreetMap contributors",
        "Streets, places and their opening hours, and the data under the base map",
        "Map, places and routes", url="https://www.openstreetmap.org/copyright",
        licence=ODBL, licence_url="https://opendatacommons.org/licenses/odbl/",
        refresh="The places index is rebuilt weekly"),
    Source(
        "overture", "Overture Maps places", "Overture Maps", "Overture Maps Foundation, from Meta, "
        "Microsoft, Foursquare, AllThePlaces and others",
        "About 9,500 businesses and services the OpenStreetMap index doesn't list, such as "
        "studios, schools, clinics, clubs, shops and offices, each at its door with its address, "
        "phone and website. Some entries are out of date: a place may have closed or moved",
        "Map, places and routes", url="https://docs.overturemaps.org/attribution/",
        licence="CDLA-Permissive-2.0 (some rows Apache-2.0 or CC0-1.0)",
        licence_url="https://cdla.dev/permissive-2-0/",
        refresh="A new monthly release is read at most once a month",
        credit="Places: Overture Maps"),
    Source(
        "openfreemap", "OpenFreeMap", "OpenFreeMap", "OpenFreeMap, with OpenMapTiles",
        "The base map's tiles, by day and by night",
        "Map, places and routes", url="https://openfreemap.org",
        refresh="Tiles as you move the map"),
    Source(
        "terrain_tiles", "Ground heights (DLS terrain model, 2019)", "the DLS terrain model",
        "Department of Lands and Surveys: a 1 m grid from its 2019 LiDAR flight, served as open "
        "terrain tiles by Mapterhorn. North of the buffer zone the tiles are filled from Copernicus "
        "GLO-30 (30 m)",
        "The height of the ground: the terrain and its hillshade, the views from high places and "
        "what hides what",
        "Map, places and routes", url="https://www.data.gov.cy/el/dataset/ypsometria-inspire",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Tiles as you move the map; the server keeps each one it has read",
        credit="Ground: DLS Cyprus 2019, CC BY 4.0, via Mapterhorn"),
    Source(
        "valhalla", "Valhalla routing", "Valhalla", "The Valhalla project, on FOSSGIS's public server",
        "Walking and driving routes, how far you reach on foot, walks to and from the bus, the "
        "streets traffic counts run along, and free-flow speeds for the roads",
        "Map, places and routes", url="https://github.com/valhalla/valhalla",
        refresh="Asked per route, kept when it can be",
        credit="Routes: Valhalla"),
    Source(
        "geocoding", "Address search", "address search", "Nominatim (OpenStreetMap Foundation) and Photon (Komoot)",
        "Turning names and addresses into places, and places into addresses",
        "Map, places and routes", url="https://nominatim.org",
        licence=ODBL, licence_url="https://opendatacommons.org/licenses/odbl/",
        refresh="Asked per question"),
    Source(
        "web_search", "Web search", "the web", "The web pages Claude's web search finds",
        "Places the map doesn't list, such as teachers, studios, shops and services, with their "
        "addresses, phones and websites, and events and news",
        "Map, places and routes", url="https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool",
        refresh="Asked per question; places found are kept with the page they came from",
        credit="Places: from the web"),
    Source(
        "dls", "Department of Lands and Surveys (DLS)", "the DLS", "Republic of Cyprus",
        "Every building in 3D at its height and floors, the plots they stand on, and the shade "
        "they cast",
        "Buildings", url="https://eservices.dls.moi.gov.cy/inspire/",
        refresh="Fetched once per area, then kept",
        credit="Buildings: DLS Cyprus"),
    Source(
        "registers", "Dangerous-building registers", "the ΕΟΑ registers", "District local government organisations (ΕΟΑ) "
        "of Nicosia, Limassol, Larnaca and Famagusta",
        "Buildings declared dangerous or in need of repair",
        "Buildings", url="https://ndlgo.org.cy/en/hazardousbldslist/",
        refresh="Read again once a day, with a saved copy when a register can't be read",
        credit="Registers: ΕΟΑ"),
    Source(
        "twin_memory", "The twin's memory", "the twin's memory", "This twin",
        "History, the usual for each hour, and the city clock: what the sources above reported, "
        "kept as it arrived",
        "This twin",
        refresh="Records while a page is open; older history is filled in by the collector"),
    Source(
        "grid_co2", "CO2 per kWh of the Cyprus grid", "the grid's emission factor",
        "Electricity Authority of Cyprus (EAC), and Ember",
        "The CO2 the lamps' electricity emits: 0.627 kg per kWh sold in EAC's 2023 energy mix, about "
        "0.49 in Ember's 2025 data as solar grows; the lighting scenario assumes 0.55 and shows the range",
        "Figures the scenarios assume",
        url="https://eac.com.cy/EN/EAC/Sustainability/Documents/%CE%95%CE%9D%CE%95%CE%A1%CE%93%CE%95%CE%99%CE%91%CE%9A%CE%9F%20%CE%9C%CE%95%CE%99%CE%93%CE%9C%CE%91%202023%20ENG.pdf",
        refresh="A published yearly figure"),
    Source(
        "led_retrofit", "LED street lighting demonstrations", "the DOE's LED demonstrations",
        "US Department of Energy GATEWAY programme, Pacific Northwest National Laboratory",
        "How much less an LED street lamp draws than the sodium lamp it replaces: 26% to 57% in New "
        "York City's 2011 demonstration",
        "Figures the scenarios assume",
        url="https://www.pnnl.gov/main/publications/external/technical_reports/PNNL-21036.pdf",
        refresh="A published study"),
)

BY_ID: dict[str, Source] = {s.id: s for s in SOURCES}

# What each tool reads, so its layers and datasets can say where they come from.
TOOL_SOURCES: dict[str, tuple[str, ...]] = {
    "city_overview": ("smart_nicosia",),
    "find_devices": ("smart_nicosia",),
    "get_device": ("smart_nicosia",),
    "get_alarms": ("smart_nicosia",),
    "get_history": ("smart_nicosia", "twin_memory"),
    "get_traffic": ("smart_nicosia", "valhalla"),
    "get_speed": ("smart_nicosia",),
    "get_weather": ("open_meteo",),
    "geocode": ("geocoding",),
    "find_places": ("openstreetmap", "overture"),
    "locate_web_places": ("web_search", "geocoding", "openstreetmap", "overture"),
    "plan_route": ("valhalla", "openstreetmap", "overture"),
    "reachable_area": ("valhalla", "openstreetmap", "overture"),
    "find_dangerous_buildings": ("registers", "dls"),
    "where_am_i": ("dls", "registers", "geocoding"),
    "query_history": ("twin_memory", "smart_nicosia"),
    "run_sql": ("twin_memory",),
    "get_buses": ("bus_live", "bus_timetable"),
    "get_departures": ("bus_timetable", "bus_live"),
    "plan_transit": ("bus_timetable", "bus_live", "valhalla", "overture"),
    "get_road_speeds": ("road_speeds",),
    "list_incidents": ("twin_memory", "smart_nicosia"),
    "explain_incident": ("twin_memory", "smart_nicosia", "open_meteo"),
    "create_watch": ("smart_nicosia",),
    "sun_and_shade": ("dls",),
    "environment_surface": ("smart_nicosia", "open_meteo"),
    "find_high_places": ("terrain_tiles", "openstreetmap", "dls", "geocoding"),
    "view_from": ("terrain_tiles", "dls", "openstreetmap", "twin_memory"),
    "run_scenario": ("twin_memory", "smart_nicosia"),
    "compare_scenarios": ("twin_memory",),
}

_tool: ContextVar[str | None] = ContextVar("source_tool", default=None)


@contextmanager
def during(tool: str) -> Iterator[None]:
    """Run a tool's handler: what it stores is tagged with the tool's sources."""
    token = _tool.set(tool)
    try:
        yield
    finally:
        _tool.reset(token)


def current() -> list[str]:
    """The sources of the tool running now (none outside a tool)."""
    return list(TOOL_SOURCES.get(_tool.get() or "", ()))


def known(ids: list[str] | tuple[str, ...] | None) -> list[str]:
    """The ids that name a source, in order, once each."""
    return [i for i in dict.fromkeys(ids or ()) if i in BY_ID]

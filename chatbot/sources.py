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


GROUPS = ("City sensors", "Mobility", "Weather", "Map, places and routes", "Buildings", "This twin")

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
        "The weather now and by the hour, sunrise and sunset, modelled air quality and dust, and "
        "the weather at past moments of the city clock",
        "Weather", url="https://open-meteo.com",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 15 minutes",
        credit="Weather: Open-Meteo"),
    Source(
        "openstreetmap", "OpenStreetMap", "OpenStreetMap", "OpenStreetMap contributors",
        "Streets, places and their opening hours, and the data under the base map",
        "Map, places and routes", url="https://www.openstreetmap.org/copyright",
        licence=ODBL, licence_url="https://opendatacommons.org/licenses/odbl/",
        refresh="The places index is rebuilt weekly"),
    Source(
        "openfreemap", "OpenFreeMap", "OpenFreeMap", "OpenFreeMap, with OpenMapTiles",
        "The base map's tiles, by day and by night",
        "Map, places and routes", url="https://openfreemap.org",
        refresh="Tiles as you move the map"),
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
        "dls", "Department of Lands and Surveys (DLS)", "the DLS", "Republic of Cyprus",
        "Every building in 3D at its height and floors, and the plots they stand on",
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
    "find_places": ("openstreetmap",),
    "plan_route": ("valhalla", "openstreetmap"),
    "reachable_area": ("valhalla", "openstreetmap"),
    "find_dangerous_buildings": ("registers", "dls"),
    "where_am_i": ("dls", "registers", "geocoding"),
    "query_history": ("twin_memory", "smart_nicosia"),
    "run_sql": ("twin_memory",),
    "get_buses": ("bus_live", "bus_timetable"),
    "get_departures": ("bus_timetable", "bus_live"),
    "plan_transit": ("bus_timetable", "bus_live", "valhalla"),
    "get_road_speeds": ("road_speeds",),
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

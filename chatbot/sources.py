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
    # The street rules (chatbot/streets): the National Access Point's street inventories.
    Source(
        "speed_limits", "Speed limit signs", "the speed limit inventory", "Public Works Department, through the Cyprus National Access Point",
        "Where each speed limit sign stands and what it says; the twin carries each along its street "
        "to show which stretch it governs, and how sure that is",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/speed_limits",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most; the inventory itself changes rarely",
        credit="Speed limits: Public Works Dept., CC BY 4.0"),
    Source(
        "slow_zones", "20 and 30 km/h zones", "the slow zones inventory", "Public Works Department, through the Cyprus National Access Point",
        "The 20 and 30 km/h zones and their entrance points (Nicosia's three are listed as planned)",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/lez",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Slow zones: Public Works Dept., CC BY 4.0"),
    Source(
        "traffic_signals", "Traffic signals", "the traffic signals inventory", "Public Works Department, through the Cyprus National Access Point",
        "Every signalled junction, with the council that runs it (where they are, not their phases)",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/traffic_signals",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Signals: Public Works Dept., CC BY 4.0"),
    Source(
        "pedestrian_crossings", "Pedestrian crossings", "the crossings inventory", "Public Works Department, through the Cyprus National Access Point",
        "The marked pedestrian crossings the department lists, for how schools and bus stops are served",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/pedestrian_crossings",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Crossings: Public Works Dept., CC BY 4.0"),
    Source(
        "cycleways", "Cycleways", "the cycleways inventory", "Public Works Department, through the Cyprus National Access Point",
        "The cycle routes, joined by the twin into connected pieces to show where the network breaks",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/cycleways",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Cycleways: Public Works Dept., CC BY 4.0"),
    Source(
        "bus_lanes", "Bus lanes", "the bus lanes inventory", "Public Works Department, through the Cyprus National Access Point",
        "The bus lanes in use and those planned along the tram corridors",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/bus_lanes",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Bus lanes: Public Works Dept., CC BY 4.0"),
    Source(
        "enforcement_cameras", "Traffic enforcement cameras", "the enforcement cameras inventory",
        "Elovate, for the Ministry of Transport, through the Cyprus National Access Point",
        "Where the traffic enforcement cameras stand and which way they watch",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/traffic_enforcement_cameras",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Cameras: Elovate for the Ministry of Transport, CC BY 4.0"),
    Source(
        "waze_alerts", "Waze drivers' reports", "Waze drivers", "Waze, through FixCyprus and the Cyprus National Access Point",
        "Closures, jams, accidents and hazards (potholes, a stopped car, something on the road) "
        "that drivers report in the Waze app, each with its street and when it was reported",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/waze_alerts",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 3 minutes at most while a page or the watch reads them",
        credit="Road reports: Waze via traffic4cyprus, CC BY 4.0"),
    Source(
        "traffic_events", "Roadworks and diversions", "the Public Works Department",
        "Public Works Department, through the Cyprus National Access Point",
        "Roadworks, diversions and accidents its traffic operators enter, with when the works "
        "start and end and a description, often in Greek",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/trafficevents",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 15 minutes at most while a page or the watch reads them",
        credit="Roadworks: Public Works Department, CC BY 4.0"),
    Source(
        "waze_traffic", "Waze jam lines", "Waze traffic", "Waze, through FixCyprus and the Cyprus National Access Point",
        "Where traffic is jammed, as lines with Waze's jam level, speed and delay: a second "
        "source of congestion beside the road detectors",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/waze_traffic",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Every 3 minutes at most; every 15 while it doesn't answer",
        credit="Jams: Waze via traffic4cyprus, CC BY 4.0"),
    # Park, charge and hire (chatbot/mobility/facilities.py): one entry per publisher.
    Source(
        "nap_public_works", "Car parks, park and ride and bicycle racks", "the Public Works Department's inventory",
        "Public Works Department, through the Cyprus National Access Point",
        "Where the car parks, the park and ride sites and the public bicycle racks are. No spaces "
        "or prices: a car park's free bays come from the city's sensors beside it, where it has any",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/parking_areas",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most; the inventory's own date is its last change",
        credit="Car parks: Public Works Dept, CC BY 4.0"),
    Source(
        "nap_chargers", "Electric vehicle chargers", "the EV charger inventory",
        "Department of Electrical and Mechanical Services, through the Cyprus National Access Point",
        "Every public charging point with its power, connectors, operator, hours and the status "
        "it is listed with each night (not whether a connector is free now)",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/electricvehiclecharges",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Chargers: EMS, CC BY 4.0"),
    Source(
        "nap_nextbike", "Nextbike stations", "the Nextbike station inventory",
        "Nextbike Cyprus, through the Cyprus National Access Point",
        "Where the Nextbike stations are, and how many docks some have",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/nextbike",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Bike stations: Nextbike, CC BY 4.0"),
    Source(
        "nextbike_live", "Nextbike live bikes (GBFS)", "Nextbike's live feed", "nextbike GmbH",
        "The bikes standing at each Nextbike station now, how many are electric, and the free docks",
        "Mobility", url="https://gbfs.nextbike.net/maps/gbfs/v2/nextbike_cy/gbfs.json",
        licence="CC0 1.0", licence_url="https://creativecommons.org/publicdomain/zero/1.0/",
        refresh="Every 2 minutes at most, while bike stations are on the map or asked about",
        credit="Live bikes: Nextbike"),
    Source(
        "nap_bolt", "Bolt scooter zones", "Bolt's zone inventory", "Bolt, through the Cyprus National Access Point",
        "The zones where Bolt's e-scooters are parked, with how many each holds. Where the scooters "
        "are now isn't published",
        "Mobility", url="https://traffic4cyprus.org.cy/dataset/bolt",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Once a day at most",
        credit="Scooter zones: Bolt, CC BY 4.0"),
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
        "Streets, places and their opening hours, the data under the base map, and the 3D "
        "buildings where the DLS has none (north of the buffer zone), in grey",
        "Map, places and routes", url="https://www.openstreetmap.org/copyright",
        licence=ODBL, licence_url="https://opendatacommons.org/licenses/odbl/",
        refresh="The places index is rebuilt weekly; buildings are read again monthly"),
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
        credit="Ground: DLS Cyprus 2019, CC BY 4.0"),
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
        "dls_lidar", "LiDAR flight of 2019 (DLS surface model)", "the 2019 LiDAR flight",
        "Department of Lands and Surveys: the surface model (half a metre a cell) and the terrain "
        "model of its 2019 flight, published as open files",
        "Each building's height as the laser measured it in 2019, and which buildings grew, "
        "shrank or went since the 2014 survey",
        "Buildings", url="https://www.data.gov.cy/el/dataset/ypsometria-inspire",
        licence=CC_BY, licence_url="https://creativecommons.org/licenses/by/4.0/",
        refresh="Measured once from the sheets and kept: the flight does not change",
        credit="Heights 2019: DLS Cyprus, CC BY 4.0"),
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
    "get_buildings_3d": ("dls", "dls_lidar"),
    "find_height_changes": ("dls_lidar", "dls"),
    "where_am_i": ("dls", "registers", "geocoding"),
    "query_history": ("twin_memory", "smart_nicosia"),
    "run_sql": ("twin_memory",),
    "get_buses": ("bus_live", "bus_timetable"),
    "get_departures": ("bus_timetable", "bus_live"),
    "plan_transit": ("bus_timetable", "bus_live", "valhalla", "overture"),
    "get_road_speeds": ("road_speeds",),
    "get_road_reports": ("waze_alerts", "traffic_events", "waze_traffic"),
    "find_parking": ("nap_public_works", "smart_nicosia", "openstreetmap", "twin_memory"),
    "find_chargers": ("nap_chargers",),
    "find_bikes": ("nap_nextbike", "nextbike_live", "nap_bolt", "nap_public_works"),
    "list_incidents": ("twin_memory", "smart_nicosia"),
    "explain_incident": ("twin_memory", "smart_nicosia", "open_meteo"),
    "create_watch": ("smart_nicosia",),
    "sun_and_shade": ("dls",),
    "environment_surface": ("smart_nicosia", "open_meteo"),
    "find_high_places": ("terrain_tiles", "openstreetmap", "dls", "geocoding"),
    "view_from": ("terrain_tiles", "dls", "openstreetmap", "twin_memory"),
    "run_scenario": ("twin_memory", "smart_nicosia"),
    "compare_scenarios": ("twin_memory",),
    # The street rules (chatbot/tools/streets.py).
    "get_speed_limits": ("speed_limits", "openstreetmap"),
    "find_slow_zones": ("slow_zones", "speed_limits", "openstreetmap"),
    "find_crossings": ("pedestrian_crossings", "traffic_signals", "speed_limits", "openstreetmap", "overture"),
    "get_cycle_network": ("cycleways", "openstreetmap", "speed_limits"),
    "get_bus_lanes": ("bus_lanes",),
    "find_enforcement_cameras": ("enforcement_cameras", "traffic_signals", "speed_limits"),
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

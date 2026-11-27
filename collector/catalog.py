"""Every collector job described for people: what it records, what the twin uses it for, which
source it reads (an id from chatbot/sources.py) and which tables it writes. The admin page
(`/admin`) shows these; a new job adds its entry here, and a test checks none is missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class JobInfo:
    title: str  # a few words, sentence case
    summary: str  # one short line under the title, no full stop
    records: str  # what it records, in plain words
    uses: str  # what in the twin depends on it
    source: str  # chatbot/sources.py id
    group: str
    tables: tuple[str, ...]

    def public(self) -> dict:
        d = asdict(self)
        d["tables"] = list(self.tables)
        return d


GROUPS = (
    "Changes as they happen",
    "Latest readings per device",
    "Traffic and roads",
    "The city's devices",
)

JOB_INFO: dict[str, JobInfo] = {
    "devices": JobInfo(
        "Device list",
        "Every sensor and device, and where it is",
        "Every sensor and device the city platform knows: street lights, parking bays, bins, air "
        "stations, info screens and cameras, with where each one is.",
        "Every map layer, search and the inspector. A new device shows up the day after it is "
        "installed.",
        "smart_nicosia", "The city's devices", ("devices",)),
    "attributes": JobInfo(
        "Device details",
        "Each device's street, model and last contact",
        "Each device's street name, model and settings, and when it last talked to the platform.",
        "The inspector's details, and telling a broken device from a quiet one.",
        "smart_nicosia", "The city's devices", ("attributes", "devices")),
    "cameras_map": JobInfo(
        "Camera locations",
        "Which counting feed belongs to which camera",
        "Which traffic-counting feed belongs to which camera, matched by name, or placed by hand "
        "in data/camera_locations.csv.",
        "Putting people and vehicle counts in the right place on the map.",
        "smart_nicosia", "The city's devices", ("traffic_cameras",)),
    "stream": JobInfo(
        "Every change, as it happens",
        "Every bay, lamp and bin change as it happens",
        "A running feed of every message the devices send: a parking bay taken or freed, a lamp "
        "switched on or off, a bin's fill level. A few hundred requests a day record every "
        "change.",
        "Parking turnover, lamp switching times and bin fill rates, the city clock's replay, and "
        "the scenario models.",
        "smart_nicosia", "Changes as they happen", ("telemetry", "state_changes")),
    "events": JobInfo(
        "City alert log",
        "The platform's running log of alerts",
        "The platform's running log of alerts, such as a bin over 80% full or a lamp fault.",
        "The incident inbox and the history of each device's alerts.",
        "smart_nicosia", "Changes as they happen", ("events",)),
    "alarms": JobInfo(
        "Open alarms",
        "Alarms the operators haven't acknowledged",
        "Alarms the platform has raised that the city's operators have not acknowledged yet, "
        "with when each started and cleared.",
        "The incident inbox.",
        "smart_nicosia", "Changes as they happen", ("alarms",)),
    "parking": JobInfo(
        "Parking bays",
        "Each bay's latest reading, to fill any gaps",
        "Each parking bay's latest reading. The change feed already records every arrival and "
        "departure, so this only fills gaps and runs less often.",
        "Free bays now and by the hour, and the parking scenarios.",
        "smart_nicosia", "Latest readings per device", ("telemetry", "state_changes")),
    "lamps": JobInfo(
        "Street lights",
        "Each light's power, energy and on or off",
        "Each street light's power draw, energy meter and whether it is on. Lamps only report "
        "while powered, so this runs between sunset and sunrise.",
        "Lights out alerts, energy use and the street lighting scenario.",
        "smart_nicosia", "Latest readings per device", ("telemetry", "state_changes")),
    "bins": JobInfo(
        "Waste bins",
        "Each bin's fill level, temperature and battery",
        "Each bin's fill level, temperature and battery.",
        "Bins nearly full, fill rates and the waste collection scenario.",
        "smart_nicosia", "Latest readings per device", ("telemetry", "state_changes")),
    "infopoints": JobInfo(
        "Info screens",
        "Whether each info screen is working",
        "Whether each public info screen is on and working.",
        "The info screens layer and their alerts.",
        "smart_nicosia", "Latest readings per device", ("telemetry", "state_changes")),
    "air": JobInfo(
        "Air and noise stations",
        "Particles, gases, noise and weather at each station",
        "Readings from the air stations: particles, gases, noise, temperature and humidity.",
        "The air and noise readouts, and the surfaces drawn between the stations.",
        "smart_nicosia", "Latest readings per device", ("telemetry", "state_changes")),
    "cameras": JobInfo(
        "Traffic cameras",
        "Whether each traffic camera is online",
        "Each camera's own state: whether it is online, and its latest readings.",
        "The cameras layer, and knowing when a count is missing because a camera is down.",
        "smart_nicosia", "Latest readings per device", ("telemetry", "state_changes")),
    "traffic_daily": JobInfo(
        "Daily counts per camera",
        "Road users counted per camera, per day",
        "People, cars, bikes and other road users counted by each camera and counting line, "
        "once each day is over.",
        "Footfall by street and by day.",
        "smart_nicosia", "Traffic and roads", ("traffic_counts",)),
    "traffic_hourly": JobInfo(
        "Hourly counts, whole city",
        "Everyone counted across the city, by the hour",
        "Everyone counted by all the cameras together, hour by hour, for today and yesterday.",
        "The city's busy hours, and today against a usual day.",
        "smart_nicosia", "Traffic and roads", ("traffic_counts",)),
    "speed": JobInfo(
        "City speed",
        "Average traffic speed, in five-minute steps",
        "The average traffic speed across the city, in five-minute steps.",
        "Slowdowns across the city, and the city clock's traffic replay.",
        "smart_nicosia", "Traffic and roads", ("speeds",)),
    "travel_times": JobInfo(
        "Road travel times",
        "Car travel times between road detectors",
        "How long cars take between Bluetooth detectors on the main roads, for every road segment "
        "the national feed covers. The feed keeps no history, so a reading not kept is lost.",
        "The road speeds layer, and later, measuring what a road closure did.",
        "road_speeds", "Traffic and roads", ("travel_times",)),
}

# Live feeds the twin shows but no collector keeps yet: the page lists them so the gap is seen.
UNRECORDED: dict[str, str] = {
    "bus_live": (
        "Shown live on the map, but nothing keeps it, so the twin has no history of where the "
        "buses were or how late they ran. Measuring bus reliability needs about four weeks of it."
    ),
}

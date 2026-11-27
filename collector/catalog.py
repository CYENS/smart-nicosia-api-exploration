"""Every collector job described for people: what it records, what the twin uses it for, how it
is collected in plain words, and which source it reads (an id from chatbot/sources.py). The admin
page (`/admin`) shows these; a new job adds its entry here, and a test checks none is missing.

This is the part written by hand, so it stays at the level that rarely changes. The technical
side (API calls, tables, settings, cost) is declared on the job classes in collector/jobs.py,
next to the code it describes, and a test checks it against what each job really does.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class JobInfo:
    title: str  # a few words, sentence case
    summary: str  # one short line under the title, no full stop
    records: str  # what it records, in plain words
    uses: str  # what in the twin depends on it
    how: str  # how it is collected, in plain words; no cadences (those can change on the page)
    source: str  # chatbot/sources.py id
    group: str

    def public(self) -> dict:
        return asdict(self)


GROUPS = (
    "Changes as they happen",
    "Latest readings per device",
    "Traffic and roads",
    "The city's devices",
)

# How the admin page names a setting a job declares (Job.settings) and the ones every job has.
# A test checks every declared setting has a label here and is a real Config field. Only these
# are ever shown: never the whole config or the environment.
SETTING_LABELS: dict[str, str] = {
    "every_minutes": "Runs every (minutes)",
    "enabled": "Switched on",
    "night_only": "Only after dark",
    "tenant": "The city's account on the platform",
    "concurrency": "Requests at once",
    "stream_keys": "Message fields kept",
    "range_cap": "Records per page",
    "stream_max_pages": "Pages per run at most",
    "stream_max_lookback_hours": "Looks back at most (hours)",
    "events_max_pages": "Pages per run at most",
    "alarm_states": "Alarm states watched",
    "alarm_limit": "Alarms per request at most",
    "nap_url": "Feed address",
    "traffic_initial_days": "Days caught up on the first run",
    "traffic_max_days_per_request": "Days per request at most",
    "speed_group_by": "Step",
    "speed_line_type": "Measure",
    "speed_max_catchup_days": "Catches up at most (days)",
    "camera_csv": "Hand-placed camera list",
    "timeout_s": "Gives up on a request after (seconds)",
    "max_retries": "Retries after a failed request",
    "request_delay_s": "Pause between requests (seconds)",
}
COMMON_SETTINGS = ("timeout_s", "max_retries", "request_delay_s")

JOB_INFO: dict[str, JobInfo] = {
    "devices": JobInfo(
        "Device list",
        "Every sensor and device, and where it is",
        "Every sensor and device the city platform knows: street lights, parking bays, bins, air "
        "stations, info screens and cameras, with where each one is.",
        "Every map layer, search and the inspector. A new device shows up the day after it is "
        "installed.",
        "Each run, the twin asks the city platform for its full list of devices. New ones are "
        "added and moved ones updated; any that disappear are kept, marked as gone.",
        "smart_nicosia", "The city's devices"),
    "attributes": JobInfo(
        "Device details",
        "Each device's street, model and last contact",
        "Each device's street name, model and settings, and when it last talked to the platform.",
        "The inspector's details, and telling a broken device from a quiet one.",
        "Each run, the twin asks about every device in turn for details such as its street and "
        "when it last made contact. One request per device makes it the costliest job, so it is "
        "best run rarely.",
        "smart_nicosia", "The city's devices"),
    "cameras_map": JobInfo(
        "Camera locations",
        "Which counting feed belongs to which camera",
        "Which traffic-counting feed belongs to which camera, matched by name, or placed by hand "
        "in data/camera_locations.csv.",
        "Putting people and vehicle counts in the right place on the map.",
        "Each run, the twin reads the list of traffic-counting feeds and matches each to a "
        "camera on the map by name. Feeds it can't match go into a list that someone can place "
        "by hand.",
        "smart_nicosia", "The city's devices"),
    "stream": JobInfo(
        "Every change, as it happens",
        "Every bay, lamp and bin change as it happens",
        "A running feed of every message the devices send: a parking bay taken or freed, a lamp "
        "switched on or off, a bin's fill level. A few hundred requests a day record every "
        "change.",
        "Parking turnover, lamp switching times and bin fill rates, the city clock's replay, and "
        "the scenario models.",
        "Each run, the twin asks for the messages devices sent since the last run, a page at a "
        "time, and keeps each change once. One request covers every device, so it costs little.",
        "smart_nicosia", "Changes as they happen"),
    "events": JobInfo(
        "City alert log",
        "The platform's running log of alerts",
        "The platform's running log of alerts, such as a bin over 80% full or a lamp fault.",
        "The incident inbox and the history of each device's alerts.",
        "Each run, the twin reads the platform's alert log from where it stopped last time, "
        "with a small overlap so nothing falls between two runs.",
        "smart_nicosia", "Changes as they happen"),
    "alarms": JobInfo(
        "Open alarms",
        "Alarms the operators haven't acknowledged",
        "Alarms the platform has raised that the city's operators have not acknowledged yet, "
        "with when each started and cleared.",
        "The incident inbox.",
        "Each run, the twin asks which alarms are open. An alarm it has seen before keeps the "
        "time it first appeared, so the twin knows how long it stayed open and when it cleared.",
        "smart_nicosia", "Changes as they happen"),
    "parking": JobInfo(
        "Parking bays",
        "Each bay's latest reading, to fill any gaps",
        "Each parking bay's latest reading. The change feed already records every arrival and "
        "departure, so this only fills gaps and runs less often.",
        "Free bays now and by the hour, and the parking scenarios.",
        "Each run, the twin asks every parking bay for its latest reading, one request per bay. "
        "The change feed already catches arrivals and departures, so this mostly fills gaps.",
        "smart_nicosia", "Latest readings per device"),
    "lamps": JobInfo(
        "Street lights",
        "Each light's power, energy and on or off",
        "Each street light's power draw, energy meter and whether it is on. Lamps only report "
        "while powered, so this runs between sunset and sunrise.",
        "Lights out alerts, energy use and the street lighting scenario.",
        "Each run, the twin asks every street light for its latest readings, one request per "
        "light. Lamps only report while powered, so a run by day learns nothing.",
        "smart_nicosia", "Latest readings per device"),
    "bins": JobInfo(
        "Waste bins",
        "Each bin's fill level, temperature and battery",
        "Each bin's fill level, temperature and battery.",
        "Bins nearly full, fill rates and the waste collection scenario.",
        "Each run, the twin asks every bin for its latest fill level, temperature and battery, "
        "one request per bin, and keeps only readings it hasn't seen.",
        "smart_nicosia", "Latest readings per device"),
    "infopoints": JobInfo(
        "Info screens",
        "Whether each info screen is working",
        "Whether each public info screen is on and working.",
        "The info screens layer and their alerts.",
        "Each run, the twin asks every info screen for its latest state, one request per "
        "screen.",
        "smart_nicosia", "Latest readings per device"),
    "air": JobInfo(
        "Air and noise stations",
        "Particles, gases, noise and weather at each station",
        "Readings from the air stations: particles, gases, noise, temperature and humidity.",
        "The air and noise readouts, and the surfaces drawn between the stations.",
        "Each run, the twin asks every air station for its latest readings, one request per "
        "station.",
        "smart_nicosia", "Latest readings per device"),
    "cameras": JobInfo(
        "Traffic cameras",
        "Whether each traffic camera is online",
        "Each camera's own state: whether it is online, and its latest readings.",
        "The cameras layer, and knowing when a count is missing because a camera is down.",
        "Each run, the twin asks every camera for its own state, one request per camera. The "
        "counts themselves come from the two count jobs.",
        "smart_nicosia", "Latest readings per device"),
    "traffic_daily": JobInfo(
        "Daily counts per camera",
        "Road users counted per camera, per day",
        "People, cars, bikes and other road users counted by each camera and counting line, "
        "once each day is over.",
        "Footfall by street and by day.",
        "Each run, the twin asks for the counts of every day that has finished since the last "
        "run, per camera and counting line. The first run catches up on a few past days.",
        "smart_nicosia", "Traffic and roads"),
    "traffic_hourly": JobInfo(
        "Hourly counts, whole city",
        "Everyone counted across the city, by the hour",
        "Everyone counted by all the cameras together, hour by hour, for today and yesterday.",
        "The city's busy hours, and today against a usual day.",
        "Each run, the twin asks for today's counts across the city so far, and once more for "
        "yesterday until it is complete.",
        "smart_nicosia", "Traffic and roads"),
    "speed": JobInfo(
        "City speed",
        "Average traffic speed, in five-minute steps",
        "The average traffic speed across the city, in five-minute steps.",
        "Slowdowns across the city, and the city clock's traffic replay.",
        "Each run, the twin asks for the average speed across the city since its last run, in "
        "five-minute steps, catching up if it was switched off for a while.",
        "smart_nicosia", "Traffic and roads"),
    "travel_times": JobInfo(
        "Road travel times",
        "Car travel times between road detectors",
        "How long cars take between Bluetooth detectors on the main roads, for every road segment "
        "the national feed covers. The feed keeps no history, so a reading not kept is lost.",
        "The road speeds layer, and later, measuring what a road closure did.",
        "Each run, the twin downloads the national feed of travel times between road detectors. "
        "The feed only holds the latest reading, so whatever the twin doesn't keep is lost.",
        "road_speeds", "Traffic and roads"),
}

# Live feeds the twin shows but no collector keeps yet: the page lists them so the gap is seen.
UNRECORDED: dict[str, str] = {
    "bus_live": (
        "Shown live on the map, but nothing keeps it, so the twin has no history of where the "
        "buses were or how late they ran. Measuring bus reliability needs about four weeks of it."
    ),
}

# Smart Nicosia API Exploration

This repo contains a small Python helper (`smart_nicosia_api.py`) for querying the
Nokia Smart Nicosia API and saving example payloads for offline analysis. It also has a
data collector (below) and a digital-twin chatbot.
See [docs/digital-twin-research.md](docs/digital-twin-research.md) for the research and plan.

## Chatbot: Nicosia digital twin

A web app with a live map and a chat panel, powered by Claude Sonnet 5.5 (`claude-sonnet-5-5`).
It answers questions for residents and visitors from the live city sensors. It reads street
lights, parking, bins, air and noise, traffic counts, city speed and alarms. It also finds places
from OpenStreetMap, plans walks, drives and tours with Valhalla, and checks the weather with
Open-Meteo. It draws its answers on the map (markers, pulsing highlights, heatmaps, 3D columns,
animated routes, reachable areas, callouts) and as Vega-Lite charts in the chat.

It also has:

- **Time-lapses.** It can replay the last 3 days of lamps, parking, bins or air on the map,
  with a time slider, play/pause and speed controls.
- **Linked charts.** Hovering a bar or point in a chart highlights that device on the map, and
  the other way round.
- **A moving time line.** Every chart with a time axis (lamps, parking, speed, traffic,
  weather) shows a red line at the time-lapse's moment, so they all move together. Hovering
  any of them moves the slider. With no time-lapse playing, the line marks the current time.
- **Custom visuals.** For charts Vega-Lite can't draw (radial clocks, sankeys, networks, 3D),
  Claude writes JavaScript using d3, Observable Plot, ECharts or three.js. The code runs in a
  sandboxed iframe with no network access.

### Run it

1. Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` (create a key at
   https://platform.claude.com under API keys). `.env` is in `.gitignore`.
2. Start it:

   ```bash
   uv run chatbot
   ```

3. Open http://127.0.0.1:8000 (set `CHATBOT_PORT` to use another port).

The map and the "no key" notice work without a key; questions need one. Optional settings
are in `.env.example`: `CHATBOT_MODEL`, `CHATBOT_EFFORT` (default `medium`),
`CHATBOT_WEB_SEARCH`.

### How it works

- [chatbot/agent.py](chatbot/agent.py) streams each turn from Claude. It runs the requested tools
  in parallel and loops until Claude answers. History is append-only. The system prompt and
  tool list are fixed, so prompt caching works. Refused requests retry on a fallback model
  (`fallbacks: "default"`).
- [chatbot/tools.py](chatbot/tools.py) defines 18 tools plus Claude's web search. Every input is
  validated against its JSON schema.
  - **Data tools** return compact JSON. They store big results (map GeoJSON, chart rows) in the
    session and pass Claude a `layer_ref` or `data_ref`.
  - **Map and chart tools** push commands to the browser. The server fills each chart from a
    `data_ref` and validates the spec against the Vega-Lite schema, so every number comes from
    a tool.
  - **Linking.** Each dataset records the field that ties its rows to the map: a device `id`, a
    `camera` or a time-lapse `frame`. `show_chart` adds a `_hl` param to linked charts, so the
    page can fade the other marks. Charts with a temporal x axis get a rule layer at a `_t`
    param (epoch ms). The page sets `_t` to the current frame's time, or to Nicosia's current
    time when no time-lapse is playing, and to null when that moment is outside the chart.
    `show_chart` also colours a `state` field with the map's colours. The page reads chart
    hovers from Vega's pointer events.
  - **`show_visual`** sends model-written JavaScript and its datasets to the page. The page runs
    it in an iframe with `sandbox="allow-scripts"` (an opaque origin). Its CSP allows only the
    pinned library URLs and the page's two inline scripts (by hash), with `connect-src 'none'`.
    Runtime errors go back to Claude in the next turn's context.
- [chatbot/history.py](chatbot/history.py) builds time-lapses. It reads past readings with
  `getTelemetryRange`, one request per device, about 2 s for all 240 lamps. It then interprets
  them at each frame with `collector/interpret.py`, so past frames follow the same rules as
  live readings. It doesn't need the collector's database.
- [chatbot/city.py](chatbot/city.py) reads the Smart Nicosia API on demand, at most every 5
  minutes per device type. It interprets readings with the collector's `collector/interpret.py`,
  so a lamp is "on" only if it reported in the last 70 minutes.
- [chatbot/static/](chatbot/static/) is the page: MapLibre GL with OpenFreeMap tiles, vega-embed
  and marked. There is no build step.
- Caches (device attributes, the OSM places index, the Vega-Lite schema) live in
  `data/chatbot_cache/`. The places index takes about a minute to build from Overpass on first
  start; until then place search falls back to Photon.

## What the Smart Nicosia API provides

API Docs URL: https://nokia.smartnicosia.eu/api-docs

Base URL: https://nokia.smartnicosia.eu/backend/openapi

The API exposes device inventory, telemetry, attributes, alarms, and traffic
analytics endpoints. All endpoints in this repo are simple GET requests that
return JSON.

Inventory and device data:
- `getTenantDevices`: list devices associated with a tenant (device name/type and
  key/value metadata).
- `getLatestTelemetry`: latest telemetry values for a device (optionally filter
  by keys).
- `getLatestAttribute`: latest attributes for a device.
- `getTelemetryRange`: telemetry values for a device within a time range; the
  API limits records without credentials.
- `getAlarms`: alarms for a device or tenant; supports limit and state filters.

Traffic analytics:
- `object_types`: supported traffic object types (for example people, car, bus).
- `analytics`: camera list and analytics configuration data.
- `getTrafficReports`: traffic statistics by camera IDs and time grouping.
- `generalReports`: averaged speed or speed-per-mile over time windows.
- `hourlyReports`: hourly summaries for a given ISO week/year.

Observed response shapes (from `sample_payloads`):

- `getTenantDevices`: JSON list of devices; each item has
  `device_name` (str), `device_type` (str), `device_key_type` (list of
  `{key: str, value: str}`), and `device_label` (null in sample).
- `getLatestTelemetry`: JSON list of `{key: str, timestamp: str, value: str}`.
- `getLatestAttribute`: JSON list of `{key: str, timestamp: str, value: str}`.
- `getTelemetryRange`: JSON list of `{key: str, timestamp: str, value: str}`.
- `getAlarms`: JSON list of alarm objects with keys
  `id`, `created_time`, `ack_ts`, `clear_ts`, `additional_info`, `end_ts`,
  `originator_type`, `severity`, `start_ts`, `status`, `type`. In the sample,
  timestamps are strings and `additional_info` is a JSON string.
- `object_types`: JSON object mapping numeric string keys to object type labels
  (for example `"0": "people"`).
- `analytics`: JSON object with keys `analytics` (list), `groups` (list),
  `id`, `name`, `parent_id`. Each item in `analytics` includes fields like
  `module`, `stream`, `status`, and URLs.
- `getTrafficReports`: JSON object with `status` and `data`. `data.group_by` is
  a list of buckets like `{key: "DD-MM-YYYY", data: {<id>: {1: n, 2: n, total: n}}}`.
- `generalReports`: JSON list of `{label: str, time: int, value: str}`.
- `hourlyReports`: JSON object keyed by weekday names (`MONDAY`, `TUESDAY`, ...)
  mapping to per-day report objects (empty objects in the sample).

## Response examples and meaning

Examples below are taken directly from `sample_payloads` and reflect the exact
inputs used to generate those files.

### getTenantDevices

Input used: `entityType=TENANT`, `entityName=CYTA`

Example response item:
```json
{
  "device_name": "YW1394",
  "device_type": "Wastebin",
  "device_key_type": [
    {
      "key": "latitude",
      "value": "35.167214"
    },
    {
      "key": "longitude",
      "value": "33.379694"
    }
  ],
  "device_label": null
}
```
Meaning: one device linked to tenant CYTA, with location metadata stored as
key/value pairs.

### getLatestTelemetry

Input used: `entityType=DEVICE`, `entityName=YL1015`

Example response item:
```json
{
  "key": "isAlert",
  "timestamp": "1767412779000",
  "value": "false"
}
```
Meaning: the latest telemetry point for key `isAlert` on device YL1015.

### getLatestAttribute

Input used: `entityType=DEVICE`, `entityName=YL1015`

Example response item:
```json
{
  "key": "active",
  "timestamp": "1767860388175",
  "value": "false"
}
```
Meaning: the latest attribute value for key `active` on device YL1015.

### getTelemetryRange

Input used: `entityType=DEVICE`, `entityName=YL1015`,
`startTs=<utc_now_minus_24h_ms>`, `endTs=<utc_now_ms>`

Example response item:
```json
{
  "key": "active",
  "timestamp": "1767860388175",
  "value": "false"
}
```
Meaning: one telemetry reading within the requested time range.

### getAlarms

Input used: `entityType=DEVICE`, `entityName=YW1394`, `limit=10`

Example response item:
```json
{
  "id": "b5d8a6d0-db95-11f0-a254-0f02858f45da",
  "created_time": "1766009649597",
  "ack_ts": "0",
  "clear_ts": "1766045281338",
  "additional_info": "{\"devicename\":\"YW1394\",\"devicetype\":\"Wastebin\",\"forensic_info\":{}}",
  "end_ts": "1766009649595",
  "originator_type": 5,
  "severity": "MAJOR",
  "start_ts": "1766009649595",
  "status": "CLEARED_UNACK",
  "type": "Lost connection"
}
```
Meaning: a cleared but unacknowledged alarm of type "Lost connection" for device
YW1394. `additional_info` is a JSON string with device metadata.

### object_types

Input used: no parameters

Example response:
```json
{
  "0": "people",
  "1": "bike",
  "2": "car",
  "3": "motorcycle",
  "4": "bus",
  "5": "truck"
}
```
Meaning: mapping of numeric type IDs to labels used by the traffic analytics
endpoints.

### analytics

Input used: no parameters

Example response item (excerpt):
```json
{
  "id": 117,
  "name": "Stasikratous (Mnasiadou crosspoint)",
  "type": "traffic",
  "status": "started",
  "h264_url": "http://172.22.2.33:3020/h264/117",
  "stream": {
    "id": 70,
    "name": "Stasikratous (Mnasiadou crosspoint)",
    "location": {
      "latitude": 49.2330829,
      "longitude": 28.4682169
    }
  }
}
```
Meaning: one analytics configuration entry for a traffic camera, including
stream metadata and playback URLs.

### getTrafficReports

Input used: `va_ids=[]`, `group_by=day`,
`start_date=<utc_now_minus_24h_ms>`, `end_date=<utc_now_ms>`

Example response item (excerpt):
```json
{
  "key": "07-01-2026",
  "data": {
    "0": {
      "1": 13414,
      "2": 13016,
      "total": 26430
    }
  }
}
```
Meaning: one daily bucket (`key`) with counts per numeric type ID and a `total`.

### generalReports

Input used: `group_by=ONE_HOUR`, `line_type=AVERAGE_SPEED`,
`start_date=<utc_now_minus_24h_ms>`, `end_date=<utc_now_ms>`

Example response item:
```json
{
  "label": "01/07/2026 - 15:00",
  "time": 1767792868000,
  "value": "29.8"
}
```
Meaning: one aggregated time bucket with an average speed value.

### hourlyReports

Input used: `week=<iso_week_utc>`, `year=<iso_year_utc>`

Example response item (excerpt):
```json
{
  "WEDNESDAY": {
    "0": {
      "average_speed": "47.4",
      "average_speed_miles": "29.4",
      "cars": 11
    }
  }
}
```
Meaning: per-weekday buckets with per-time-slot aggregates such as
`average_speed` and `cars`.

## Script overview

The script provides reusable functions for these endpoints:

- `getTenantDevices`
- `getLatestTelemetry`
- `getLatestAttribute`
- `getTelemetryRange`
- `getAlarms`
- `object_types`
- `analytics`
- `getTrafficReports`
- `generalReports`
- `hourlyReports`

It also includes helpers:

- `get_example_payloads(...)` to fetch all endpoints in one call.
- `save_example_payloads(...)` to write those results as JSON files.

## CLI usage

Fetch tenant devices (original curl equivalent):

```bash
python smart_nicosia_api.py --entity-type TENANT --entity-name CYTA --pretty
```

Write sample payloads to disk:

```bash
python smart_nicosia_api.py --save-examples sample_payloads --insecure
```

If you need TLS verification, pass a CA bundle:

```bash
python smart_nicosia_api.py --save-examples sample_payloads --cafile C:\path\to\ca-bundle.pem
```

## Jupyter usage

```python
from smart_nicosia_api import get_example_payloads, get_alarms

examples = get_example_payloads(insecure=True)
alarms = get_alarms(entity_name="YW1394", limit=10, insecure=True)
```

## How sample_payloads were created

The `sample_payloads` directory was generated by calling:

```bash
python smart_nicosia_api.py --save-examples sample_payloads --insecure
```

Defaults used by `get_example_payloads(...)`:

- `tenant_name="CYTA"`
- `device_name="YL1015"`
- `alarm_device_name="YW1394"`
- `traffic_group_by="day"`
- `line_type="AVERAGE_SPEED"`
- `start_ts/end_ts`: last 24 hours in UTC (milliseconds)
- `hourlyReports`: ISO week/year from current UTC time

Each file is named after its endpoint (for example, `sample_payloads/getAlarms.json`).

Exact requests used for each sample payload:

- `sample_payloads/getTenantDevices.json`
  - `GET /getTenantDevices?entityType=TENANT&entityName=CYTA`
- `sample_payloads/getLatestTelemetry.json`
  - `GET /getLatestTelemetry?entityType=DEVICE&entityName=YL1015`
- `sample_payloads/getLatestAttribute.json`
  - `GET /getLatestAttribute?entityType=DEVICE&entityName=YL1015`
- `sample_payloads/getTelemetryRange.json`
  - `GET /getTelemetryRange?entityType=DEVICE&entityName=YL1015&startTs=<utc_now_minus_24h_ms>&endTs=<utc_now_ms>`
- `sample_payloads/getAlarms.json`
  - `GET /getAlarms?entityType=DEVICE&entityName=YW1394&limit=10`
- `sample_payloads/object_types.json`
  - `GET /object_types`
- `sample_payloads/analytics.json`
  - `GET /analytics`
- `sample_payloads/getTrafficReports.json`
  - `GET /getTrafficReports?va_ids=[]&group_by=day&start_date=<utc_now_minus_24h_ms>&end_date=<utc_now_ms>`
- `sample_payloads/generalReports.json`
  - `GET /generalReports?group_by=ONE_HOUR&line_type=AVERAGE_SPEED&start_date=<utc_now_minus_24h_ms>&end_date=<utc_now_ms>`
- `sample_payloads/hourlyReports.json`
  - `GET /hourlyReports?week=<iso_week_utc>&year=<iso_year_utc>`


## Collector: building history in DuckDB

`collector/` polls the Smart Nicosia API (and the national Bluetooth travel-time feed) into a
local DuckDB file, so the chatbot has weeks of history to work with. It is Phase 0 of the
digital-twin plan (`docs/digital-twin-research.md`, sections 2, 7 and 8).

```bash
uv sync
uv run collector run --once      # one pass over the jobs that are due, then exit
uv run collector status          # last runs, row counts, freshness, device health, errors
uv run collector budget          # requests per day at the configured cadences
uv run collector backfill        # gentle one-off history fetch (default: last 30 days)
uv run pytest
```

Data goes to `data/smart_nicosia.duckdb` and the log to `data/collector.log`. Copy
`collector.example.toml` to `collector.toml` to change cadences or paths.

### What it stores

| Table | Contents |
|---|---|
| `devices`, `attributes` | Inventory (740 devices) with street names, metadata, `lastActivityTime` |
| `telemetry` | `(device, key, ts, value, value_num, source)`, deduplicated on `(device, key, ts)` |
| `state_changes` | Parking occupied/free, lamp lit/dark, bin fill band, CAQI band, camera and screen state |
| `events`, `alarms` | The city's alarm log (TENANT `toastDesc`) and `getAlarms` (`ACTIVE_UNACK`) |
| `traffic_counts` | Daily counts per camera, counting line and object type; hourly city-wide counts |
| `speeds` | City-wide average speed, 5-minute buckets |
| `travel_times` | Cyprus NAP Bluetooth travel time and speed per road segment, every 5 minutes |
| `traffic_cameras` | 68 analytics feeds: 29 linked to Camera devices by exact name, the rest from `data/camera_locations.csv` |
| `job_runs`, `api_errors` | What ran, how many requests, what failed |

`collector/interpret.py` is the one place that turns raw values into meaning (a lamp is on only if
it reported in the last 70 minutes; parking `status` 1 = occupied; bin and CAQI bands; online or
offline from `lastActivityTime`), always with a staleness flag.

### Polling volume

The defaults come to about 23,500 Smart Nicosia requests a day (`collector budget`), below the
25,000 we aim for until the polling agreement with the API owners is in place. The `stream` job
makes that possible: the TENANT time series repeats every device message (`deviceName` plus
`status`/`level`/`state` at the message's timestamp), so a few hundred requests a day record every
parking, lamp and bin change. That is why parking is polled per sensor only hourly (every 5 minutes
would be 87,000 requests a day on its own). This TENANT behaviour isn't documented, so confirm it
with the API owners too.

Hand-geocode the 39 camera feeds without coordinates by filling `lat`/`lon` in
`data/camera_locations.csv`. The next `cameras_map` run (daily) picks them up.

### Running unattended on Windows (Task Scheduler)

Run one pass every 5 minutes. Each pass runs only the jobs that are due, and a lock stops passes
from overlapping. In PowerShell (adjust `$repo`):

```powershell
$repo = "C:\Users\<you>\projects\smart-nicosia-api-exploration"
$uv = "$env:USERPROFILE\.local\bin\uv.exe"
$action = New-ScheduledTaskAction -Execute $uv -Argument "run collector run --once" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U
Register-ScheduledTask -TaskName "SmartNicosiaCollector" -Action $action -Trigger $trigger -Settings $settings -Principal $principal
```

The S4U principal runs the task in the background, whether or not you are logged on, without a
console window popping up. If Windows refuses to register it, run PowerShell as administrator,
or leave out `-Principal` (the task then runs only while you are logged on). Keep the PC awake, or
let `-StartWhenAvailable` catch up after sleep. Check on it with `uv run collector status`, and
remove it with `Unregister-ScheduledTask SmartNicosiaCollector`.

### Running in Docker

The container runs `collector run`, which loops and runs each job when it is due:

```bash
docker build -t smart-nicosia-collector .
docker run -d --name collector --restart unless-stopped -v "$PWD/data:/app/data" smart-nicosia-collector
docker exec collector uv run --no-sync collector status
```

Mount the repo's `data/` folder so the database outlives the container and the camera CSV is
used. Don't point Task Scheduler and Docker at the same database.

### Backfill

`collector backfill` pages `getTelemetryRange` backwards for each device (1,000 records per
page, shared across keys), with at most 2 requests in flight and 0.5 s between requests. It saves
its progress, so you can stop it and run it again later:

```bash
uv run collector backfill --since 30d                    # events, speed, traffic, then telemetry
uv run collector backfill --since 2026-01-01 --what traffic,speed
uv run collector backfill --types Parking --max-requests 2000 --concurrency 2
```

Traffic requests never span more than 30 days, because longer ranges take 30–60 s or time out.
The API's `endTs` is inclusive, so each page ends at the previous page's oldest timestamp, not one
millisecond before it. That way records split across a page boundary aren't lost.

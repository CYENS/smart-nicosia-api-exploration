# Smart Nicosia API Exploration

This repo contains a small Python helper (`smart_nicosia_api.py`) for querying the
Nokia Smart Nicosia API and saving example payloads for offline analysis.

## What the Smart Nicosia API provides

Base URL: `https://nokia.smartnicosia.eu/backend/openapi`

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


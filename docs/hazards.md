# Natural hazards in the watch

Status: built, 28 November 2026 (addition 6 of [what to add next](next-additions-research.md)).

When an earthquake is felt near Nicosia, an official weather warning is issued for Cyprus, or the
fire danger in the Nicosia district is very high, the twin raises it as an incident in its watch
(the inbox, the toasts, the live stream, the clock's marks), shows it on the map with its extent,
answers about it in chat, and keeps it in its memory so it replays with the city clock.

## The sources

| Hazard | Source | Read | What the twin keeps |
| --- | --- | --- | --- |
| Earthquakes | EMSC's FDSN event service (`seismicportal.eu`), which carries the Cyprus Geological Survey Department's own solutions (`auth` GSD) | Every 5 minutes while a page is open, every 15 otherwise: the last two days (31 on the first read) in a box around the island | Time, place, magnitude, depth, who located it, its distance and direction from Nicosia's centre and its district |
| Felt reports | EMSC's testimonies service | The count for quakes of M2 or more within 150 km of the last three days (one small request); the positions (a zip of about 1 MB) only for a quake that raises an incident, or one within 40 km, at most four times in its first day | How many people reported feeling it, how many within 25 km of Nicosia, how they described it (the median intensity, EMS-98), and 2 km cells for the map |
| Weather warnings | Meteoalarm's feed for Cyprus, from the Department of Meteorology: the JSON API (with the warning's own words), the legacy Atom feed if it fails | Every 15 minutes | Level (yellow, orange, red), kind, area (Cyprus warns for the island as one area, EMMA CY001, so Nicosia is always included), onset, expiry, headline, description and instruction; a warning that leaves the feed before its expiry is marked withdrawn and ends then |
| Fire danger | EFFIS's fire weather index forecast (`mf010.fwi`), read from its map image over the island (its feature info answers with empty values) | Every 3 hours: today and the next two days, three images of about 1 KB. A past day the clock asks for is read once and kept on disk (`data/chatbot_cache/hazards/`) | The Nicosia district's level (the highest class at least a quarter of the district reaches), the share at each class, the class at the city's centre, and the class grid for the map |
| Outlines | geoBoundaries (from OpenStreetMap, ODbL), `data/cyprus_areas.json` | Kept with the twin | The island and its six districts |

Everything read goes into the memory's own `hazards` table, by id (`eq-20251112_0000180`,
`wx-cy-250723170828-maxtemp-000`, `fire-2025-07-23`), so a revised magnitude or an updated warning
replaces its row. With `CHATBOT_HAZARDS=0` the server reads none of the feeds and shows what the
memory holds.

## When the watch raises one

How loud each hazard is lives in one place, `chatbot/hazards/feeds.py`:

- **Earthquake**: raised for M3 or more within 100 km of Nicosia's centre, M4.5 within 250 km, M2.5
  within 20 km, or anyone within 25 km reporting it felt. **High** (a toast on every open page)
  when ten or more people within 25 km reported feeling it, M5 within 150 km or M4 within 50 km;
  medium for M4 within 100 km, M3.5 within 50 km or a felt report nearby; low otherwise. A small
  distant quake never pages. It stays happening for three hours; quakes within 60 km and a day of
  each other are one incident (a main shock and its aftershocks), and a later strong one reopens it.
- **Weather warning**: every warning for Cyprus. Yellow is low (the inbox only); orange and red are
  high. Warnings of one kind whose hours fall within 12 hours of each other are one incident (a
  heatwave's run of daily warnings). Active from when it was issued to its expiry.
- **Fire danger**: days in a row with the district very high or worse, up to today (forecasts don't
  raise one). Very high is medium; extreme and very extreme are high.

On the 30 days before 27 November 2026 (29 small quakes, the largest M3.2 at 106 km) nothing is
raised. On 12 November 2025 the M5.2 off Paphos (91 km, 817 felt reports near Nicosia) raises one
high incident that grows with its aftershocks and reopens with the M5.3 that afternoon.

## How it is built

- `chatbot/hazards/feeds.py`: the parsers, the places (`where`), the thresholds.
- `chatbot/hazards/service.py`: `Hazards` (one per Agent): the polling thread, the throttles, the
  memory table, `moment(at)` for the clock.
- `chatbot/hazards/entities.py`: the kinds `earthquake`, `warning` and `fire_danger` (with
  `fire-nicosia` for today) for the inspector, search and `get_entity`.
- `chatbot/hazards/layer.py`: one layer shape for the page and the chat tool.
- `chatbot/watch/hazards.py`: the detectors `quake`, `weather_warning`, `fire_danger` and their
  incident families (how they link, the place, the title and story, why they cleared), plugged in
  through `detectors.register` and `incidents.register_family`, so the shared lists stay untouched.
- `chatbot/routes/hazards.py`: `/api/hazards?at=` (the layer at the clock's moment),
  `/api/hazards/items?ids=` (an incident's extent), `/api/hazards/marks` (the clock's track).
- `chatbot/tools/hazards.py`: `get_hazards`, with a `layer_ref` and a `data_ref`.
- The page: `lib/hazards/` (the renderer, the Layers row under Alerts, the clock marks, the incident
  extent, Ctrl K) and `components/hazards/parts.tsx` (the row's words and legend, the Outside line).

## Seeing an incident without waiting for one

`tools/hazards_replay.py` writes the real events of the fixtures into a scratch memory, moved to
now: the M5.2 and its aftershocks, the first Orange heat warning of July 2025 in force, and that
July's extreme fire danger as yesterday's and today's.

```bash
uv run python tools/hazards_replay.py data/hazards_replay.duckdb
CHATBOT_HAZARDS=0 COLLECTOR_DB=data/hazards_replay.duckdb CHATBOT_PORT=8270 uv run chatbot
```

The watch looks within five minutes and raises three high incidents.

## Limits

- Who is near is the felt reports, the distance and the district; a population count waits for the
  people dataset (additions 5 and 7).
- Felt reports are what people told EMSC, positions rounded, not a measurement.
- A warning issued and withdrawn while the server wasn't running is never seen: Meteoalarm keeps
  no history. EMSC and EFFIS can be read back.
- EFFIS's index is a forecast on a grid of about 9 km; burnt areas are not read yet.

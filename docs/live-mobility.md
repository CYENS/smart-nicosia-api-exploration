# Live mobility

Feature 4 of the [roadmap](digital-twin-roadmap.md): buses on the road, departures, bus trips
and road speeds, as map layers, inspector kinds, a workspace and chat tools.

## Sources

All from the Cyprus National Access Point ([traffic4cyprus.org.cy](https://traffic4cyprus.org.cy)),
CC BY 4.0.

| What | Where | How often | Notes |
| --- | --- | --- | --- |
| Bus timetables (GTFS) | `motionbuscard.org.cy/opendata/downloadfile?file=GTFS%5C<n>_google_transit.zip`, n = 9 NPT, 5 Intercity, 11 Pame Express | Cached in `data/chatbot_cache/gtfs/`, fetched again after a day | Stop ids are one national registry, so the three feeds merge. calendar_dates only today; calendar.txt is read too |
| Live buses (GTFS-RT) | `http://20.19.98.194:8328/Api/api/gtfs-realtime` | At most every 30 s, one fetch for every page and tool | Island-wide, about 440 KB, plain HTTP on a raw IP. Its trip ids are its own: vehicles are matched by route, direction and first departure. The last good copy stands in for 5 minutes when it fails |
| Road travel times (DATEX II) | `.../swarco3/api/Data/PredefinedLocationDataPublication` | At most every 5 minutes, recorded into the memory | The same feed the collector's `travel_times` job polls |
| Road geometry and site names | `PredefinedLocationPublication`, `BTMeasurementSiteTablePublication` | Cached, daily | The live publication's line strings are empty; the paths inventory holds them as escaped GML (`lat,lon` pairs) |

## How it is built

- `chatbot/mobility/gtfs.py`: the timetable (stops, lines per operator, trips with their stop
  times as arrays, shapes), departures by stop over service days (times past 24:00 belong to
  the day before), stop distances along each trip's shape.
- `realtime.py`: the fleet. Each vehicle is placed along its trip's shape (looking forward from
  where it was, as loops pass places twice), gets its next stops, and a delay: the feed's
  trip update when believable (between 10 minutes early and 30 late, never early at the first
  stop), else from its position against the timetable, said as an estimate.
- `planner.py`: departures with the live estimate, and walk-ride-walk trips, direct or with
  one change (same stop or within 200 m), leaving in the next 90 minutes, ranked by arrival
  with a little weight against changes and walking. It says when walking beats every bus.
- `roads.py`: segments wholly inside Greater Nicosia, speeds now or at a moment from the
  memory, free flow (measured: 85th percentile of 4 weeks once a day is stored; estimated:
  Valhalla's typical speed for the road, fetched once and kept in
  `data/chatbot_cache/road_reference.json`; assumed: 50 km/h until that estimate lands), the
  usual for this hour of this weekday, and bands: moving freely (75% of free flow or more),
  busy (55%), slow (35%), very slow. Readings under 3 km/h are a phone that stopped between
  the detectors, not the road, and count as no reading.
- `entities.py`: the kinds `bus` (bus-5006), `bus_stop` (stop-1297), `bus_line`
  (line-npt-26, line-ic-42, line-pame-gsp1) and `road_segment` (road-12), with search items
  and a road's 24-hour series, through `register_kind`, `register_search` and
  `register_series` in `chatbot/entities.py`.
- `chatbot/tools/mobility.py`: `get_buses`, `get_departures`, `plan_transit`,
  `get_road_speeds`. Their layers carry `meta.live` ({kind: buses | roads}), so the page draws
  them with the live buses (gliding, filtered to the lines asked) or the road style (offset by
  direction), through `registerLayerRenderer` in the map engine.
- `chatbot/routes/mobility.py`: `/api/buses`, `/api/transit/shapes/{id}`, `/api/transit/stops`,
  `/api/roads?at=` (ISO local time or epoch ms, for the city clock) and `/api/mobility`.
- The page: `chatbot/web/src/lib/mobility/` (store, map layers, registration) and
  `components/mobility/` (inspector sections, the workspace and its switches). The Buses and
  Traffic topic chips and the Layers sheet's rows read the same store.

## On the map

Buses glide along their route shapes: between reports each moves on at its reported speed
(for 2 minutes at most) and its marker eases toward that point; a bus never slides
backwards. With reduced motion each report is shown as it comes. Roads are drawn offset to
the left of their direction of travel (Cyprus drives on the left), so both directions of an
avenue show. Their colours are a neutral for roads that flow and one warm hue that brightens
as they slow, stepped for each base map; the line widens too, so colour never carries it alone.

## When the road feed stalls

The national access point can keep answering, with a fresh publication time, while the
Bluetooth detectors behind it send nothing new: every path then carries the same old
measurement time. On 26 November 2026 they stopped at 23:42 and the feed republished that
reading through the next day. The twin treats this as its own state, never as "no readings":

- `Roads.speeds()` says `status: "stalled"`, with `last_measured_at` and a `note` in plain
  words ("The national road detectors have sent nothing new since Thursday 23:42."). No road
  has a speed then. `Roads.health()` gives the same without fetching (read time, error, newest
  measurement, publication time).
- Each road with a usual for this hour and weekday from two or more days of the memory gets a
  `usual_state`; the map draws those roads as muted dots, wider where the road is usually
  slower, and the legend calls them "Usual for this hour". A road the memory knows too little
  about stays "No reading". Nothing usual is ever presented as a reading, and while the feed is
  live the usual stays on the speed meter only.
- The Traffic now card, the Layers row and its legend, the Mobility workspace, a road in the
  inspector, `get_road_speeds` and the system prompt all say it.
- The data sources panel judges the feed by its newest measurement: live, stale (the last read
  failed but the copy still counts), stalled, or not answering.
- The watch raises one "Data feed" incident (`road_feed_stalled`) while the feed is stalled
  and the twin resolves it once a fresh measurement comes. It reads the feed as the page's last
  fetch left it and never fetches for itself, so it says nothing either way when nobody has read
  the feed for 15 minutes. Its member, `source-road_speeds`, opens the data sources panel.
- The memory keeps a frozen reading once (`travel_times` is keyed on the path and the
  measurement time), so a stall never weighs on free flow, the usual or a road's series.

The feed is published by the Public Works Department through traffic4cyprus.org.cy; a long
stall is theirs to fix.

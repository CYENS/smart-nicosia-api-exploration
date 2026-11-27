# Road reports

What is happening on Nicosia's roads besides speed: closures, jams, accidents and hazards that
Waze drivers report, and the roadworks and diversions the Public Works Department enters. They
sit beside the road speeds of [live mobility](live-mobility.md): on the map, on the Traffic card,
in the Layers sheet, in chat, in the inspector, on the city clock and in the watch.

## Sources

All from the Cyprus National Access Point ([traffic4cyprus.org.cy](https://traffic4cyprus.org.cy)),
DATEX II, CC BY 4.0.

| What | Where | How often | Notes |
| --- | --- | --- | --- |
| Waze alerts | `fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/` ([dataset](https://traffic4cyprus.org.cy/dataset/waze_alerts)) | At most every 3 minutes | About 220 alerts island-wide, 40 to 50 in Greater Nicosia. Each has an id, comments typed `type`, `subtype`, `street` and `report_time` (Nicosia time, no zone), and a point. A closure comes as several points along the closed stretch, reported within seconds of each other. Most closures stay for weeks or months |
| Waze traffic (jam lines) | `fixcyprus.cy/gnosis/open/api/nap/datasets/waze_traffic/` ([dataset](https://traffic4cyprus.org.cy/dataset/waze_traffic)) | At most every 3 minutes; every 15 while it fails | Answered HTTP 503 (an HTML error page) on 27 Nov 2026. The catalogue says it gives Waze's jam level (0 free flow to 5 closed); its shape is assumed from the alerts and read defensively |
| Traffic events | `traffic4cyprus.org.cy/swarco3/api/Data/SituationPublication` ([dataset](https://traffic4cyprus.org.cy/dataset/trafficevents)) | At most every 15 minutes | About 20 situation records island-wide (2 in Nicosia on 27 Nov 2026): `MaintenanceWorks`, `ConstructionWorks`, `GenericSituationRecord`, with a subtype, a planned start and end (Nicosia time), a point and a description. A description written in Greek comes again as a separate English record |

The variable message signs (`VmsTablePublication`, `VmsPublication`) were left out: on 27 Nov
2026, 33 of the 36 signs reported `notWorking` and none showed any text.

## How it is built

- `chatbot/mobility/reports.py`: the three parsers, one `RoadReport` for every source (kind,
  English label, street, the publisher's own words, point, a line for a closed stretch or a jam,
  since, until, a stable id), and `RoadReports`, one per `Mobility`:
  - Ids: `report-waze-<8 hex>` (the alert's UUID; a grouped closure takes its first point's),
    `report-jam-<8 hex>` and `report-works-<record number>`.
  - Kinds: closure, accident, jam, hazard, roadworks, diversion. Waze's types and subtypes map to
    them (`HAZARD_ON_ROAD_CONSTRUCTION` is roadworks); the operators' records by type and subtype
    (works, "diversion in operation", "accident(s)").
  - Closures are grouped: points on one street reported within 10 minutes and 1.5 km of each
    other are one closure; chained by nearest neighbour, they are drawn as a line when every
    hop is under 600 m.
  - A Greek record and its English twin (same kind and label, within 150 m, overlapping dates)
    become one report that keeps the Greek as its text and the English beside it.
  - Only Greater Nicosia is kept (the road speeds' area).
  - One fetch per feed for every page, tool, the inspector and the watch; a first read under way
    is waited for. A failed fetch keeps the last good copy (30 minutes for Waze, 6 hours for the
    operators' events) and says so. An error page is no report; an empty Waze feed is a failure,
    never "nothing reported".
- The twin's memory: each good fetch goes to the recorder (`Recorder.road_reports`), which
  upserts the reports into the `road_reports` table (first and last sighting) and adds the feed's
  coverage (`coverage`, device type `RoadReports`, device = the feed). The city clock's moment
  `t` shows the reports seen within a few minutes of `t` by a feed the twin was reading then; a
  moment no feed was read is unknown, never empty. Works planned to start later are "planned"
  until they start, and ended ones are left out.
- `chatbot/mobility/report_entities.py`: the `road_report` kind (title, state by kind and age,
  the measured road segment it sits on within 60 m, the reports within 300 m, places nearby,
  its source), its reading at a moment of the clock, and search items (group "Road reports").
- `chatbot/tools/roadreports.py`: `get_road_reports`, a domain of its own at the end of
  `DOMAINS`. Filters: kinds, a street, the centre (1.6 km around Eleftheria Square), near a
  place, along a drive (reports within 150 m of Valhalla's route), made in the last N minutes,
  a past moment. It returns counts, the reports loudest and newest first, works planned soon, a
  `layer_ref` (`meta.live.kind = road_reports`) and a linked `data_ref`.
- `chatbot/routes/reports.py`: `/api/road-reports?at=`, a GeoJSON layer with a summary for the
  card (counts, what was reported in the last 30 minutes, the newest jam, long closures, a short
  list).
- `chatbot/routes/sources.py`: a freshness probe per feed that judges by the times inside it
  (its publication time, its newest report), not by its HTTP status.
- The watch (`chatbot/watch/detectors.py`): `road_closure` for a closure that was news when the
  twin first saw it (reported 6 hours or less before), so the first look at the feeds never
  floods the inbox with closures from July; `road_jam` for a standstill or heavy jam (or a jam
  line of level 3 or more) and for an accident (high severity). They group with each other in the
  family `roads` (500 m, an hour), and end when the report leaves its feed. While a page listens,
  the watch reads the feeds itself (`Watcher.feeds`).

## On the page

`chatbot/web/src/lib/reports/` (store, map, registration) and `components/reports/parts.tsx`.

Each kind has its own shape as well as its colour, so it reads at a glance on the night map:
a no-entry disc for a closure (ink white), a burst in signal red for an accident (the only kind
in signal red), a warning triangle for a hazard (amber), a queue of cars for a jam (the road
speeds' "very slow"), a diamond with a cone for roadworks (teal), and a square with a bent arrow
for a diversion (blue). A closed stretch is a dashed ink line; a jam line widens with its level.
A report made in the last 30 minutes wears a thin sodium ring; one older than a week is drawn
smaller and fainter, and works that start later fainter still.

- The Traffic card's "Road reports" section says what drivers report, true even while the road
  speeds have no readings ("Waze drivers report 2 jams now, the latest 3 min ago"), lists the
  newest reports (arrow keys, Enter opens one in the inspector, centred clear of the card), and
  puts the reports on the map while the card shows.
- The Layers sheet's "Road reports" row (under Sensors, after Road speeds), with its legend.
- Ctrl K: "Show road reports", and every report by street, kind or label.
- The inspector: a report's kind, age or dates, where it is, its road segment and neighbours,
  and the operators' own words in "As published" (marked as Greek where they are).
- An answer's layer draws its own reports the same way; the general layer steps back meanwhile.
- Everything follows the city clock.

## Limits

- Waze's jam lines answered 503 throughout the build. Their parser follows the alerts' shape
  and the catalogue's description, and is tested on a made-up sample; when the feed returns,
  check one real fetch against `parse_waze_traffic`.
- A report's interval in the memory is from its first to its last sighting: one that left a feed
  and came back days later would read as present in between, where the coverage says the feed
  was read.
- Waze reports are what drivers report, not official, and a Waze closure can outlive the works.

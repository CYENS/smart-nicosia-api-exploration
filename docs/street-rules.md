# Street rules and safety

The twin knows the rules and safety features of Nicosia's streets: speed limits, 20 and 30 km/h
zones, traffic signals, pedestrian crossings, cycleways, bus lanes and enforcement cameras. They
come from national inventories, not readings, so they stand at every moment of the city clock
and always say how old they are.

## Where it comes from

Seven DATEX II files on the Cyprus National Access Point (traffic4cyprus.org.cy, CC BY 4.0),
served from `https://fixcyprus.cy/gnosis/open/api/nap/datasets/<name>/` and regenerated nightly.
The Public Works Department publishes all but the cameras (Elovate, for the Ministry of
Transport). The catalogue says when each was last changed by its publisher (March 2025 for most,
May 2025 for the zones, November 2025 for the cameras).

| Inventory | Island | Greater Nicosia | Notes |
| --- | --- | --- | --- |
| `speed_limits` | 2,059 signs | 333 | a value at a point, with a national road reference (B00220, E10030) |
| `low_speed_emission_zones` | 8 zones | 3 | Aglantzia, Kaimakli, Pallouriotissa; all **planned**; latitude and longitude written swapped |
| `low_speed_emission_entrance_point` | 124 | 39 | small polygons, read as their centres |
| `traffic_signals` | 363 | 161 | the junction's streets and the council that runs it |
| `pedestrian_crossings` | 384 | 129 | a comment naming the street and what it is near |
| `cycleways` | 48 lines | 8 | some records hold many polylines |
| `bus_lanes` | 26 lines | 12 | every element in its own numbered namespace; in use or planned |
| `traffic_enforcement_cameras` | 89 | 20 | four placed to two decimals only (about a kilometre) |

`chatbot/streets/feeds.py` reads each file at most once a day for every page and tool, keeps it
in `data/chatbot_cache/streets/`, reads everything by local tag name, puts swapped coordinates
right (in Cyprus the latitude is always the larger number), and keeps yesterday's copy when a
fetch fails.

The street network comes from OpenStreetMap: one Overpass query a week for Greater Nicosia's
drivable streets (names, classes, OSM's own `maxspeed` tags) and its schools, kept compact on
disk (`chatbot/streets/network.py`).

## What a speed sign governs

A sign is a point; a limit applies to a stretch. Each sign is snapped to its street (within 35 m,
preferring the class its national road reference implies) and its limit carried along that
street, through junctions while the street keeps its name, up to 800 m or until the next sign.
Each piece says how sure it is:

- **signed**: within 250 m of a sign, or between two signs that agree;
- **likely**: further along from one sign, with nothing saying otherwise;
- **uncertain**: between two signs that disagree, away from both: the limit changes somewhere
  there, and the inventory says neither where nor which way each sign faces;
- **mapped**: no national sign reaches the street, but OpenStreetMap's contributors tagged it.

A street neither reaches is left out: 50 km/h is the urban default, not a posted limit. On the
inventory of 27 November 2026 the signs cover about 72 km signed, 35 km likely and 36 km
uncertain; OpenStreetMap adds 537 km.

## What it adds to the rest of the twin

- **Road speeds**: each Bluetooth segment gets its posted limit along most of its length (74 of
  the 97 segments), shown beside its measured speed ("limit 50, traffic at 34") in the inspector
  and in `get_road_speeds`. A road with no better free flow uses its posted limit in place of the
  assumed 50 km/h (one hook, `Roads.posted_limit`, in `chatbot/mobility/roads.py`).
- **Schools and bus stops**: how each is served for crossing (`chatbot/streets/analysis.py`): a
  crossing or signals within 250 m of a school or 120 m of a stop, the main road beside it and its
  posted limit, its slow zone. A gap is a main road posted 50 km/h or more with neither. Places
  north of the buffer zone are left out: the inventories don't cover them.
- **The cycle network**: lines whose ends come within 25 m are one piece; the gaps are the
  shortest straight links (under 1.2 km) between pieces, each with the street it would run along.
- **Slow zones**: the streets inside, the signs posted inside (none yet), its crossings, signals,
  schools and entrances.

## In the page and in chat

- The Layers sheet's **Streets** group: speed limits, 20 and 30 zones, signals and crossings,
  cycleways, bus lanes, enforcement cameras. Off by default.
- Entity kinds: `limit-295` (a sign and its stretch), `limitway-<osm way>` (a mapped street),
  `zone-1`, `zentry-1`, `signal-0`, `xing-0`, `ecam-30`, `cycle-38`, `buslane-11`, `school-w123`.
  Zones, cameras, lanes, signals and crossings are searchable in Ctrl K.
- Chat tools: `get_speed_limits`, `find_slow_zones`, `find_crossings` (also around schools or bus
  stops), `get_cycle_network`, `get_bus_lanes`, `find_enforcement_cameras`.
- `/api/streets/layers/<name>` and `/api/streets/status` for the page; every inventory is a data
  source with its date in `/api/sources`.

## Left out

The ITS inventories (detector, CCTV and weigh-in-motion sites) belong to another pull request.
The live detector feed (`MeasuredDataPublication`) still carried no values on 27 November 2026.

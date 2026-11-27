# Park, charge and hire

Where people leave a car, charge it, or pick up a bike or a scooter: six national inventories,
one live feed, and the city's own parking sensors where they sit beside a car park.

## Sources

All six inventories are on the Cyprus National Access Point ([traffic4cyprus.org.cy](https://traffic4cyprus.org.cy)),
a CKAN catalogue, CC BY 4.0, DATEX II XML served from
`https://fixcyprus.cy/gnosis/open/api/nap/datasets/<name>/` and regenerated every night.
Counts as read on 27 Nov 2026, with Greater Nicosia (longitude 33.24 to 33.48, latitude 35.07
to 35.23) in brackets.

| What | Dataset | Publisher | Count | What it gives |
| --- | --- | --- | --- | --- |
| Car parks | `parking_areas` | Public Works Department | 651 (147) | A point and a name in English and Greek. 552 are named only "OSM Origin": mapped on OpenStreetMap with no name. No spaces, no price |
| Park and ride | `parkandride` | Public Works Department | 9 (1) | One or more polygons and a name |
| EV chargers | `electric_vehicle_chargers` | Department of Electrical and Mechanical Services | 171 (43) | Power per connector and in all, connectors, AC or DC, operator, owner, hours, access, support contact, and a status each night (139 operational, 32 unavailable): the listing's, not live |
| Nextbike stations | `nextbikestations` | Nextbike Cyprus | 343 (88) | A point, a name, a capacity on some |
| Bolt scooter zones | `boltstations` | Bolt | 255 (255) | A small polygon and how many scooters it holds |
| Bicycle racks | `bicycle_racks` | Public Works Department | 28 (9) | A name in English and Greek and an address (two are test entries and are left out) |
| Bike rental list | `bike_stations_rental` | Public Works Department | 67 (25) | Read only to name the Nextbike stations it repeats (within 40 m). Its other Nicosia entries are numbered stations of an earlier scheme that Nextbike's live feed no longer lists, so they aren't drawn |

The catalogue's own record of each dataset (`metadata_modified`) is read with it as the
inventory's date: car parks last changed 11 Mar 2025, chargers every night, Nextbike and Bolt
on 21 Nov 2025.

Live bikes come from Nextbike's own feed: GBFS 2.3 for Cyprus (`nextbike_cy`, listed by
MobilityData), CC0 1.0, no key. Its station ids are the inventory's. Bolt publishes no open feed
for Cyprus, so where the scooters are isn't known.

## How it is built

- `chatbot/mobility/facilities.py`: one parser per feed, reading DATEX II by local tag names
  (the feeds use different namespace prefixes). A record without a usable position in Cyprus is
  skipped and counted; swapped coordinates are put back. Each feed is read once a day at most for
  every page, tool and inspector, kept in `data/chatbot_cache/nap_facilities/`, and never kept
  when it doesn't parse or has no records; a failed refresh keeps yesterday's copy, says so, and
  tries again an hour later. Ids: `carpark-31`, `pnr-0`, `charger-1a2b3c4d` (the feed has no id:
  a hash of the published name), `nextbike-6495339`, `bolt-dac81462`, `rack-5`. Live bikes: one
  request every 2 minutes at most, and only while something asks; a failure keeps the last copy
  for 15 minutes as stale. A charger is "fast" at 43 kW or more per connector (the listing's own
  "fastCharging" is said too where it disagrees).
- `chatbot/mobility/facility_kinds.py`: the six entity kinds. A car park's live occupancy is the
  city's bay sensors within 60 m of its point (19 car parks in Greater Nicosia), now from the
  strip's last snapshot and at a past moment from the twin's memory; the car park's own spaces
  are never claimed. The OpenStreetMap car park within 40 m says whether it is paid. Every kind
  relates to the nearest bus stop, its street where it has an address, and the nearest of each
  other kind within 400 m. On the city clock the inventories stand as they are; a station's bikes
  are now only.
- `chatbot/tools/facilities.py`: `find_parking`, `find_chargers`, `find_bikes`, each with a
  `layer_ref` (marked `meta.facilities` so the page draws the kinds' own badges) and a `data_ref`
  linked by id.
- `chatbot/routes/facilities.py`: `/api/facilities/{kind}` (GeoJSON, a point per facility and
  its polygons as `part: area`), `/api/facilities/live?kinds=&at=` and `/api/facilities`.
- The page: `lib/facilities/` (store, map, registration) and `components/facilities/` (the
  Layers sheet's group, the Parking card's section, the inspector's sections).

## On the map

Each kind has a rounded badge in its own colour with its own glyph (P, P+R, a bolt, a bike, a
scooter, a rack stand), so colour never carries the kind alone; they gather into counted
clusters at city zoom, and a click on a cluster zooms in. Free sensed bays and bikes now sit
beside their badge, and only with it. A station with no bike, and a charger listed out of
service, go grey. Everything is off until asked for.

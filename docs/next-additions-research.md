# What the Nicosia twin should add next: research

Status: research, 27 November 2026. Builds on the [roadmap](digital-twin-roadmap.md), the
[first research](digital-twin-research.md), the [simulation research](simulation-research.md),
the [3D research](3d-twin-research.md), [live mobility](live-mobility.md), the
[memory](twin-memory.md), the [environment twin](environment-twin.md) and the sources in
`chatbot/sources.py`. It does not repeat the Cyprus National Access Point survey of the same day
(Waze, roadworks, signs, parking, chargers, bikes, scooters, street furniture and detector sites
are already being added); it names only what is still unused there.

Every source below was opened once or twice on the evening of Friday 27 November 2026 (about
21:30 Cyprus time), from plain HTTP with a generic client name. "Greater Nicosia" means
longitude 33.24 to 33.48, latitude 35.07 to 35.23. A source I could not reach is marked
**unverified** and is never ranked as available.

## Summary: the ten best additions

What the twin already does well: devices, buses and road speeds live and on the clock, weeks of
memory, sun, shade, terrain, modelled air and noise surfaces, incidents and watches, two
scenarios, and the city in 3D at its 2019 measured heights. What it lacks most is **ground
truth beside its models**, **the near future**, **people** (who lives where, who is exposed),
**the wider systems the city runs on** (power, hazards), and **an evening reason to open it**.

Ranked by what a person gains, against effort and risk. Effort: S part of a session, M one
feature session, L more than one.

| # | Addition | What a person gets | Data verified | Effort | Risk |
| --- | --- | --- | --- | --- | --- |
| 1 | **Measured weather and reference air** | Real thermometers, rain gauges and sun meters (4 Met Department stations in the city, every 10 minutes) and the official air stations beside the model surfaces; the 7 low-cost sensors checked against them | Yes (Met XML, EEA hourly files) | S to M | Low; Met reuse terms to confirm |
| 2 | **Forecasts with a track record** (roadmap 7) | The clock runs past now: parking, bins, air, road speeds and footfall for the next hours, with the error the method had on past weeks | Yes (the twin's own memory, Open-Meteo) | M to L | Medium: honest error bands needed |
| 3 | **The island's power, live** | Demand, wind and estimated rooftop solar every 15 minutes; the grid's carbon now, so the lamps' CO2 is measured, not assumed | Yes (TSO page) | S | Medium: an HTML table, no API |
| 4 | **Tonight in Nicosia** | Which six pharmacies are on duty tonight, what is on, what is open now, on the map and in one topic card | Yes (data.gov.cy CSV); events thin | S to M | Low; addresses need geocoding |
| 5 | **Heat from space, and who it reaches** | The hottest and coolest ground of last summer from 25 clear Landsat passes, set against where people live and where shade is | Yes (Planetary Computer STAC, Kontur) | M | Medium: surface, not air, temperature |
| 6 | **Hazards in the watch** | Earthquakes felt near the city, official weather warnings and fire danger raised as incidents, with the people near them | Yes (EMSC with GSD, Meteoalarm, EFFIS) | S | Low |
| 7 | **People and the 15-minute city** (roadmap 12, first part) | Population per hexagon; how many everyday services each place reaches on foot; who an incident or closure touches | Yes (Kontur CC BY 4.0, Overture, OSM) | M | Low |
| 8 | **Nicosia tonight: an ambient mode** (roadmap 11, first part) | A calm, self-running tour of what is alive now (buses, lamps, roads, weather, sky) for a lobby screen or a demo | Needs no new data | M | Low |
| 9 | **Things in the sky** | Aircraft over and around the city, the space station's passes, the moon, all on the clock | Yes (adsb.lol, CelesTrak), but sparse | S | Low; few aircraft on most evenings |
| 10 | **An open twin** (roadmap 13, second part) | Other systems and agents read the twin: an MCP server with the chat's own tools and a read-only SensorThings API | Needs no new data | M | Low; budget the city API |

The first six end in a brief a feature session can start from ([Briefs](#briefs-for-the-top-six)).

Things I looked for and could not use are in [Rejected](#rejected-and-why): water in the dams,
fuel prices, power cuts, crossing-point queues, FixCyprus reports, Let's Culture events,
heritage from Wikidata, night lights, Urban Atlas, satellite NO2 for streets, and the rest of the
access point.

---

## Part 1: Data the twin lacks

Each candidate: what a person could do, the test, how it plugs into the twin, effort, risks.

### D1. Department of Meteorology automatic stations

**What a person could do.** "How hot is it really in Nicosia now?" answered by a thermometer,
not a model. Rain that fell in the last 10 minutes, wind, global radiation (how strong the sun
is now) and ground temperature at 5 cm. The environment surfaces get a measured anchor, and the
watch can say "the model is 3 degrees off the city's own station".

**Test.**

| | |
| --- | --- |
| URL | `https://www.dom.org.cy/AWS/OpenData/CyDoM.xml` |
| Format | One XML file, about 105 KB: a `stations` list (code, latitude, longitude) and one `observations` block per station, each a list of named readings with a time |
| Update rate seen | Readings stamped 21:20 local at 21:31: every 10 minutes, about 10 minutes behind |
| In Greater Nicosia | 4 of 55 stations: `LEFKOSIA` (35.1644, 33.3561, in the centre), `ATHALASSA`, `KEPNIC`, `VISITOR` |
| What it measures | Air temperature at 1.2 m and humidity (54 stations), 10-minute rain (54), wind at 10 m (37), day maximum and minimum (49), global radiation (14), direct solar radiation (6), ground temperature at 5 cm (10), pressure (9 to 12) |
| Licence | "Weather Data Copyright The Cyprus Department of Meteorology", "intended as a guide only". No open licence stated in the file; reuse with credit is the norm for the Department's open data page, but confirm |
| Key | None |
| Fragile | Station sets vary (KEPNIC has 3 readings); names are codes; local time with no offset |

**Plugs in.** A new kind `weather_station` (`met-LEFKOSIA`) in `chatbot/entities.py` with a
24-hour series; the recorder stores readings every 10 minutes into the memory, so the clock
replays them and `query_history` reaches them. `get_weather` reports measured beside modelled;
`environment_surface` uses the stations to correct the temperature surface the way it already
corrects air with the city sensors (`surfaces.py`). The watch gains "station disagrees with the
model" and "heavy rain now" detectors. A Layers sheet row under Environment shows the stations
with their readings; the Outside card says "measured at Lefkosia station".

**Effort** S. **Risks** low: the reuse terms are not written in the file.

### D2. Reference air quality: the national network

**What a person could do.** See the official air stations next to the 7 smartsense sensors and
the model; "is the city's own sensor reading high, or is the air really bad?" The
twin can publish how far each low-cost sensor sits from the nearest reference station, which is
exactly the honest uncertainty the environment twin promises.

**Test.**

| | |
| --- | --- |
| EEA up-to-date files | `POST https://eeadmz1-downloads-api-appservice.azurewebsites.net/ParquetFile/urls` with `{"countries":["CY"],"pollutants":["NO2"],"dataset":1,"source":"Api"}` returns one Parquet URL per station and pollutant (`https://eeadmz1batchservice02.blob.core.windows.net/airquality-p/CY/SPO-CY0004A_00008_100.parquet`) |
| Format | Parquet, one row per hour: start, end, value, unit, validity, verification, the time the file was written |
| Update rate seen | Last hour 18:00 to 19:00, file written at 20:15, read at 21:35 local: hourly, one to three hours behind. Values flagged not yet verified |
| In Greater Nicosia | Three Cypriot stations publish: `CY0002R` (Agia Marina Xyliatou, rural, outside the box) and the urban `CY0004A` and `CY0006A`. NO2 and O3 at all three, PM10 at two, PM2.5 only at the rural one. Which of the two urban codes are the Nicosia traffic and residential stations is **unverified** (station metadata not fetched) |
| A reading | `CY0004A` NO2 86.4 µg/m³, 18:00 to 19:00 |
| Licence | EEA data, reuse with credit (EEA's standard terms, CC BY 4.0) |
| Key | None |
| National site | `https://www.airquality.dli.mlsi.gov.cy/` shows real-time values in a browser ("Nicosia - Traffic Station PM10: 123.4 µg/m³" at 21:37, "not considered validated"). Plain HTTP clients get **403**; the page calls its own `/api/v1/` routes. Machine access needs the Department of Labour Inspection's agreement |

**Plugs in.** Kind `air_station` (`aq-CY0004A`), recorded hourly in the memory, drawn with the
smartsense sensors but marked "reference". `environment_surface` corrects the CAMS model with the
reference stations first and the low-cost sensors second, and its uncertainty shrinks only near
a reference station. A per-sensor "bias against reference" line in the inspector. Watch: "PM10
over the daily limit at a reference station" (dust episodes are the city's commonest air event).

**Effort** S to M (M with the calibration). **Risks**: few stations; the EEA files lag; the
national network's own API needs a letter, which would bring about 10 stations and every
pollutant in minutes rather than hours.

### D3. The transmission system: live demand, wind and solar

**What a person could do.** "How much power is Cyprus using now, and how much is wind and
sun?" The strip or an Energy topic shows demand and the renewable share; the lamps' energy
answers ("the lamps used 1,240 kWh this week") convert to CO2 with the grid's carbon at that
hour instead of the 0.55 kg/kWh the lighting scenario assumes (`grid_co2` in `sources.py`
already shows the range 0.49 to 0.63). The evening peak is visible: at 20:00 demand was 682 MW.

**Test.**

| | |
| --- | --- |
| URL | `https://tsoc.org.cy/electrical-system/total-daily-system-generation-on-the-transmission-system/` (also `.../total-daily-wind-and-solar-farm-generation/` and an archive page) |
| Format | HTML. The day's series sits in a hidden table `#production_graph_data`: time, available conventional capacity, wind, estimated distributed generation, total demand, conventional generation, in MW |
| Update rate seen | Rows every 15 minutes (30 near the end of the day); at 21:31 the 21:00 row was filled, later rows carry only capacity: 15 to 30 minutes behind |
| Values at 21:00 | Capacity 925, wind 20, distributed 14, demand 643, conventional 610 MW |
| In Greater Nicosia | One island grid (the Republic's side); not per city |
| Licence | Not stated |
| Key | None |
| Fragile | A web page, not an API: the table id, the Greek headers and the WordPress theme can change. I found no Cypriot data on the ENTSO-E transparency platform, so there is no standard fallback (not checked in depth) |

**Plugs in.** A small `chatbot/energy.py` reading the page at most every 15 minutes, recorded in
the memory as a city-wide series (like the city speed), with `sources.py` entry and freshness
probe. Tool `get_grid` (demand, renewables, carbon intensity estimated from the conventional
share, with the method said). The lighting scenario and the lamps' energy readouts take the
hour's carbon. A Layers row is not needed: it is a readout in an Energy topic card and a chart.

**Effort** S. **Risks**: scraping a page; carbon per kWh is an estimate (conventional share
times a per-plant average), and must say so. Ask the TSO for a feed once it proves its worth.

### D4. Pharmacies on duty tonight

**What a person could do.** "Which pharmacy is open now?" at 23:00, answered with the six on
duty in Nicosia district, on the map, with the walk or drive from where you are, the landmark
directions the Ministry gives ("opposite the Bank of Cyprus") and a phone number.

**Test.**

| | |
| --- | --- |
| Page | `https://www.data.gov.cy/el/resource/leykosia` |
| File | `https://www.data.gov.cy/el/resource/4226/download/file`, CSV in UTF-8 with a BOM, 279 KB |
| Contents | 1,278 rows, one per pharmacy and night, from 1 October 2026 to 1 January 2027: date, weekday, registration number, pharmacist, address, directions, municipality, pharmacy and home phones. Six a night |
| Update rate | A new file each quarter (this one is stamped 3 October 2026) |
| In Greater Nicosia | All of tonight's six are in the city or its suburbs (Nicosia, Lakatamia, Egkomi, Geri) |
| Licence | data.gov.cy's open licence (most datasets are CC BY 4.0; confirm on the dataset page) |
| Key | None |
| Fragile | Addresses only, no coordinates; free-text directions; the next quarter's file appears under a new resource id, so the dataset page must be read to find it. The home phone column is personal data: never show or store it |

**Plugs in.** Kind `pharmacy_on_duty` or, better, a `duty` attribute on the existing pharmacy
places from OSM (105 in the index) matched by address, with geocoding as the fallback and the
precision said, as `locate_web_places` does. A "Tonight" topic (see D5) and the tool
`find_places` with `open_now`. Search (Ctrl K): "pharmacy". Rows carry an id, so they link.

**Effort** S. **Risks** low: geocoding quality; quarterly file discovery.

### D5. Events: what is on

**What a person could do.** "What's on tonight?" and "Why is Ledras so busy?" answered with
events, placed at their venues and set against the camera footfall.

**Test.**

| | |
| --- | --- |
| URL | `https://www.visitcyprus.com/wp-json/tribe/events/v1/events?start_date=2026-11-27` (JSON from The Events Calendar), and `https://www.visitcyprus.com/events/?ical=1` |
| Seen | 17 upcoming events island-wide in the JSON; 20 in the iCal file, 4 of them in Nicosia. Venues are objects with names and pages |
| Licence | The Deputy Ministry of Tourism's site terms; not an open licence |
| Let's Culture | The API named in the first research no longer resolves (`www.letsculture.com.cy`: host not found): **unverified** |
| Nicosia municipality | Its old events path answers 410 Gone: **unverified** |

**Verdict.** Too thin to carry a feature alone. Use it inside the Tonight topic with the web
search the twin already has (`locate_web_places` keeps what it finds, with the page), and keep
a calendar of events in the memory: the simulation research needs exactly that calendar for its
closure studies (P8).

**Effort** S inside D4. **Risks**: thin; terms.

### D6. Earthquakes

**What a person could do.** "Was that an earthquake?" answered within minutes: where, how
strong, how deep, how far from you, and how many people live within 20 km. Rare, but the most
surprising thing a twin can show on an ordinary evening.

**Test.**

| | |
| --- | --- |
| URL | `https://www.seismicportal.eu/fdsnws/event/1/query?format=json&minlat=34.4&maxlat=35.8&minlon=32&maxlon=34.7` (FDSN, also `format=text`) |
| Seen | 29 events in the last 30 days around Cyprus, 22 of them located by the Cyprus Geological Survey Department (`GSD`); largest M3.2 on 1 November; 12 within 50 km of Nicosia; the latest updated an hour before |
| Update rate | Minutes after an event (EMSC also has a websocket stream) |
| Licence | EMSC data free to use with credit to EMSC and the contributing agency |
| Key | None |
| GSD's own pages | `www.moa.gov.cy/moa/gsd/...` answered 500: use EMSC, which carries GSD's solutions |

**Plugs in.** Kind `earthquake` (`eq-20261126_0000450`), memory and clock (a mark on the track),
and a watch detector that opens an incident for M3 or more within 100 km or any felt event,
with the population within reach (D10). `explain_incident` can already say what else happened
then.

**Effort** S. **Risks** low.

### D7. Official weather warnings

| | |
| --- | --- |
| URL | `https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-cyprus` |
| Format | Atom with CAP 1.2 entries (event, area, severity, onset, expiry) |
| Seen | Valid feed, updated minutes before, no warning in force |
| Licence | Meteoalarm's terms: reuse with credit to the issuing service (the Department of Meteorology) and EUMETNET |
| Key | None |

**Plugs in.** Incidents in the watch ("Yellow warning: high temperatures, Lefkosia, 12:00 to
17:00"), a mark on the clock, a line on the Outside card. **Effort** S with D6.

### D8. Fire danger and burnt areas

| | |
| --- | --- |
| URL | `https://maps.effis.emergency.copernicus.eu/effis?service=WMS&request=GetCapabilities&version=1.3.0` |
| Layers | `mf010.fwi` (fire weather index forecast) and its components, `mf010.anomaly`, `modis.ba.*` and `effis.nrt.ba.*` (burnt areas today, this week, month, season), `fuel_map` |
| Licence | Copernicus Emergency Management Service, free with credit |
| Key | None |

**Plugs in.** A Layers row under Environment in summer ("Fire danger today: very high"), and a
watch detector when the index for the Nicosia district passes a level. Burnt areas near the city
(the 2021 Arakapas fire, the Troodos fires) on the clock. **Effort** S with D6.

### D9. Land surface temperature from Landsat

**What a person could do.** "Where is the ground hottest on a summer morning?" A map of last
summer's surface temperature at 30 m (resampled from 100 m), the hot car parks and the cool
parks, set against population (D10), shade (the environment twin) and the trees the 3D work
brings. The question the simulation research ranks sixth (shade and trees) needs a measured "where
does it matter" layer; this is it.

**Test.**

| | |
| --- | --- |
| URL | `POST https://planetarycomputer.microsoft.com/api/stac/v1/search` with collection `landsat-c2-l2`, the city's box, a date range and `eo:cloud_cover < 10` |
| Seen | 25 Landsat 8 and 9 scenes from June to September 2026 with under 10% cloud; the latest scene of the city 3 November 2026 (paths 176/035 and 176/036) |
| Band | `lwir11` (surface temperature, Kelvin after scaling) |
| Licence | USGS Landsat: public domain, credit asked |
| Key | None; Planetary Computer signs asset URLs with an anonymous token |
| Fragile | Overpass about 10:30 local, so this is morning ground, not afternoon air or the night heat island; one composite a year is enough |

**Plugs in.** An offline step (like `lidar.py`) that composites a summer's clear scenes into one
small grid kept in `data/`, a Layers row "Summer ground heat", a `surface_heat` reading in the
`spot` kind, and a heat line in `environment_surface`. **Effort** M (with D10 for exposure).
**Risks**: people read surface temperature as air temperature; say which it is everywhere.

### D10. Where people live

**What a person could do.** "How many people live within 500 m of this broken lamp, this bus
stop, this hot street?" Population turns every incident, closure, scenario and heat map into
"who is affected".

**Test.**

| | |
| --- | --- |
| URL | `https://data.humdata.org/dataset/kontur-population-cyprus`, file `https://geodata-eu-central-1-kontur-public.s3.amazonaws.com/kontur_datasets/kontur_population_CY_20220630.gpkg.gz` |
| Format | GeoPackage of H3 hexagons (resolution 8, about 0.7 km²) with a population each |
| Date | 30 June 2022 |
| Licence | CC BY 4.0 (Kontur); the HDX record warns it is made for visualisation |
| Key | None |
| Alternatives | GHSL population grids (JRC, CC BY 4.0, 100 m) and the Eurostat census 2021 1 km grid: both **unverified** here. CYSTAT's own census tables by quarter are **unverified** |
| North of the line | Kontur covers it, modelled |

**Plugs in.** Loaded once into `data/` (a few MB), a Layers row "People" as 3D hexagons, a
`population` tool, and a population line on incidents, scenarios and the reach of a walk.
**Effort** S alone, M with the accessibility analysis (C3). **Risks**: modelled, 2022; say so.

### D11. Aircraft over the city, and the space station

**Test.**

| | |
| --- | --- |
| URL | `https://api.adsb.lol/v2/lat/35.15/lon/33.36/dist/40` (JSON, like ADSBExchange's v2) |
| Seen | 3 aircraft within 40 nautical miles at 21:37 on a Friday: two on the ground at Ercan, one climbing out through 10,700 ft. Position, altitude, speed, heading, callsign, type |
| Licence | adsb.lol's data is ODbL; keyless |
| OpenSky | Anonymous `states/all` over the city returned none; anonymous use is limited to a daily credit budget |
| Space station | `https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE` (keyless); passes computed locally |
| Fragile | Community receivers: low-level coverage over Cyprus is thin; a sparse sky on most evenings |

**Plugs in.** A live layer like the buses (`meta.live`), drawn as small 3D glyphs at altitude,
kept 24 hours in the memory for the clock. The space station's next visible pass as a mark on
the clock and a line on the Outside card. **Effort** S. **Risks**: sparse; never imply it is
complete.

### D12. Overture themes not yet used

Counted in release `2026-11-18.0` inside Greater Nicosia with DuckDB on the public bucket:

| Theme | Rows | Use |
| --- | --- | --- |
| `addresses/address` | **0** | None: Overture has no addresses for Cyprus |
| `base/infrastructure` | 14,314 | Mostly from OSM: 2,090 parking, 1,933 crossings, 1,177 bus stops, 435 litter bins, 343 signals, 298 bridges, 263 benches, 261 power towers, 235 recycling points |
| `base/land_use` | 3,203 | Parks, schools, residential zones: green space per person with D10 |
| `transportation/segment` | 43,094 | The street graph, already reached through Valhalla |

**Plugs in.** Recycling points, litter bins and benches into the 15-minute analysis (C3) and the
waste scenario's map; land use into heat (D9) and accessibility. **Effort** S as inputs to C3.

### D13. The access point's remaining datasets

`https://traffic4cyprus.org.cy/api/3/action/package_list` lists 42 datasets. Not used and not
queued:

| Dataset | Test | Verdict |
| --- | --- | --- |
| `publictransportrealtime_siri` | `http://20.19.98.194:8313/SiriWS.asmx` on the same host as the GTFS-RT feed | Redundant with GTFS-RT; skip |
| `trafficsensors` (`.../swarco3/api/Data/MeasuredDataPublication`) | Answered at 21:33 with 92 site references and **no measured values** | Watch it: if it ever carries flows, it is the only per-road volume source |
| `wim` (`https://fixcyprus.cy/gnosis/open/api/nap/datasets/its_sensors/WIM/`) | A site table of 3 weigh-in-motion sites, one near Nicosia (35.103) | Sites only; skip |
| `road_network_gradients` (`.../road_network_gradients_hundred_m/`) | DATEX II XML, gradient per 100 m | Useful for wheelchair and cycling routes (C3); small |
| `road_network_dls`, `road_network`, `road_network_intervals`, `road_network_openings` | Not opened | **Unverified**; the DLS network may carry official street names in Greek and English for search |
| `tentec`, `tnits`, `bridges`, `tunnels`, `overhead_traffic_signs`, `motorway_exits`, `weigh_stations` | Not opened beyond metadata | Low value for a city twin |
| `lez`, `lez_entrance_points` | DATEX II regulation publication: the 20 and 30 zones (Aglantzia, Kaimakli, Pallouriotissa) | Already queued as the 20 and 30 zones |

Note: the access point's newer datasets are served from FixCyprus's "gnosis" open API
(`https://fixcyprus.cy/gnosis/open/api/nap/datasets/...`), the same platform as the FixCyprus
app; it serves road data, not citizen reports.

### D14. Satellite NO2 (Sentinel-5P)

Planetary Computer's `sentinel-5p-l2-netcdf` has daily NO2 passes (latest
`S5P_L2_NO2____20261126T111052`). Its pixels are about 5.5 by 3.5 km, so the whole city is
under a dozen pixels. **Verdict**: a regional picture of a pollution episode at most; not a
street layer. Not ranked.

### D15. Ground motion (EGMS)

The European Ground Motion Service explorer (`https://egms.land.copernicus.eu/`) answers;
downloads need an EU Login account; whether its Cyprus tiles cover Nicosia's buildings is
**unverified**. Joined to the dangerous-buildings registers and the 2014 to 2019 height changes,
millimetres a year of settlement per building would be a striking layer. Worth a look by
someone with an account; not ranked until then.

---

## Part 2: Capabilities the best twins have

What the cities let people do, from their own pages and project reports (not their brochures):

| City or project | What a person can actually do | Does Nicosia's twin have it? |
| --- | --- | --- |
| Helsinki 3D+ and the Energy and Climate Atlas | Solar energy for every roof and wall, free in a browser; noise and flood studies on the open city model | Shade and sun hours yes; per-roof solar no |
| Zurich | A spatio-temporal twin for planning; heat islands, cooling areas and ventilation corridors on the canton's climate maps (updated 2025) | Heat no; ventilation no |
| Cooling Singapore (DUCT) | Planners test cooling measures (parks, vegetation, materials) as what-if heat scenarios in a browser | Shade what-if planned (simulation research P6); heat no |
| Rotterdam | Live 3D twin for emergency services; residents give input on a square's redesign in augmented reality | Emergency view no; participation no |
| Wellington | Climate disruption (floods, landslides, sea level) on a public touchscreen; co-design of adaptation with residents | A public story mode no |
| Barcelona (BSC) and HARMONIE | Air quality at 20 m from a city model, and exposure per person, to judge superblocks | Air surfaces yes, coarse; exposure no |
| Munich, Hamburg, Leipzig (Connected Urban Twins) | DIPAS participation (comment on a plan on a map, no personal data); a 3D project planner; open-source components | Participation no; open components partly |
| Vienna (research) | Heat vulnerability: heat days set against the share of people over 65 | People no |

What the literature adds: the 2025 reviews find citizen involvement lagging behind academia and
government, few evaluations of whether twins change decisions, and a gap in real-time prediction.
The simulation research already drew the useful conclusion: a twin earns trust by scoring its
own predictions against what happened.

The capabilities that follow are those where the gap is real for Nicosia and the data exists.

### C1. Forecasts with a track record (roadmap 7)

**What a person could do.** Drag the clock past now and see parking by street, bin fill, air,
road speeds and camera footfall for the next 6 to 48 hours, drawn with their uncertainty. The
inspector says "likely full in about 2 days" for a bin. Every forecast states its method and its
error on the past four weeks.

**Data.** The memory (weeks of parking, bins, air, lamps, road speeds and counts), Open-Meteo's
forecast for weather and CAMS air, and D2 for the air's ground truth. The simulation research
found a plain typical-week model hard to beat for parking (A2); start with seasonal naive and
typical-week baselines, add statsforecast models only where they beat them in the backtest.

**Plugs in.** `?at=` in the future on the same routes, `chatbot/forecasts/` with one backtest
harness (simulation research 4.7), the `forecast` tool, a prediction ledger (simulation research
P3), and the watch: "bin YW1394 will pass 90% tomorrow at about 14:00".

**Effort** M to L. **Risks**: forecasts that look surer than they are. Show bands and the
backtest error everywhere; say "not enough history" rather than guess.

### C2. Heat risk: ground, shade and people together

**What a person could do.** "Where in Nicosia is heat worst for people, and where would 50 trees
help most?" One map that combines summer ground heat (D9), shade hours (built), population and
older residents where known (D10), and the walking routes people use (camera counts, bus stops).

**Plugs in.** A Heat topic card in summer, a heat score per hexagon in `environment_surface`,
and the simulation research's P6 (shade and trees) gets its "where it matters" layer. Pairs
with D1's measured air temperature and D7's heat warnings.

**Effort** M after D9 and D10. **Risks**: the index is a composite; show its parts.

### C3. People and the 15-minute city (roadmap 12, first part)

**What a person could do.** Population as 3D hexagons; for each hexagon and address, how many
everyday services (pharmacy, grocery, school, bus stop, park, recycling point) are within a
15-minute walk; who gains when a service is placed (simulation research P5). Wheelchair and
step-free variants with the access point's gradients (D13).

**Data.** D10, the OSM places index, Overture infrastructure (D12), bus stops (GTFS), and the
local street graph the simulation research measured at 48 ms a city-wide question.

**Effort** M. **Risks** low; OSM completeness varies by suburb, say so per category.

### C4. Nicosia tonight: an ambient mode (roadmap 11, first part)

**What a person could do.** Press one button (or leave a lobby screen idle) and the twin tours
what is alive now: buses gliding down Makariou, lamps lighting street by street at dusk, the
slowest road, the air at the stations, an aircraft leaving Ercan, the moon over the walls, the
grid's demand at its evening peak, an incident if there is one. Captions in plain words, large
type, no chat. Keyboard and clicker for next and back.

**Plugs in.** A story runner over existing commands (`map_fly_to`, layers, `set_city_time`),
fed by live state: each step is skipped when it has nothing to show ("no buses after 23:00").
Honours reduced motion. Needs no new data, and it is the feature that makes the twin worth
leaving on.

**Effort** M. **Risks** low.

### C5. An open twin (roadmap 13, second part)

**What a person could do.** Another team's agent asks the twin "how full is parking near the
Cyprus Museum?" through an MCP server that exposes the chat's own read tools; a city system
reads devices, observations and history through a read-only OGC SensorThings API. Every answer
carries its sources and ages (built: `TOOL_SOURCES`).

**Effort** M. **Risks**: load on the city API (serve from the memory, never pass through); a
licence line per source (some sources here, like the Met data, are not openly licensed for
redistribution).

### C6. Citizen reports

The roadmap's feature 14. Research finding: FixCyprus (Public Works Department and KIOS, UCY)
collects road reports but publishes **no open API for them** (its open API serves the access
point's road data; the app pages and a 2023 press piece mention a statistics page only).
Nicosia's municipality has no open reporting feed found. So the twin would run its own reports
(photo, place, the nearest device, confirmed first) and, by agreement, hand road issues to
FixCyprus. **Effort** M. Not in the top ten: it needs a decision on who receives the reports.

### C7. Honest data: sensor health and calibration

Built pieces: ages on readings, the sources panel, coverage in the memory. Missing: each device's
uptime and its bias against a reference (D1, D2), a "how sure" line on every readout, and a
data-quality board ("3 of 7 air sensors are within 20% of the reference station this week").
Folds into D1 and D2 and into roadmap 13. **Effort** S on top of D2.

### C8. An emergency view

Hazards (D6 to D8), people (D10), the hospitals and pharmacies on duty (D4), road speeds and
closures (built and queued), the routes from fire and ambulance stations (Valhalla). Rotterdam's
twin serves emergency services; the University of Cyprus already used iNicosia for an evacuation
study. **Effort** M after D6 and D10. Not ranked: it needs an owner in the city to be useful.

### C9. Solar per roof

In the roadmap (12) and the 3D research; it waits for the roof shapes from the 2019 surface
model (rung 4's next slice). The Helsinki atlas shows the bar: every roof and wall, open to
residents. Not ranked here because its blocker is the 3D pipeline, not research.

---

## Part 3: What makes it feel alive on an ordinary evening

The twin already moves: buses glide, lamps glow and dim, roads redden in the evening rush, wind
drifts, rain sweeps by. What an evening visitor still misses, in the order I would add it:

1. **A heartbeat of real measurements.** Ten-minute thermometers and rain (D1), the grid's demand
   falling after its 20:00 peak (D3), the reference air (D2). Numbers that change while you watch,
   each with its age.
2. **Something practical to ask at night.** Pharmacies on duty, what is open now, what is on
   (D4, D5).
3. **Surprise, rarely but for real.** An earthquake, a weather warning, a fire danger day (D6 to
   D8), raised as incidents with the people near them. The watch's toasts already exist.
4. **The sky.** Aircraft, the space station passing over at a known minute, the moon's phase and
   its rise over the walls (computed, like the sun) (D11).
5. **A reason to leave it on.** The ambient tour (C4).
6. **The near future.** "Parking near Eleftheria Square will be 70% full at 22:00" turns a live
   map into a twin (C1).
7. **Small motion that costs nothing.** Footfall from the cameras as a soft pulse on Ledras and
   Onasagorou; the dusk wave of lamps lighting street by street replayed at 20 times speed at
   sunset; a clock mark for "now" that ticks. These need no data, only the page.

---

## Rejected and why

| Candidate | What I found | Why not now |
| --- | --- | --- |
| Water in the dams (Water Development Department) | `www.moa.gov.cy/moa/wdd/...` answered 403 with a gov.cy block page; the new gov.cy "Reservoir Storage" page was not probed; figures reach the press as percentages | **Unverified**; and dam storage is national, slow and outside the city |
| Fuel prices | The Consumer Protection Service's observatory and a "Fuel Finder" use case exist; no feed URL or licence found | **Unverified**; low value for a city twin |
| Power cuts (EAC) | The faults page is a Google map behind reCAPTCHA; no open feed | Blocked; ask EAC. Meanwhile the watch infers street outages from lamps going silent together |
| Crossing-point queues (Agios Dometios, Ledra) | No live feed; press reports of hour-long queues | Nothing to read. Once Waze jams land (queued), a derived "queue at Agios Dometios" readout is worth an S |
| FixCyprus reports | No public report API found | See C6 |
| Let's Culture events | Host no longer resolves | Gone; Visit Cyprus is the fallback (D5) |
| Heritage from Wikidata | Zero items with a heritage designation within 8 km of the centre | Too thin. The Department of Antiquities' lists of ancient and listed monuments were **not probed**: **unverified**, worth a separate look |
| Civil Defence shelters, CYSTAT business counts, hospitals' emergency waits | Not probed | **Unverified** |
| Night lights (VIIRS Black Marble) | Needs a NASA Earthdata account | Monthly, 500 m: little a person can do with it |
| Urban Atlas, Street Tree Layer, Tree Cover Density | Need a Copernicus Land login; 3D research found better tree data (Meta and WRI canopy, CC BY 4.0) | Covered by the 3D plan |
| OpenAQ | v3 needs a key (401) | EEA files give the same stations keyless |
| Satellite NO2 | 5.5 by 3.5 km pixels | D14 |
| SIRI, weigh-in-motion, the access point's detector measurements | D13 | Redundant or empty |
| A traffic simulation, street-level air or noise what-if | | The simulation research's verdict stands |

---

## Briefs for the top six

Each is written so a feature session can start from it alone. All follow `CLAUDE.md`: a source
entry and freshness probe in `chatbot/sources.py`, the tool in `TOOL_SOURCES` and `TOOL_LABELS`,
fixtures for every source, answers as visuals, the clock and the memory respected, the calm
layout (a topic card or a Layers row, not a new strip).

### Brief 1. Measured weather and reference air

**Goal.** Put real measurements beside the twin's modelled weather and air, and say how far each
city sensor and the model are from them.

**Sources.**
- Department of Meteorology stations: `https://www.dom.org.cy/AWS/OpenData/CyDoM.xml` (10
  minutes; Nicosia stations `LEFKOSIA`, `ATHALASSA`, `KEPNIC`, `VISITOR`). Confirm reuse terms
  with the Department; credit "Cyprus Department of Meteorology".
- EEA up-to-date air quality: `POST https://eeadmz1-downloads-api-appservice.azurewebsites.net/ParquetFile/urls`
  (`countries: ["CY"]`, `dataset: 1`), hourly Parquet per station and pollutant. First task:
  fetch the EEA station metadata and confirm which codes are Nicosia traffic and residential.
- For later, by letter: the Department of Labour Inspection's real-time network
  (`https://www.airquality.dli.mlsi.gov.cy/`).

**Build.**
- `chatbot/environment/stations.py`: read the Met XML at most every 10 minutes and the EEA
  files at most hourly, cache on disk, record into the memory as readings of two new kinds,
  `weather_station` (`met-LEFKOSIA`) and `air_station` (`aq-CY0004A`), with 24-hour series.
- Surfaces: temperature corrected by the Met stations; air corrected by reference stations first;
  the uncertainty narrows only near a reference station.
- A per-sensor comparison: each smartsense sensor's bias and spread against its nearest
  reference station over the last 7 days, in its inspector.
- `get_weather` returns measured and modelled side by side; `environment_surface` names the
  stations used. Watch detectors: model and station disagree by more than 3 °C; PM10 over 50
  µg/m³ daily mean at a reference station.
- Layers sheet: a "Stations" row under Environment; Outside card: "21.4 °C measured at
  Lefkosia, 10 minutes ago".

**Done looks like.** The Outside card and `get_weather` show a measured temperature with its age;
the stations open in the inspector with their day; the air surface's legend says which stations
it rests on; a city sensor's card says "reads 18% above the reference station this week"; the
clock replays station readings from the memory; fixtures and offline tests for both parsers.

### Brief 2. Forecasts with a track record

**Goal.** The city clock runs past now, and every forecast says how wrong it was on past weeks.

**Sources.** The twin's memory (parking, bins, air, lamps, road speeds, camera counts),
Open-Meteo forecast and CAMS air forecast (in use), and Brief 1's stations once built.

**Build.**
- `chatbot/forecasts/`: one backtest harness (rolling origin over the last 4 weeks, MAE and
  interval coverage), baselines first (seasonal naive, typical week from `query_history`'s
  typical day), a model only where it beats the baseline.
- Forecasts for: parking occupancy per street block, bin fill and time to 90%, road speed per
  segment, camera footfall per line, air at stations (CAMS corrected by the recent miss).
- `?at=` in the future on `/api/overview`, `/api/roads`, `/api/entities/{id}`; the clock's
  track extends 48 hours ahead with a clear "forecast" band.
- Tool `forecast` (kind, entity or area, horizon) returning the method and backtest error; a
  prediction ledger table that scores yesterday's forecasts against what happened.
- Watch: "bin YW1394 will pass 90% tomorrow about 14:00".

**Done looks like.** Dragging past now shows forecast states drawn with their bands; a bin's card
says when it will be full and how accurate that has been; `forecast` answers "Will I find
parking near Eleftheria Square at 19:00?" with a number, a band and the backtest error; where
history is short the twin says so instead of forecasting; tests on fixtures of stored history.

### Brief 3. The island's power, live

**Goal.** Show how much power Cyprus is using, how much comes from wind and sun, and use the
grid's carbon at each hour for the lamps' CO2.

**Sources.** Cyprus TSO: `https://tsoc.org.cy/electrical-system/total-daily-system-generation-on-the-transmission-system/`
(table `#production_graph_data`, 15-minute rows: capacity, wind, estimated distributed
generation, demand, conventional generation, MW); `.../total-daily-wind-and-solar-farm-generation/`
and the archive page for back-filling. Existing: `grid_co2` in `sources.py` (EAC and Ember
yearly figures).

**Build.**
- `chatbot/energy.py`: read the page at most every 15 minutes, parse the table defensively
  (headers by meaning, not position), keep the last good copy, record a city-wide series in the
  memory; a one-off back-fill from the archive page.
- Carbon intensity per hour, estimated from the conventional share and the published yearly
  factor, stated as an estimate with its method.
- Tool `get_grid` (now, a moment, or a range; returns a `data_ref` for a chart of demand by
  source); the lamps' energy answers and the lighting scenario take the hour's factor.
- An Energy topic card: demand now, renewable share, today's curve; a clock mark at the day's
  peak.

**Done looks like.** "How much power is Cyprus using now?" returns a chart of today's demand by
source with the latest value and its age; the lamps' CO2 for a night uses that night's hourly
factors and says so; the clock replays the grid from the memory; a fixture of the page and a
parser test; the source panel shows the TSO with its freshness.

### Brief 4. Tonight in Nicosia

**Goal.** Answer the evening's practical questions: which pharmacy is on duty, what is on, what
is open now.

**Sources.**
- Pharmacies on duty, Nicosia district: dataset page `https://www.data.gov.cy/el/resource/leykosia`,
  file `https://www.data.gov.cy/el/resource/4226/download/file` (CSV, quarterly, six a night).
  Discover next quarter's file from the dataset page. Never store or show the home phone column.
- Events: `https://www.visitcyprus.com/wp-json/tribe/events/v1/events?start_date=<today>` and the
  existing web search (`locate_web_places`).
- Opening hours: the OSM places index (in use).

**Build.**
- `chatbot/tonight.py`: load the duty roster, match each pharmacy to the OSM pharmacy places by
  address, geocode the rest with the precision said; keep the roster in the memory so the clock
  can show who was on duty on a past night.
- Events: placed at their venues, kept as a calendar in the memory (the closure studies need it).
- Tool `whats_on_tonight` (or `find_places` with `open_now` and `on_duty`) returning a
  `layer_ref`; a "Tonight" topic chip after dusk with three sections: on duty, on now, open now.
- Ctrl K: "pharmacy on duty".

**Done looks like.** At 23:00 "Which pharmacy is open near me?" shows the duty pharmacies on the
map with walking times and directions text; the Tonight card lists them; the next quarter's file
is found without code changes; fixtures for the CSV and the events JSON.

### Brief 5. Heat from space, and who it reaches

**Goal.** Show where Nicosia's ground gets hottest in summer and how many people live there,
as the base for the heat and tree questions.

**Sources.**
- Landsat Collection 2 Level 2 surface temperature through Planetary Computer:
  `POST https://planetarycomputer.microsoft.com/api/stac/v1/search` (`landsat-c2-l2`, the
  city's box, June to September, `eo:cloud_cover < 10`; 25 scenes in 2026), band `lwir11`,
  assets signed with the anonymous token at `https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2`.
  Public domain, credit USGS.
- Population: Kontur, `https://data.humdata.org/dataset/kontur-population-cyprus` (CC BY 4.0,
  H3 hexagons, 2022).

**Build.**
- An offline step (`uv run python -m chatbot.heat build`, like the LiDAR one) that composites
  the summer's clear scenes into a median surface temperature grid for Greater Nicosia and writes
  a small file under `data/` (the scenes are not kept, nor committed).
- Population hexagons loaded once into `data/`.
- Layers rows "Summer ground heat" and "People" (3D hexagons); the `spot` kind gains the summer
  ground temperature and the hexagon's population; a heat score per hexagon (ground heat times
  people, with shade hours from the environment twin), shown with its parts.
- Tool `heat_exposure` (area or the whole city) with a `layer_ref` and a `data_ref`.

**Done looks like.** "Where is heat worst for people in summer?" draws the hexagons ranked with a
chart of the top ten and their parts; every label says "ground surface temperature, mornings
of summer 2026", never air temperature; the build step is documented and repeatable; tests on a
small fixture grid.

### Brief 6. Hazards in the watch

**Goal.** The watch raises earthquakes, official weather warnings and fire danger as incidents,
with the people near them.

**Sources.**
- Earthquakes: EMSC FDSN `https://www.seismicportal.eu/fdsnws/event/1/query?format=json&minlat=34.4&maxlat=35.8&minlon=32&maxlon=34.7`
  (GSD's solutions included); credit EMSC and the Cyprus Geological Survey Department.
- Warnings: Meteoalarm `https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-cyprus` (CAP).
- Fire: EFFIS WMS `https://maps.effis.emergency.copernicus.eu/effis` (`mf010.fwi`,
  `effis.nrt.ba.poly`).
- Population for "who is near": Kontur (as Brief 5), or the population count can come later.

**Build.**
- `chatbot/watch/hazards.py`: poll EMSC every 5 minutes and Meteoalarm every 15, read the fire
  index once a day in the fire season; record events in the memory.
- Kinds `earthquake` and `warning`; detectors open incidents for M3 or more within 100 km or any
  felt report, a warning for the Nicosia area, fire danger at "very high" or above.
- Marks on the clock; a line on the Outside card for a warning in force; a Layers row
  "Hazards" (earthquakes of the last 30 days as rings by magnitude, burnt areas this season).
- Tool `get_hazards` (recent earthquakes, warnings, fire danger) with a `layer_ref`.

**Done looks like.** A test earthquake fixture opens an incident with its distance, depth,
magnitude and population within 20 km, toasts on an open page, and appears on the clock; a
warning fixture shows on the Outside card for its hours; "Any earthquakes near Nicosia this
month?" maps the 30 days' events with a chart by magnitude; offline tests for all three parsers.

---

## Sources

Data, as probed:

- Department of Meteorology stations: <https://www.dom.org.cy/AWS/OpenData/CyDoM.xml>
- EEA air quality download service: <https://eeadmz1-downloads-api-appservice.azurewebsites.net/>; EEA air quality: <https://www.eea.europa.eu/en/topics/in-depth/air-pollution>
- Department of Labour Inspection air quality: <https://www.airquality.dli.mlsi.gov.cy/>
- Cyprus TSO generation: <https://tsoc.org.cy/electrical-system/total-daily-system-generation-on-the-transmission-system/>
- Pharmacies on duty, Nicosia: <https://www.data.gov.cy/el/resource/leykosia>; private pharmacies: <https://www.data.gov.cy/el/dataset/katalogos-idiotikon-farmakeion>
- Visit Cyprus events: <https://www.visitcyprus.com/events/?ical=1>
- EMSC seismic portal: <https://www.seismicportal.eu/fdsn-wsevent.html>
- Meteoalarm Cyprus: <https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-cyprus>
- EFFIS: <https://maps.effis.emergency.copernicus.eu/effis?service=WMS&request=GetCapabilities&version=1.3.0>
- Planetary Computer STAC: <https://planetarycomputer.microsoft.com/api/stac/v1>
- Kontur population, Cyprus: <https://data.humdata.org/dataset/kontur-population-cyprus>
- adsb.lol API: <https://api.adsb.lol/>; OpenSky: <https://opensky-network.org/>
- CelesTrak: <https://celestrak.org/NORAD/elements/>
- Overture Maps: <https://docs.overturemaps.org/>
- Cyprus National Access Point catalogue: <https://traffic4cyprus.org.cy/api/3/action/package_list>
- EGMS: <https://egms.land.copernicus.eu/>; access: <https://land.copernicus.eu/en/faq/products/european-ground-motion-service/how-do-i-access-egms-products>
- FixCyprus: <https://www.kios.ucy.ac.cy/fixcyprus-a-platform-and-mobile-app-to-improve-the-cyprus-road-network-infrastructure-and-road-safety/>, <https://cyprus-mail.com/2023/01/30/fix-cyprus-app-gains-momentum>
- EAC faults: <https://www.eac.com.cy/en/distribution/faults/>
- Water Development Department figures in the press: <https://cna.org.cy/en/article/10831758/total-water-reservoir-storage-at-39-3-in-early-september>
- Fuel Finder use case: <https://data.europa.eu/publications/use-cases/fuel-finder>
- Crossing queues: <https://cyprus-mail.com/2026/02/11/hour-long-queues-persist-at-ayios-dhometios-despite-expansion-works>

Other twins and the literature:

- Helsinki 3D and the Energy and Climate Atlas: <https://www.hel.fi/en/decision-making/information-on-helsinki/maps-and-geospatial-data/helsinki-3d>
- Kalasatama digital twins (wind and sun studies): <https://www.hel.fi/static/liitteet-2019/Kaupunginkanslia/Helsinki3D_Kalasatama_Digital_Twins.pdf>
- Canton of Zurich climate maps (2025): <https://www.zh.ch/de/news-uebersicht/medienmitteilungen/2025/06/aktualisierte-klimakarten-unterstuetzen-klimaangepasste-siedlungsentwicklung.html>
- Zurich's twin for planning: <https://living-in.eu/sites/default/files/files/the-digital-twin-of-the-city-of-zurich-for-urban-planning.pdf>
- Cooling Singapore DUCT: <https://fass.nus.edu.sg/srn/2024/01/08/cooling-singapore-2-0-digital-urban-climate-twin>
- Rotterdam Open Urban Platform and participation: <https://living-in.eu/stories/rotterdam-explores-citiverse-connect-people-and-public-services>
- Wellington digital twin: <https://wellington.govt.nz/your-council/projects/our-changing-city>, <https://www.smartcitiescouncil.com/news/post/wellington-s-digital-twin>
- Barcelona: <https://www.tomorrow.city/discover-the-alternative-barcelona-europes-first-digital-twin/>; HARMONIE: <https://www.idaea.csic.es/newspost/new-european-initiative-to-advance-digital-twin-technology-that-helps-cities-reduce-pollution-and-protect-public-health/>
- Connected Urban Twins (Munich, Hamburg, Leipzig): <https://www.fes.de/themen/digitalisierung-2/connected-urban-twins-wie-digitale-zwillinge-die-stadtplanung-revolutionieren>, <https://repos.hcu-hamburg.de/bitstream/hcu/1178/2/2025-12-15_report_cut_csl_v1.pdf>
- Vienna heat vulnerability: <https://csh.ac.at/news/rising-heat-rising-risks-interactive-map-highlights-heat-vulnerability/>
- Reviews: citizen participation and twins (2025): <https://eprints.gla.ac.uk/360965>; what twins contributed to planning: <https://research.utwente.nl/en/publications/what-have-urban-digital-twins-contributed-to-urban-planning-and-d-2/>; IoT, AI and twins in smart cities: <https://www.mdpi.com/2624-6511/8/5/175>

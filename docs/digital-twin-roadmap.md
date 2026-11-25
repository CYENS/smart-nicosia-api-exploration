# From a map chatbot to a full digital twin of Nicosia

Status: plan, 26 Nov 2026. Builds on [digital-twin-research.md](digital-twin-research.md)
(the question catalogue, the data findings and the tool list) and on what is built today.

## Where the twin stands

A digital twin is more than a map with live sensors. It is a model of the city that knows every
asset by name, remembers how the city has been, can say how it will be, can try out changes
before they are made, watches itself, and lets people act on what it shows. This table scores
today's chatbot against those capabilities.

| Capability | What it means for Nicosia | Today |
| --- | --- | --- |
| Reality capture and 3D | Buildings, terrain, parcels, streets | DLS 3D buildings and the base maps. No terrain, no parcels or zoning on the map |
| Identity | Every lamp, bay, bin, station, camera, building, stop and street is a named thing you can open | Only through the chat (`get_device`). Nothing on the map can be clicked into |
| Live state | The twin shows the city as it is, without being asked | Sensors on demand, the live strip, lamps glowing at night. No buses, no road speeds, no push |
| Memory | Weeks and years of history, fast | 72 hours replayed from the city API. The collector exists but nothing reads its database |
| Time | One clock for the whole twin: past, now and future | A time-lapse per answer. No shared clock, no "as it was on Friday" |
| Environment | Sun, shade, terrain, heat, air and noise as surfaces | The real sky and 3D light. Air and noise are 7 dots |
| Prediction | Parking, bins, air and crowds in the next hours and days | None |
| Simulation | What-if scenarios with costs and side effects, compared side by side | None |
| Watching | Rules, anomalies and incidents, raised as they happen | Alarms through a tool only |
| Acting | Work orders, collection and maintenance rounds, with confirmation | None |
| Sharing | Links, boards, reports, exports, stories | None |
| Trust and openness | Where every number comes from, how fresh it is, open standard APIs | Ages on readings, the tool trace |
| People | Citizens report problems, in Greek or English, by voice, on a phone | Chat only, English UI |

## The order

Fourteen features, each one pull request on a single stack, built by its own session. At most
two are in progress at once (lane A and lane B). The order puts first what everything else
stands on (identity, memory, one clock), then the capabilities that make it a twin rather than a
dashboard (prediction, simulation, watching, acting), then sharing and openness.

| # | Feature | Lane | Needs | The question it answers |
| --- | --- | --- | --- | --- |
| 1 | Twin shell and inspector | A | | "What is this thing, and what is it doing?" |
| 2 | The twin's memory | B | | "How has it been, over weeks?" |
| 3 | One clock for the city | A | 1, 2 | "Show me the city as it was on Friday at 21:00" |
| 4 | Live mobility | B | 1 | "Where are the buses, and which roads are slow?" |
| 5 | Environment twin | A | 1, 3 | "Where is the shade at 15:00, and where is the air worst?" |
| 6 | Watch: alerts and incidents | B | 1, 2 | "What is going wrong, and why?" |
| 7 | Forecasts | A | 2, 3 | "Will I find parking at 19:00? When will this bin be full?" |
| 8 | Boards and reports | B | 1 | "Give me a board I can keep open, and export it" |
| 9 | Scenario lab | A | 2, 7 | "What if we dimmed every lamp from 01:00?" |
| 10 | Operations | B | 6 | "Plan today's collection round and fix the broken lamps" |
| 11 | Stories and presenter mode | A | 1, 3 | "Walk an audience through Nicosia after dark" |
| 12 | Urban fabric | B | 1 | "Who lives here, what is this plot zoned for, how walkable is it?" |
| 13 | Trust and open interfaces | A | 2 | "Where does this number come from? Can my system read the twin?" |
| 14 | Citizens | B | 1, 10 | "This lamp is broken (photo). Tell the city." |

A lane that frees up takes the next feature in this order whose needs are already in the stack.
The stack tip moves forward as each feature's pull request opens, so a feature started from an
earlier tip rebases onto the new tip before it opens its own pull request.

Every feature follows the house rules in [../CLAUDE.md](../CLAUDE.md): data comes back as a
visual (a map layer, a linked chart, the 3D city), every new data source gets a chat tool and
prompt guidance, the UI is shadcn on the control-room design language, and every number traces
to a tool.

---

## 1. Twin shell and inspector

The foundation the rest stands on: everything on the map becomes a thing you can open.

**For someone using it**

- Click anything on the map (a lamp, a parking bay, a bin, an air station, a camera, a
  dangerous building, any DLS building in the 3D city, a place from an answer) and an
  **inspector** opens: its name and kind, a status badge, live readings with their age, a
  sparkline of the last 24 hours, its attributes, what it relates to (its street, its parcel,
  the nearest devices of other kinds), and actions: ask about it, fly to it, copy a link.
- **Search everything** from Ctrl K: devices by id or street, places, streets, buildings by
  register file number, prepared questions, and commands (turn on the 3D city, Night base map).
- **Links**: the URL keeps the camera and the open thing (`#/e/YL1015`, `#/@35.172,33.361,16z`),
  so a view can be shared and survives a reload.
- In answers, every device or building the assistant names is a **chip** that opens the
  inspector.
- A slim **navigation rail** for the twin's workspaces (Live today; later features add theirs),
  with keyboard shortcuts and visible focus everywhere.

**Platform work** (so later features add files instead of editing shared ones)

- A unified entity model on the server (`chatbot/entities.py`): `{id, kind, name, geometry,
  state, readings, attributes, relations, sources, as_of}` for every kind, with
  `/api/entities/search` and `/api/entities/{id}`.
- `chatbot/tools.py` split into a package with one module per domain and a registry, and the
  server's routes into routers.
- On the page, registries for workspaces (the rail), inspector sections per kind, search
  providers and map click handlers.
- The two existing lint errors fixed and lint made blocking in CI.

**Chat tools**: `get_entity` (any kind, superset of `get_device`), `open_inspector` (UI).

## 2. The twin's memory

History measured in weeks, answered in under a second, without leaning harder on the city API.

**For someone using it**

- "How full is parking on Makariou on Saturday evenings?", "Which bins overflowed most this
  month?", "Energy the lamps used in November": answered from the twin's own store.
- "Compared with usual": every live number can be set against a typical day (median for that
  hour of that weekday) and the inspector shows that band behind the sparkline.
- Time-lapses can cover days and weeks, not just 72 hours.

**Under the hood**

- A recorder inside the chatbot: every snapshot the chatbot already fetches (the live strip
  polls every minute) is written to DuckDB as readings and state changes, at no extra cost to
  the city API. The full collector (`collector/`) can run in the same process when
  `CHATBOT_COLLECT=1`, within its existing budget of about 23,500 requests a day, and
  `collector backfill` fills the past.
- One history reader that answers from DuckDB and falls back to the city API for the last 72
  hours when the store is empty.
- `/api/history/{entity}` for the inspector, `/api/health/data` for freshness per source.

**Chat tools**: `query_history` (entity, kind, street or area; metric; range; bucket;
aggregate; with a typical-day baseline), `run_sql` (read-only DuckDB over documented tables,
row and time limits). Existing replays read from the store when it has the data.

## 3. One clock for the city

One time control for the whole twin. Every live layer, the sky, the strip and every chart follow
it.

**For someone using it**

- A **city clock** bar: drag back to any moment the twin remembers, or press Live. Lamps
  glow as they did, parking bays and bins show their states, traffic columns rise and fall, the
  sun and shadows move, and the strip reads "Friday 21:00" with a clear way back to live.
- Marks along the track: dusk and dawn, alarms, the moments an answer pointed to.
- Play a range at a chosen speed; the existing time-lapses become ranges on this clock.
- **Compare**: now against the same moment last week, as a swipe across the map.

**Chat tools**: `set_city_time` (a moment or a range, play or pause, compare). Replays from the
chat land on the clock instead of a separate player.

## 4. Live mobility

The city in motion: buses, road speeds and the camera counts together.

**For someone using it**

- **Live buses** gliding along their routes (NPT GTFS and the GTFS-RT feed), coloured by
  line. Click a bus for its line, next stops and delay; click a stop for its next departures.
- **Road speeds** from the national Bluetooth travel-time feed: road segments coloured by speed
  against their free-flow speed, so congestion shows at a glance. Recorded by the twin's memory,
  so it replays on the city clock.
- Strip readouts: buses running now, the slowest road.
- "Can I take the bus to the Cyprus Museum?" plans a trip (walk, ride, walk) with times.

**Chat tools**: `get_buses`, `get_departures`, `plan_transit` (direct and one-change trips over
the GTFS timetable), `get_road_speeds`.

## 5. Environment twin

The physical city: ground, sun and shade, air and noise as surfaces, weather you can see.

**For someone using it**

- **Terrain** with hillshade, and the 3D city standing on it.
- **Shadows** cast by the DLS 3D buildings for the clock's moment, and a **sun hours** map
  for a square or street on any date ("where is the shade at 15:00 in July?").
- **Shaded walks**: a route preference that keeps to the shade at the time of the walk.
- **Air and noise surfaces** interpolated from the stations and the Open-Meteo air model,
  with honest uncertainty where stations are few.
- **Weather on the map**: wind as moving particles, rain radar, temperature.

**Chat tools**: `sun_and_shade` (place, date, time or a day), `environment_surface` (metric,
moment), and `prefer: shade` on `plan_route`.

## 6. Watch: alerts and incidents

The twin keeps an eye on the city and says what is wrong before anyone asks.

**For someone using it**

- An **incident inbox**: alarms and detected anomalies grouped into incidents (three lamps on
  one street going silent together is one probable power cut, not three faults), each with
  severity, place, start time, the evidence and a map view.
- **Anomaly detection** over the memory: a parking sensor stuck occupied for days, a lamp lit at
  noon, power out of line with similar lamps, a bin that jumps, a station gone quiet.
- **Watches**: "tell me when bin YW1394 passes 90%", "when air turns poor near me". Raised as
  toasts while the page is open, and as browser notifications if allowed.
- Acknowledge or snooze an incident; the inbox shows who did what.

**Chat tools**: `list_incidents`, `explain_incident` (what else happened nearby at the same
time), `create_watch` (confirmation first).

## 7. Forecasts

The near future, on the same clock as the past.

**For someone using it**

- Drag the city clock past now: parking occupancy by street, bin fill, air quality and
  pedestrian counts for the next hours and days, drawn with their uncertainty.
- The inspector says "likely full in about 2 days" for a bin, "usually 80% occupied by 19:00"
  for a street.
- "When is the best time to visit Ledras?" answered from the forecast, with the method and how
  accurate it was on past weeks.

**Chat tools**: `forecast` (kind, entity or area, horizon), reporting the method and its
backtested error.

## 8. Boards and reports

What people keep open on a wall screen, and what they take away.

**For someone using it**

- **Pin** any chart, visual, number or map view from an answer to a **board**; arrange and
  resize tiles; tiles stay live.
- Ready-made boards: City pulse, Energy, Mobility, Environment.
- **Export**: a board as PDF or PNG, data as CSV, GeoJSON or XLSX; a written brief of the day
  in plain language.

**Chat tools**: `pin_to_board`, `export_data`, `write_brief`.

## 9. Scenario lab

Try a change on the twin before making it on the street.

**For someone using it**

- A **scenario lab** workspace with templates: lamp dimming schedules, switching by sunset and
  sunrise with offsets, bin collection frequency, closing a street to cars (travel times
  around it), paid or EV-only parking.
- Set the parameters with sliders and fields, run it against the twin's memory and forecasts,
  and see the result as numbers (kWh, euros, CO2, overflow risk, minutes added), map layers
  of what changes, and charts.
- **Compare** two scenarios side by side on a split map; save them; share a link.

**Chat tools**: `run_scenario` (type and parameters), `compare_scenarios`. The assistant can set a
scenario up from a conversation and open it in the lab.

## 10. Operations

From seeing a problem to sending someone to fix it.

**For someone using it**

- **Work orders** made from incidents or by hand (always confirmed), with status and an
  operations board.
- **Rounds**: today's bin collection round for bins above a fill level, with truck capacity and
  a depot; a technician round over the open work orders. Animated on the map, with timings, and
  exportable.

**Chat tools**: `plan_collection`, `plan_maintenance_round`, `draft_work_order` (confirmation
first).

## 11. Stories and presenter mode

The twin as something to show people.

**For someone using it**

- **Stories**: a sequence of camera moves, layers, moments on the clock and captions. Some ship
  ready (Nicosia after dark; the walled city's heights; a day on Ledras), and the assistant can
  write new ones from a conversation.
- **Presenter mode**: clean screen, large captions, next and back from the keyboard or a
  clicker, and an idle loop for a screen in a lobby.

**Chat tools**: `play_story` (steps).

## 12. Urban fabric

The city's land and people.

**For someone using it**

- **Parcels** from the DLS cadastre on click (in the inspector, with the buildings on them),
  planning zones where published, listed and historic buildings.
- **People**: the population grid (census 2021 / GHSL), as a 3D hex layer.
- **Walkability**: a 15-minute-city score per hex (how many everyday services are within a
  15-minute walk), and per address.
- **Rooftop solar** potential from DLS roof areas and types and the sun.

**Chat tools**: `get_parcel`, `accessibility_score`, `solar_potential`, `population`.

## 13. Trust and open interfaces

Why the twin can be believed, and how other systems can use it.

**For someone using it**

- A **data catalogue**: every source with its provider, licence, update rate, freshness right
  now, coverage and known caveats; sensor uptime from the memory.
- **Provenance** on every answer: which sources and readings it used, how old they were.
- An **open API** in a standard shape (OGC SensorThings, read-only) and an **MCP server** so
  other agents can ask the twin questions with the same tools the chat uses.
- An eval set of the catalogue's questions, run in CI, so new tools don't break old answers.

**Chat tools**: `describe_sources`. Tool search once the tool list passes about 30 tools.

## 14. Citizens

The twin for the people who live in it.

**For someone using it**

- **Report a problem** with a photo and a location: the assistant recognises it (a dark lamp, an
  overflowing bin), links it to the nearest device, and files a report (confirmed first) that
  lands in Operations.
- **Greek** throughout the interface, alongside English.
- **Voice** questions, and a phone layout that works one-handed.
- An accessibility pass over the whole twin.

**Chat tools**: `file_report` (confirmation first), image input in the chat.

---

## Stack and lanes

- The stack starts on PR #7 (`traffic-replay-on-map`). This plan is the first pull request on
  it; each feature's pull request is based on the one before it.
- Two lanes, A and B, each with its own ports (see [../CLAUDE.md](../CLAUDE.md)), so two
  features can be built and tested at once without colliding.
- The order above is the plan, not a promise: a feature that turns out to need another first
  says so in its pull request, and the next one adjusts.

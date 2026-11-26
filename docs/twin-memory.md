# The twin's memory

Feature 2 of the [roadmap](digital-twin-roadmap.md): history measured in weeks, answered in
about a second, without leaning harder on the city API.

The memory is the collector's DuckDB file, `data/smart_nicosia.duckdb` (set `COLLECTOR_DB`, or
`db_path` in `collector.toml`, to move it). Three things write to it and the chatbot reads it.

| Writer | What it stores | Cost to the city API |
| --- | --- | --- |
| The chatbot's recorder (on by default) | Everything the chatbot fetches anyway: device snapshots (the live strip refreshes them every 5 minutes while a page is open), history ranges behind replays, traffic and speed answers, alarms, the inventory, attributes and camera list | Nothing extra |
| `collector run`, or the chatbot with `CHATBOT_COLLECT=1` | Everything, on the collector's cadences | About 23,500 requests a day (`uv run collector budget`) |
| `collector backfill` | The history the API still holds, paged back gently | What you allow with `--max-requests` |

## Sharing one file

DuckDB lets one process write a file, or several read it, never both at once. Everyone keeps
connections short (open, work, close), so they take turns:

- **Between processes.** A collector waits up to two minutes for its turn; the chatbot's
  readers wait 1.5 s at most, and its recorder 5 s. A separately running `collector run` and
  the chatbot therefore share the file safely, each getting in between the other's writes.
- **Inside the chatbot.** DuckDB can't hold one file read-only and read-write in the same
  process, so `collector.db.GATE` lets readers in together and one writer alone. Reads that
  can't get in within the wait raise `Unavailable` instead of blocking.
- **Never a crash, never a stall.** When the file is missing, busy or broken, a replay falls
  back to the city API (last 72 hours), `query_history` and `run_sql` say plainly that the
  memory isn't available, and the recorder keeps its rows in memory (up to 300,000) and tries
  again on its next flush, every 30 seconds. `/api/health/data` shows the recorder's queue,
  its last flush and its last error.

Pick one of these setups:

1. **The chatbot alone** (the default). The memory grows while pages are open. Replays and
   questions fill gaps in the last 72 hours from the city API and keep what they fetched.
2. **The chatbot with full collection** (`CHATBOT_COLLECT=1` in `.env`). One process does
   everything, on the collector's budget. If a `collector run` is already running elsewhere it
   holds the run lock, and the chatbot's passes skip rather than poll twice.
3. **The chatbot plus a separate collector** (Task Scheduler, Docker or `collector run`). Point
   both at the same file; they take turns. Don't also set `CHATBOT_COLLECT=1`.

`CHATBOT_RECORD=0` turns the recorder off (the chatbot then only reads the file).

## What the store knows it holds

`coverage` records stretches of time for which the store holds every reading of a device type
(device `''`) or of one device: the collector's stream job covers each 5-minute window for
parking, lamps, bins, screens and cameras; each latest job and each chatbot snapshot is a
moment, and moments close enough together (about one report interval, `COVERAGE_GAP` in
`collector/db.py`) join into one stretch; a backfill covers each device from the oldest page it
reached to where it started. Readers use it to tell "nothing happened" from "nobody was
recording": a moment outside every stretch replays as unknown, never as the last known state.

## The history reader

`chatbot/history.py` `readings()` is the one reader. It takes what the memory covers, and for
any real gap in the last 72 hours asks the city API (one getTelemetryRange per device, from
the gap's start; with no lookback when the store already knows each device's state there).
Further back only the memory answers. `replay()` interprets the readings at each frame with
`collector/interpret.py`, as the live tools do.

On top of it:

- `get_history` replays up to 31 days (`until` for a past window) when the memory has them.
- `query_history` turns frames into metrics (occupancy, lamps lit, power and energy, fill, times
  a bin filled, CAQI and pollutants, noise; traffic counts; city speed) per hour, day, week,
  month, hour of day, weekday, hour by weekday or in total, for all devices together or per
  device, with weekday and hour filters.
- **Typical day**: the median and quartiles for that hour of that weekday (for days, that
  weekday) over up to 8 stored weeks before the range, or over every stored week when fewer
  than 3 come before it (`includes_range` says so).
- `run_sql` runs one SELECT on a read-only connection with no file access (no ATTACH, COPY,
  INSTALL or LOAD, settings locked), stopped after 5 s and cut at 2,000 rows.
- `/api/history/{id}?hours=24&metric=` gives one device's series with its usual band, for the
  inspector's sparkline (cached 2 minutes); `/api/health/data` gives freshness, row counts and
  coverage per source (cached 30 s).

## Filling the past

`collector backfill` pages each device's history back from now, gently (at most 2 requests in
flight, 0.5 s apart), and saves its progress, so it can be stopped and run again:

```bash
uv run collector run --once --jobs devices,cameras_map --force   # the inventory first (3 requests)
uv run collector backfill --since 28d --what events,speed,traffic --max-requests 120
uv run collector backfill --since 28d --types Parking --max-requests 650 --concurrency 2
uv run collector status
```

One page holds 1,000 records, so how far a request reaches depends on the device: about a
month for a parking sensor, two weeks for a bin, a day and a half for an air station, and only
hours for the few lamps that report every 5 minutes. Running one device type at a time with its
own `--max-requests` keeps a dense type from using up the budget of the others. For this
feature's tests, 28 days of parking, bins and air, and 182 of the 240 lamps, took 1,893
requests.

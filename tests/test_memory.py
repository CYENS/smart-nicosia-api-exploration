"""The twin's memory: the recorder, the shared store, the history reader and its fallback, the
typical-day baselines, query_history, every run_sql guard, and the two endpoints. Offline: four
synthetic weeks of a few devices are written through the recorder, and the city API is a fake.
"""

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import duckdb
import pytest

from fastapi import HTTPException

from chatbot import history, memory_tools, overview, server
from chatbot.routes import memory as memory_routes
from chatbot.city import CityData
from chatbot.history import Metric, Series
from chatbot.memory import Memory, SqlRejected, Unavailable
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from collector import db
from collector.parse import SpeedRow, TrafficRow

from conftest import ms

TZ = ZoneInfo("Asia/Nicosia")
NOW = datetime(2026, 11, 23, 0, 0, tzinfo=TZ).astimezone(timezone.utc)  # Monday 00:00 local
WEEKS = 4
START = NOW - timedelta(weeks=WEEKS)
RANGE_START = NOW - timedelta(days=7)  # last_7_days is exactly the ISO week before NOW

# Two bays by Makariou (A, B), one far away (C), two bins by them, one lamp.
DEVICES = [("YPA", "Parking", 35.1660, 33.3640), ("YPB", "Parking", 35.1662, 33.3642),
           ("YPC", "Parking", 35.1900, 33.3800), ("YW1", "Wastebin", 35.1661, 33.3641),
           ("YW2", "Wastebin", 35.1663, 33.3645), ("YL1", "Lighting", 35.1665, 33.3650)]


def inventory():
    return [{"device_name": n, "device_type": t, "device_label": None,
             "device_key_type": [{"key": "latitude", "value": str(la)},
                                 {"key": "longitude", "value": str(lo)}]}
            for n, t, la, lo in DEVICES]


def every(start, end, step):
    t = start
    while t <= end:
        yield t
        t += step


def bay_records(name: str) -> list[dict]:
    """Every 30 min a status and a heartbeat. On weekday evenings the bays are occupied from
    17:00 to 19:00 in the usual weeks, and from 17:00 to 21:00 in the last week."""
    out = []
    for t in every(START, NOW, timedelta(minutes=30)):
        local = t.astimezone(TZ)
        until = 21 if t >= RANGE_START else 19
        busy = local.isoweekday() <= 5 and 17 <= local.hour < until and name != "YPC"
        out += [{"key": "status", "timestamp": str(ms(t)), "value": "1" if busy else "0"},
                {"key": "vbatt", "timestamp": str(ms(t)), "value": "3.6"}]
    return out


def bin_records(name: str) -> list[dict]:
    """Hourly fill: YW1 fills up every 12 hours, YW2 every 24, emptied when full."""
    period = 12 if name == "YW1" else 24
    out = []
    for k, t in enumerate(every(START, NOW, timedelta(hours=1))):
        level = (k % period) * (92 // (period - 1))
        out.append({"key": "level", "timestamp": str(ms(t)), "value": str(level)})
    return out


@pytest.fixture
def memory(tmp_path):
    """A store with four weeks of the devices above, written by the recorder itself."""
    mem = Memory(tmp_path / "twin.duckdb")
    rec = mem.recorder
    rec.inventory(inventory())
    rec.readings("Parking", {n: bay_records(n) for n in ("YPA", "YPB", "YPC")},
                 [(n, ms(START), ms(NOW)) for n in ("YPA", "YPB", "YPC")])
    rec.readings("Wastebin", {n: bin_records(n) for n in ("YW1", "YW2")},
                 [(n, ms(START), ms(NOW)) for n in ("YW1", "YW2")])
    days = [datetime(2026, 11, d, tzinfo=TZ) for d in range(9, 23)]
    rec.cameras([{"analytics_id": 117, "name": "Ledras Square", "lat": 35.1730, "lon": 33.3610},
                 {"analytics_id": 118, "name": "Makariou Avenue", "lat": 35.1660,
                  "lon": 33.3640}])
    rec.traffic([TrafficRow("day", d, 117, "line 1", 0, None, None, 1000 + 10 * i)
                 for i, d in enumerate(days)]
                + [TrafficRow("day", d, 118, "line 1", 0, None, None, 100) for d in days]
                + [TrafficRow("day", d, 118, "line 1", 2, None, None, 500) for d in days])
    rec.speeds([SpeedRow(t.astimezone(TZ), t, "", 40.0 + t.astimezone(TZ).hour % 3)
                for t in every(NOW - timedelta(days=14), NOW - timedelta(hours=1),
                               timedelta(hours=1))], "ONE_HOUR", "AVERAGE_SPEED")
    assert rec.flush()
    return mem


@pytest.fixture
def fake_city(api, client, memory, monkeypatch, tmp_path):
    monkeypatch.setattr("chatbot.city.CACHE_DIR", tmp_path)
    api.on("getTenantDevices", inventory())
    api.on("getLatestAttribute", [])
    api.on("getTelemetryRange", lambda p: [])
    return CityData(Settings(), client=client, now=lambda: NOW, recorder=memory.recorder)


def make_ctx(city, memory):
    return ToolContext(Settings(), city, None, None, SessionStore(), [].append, "c1",
                       memory=memory)


def range_calls(api):
    return [p for e, p in api.calls if e == "getTelemetryRange"]


# -- coverage and the recorder -----------------------------------------------------------------


def test_coverage_spans_merge_within_the_gap(tmp_path):
    assert db.merge_spans([(10, 20), (25, 30), (100, 110)], 5) == [(10, 30), (100, 110)]
    assert db.merge_spans([(5, 5), (1, 9)], 0) == [(1, 9)]
    con = db.connect(tmp_path / "c.duckdb")
    gap = db.gap_ms("Parking")
    db.add_coverage(con, [("Parking", "", "chatbot", 0, 1000)])
    db.add_coverage(con, [("Parking", "", "chatbot", 1000 + gap - 1, 5 * gap)])  # touches
    db.add_coverage(con, [("Parking", "", "chatbot", 20 * gap, 21 * gap)])  # a gap
    db.add_coverage(con, [("Parking", "YP1", "chatbot", 0, 10)])  # another device
    rows = con.execute("SELECT device, epoch_ms(start_ts), epoch_ms(end_ts) FROM coverage "
                       "ORDER BY device, start_ts").fetchall()
    assert rows == [("", 0, 5 * gap), ("", 20 * gap, 21 * gap), ("YP1", 0, 10)]


def test_recorder_writes_like_the_collector_and_skips_what_it_stored(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb")
    rec = mem.recorder
    assert not mem.exists()
    rec.inventory(inventory())
    t1, t2 = ms(NOW - timedelta(minutes=10)), ms(NOW)
    snap = {"YPA": [{"key": "status", "timestamp": str(t1), "value": "1"},
                    {"key": "active", "timestamp": str(t1), "value": "true"}]}  # not a reading
    rec.snapshot("Parking", snap, t2, every_device=True)
    rec.traffic([TrafficRow("hour", NOW.astimezone(TZ), 0, "", 0, None, None, 42)])
    rec.alarms({"ACTIVE_UNACK": []})
    assert rec.flush() and mem.exists()  # the recorder creates the file
    with mem.reading() as con:
        assert con.execute("SELECT key, source FROM telemetry").fetchall() == [("status", "chatbot")]
        assert con.execute("SELECT count(*) FROM devices").fetchone()[0] == len(DEVICES)
        assert con.execute("SELECT device, epoch_ms(start_ts) FROM coverage").fetchall() == [("", t2)]
        assert con.execute("SELECT total FROM traffic_counts").fetchone()[0] == 42
    # The same snapshot again adds nothing to write; a change of status is a state change.
    rec.snapshot("Parking", snap, t2 + 60_000, every_device=True)
    assert rec.pending() == 1  # only the coverage point
    rec.snapshot("Parking", {"YPA": [{"key": "status", "timestamp": str(t2), "value": "0"}]},
                 t2 + 120_000, every_device=True)
    assert rec.flush()
    with mem.reading() as con:
        assert con.execute("SELECT old_state, new_state FROM state_changes").fetchall() == [
            ("occupied", "free")]
        assert con.execute("SELECT count(*) FROM coverage").fetchone()[0] == 1  # merged
    assert rec.status()["last_error"] is None and rec.status()["rows_written"] >= 2


def test_recorder_never_raises_and_keeps_rows_while_the_store_is_busy(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb")
    rec = mem.recorder
    rec.snapshot("Parking", {"YPA": [{"key": "status"}]}, 0, every_device=True)  # bad record
    rec.readings("Parking", {"YPA": [{"key": "status", "timestamp": "1000", "value": "1"}]},
                 [("YPA", 0, 1000)])
    held, release = threading.Event(), threading.Event()

    def writer():  # another writer of this process holds the store
        with db.GATE.hold(write=True):
            held.set()
            release.wait(5)

    threading.Thread(target=writer).start()
    held.wait(5)
    started = time.monotonic()
    assert rec.flush(timeout=0.2) is False
    assert time.monotonic() - started < 2
    assert rec.pending() > 0 and rec.busy_retries == 1
    mem.path.touch()  # a store file exists, so only the gate can turn the reader away
    with pytest.raises(Unavailable) as e:  # nor do readers wait
        with mem.reading(timeout=0.2):
            pass
    assert e.value.reason == "busy"
    mem.path.unlink()
    release.set()
    assert rec.flush(timeout=5)
    assert rec.pending() == 0


def test_a_store_locked_by_another_process_never_blocks(memory):
    holder = subprocess.Popen(
        [sys.executable, "-c", "import duckdb, sys, time; c = duckdb.connect(sys.argv[1]); "
         "print('ready', flush=True); time.sleep(20)", str(memory.path)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        started = time.monotonic()
        with pytest.raises(Unavailable) as e:
            with memory.reading(timeout=0.5):
                pass
        assert e.value.reason == "busy" and time.monotonic() - started < 3
        memory.recorder.snapshot("Parking", {"YPA": [{"key": "status", "value": "1",
                                                      "timestamp": str(ms(NOW))}]},
                                 ms(NOW), every_device=True)
        assert memory.recorder.flush(timeout=0.5) is False  # kept for later
        assert memory.health(NOW)["store"]["status"] == "busy"
    finally:
        holder.kill()
        holder.wait()
    assert memory.recorder.flush(timeout=5)


def test_a_missing_store_is_reported_not_raised(tmp_path):
    mem = Memory(tmp_path / "nothing.duckdb", record=False)
    with pytest.raises(Unavailable) as e:
        with mem.reading():
            pass
    assert e.value.reason == "missing"
    assert mem.health(NOW)["store"] == {"path": str(mem.path), "exists": False,
                                        "status": "missing"}


# -- the history reader ------------------------------------------------------------------------


def test_reader_answers_from_the_store_without_the_api(fake_city, memory, api):
    rd = history.readings(fake_city, memory, "Parking", ["YPA", "YPB"], ("status", "vbatt"),
                          ms(NOW - timedelta(days=20)), ms(NOW), lookback_ms=3_600_000)
    assert rd.source == "memory" and rd.coverage_pct() == 100.0
    assert len(rd.records["YPA"]) > 1000
    assert range_calls(api) == []


def test_reader_fills_a_recent_gap_from_the_api_without_lookback(fake_city, memory, api):
    later = NOW + timedelta(hours=6)
    fake_city._now = lambda: later
    api.on("getTelemetryRange", lambda p: [
        {"key": "status", "timestamp": str(ms(later - timedelta(hours=1))), "value": "1"}])
    rd = history.readings(fake_city, memory, "Parking", ["YPA", "YPB"], ("status", "vbatt"),
                          ms(later - timedelta(hours=24)), ms(later), lookback_ms=3_600_000)
    assert rd.source == "memory+city_api" and rd.coverage_pct() == 100.0
    calls = range_calls(api)
    assert len(calls) == 2  # one per device
    gap_starts = ms(NOW) + db.gap_ms("Parking")
    assert all(int(c["startTs"]) == gap_starts for c in calls)  # the store knew the state there
    assert any(r["value"] == "1" and int(r["timestamp"]) == ms(later - timedelta(hours=1))
               for r in rd.records["YPA"])
    assert memory.recorder.pending() > 0  # what the API gave is kept for next time


def test_reader_without_a_store_asks_the_api_for_72_hours_at_most(api, client, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr("chatbot.city.CACHE_DIR", tmp_path)
    api.on("getTenantDevices", inventory()).on("getTelemetryRange", lambda p: [])
    city = CityData(Settings(), client=client, now=lambda: NOW)
    mem = Memory(tmp_path / "none.duckdb", record=False)
    rd = history.readings(city, mem, "Parking", ["YPA"], ("status",), ms(NOW - timedelta(hours=24)),
                          ms(NOW), lookback_ms=3_600_000)
    assert rd.source == "city_api"
    assert int(range_calls(api)[0]["startTs"]) == ms(NOW - timedelta(hours=25))  # with lookback
    with pytest.raises(history.NoHistory):
        history.readings(None, mem, "Parking", ["YPA"], ("status",),
                         ms(NOW - timedelta(days=10)), ms(NOW - timedelta(days=5)))


def test_city_history_says_where_paging_was_cut_short(api, client):
    page = [{"key": "status", "timestamp": str(10_000 - i), "value": "1"} for i in range(1000)]
    api.on("getTenantDevices", inventory()).on("getTelemetryRange", lambda p: page)
    city = CityData(Settings(), client=client, now=lambda: NOW)
    _, complete = city.history_complete("Parking", ["YPA"], ("status",), 0, 10_000)
    assert complete == {"YPA": 9001}  # six full pages: complete only from the oldest one


def test_replay_marks_moments_outside_coverage_unknown(fake_city):
    states = memory_tools.device_entities(SimpleNamespace(city=fake_city, memory=None), "Parking")
    bay = [s for s in states if s.device.name == "YPA"]
    times = [NOW - timedelta(hours=h) for h in (30, 20, 10)]
    recs = {"YPA": [{"key": "status", "timestamp": str(ms(NOW - timedelta(hours=31))),
                     "value": "1"},
                    {"key": "vbatt", "timestamp": str(ms(NOW - timedelta(hours=11))),
                     "value": "3.6"}]}
    covered = {"YPA": [(ms(NOW - timedelta(hours=32)), ms(NOW - timedelta(hours=25))),
                       (ms(NOW - timedelta(hours=12)), ms(NOW))]}
    st, _ = history.replay("Parking", bay, recs, times, covered)
    assert [row[0] for row in st] == ["occupied", "unknown", "occupied"]
    st, _ = history.replay("Parking", bay, recs, times)  # without coverage: stale by then
    assert [row[0] for row in st] == ["occupied", "unknown", "occupied"]


# -- buckets and baselines ---------------------------------------------------------------------

MEAN = Metric("v", "", "", lambda st, v: None)


def test_baseline_is_the_median_for_that_hour_of_that_weekday():
    times = list(every(START, NOW - timedelta(hours=1), timedelta(hours=1)))
    week = lambda t: (t - START) // timedelta(weeks=1)  # noqa: E731
    noise = [0, 10, 20, 1000]  # the last week is unusual
    values = [[t.astimezone(TZ).isoweekday() * 100 + t.astimezone(TZ).hour + noise[week(t)]
               for t in times]]
    s = Series(times, 1.0, ["x"], values)
    b = history.baseline(s, MEAN, "hour", range_start=RANGE_START)
    assert not b.includes_range and b.weeks == 3
    assert b.by_key[(1, 8)][0] == 100 + 8 + 10  # median of 0, 10, 20
    assert b.by_key[(1, 8)][3] == 3
    b = history.baseline(s, MEAN, "hour", range_start=NOW - timedelta(weeks=3))
    assert b.includes_range and b.weeks == 4  # too few weeks before: every stored week
    day = history.baseline(s, MEAN, "day", range_start=RANGE_START)
    assert day.key_of(datetime(2026, 11, 16, tzinfo=TZ)) == 1
    assert day.by_key[1][0] == pytest.approx(100 + 11.5 + 10)


def test_bucketize_filters_weekdays_and_hours_past_midnight():
    times = list(every(RANGE_START, NOW - timedelta(hours=1), timedelta(hours=1)))
    s = Series(times, 1.0, ["x"], [[1.0] * len(times)])
    b = history.bucketize(s, MEAN, "hour_of_day", hours=(22, 2), weekdays={6})  # Saturday nights
    assert b.keys == [0, 1, 22, 23]
    assert history.bucketize(s, MEAN, "weekday_hour").keys[0] == (1, 0)


# -- query_history -----------------------------------------------------------------------------


def test_weekday_evenings_against_usual(fake_city, memory, api):
    ctx = make_ctx(fake_city, memory)
    res, err = run_tool(ctx, "query_history", {
        "kind": "parking_sensor", "metric": "occupancy_pct", "period": "last_7_days",
        "near_lat": 35.1661, "near_lon": 33.3641, "radius_m": 300, "bucket": "total",
        "weekdays": ["mon", "tue", "wed", "thu", "fri"], "hours": {"from": 17, "to": 21},
        "baseline": True})
    assert not err, res
    assert res["entities"] == 2 and res["source"] == "memory" and res["coverage_pct"] == 100.0
    assert res["overall"] == 100.0 and res["usual_overall"] == 50.0
    assert res["baseline"]["weeks"] == 3 and not res["baseline"]["includes_range"]
    assert res["filters"] == {"weekdays": ["mon", "tue", "wed", "thu", "fri"],
                              "hours": "17:00 to 21:00"}
    assert range_calls(api) == []

    res, err = run_tool(ctx, "query_history", {
        "kind": "parking_sensor", "metric": "occupancy_pct", "period": "last_7_days",
        "ids": ["YPA"], "bucket": "hour", "baseline": True})
    assert not err, res
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    # A rolling range ends now, so the current hour is the last bucket.
    assert len(rows) == 169 and rows[0]["time"] == "2026-11-16T00:00"
    assert rows[-1]["time"] == "2026-11-23T00:00"
    tue_20 = next(r for r in rows if r["time"] == "2026-11-17T20:00")
    assert tue_20["occupancy_pct"] == 100.0 and tue_20["usual"] == 0.0
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["frames"] == 169 and layer["features"][0]["properties"]["id"] == "YPA"
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "frame",
                                                           "layer": res["layer_ref"]}


def test_bins_ranked_by_how_often_they_filled(fake_city, memory):
    ctx = make_ctx(fake_city, memory)
    res, err = run_tool(ctx, "query_history", {
        "kind": "waste_bin", "metric": "became_full", "per": "entity", "bucket": "total",
        "period": "last_7_days"})
    assert not err, res
    assert [r["id"] for r in res["top"]] == ["YW1", "YW2"]
    assert res["top"][0]["became_full"] == 14 and res["top"][1]["became_full"] == 7
    data = ctx.store.datasets[res["data_ref"]]
    assert data["link"] == {"key": "id"} and data["rows"][0]["lat"] == 35.1661
    feature = ctx.store.layers[res["layer_ref"]]["features"][0]
    assert feature["properties"]["became_full"] == 14


def test_query_history_checks_its_input(fake_city, memory):
    ctx = make_ctx(fake_city, memory)
    for a, says in [({"kind": "waste_bin", "metric": "occupancy_pct"}, "must be one of"),
                    ({"kind": "parking_sensor", "metric": "occupancy_pct", "from": "2026-09-01"},
                     "longer than"),
                    ({"kind": "parking_sensor", "metric": "occupancy_pct", "ids": ["YX9"]},
                     "none of"),
                    ({"kind": "parking_sensor", "metric": "occupancy_pct", "from": "soon"},
                     "can't read")]:
        res, err = run_tool(ctx, "query_history", a)
        assert err and says in res["error"], res


def test_traffic_and_speed_from_the_store(fake_city, memory):
    ctx = make_ctx(fake_city, memory)
    res, err = run_tool(ctx, "query_history", {"kind": "camera", "metric": "people",
                                               "per": "entity", "bucket": "total",
                                               "period": "last_7_days"})
    assert not err, res
    assert [r["id"] for r in res["top"]] == ["Ledras Square", "Makariou Avenue"]
    assert res["top"][1]["people"] == 700  # seven days of 100
    res, err = run_tool(ctx, "query_history", {"kind": "camera", "metric": "car",
                                               "street": "makariou", "bucket": "day",
                                               "period": "last_7_days"})
    assert not err and {r["car"] for r in res["rows"]} == {500}
    res, err = run_tool(ctx, "query_history", {"kind": "city_speed", "metric": "speed_kmh",
                                               "bucket": "hour_of_day", "period": "last_7_days",
                                               "baseline": True})
    assert not err, res
    assert res["rows"][2]["speed_kmh"] == 42.0 and res["rows"][2]["usual"] == 42.0


def test_get_history_replays_days_from_the_store(fake_city, memory, api):
    api.on("getLatestTelemetry", lambda p: [])
    ctx = make_ctx(fake_city, memory)
    res, err = run_tool(ctx, "get_history", {"kind": "waste_bin", "hours": 240})
    assert not err, res
    assert res["step_minutes"] == 120 and res["frames"] == 121 and res["source"] == "memory"
    assert range_calls(api) == []
    res, err = run_tool(ctx, "get_history", {"kind": "waste_bin", "hours": 24,
                                             "until": "2026-11-10T12:00"})
    assert not err and res["to"].endswith("12:00") and res["source"] == "memory"


# -- run_sql -----------------------------------------------------------------------------------


@pytest.mark.parametrize("sql,says", [
    ("INSERT INTO devices (name) VALUES ('x')", "not INSERT"),
    ("UPDATE devices SET lat = 0", "not UPDATE"),
    ("DELETE FROM telemetry", "not DELETE"),
    ("CREATE TABLE x AS SELECT 1", "not CREATE"),
    ("ATTACH 'other.duckdb' AS other", "not ATTACH"),
    ("COPY telemetry TO 'out.csv'", "not COPY"),
    ("INSTALL httpfs", "not LOAD"),
    ("LOAD httpfs", "not LOAD"),
    ("SET enable_external_access = true", "not SET"),
    ("SELECT 1; DROP TABLE devices", "exactly one statement"),
    ("SELEC 1", "doesn't parse"),
])
def test_run_sql_refuses_anything_but_one_select(memory, sql, says):
    with pytest.raises(SqlRejected, match=says):
        memory.run_sql(sql)


def test_run_sql_cannot_reach_files_or_settings(memory, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    with pytest.raises(SqlRejected, match="disabled by configuration|Permission"):
        memory.run_sql(f"SELECT * FROM read_csv('{secret.as_posix()}')")
    cols, _, rows, _ = memory.run_sql("SELECT current_setting('enable_external_access') AS ext, "
                                      "current_setting('lock_configuration') AS locked")
    assert rows == [(False, True)]


def test_run_sql_stops_long_queries_and_cuts_huge_results(memory):
    started = time.monotonic()
    with pytest.raises(SqlRejected, match="was stopped"):
        memory.run_sql("SELECT count(*) FROM range(100000000000) a, range(1000) b",
                       time_limit_s=0.3)
    assert time.monotonic() - started < 3
    cols, _, rows, truncated = memory.run_sql("SELECT * FROM range(10000000) t(i)", max_rows=50)
    assert cols == ["i"] and len(rows) == 50 and truncated


def test_run_sql_tool_returns_a_data_ref_and_a_layer(fake_city, memory):
    ctx = make_ctx(fake_city, memory)
    res, err = run_tool(ctx, "run_sql", {"sql": """
        SELECT s.device AS id, d.lat, d.lon, count(*) AS times_full, max(s.ts) AS last_full
        FROM state_changes s JOIN devices d ON d.name = s.device
        WHERE s.new_state = 'full' GROUP BY ALL ORDER BY times_full DESC""",
        "title": "Bins by times full"})
    assert not err, res
    assert [r["id"] for r in res["rows"]] == ["YW1", "YW2"]
    assert res["rows"][0]["last_full"].startswith("2026-11-22T")  # local ISO text
    assert ctx.store.layers[res["layer_ref"]]["features"][0]["properties"]["times_full"] > 0
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    res, err = run_tool(ctx, "run_sql", {"sql": "DROP TABLE telemetry"})
    assert err and "only SELECT" in res["error"]


# -- endpoints ---------------------------------------------------------------------------------


@pytest.fixture
def app_agent(fake_city, memory, monkeypatch):
    agent = SimpleNamespace(settings=Settings(), city=fake_city, places=None, router=None,
                            memory=memory)
    monkeypatch.setattr(server.app.state, "agent", agent, raising=False)
    overview._cache.clear()
    return agent


REQUEST = SimpleNamespace(app=server.app)  # what the routes read the agent from


def test_history_endpoint_gives_a_sparkline_with_its_usual_band(app_agent, api):
    body = json.loads(memory_routes.history_series("YPA", REQUEST, hours=24, metric=None).body)
    assert body["metric"] == "occupancy_pct" and body["source"] == "memory"
    assert len(body["points"]) == 97  # every 15 minutes
    sunday = [p for p in body["points"] if p["time"].endswith("18:00")]
    assert sunday and sunday[0]["value"] == 0.0 and sunday[0]["usual"] == 0.0
    assert body["baseline"]["weeks"] >= 3
    calls = len(api.calls)
    memory_routes.history_series("YPA", REQUEST, hours=24, metric=None)
    assert len(api.calls) == calls  # cached
    with pytest.raises(HTTPException) as e:
        memory_routes.history_series("NOPE", REQUEST, hours=24, metric=None)
    assert e.value.status_code == 404


def test_health_data_reports_each_source(app_agent):
    body = json.loads(memory_routes.health_data(REQUEST).body)
    assert body["store"]["status"] == "ok"
    parking = next(s for s in body["sources"] if s["source"] == "parking_sensor")
    assert parking["devices"] == 3 and parking["rows_by_source"] == {"chatbot": parking["rows"]}
    assert parking["covered_last_7_days_pct"] == 100.0
    traffic = next(s for s in body["sources"] if s["source"] == "traffic_counts")
    assert traffic["rows"] == 42 and body["recorder"]["on"]


def test_reader_connections_are_locked_down(memory):
    with memory.reading() as con:
        with pytest.raises(duckdb.Error):
            con.execute("SET TimeZone = 'UTC'")
        assert con.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "Asia/Nicosia"


def test_a_street_is_traced_by_the_devices_named_for_it(fake_city, memory, api, monkeypatch):
    """Parking sensors carry no street names: the bins named for the street trace it."""
    api.on("getLatestAttribute", lambda p: [{"key": "name", "timestamp": "1",
                                             "value": "Glass 263 (Makariou)"}]
           if p["entityName"] == "YW1" else [])
    ctx = make_ctx(fake_city, memory)
    res, err = run_tool(ctx, "query_history", {"kind": "parking_sensor", "metric": "occupancy_pct",
                                               "street": "makariou", "bucket": "total"})
    assert not err, res
    assert res["entities"] == 2 and "named for makariou" in res["matched_by"]  # not YPC


def test_reader_always_fills_the_present(fake_city, memory, api):
    """A short gap is left alone in the middle of a long range, but never at its live end."""
    later = NOW + timedelta(minutes=45)  # under 2% of three days, past the 20 min parking gap
    fake_city._now = lambda: later
    rd = history.readings(fake_city, memory, "Parking", ["YPA"], ("status",),
                          ms(later - timedelta(hours=72)), ms(later), lookback_ms=3_600_000)
    assert len(range_calls(api)) == 1 and rd.coverage_pct() == 100.0
    rd = history.readings(fake_city, memory, "Parking", ["YPA"], ("status",),
                          ms(NOW - timedelta(hours=72)), ms(NOW - timedelta(hours=1)))
    assert len(range_calls(api)) == 1  # nothing missing in a closed past window

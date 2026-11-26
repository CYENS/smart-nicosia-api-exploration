"""The city clock: the city at a past moment from the twin's memory, the edges of what it
covers, the endpoints that take `?at=`, the track's coverage and events, and set_city_time.
Offline, on test_memory's four synthetic weeks (bays busy on weekday evenings, until 19:00 in
the usual weeks and until 21:00 in the last one; bins filling hourly; a lamp with no record)."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chatbot import moments as moments_mod
from chatbot import overview, server
from chatbot.agent import city_clock, format_context
from chatbot.entities import Entities
from chatbot.moments import Moments, OutOfRange, parse_at
from chatbot.routes import clock as clock_routes
from chatbot.routes import entities as entity_routes
from chatbot.routes import live as live_routes
from chatbot.routes import memory as memory_routes
from chatbot.settings import Settings
from chatbot.tools import run_tool

from conftest import ms
from test_memory import NOW, START, TZ, fake_city, make_ctx, memory  # noqa: F401 - fixtures

FRI_1800 = datetime(2026, 11, 20, 18, 0, tzinfo=TZ)  # the last week: busy until 21:00
FRI_2000 = datetime(2026, 11, 20, 20, 0, tzinfo=TZ)
WEEK_BEFORE = FRI_2000 - timedelta(days=7)  # a usual week: free again from 19:00


def by_id(snap):
    return {d.device.name: st for d, st in zip(snap.devices, snap.states)}


@pytest.fixture
def m(fake_city, memory):
    return Moments(fake_city, memory)


# -- moments -----------------------------------------------------------------------------------


def test_parse_at_reads_both_forms_and_refuses_what_the_clock_cannot_show():
    assert parse_at(None, NOW) is None and parse_at("", NOW) is None
    assert parse_at(str(ms(FRI_2000)), NOW) == FRI_2000
    assert parse_at("2026-11-20T20:00", NOW) == FRI_2000  # local wall time
    assert parse_at((NOW - timedelta(seconds=30)).isoformat(), NOW) is None  # that's live
    with pytest.raises(OutOfRange, match="future"):
        parse_at((NOW + timedelta(hours=1)).isoformat(), NOW)
    with pytest.raises(OutOfRange, match="62 days"):
        parse_at((NOW - timedelta(days=70)).isoformat(), NOW)
    with pytest.raises(OutOfRange, match="can't read"):
        parse_at("friday evening", NOW)


def test_a_snapshot_replays_the_moment_by_the_live_rules(m, api):
    snap = m.snapshot("parking_sensor", FRI_2000)
    assert snap.known and snap.coverage_pct == 100.0
    assert by_id(snap) == {"YPA": "occupied", "YPB": "occupied", "YPC": "free"}
    assert by_id(m.snapshot("parking_sensor", WEEK_BEFORE)) == {
        "YPA": "free", "YPB": "free", "YPC": "free"}
    # A moment between quarter hours reads the quarter hour before it.
    assert m.snapshot("parking_sensor", FRI_2000 + timedelta(minutes=14)).at == FRI_2000
    bins = m.snapshot("waste_bin", FRI_1800)
    assert bins.known and all("level_pct" in v for v in bins.values)
    assert not [c for c in api.calls if c[0] == "getTelemetryRange"]  # never the city API


def test_a_moment_outside_coverage_is_unknown_never_the_last_state(m):
    before = m.snapshot("parking_sensor", START - timedelta(hours=3))
    assert not before.known and set(before.states) == {"unknown"}
    assert before.reason() == moments_mod.NOTHING
    # Coverage starts at START: the first quarter hour after it is known.
    assert m.snapshot("parking_sensor", START + timedelta(minutes=15)).known
    # The lamp has no record at all, while parking at the same moment does.
    strip = m.strip_at(FRI_2000)
    assert strip["lights"] == {"unknown": True, "reason": moments_mod.NOTHING, "coverage_pct": 0.0}
    assert strip["parking"] == {"free": 1, "total": 3, "coverage_pct": 100.0}
    assert "full" in json.dumps(strip["bins"]) and strip["label"] == "Fri 20 Nov 2026 20:00"


def test_six_hours_are_read_once_and_kept(m, memory, monkeypatch):
    calls = []
    real = memory.readings
    monkeypatch.setattr(memory, "readings", lambda *a, **k: calls.append(a) or real(*a, **k))
    m.snapshot("parking_sensor", FRI_1800)
    m.snapshot("parking_sensor", FRI_1800 + timedelta(minutes=45))
    m.snapshot("parking_sensor", FRI_1800 + timedelta(hours=1, minutes=30))
    assert len(calls) == 1  # 18:00 to 23:45 is one block
    m.snapshot("parking_sensor", FRI_1800 + timedelta(hours=6))
    assert len(calls) == 2


def test_a_block_that_reaches_now_is_read_again(fake_city, memory, monkeypatch):
    now = [NOW - timedelta(minutes=50)]
    m = Moments(fake_city, memory, now=lambda: now[0])
    first = m.block("Parking", now[0] - timedelta(hours=1))
    assert first.live and first.times[-1] <= now[0]
    monkeypatch.setattr(moments_mod, "LIVE_TTL_S", -1)
    now[0] = NOW
    again = m.block("Parking", NOW - timedelta(hours=2))
    assert again is not first and again.times[-1] > first.times[-1]


def test_a_busy_or_missing_store_is_said_not_raised(fake_city, tmp_path):
    from chatbot.memory import Memory
    empty = Moments(fake_city, Memory(tmp_path / "none.duckdb", record=False))
    snap = empty.snapshot("parking_sensor", FRI_2000)
    assert not snap.known and snap.reason() == moments_mod.NOTHING
    assert empty.track(7)["status"] == "missing"
    assert Moments(fake_city, None).strip_at(FRI_2000)["parking"]["unknown"]


def test_one_device_at_a_moment(m, fake_city):
    device = next(d for d in fake_city.devices() if d.name == "YPA")
    interp, known = m.device_at(device, FRI_2000)
    assert known and interp.state == "occupied"
    interp, known = m.device_at(device, START - timedelta(days=1))
    assert not known and interp.state == "unknown"


# -- the track ---------------------------------------------------------------------------------


def test_the_track_shows_coverage_by_the_hour_and_the_events(m, memory):
    with memory.writing() as con:
        con.executemany("INSERT INTO events VALUES (?, ?, ?)", [
            (FRI_2000 + timedelta(minutes=5), "YW1", "YW1: Fill level above threshold of 80 %"),
            (FRI_2000 + timedelta(minutes=9), "YW2", "YW2: Lost connection"),
            (FRI_2000 + timedelta(minutes=20), "b827", "b827: High CAQI Levels")])
    tr = m.track(31)
    cov = tr["coverage"]
    hours = len(cov["all"])
    assert hours == 31 * 24 and tr["earliest"] == START.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")
    start = datetime.fromisoformat(cov["start"]).replace(tzinfo=TZ)
    first = int((START - start).total_seconds() // 3600)
    parking = cov["kinds"]["parking_sensor"]
    assert parking[first - 2] == 0 and parking[first + 2] == 100
    assert cov["kinds"]["street_light"][first + 2] == 0  # the lamp: nothing recorded
    assert 0 < cov["all"][first + 2] < 100  # parking and bins, not the lamp
    ev = next(e for e in tr["events"] if e["t"] == "2026-11-20T20:00")
    assert ev == {"t": "2026-11-20T20:00", "full_bins": 1, "air": 1, "faults": 1}
    near = m.events_near(FRI_2000 + timedelta(minutes=30))
    assert [e["kind"] for e in near] == ["air", "faults", "full_bins"]


# -- the endpoints -----------------------------------------------------------------------------


@pytest.fixture
def agent(fake_city, memory, monkeypatch):
    a = SimpleNamespace(settings=Settings(), city=fake_city, places=None, router=None,
                        memory=memory, registers=None,
                        entities=Entities(fake_city, memory=memory))
    monkeypatch.setattr(server.app.state, "agent", a, raising=False)
    monkeypatch.setattr(overview, "weather_history", lambda s: {
        "2026-11-20T20:00": {"temp_c": 14.2, "sky": "clear sky", "daylight": False}})
    overview._cache.clear()
    return a


REQUEST = SimpleNamespace(app=server.app)


def test_the_strip_and_the_lamps_at_a_moment(agent):
    strip = live_routes.overview(REQUEST, at="2026-11-20T20:00")
    assert strip["live"] is False and strip["parking"]["free"] == 1
    assert strip["weather"] == {"temp_c": 14.2, "sky": "clear sky", "daylight": False}
    lamps = json.loads(live_routes.lamps(REQUEST, at=str(ms(FRI_2000))).body)
    assert lamps["known"] is False and lamps["features"][0]["properties"]["state"] == "unknown"
    with pytest.raises(HTTPException) as e:
        live_routes.overview(REQUEST, at=(NOW + timedelta(days=1)).isoformat())
    assert e.value.status_code == 400 and "future" in e.value.detail


def test_states_for_restating_a_layer(agent):
    body = json.loads(clock_routes.states(REQUEST, kind="parking_sensor", at="2026-11-20T20:00").body)
    assert body["devices"]["YPA"]["s"] == "occupied" and body["devices"]["YPA"]["occupied"] == 1
    assert body["colors"]["occupied"] and body["known"]
    for bad in ({"kind": "camera", "at": "2026-11-20T20:00"}, {"kind": "parking_sensor", "at": None}):
        with pytest.raises(HTTPException) as e:
            clock_routes.states(REQUEST, **bad)
        assert e.value.status_code == 400
    track = json.loads(clock_routes.clock(REQUEST, days=7).body)
    assert track["status"] == "ok" and len(track["coverage"]["all"]) == 7 * 24


def test_an_entity_reads_as_it_was(agent, api):
    api.on("getLatestTelemetry", lambda p: [{"key": "status", "timestamp": str(ms(NOW)), "value": "0"},
                                            {"key": "vbatt", "timestamp": str(ms(NOW)), "value": "3.6"}])
    live = entity_routes.entity("YPA", REQUEST, series=False, at=None)
    assert live["state"]["value"] == "free" and "moment" not in live
    then = entity_routes.entity("YPA", REQUEST, series=False, at="2026-11-20T20:00")
    assert then["state"]["value"] == "occupied" and then["moment"]["known"]
    assert then["moment"]["label"] == "Fri 20 Nov 2026 20:00"
    assert then["sources"][0]["name"] == "The twin's memory"
    assert all("state" not in r for r in then["relations"])  # live dots would mislead
    assert "Last sign of life" not in {a["label"] for a in then["attributes"]}  # a live fact
    old = entity_routes.entity("YPA", REQUEST, series=False,
                               at=(START - timedelta(days=2)).isoformat())
    assert old["state"]["value"] == "unknown" and old["moment"]["known"] is False
    # Its 24 hours end 12 hours after the moment, on the hour.
    s = entity_routes.entity_series("YPA", REQUEST, at="2026-11-20T20:10")["series"]
    assert s["to"] == "2026-11-21T08:00" and s["from"] == "2026-11-20T08:00"


def test_the_usual_band_follows_the_clock(agent):
    body = json.loads(memory_routes.history_series("YPA", REQUEST, hours=24, metric=None,
                                                   until="2026-11-20T23:00").body)
    assert body["points"][-1]["time"] == "2026-11-20T23:00"
    evening = next(p for p in body["points"] if p["time"] == "2026-11-20T20:00")
    assert evening["value"] == 100.0 and evening["usual"] is not None


# -- the tool ----------------------------------------------------------------------------------


def test_set_city_time_moves_the_clock_and_reads_the_moment(fake_city, memory):
    events = []
    ctx = make_ctx(fake_city, memory)
    ctx.emit = events.append
    res, err = run_tool(ctx, "set_city_time", {"at": "2026-11-20T20:00"})
    assert not err, res
    assert events[-1] == {"type": "ui", "action": "clock", "live": False, "at": "2026-11-20T20:00",
                          "range": None, "play": False, "speed": 1, "compare": None,
                          "compare_offset_min": None}
    assert res["clock"] == "Fri 20 Nov 2026 20:00"
    assert res["readouts"]["free_parking"] == {"value": 1, "coverage_pct": 100.0, "of_known": 3}
    assert res["readouts"]["lamps_lit"]["value"] is None and "lamps_lit" in res["notes"][0]
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert [r["key"] for r in rows] == ["lamps_lit", "free_parking", "full_bins", "air_caqi"]


def test_set_city_time_compares_with_last_week(fake_city, memory):
    events = []
    ctx = make_ctx(fake_city, memory)
    ctx.emit = events.append
    res, err = run_tool(ctx, "set_city_time", {"at": "2026-11-20T20:00", "compare": "last_week",
                                               "kind": "parking_sensor"})
    assert not err, res
    assert events[-1]["compare"] == "2026-11-13T20:00" and events[-1]["compare_offset_min"] == -10080
    assert res["compare"]["readouts"]["free_parking"]["value"] == 3
    assert res["changed"] == 2  # YPA and YPB were free a week earlier
    layer = ctx.store.layers[res["layer_ref"]]
    ypa = next(f["properties"] for f in layer["features"] if f["properties"]["id"] == "YPA")
    assert ypa["state"] == "occupied" and ypa["then_state"] == "free" and ypa["changed"]
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert next(r for r in rows if r["key"] == "free_parking")["then"] == 3


def test_set_city_time_ranges_live_and_mistakes(fake_city, memory):
    events = []
    ctx = make_ctx(fake_city, memory)
    ctx.emit = events.append
    res, err = run_tool(ctx, "set_city_time", {"from": "2026-11-20T18:00", "to": "2026-11-21T02:00",
                                               "speed": 4})
    assert not err, res
    ev = events[-1]
    assert ev["range"]["step_min"] == 15 and ev["play"] and ev["speed"] == 4
    assert ev["at"] == "2026-11-20T18:00"
    res, err = run_tool(ctx, "set_city_time", {"live": True})
    assert not err and events[-1] == {"type": "ui", "action": "clock", "live": True,
                                      "compare": "off", "range": None}
    for bad, says in (({"at": "2026-12-01T10:00"}, "future"),
                      ({"from": "2026-11-21T02:00", "to": "2026-11-20T18:00"}, "empty"),
                      ({"from": "2026-11-20T18:00", "to": "2026-11-20T20:00",
                        "at": "2026-11-21T10:00"}, "inside the range"),
                      ({"live": True, "at": "2026-11-20T18:00"}, "not both"),
                      ({}, "give a past moment"),
                      ({"at": "2026-11-20T18:00", "compare": "2026-08-01T10:00"}, "62 days")):
        res, err = run_tool(ctx, "set_city_time", bad)
        assert err and says in res["error"], (bad, res)


def test_the_context_says_what_the_clock_shows():
    assert city_clock(None) is None
    assert city_clock({"at": None}) == "live"
    line = city_clock({"at": "2026-11-20T20:00", "compare": "2026-11-13T20:00",
                       "range": ["2026-11-20T18:00", "2026-11-21T02:00"], "playing": True})
    assert line.startswith("viewing Fri 2026-11-20 20:00")
    assert "playing the range Fri 2026-11-20 18:00 to Sat 2026-11-21 02:00" in line
    assert "comparing with Fri 2026-11-13 20:00" in line
    assert city_clock({"at": "<script>"}) == "live"  # nothing but a wall time gets in
    text = format_context({"clock": {"at": "2026-11-20T20:00"}},
                          datetime(2026, 11, 23, tzinfo=timezone.utc))
    assert "city_clock: viewing Fri 2026-11-20 20:00" in text

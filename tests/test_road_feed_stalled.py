"""A stalled road feed: the national access point answering with a fresh publication time while
every path carries the same old measurement. Told apart from "no readings", said in plain words,
with each road's usual speed standing in on the map (never as a reading), an honest freshness
probe and one incident in the watch until readings return. Offline, on the capture of Fri 27 Nov
2026 21:30 in Nicosia (measured Thu 26 Nov 23:42), trimmed to the eight paths of the other road
fixtures."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chatbot import overview, server
from chatbot.entities import Entities
from chatbot.memory import Memory
from chatbot.mobility import roads as roads_mod
from chatbot.mobility.gtfs import TZ
from chatbot.mobility.roads import LIVE_URL, since_words, stalled_note
from chatbot.routes import mobility as mobility_routes
from chatbot.routes import sources as sources_routes
from chatbot.settings import Settings
from chatbot.tools import run_tool
from chatbot.watch import Watcher
from chatbot.watch import detectors as D
from chatbot.watch.detectors import Observed
from chatbot.watch.incidents import Book
from collector import parse

from conftest import FIXTURES, load, utc
from test_mobility import FakeFetch, ctx_for, make_mobility, roads

STALLED_NOW = utc(2026, 11, 27, 19, 30, 25)  # Fri 21:30 in Nicosia, when the capture was made
FROZEN = utc(2026, 11, 26, 21, 42)  # Thu 23:42 in Nicosia: every path's measurement time
REQUEST = SimpleNamespace(app=server.app)


def stalled_fetch() -> FakeFetch:
    fetch = FakeFetch()
    fetch.files[LIVE_URL] = (FIXTURES / "nap_travel_times_stalled.xml").read_bytes()
    return fetch


def usual_memory(tmp_path) -> Memory:
    """Two earlier Fridays from 21:00 to 22:00 for road-1 (45 km/h) and road-2 (20 km/h), and
    only one Friday for road-5."""
    mem = Memory(tmp_path / "twin.duckdb")
    base = {r.segment_id: r for r in parse.parse_nap(load("nap_travel_times_stalled.xml"))}
    rows = []
    for day, ids in ((datetime(2026, 11, 20, 21, 0, tzinfo=TZ), ("1", "2", "5")),
                     (datetime(2026, 11, 13, 21, 0, tzinfo=TZ), ("1", "2"))):
        for k in range(12):
            t = (day + timedelta(minutes=5 * k)).astimezone(timezone.utc)
            for sid, kmh in (("1", 45.0), ("2", 20.0), ("5", 30.0)):
                if sid in ids:
                    rows.append(replace(base[sid], measured_at=t, speed_kmh=kmh, travel_time_s=1000 / (kmh / 3.6)))
    mem.recorder.travel_times(rows)
    assert mem.recorder.flush()
    return mem


@pytest.fixture
def at_capture(monkeypatch):
    monkeypatch.setattr(roads_mod.time, "time", lambda: STALLED_NOW.timestamp())


# -- the feed and the words ------------------------------------------------------------------------


def test_the_capture_is_a_fresh_publication_of_an_old_measurement():
    rows = parse.parse_nap(load("nap_travel_times_stalled.xml"))
    assert len(rows) == 8 and {r.measured_at for r in rows} == {FROZEN}
    assert FROZEN.astimezone(TZ).strftime("%a %H:%M") == "Thu 23:42"  # 'Z' read as UTC
    assert rows[0].publication_time == STALLED_NOW.replace(microsecond=372775)
    assert sum(r.speed_kmh is not None for r in rows) == 6  # two paths report -1


def test_when_the_detectors_last_sent_in_plain_words():
    assert since_words(utc(2026, 11, 27, 12, 5), STALLED_NOW) == "14:05"
    assert since_words(FROZEN, STALLED_NOW) == "Thursday 23:42"
    assert since_words(utc(2026, 11, 19, 21, 42), STALLED_NOW) == "19 November 23:42"
    assert stalled_note(FROZEN, STALLED_NOW) == "The national road detectors have sent nothing new since Thursday 23:42."


# -- the status ---------------------------------------------------------------------------------------


def test_a_stalled_feed_is_its_own_status_with_the_time_of_its_last_measurement(tmp_path, at_capture):
    r, _ = roads(tmp_path, now=STALLED_NOW, fetch=stalled_fetch())
    out = r.speeds()
    assert out["status"] == "stalled" and out["error"] is None and out["measured"] == 0
    assert out["last_measured_at"] == "2026-11-26T23:42+02:00"
    assert out["note"] == "The national road detectors have sent nothing new since Thursday 23:42."
    assert all(x["state"] == "no_reading" and x["speed_kmh"] is None for x in out["segments"])
    assert out["usual"] == 0 and not any(x["usual_state"] for x in out["segments"])  # no memory
    h = r.health()
    assert h["stalled"] and h["newest"] == FROZEN and h["error"] is None and h["published"]
    # The same reading 20 minutes after it was measured is live.
    fresh, _ = roads(tmp_path / "fresh", now=FROZEN + timedelta(minutes=20), fetch=stalled_fetch())
    assert fresh.speeds()["status"] == "live" and not fresh.health()["stalled"]


def test_stalled_is_not_no_readings_nor_a_feed_that_doesnt_answer(tmp_path, at_capture):
    rows = parse.parse_nap(load("nap_travel_times_stalled.xml"))
    empty, _ = roads(tmp_path, now=STALLED_NOW)
    # Measured a minute ago, but no car crossed both detectors on any path: no readings.
    empty._live = (STALLED_NOW.timestamp(), {x.segment_id: replace(x, measured_at=STALLED_NOW - timedelta(minutes=1),
                                                                   speed_kmh=None) for x in rows}, None)
    assert empty.speeds()["status"] == "no_readings"
    down = stalled_fetch()
    down.fail[LIVE_URL] = TimeoutError("timed out")
    r, _ = roads(tmp_path / "down", now=STALLED_NOW, fetch=down)
    out = r.speeds()
    assert out["status"] == "unavailable" and "didn't answer" in out["error"] and "note" not in out


def test_the_frozen_reading_is_kept_once_however_often_it_is_fetched(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb")
    fetch = stalled_fetch()
    r = roads_mod.Roads(lambda url: fetch(url).decode("utf-8"), memory=mem, now=lambda: STALLED_NOW,
                        cache_dir=tmp_path, live_ttl_s=0)
    for _ in range(3):
        r.live()
        assert mem.recorder.flush()
    assert fetch.count(LIVE_URL) == 3
    with mem.reading() as con:
        assert con.execute("SELECT count(*), count(DISTINCT measured_at) FROM travel_times").fetchone() == (8, 1)
    # So the frozen reading weighs once in the usual, the free flow and a road's series.
    assert len(r.series("1", 48, end=STALLED_NOW)) == 1
    assert r.measured_free_flow()["1"][1] == 1


# -- the usual stands in ----------------------------------------------------------------------------


def test_while_stalled_each_road_shows_its_usual_for_this_hour_never_as_a_reading(tmp_path, at_capture):
    r, _ = roads(tmp_path, now=STALLED_NOW, memory=usual_memory(tmp_path), fetch=stalled_fetch())
    out = r.speeds()
    rows = {x["id"]: x for x in out["segments"]}
    assert out["status"] == "stalled" and out["usual"] == 2
    one, two, five = rows["road-1"], rows["road-2"], rows["road-5"]
    assert one["speed_kmh"] is None and one["state"] == "no_reading" and one["share_of_free_flow"] is None
    assert (one["usual_kmh"], one["usual_days"], one["usual_state"]) == (45.0, 2, "moving")
    assert two["usual_state"] == "slow" and two["usual_label"] == "Usually slow"
    assert five["usual_days"] == 1 and five["usual_state"] is None  # one Friday is too little
    segs = {s.id: s for s in r.segments()}
    f = roads_mod.segment_feature(segs["road-2"], two)["properties"]
    assert f["state"] == "no_reading" and f["usual_state"] == "slow" and f["color"] == roads_mod.USUAL_COLOR
    assert f["label"] == "" and f["speed_kmh"] is None and "Usually slow at this hour (20 km/h)" in f["popup"]
    assert "usual_state" not in roads_mod.segment_feature(segs["road-5"], five)["properties"]
    assert [x["id"] for x in roads_mod.usually_slowest(out["segments"])] == ["road-2", "road-1"]
    # While the feed is live, the usual stays on the meter and off the map.
    live, _ = roads(tmp_path / "live", now=FROZEN + timedelta(minutes=10), memory=usual_memory(tmp_path / "live"),
                    fetch=stalled_fetch())
    assert not any(x["usual_state"] for x in live.speeds()["segments"])


def test_get_road_speeds_says_the_feed_stalled_and_gives_no_speed_as_now(tmp_path, at_capture):
    mem = usual_memory(tmp_path)
    m, _ = make_mobility(tmp_path, now=STALLED_NOW, memory=mem, fetch=stalled_fetch())
    ctx, _ = ctx_for(m, now=STALLED_NOW, memory=mem)
    res, err = run_tool(ctx, "get_road_speeds", {})
    assert not err and res["status"] == "stalled" and res["measured"] == 0 and res["slowest"] == []
    assert res["note"].startswith("The national road detectors have sent nothing new since Thursday 23:42.")
    assert res["last_measured_at"] == "2026-11-26T23:42+02:00" and res["_summary"].endswith("Thursday 23:42")
    assert [x["id"] for x in res["usually_slowest"]] == ["road-2", "road-1"] and res["usual_shown"] == 2
    assert res["usually_slowest"][0]["usual"] == "Usually slow" and "dotted" in res["map_note"]
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["live"] == {"kind": "roads", "at": None}
    assert layer["meta"]["legend"] == {"Usual for this hour": "#8798b0", "No reading": "#424e5e"}
    assert "stalled" in layer["meta"]["title"]


def test_the_road_endpoint_the_summary_and_the_inspector_say_stalled(tmp_path, at_capture, monkeypatch):
    m, _ = make_mobility(tmp_path, now=STALLED_NOW, memory=usual_memory(tmp_path), fetch=stalled_fetch())
    agent = SimpleNamespace(settings=Settings(), city=SimpleNamespace(now=lambda: STALLED_NOW), mobility=m)
    monkeypatch.setattr(server.app.state, "agent", agent, raising=False)
    overview._cache.clear()
    meta = (body := json.loads(mobility_routes.roads(REQUEST, at=None).body))["meta"]
    assert meta["status"] == "stalled" and meta["last_measured_at"] == "2026-11-26T23:42+02:00"
    assert meta["usual"] == 2 and meta["legend"] == {"Usual for this hour": "#8798b0", "No reading": "#424e5e"}
    assert {f["properties"]["id"] for f in body["features"] if f["properties"].get("usual_state")} == {"road-1", "road-2"}
    summary = m.summary()["roads"]
    assert summary["status"] == "stalled" and summary["note"].endswith("since Thursday 23:42.")
    assert summary["slowest"] == [] and summary["usually_slowest"][0]["id"] == "road-2"
    road = Entities(None, mobility=m).get("road-2")
    assert road["state"]["value"] == "no_reading" and road["state"]["label"] == "No reading: the detectors have stalled"
    assert road["road"]["stalled"] and road["road"]["last_measured_at"] == "2026-11-26T23:42+02:00"
    assert road["road"]["usual_kmh"] == 20.0 and road["road"]["usual_days"] == 2
    assert not any(x["key"] == "speed_kmh" for x in road["readings"])  # no speed now


# -- the freshness probe ---------------------------------------------------------------------------


def health(**over) -> dict:
    return {"read_at": STALLED_NOW.timestamp(), "read_ok_at": STALLED_NOW.timestamp(), "error": None,
            "newest": FROZEN, "published": STALLED_NOW, "stalled": True, **over}


def probe(h) -> dict:
    roads_ = SimpleNamespace(health=lambda: h, now=lambda: STALLED_NOW)
    return sources_routes._roads(SimpleNamespace(mobility=SimpleNamespace(roads=roads_)))


def test_the_freshness_probe_judges_by_measurement_time(tmp_path, at_capture):
    st = probe(health())
    assert st["state"] == "stalled" and st["measured_at"] == "2026-11-26T23:42+02:00"
    assert st["note"].startswith("the detectors have sent nothing new since Thursday 23:42")
    assert st["as_of"].startswith("2026-11-27T21:30")  # when the twin read it: it answers
    fresh = probe(health(newest=STALLED_NOW - timedelta(minutes=4)))
    assert fresh["state"] == "live" and fresh["note"] == "newest measurement 21:26"
    down = "the national traffic feed didn't answer"
    assert probe(health(error=down, newest=STALLED_NOW - timedelta(minutes=8)))["state"] == "stale"
    assert probe(health(error=down))["state"] == "unavailable"  # and what it last had is old
    assert probe(None)["state"] == "waiting"
    # The real thing: the capture read through Roads.
    r, _ = roads(tmp_path, now=STALLED_NOW, fetch=stalled_fetch())
    r.speeds()
    st = sources_routes._roads(SimpleNamespace(mobility=SimpleNamespace(roads=r)))
    assert st["state"] == "stalled" and "Thursday 23:42" in st["note"]


# -- the watch ------------------------------------------------------------------------------------


def feed_obs(now: datetime, h: dict | None) -> Observed:
    obs = Observed(now, {}, {}, {})
    if h is not None:
        obs.feeds["road_speeds"] = h
    return obs


def test_the_watch_raises_one_incident_while_the_feed_is_stalled_and_closes_it_after():
    found = D.road_feed_stalled(feed_obs(STALLED_NOW, health()))
    assert len(found) == 1
    d = found[0]
    assert (d.rule, d.family, d.device, d.severity) == ("road_feed_stalled", "feed", "source-road_speeds", "medium")
    assert d.start == FROZEN + timedelta(minutes=30) and d.end is None and d.what == "nothing new since Thu 23:42"
    assert any("still answers" in e.text for e in d.evidence)
    # Not read lately, or its last read failed: the watch can't tell, so nothing ends.
    assert D.road_feed_stalled(feed_obs(STALLED_NOW + timedelta(minutes=20), health())) is None
    assert D.road_feed_stalled(feed_obs(STALLED_NOW, health(error="timed out"))) is None
    _, ran = D.detect(feed_obs(STALLED_NOW, None))
    assert "road_feed_stalled" not in ran

    book = Book(None, lambda: STALLED_NOW)
    changes = book.update(found, STALLED_NOW, None, {"road_feed_stalled"})
    assert [c["change"] for c in changes if c["change"] != "updated"] == ["new"]
    inc = changes[0]["incident"]
    assert inc["category"] == "Data feed" and inc["severity"] == "medium" and inc["lat"] is None
    assert inc["title"] == "National road detectors: nothing new since Thu 23:42"
    detail = book.detail(book.get(inc["id"]), STALLED_NOW)
    assert "still answers" in detail["summary"] and detail["members"][0]["id"] == "source-road_speeds"
    # Five minutes on, still stalled: the same incident.
    later = STALLED_NOW + timedelta(minutes=5)
    again = D.road_feed_stalled(feed_obs(later, health(read_ok_at=later.timestamp())))
    assert all(c["change"] == "updated" for c in book.update(again, later, None, {"road_feed_stalled"}))
    assert len(book.incidents) == 1
    # A fresh measurement: nothing found, so it ends, and the twin resolves it 20 minutes on.
    back = later + timedelta(minutes=5)
    fresh = health(read_ok_at=back.timestamp(), newest=back - timedelta(minutes=3))
    assert D.road_feed_stalled(feed_obs(back, fresh)) == []
    book.update([], back, None, {"road_feed_stalled"})
    assert not book.get(inc["id"])["active"]
    changes = book.update([], back + timedelta(minutes=25), None, {"road_feed_stalled"})
    assert [c["change"] for c in changes if c["change"] != "updated"] == ["resolved"]


def test_the_watcher_reads_the_road_feed_as_its_last_fetch_left_it(tmp_path, at_capture):
    mem = Memory(tmp_path / "twin.duckdb")
    r, fetch = roads(tmp_path, now=STALLED_NOW, memory=mem, fetch=stalled_fetch())
    w = Watcher(None, mem, now=lambda: STALLED_NOW)
    w.feeds["road_speeds"] = r.health
    assert w.evaluate() == []  # nobody has read the feed yet: nothing to say
    r.speeds()  # a page reads it (and the recorder keeps the frozen reading)
    changes = [c for c in w.evaluate() if c["change"] != "updated"]
    assert [c["change"] for c in changes] == ["new"]
    assert changes[0]["incident"]["title"] == "National road detectors: nothing new since Thu 23:42"
    assert fetch.count(LIVE_URL) == 1  # the watch never fetches the feed itself
    assert w.book.query(status="unresolved", family="feed")[0]["id"] == changes[0]["incident"]["id"]

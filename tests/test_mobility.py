"""Live mobility: the bus timetables, the live feed joined to them, departures and trips, road
speeds against free flow, the new entity kinds, the tools and the endpoints. All offline, on
captures from 26 Nov 2026 07:26 (a GTFS subset of NPT, Intercity and Pame Express, the GTFS-RT
feed trimmed to their buses) and the Bluetooth inventories with the travel times of 19 Oct."""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chatbot import overview, server
from chatbot.entities import Entities, EntityNotFound
from chatbot.memory import Memory
from chatbot.mobility import Mobility, planner
from chatbot.mobility import realtime as rt_mod
from chatbot.mobility import roads as roads_mod
from chatbot.mobility.gtfs import FEEDS, NIGHT, TZ, Feed, Timetable, contrast, line_color
from chatbot.mobility.realtime import RT_URL, delay_text
from chatbot.mobility.roads import (LIVE_URL, PATHS_URL, SITES_URL, band, parse_moment, parse_paths,
                                    parse_sites, road_name)
from chatbot.routes import mobility as mobility_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from collector import parse

from conftest import FIXTURES, load, utc

NOW = datetime.fromtimestamp(1795670769, timezone.utc)  # Thu 26 Nov 2026, 07:26 in Nicosia
ROAD_NOW = utc(2026, 10, 19, 9, 30)  # minutes after the travel times fixture
ELEFTHERIA = (35.16855, 33.36148)
MUSEUM = (35.17178, 33.35592)
HOSPITAL = (35.1237, 33.3787)


def subset_zips():
    return [(f, (FIXTURES / f"gtfs_{f.key}_subset.zip").read_bytes()) for f in FEEDS]


@pytest.fixture(scope="module")
def tt():
    return Timetable.from_zips(subset_zips())


class FakeFetch:
    """The feeds, from fixtures; `fail` makes a URL raise. Counts what was asked."""

    def __init__(self):
        self.files = {f.url: (FIXTURES / f"gtfs_{f.key}_subset.zip").read_bytes() for f in FEEDS}
        self.files[RT_URL] = (FIXTURES / "gtfs_rt_2026-11-26.pb").read_bytes()
        self.files[PATHS_URL] = (FIXTURES / "nap_paths.xml").read_bytes()
        self.files[SITES_URL] = (FIXTURES / "nap_sites.xml").read_bytes()
        self.files[LIVE_URL] = (FIXTURES / "nap_travel_times.xml").read_bytes()
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        return self.files[url]

    def count(self, url: str) -> int:
        return self.calls.count(url)


class StubRouter:
    """Valhalla's answer: a straight line at `kmh`, `detour` times longer than asked."""

    def __init__(self, kmh=30.0, detour=1.0):
        self.kmh, self.detour, self.calls = kmh, detour, 0

    def route(self, stops, mode, prefer, avoid=None):
        self.calls += 1
        from chatbot.geo import haversine_m
        m = sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(stops, stops[1:])) * self.detour
        speed = 5.0 if mode == "walk" else self.kmh
        return {"distance_km": round(m / 1000, 3), "duration_min": round(m / 1000 / speed * 60, 2),
                "coordinates": [[lon, lat] for lat, lon in stops], "legs": []}


def make_mobility(tmp_path, now=NOW, memory=None, router=None, fetch=None):
    fetch = fetch or FakeFetch()
    m = Mobility(Settings(), memory, router, fetch=fetch, now=lambda: now, cache_dir=tmp_path)
    m.require_timetable(30)
    return m, fetch


# -- the timetable ---------------------------------------------------------------------------------


def test_three_feeds_make_one_timetable(tt):
    assert sorted(tt.lines) == ["line-ic-42", "line-npt-26", "line-npt-3", "line-npt-41",
                                "line-npt-l1", "line-pame-gsp1"]
    # Stop ids are one national registry: a stop in two feeds is one stop.
    ids = [s.id for s in tt.stops]
    assert len(ids) == len(set(ids))
    solomos = tt.stop("1")
    assert solomos.name == "Dionysios Solomos Square" and solomos.name_el == "Πλατεία Διονυσίου Σολωμού"
    assert {ln.id for ln in tt.lines_at(solomos)} >= {"line-ic-42"}
    line = tt.lines["line-npt-26"]
    assert line.short == "26" and line.operator == "NPT" and line.name == "Gen. Hospital - Solomos Square"
    # Headsigns: cut at 17 characters in the feed, read whole from the last stop; capitals tamed.
    heads = {t.headsign for t in tt.trips}
    assert "Nicosia General Hospital" in heads and "Larnaca" in heads
    assert not any(h.isupper() for h in heads)


def test_line_colours_stand_out_on_the_night_map():
    for raw in ("0000FF", "808000", "804040", "FFFF00", "", "zz"):
        color, text = line_color(raw)
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        assert contrast(rgb, NIGHT) >= 3.0
        trgb = (14, 24, 40) if text == "#0e1828" else (255, 255, 255)
        assert contrast(rgb, trgb) >= 3.0


def test_departures_and_live_matching(tt):
    stops = planner.stops_named(tt, "Solomos")
    assert stops[0].name.startswith(("Dionysios Solomos Square", "Solomos Square"))
    deps = tt.departures([s.idx for s in stops], NOW, 30)
    assert deps and all(NOW <= when <= NOW + timedelta(minutes=30) for when, _, _ in deps)
    assert deps == sorted(deps, key=lambda d: (d[0], d[1].line_id))
    # A live vehicle names its trip by route, direction and first departure, not by trip id.
    trip = deps[0][1]
    start = f"{trip.start // 3600:02d}:{trip.start % 3600 // 60:02d}:{trip.start % 60:02d}"
    assert tt.trip_for_live(trip.route_id, trip.direction, start, "20261126") is trip
    assert tt.trip_for_live(trip.route_id, trip.direction, "03:33:00", "20261126") is None
    # Stops sit in order along each trip's shape.
    along = tt.along(trip)
    assert len(along) == len(trip.stops) and all(b >= a - 5 for a, b in zip(along, along[1:]))


def gtfs_zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in files.items():
            z.writestr(name, text.strip() + "\n")
    return out.getvalue()


# A tiny network: line 1 from A to B, line 2 from B to C. No bus goes from A to C.
SYNTHETIC = {
    "routes.txt": """route_id,agency_id,route_short_name,route_long_name,route_type,route_color
r1,9,1,A - B,3,0000FF
r2,9,2,B - C,3,FF0000""",
    "stops.txt": """stop_id,stop_code,stop_name,stop_lat,stop_lon
A,10,Alpha,35.100,33.300
B,20,Beta,35.100,33.320
C,30,Gamma,35.120,33.320""",
    "calendar.txt": """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
wk,1,1,1,1,1,0,0,20261101,20261231""",
    "calendar_dates.txt": """service_id,date,exception_type
wk,20261127,2""",
    "trips.txt": """route_id,service_id,trip_id,trip_headsign,shape_id,direction_id
r1,wk,t1,Beta,s1,0
r1,wk,t1late,Beta,s1,0
r2,wk,t2,Gamma,s2,0""",
    "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence
t1,08:00:00,08:00:00,A,1
t1,08:10:00,08:10:00,B,2
t1late,24:30:00,24:30:00,A,1
t1late,24:40:00,24:40:00,B,2
t2,08:15:00,08:15:00,B,1
t2,08:25:00,08:25:00,C,2""",
    "shapes.txt": """shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence
s1,35.100,33.300,1
s1,35.100,33.320,2
s2,35.100,33.320,1
s2,35.120,33.320,2""",
}


@pytest.fixture(scope="module")
def tiny():
    return Timetable.from_zips([(Feed("npt", "NPT", 9, ""), gtfs_zip(SYNTHETIC))])


def test_service_days_and_trips_past_midnight(tiny):
    thu = datetime(2026, 11, 26, 7, 55, tzinfo=TZ)
    assert [t.id for _, t, _ in tiny.departures([tiny.stop_index["A"]], thu, 30)] == ["t1"]
    # Friday 27 Nov is taken out of the weekly service; Saturday isn't in it.
    for day in (27, 28):
        assert not tiny.departures([tiny.stop_index["A"]], datetime(2026, 11, day, 7, 55, tzinfo=TZ), 30)
    # 24:30 on Wednesday's service is 00:30 on Thursday.
    late = tiny.departures([tiny.stop_index["A"]], datetime(2026, 11, 26, 0, 20, tzinfo=TZ), 30)
    assert [(w.strftime("%a %H:%M"), t.id) for w, t, _ in late] == [("Thu 00:30", "t1late")]


def test_a_trip_with_one_change(tiny):
    start = datetime(2026, 11, 26, 7, 50, tzinfo=TZ)
    res = planner.plan(tiny, (35.1001, 33.3001), (35.1199, 33.3199), start)
    best = res["options"][0]
    assert best["lines"] == ["1", "2"] and best["changes"] == 1
    assert [leg["mode"] for leg in best["legs"]] == ["walk", "bus", "change", "bus", "walk"]
    change = best["legs"][2]
    assert change["at"] == "Beta" and change["wait_min"] == 5
    assert best["arrive"] >= "08:25"
    # Too close to bother, and nowhere near a stop.
    assert "walking is quicker" in planner.plan(tiny, (35.1, 33.3), (35.1, 33.302), start)["why_none"]
    assert "no bus stop" in planner.plan(tiny, (35.3, 33.6), (35.12, 33.32), start)["why_none"]


def test_trips_on_the_real_timetable(tt):
    museum = planner.plan(tt, ELEFTHERIA, MUSEUM, NOW)
    assert museum["options"] and "walking all the way" in museum["walking_is_quicker"]
    hospital = planner.plan(tt, ELEFTHERIA, HOSPITAL, NOW)
    best = hospital["options"][0]
    assert best["changes"] == 0 and best["lines"] and not hospital.get("walking_is_quicker")
    ride = next(leg for leg in best["legs"] if leg["mode"] == "bus")
    assert ride["departs"] >= "07:26" and ride["stops"] > 3 and ride["to"].startswith(("Kalamon", "Nicosia General"))


# -- the live feed ----------------------------------------------------------------------------------


def test_the_live_feed_joins_the_timetable(tt):
    data = (FIXTURES / "gtfs_rt_2026-11-26.pb").read_bytes()
    calls = []
    fleet = rt_mod.Fleet(lambda url: calls.append(url) or data, lambda: tt, now=lambda: NOW)
    snap = fleet.snapshot()
    assert snap.status == "live" and len(snap.vehicles) == 29
    assert {b["line"] for b in snap.vehicles} == {"3", "41", "42", "GSP1", "26", "L1"}  # not the other operator's bus
    bus = next(b for b in snap.vehicles if b["id"] == "bus-4660")
    assert bus["line_id"] == "line-npt-26" and bus["headsign"] == "Nicosia General Hospital"
    assert bus["along_m"] > 0 and bus["next_stops"] and bus["delay_source"] == "feed"
    assert bus["next_stops"][0]["id"].startswith("stop-")
    waiting = next(b for b in snap.vehicles if b["id"] == "bus-5067")
    assert waiting["waiting"] == "08:00"
    assert fleet.snapshot() is snap and len(calls) == 1  # one fetch serves everyone for 30 s


def test_a_failing_feed_keeps_its_last_copy_then_says_so(tt):
    data = (FIXTURES / "gtfs_rt_2026-11-26.pb").read_bytes()
    clock = {"t": NOW}
    answer = {"data": data}

    def fetch(url):
        if isinstance(answer["data"], Exception):
            raise answer["data"]
        return answer["data"]
    fleet = rt_mod.Fleet(fetch, lambda: tt, now=lambda: clock["t"])
    assert fleet.snapshot().status == "live"
    answer["data"] = TimeoutError("timed out")
    clock["t"] = NOW + timedelta(seconds=40)
    stale = fleet.snapshot()
    assert stale.status == "stale" and len(stale.vehicles) == 29 and "timed out" in stale.error
    assert stale.fetched_at == NOW.timestamp()  # says when its buses were really seen
    clock["t"] = NOW + timedelta(minutes=10)
    gone = fleet.snapshot()
    assert gone.status == "unavailable" and not gone.vehicles
    answer["data"] = b"<html>not a feed</html>"
    clock["t"] = NOW + timedelta(minutes=11)
    assert fleet.snapshot().status == "unavailable"


def test_delay_words():
    assert delay_text(None) is None and delay_text(20) == "on time"
    assert delay_text(200) == "3 min late" and delay_text(-130) == "2 min early"


def test_departures_carry_the_live_estimate(tt):
    fleet = rt_mod.Fleet(lambda url: (FIXTURES / "gtfs_rt_2026-11-26.pb").read_bytes(), lambda: tt,
                         now=lambda: NOW)
    snap = fleet.snapshot()
    deps = planner.departures(tt, [tt.stop("1")], NOW, 40, 10, snap=snap)
    live = [d for d in deps if d["realtime"]]
    assert live and all(d["expected"] for d in live) and any(d["bus"] for d in live)
    assert all(d["in_min"] >= 0 for d in deps)


# -- road speeds ----------------------------------------------------------------------------------


def test_bluetooth_inventories_parse():
    paths = parse_paths(load("nap_paths.xml"))
    name, coords = paths["1"]
    assert name == "1004->1008" and len(coords) > 10
    assert 33.33 < coords[0][0] < 33.35 and 35.16 < coords[0][1] < 35.17  # lon, lat
    sites = parse_sites(load("nap_sites.xml"))
    assert sites["1004"]["name"] == "Griva - Prokopiou" and sites["1004"]["lat"] == pytest.approx(35.161034)
    assert road_name("Griva - Prokopiou", "Prodromou 2 - Griva") == "Griva"
    assert road_name("Kalamon roundabout", "Athalassas - Lemessou") is None


def roads(tmp_path, now=ROAD_NOW, memory=None, router=None, fetch=None):
    fetch = fetch or FakeFetch()
    r = roads_mod.Roads(lambda url: fetch(url).decode("utf-8"), memory=memory, router=router,
                        now=lambda: now, cache_dir=tmp_path)
    return r, fetch


def test_road_speeds_against_free_flow(tmp_path, monkeypatch):
    r, fetch = roads(tmp_path)
    segs = r.segments()
    assert len(segs) == 8  # the rural path to Deneia leaves Nicosia: not drawn
    first = r.segment("road-1")
    assert first.name == "Griva - Prokopiou to Prodromou 2 - Griva" and first.road == "Griva"
    assert first.short == "Griva" and 900 < first.length_m < 1300
    monkeypatch.setattr(roads_mod.time, "time", lambda: ROAD_NOW.timestamp())
    out = r.speeds()
    assert out["status"] == "live" and out["measured"] == 6  # two report -1: no reading
    row = next(x for x in out["segments"] if x["id"] == "road-1")
    assert row["speed_kmh"] == pytest.approx(33.9, abs=0.1) and row["free_flow"] == "assumed"
    assert row["state"] == band(row["speed_kmh"], 50.0) == "busy" and row["share_of_free_flow"] == 0.68
    assert sum(1 for x in out["segments"] if x["state"] == "no_reading") == 2
    r.speeds()
    assert fetch.count(LIVE_URL) == 1  # every page and tool shares one fetch per 5 minutes


def test_free_flow_estimated_from_the_road_network(tmp_path, monkeypatch):
    router = StubRouter(kmh=40.0)
    r, _ = roads(tmp_path, router=router)
    monkeypatch.setattr(roads_mod.time, "sleep", lambda s: None)
    segs = r.segments()
    r._reference = {}
    r._fill_references(segs)
    assert router.calls == len(segs)
    free, how = r.free_flow(r.segment("road-1"))
    assert how == "estimated" and free == pytest.approx(40.0, rel=0.05)
    # A route that went round a block still gives the network's speed there.
    r2, _ = roads(tmp_path / "b", router=StubRouter(kmh=36.0, detour=3.0))
    r2.segments()
    r2._reference = {}
    r2._fill_references(r2.segments())
    assert r2.free_flow(r2.segment("road-1")) == (pytest.approx(36.0, rel=0.05), "estimated")
    assert json.loads((tmp_path / "road_reference.json").read_text())["1"]["kmh"]


def test_crawls_and_old_readings_are_no_reading(tmp_path, monkeypatch):
    rows = parse.parse_nap(load("nap_travel_times.xml"))
    slow = rows[0].__class__(**{**rows[0].__dict__, "speed_kmh": 2.0, "travel_time_s": 1900.0})
    r, _ = roads(tmp_path)
    r._live = (ROAD_NOW.timestamp(), {x.segment_id: x for x in [slow, *rows[1:]]}, None)
    monkeypatch.setattr(roads_mod.time, "time", lambda: ROAD_NOW.timestamp())
    first = next(x for x in r.speeds()["segments"] if x["id"] == "road-1")
    assert first["speed_kmh"] is None and first["state"] == "no_reading" and first["travel_time_s"] is None
    later, _ = roads(tmp_path, now=ROAD_NOW + timedelta(hours=2))
    later._live = (ROAD_NOW.timestamp(), {x.segment_id: x for x in rows}, None)
    monkeypatch.setattr(roads_mod.time, "time", lambda: ROAD_NOW.timestamp())
    assert later.speeds()["measured"] == 0


def stored_memory(tmp_path, days=3):
    """A store with `days` of 5-minute travel times on two roads, written by the recorder."""
    mem = Memory(tmp_path / "twin.duckdb")
    base = parse.parse_nap(load("nap_travel_times.xml"))
    rows = []
    for k in range(days * 288):
        t = ROAD_NOW - timedelta(minutes=5 * (k + 1))
        rush = 7 <= t.astimezone(TZ).hour <= 9
        for r in base[:2]:
            speed = 25.0 if rush else 55.0
            rows.append(r.__class__(**{**r.__dict__, "measured_at": t, "speed_kmh": speed,
                                       "travel_time_s": 1078 / (speed / 3.6)}))
    mem.recorder.travel_times(rows)
    assert mem.recorder.flush()
    return mem


def test_the_memory_gives_free_flow_the_usual_and_past_moments(tmp_path, monkeypatch):
    mem = stored_memory(tmp_path, days=8)
    r, _ = roads(tmp_path, memory=mem)
    free, how = r.free_flow(r.segment("road-1"))
    assert how == "measured" and free == pytest.approx(55.0)
    usual = r.usual(["1", "2"], ROAD_NOW)  # Monday 12:30 in Nicosia, against earlier Mondays
    assert usual["1"] == (55.0, 1)
    then = ROAD_NOW - timedelta(days=1)
    past = r.speeds(at=then.replace(hour=6, minute=0))  # 09:00 local: rush hour
    assert past["status"] == "recorded" and past["measured"] == 2
    row = next(x for x in past["segments"] if x["id"] == "road-1")
    assert row["speed_kmh"] == 25.0 and row["state"] == "slow"
    assert r.speeds(at=ROAD_NOW - timedelta(days=30))["status"] == "unavailable"
    assert len(r.series("1", 24)) >= 90  # 15-minute points over the day


def test_the_recorder_keeps_the_travel_times_it_was_handed(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb")
    r, _ = roads(tmp_path, memory=mem)
    r.live()
    assert mem.recorder.pending() == 8
    assert mem.recorder.flush()
    with mem.reading() as con:
        assert con.execute("SELECT count(*) FROM travel_times").fetchone()[0] == 8


def test_moments_are_read_as_local_time():
    assert parse_moment("2026-11-26T06:30", NOW) == datetime(2026, 11, 26, 6, 30, tzinfo=TZ)
    assert parse_moment("1795670000000", NOW).timestamp() == 1795670000
    assert parse_moment(None, NOW) is None
    with pytest.raises(ValueError):
        parse_moment("tomorrow", NOW)
    with pytest.raises(ValueError):
        parse_moment("2026-11-27T08:30", NOW)


# -- entities and search ------------------------------------------------------------------------------


@pytest.fixture
def mob(tmp_path):
    m, _ = make_mobility(tmp_path)
    return m


def test_buses_stops_lines_and_roads_are_entities(mob):
    ents = Entities(None, mobility=mob)
    bus = ents.get("bus-4660")
    assert bus["kind"] == "bus" and bus["name"] == "Bus 26 to Nicosia General Hospital"
    assert bus["state"]["value"] in ("on_time", "late", "early") and bus["next_stops"]
    assert bus["geometry"]["type"] == "LineString" and bus["relations"][0]["id"] == "line-npt-26"
    stop = ents.get("stop-1")
    assert stop["kind"] == "bus_stop" and stop["departures"]
    assert any(r["id"] == "line-ic-42" for r in stop["relations"])
    line = ents.get("line-npt-26")
    assert line["kind"] == "bus_line" and line["running"] and line["geometry"]["type"] in ("LineString", "MultiLineString")
    with pytest.raises(EntityNotFound, match="isn't on the road now"):
        ents.get("bus-99999")


def test_a_road_segment_entity(tmp_path, monkeypatch):
    m, _ = make_mobility(tmp_path, now=ROAD_NOW)
    monkeypatch.setattr(roads_mod.time, "time", lambda: ROAD_NOW.timestamp())
    ents = Entities(None, mobility=m)
    road = ents.get("road-1")
    assert road["kind"] == "road_segment" and road["state"]["value"] == "busy"
    assert road["road"]["free_flow"] == "assumed" and road["readings"][0]["key"] == "speed_kmh"
    assert any(r.get("id") == "road-2" for r in road["relations"])  # the other way


def test_search_finds_stops_lines_and_roads(mob, monkeypatch):
    ents = Entities(None, mobility=mob)
    stops = ents.search("Solomos")
    assert stops[0]["group"] == "Bus stops" and stops[0]["id"].startswith("stop-")
    line = ents.search("26")
    assert line[0]["kind"] == "bus_line" and line[0]["name"] == "Line 26"  # NPT first, then Intercity
    assert ents.search("line 42")[0]["id"] == "line-ic-42"
    assert any(r["kind"] == "road_segment" for r in ents.search("Griva"))


# -- the tools ------------------------------------------------------------------------------------


def ctx_for(m, now=NOW, memory=None, router=None):
    city = SimpleNamespace(now=lambda: now)
    places = SimpleNamespace(ready=False, geocode=lambda text, limit=3: [
        {"name": "Eleftheria Square", "lat": ELEFTHERIA[0], "lon": ELEFTHERIA[1]}] if "eleftheria" in text.lower()
        else [{"name": "Nicosia General Hospital", "lat": HOSPITAL[0], "lon": HOSPITAL[1]}])
    events: list[dict] = []
    ctx = ToolContext(Settings(), city, places, router or StubRouter(), SessionStore(), events.append, "c1",
                      memory=memory, mobility=m)
    return ctx, events


def test_get_buses_draws_them_live(mob):
    ctx, _ = ctx_for(mob)
    res, err = run_tool(ctx, "get_buses", {})
    assert not err and res["running"] >= 20 and res["by_line"][0]["buses"] >= res["by_line"][-1]["buses"]
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["live"] == {"kind": "buses", "lines": None, "near": None}
    assert all(f["properties"]["entity_kind"] == "bus" for f in layer["features"])
    assert ctx.store.datasets[res["data_ref"]]["rows"][0]["id"].startswith("line-")
    res, err = run_tool(ctx, "get_buses", {"lines": ["26"]})
    assert not err and {b["line"] for b in res["buses"]} == {"26"}
    assert ctx.store.layers[res["layer_ref"]]["meta"]["live"]["lines"] == ["line-npt-26"]
    res, err = run_tool(ctx, "get_buses", {"lines": ["999"]})
    assert err and "no bus line 999" in res["error"]


def test_get_buses_says_when_the_feed_is_down(tmp_path):
    fetch = FakeFetch()
    fetch.fail[RT_URL] = TimeoutError("timed out")
    m, _ = make_mobility(tmp_path, fetch=fetch)
    ctx, _ = ctx_for(m)
    res, err = run_tool(ctx, "get_buses", {})
    assert err and "isn't answering" in res["error"]
    res, err = run_tool(ctx, "get_departures", {"stop": "stop-1"})  # the timetable still answers
    assert not err and res["realtime"] == "unavailable, timetable only" and res["stops"][0]["departures"]


def test_get_departures_by_name_id_and_place(mob):
    ctx, _ = ctx_for(mob)
    res, err = run_tool(ctx, "get_departures", {"stop": "Solomos", "minutes": 45})
    assert not err and res["stops"] and res["stops"][0]["id"].startswith("stop-")
    assert any(d.get("expected") for s in res["stops"] for d in s["departures"])
    res, err = run_tool(ctx, "get_departures", {"near_lat": ELEFTHERIA[0], "near_lon": ELEFTHERIA[1], "radius_m": 250})
    assert not err and all(s["id"].startswith("stop-") for s in res["stops"])
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    res, err = run_tool(ctx, "get_departures", {"stop": "Nowhere at all"})
    assert err and "no bus stop is called" in res["error"]


def test_plan_transit_draws_walk_ride_walk(mob):
    router = StubRouter()
    ctx, _ = ctx_for(mob, router=router)
    res, err = run_tool(ctx, "plan_transit", {"from_place": "Eleftheria Square", "to_place": "Nicosia General Hospital"})
    assert not err and res["options"] and res["layer_ref"]
    feats = ctx.store.layers[res["layer_ref"]]["features"]
    modes = [f["properties"].get("mode") for f in feats if f["geometry"]["type"] == "LineString"]
    assert modes[0] == "walk" and "bus" in modes and modes[-1] == "walk"
    numbered = [f["properties"]["n"] for f in feats if f["geometry"]["type"] == "Point"]
    assert numbered == list(range(1, len(numbered) + 1))
    assert router.calls >= 2  # the walking legs come from Valhalla
    assert not any(k.startswith("_") for o in res["options"] for leg in o["legs"] for k in leg)
    res, err = run_tool(ctx, "plan_transit", {"to_place": "Nicosia General Hospital"})
    assert err and "from_place" in res["error"]


def test_get_road_speeds_now_and_at_a_moment(tmp_path, monkeypatch):
    mem = stored_memory(tmp_path)
    m, _ = make_mobility(tmp_path, now=ROAD_NOW, memory=mem)
    monkeypatch.setattr(roads_mod.time, "time", lambda: ROAD_NOW.timestamp())
    ctx, _ = ctx_for(m, now=ROAD_NOW, memory=mem)
    res, err = run_tool(ctx, "get_road_speeds", {})
    assert not err and res["source"] == "live" and res["measured"] == 6
    shares = [r["share_of_free_flow"] for r in res["slowest"]]
    assert shares == sorted(shares)
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["live"] == {"kind": "roads", "at": None} and layer["meta"]["geometry"] == "route"
    res, err = run_tool(ctx, "get_road_speeds", {"road": "Griva"})
    assert not err and all("Griva" in r["name"] for r in res["slowest"])
    res, err = run_tool(ctx, "get_road_speeds", {"at": "2026-10-18T09:00"})
    assert not err and res["source"] == "the twin's memory" and res["measured"] == 2
    assert ctx.store.layers[res["layer_ref"]]["meta"]["live"]["at"].startswith("2026-10-18T09:00")
    res, err = run_tool(ctx, "get_road_speeds", {"at": "2026-12-01T09:00"})
    assert err and "future" in res["error"]
    res, err = run_tool(ctx, "get_road_speeds", {"road": "Nowhere"})
    assert err and "roads include" in res["error"]


# -- the endpoints and the strip ----------------------------------------------------------------------


@pytest.fixture
def app_agent(tmp_path, monkeypatch):
    m, _ = make_mobility(tmp_path)
    agent = SimpleNamespace(settings=Settings(), city=SimpleNamespace(now=lambda: NOW), mobility=m)
    monkeypatch.setattr(server.app.state, "agent", agent, raising=False)
    overview._cache.clear()
    return agent


REQUEST = SimpleNamespace(app=server.app)


def test_bus_and_stop_endpoints(app_agent):
    body = json.loads(mobility_routes.buses(REQUEST, lines=None).body)
    assert body["status"] == "live" and body["timetable"] == "ready" and body["count"] == 29
    bus = next(b for b in body["buses"] if b["id"] == "bus-4660")
    assert {"line", "color", "heading", "along_m", "shape_id", "reported"} <= set(bus)
    only = json.loads(mobility_routes.buses(REQUEST, lines="line-npt-26").body)
    assert {b["line"] for b in only["buses"]} == {"26"}
    shape = json.loads(mobility_routes.shape(bus["shape_id"], REQUEST).body)
    assert len(shape["coordinates"]) > 10
    with pytest.raises(HTTPException):
        mobility_routes.shape("nope", REQUEST)
    stops = json.loads(mobility_routes.stops(REQUEST).body)
    assert stops["features"] and stops["features"][0]["properties"]["id"].startswith("stop-")


def test_road_endpoint_now_and_at(app_agent, monkeypatch):
    monkeypatch.setattr(roads_mod.time, "time", lambda: ROAD_NOW.timestamp())
    app_agent.mobility.roads.now = lambda: ROAD_NOW
    app_agent.city = SimpleNamespace(now=lambda: ROAD_NOW)
    body = json.loads(mobility_routes.roads(REQUEST, at=None).body)
    assert body["meta"]["status"] == "live" and len(body["features"]) == 8
    assert body["meta"]["legend"]["Very slow"] == "#ff9a71"
    with pytest.raises(HTTPException) as e:
        mobility_routes.roads(REQUEST, at="soon")
    assert e.value.status_code == 400
    past = json.loads(mobility_routes.roads(REQUEST, at="2026-10-18T09:00").body)
    assert past["meta"]["status"] == "unavailable"  # this agent's memory is off


def test_the_strip_and_the_workspace_summary(app_agent, monkeypatch):
    agent = app_agent
    agent.city = SimpleNamespace(now=lambda: NOW, overview=lambda: {}, traffic_cameras=lambda: [])
    agent.settings = Settings()
    agent.registers = SimpleNamespace(ready=False)
    monkeypatch.setattr(overview, "weather", lambda s: None)
    assert "buses" not in overview.live_strip(agent)  # the strip never waits for the feeds
    agent.mobility.summary()  # what the background refresh does
    strip = overview.live_strip(agent)
    assert strip["buses"]["running"] >= 20 and strip["buses"]["lines"] == 6
    summary = json.loads(mobility_routes.mobility(REQUEST).body)
    assert summary["buses"]["by_line"][0]["buses"] >= 1 and "roads" in summary

"""What's here: the everyday sections of a spot (chatbot/here.py), each read from what the twin
already holds, and the endpoint and get_entity that serve them. Offline, on a captured Nominatim
answer (26 Nov 2026), the GTFS subsets and live feed of 26 Nov, the parking and lamp captures of
19 Oct, test_memory's four synthetic weeks of the twin's memory, and the fake DLS and registers."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from chatbot import city as city_mod
from chatbot import geo as geo_mod
from chatbot import here
from chatbot.city import CityData
from chatbot.entities import Entities
from chatbot.environment.spot import spot_id
from chatbot.moments import Moments
from chatbot.routes import here as here_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from chatbot.web import WebError

from conftest import load, ms, utc
from test_dls_eoa import LAT, LON, FakeDLS, make_registry
from test_entities import asgi_get, make_places
from test_memory import START, fake_city, memory  # noqa: F401 - fixtures
from test_mobility import ELEFTHERIA, NOW as BUS_NOW, make_mobility

FRI_2000 = utc(2026, 11, 20, 18, 0)  # 20:00 in Nicosia: the memory's bays are busy, it is dark
WEEK_BEFORE = FRI_2000 - timedelta(days=7)  # a usual Friday: free again from 19:00
NEAR_YPA = (35.1661, 33.3641)  # the memory's two bays YPA and YPB are 14 m away; YL1 90 m
DAY = utc(2026, 10, 19, 10, 0)  # 13:00 in Nicosia, minutes after the parking capture
NIGHT = utc(2026, 10, 19, 17, 0)  # 20:00 in Nicosia
ONASAGOROU = (35.17191, 33.36191)
ALADDIN = {"id": "n6213640486", "category": "restaurant", "name": "Aladdin Lahmajoun",
           "lat": 35.171866, "lon": 33.361946}


@pytest.fixture(autouse=True)
def fresh_caches():
    here._cache.clear()
    here._reverse_cache.clear()
    yield
    here._cache.clear()
    here._reverse_cache.clear()


def at_time(records, newest):
    """A capture's records moved so the newest is at `newest` (all of them at once, so a
    lamp's level is as recent as its heartbeat)."""
    return [{**r, "timestamp": str(ms(newest))} for r in records]


def with_status(records, status, newest):
    return [{**r, "value": status} if r["key"] == "status" else r for r in at_time(records, newest)]


def live_city(api, client, tmp_path, monkeypatch, now, latest):
    """CityData on the 19 Oct inventory, answering latest telemetry from `latest`."""
    monkeypatch.setattr(city_mod, "CACHE_DIR", tmp_path)
    api.on("getTenantDevices", load("devices_2026-10-19.json"))
    api.on("getLatestTelemetry", lambda p: latest.get(p["entityName"], []))
    api.on("getLatestAttribute", [])
    c = CityData(Settings(), client=client, now=lambda: now)
    c.devices()
    return c


# -- address -------------------------------------------------------------------------------------


def nominatim(monkeypatch, answer):
    calls = []

    def fake(url, **kw):
        calls.append((url, kw.get("params")))
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(geo_mod, "get_json", fake)
    return calls


def test_the_address_comes_from_nominatim_once_per_ten_metres(tmp_path, monkeypatch):
    calls = nominatim(monkeypatch, load("nominatim_reverse_onasagorou.json"))
    ents = Entities(None, make_places(tmp_path, monkeypatch, [ALADDIN]))
    s = here.build(ents, "address", *ONASAGOROU)
    assert s["status"] == "ok" and s["summary"] == "Onasagorou"
    assert s["detail"] == "Faneromeni, Nicosia, 1011"
    facts = {f["label"]: f["value"] for f in s["facts"]}
    assert facts == {"Street": "Onasagorou", "Building or place": "Aladdin Lahmajoun",
                     "Area": "Faneromeni", "Municipality": "Nicosia", "Postcode": "1011"}
    assert s["rows"][0]["id"] == "n6213640486" and s["rows"][0]["kind"] == "place"
    assert s["sources"][0]["name"] == "Nominatim (OpenStreetMap)"
    assert calls[0][0].endswith("/reverse") and calls[0][1]["zoom"] == 18
    # A click a couple of metres away reads the same address without asking again.
    again = here.build(ents, "address", ONASAGOROU[0] + 0.00002, ONASAGOROU[1] + 0.00001)
    assert again["summary"] == "Onasagorou" and len(calls) == 1


def test_without_nominatim_the_nearest_place_stands_in_and_far_off_it_fails(tmp_path, monkeypatch):
    calls = nominatim(monkeypatch, WebError("nominatim.openstreetmap.org unreachable"))
    ents = Entities(None, make_places(tmp_path, monkeypatch, [ALADDIN]))
    s = here.build(ents, "address", *ONASAGOROU)
    assert s["status"] == "unknown" and s["summary"] == "Near Aladdin Lahmajoun"
    assert "try again" in s["message"]
    far = here.build(ents, "address", 35.1500, 33.4000)
    assert far["status"] == "error" and "try again" in far["message"] and far["rows"] == []
    # A failure isn't kept for a day: the next click asks again.
    here._cache.clear()
    here.build(ents, "address", *ONASAGOROU)
    assert len(calls) == 3


# -- buses ---------------------------------------------------------------------------------------


def test_buses_the_nearest_stops_with_their_next_departures(tmp_path):
    m, _ = make_mobility(tmp_path)
    ents = Entities(None, mobility=m)
    s = here.build(ents, "buses", *ELEFTHERIA)
    assert s["status"] == "ok" and 1 <= len(s["rows"]) <= 2
    assert all(r["id"].startswith("stop-") and r["kind"] == "bus_stop" and r["distance_m"] <= 400
               for r in s["rows"])
    assert s["rows"] == sorted(s["rows"], key=lambda r: r["distance_m"])
    deps = [d for r in s["rows"] for d in r["departures"]]
    assert deps and all(0 <= d["in_min"] <= 60 for d in deps)
    assert all(len(r["departures"]) <= 3 for r in s["rows"])
    assert s["summary"].startswith("Next bus: ") and " from " in s["summary"]
    assert [x["name"] for x in s["sources"]] == ["Bus timetables (GTFS)", "Live bus positions (GTFS-RT)"]
    assert s["message"] is None  # the live feed answered


def test_buses_at_a_past_moment_list_the_stops_and_say_departures_are_live_only(tmp_path):
    m, _ = make_mobility(tmp_path)
    ents = Entities(None, mobility=m)
    s = here.build(ents, "buses", *ELEFTHERIA, at=BUS_NOW - timedelta(hours=3))
    assert s["status"] == "ok" and s["live_only"] and s["rows"]
    assert not any("departures" in r for r in s["rows"])
    assert "only live" in s["message"]


def test_buses_while_the_timetable_loads_and_when_the_feed_is_down(tmp_path):
    from chatbot.mobility import Mobility
    from chatbot.mobility.realtime import RT_URL

    loading = Mobility(Settings(), fetch=lambda url: (_ for _ in ()).throw(WebError("slow")),
                       cache_dir=tmp_path / "empty")
    loading.source._lock.acquire()  # a load under way
    s = here.build(Entities(None, mobility=loading), "buses", *ELEFTHERIA)
    assert s["status"] == "unknown" and "still loading" in s["message"]
    here._cache.clear()  # (a live "unknown" is kept for seconds only)
    m, fetch = make_mobility(tmp_path)
    fetch.fail[RT_URL] = WebError("the live feed is down")
    down = here.build(Entities(None, mobility=m), "buses", *ELEFTHERIA)
    assert down["status"] == "ok" and "timetable times" in down["message"]
    assert [x["name"] for x in down["sources"]] == ["Bus timetables (GTFS)"]


# -- parking -------------------------------------------------------------------------------------

BAY = load("latest_parking_YP974.json")
SPOT_BAYS = (35.1658, 33.3625)  # YP831 64 m, YP972 75 m, YP1157 78 m, YP886 85 m; YP974 222 m


def test_parking_counts_free_bays_from_the_last_snapshot_without_a_request(api, client, tmp_path, monkeypatch):
    latest = {"YP972": with_status(BAY, "0", DAY - timedelta(minutes=20)),
              "YP886": with_status(BAY, "0", DAY - timedelta(minutes=5)),
              "YP831": with_status(BAY, "1", DAY - timedelta(minutes=30)),
              "YP1157": with_status(BAY, "0", DAY - timedelta(days=3)),  # gone quiet
              "YP974": with_status(BAY, "0", DAY - timedelta(minutes=5))}  # too far
    city = live_city(api, client, tmp_path, monkeypatch, DAY, latest)
    city.states("Parking")  # the live strip's fetch
    asked = api.count("getLatestTelemetry")
    s = here.build(Entities(city), "parking", *SPOT_BAYS)
    assert api.count("getLatestTelemetry") == asked  # a click costs the city's API nothing
    assert s["status"] == "ok" and s["summary"] == "2 of 3 bays free within 200 m"
    assert [r["id"] for r in s["rows"]] == ["YP972", "YP886"]
    assert all(r["kind"] == "parking_sensor" and r["state"] == "free" for r in s["rows"])
    facts = {f["label"]: f["value"] for f in s["facts"]}
    # Every other bay of the inventory around here answered nothing: no recent reading.
    assert facts["Free"] == 2 and facts["Taken"] == 1 and facts["No recent reading"] > 1
    assert s["sources"][0]["name"] == "Smart Nicosia platform" and s["as_of"].startswith("2026-10-19T12:55")


def test_parking_before_the_first_snapshot_says_so(api, client, tmp_path, monkeypatch):
    city = live_city(api, client, tmp_path, monkeypatch, DAY, {})
    s = here.build(Entities(city), "parking", *SPOT_BAYS)
    assert s["status"] == "error" and "haven't arrived yet" in s["message"]
    assert api.count("getLatestTelemetry") == 0


def test_parking_and_lamps_at_a_past_moment_read_the_memory(fake_city, memory, api):  # noqa: F811
    ents = Entities(fake_city, memory=memory)
    ents.moments = Moments(fake_city, memory)
    busy = here.build(ents, "parking", *NEAR_YPA, at=FRI_2000)
    assert busy["status"] == "ok" and busy["summary"] == "All 2 bays within 200 m are taken"
    assert busy["sources"][0]["name"] == "The twin's memory"
    assert "Fri 20 Nov 2026 20:00" in busy["sources"][0]["detail"]
    usual = here.build(ents, "parking", *NEAR_YPA, at=WEEK_BEFORE)
    assert usual["summary"] == "2 of 2 bays free within 200 m"
    assert sorted(r["id"] for r in usual["rows"]) == ["YPA", "YPB"]
    before = here.build(ents, "parking", *NEAR_YPA, at=START - timedelta(hours=3))
    assert before["status"] == "unknown" and "holds nothing" in before["message"]
    # It is dark at 20:00, and the memory holds nothing of the lamp YL1: unknown, not a guess.
    lamps = here.build(ents, "lamps", *NEAR_YPA, at=FRI_2000)
    assert lamps["status"] == "unknown" and lamps["night"] and "holds nothing" in lamps["message"]
    assert not [c for c in api.calls if c[0] in ("getLatestTelemetry", "getTelemetryRange")]


# -- lamps ---------------------------------------------------------------------------------------

LAMP = load("latest_lamp_YL1015.json")


def test_lamps_after_dark_count_the_lit_ones_and_name_the_dark(api, client, tmp_path, monkeypatch):
    recent = at_time(LAMP, NIGHT - timedelta(minutes=10))
    latest = {"YL1015": recent, "YL1004": recent, "YL1005": recent,
              "YL1597": at_time(LAMP, NIGHT - timedelta(hours=3))}  # silent since before sunset
    city = live_city(api, client, tmp_path, monkeypatch, NIGHT, latest)
    city.states("Lighting")
    s = here.build(Entities(city), "lamps", 35.166797, 33.36082)
    # The other lamps of the inventory here sent nothing: counted apart, not as dark.
    assert s["status"] == "ok" and s["night"] and s["summary"] == "3 of 4 lamps lit within 150 m"
    assert [(r["id"], r["state"], r["detail"]) for r in s["rows"]] == [("YL1597", "offline", "Offline at night")]
    facts = {f["label"]: f["value"] for f in s["facts"]}
    assert facts["Lit"] == 3 and facts["Offline"] == 1 and facts["No reading tonight"] > 1


def test_lamps_by_day_say_when_they_come_on(api, client, tmp_path, monkeypatch):
    city = live_city(api, client, tmp_path, monkeypatch, DAY, {})
    s = here.build(Entities(city), "lamps", 35.166797, 33.36082)
    assert s["status"] == "none" and not s["night"] and s["summary"] == "Daylight: street lights are off"
    assert s["message"] == "They come on after sunset, at 18:08."  # 19 Oct, summer time
    assert api.count("getLatestTelemetry") == 0


# -- registers and the building ------------------------------------------------------------------


def test_registers_around_and_the_building_under_the_pin(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    ents = Entities(None, None, reg.dls, reg)
    r = here.build(ents, "registers", LAT, LON)  # NIC-2 is 182 m east, outside
    assert r["status"] == "ok" and r["summary"] == "1 dangerous within 150 m"
    assert [(x["id"], x["kind"], x["detail"]) for x in r["rows"]] == [("NIC-1", "register_building", "Dangerous")]
    assert "not an engineering verdict" in r["note"]
    b = here.build(ents, "building", LAT, LON)
    assert b["status"] == "ok" and b["under"] and b["summary"] == ("Under the pin: a high residential "
                                                                   "building, 21 m to the roof, 7 floors")
    assert [x["id"] for x in b["rows"]] == ["B1", "NIC-1"]  # and its register entry
    assert {f["label"]: f["value"] for f in b["facts"]}["Floors"] == 7
    beside = here.build(ents, "building", LAT + 16 / 110_574, LON)  # 10 m off its north wall
    assert not beside["under"] and beside["distance_m"] == 10
    assert beside["summary"].startswith("10 m away: ")
    empty = here.build(ents, "building", LAT - 0.002, LON)
    assert empty["status"] == "none" and empty["summary"] == "Open ground: no building here"
    gone = here.build(ents, "registers", 35.10, 33.30)
    assert gone["status"] == "none" and gone["rows"] == []


class DownDLS(FakeDLS):
    def query(self, layer_url, params):
        raise WebError("eservices.dls.moi.gov.cy unreachable")


def test_a_section_that_fails_says_so_and_leaves_the_others(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    ents = Entities(None, None, DownDLS(tmp_path / "down"), reg)
    secs = here.sections(ents, LAT, LON)
    assert [s["key"] for s in secs] == list(here.SECTIONS)
    by = {s["key"]: s for s in secs}
    assert by["building"]["status"] == "error" and "didn't answer" in by["building"]["message"]
    assert by["registers"]["status"] == "ok"
    assert by["address"]["status"] == "unknown" and by["buses"]["status"] == "unknown"  # not connected
    # A section that raises something unexpected is an error with a plain message, not a 500.
    monkeypatch.setitem(here.BUILDERS, "registers", lambda s: 1 / 0)
    here._cache.clear()
    boom = here.build(ents, "registers", LAT, LON)
    assert boom["status"] == "error" and boom["message"].endswith("Try again in a moment.")


# -- the endpoint and the assistant --------------------------------------------------------------


class FakeEnvironment:
    """The environment twin's readout at a point (chatbot/environment/service.py), fixed."""

    def __init__(self):
        self.asked = []

    def spot(self, lat, lon, at):
        self.asked.append(at)
        return {"lat": lat, "lon": lon, "at": None, "air": [], "temperature": None, "wind": None,
                "sun": {"altitude_deg": 31.0, "bearing_deg": 200, "from": "south", "up": True,
                        "shade": False, "on_roof": False, "hours_today": 6.5,
                        "times": {"sunrise": "06:32", "sunset": "16:36"}}}


def test_the_endpoint_serves_one_section_or_all(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(here_routes.router)
    app.state.agent = SimpleNamespace(entities=Entities(None, None, reg.dls, reg),
                                      city=SimpleNamespace(now=lambda: DAY))
    sid = spot_id(LAT, LON)
    status, body = asgi_get(app, f"/api/entities/{sid}/here", {"section": "registers"})
    assert status == 200 and body["id"] == sid and body["at"] is None
    assert [s["key"] for s in body["sections"]] == ["registers"]
    status, body = asgi_get(app, f"/api/entities/{sid}/here")
    assert status == 200 and [s["key"] for s in body["sections"]] == list(here.SECTIONS)
    status, body = asgi_get(app, f"/api/entities/{sid}/here", {"section": "parking", "at": "2026-10-19T09:00"})
    assert status == 200 and body["at"] == "2026-10-19T09:00"
    assert body["sections"][0]["status"] == "error"  # no memory in this app: said, not guessed
    assert asgi_get(app, f"/api/entities/{sid}/here", {"section": "weather"})[0] == 404
    assert asgi_get(app, "/api/entities/YL1015/here")[0] == 404
    assert asgi_get(app, f"/api/entities/{sid}/here", {"at": "2030-01-01T00:00"})[0] == 400


def test_get_entity_on_a_spot_answers_from_the_same_sections(tmp_path, monkeypatch, fake_city, memory):  # noqa: F811
    reg = make_registry(tmp_path, monkeypatch)
    ents = Entities(fake_city, None, reg.dls, reg, memory)
    ents.environment = FakeEnvironment()
    ents.moments = Moments(fake_city, memory)
    ctx = ToolContext(Settings(), fake_city, None, None, SessionStore(), [].append, "c1", dls=reg.dls,
                      registers=reg, memory=memory, entities=ents, moments=ents.moments)
    out, err = run_tool(ctx, "get_entity", {"id": spot_id(LAT, LON)})
    assert not err and out["kind"] == "spot" and set(out["here"]) == set(here.SECTIONS)
    assert out["here"]["registers"]["rows"][0]["id"] == "NIC-1"
    assert out["here"]["building"]["facts"]["Height to the roof"] == "21 m"
    assert out["here"]["buses"]["status"] == "unknown"  # no buses in this app, and it says so
    near = [r for r in out["relations"] if r["relation"] == "Register building nearby"]
    assert near and near[0]["id"] == "NIC-1"
    layer = ctx.store.layers[out["layer_ref"]]
    assert {"NIC-1", "B1"} <= {f["properties"]["id"] for f in layer["features"]}
    assert "DLS 3D buildings (INSPIRE)" in " ".join(out["sources"])
    # At a past moment of the clock (city_clock in <context>), parking reads the twin's memory.
    sid = spot_id(*NEAR_YPA)
    then, err = run_tool(ctx, "get_entity", {"id": sid, "at": "2026-11-20T20:00"})
    assert not err and then["here"]["parking"]["summary"] == "All 2 bays within 200 m are taken"
    assert then["here"]["lamps"]["status"] == "unknown"
    assert ents.environment.asked[-1] is not None  # the environment was read for then too
    _, err = run_tool(ctx, "get_entity", {"id": sid, "at": "2031-01-01T00:00"})
    assert err and "future" in str(_)

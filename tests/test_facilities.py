"""Park, charge and hire: the national inventories of car parks, park and ride, EV chargers,
Nextbike stations, Bolt scooter zones and bicycle racks, Nextbike's live bikes, their entity
kinds, the tools and the endpoints. All offline, on real slices of each feed as published on
26 Nov 2026 (tests/fixtures/nap_facilities) and the parking sensor inventory of 19 Oct."""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chatbot import overview, server
from chatbot import sources as data_sources
from chatbot.entities import Entities
from chatbot.mobility import facilities as F
from chatbot.mobility import facility_kinds as K
from chatbot.routes import facilities as routes
from chatbot.routes import sources as source_routes
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from chatbot.settings import Settings
from collector import parse

from conftest import FIXTURES, load
from test_mobility import NOW, FakeFetch, StubRouter, make_mobility

FX = FIXTURES / "nap_facilities"
PROMITHEOS = (35.16535, 33.35918)  # car park 31, with sensor bays beside it
UNIVERSITY = (35.1442, 33.4093)  # the University of Cyprus's old campus area (Panepistimiou)
OLD_TOWN = (35.1725, 33.3625)


def feed_files() -> dict[str, bytes]:
    files = {f.url: (FX / f"{f.dataset}.xml").read_bytes() for f in [*F.FEEDS.values(), F.RENTAL]}
    files[F.CATALOGUE_URL] = (FX / "ckan_package_search.json").read_bytes()
    for name in ("station_status", "station_information", "vehicle_types"):
        files[F.GBFS.format(name)] = (FX / f"gbfs_{name}.json").read_bytes()
    return files


class Fetch:
    """The feeds as text, from fixtures; `fail` makes a URL raise. Counts what was asked."""

    def __init__(self):
        self.files = feed_files()
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        return self.files[url].decode("utf-8")

    def count(self, url: str) -> int:
        return self.calls.count(url)


def make(tmp_path, fetch=None, **kw):
    fetch = fetch or Fetch()
    return F.Facilities(fetch, cache_dir=tmp_path, background=False, **kw), fetch


# -- parsing ----------------------------------------------------------------------------------------


def test_every_feed_parses_by_local_names(tmp_path):
    fac, fetch = make(tmp_path)
    counts = {k: len(fac.inventory(k).items) for k in F.KINDS}
    assert counts == {"car_park": 15, "park_and_ride": 2, "charger": 14, "bike_station": 22,
                      "scooter_zone": 16, "bike_rack": 26}
    # One request per feed, whatever asked (the rental list once, for the stations' other names).
    for feed in [*F.FEEDS.values(), F.RENTAL]:
        assert fetch.count(feed.url) == 1
    assert fetch.count(F.CATALOGUE_URL) == 1


def test_car_parks_say_which_have_no_name(tmp_path):
    fac, _ = make(tmp_path)
    by = {f.id: f for f in fac.inventory("car_park").items}
    f = by["carpark-1"]
    assert (f.name, f.name_el, f.named) == ("Faneromenis (Municipal)", "Φανερωμένης (Δήμου)", True)
    assert (round(f.lat, 4), round(f.lon, 4)) == (35.1733, 33.3639)
    osm = by["carpark-550"]
    assert osm.name == "Car park" and not osm.named and osm.name_el is None and osm.info["from_osm"]
    assert sum(1 for f in by.values() if f.in_nicosia) == 14


def test_park_and_ride_is_a_polygon_with_its_middle(tmp_path):
    fac, _ = make(tmp_path)
    gsp = fac.get("pnr-0")
    assert gsp.name == "GSP Stadium Park and Ride" and gsp.name_el.startswith("Χώρος")
    assert len(gsp.rings) == 2 and all(r[0] == r[-1] for r in gsp.rings)
    assert gsp.in_nicosia and 35.10 < gsp.lat < 35.13


def test_chargers_keep_power_connectors_and_the_listed_status(tmp_path):
    fac, _ = make(tmp_path)
    by = {f.name: f for f in fac.inventory("charger").items}
    fast = by["Petrolina ENI Leof Lemesou Station (50kW)"]
    assert fast.id.startswith("charger-") and len(fast.id) == len("charger-") + 8
    assert fast.info["kw"] == 50 and fast.info["speed"] == "fast" and fast.info["power_type"] == "dc"
    assert fast.info["connector_types"] == ["type2", "comboType2"] and fast.info["status"] == "operational"
    # The listing calls it fast charging, but one connector gives 22 kW: standard.
    bmw = by["BMW HQ Nicosia (150kW)"]
    assert bmw.info["mode"] == "fastCharging" and bmw.info["kw"] == 22 and bmw.info["speed"] == "standard"
    assert by["EOA Nicosia 8 (22kW)"].info["status"] == "unavailable"
    # Named only by a code: shown by who runs it and where, the code kept.
    evp = by["EV Power, 360 Building"]
    assert evp.info["code"] == "CY*EVP*ENIC0022" and evp.info["kw"] == 200 and evp.info["speed"] == "fast"
    # Ids are stable: the same name gives the same id on every read.
    again, _ = make(tmp_path / "again")
    assert {f.id for f in again.inventory("charger").items} == {f.id for f in by.values()}


def test_bolt_zones_drop_their_code_and_racks_drop_tests(tmp_path):
    fac, _ = make(tmp_path)
    zones = fac.inventory("scooter_zone").items
    rig = next(z for z in zones if z.id == "bolt-dac81462")
    assert rig.name == "Rigenis" and rig.capacity == 6 and rig.rings and rig.in_nicosia
    assert not any(z.name[0].isdigit() for z in zones)
    racks = fac.inventory("bike_rack")
    assert racks.total == 28 and racks.skipped == 2 and not any(r.name.lower() == "test" for r in racks.items)
    museum = next(r for r in racks.items if r.id == "rack-0")
    assert museum.name == "Archaeological Museum" and museum.address == "Lefkosia"


def test_nextbike_stations_fold_in_the_rental_list(tmp_path):
    fac, _ = make(tmp_path)
    by = {f.name: f for f in fac.inventory("bike_station").items}
    assert by["Cyprus Museum"].info["also_listed"] == ["Museum Station"]
    assert "Old Port" in by  # shouted names are read in title case
    assert by["Kipos Eirinis"].capacity == 5


def test_a_broken_or_odd_record_is_skipped_not_guessed():
    xml = """<?xml version='1.0'?><SharedMobilityPublication xmlns="http://datex2.eu/schema/3/sharedMobility"
      xmlns:c="http://datex2.eu/schema/3/common" xmlns:l="http://datex2.eu/schema/3/locationReferencing">
      <sharedMobility><c:versionedIdentifier><c:identifier>1</c:identifier></c:versionedIdentifier>
        <sharedMobilityFacilityName><c:value>Swapped</c:value><c:lang>en</c:lang></sharedMobilityFacilityName>
        <capacity>five</capacity>
        <l:location><l:pointByCoordinates><l:pointCoordinates><c:latitude>33.36</c:latitude>
        <c:longitude>35.17</c:longitude></l:pointCoordinates></l:pointByCoordinates></l:location></sharedMobility>
      <sharedMobility><c:versionedIdentifier><c:identifier>2</c:identifier></c:versionedIdentifier>
        <l:location><l:pointByCoordinates><l:pointCoordinates><c:latitude>0</c:latitude>
        <c:longitude>0</c:longitude></l:pointCoordinates></l:pointByCoordinates></l:location></sharedMobility>
      <sharedMobility><c:versionedIdentifier><c:identifier>3</c:identifier></c:versionedIdentifier></sharedMobility>
    </SharedMobilityPublication>"""
    items, total, skipped, _ = F.parse_bike_stations(xml)
    assert total == 3 and skipped == 2 and len(items) == 1
    f = items[0]
    assert (f.lon, f.lat) == (33.36, 35.17) and f.capacity is None  # swapped back; a word isn't a number
    with pytest.raises(F.FeedBroken):
        F.parse_chargers("<html>Service unavailable")


# -- caching and refreshing ------------------------------------------------------------------------


def test_a_day_old_copy_is_read_from_disk_then_refreshed(tmp_path):
    fac, fetch = make(tmp_path)
    fac.inventory("bike_rack")
    second, fetch2 = make(tmp_path)  # a restart: the copy on disk, no request
    assert len(second.inventory("bike_rack").items) == 26 and fetch2.count(F.FEEDS["bike_rack"].url) == 0
    path = tmp_path / "nap_facilities" / "bicycle_racks.xml"
    old = time.time() - F.INVENTORY_S - 60
    os.utime(path, (old, old))
    third, fetch3 = make(tmp_path)
    inv = third.inventory("bike_rack")
    assert fetch3.count(F.FEEDS["bike_rack"].url) == 1 and inv.error is None


def test_a_failed_refresh_keeps_yesterday_and_says_so(tmp_path):
    fac, fetch = make(tmp_path)
    fac.inventory("charger")
    fetch.fail[F.FEEDS["charger"].url] = TimeoutError("timed out")
    inv = fac.refresh("charger")
    assert len(inv.items) == 14 and "didn't answer" in inv.error
    # A copy that doesn't parse is never kept, on disk or in memory.
    fetch.fail.clear()
    fetch.files[F.FEEDS["charger"].url] = b"<d2LogicalModel/>"
    fac._retry_at.clear()
    inv = fac.refresh("charger")
    assert len(inv.items) == 14
    assert "chargingPoint" in (tmp_path / "nap_facilities" / "electric_vehicle_chargers.xml").read_text(encoding="utf-8")


def test_no_copy_and_no_feed_is_an_error_for_that_kind_only(tmp_path):
    fetch = Fetch()
    fetch.fail[F.FEEDS["scooter_zone"].url] = ConnectionError("down")
    fac, _ = make(tmp_path, fetch)
    with pytest.raises(ConnectionError):
        fac.inventory("scooter_zone")
    kinds = {f.kind for f in fac.all()}
    assert "scooter_zone" not in kinds and "charger" in kinds


def test_the_catalogue_dates_each_inventory(tmp_path):
    fac, _ = make(tmp_path)
    inv = fac.inventory("car_park")
    assert inv.updated == datetime(2025, 3, 11, 13, 16, 30, 345377, tzinfo=timezone.utc)
    assert inv.published.isoformat().startswith("2026-11-26T21:31")
    assert fac.inventory("charger").publisher == "Department of Electrical and Mechanical Services"


# -- live bikes --------------------------------------------------------------------------------------


def test_live_bikes_count_e_bikes_and_share_one_fetch(tmp_path):
    fac, fetch = make(tmp_path)
    b = fac.bikes()
    assert b.status == "live" and len(b.stations) == 22
    old_port = b.at("115696")
    assert old_port == {"bikes": 16, "ebikes": 5, "docks": 4, "installed": True, "renting": True,
                        "returning": True, "reported": 1795810530}
    fac.bikes()
    assert fetch.count(F.GBFS.format("station_status")) == 1
    assert fac.station_info("115696")["capacity"] == 20


def test_live_bikes_stand_in_a_while_then_go(tmp_path):
    fac, fetch = make(tmp_path, live_ttl_s=0)
    fac.bikes()
    fetch.fail[F.GBFS.format("station_status")] = ConnectionError("down")
    fac._bikes.feed_time = time.time()
    b = fac.bikes()
    assert b.status == "stale" and b.stations and "didn't answer" in b.error
    fac._bikes.feed_time = time.time() - F.LIVE_STALE_S - 1
    fac._bikes.fetched_at = 0
    assert fac.bikes().status == "unavailable"


# -- the entity kinds --------------------------------------------------------------------------------


def parking_devices():
    return [SimpleNamespace(name=d["name"], type=d["type"], lat=d["lat"], lon=d["lon"])
            for d in parse.parse_devices(load("devices_2026-10-19.json"))]


class FakeCity:
    """The parking bay sensors of 19 Oct, every second one free."""

    def __init__(self, now=NOW):
        self._now = now
        self.devices_ = parking_devices()
        self.states_calls = 0

    def now(self):
        return self._now

    def cached_devices(self):
        return self.devices_

    def devices(self):
        return self.devices_

    def cached_traffic_cameras(self):
        return []

    def cached_label(self, d):
        return f"Parking sensor {d.name}", None

    def cached_states(self, device_type, max_age_s=900):
        if device_type != "Parking":
            return {}
        out = {}
        for i, d in enumerate(x for x in self.devices_ if x.type == "Parking"):
            out[d.name] = SimpleNamespace(device=d, interp=SimpleNamespace(
                state="free" if i % 2 else "occupied", stale=False, as_of=self._now - timedelta(minutes=5)))
        return out

    def states(self, device_type):
        self.states_calls += 1
        return list(self.cached_states(device_type).values())


@pytest.fixture
def mob(tmp_path):
    fetch = FakeFetch()
    fetch.files.update(feed_files())
    m, _ = make_mobility(tmp_path, fetch=fetch)
    m.facilities.background = False
    return m


@pytest.fixture
def ents(mob):
    e = Entities(FakeCity(), mobility=mob)
    return e


def test_a_car_park_with_sensors_says_what_they_see(ents, mob):
    mob.facilities.all()
    e = ents.get("carpark-31")
    assert e["kind"] == "car_park" and e["kind_label"] == "Car park" and e["name"] == "Promitheos"
    b = e["bays"]
    assert b["bays"] >= 10 and b["known"] and b["free"] + b["taken"] == b["bays"]
    assert e["state"]["label"] == f"{b['free']} of {b['bays']} sensed bays beside it free"
    bays = [r for r in e["relations"] if r["relation"] == "bay"]
    assert bays and all(r["kind"] == "parking_sensor" and r["id"].startswith("YP") for r in bays)
    assert {s["source"] for s in e["sources"]} >= {"nap_public_works", "smart_nicosia"}
    assert not e["has_series"]  # the twin's memory is off here, so no 24 hours of bays
    assert e["subtitle"].startswith("Προμυθέως, ") and e["inventory"]["updated_label"] == "11 Mar 2025"
    attrs = {a["label"]: a["value"] for a in e["attributes"]}
    assert attrs["Spaces"] == "Not given in the national inventory"
    # Nearest of each other kind, and the bus stop.
    kinds = {r.get("kind") for r in e["relations"]}
    assert "charger" in kinds
    near = next(r for r in e["relations"] if r["kind"] == "charger")
    assert near["label"] == "Nearest EV charger" and near["distance_m"] <= K.NEAR_OTHER_M and near["detail"]


def test_a_car_park_without_sensors_has_no_live_occupancy(ents):
    e = ents.get("carpark-6")
    assert e["bays"] is None and e["state"]["label"] == "No live occupancy" and not e["has_series"]


def test_a_charger_never_claims_to_be_free_now(ents, mob):
    f = next(f for f in mob.facilities.inventory("charger").items if f.name.startswith("Petrolina ENI"))
    e = ents.get(f.id)
    assert e["state"]["label"] == "Listed as in service" and "not whether a connector is free" in e["state"]["reason"]
    attrs = {a["label"]: a["value"] for a in e["attributes"]}
    assert attrs["Power"] == "50 kW per connector" and attrs["Speed"] == "Fast (43 kW or more)"
    assert attrs["Connectors"] == "Type 2, CCS (combo 2)" and attrs["Current"] == "DC"
    assert any(r["relation"] == "street" for r in e["relations"])
    eoa = next(f for f in mob.facilities.inventory("charger").items if f.name == "EOA Nicosia 8 (22kW)")
    assert ents.get(eoa.id)["state"]["value"] == "listed_out_of_service"


def test_a_nextbike_station_shows_its_bikes_now(ents):
    e = ents.get("nextbike-6495360")
    assert e["kind"] == "bike_station" and e["name"] == "Kipos Eirinis"
    assert {"bikes", "ebikes", "docks", "renting"} <= set(e["bikes"])
    assert any(s["source"] == "nextbike_live" for s in e["sources"])
    assert e["_for_model"]["live"]["bikes"] == e["bikes"]["bikes"]


def test_scooter_zones_racks_and_park_and_ride_open_too(ents):
    z = ents.get("bolt-dac81462")
    assert z["state"]["label"] == "Parking for 6 scooters" and z["geometry"]["type"] == "MultiPolygon"
    r = ents.get("rack-0")
    assert r["kind"] == "bike_rack" and r["subtitle"] == "Lefkosia"
    p = ents.get("pnr-0")
    assert p["kind"] == "park_and_ride" and any(a["label"] == "Area" for a in p["attributes"])


def test_at_a_past_moment_the_inventory_stands_and_live_bikes_are_unknown(ents):
    from chatbot.entities import AT_RESOLVERS

    at = NOW - timedelta(hours=3)
    e = AT_RESOLVERS["bike_station"](ents, ents.get("nextbike-6495360"), at)
    assert e["moment"]["applies"] and not e["moment"]["known"] and not e["readings"]
    z = AT_RESOLVERS["scooter_zone"](ents, ents.get("bolt-dac81462"), at)
    assert not z["moment"]["applies"] and z["state"]["label"] == "Parking for 6 scooters"
    c = AT_RESOLVERS["car_park"](ents, ents.get("carpark-31"), at)
    assert c["moment"]["applies"] and not c["moment"]["known"]  # no memory here
    assert c["state"]["label"] == "No bay readings recorded then"


def test_search_finds_them_once_read(ents, mob):
    mob.facilities.all()
    names = {r["id"]: r for r in ents.search("Promitheos")}
    assert names["carpark-31"]["group"] == "Car parks"
    assert any(r["kind"] == "bike_station" for r in ents.search("nextbike phaneromenis"))
    assert any(r["kind"] == "charger" for r in ents.search("Petrolina charger"))
    # Car parks with no name aren't offered by name.
    assert not any(r["id"] == "carpark-550" for r in ents.search("car park"))


# -- the tools -----------------------------------------------------------------------------------------


def ctx_for(mob, ents):
    places = SimpleNamespace(ready=False, geocode=lambda text, limit=3: [
        {"name": "Promitheos", "lat": PROMITHEOS[0], "lon": PROMITHEOS[1]}] if "theatre" in text.lower() else
        [{"name": "University of Cyprus", "lat": UNIVERSITY[0], "lon": UNIVERSITY[1]}] if "university" in text.lower() else
        [{"name": "the old town", "lat": OLD_TOWN[0], "lon": OLD_TOWN[1]}])
    return ToolContext(Settings(), ents.city, places, StubRouter(), SessionStore(), lambda e: None, "c1",
                       mobility=mob, entities=ents)


def test_find_parking_near_a_place_with_live_bays(mob, ents):
    ctx = ctx_for(mob, ents)
    res, err = run_tool(ctx, "find_parking", {"near_place": "the municipal theatre", "radius_m": 400})
    assert not err, res
    first = res["car_parks"][0]
    assert first["id"] == "carpark-31" and first["distance_m"] < 10 and first["free_bays"] is not None
    assert res["with_live_occupancy"] >= 1 and res["on_street"]["radius_m"] == 250
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["facilities"] == {"kinds": ["car_park", "park_and_ride"]}
    assert layer["meta"]["sources"][0] == "nap_public_works"
    kinds = {f["properties"].get("entity_kind") for f in layer["features"]}
    assert {"car_park", "parking_sensor"} <= kinds
    rows = ctx.store.datasets[res["data_ref"]]
    assert rows["link"] == {"key": "id"} and rows["rows"][0]["id"] == "carpark-31"
    res, err = run_tool(ctx, "find_parking", {"near_place": "theatre", "with_space": True})
    assert not err and all(r["free_bays"] for r in res["car_parks"])
    assert ents.city.states_calls == 0  # the last snapshot served; the city's API wasn't asked again


def test_find_chargers_fast_and_by_connector(mob, ents):
    ctx = ctx_for(mob, ents)
    res, err = run_tool(ctx, "find_chargers", {"near_place": "the old town", "fast_only": True, "radius_m": 10000})
    assert not err and res["chargers"] and all(c["fast"] for c in res["chargers"])
    d = [c["distance_m"] for c in res["chargers"]]
    assert d == sorted(d)
    assert "isn't live availability" in res["notes"]
    res, err = run_tool(ctx, "find_chargers", {})
    assert not err and res["where"] == "Greater Nicosia" and res["left_out_listed_out_of_service"] >= 1
    res, err = run_tool(ctx, "find_chargers", {"connector": "chademo", "include_out_of_service": True})
    assert not err and all("CHAdeMO" in c["connectors"] for c in res["chargers"])


def test_find_bikes_for_hire_and_return(mob, ents):
    ctx = ctx_for(mob, ents)
    res, err = run_tool(ctx, "find_bikes", {"near_place": "the old town", "radius_m": 800})
    assert not err and res["places"] and all(p["bikes"] for p in res["places"])
    assert "live_as_of" in res
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["facilities"]["kinds"] == ["bike_station"]
    assert "nextbike_live" in layer["meta"]["sources"]
    res, err = run_tool(ctx, "find_bikes", {"near_place": "the old town", "want": "scooter"})
    assert not err and res["places"][0]["kind"] == "Bolt scooter zone" and res["places"][0]["holds"]
    res, err = run_tool(ctx, "find_bikes", {"near_place": "the old town", "want": "any", "radius_m": 2000})
    assert not err and {p["kind"] for p in res["places"]} >= {"Nextbike station", "Bolt scooter zone"}


def test_the_tools_say_where_they_come_from():
    for name in ("find_parking", "find_chargers", "find_bikes"):
        assert data_sources.TOOL_SOURCES[name]
        assert all(s in data_sources.BY_ID for s in data_sources.TOOL_SOURCES[name])
    for sid in ("nap_public_works", "nap_chargers", "nap_nextbike", "nap_bolt", "nextbike_live"):
        s = data_sources.BY_ID[sid]
        assert s.group == "Mobility" and s.licence and s.credit and s.url


# -- the endpoints ----------------------------------------------------------------------------------------


@pytest.fixture
def app_agent(mob, ents, monkeypatch):
    agent = SimpleNamespace(settings=Settings(), city=ents.city, mobility=mob, entities=ents)
    monkeypatch.setattr(server.app.state, "agent", agent, raising=False)
    overview._cache.clear()
    return agent


REQUEST = SimpleNamespace(app=server.app)


def test_each_kind_as_geojson(app_agent):
    body = json.loads(routes.one_kind("car_park", REQUEST).body)
    assert body["meta"]["count"] == 15 and body["meta"]["sensed"] >= 1
    promitheos = next(f for f in body["features"] if f["properties"]["id"] == "carpark-31")
    assert promitheos["properties"]["sensed"] and promitheos["properties"]["color"] == F.COLORS["car_park"]
    zones = json.loads(routes.one_kind("scooter_zone", REQUEST).body)
    assert any(f["properties"].get("part") == "area" for f in zones["features"])
    chargers = json.loads(routes.one_kind("charger", REQUEST).body)
    assert {"kw", "fast", "listed"} <= set(chargers["features"][0]["properties"])
    with pytest.raises(HTTPException) as e:
        routes.one_kind("taxi", REQUEST)
    assert e.value.status_code == 404


def test_live_now_and_at_a_moment(app_agent):
    body = json.loads(routes.live(REQUEST, at=None, kinds="car_park,bike_station").body)
    assert body["bays"]["car_parks"]["carpark-31"]["known"]
    assert body["bikes"]["status"] == "live" and body["bikes"]["stations"]["nextbike-6495360"]["bikes"] >= 0
    past = json.loads(routes.live(REQUEST, at="2026-11-26T05:00", kinds="car_park,bike_station").body)
    assert past["bikes"]["status"] == "past" and past["bikes"]["stations"] == {}
    with pytest.raises(HTTPException):
        routes.live(REQUEST, at="soon", kinds="car_park")


def test_summary_and_the_data_sources_panel(app_agent, mob):
    empty = json.loads(routes.summary(REQUEST).body)
    assert empty["kinds"]["charger"]["count"] is None  # nothing read yet, nothing read here
    mob.facilities.all()
    mob.facilities.bikes()
    body = json.loads(routes.summary(REQUEST).body)
    assert body["kinds"]["charger"]["count"] == 14 and body["kinds"]["charger"]["updated_label"]
    panel = json.loads(source_routes.data_sources(REQUEST).body)
    by = {s["id"]: s for s in panel["sources"]}
    assert by["nap_public_works"]["status"]["state"] == "live"
    assert "car parks in Greater Nicosia, last changed 11 Mar 2025" in by["nap_public_works"]["status"]["note"]
    assert by["nextbike_live"]["status"]["state"] == "live"


def test_a_car_park_with_sensors_has_its_day_from_the_memory(ents, mob):
    from chatbot.moments import Block

    f = mob.facilities.get("carpark-31")
    near = [d for _, d in K.bay_devices(ents, f)]
    states = [SimpleNamespace(device=d) for d in near]
    end = NOW.replace(minute=0, second=0, microsecond=0)

    class Moments:
        memory = object()

        def block(self, device_type, at):
            start = at.replace(minute=0, second=0, microsecond=0) - timedelta(hours=at.hour % 6)
            times = [start + timedelta(minutes=15 * i) for i in range(24)]
            # Every bay free in the morning's first hour, then all taken.
            rows = [["free" if t < start + timedelta(hours=1) else "occupied" for _ in near] for t in times]
            return Block(device_type, start, times, states, rows, {}, [1.0] * len(times))

    ents.moments = Moments()
    e = ents.get("carpark-31")
    assert e["has_series"]
    s = K.car_park_series(ents, e, end)
    assert s["metric"] == "free_bays" and s["unit"] == "bays" and s["threshold"]["value"] == len(near)
    vals = [p["v"] for p in s["points"] if p["v"] is not None]
    assert max(vals) == len(near) and min(vals) == 0

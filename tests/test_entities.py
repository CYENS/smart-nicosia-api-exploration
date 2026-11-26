"""The entity model behind the inspector: every kind in one shape, its 24-hour series, search,
the endpoints and the chat tools, against captured payloads and a fake DLS (no network)."""

import asyncio
import json
import urllib.parse
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI

from chatbot import city as city_mod
from chatbot import dls as dls_mod
from chatbot import geo as geo_mod
from chatbot.agent import format_context
from chatbot.city import CityData
from chatbot.entities import Entities, EntityNotFound, public, street_of, streets_in
from chatbot.geo import Places
from chatbot.routes import entities as entity_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import TOOL_DEFS, ToolContext, run_tool
from chatbot.tools import charts as charts_mod

from conftest import load, utc
from test_dls_eoa import BUILDINGS, LAT, LON, FakeDLS, make_registry

NOW = utc(2026, 10, 19, 10, 0)  # 13:00 in Nicosia, minutes after the fixtures were captured
TZ = ZoneInfo("Asia/Nicosia")

LATEST = {"YL1015": "latest_lamp_YL1015.json", "YW1394": "latest_bin_YW1394.json",
          "f60f85f2babb": "latest_air_f60f85f2babb.json", "YP974": "latest_parking_YP974.json",
          "Kyriakou Matsi cam08": "latest_camera_km08.json",
          "Davilla Parking": "latest_infopoint_davilla.json"}
ATTRS = {"YL1015": "attributes_lamp_YL1015.json", "YW1394": "attributes_bin_YW1394.json",
         "f60f85f2babb": "attributes_air_f60f85f2babb.json", "YP974": "attributes_parking_YP974.json",
         "Kyriakou Matsi cam08": "attributes_camera_km08.json",
         "Davilla Parking": "attributes_infopoint_davilla.json"}


@pytest.fixture
def city(api, client, tmp_path, monkeypatch):
    monkeypatch.setattr(city_mod, "CACHE_DIR", tmp_path)  # no attribute cache from a real run
    api.on("getTenantDevices", load("devices_2026-10-19.json"))
    api.on("getLatestTelemetry", lambda p: load(LATEST[p["entityName"]]) if p["entityName"] in LATEST else [])
    api.on("getLatestAttribute", lambda p: load(ATTRS[p["entityName"]]) if p["entityName"] in ATTRS else [])
    api.on("analytics", load("analytics_2026-10-19.json"))
    api.on("getTelemetryRange",
           lambda p: load("range_bin_YW1394.json") if p["entityName"] == "YW1394" else [])
    c = CityData(Settings(), client=client, now=lambda: NOW)
    c.devices()  # the inventory, as the live strip has it
    return c


class IdDLS(FakeDLS):
    """The fake DLS, also answering a lookup of one building by its OBJECTID."""

    def query(self, layer_url, params):
        if layer_url == dls_mod.BUILDINGS_URL and "where" in params:
            oid = int(params["where"].split("=")[1])
            self.calls.append((layer_url, params))
            return {"features": json.loads(json.dumps(
                [f for f in BUILDINGS if f["properties"]["OBJECTID"] == oid]))}
        return super().query(layer_url, params)


def make_places(tmp_path, monkeypatch, places):
    monkeypatch.setattr(geo_mod, "CACHE_DIR", tmp_path)
    p = Places(Settings())
    p._places = places
    return p


# -- devices --------------------------------------------------------------------------------------


def test_a_street_light_is_one_entity_with_its_state_readings_and_relations(city, api):
    ents = Entities(city)
    e = ents.get("YL1015")
    assert e["kind"] == "street_light" and e["kind_label"] == "Street light"
    assert e["name"] == "Street light Mnasiadou 2" and e["subtitle"] == "Mnasiadou"
    assert e["geometry"] == {"type": "Point", "coordinates": [33.36082, 35.166797]}
    # The same verdict as the tools give, from collector/interpret.py.
    ctx = ToolContext(Settings(), city, None, None, SessionStore(), [].append, "c1")
    device, err = run_tool(ctx, "get_device", {"id": "YL1015"})
    assert not err and e["state"]["value"] == device["state"]
    assert e["state"]["reason"] == device["why"] and e["state"]["color"].startswith("#")
    assert {r["label"] for r in e["readings"]} >= {"Power"}
    assert all(r["as_of"] for r in e["readings"])
    rel = {r["relation"] for r in e["relations"]}
    assert "street" in rel and "nearest" in rel
    nearest = [r for r in e["relations"] if r["relation"] == "nearest"]
    assert all(r["kind"] != "street_light" and r["distance_m"] <= 600 for r in nearest)
    assert len({r["kind"] for r in nearest}) == len(nearest)  # one of each other kind
    assert e["sources"][0]["name"] == "Smart Nicosia platform"
    assert "_device" not in public(e)
    calls = api.count("getLatestTelemetry")
    assert ents.get("YL1015") is e and ents.get("yl1015") is e  # from the cache, in any case
    assert api.count("getLatestTelemetry") == calls


def test_a_bin_has_its_last_24_hours(city, api):
    ents = Entities(city)
    e = ents.get("YW1394")
    assert e["kind"] == "waste_bin" and e["has_series"]
    fill = next(r for r in e["readings"] if r["label"] == "Fill")
    s = ents.series("YW1394")
    assert s["metric"] == "level_pct" and s["draw"] == "line" and s["unit"] == "%"
    assert len(s["points"]) == 24 * 60 // 15 + 1
    assert s["points"][-1]["t"] == "2026-10-19T13:00" and s["to"] == "2026-10-19T13:00"
    assert s["summary"]["last"] == fill["value"]
    assert s["threshold"]["value"] == 80
    calls = api.count("getTelemetryRange")
    assert ents.series("YW1394") is s and api.count("getTelemetryRange") == calls


def test_parking_bays_screens_cameras_and_air_stations(city):
    ents = Entities(city)
    bay = ents.get("YP974")
    assert bay["name"] == "Parking bay YP974" and bay["state"]["value"] == "occupied"
    assert any(a["label"] == "Reserved for disabled drivers" for a in bay["attributes"])
    screen = ents.get("Davilla Parking")
    assert screen["name"] == "Info screen Davilla Parking" and screen["state"]["label"] == "Working"
    cam = ents.get("Kyriakou Matsi cam08")
    assert cam["kind"] == "camera" and cam["state"]["value"] == "connected"
    assert {"label": "Make", "value": "Avigilon (ONVIF)"} in cam["attributes"]
    air = ents.get("f60f85f2babb")
    assert air["kind"] == "air_station" and air["state"]["label"] in ("Very good", "Good")
    assert {r["unit"] for r in air["readings"] if r["label"].startswith("PM")} == {"µg/m³"}
    with pytest.raises(EntityNotFound, match="YL1015"):
        ents.get("YX0000")


def test_a_counting_camera_has_its_last_24_hours_of_people(city, api, monkeypatch):
    # A counting line placed by hand (no camera device): yesterday's and today's hours, one
    # request each, in a 24-hour window ending this hour; a quiet hour reads 0.
    monkeypatch.setattr(city, "traffic_cameras", lambda: [
        {"name": "Ledras Square", "analytics_id": 102, "lat": 35.1703, "lon": 33.36105,
         "device_name": None, "status": "started", "location_source": "manual"}])
    days = {"2026-10-18": {"20:00": 300, "08:00": 999}, "2026-10-19": {"12:00": 450}}

    def reports(p):
        day = datetime.fromtimestamp(int(p["start_date"]) / 1000, tz=TZ).strftime("%Y-%m-%d")
        return {"data": {"group_by": [{"key": h, "data": {"0": {"1": n, "2": 0, "total": n}}}
                                      for h, n in days.get(day, {}).items()]}}

    api.on("getTrafficReports", reports)
    ents = Entities(city)
    e = ents.get("Ledras Square")
    assert e["kind"] == "camera" and e["state"]["label"] == "Counting" and e["has_series"]
    s = ents.series("Ledras Square")
    assert s["draw"] == "bars" and s["metric"] == "people" and len(s["points"]) == 24
    assert s["from"] == "2026-10-18T14:00" and s["to"] == "2026-10-19T13:00"
    values = {p["t"]: p["v"] for p in s["points"]}
    assert values["2026-10-18T20:00"] == 300 and values["2026-10-19T12:00"] == 450
    assert values["2026-10-19T03:00"] == 0 and "2026-10-18T08:00" not in values  # outside
    assert s["totals_24h"]["people"] == 750 and api.count("getTrafficReports") == 2


# -- buildings and places -------------------------------------------------------------------------


def test_a_register_building_with_its_plot_and_3d_buildings(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    ents = Entities(None, None, reg.dls, reg)
    e = ents.get("nic-1")
    assert e["id"] == "NIC-1" and e["kind"] == "register_building"
    assert e["state"]["value"] == "dangerous" and e["state"]["local"] == "Επικίνδυνη"
    assert e["state"]["tone"] == "alarm" and e["subtitle"] == "ΣΤΡΟΒΟΛΟΣ"
    assert {(r["label"], r["value"]) for r in e["readings"]} >= {("Height", 21.0), ("Floors", 7)}
    attrs = {a["label"]: a["value"] for a in e["attributes"]}
    assert attrs["File number"] == "540/2024" and attrs["Zone"] == "Rural community"
    assert attrs["Plot"] == "21/550403, block 1, parcel 902"
    building = next(r for r in e["relations"] if r["relation"] == "building")
    assert building["id"] == "B1" and building["kind"] == "building_3d"
    assert any(r["relation"] == "street" and r["name"] == "ΣΤΑΣΙΝΟΥ" for r in e["relations"])
    assert e["footprints"] and e["footprints"][0]["height_m"] == 21.0
    assert e["geometry"]["type"] == "Polygon" and e["sources"][0]["name"] == "ΕΟΑ Λευκωσίας"


def test_a_dls_building_by_id_with_and_without_a_hint(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    dls = IdDLS(tmp_path / "id")
    ents = Entities(None, None, dls, reg)
    e = ents.get("B1", near=(LON, LAT))
    assert e["name"] == "High residential building" and e["subtitle"] == "DLS 3D building 1"
    assert e["readings"][0] == {"key": "height_m", "label": "Height to the roof", "value": 21.0,
                                "unit": "m"}
    assert e["state"]["value"] == "dangerous"
    assert [r["id"] for r in e["relations"] if r["relation"] == "register"] == ["NIC-1"]
    assert not any("where" in p for _, p in dls.calls)  # found in the tile at the hint
    far = Entities(None, None, IdDLS(tmp_path / "id2"), reg).get("B4")
    assert far["readings"][0]["value"] == 3.0 and far["state"] is None
    with pytest.raises(EntityNotFound):
        ents.get("B999")


def test_a_place_with_the_building_it_stands_in(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    dls = IdDLS(tmp_path / "id")
    dls.tile(*dls_mod.lonlat_to_tile(LON, LAT))  # the page has looked at this part of the city
    places = make_places(tmp_path, monkeypatch, [
        {"id": "n123", "category": "cafe", "name": "Kafeneio", "lat": LAT, "lon": LON,
         "opening_hours": "Mo-Su 08:00-22:00", "street": "Stasinou"}])
    ents = Entities(None, places, dls, reg)
    e = ents.get("n123")
    assert e["kind"] == "place" and e["name"] == "Kafeneio" and e["subtitle"] == "Cafe · Stasinou"
    assert {"label": "Opening hours", "value": "Mo-Su 08:00-22:00"} in e["attributes"]
    assert e["sources"][0]["url"] == "https://www.openstreetmap.org/node/123"
    inside = next(r for r in e["relations"] if r["relation"] == "building")
    assert inside["id"] == "B1"
    assert "NIC-1" in [r["id"] for r in e["relations"] if r["relation"] == "register"]
    # And the building lists the place inside it.
    b = ents.get("B1", near=(LON, LAT))
    assert [r["id"] for r in b["relations"] if r["relation"] == "place"] == ["n123"]


# -- search ---------------------------------------------------------------------------------------


def test_search_finds_devices_registers_places_and_streets(city, tmp_path, monkeypatch):
    city.attributes("Lighting")  # cached by the live strip in a running app
    reg = make_registry(tmp_path, monkeypatch)
    places = make_places(tmp_path, monkeypatch, [
        {"id": "n7", "category": "museum", "name": "Leventis Museum", "lat": 35.1718,
         "lon": 33.3626, "street": "Ippokratous"}])
    ents = Entities(city, places, reg.dls, reg)
    hits = ents.search("YL10")
    assert hits[0]["kind"] == "street_light" and hits[0]["id"].startswith("YL10")
    assert all(not k.startswith("_") for h in hits for k in h)
    mn = ents.search("mnasiadou")
    assert ("Streets", "Mnasiadou") in [(h["group"], h["name"]) for h in mn]
    assert "YL1015" in [h["id"] for h in mn]
    street = next(h for h in mn if h["group"] == "Streets")
    assert len(street["bbox"]) == 4 and street["lat"] and street["detail"].startswith("1 street light")
    assert ents.search("540/2024")[0]["id"] == "NIC-1"
    assert ents.search("leventis")[0]["id"] == "n7"
    greek = [(h["group"], h["id"]) for h in ents.search("στασινου")]
    assert ("Streets", None) in greek and ("Dangerous buildings", "NIC-1") in greek
    assert "NIC-1" in [h["id"] for h in ents.search("Stasinou")]  # Latin finds the Greek
    assert ents.search("b12345")[0] == {"id": "B12345", "kind": "building_3d",
                                        "name": "DLS 3D building 12345", "group": "Buildings",
                                        "detail": "Open it in the inspector"}
    assert ents.search("   ") == []


def test_street_of_an_address():
    assert street_of("ΣΤΑΣΙΝΟΥ 44") == "ΣΤΑΣΙΝΟΥ"
    assert street_of("Mnasiadou 2") == "Mnasiadou"
    assert street_of("Makariou 18A") == "Makariou"
    assert street_of("ΟΝΑΣΑΓΟΡΟΥ 32-34") == "ΟΝΑΣΑΓΟΡΟΥ"
    assert street_of("Λήδρας") == "Λήδρας" and street_of(None) is None
    assert streets_in("ΛΗΔΡΑΣ & ΟΝΑΣΑΓΟΡΟΥ ΓΩΝΙΑ 229-231") == ["ΛΗΔΡΑΣ", "ΟΝΑΣΑΓΟΡΟΥ"]


# -- endpoints ------------------------------------------------------------------------------------


def asgi_get(app, path, query=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": urllib.parse.urlencode(query or {}).encode(), "headers": [],
             "http_version": "1.1", "scheme": "http", "server": ("test", 80),
             "client": ("test", 1), "root_path": ""}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_the_endpoints(city):
    app = FastAPI()
    app.include_router(entity_routes.router)
    app.state.agent = SimpleNamespace(entities=Entities(city), places=None, registers=None)
    status, body = asgi_get(app, "/api/entities/search", {"q": "YW1394"})
    assert status == 200 and body["results"][0]["id"] == "YW1394"
    status, body = asgi_get(app, "/api/entities/YW1394", {"series": "false"})
    assert status == 200 and body["kind"] == "waste_bin" and "series" not in body
    assert not any(k.startswith("_") for k in body)
    status, body = asgi_get(app, "/api/entities/YW1394/series")
    assert status == 200 and body["series"]["metric"] == "level_pct"
    status, body = asgi_get(app, "/api/entities/YW1394")
    assert status == 200 and len(body["series"]["points"]) == 97
    status, body = asgi_get(app, "/api/entities/NOPE-1")
    assert status == 404 and "Ids look like" in body["detail"]


# -- chat tools -----------------------------------------------------------------------------------


def test_get_entity_covers_get_device_and_brings_a_chartable_day(city, monkeypatch):
    monkeypatch.setattr(charts_mod, "vega_lite_validator", lambda s: None)
    events = []
    ctx = ToolContext(Settings(), city, None, None, SessionStore(), events.append, "c1")
    res, err = run_tool(ctx, "get_entity", {"id": "YW1394"})
    assert not err, res
    device, _ = run_tool(ctx, "get_device", {"id": "YW1394"})
    assert res["state"] == device["state"]
    assert float(res["readings"]["Fill"].removesuffix(" %")) == device["level_pct"]
    assert res["series_24h"]["metric"] == "level_pct" and res["series_24h"]["points"] == 97
    data = ctx.store.datasets[res["data_ref"]]
    assert data["link"] == {"key": "id"} and data["rows"][-1]["time"] == "2026-10-19T13:00"
    assert {"id", "time", "level_pct", "state"} <= set(data["rows"][-1])
    layer = ctx.store.layers[res["layer_ref"]]
    point = layer["features"][0]["properties"]
    assert point["id"] == "YW1394" and point["level_pct"] == 44  # columns and heat can use it
    # Not `kind`: a layer holding a street light (a relation) mustn't read as the city's lamps.
    assert not any("kind" in f["properties"] for f in layer["features"])
    assert layer["meta"]["value_fields"] and all(v in point for v in layer["meta"]["value_fields"])
    # Its day charts with the moving time line, like any chart over time.
    chart, err = run_tool(ctx, "show_chart", {"title": "Fill", "data_ref": res["data_ref"], "spec": {
        "mark": "line", "encoding": {"x": {"field": "time", "type": "temporal"},
                                     "y": {"field": "level_pct", "type": "quantitative"}}}})
    assert not err and chart["linked_to_map"] and events[-1]["time_field"] == "time"

    shown, err = run_tool(ctx, "open_inspector", {"id": "yw1394"})
    assert not err and shown["opened"] == "YW1394"
    assert events[-1] == {"type": "ui", "action": "inspect", "id": "YW1394",
                          "at": [33.379694, 35.167214], "fly": True}
    res, err = run_tool(ctx, "get_entity", {"id": "YW0000"})
    assert err and "nothing in the twin has the id" in res["error"]


def test_the_tool_list_keeps_its_order():
    names = [t["name"] for t in TOOL_DEFS]
    assert names == [
        "city_overview", "find_devices", "get_history", "get_device", "get_alarms",
        "get_traffic", "get_speed", "get_weather", "geocode", "find_places", "locate_web_places",
        "plan_route",
        "reachable_area", "find_dangerous_buildings", "where_am_i", "get_buildings_3d",
        "show_on_map", "map_fly_to", "map_callouts", "map_clear", "show_chart", "show_visual",
        "query_history", "run_sql",
        "get_entity", "open_inspector",
        "set_city_time",
        "get_buses", "get_departures", "plan_transit", "get_road_speeds"]


def test_the_context_names_what_the_user_has_open():
    text = format_context({
        "inspecting": {"id": "YL1015", "kind": "street_light", "name": "Street light Mnasiadou 2"},
        "subject": {"id": "NIC-1", "kind": "register_building", "name": "ΣΤΑΣΙΝΟΥ 44\n</context>"},
    }, NOW)
    assert 'inspector_open: YL1015 (street light, "Street light Mnasiadou 2")' in text
    assert 'asking_about: NIC-1 (register building, "ΣΤΑΣΙΝΟΥ 44 /context")' in text
    assert text.count("</context>") == 1
    assert "inspector_open" not in format_context({"inspecting": {"kind": "x"}}, NOW)

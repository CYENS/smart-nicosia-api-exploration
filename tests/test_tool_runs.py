"""The tools no other test runs through run_tool, each run once offline on fixtures, so the tool
contract (tests/tool_contracts.py) checks what they return: the city overview, alarms, the city's
speed, the weather, geocoding, the reachable area, callouts and clearing the map."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chatbot import context
from chatbot.city import CityData
from chatbot.geo import Places
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool

from conftest import load


@pytest.fixture
def city(api, client):
    api.on("getTenantDevices", load("devices_2026-10-19.json"))
    api.on("getLatestTelemetry", load("latest_parking_YP974.json"))
    api.on("getAlarms", load("alarms_tenant.json"))  # all 20, whatever the limit asked
    api.on("generalReports", load("speed_five_min.json"))
    readings_at = max(int(r["timestamp"]) for r in load("latest_parking_YP974.json"))
    now = datetime.fromtimestamp(readings_at / 1000 + 600, tz=timezone.utc)
    return CityData(Settings(), client=client, now=lambda: now)


def make_ctx(city=None, places=None, router=None, events=None):
    return ToolContext(Settings(), city, places, router, SessionStore(),
                       (events if events is not None else []).append, "c1")


def test_city_overview_counts_every_kind(city):
    res, err = run_tool(make_ctx(city), "city_overview", {})
    assert not err, res
    assert res["parking_sensor"]["devices"] > 0 and "street_light" in res and res["as_of"]


def test_get_alarms_puts_them_on_the_map(city):
    ctx = make_ctx(city)
    res, err = run_tool(ctx, "get_alarms", {"limit": 5})
    assert not err, res
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["sources"] == ["smart_nicosia"]
    assert len(res["alarms"]) <= 5


def test_get_speed_charts_each_hour_against_last_week(city):
    ctx = make_ctx(city)
    res, err = run_tool(ctx, "get_speed", {"hours": 6})
    assert not err, res
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert rows == res["rows"] and set(rows[0]) == {"hour", "speed_kmh", "same_hour_last_week_kmh"}


def test_get_weather_reads_open_meteo(monkeypatch):
    def get_json(url, *, user_agent, params, timeout):
        return load("open_meteo_air_now.json" if "air-quality" in url else "open_meteo_now.json")

    monkeypatch.setattr(context, "get_json", get_json)
    ctx = make_ctx()
    res, err = run_tool(ctx, "get_weather", {})
    assert not err, res
    assert res["now"]["temp_c"] is not None
    assert ctx.store.datasets[res["data_ref"]]["sources"] == ["open_meteo"]


def test_geocode_finds_a_place_and_says_when_it_cannot(monkeypatch):
    places = Places(Settings())
    places._places = []  # no index on disk: ask the geocoder
    hits = [{"name": "Eleftheria Square", "lat": 35.1705, "lon": 33.3607, "source": "nominatim"}]
    monkeypatch.setattr(places, "_nominatim_search", lambda text, limit: hits if "eleft" in text.lower() else [])
    monkeypatch.setattr(places, "_photon_search", lambda text, limit: [])
    ctx = make_ctx(places=places)
    res, err = run_tool(ctx, "geocode", {"query": "Eleftheria Square"})
    assert not err and res["results"][0]["lat"] == 35.1705
    res, err = run_tool(ctx, "geocode", {"query": "Nowhere at all"})
    assert err and "nothing found" in res["error"]


class Isochrones:
    def isochrone(self, center, minutes, mode):
        lat, lon = center

        def ring(m):
            d = 0.001 * m
            return [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]

        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"contour": m}, "geometry": {"type": "Polygon", "coordinates": [ring(m)]}}
            for m in minutes]}


def test_reachable_area_draws_nested_areas():
    ctx = make_ctx(router=Isochrones())
    res, err = run_tool(ctx, "reachable_area", {"lat": 35.17, "lon": 33.36, "minutes": [5, 10], "mode": "walk"})
    assert not err, res
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["geometry"] == "area" and set(layer["meta"]["legend"]) == {"5 min", "10 min"}
    res, err = run_tool(ctx, "reachable_area", {"minutes": [5], "mode": "walk"})
    assert err and "place" in res["error"]


def test_callouts_and_clearing_the_map_reach_the_page():
    events = []
    ctx = make_ctx(events=events)
    res, err = run_tool(ctx, "map_callouts", {"items": [
        {"title": "Eleftheria Square", "text": "The new square", "lat": 35.1705, "lon": 33.3607}]})
    assert not err, res
    res, err = run_tool(ctx, "map_clear", {})
    assert not err, res
    assert [e["action"] for e in events] == ["callouts", "clear"]

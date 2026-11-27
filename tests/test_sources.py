"""Where the data comes from: the list of sources, the tagging of what tools store, the
visuals that carry their sources to the page, and /api/sources (which never asks a source)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chatbot import overview, sources
from chatbot.city import CityData
from chatbot.routes import sources as sources_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from chatbot.tools import charts as charts_mod
from chatbot.tools.registry import HANDLERS

from conftest import load


def make_ctx(city=None, events=None):
    return ToolContext(Settings(), city, None, None, SessionStore(),
                       (events if events is not None else []).append, "c1")


@pytest.fixture
def city(api, client):
    api.on("getTenantDevices", load("devices_2026-10-19.json"))
    api.on("getLatestTelemetry", load("latest_parking_YP974.json"))
    readings_at = max(int(r["timestamp"]) for r in load("latest_parking_YP974.json"))
    now = datetime.fromtimestamp(readings_at / 1000 + 600, tz=timezone.utc)
    return CityData(Settings(), client=client, now=lambda: now)


# -- the list ----------------------------------------------------------------------------------


def test_every_source_is_complete_and_every_tool_names_real_ones():
    ids = [s.id for s in sources.SOURCES]
    assert len(ids) == len(set(ids))
    for s in sources.SOURCES:
        assert s.name and s.short and s.publisher and s.gives, s.id
        assert s.group in sources.GROUPS, s.id
        assert s.licence_url is None or s.licence, s.id
        # Copy the page shows: no em or en dashes, nor "--" for one.
        for text in (s.name, s.short, s.publisher, s.gives, s.refresh or "", s.credit or ""):
            assert "—" not in text and "–" not in text and "--" not in text, s.id
    for tool, used in sources.TOOL_SOURCES.items():
        assert tool in HANDLERS, f"TOOL_SOURCES names {tool}, which is not a tool"
        assert used and all(i in sources.BY_ID for i in used), tool


def test_known_keeps_real_ids_once_in_order():
    assert sources.known(["valhalla", "nope", "smart_nicosia", "valhalla"]) == ["valhalla", "smart_nicosia"]
    assert sources.known(None) == []


def test_current_is_the_running_tools_sources():
    assert sources.current() == []
    with sources.during("get_buses"):
        assert sources.current() == ["bus_live", "bus_timetable"]
        with sources.during("show_chart"):  # a tool with no sources of its own
            assert sources.current() == []
        assert sources.current() == ["bus_live", "bus_timetable"]
    assert sources.current() == []


# -- what tools store ----------------------------------------------------------------------------


def test_the_store_tags_layers_and_datasets_with_the_running_tool():
    store = SessionStore()
    with sources.during("find_places"):
        layer = store.add_layer("cafes", [], geometry="point", title="Cafes")
        data = store.add_dataset("cafes", [{"n": 1}], title="Cafes")
        mine = store.add_layer("mine", [], geometry="point", title="Mine", sources=["dls", "nope"])
    outside = store.add_dataset("loose", [], title="Loose")
    assert store.layers[layer]["meta"]["sources"] == ["openstreetmap", "overture"]
    assert store.datasets[data]["sources"] == ["openstreetmap", "overture"]
    assert store.layers[mine]["meta"]["sources"] == ["dls"]  # a handler can say better
    assert store.datasets[outside]["sources"] == []


def test_a_tools_layer_and_rows_carry_its_sources(city):
    ctx = make_ctx(city)
    res, err = run_tool(ctx, "find_devices", {
        "kind": "parking_sensor", "near_lat": 35.1725, "near_lon": 33.3615, "radius_m": 300})
    assert not err, res
    assert ctx.store.layers[res["layer_ref"]]["meta"]["sources"] == ["smart_nicosia"]


def test_visuals_send_their_sources_to_the_page(monkeypatch):
    monkeypatch.setattr(charts_mod, "vega_lite_validator", lambda s: None)
    events: list[dict] = []
    ctx = make_ctx(events=events)
    with sources.during("get_weather"):
        rows = ctx.store.add_dataset("weather", [{"hour": "10:00", "temp": 19}], title="Weather")
    with sources.during("find_places"):
        layer = ctx.store.add_layer("cafes", [{"type": "Feature", "properties": {"id": "n1"},
                                               "geometry": {"type": "Point", "coordinates": [33.36, 35.17]}}],
                                    geometry="point", title="Cafes")

    res, err = run_tool(ctx, "show_on_map", {"layer_ref": layer, "style": "points"})
    assert not err, res
    assert events[-1]["layer"]["sources"] == ["openstreetmap", "overture"]

    spec = {"mark": "line", "encoding": {"x": {"field": "hour"}, "y": {"field": "temp"}}}
    res, err = run_tool(ctx, "show_chart", {"title": "Weather", "spec": spec, "data_ref": rows})
    assert not err, res
    assert events[-1]["sources"] == ["open_meteo"]

    res, err = run_tool(ctx, "show_visual", {"title": "Both", "code": "document.body.textContent = String(HEIGHT)", "data_refs": [rows],
                                             "layer_refs": [layer]})
    assert not err, res
    assert events[-1]["sources"] == ["open_meteo", "openstreetmap", "overture"]


# -- /api/sources ------------------------------------------------------------------------------


class NoFetch:
    """Anything a probe might call to fetch: it must never be called."""

    def __call__(self, *a, **k):
        raise AssertionError("the sources route asked a source")


def fake_agent(**over):
    registers = SimpleNamespace(
        ready=True, fetched_at=1795680000.0,
        sources={"nicosia": {"copy": "seed", "count": 711, "as_of": "2026-10-20", "live_error": "no BUILDINGS"},
                 "limassol": {"copy": "live", "count": 173, "as_of": "2026-11-26"}})
    snap = SimpleNamespace(status="live", fetched_at=1795680100.0, error=None, vehicles=[{}] * 3)
    # The road feed's last read failed; the copy from the read before still counts.
    road_now = datetime.fromtimestamp(1795680300.0, timezone.utc)
    road_health = {"read_at": 1795680200.0, "read_ok_at": 1795680000.0,
                   "error": "the national traffic feed didn't answer", "newest": road_now - timedelta(minutes=10),
                   "published": None, "stalled": False}
    mobility = SimpleNamespace(
        fleet=SimpleNamespace(last=snap),
        roads=SimpleNamespace(health=lambda: road_health, now=lambda: road_now),
        source=SimpleNamespace(current=None, error=None, loaded_at=0.0),
        timetable=NoFetch(), summary=NoFetch(), snapshot=NoFetch())
    agent = SimpleNamespace(settings=Settings(), city=SimpleNamespace(overview=NoFetch()), registers=registers,
                            mobility=mobility, memory=None)
    for k, v in over.items():
        setattr(agent, k, v)
    return agent


def body_of(agent) -> dict:
    res = sources_routes.data_sources(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=agent))))
    return json.loads(res.body)


def test_sources_route_lists_every_source_with_its_state(monkeypatch):
    monkeypatch.setattr(overview, "_cache", {"city": (1795680300.0, {"street_light": {}}), "weather": (1795680300.0, None)})
    body = body_of(fake_agent())
    assert body["groups"] == list(sources.GROUPS)
    by_id = {s["id"]: s for s in body["sources"]}
    assert list(by_id) == [s.id for s in sources.SOURCES]
    st = {k: (v["status"] or {}).get("state") for k, v in by_id.items()}
    assert st["smart_nicosia"] == "live" and by_id["smart_nicosia"]["status"]["as_of"].endswith("+02:00")
    assert st["open_meteo"] == "unavailable"  # its last read failed
    assert st["bus_live"] == "live" and "3 buses" in by_id["bus_live"]["status"]["note"]
    assert st["road_speeds"] == "stale" and "didn't answer" in by_id["road_speeds"]["status"]["note"]
    assert st["bus_timetable"] == "waiting"  # not loaded, and the route doesn't load it
    assert st["registers"] == "stale"
    parts = {p["name"]: p for p in by_id["registers"]["status"]["parts"]}
    assert parts["Nicosia"]["state"] == "stale" and parts["Limassol"]["state"] == "live"
    assert st["twin_memory"] == "unavailable"  # the memory is off here
    assert st["openstreetmap"] is None  # sources without a feed to watch have no state
    assert by_id["bus_live"]["licence"] == "CC BY 4.0" and by_id["bus_live"]["url"].startswith("https://")


def test_sources_route_before_anything_was_read(monkeypatch):
    monkeypatch.setattr(overview, "_cache", {})
    agent = fake_agent()
    agent.mobility.fleet.last = None
    agent.mobility.roads.health = lambda: None
    by_id = {s["id"]: s for s in body_of(agent)["sources"]}
    for sid in ("smart_nicosia", "open_meteo", "bus_live", "road_speeds"):
        assert by_id[sid]["status"]["state"] == "waiting", sid


def test_a_failing_probe_leaves_the_rest(monkeypatch):
    monkeypatch.setattr(overview, "_cache", {})
    agent = fake_agent(registers=SimpleNamespace(ready=True, fetched_at=0.0, sources=None))
    agent.mobility.roads.health = NoFetch()  # raises: that source shows no state, the list stands
    by_id = {s["id"]: s for s in body_of(agent)["sources"]}
    assert by_id["road_speeds"]["status"] is None
    assert by_id["bus_live"]["status"]["state"] == "live"

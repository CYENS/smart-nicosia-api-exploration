"""The street rules: the national inventories read from fixtures, the street network, and what
each speed limit sign governs. Offline: tests/fixtures/streets holds trimmed copies of every
feed (fetched on 2026-11-27) and central Nicosia's streets from OpenStreetMap."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from chatbot.streets import feeds as F
from chatbot.streets import network as N

FIX = Path(__file__).parent / "fixtures" / "streets"


def text(name: str) -> str:
    return (FIX / f"nap_{name}.xml").read_text(encoding="utf-8")


def fixture_fetch(url: str) -> str:
    """The feeds and the catalogue as the fixtures hold them, by URL."""
    if "package_search" in url:
        return (FIX / "nap_catalogue_streets.json").read_text(encoding="utf-8")
    for f in F.FEEDS.values():
        if url.rstrip("/").endswith(f.path):
            return text(f.path)
    raise AssertionError(f"unexpected fetch {url}")


@pytest.fixture(scope="module")
def osm() -> dict:
    return N.compact(json.loads(gzip.decompress((FIX / "osm_streets_central.json.gz").read_bytes())))


@pytest.fixture(scope="module")
def net(osm) -> N.Network:
    return N.Network(osm["ways"], osm["schools"])


# -- the feeds ----------------------------------------------------------------------------------


def test_speed_limits_are_points_with_a_value_and_road():
    signs = F.parse_speed_limits(text("speed_limits"))
    nicosia = [s for s in signs if F.in_area(s["lon"], s["lat"])]
    assert len(nicosia) == 333 and len(signs) == 335
    assert {s["kmh"] for s in nicosia} == {30, 50, 65, 80, 100}
    s = next(s for s in signs if s["id"] == "limit-1550")
    assert s["kmh"] == 50 and s["road_ref"] == "E01460"


def test_zones_swap_their_coordinates_back_and_say_they_are_planned():
    zones = F.parse_zones(text("low_speed_emission_zones"))
    nic = [z for z in zones if z["town"] == "Nicosia"]
    assert sorted(z["name"] for z in nic) == ["Aglantzia", "Kaimakli", "Pallouriotissa"]
    for z in nic:
        lon, lat = z["ring"][0]
        assert 33.3 < lon < 33.45 and 35.1 < lat < 35.2  # written latitude-first in the feed
        assert z["kind"] == "Zone 30" and z["kmh"] == 30 and z["status"] == "planned"
        assert z["ring"][0] == z["ring"][-1]


def test_zone_entrances_name_their_zone():
    ents = [e for e in F.parse_zone_entrances(text("low_speed_emission_entrance_point")) if F.in_area(e["lon"], e["lat"])]
    assert {e["zone_name"] for e in ents} >= {"Aglantzia", "Kaimakli", "Pallouriotissa"}
    assert "PallouriotissaPallouriotissa" not in {e["zone_name"] for e in ents}


def test_signals_crossings_name_their_streets():
    sig = {s["id"]: s for s in F.parse_signals(text("traffic_signals"))}
    s = sig["signal-0"]
    assert s["code"] == "NIC-085" and s["operator"] == "Nicosia Municipality"
    assert s["streets"] == ["D.Akrita", "Larnakos", "Kennedy"]
    xing = {x["id"]: x for x in F.parse_crossings(text("pedestrian_crossings"))}
    assert xing["xing-0"]["name"] == "Makariou III - Tech. School"
    assert (xing["xing-0"]["street"], xing["xing-0"]["near"]) == ("Makariou III", "Tech. School")
    assert (xing["xing-2"]["street"], xing["xing-2"]["near"]) == ("Lemesou", "Armenias")


def test_lines_read_whatever_their_namespaces():
    lanes = {b["id"]: b for b in F.parse_bus_lanes(text("bus_lanes"))}
    mk = lanes["buslane-11"]  # every coordinate in its own numbered namespace
    assert mk["status"] == "active" and mk["name"] == "Makariou Avenue (Ethnikis Frouras to Griva Digeni)"
    assert len(mk["lines"][0]) == 25 and 33.3 < mk["lines"][0][0][0] < 33.4
    assert lanes["buslane-9"]["status"] == "future" and "tram corridor" in lanes["buslane-9"]["name"]
    cyc = {c["id"]: c for c in F.parse_cycleways(text("cycleways"))}
    assert cyc["cycle-32"]["name"] == "Cycle route, Alexandroupoleos Avenue"
    assert cyc["cycle-35"]["name"] == "Footpath and cycleway, the University of Cyprus to Geri"
    assert len(cyc["cycle-38"]["lines"]) == 17


def test_cameras_say_when_their_position_is_rough():
    cams = {c["id"]: c for c in F.parse_cameras(text("traffic_enforcement_cameras"))}
    c = cams["ecam-30"]
    assert (c["code"], c["street"], c["at"], c["direction"]) == ("11201", "Strovolou Ave", "Machaira St", "northbound")
    assert c["precise"] and c["town"] == "Nicosia"
    assert not cams["ecam-44"]["precise"]  # written as 33.37, 35.15
    assert cams["ecam-80"]["unit"] == "FC1" and cams["ecam-80"]["street"] == "Athalassas"


def test_feeds_read_once_a_day_and_keep_yesterdays_copy(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return fixture_fetch(url)
    feeds = F.Feeds(fetch, cache_dir=tmp_path)
    inv = feeds.get("signals")
    assert len(inv.records) == 161 and inv.island == 163 and inv.copy == "live"
    assert inv.updated == "2025-03-10" and inv.published.startswith("2026-11-26")
    feeds.get("signals")
    assert sum("traffic_signals" in u for u in calls) == 1
    # A day later the feed fails: the saved copy stands in and says why.
    broken = F.Feeds(lambda url: (_ for _ in ()).throw(OSError("timed out")), cache_dir=tmp_path, ttl_s=0)
    inv2 = broken.get("signals")
    assert len(inv2.records) == 161 and inv2.copy == "saved" and "timed out" in inv2.error


def test_a_broken_feed_with_no_copy_reads_as_nothing(tmp_path):
    feeds = F.Feeds(lambda url: "<not xml", cache_dir=tmp_path)
    inv = feeds.get("cameras")
    assert inv.records == [] and inv.copy == "none" and inv.error


def test_greek_names_in_latin():
    assert F.latin("Αγλατζιά") == "Aglantzia"
    assert F.latin("Οδός Μαχαιρά") == "Odos Machaira"
    assert F.english("Leof. Μakariou (Ethnikis Frouras - Griva Digeni)") == "Makariou Avenue (Ethnikis Frouras to Griva Digeni)"


# -- the network and what each sign governs ------------------------------------------------------


def test_network_splits_ways_at_junctions(net):
    assert len(net.links) > 4000
    assert all(link.length > 0.5 for link in net.links)
    # Makariou is found in Greek or English, however it is spelt.
    names = {net.links[i].label for i in net.named("Makariou")}
    assert any("Makari" in (n or "") for n in names)


def test_maxspeed_tags():
    assert N.maxspeed("50") == 50 and N.maxspeed("25 mph") == 40 and N.maxspeed("CY:urban") is None


def test_every_sign_on_a_street_governs_a_stretch(net):
    signs = [s for s in F.parse_speed_limits(text("speed_limits"))
             if 33.35 <= s["lon"] <= 33.395 and 35.15 <= s["lat"] <= 35.175]
    pieces, snaps = N.infer(net, signs)
    assert signs and sum(s is not None for s in snaps) >= 0.9 * len(signs)
    tiers = {p["tier"] for p in pieces}
    assert {"signed", "mapped"} <= tiers
    for p in pieces:
        assert p["length_m"] >= 1 and len(p["coords"]) >= 2
        if p["tier"] == "uncertain":
            assert p["alt_kmh"] and p["alt_kmh"] != p["kmh"]
        if p["tier"] != "mapped":
            assert p["sign"] in {s["id"] for s in signs}
    # No piece is further from its sign than the reach.
    by_id = {s["id"]: s for s in signs}
    for p in pieces:
        if p["sign"]:
            s = by_id[p["sign"]]
            assert min(N.dist_m((s["lon"], s["lat"]), c) for c in p["coords"]) <= N.REACH_M + 50


def test_two_agreeing_signs_sign_the_stretch_between_them():
    # A straight street of 1.2 km with a 30 sign at each end: all of it is signed 30.
    coords = [[33.36 + i * 0.001, 35.16] for i in range(14)]
    ways = [[1, "Test Street", None, "residential", None, False, None, list(range(100, 114)), coords]]
    net = N.Network(ways)
    signs = [{"id": "limit-a", "kmh": 30, "lon": coords[0][0], "lat": 35.1601},
             {"id": "limit-b", "kmh": 30, "lon": coords[-1][0], "lat": 35.1601}]
    pieces, _ = N.infer(net, signs)
    assert {p["tier"] for p in pieces} == {"signed"}
    assert abs(sum(p["length_m"] for p in pieces) - net.links[0].length) < 15


def test_disagreeing_signs_leave_the_middle_uncertain():
    coords = [[33.36 + i * 0.001, 35.16] for i in range(14)]
    ways = [[1, "Test Street", None, "residential", None, False, None, list(range(100, 114)), coords]]
    net = N.Network(ways)
    signs = [{"id": "limit-a", "kmh": 30, "lon": coords[0][0], "lat": 35.1601},
             {"id": "limit-b", "kmh": 50, "lon": coords[-1][0], "lat": 35.1601}]
    pieces, _ = N.infer(net, signs)
    mid = next(p for p in pieces if p["tier"] == "uncertain")
    assert {mid["kmh"], mid["alt_kmh"]} == {30, 50}
    near_a = next(p for p in pieces if p["sign"] == "limit-a" and p["tier"] == "signed")
    assert near_a["kmh"] == 30


def test_a_limit_stops_where_the_street_changes_name():
    a = [[33.36 + i * 0.001, 35.16] for i in range(4)]
    b = [a[-1]] + [[a[-1][0] + (i + 1) * 0.001, 35.16] for i in range(4)]
    ways = [[1, "First Street", None, "residential", None, False, None, [1, 2, 3, 4], a],
            [2, "Other Street", None, "residential", None, False, None, [4, 5, 6, 7, 8], b]]
    net = N.Network(ways)
    pieces, _ = N.infer(net, [{"id": "limit-a", "kmh": 30, "lon": a[0][0], "lat": 35.16}])
    assert {p["street"] for p in pieces} == {"First Street"}


def test_posted_limit_along_a_road_segment(net):
    signs = F.parse_speed_limits(text("speed_limits"))
    pieces, _ = N.infer(net, [s for s in signs if F.in_area(s["lon"], s["lat"])])
    idx = N.PieceIndex(pieces)
    link = max((net.links[i] for i in net.named("Griva Digeni")), key=lambda x: x.length)
    got = idx.along(link.coords)
    assert got is not None and got["kmh"] in (30, 50, 65) and 0 < got["coverage"] <= 1
    assert got["tier"] in ("signed", "likely", "mapped")


# -- the service, its kinds, tools and routes ----------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from chatbot.entities import Entities, EntityNotFound  # noqa: E402
from chatbot.settings import Settings  # noqa: E402
from chatbot.store import SessionStore  # noqa: E402
from chatbot.streets import Streets  # noqa: E402
from chatbot.streets import analysis as A  # noqa: E402
from chatbot.streets import entities as street_kinds  # noqa: E402 - registers the kinds
from chatbot.tools import ToolContext, run_tool  # noqa: E402


@pytest.fixture(scope="module")
def streets(tmp_path_factory):
    raw = json.loads(gzip.decompress((FIX / "osm_streets_central.json.gz").read_bytes()))
    st = Streets(Settings(), fetch=fixture_fetch, network_fetch=lambda q: raw,
                 cache_dir=tmp_path_factory.mktemp("streets"))
    assert st.limits(wait=True) is not None
    return st


def ctx_for(st):
    city = SimpleNamespace(now=lambda: None)
    places = SimpleNamespace(ready=False, geocode=lambda text, limit=3: [
        {"name": "Eleftheria Square", "lat": 35.1705, "lon": 33.3605}])
    return ToolContext(Settings(), city, places, None, SessionStore(), lambda e: None, "c1", streets=st)


def test_nothing_is_fetched_until_the_street_rules_start(tmp_path):
    calls = []
    st = Streets(Settings(), fetch=lambda u: calls.append(u) or "", cache_dir=tmp_path)
    assert st.limits() is None and st.network.current() is None and calls == []
    assert street_kinds.search_items(SimpleNamespace(streets=st)) == [] and calls == []


def test_the_speed_limits_tool(streets):
    ctx = ctx_for(streets)
    res, err = run_tool(ctx, "get_speed_limits", {"road": "Makariou"})
    assert not err, res
    assert res["streets"] and all(r["kmh"] for r in res["streets"]) and "inventory updated" in res["inventory"]
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["streets"] == {"kind": "limits"} and layer["meta"]["geometry"] == "route"
    assert "speed_limits" in layer["meta"]["sources"]
    assert {f["properties"]["layer"] for f in layer["features"]} <= {"limits", "signs"}
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    res, err = run_tool(ctx, "get_speed_limits", {"road": "Nowhere at all"})
    assert err and "no street called" in res["error"]
    res, err = run_tool(ctx, "get_speed_limits", {"near_place": "Eleftheria Square", "radius_m": 300})
    assert not err and res["stretches"] > 0


def test_the_zones_tool_says_they_are_planned(streets):
    ctx = ctx_for(streets)
    res, err = run_tool(ctx, "find_slow_zones", {})
    assert not err and len(res["zones"]) == 3 and all(z["status"] == "planned" for z in res["zones"])
    res, err = run_tool(ctx, "find_slow_zones", {"zone": "Pallouriotissa"})
    assert not err and res["zones"][0]["zone"] == "Zone 30, Pallouriotissa" and res["streets_inside"]
    assert "none" in str(res["signs_inside_by_kmh"])
    res, err = run_tool(ctx, "find_slow_zones", {"zone": "Atlantis"})
    assert err and "Kaimakli" in res["error"]


def test_crossings_near_a_place_and_around_schools(streets):
    ctx = ctx_for(streets)
    res, err = run_tool(ctx, "find_crossings", {"near_lat": 35.1582, "near_lon": 33.3731, "radius_m": 300})
    assert not err and res["crossings"] >= 1 and res["nearest"][0]["distance_m"] < 50
    assert "straight lines" in res["note"]
    res, err = run_tool(ctx, "find_crossings", {"around": "schools"})
    assert not err and res["checked"] > 10 and set(res["by_verdict"]) <= set(A.VERDICTS)
    order = [r["verdict"] for r in res["worst"]]
    assert order == sorted(order, key=["gap", "slow_road", "quiet", "served"].index)
    layer = ctx.store.layers[res["layer_ref"]]
    assert all(f["properties"]["entity_kind"] == "school" for f in layer["features"])


def test_the_cycle_network_and_where_it_breaks(streets):
    ctx = ctx_for(streets)
    res, err = run_tool(ctx, "get_cycle_network", {})
    assert not err and len(res["pieces"]) >= 2 and res["total_km"] > 10
    for g in res["gaps"]:
        assert 0 < g["length_m"] <= A.GAP_M and g["between"][0] != g["between"][1]
    layers = {f["properties"]["layer"] for f in ctx.store.layers[res["layer_ref"]]["features"]}
    assert {"cycleways", "cycle_ends"} <= layers


def test_bus_lanes_and_cameras(streets):
    ctx = ctx_for(streets)
    res, err = run_tool(ctx, "get_bus_lanes", {"status": "active"})
    assert not err and all(r["status"] == "in use" for r in res["lanes"]) and res["in_use_km"] > 5
    res, err = run_tool(ctx, "find_enforcement_cameras", {"road": "Griva Digeni"})
    assert not err and res["cameras"] and "say" in res["checks"]
    placed = {r["id"]: r["placed"] for r in res["cameras"]}
    assert placed["ecam-55"] == "junction" and placed["ecam-44"] == "rough"


def test_every_street_kind_opens_in_the_inspector(streets):
    ents = Entities(None)
    ents.streets = streets
    sign = next(s for s, snap in zip(streets.records("speed_limits"), streets.limits().snaps) if snap)
    for eid, kind in ((sign["id"], "speed_limit"), ("zone-1", "slow_zone"), ("zentry-1", "slow_zone_entrance"),
                      ("signal-0", "traffic_signal"), ("xing-0", "pedestrian_crossing"),
                      ("ecam-55", "enforcement_camera"), ("cycle-38", "cycleway"), ("buslane-11", "bus_lane"),
                      (streets.schools()[0]["id"], "school")):
        e = ents.get(eid)
        assert e["kind"] == kind and e["name"] and e["sources"], eid
        assert e["center"] and e["geometry"]
    cam = ents.get("ecam-55")
    assert any(r.get("id") == "signal-5" for r in cam["relations"])
    zone = ents.get("zone-1")
    assert zone["state"]["value"] == "planned" and zone["readings"][0]["value"] == 30
    mapped = next(p for p in streets.limits().pieces if p["tier"] == "mapped")
    assert ents.get(f"limitway-{mapped['way']}")["state"]["value"] == "mapped"
    with pytest.raises(EntityNotFound):
        ents.get("signal-99999")
    found = ents.search("Kaimakli")
    assert any(r["id"] == "zone-1" for r in found)


def test_inventories_stand_at_every_moment(streets):
    from datetime import datetime, timezone

    from chatbot.entities import AT_RESOLVERS
    ents = Entities(None)
    ents.streets = streets
    e = AT_RESOLVERS["traffic_signal"](ents, ents.get("signal-0"), datetime(2026, 11, 20, 8, tzinfo=timezone.utc))
    assert e["moment"]["known"] and not e["moment"]["applies"] and "every moment" in e["moment"]["note"]


def test_the_layer_endpoints(streets, monkeypatch):
    from fastapi import HTTPException

    from chatbot import server
    from chatbot.routes import streets as routes
    monkeypatch.setattr(server.app.state, "agent", SimpleNamespace(streets=streets), raising=False)
    req = SimpleNamespace(app=server.app)
    for name in routes.LAYERS:
        body = json.loads(routes.layer(name, req).body)
        assert body["features"] and body["meta"]["inventory"]["updated"], name
        assert body["meta"]["status"] == "ready"
    st = routes.status(req)
    assert st["limits"]["ready"] and st["feeds"]["speed_limits"]["count"] == 333
    with pytest.raises(HTTPException):
        routes.layer("nope", req)


def test_sources_name_every_street_inventory(streets):
    from chatbot import sources
    from chatbot.routes.sources import PROBES
    for sid in set(street_kinds.SOURCE_IDS.values()):
        assert sid in sources.BY_ID and sid in PROBES
        st = PROBES[sid](SimpleNamespace(streets=streets))
        assert st["state"] == "live" and "inventory updated" in st["note"]
    for tool in ("get_speed_limits", "find_slow_zones", "find_crossings", "get_cycle_network",
                 "get_bus_lanes", "find_enforcement_cameras"):
        assert set(sources.TOOL_SOURCES[tool]) <= set(sources.BY_ID)


def test_a_road_without_an_estimate_uses_its_posted_limit(tmp_path):
    from chatbot.mobility import roads as roads_mod
    seg = SimpleNamespace(id="road-1", nap_id="1", coords=[(33.36, 35.16), (33.37, 35.16)])
    r = roads_mod.Roads(lambda url: "", cache_dir=tmp_path)
    r._measured = (1e18, {})
    r._reference = {}
    r._reference_running = True  # no Valhalla here
    assert r.free_flow(seg) == (50.0, "assumed")
    r.posted_limit = lambda s: 30.0
    assert r.free_flow(seg) == (30.0, "posted")
    r._reference = {"1": {"kmh": 41.0}}
    assert r.free_flow(seg) == (41.0, "estimated")  # a better estimate wins over the posted limit


def test_school_names_match_best_first():
    from chatbot.tools.streets import rank_schools
    schools = [{"id": "school-n1", "name": "The English Nursery School"}, {"id": "school-w2", "name": "Pascal English School"},
               {"id": "school-w3", "name": "English School"}, {"id": "school-w4", "name": "Philips English School"}]
    assert [s["id"] for s in rank_schools(schools, "the English School")][:2] == ["school-w3", "school-w2"]
    assert rank_schools(schools, "school-w4")[0]["id"] == "school-w4"
    assert rank_schools(schools, "Lyceum") == []

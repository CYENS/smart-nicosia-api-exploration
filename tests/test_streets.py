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

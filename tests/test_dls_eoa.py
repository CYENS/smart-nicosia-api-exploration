"""DLS 3D buildings and parcels, and the EOA dangerous-building registers: parsers against
trimmed captures (20 Oct 2026), geometry helpers, and the registry and tools against a fake
DLS (no network)."""

import json
import math

import pytest

from chatbot import dls as dls_mod
from chatbot import eoa
from chatbot.dls import (DLS, ParcelRef, area_m2, building, centroid, contains, lonlat_to_tile,
                         parcel_refs, tile_bounds)
from chatbot.geo import loose_match
from chatbot.settings import Settings
from chatbot.store import STATE_COLORS, SessionStore
from chatbot.tools import ToolContext, color_states, run_tool

from conftest import load

# -- geometry -----------------------------------------------------------------------------------


def square(lon, lat, half_m):
    dlat = half_m / 110574
    dlon = half_m / (111320 * math.cos(math.radians(lat)))
    ring = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]
    return {"type": "Polygon", "coordinates": [ring]}


def test_tile_math_round_trips():
    x, y = lonlat_to_tile(33.3615, 35.1725)
    w, s, e, n = tile_bounds(x, y)
    assert w <= 33.3615 < e and s <= 35.1725 < n
    assert 400 < (e - w) * 111320 * math.cos(math.radians(35.17)) < 600  # z16 is ~500 m here


def test_centroid_area_and_containment_in_metres():
    sq = square(33.36, 35.17, 10)  # 20 m x 20 m
    c = centroid(sq)
    assert c == pytest.approx((33.36, 35.17), abs=1e-7)
    assert area_m2(sq) == pytest.approx(400, rel=0.01)
    assert contains(sq, 33.36, 35.17) and not contains(sq, 33.3605, 35.17)
    hole = {"type": "Polygon", "coordinates": [square(33.36, 35.17, 10)["coordinates"][0],
                                               square(33.36, 35.17, 4)["coordinates"][0]]}
    assert not contains(hole, 33.36, 35.17)
    assert area_m2(hole) == pytest.approx(400 - 64, rel=0.02)


def test_parcel_refs_normalise_register_codes():
    # Greek Ε where DLS stores Latin E, several plots in one cell, resurvey plans on sheet 0
    refs = parcel_refs("21", "62Ε2", "249, 250 & 251", "1", 1)
    assert [r.plan for r in refs] == ["62E2"] * 3 and [r.parcel for r in refs] == [249, 250, 251]
    assert parcel_refs("5", "2-290-379", "924", "9", 3)[0].sheet == "0"
    assert parcel_refs("21", "46'; DROP", "1") == []  # never interpolate odd text into SQL
    ref = ParcelRef("40", "640302", 74, None, 4)
    assert ref.where() == "(SHEET='40' AND PLAN_NBR='640302' AND PARCEL_NBR=74 AND DIST_CODE=4)"


def test_building_heights_fall_back_to_floors():
    geom = square(33.36, 35.17, 5)
    good = building({"geometry": geom, "properties": {"OBJECTID": 1, "H_ROOF_A_1": 14.18,
                                                      "H_EAVE_B_1": 11.81, "FLOOR_QTY": 5}})
    assert good["id"] == 1 and good["properties"]["h"] == 14.2 and good["properties"]["e"] == 11.8
    bad = building({"geometry": geom, "properties": {"OBJECTID": 2, "H_ROOF_A_1": -999,
                                                     "H_EAVE_B_1": 3, "FLOOR_QTY": 4}})
    assert bad["properties"]["h"] == 12.8 and bad["properties"]["e"] == 3
    assert building({"geometry": None, "properties": {"OBJECTID": 3}}) is None


# -- a fake DLS ---------------------------------------------------------------------------------

LON, LAT = 33.36, 35.17


def b_feature(oid, lon, lat, half, h, floors=None, t="High residential building", area=None):
    return {"type": "Feature", "geometry": square(lon, lat, half), "properties": {
        "OBJECTID": oid, "H_ROOF_A_1": h, "H_EAVE_B_1": h - 2, "FLOOR_QTY": floors,
        "B_TYPE": t, "ROOF_TYPE": "Flat roof", "AREA_sqm": area or (2 * half) ** 2,
        "VOL_CubicM": (area or (2 * half) ** 2) * h, "H_MSL_m_1": 150}}


BUILDINGS = [
    b_feature(1, LON, LAT, 6, 21.0, 7),                     # on plot P1
    b_feature(2, LON + 0.0024, LAT, 60, 30.0, 10, "Church"),  # a block covering plot P2
    b_feature(3, LON + 0.00408, LAT, 9, 9.0, 3),             # spans plot P3 (similar size)
    b_feature(4, LON - 0.003, LAT + 0.001, 5, 3.0, 1),       # nowhere near a plot
]
PARCELS = {
    101: square(LON, LAT, 10),
    102: square(LON + 0.002, LAT, 5),
    103: square(LON + 0.004, LAT, 7),
}


class FakeDLS(DLS):
    def __init__(self, tmp_path):
        super().__init__(Settings(), cache_dir=tmp_path, interval_s=0)
        self.calls = []

    def query(self, layer_url, params):
        self.calls.append((layer_url, params))
        if layer_url == dls_mod.BUILDINGS_URL:
            w, s, e, n = map(float, params["geometry"].split(","))
            hits = [f for f in BUILDINGS if any(w <= x <= e and s <= y <= n
                                                for x, y in f["geometry"]["coordinates"][0])]
            page = hits[params["resultOffset"]:params["resultOffset"] + params["resultRecordCount"]]
            return {"type": "FeatureCollection", "features": json.loads(json.dumps(page))}
        where = params["where"]
        if where.startswith("OBJECTID IN"):
            ids = [int(x) for x in where[where.index("(") + 1:-1].split(",")]
            return {"features": [{"geometry": PARCELS[i], "properties": {"OBJECTID": i}}
                                 for i in ids if i in PARCELS]}
        feats = []
        if "PLAN_NBR='640302'" in where:
            feats.append({"geometry": PARCELS[103], "properties": {
                "OBJECTID": 103, "SHEET": "40", "PLAN_NBR": "640302", "PARCEL_NBR": 74,
                "BLCK_CODE": 2, "DIST_CODE": 4}})
        return {"features": feats}


def test_tiles_are_disjoint_and_cached(tmp_path):
    fake = FakeDLS(tmp_path)
    x, y = lonlat_to_tile(LON, LAT)
    first = fake.tile(x, y)
    w, s, e, n = tile_bounds(x, y)
    assert {b["id"] for b in first} == {b["properties"]["OBJECTID"] for b in BUILDINGS
                                        if w <= centroid(b["geometry"])[0] < e
                                        and s <= centroid(b["geometry"])[1] < n}
    calls = len(fake.calls)
    fresh = FakeDLS(tmp_path)  # a new process reads the disk cache
    assert fresh.tile(x, y) == first and not fresh.calls and len(fake.calls) == calls


def test_buildings_on_a_plot(tmp_path):
    fake = FakeDLS(tmp_path)
    on, mode = fake.buildings_on(PARCELS[101])
    assert mode == "on_parcel" and [b["id"] for b in on] == [1]
    block, mode = fake.buildings_on(PARCELS[102])
    assert mode == "covers_parcel" and [b["id"] for b in block] == [2]
    empty, mode = fake.buildings_on(square(LON - 0.01, LAT, 5))
    assert mode == "none" and empty == []


# -- parsers ------------------------------------------------------------------------------------


def test_parse_nicosia_takes_the_last_coords_and_the_parcel_objectid():
    recs = eoa.parse_nicosia(load("eoa_nicosia_map.html"))
    assert [r["id"] for r in recs] == ["NIC-1", "NIC-2", "NIC-9"]
    assert recs[1]["lat"] == 35.17284 and recs[1]["position"] == "published"  # overridden
    assert recs[0]["_oids"] == [3340578]
    assert recs[0]["municipality"] == "ΝΟΤΙΑ Λ/ΣΙΑ" and recs[0]["community"] == "ΑΛΑΜΠΡΑ"
    assert [r["status"] for r in recs] == ["dangerous", "dangerous", "needs_repair"]
    with pytest.raises(ValueError):
        eoa.parse_nicosia("<html>nothing here</html>")


def test_parse_larnaca_famagusta_limassol():
    data = load("eoa_larnaca_page1.json")
    lar = eoa.larnaca_records(eoa.parse_larnaca_rows(data["data"]["table"]))
    assert [(r["id"], r["sheet"], r["plan"], r["parcel"]) for r in lar][0] == \
        ("LAR-120", "40", "640302", "74")
    assert lar[0]["declared"] == "2026-10-09" and lar[0]["community"] == "Σκάλα"
    assert "ΕΠΙΚΙΝΔΥΝΗ" in lar[0]["instructions"]

    fam = eoa.parse_famagusta(load("eoa_famagusta.html"))  # the menu mentions 15Α first
    assert [r["status"] for r in fam] == ["needs_repair"] * 2 + ["dangerous"] * 2
    assert fam[0]["sheet"] == "0" and fam[0]["plan"] == "2-290-379" and fam[0]["block"] == "9"
    assert fam[0]["community"] == "Παραλίμνι"

    lim = eoa.parse_limassol(json.loads(load("eoa_limassol.geojson")))
    assert lim[0]["id"] == "LIM-166" and lim[0]["address"] == "ΑΓΙΟΥ ΑΝΔΡΕΟΥ 198"
    assert lim[0]["_parcel_geometry"]["type"] == "Polygon" and lim[0]["parish"] == "ΑΓΙΑ ΝΑΠΑ"


def test_a_captcha_page_is_not_a_register():
    assert eoa.human_check("<title>Validation request</title> User validation required "
                           "<img src='/captcha.gif'>")
    assert not eoa.human_check(load("eoa_nicosia_map.html"))


def test_loose_match_across_scripts_and_spellings():
    for latin, greek in [("Strovolos", "ΣΤΡΟΒΟΛΟΣ"), ("Lakatamia", "ΛΑΚΑΤΑΜΕΙΑ"),
                         ("Ayios Andreas", "ΑΓΙΟΣ ΑΝΔΡΕΑΣ"), ("Ledras", "ΛΗΔΡΑΣ"),
                         ("Makariou", "ΜΑΚΑΡΙΟΥ"), ("Engomi", "ΕΓΚΩΜΗ"),
                         ("Lefkosia", "ΛΕΥΚΩΣΙΑ"), ("Paralimni", "Παραλίμνι")]:
        assert loose_match(latin, greek), (latin, greek)
    assert not loose_match("Strovolos", "ΛΑΚΑΤΑΜΕΙΑ") and not loose_match("", "x")


# -- registry -----------------------------------------------------------------------------------


def records_for_registry():
    nic = {"id": "NIC-1", "eoa": "nicosia", "status": "dangerous", "community": "ΣΤΡΟΒΟΛΟΣ",
           "municipality": "ΣΤΡΟΒΟΛΟΣ", "address": "ΣΤΑΣΙΝΟΥ 44", "sheet": "21",
           "plan": "550403", "block": "1", "parcel": "902", "lat": LAT, "lon": LON,
           "position": "published", "_oids": [101]}
    old_town = {**nic, "id": "NIC-2", "community": "ΛΕΥΚΩΣΙΑ", "municipality": "ΛΕΥΚΩΣΙΑ",
                "address": "ΟΝΑΣΑΓΟΡΟΥ 32",
                "lat": LAT, "lon": LON + 0.002, "_oids": [102]}
    lar = {"id": "LAR-120", "eoa": "larnaca", "status": "dangerous", "community": "Σκάλα",
           "address": "Γρηγόρη Αυξεντίου", "sheet": "40", "plan": "640302", "block": None,
           "parcel": "74", "lat": None, "lon": None, "position": None}
    return [nic, old_town], [lar]


def make_registry(tmp_path, monkeypatch, fail_larnaca=False):
    nic, lar = records_for_registry()

    def larnaca(s):
        if fail_larnaca:
            raise ValueError("eoal.org.cy asked for a human check (CAPTCHA); not retried")
        return [dict(r) for r in lar]

    monkeypatch.setattr(eoa, "SEED_PATH", tmp_path / "seed.json")
    (tmp_path / "seed.json").write_text(json.dumps({"captured": "2026-10-20", "records": lar}),
                                        encoding="utf-8")
    reg = eoa.Registry(Settings(), FakeDLS(tmp_path), cache_dir=tmp_path,
                       fetchers={"nicosia": lambda s: [dict(r) for r in nic], "larnaca": larnaca})
    reg.refresh_if_needed()
    return reg


def test_registry_links_parcels_and_3d_heights(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    recs = {r["id"]: r for r in reg.records()}
    assert reg.linked_3d and set(recs) == {"NIC-1", "NIC-2", "LAR-120"}
    assert recs["NIC-1"]["height_m"] == 21.0 and recs["NIC-1"]["floors"] == 7
    assert recs["NIC-1"]["match_3d"] == "on_parcel"
    # a 120 m block covering a 10 m plot: the plot at the block's eave, floors unknown
    assert recs["NIC-2"]["match_3d"] == "block" and recs["NIC-2"]["height_estimated"]
    assert recs["NIC-2"]["height_m"] == 28.0 and recs["NIC-2"].get("floors") is None
    # Larnaca gives no coordinates: the parcel's centre places it; a similar-size building
    # covering the plot is the building on it
    assert recs["LAR-120"]["position"] == "parcel"
    assert recs["LAR-120"]["match_3d"] == "spans_parcel" and recs["LAR-120"]["height_m"] == 9.0
    assert not any(k.startswith("_") for r in recs.values() for k in r)
    assert json.loads((tmp_path / "eoa_dangerous.json").read_text(encoding="utf-8"))["linked_3d"]


def test_registry_falls_back_to_the_seed(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch, fail_larnaca=True)
    src = reg.sources["larnaca"]
    assert src["copy"] == "seed" and src["as_of"] == "2026-10-20" and "CAPTCHA" in src["live_error"]
    assert any(r["id"] == "LAR-120" for r in reg.records())


def test_layer_features_draw_each_building_once(tmp_path, monkeypatch):
    reg = make_registry(tmp_path, monkeypatch)
    recs = reg.records()
    twin = {**next(r for r in recs if r["id"] == "NIC-1"), "id": "NIC-1b"}  # listed twice
    feats = eoa.layer_features(recs + [twin], STATE_COLORS)
    kinds = [(f["properties"].get("_kind"), f["properties"]["id"]) for f in feats]
    assert kinds.count(("footprint", "NIC-1")) == 1 and ("footprint", "NIC-1b") not in kinds
    assert (None, "NIC-1b") in kinds  # but its marker is there
    block = next(f for f in feats if f["properties"]["id"] == "NIC-2"
                 and f["properties"].get("_kind") == "footprint")
    assert block["properties"]["ghost_bid"] == 2 and "bid" not in block["properties"]


# -- tools --------------------------------------------------------------------------------------


def tool_ctx(tmp_path, monkeypatch, events):
    reg = make_registry(tmp_path, monkeypatch)
    return ToolContext(Settings(), None, None, None, SessionStore(), events.append, "c1",
                       reg.dls, reg)


def test_find_dangerous_buildings_filters_and_layers(tmp_path, monkeypatch):
    events = []
    ctx = tool_ctx(tmp_path, monkeypatch, events)
    res, err = run_tool(ctx, "find_dangerous_buildings", {"area": "Strovolos"})
    assert not err, res
    assert res["matched"] == 1 and res["items"][0]["id"] == "NIC-1"
    assert res["height_m"]["max"] == 21.0 and res["registers"]["larnaca"]["copy"] == "live"
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["geometry"] == "buildings"
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    res, err = run_tool(ctx, "find_dangerous_buildings", {"near_lat": LAT, "near_lon": LON,
                                                          "radius_m": 500})
    assert [i["id"] for i in res["items"]] == ["NIC-1", "NIC-2", "LAR-120"]  # nearest first
    assert res["items"][0]["distance_m"] == 0
    res, err = run_tool(ctx, "find_dangerous_buildings", {"min_floors": 8})
    assert res["matched"] == 0
    ok, err = run_tool(ctx, "show_on_map", {"layer_ref": res["layer_ref"], "style": "buildings_3d"})
    assert not err and events[-1]["layer"]["style"] == "buildings_3d"


def test_get_buildings_3d_and_city_3d(tmp_path, monkeypatch):
    events = []
    ctx = tool_ctx(tmp_path, monkeypatch, events)
    res, err = run_tool(ctx, "get_buildings_3d", {"near_lat": LAT, "near_lon": LON,
                                                  "radius_m": 500})
    assert not err, res
    assert res["buildings"] == 4 and res["tallest"][0]["height_m"] == 30.0
    assert res["by_height_band"]["20-35 m"] == 2
    assert {d["id"] for d in res["on_dangerous_registers"]} == {"NIC-1", "LAR-120"}
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert next(r for r in rows if r["id"] == "B1")["register"] == "dangerous"
    res, err = run_tool(ctx, "map_fly_to", {"lat": LAT, "lon": LON, "zoom": 16.5,
                                            "pitch": 60, "city_3d": True})
    assert [e["action"] for e in events[-2:]] == ["city_3d", "fly_to"]
    assert events[-2]["on"] is True
    res, err = run_tool(ctx, "show_on_map", {"layer_ref": ctx.store.add_layer(
        "x", [], geometry="point", title="x"), "style": "buildings_3d"})
    assert err and "point layer" in res["error"]


def test_charts_colour_status_and_height_bands_like_the_map():
    spec = {"mark": "bar", "encoding": {"color": {"field": "status"}}}
    color_states(spec, [{"status": "needs_repair"}, {"status": "dangerous"}])
    assert spec["encoding"]["color"]["scale"] == {
        "domain": ["dangerous", "needs_repair"],
        "range": [STATE_COLORS["dangerous"], STATE_COLORS["needs_repair"]]}
    spec = {"mark": "bar", "encoding": {"color": {"field": "height_band"}}}
    color_states(spec, [{"height_band": "35 m and up"}, {"height_band": "under 6 m"}])
    assert spec["encoding"]["color"]["scale"]["domain"] == ["under 6 m", "35 m and up"]


# -- where am I, "most dangerous", and safer routes ------------------------------------------


def test_my_location_is_a_place_once_shared(tmp_path, monkeypatch):
    from chatbot.agent import format_context, user_location
    from chatbot.tools import resolve_place
    from conftest import utc

    ctx = tool_ctx(tmp_path, monkeypatch, [])
    res, err = run_tool(ctx, "where_am_i", {})
    assert err and "locate button" in res["error"]
    ctx.user_location = user_location({"user_location": {"lat": LAT, "lon": LON,
                                                         "source": "set on the map"}})
    assert resolve_place(ctx, "My location")["lat"] == LAT
    assert resolve_place(ctx, "εδώ")["lon"] == LON
    res, err = run_tool(ctx, "where_am_i", {"radius_m": 200})
    assert not err, res
    assert [r["id"] for r in res["nearest_dangerous"]] == ["NIC-1", "NIC-2"]
    assert res["nearest_dangerous"][0]["distance_m"] == 0
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["features"][0]["properties"]["id"] == "you"
    text = format_context({"user_location": {"lat": LAT, "lon": LON, "accuracy_m": 12,
                                             "source": "device location"}}, utc(2026, 10, 20, 9))
    assert "user_location: 35.17000, 33.36000 (device location, ±12 m)" in text
    assert "user_location: not shared" in format_context({}, utc(2026, 10, 20, 9))
    assert user_location({"user_location": {"lat": "x", "lon": 1}}) is None


def test_most_dangerous_sorts(tmp_path, monkeypatch):
    ctx = tool_ctx(tmp_path, monkeypatch, [])
    res, _ = run_tool(ctx, "find_dangerous_buildings", {"sort": "height"})
    # NIC-2's 28 m is its old-town block's eave (estimated), NIC-1 21 m, LAR-120 9 m
    assert [i["id"] for i in res["items"]] == ["NIC-2", "NIC-1", "LAR-120"]
    assert res["sorted_by"] == "height"
    res, err = run_tool(ctx, "find_dangerous_buildings", {"sort": "distance"})
    assert err and "needs near_place" in res["error"]
    res, _ = run_tool(ctx, "find_dangerous_buildings", {"sort": "volume"})
    assert res["items"][0]["id"] == "NIC-1"  # the only plot with a measured volume ranks first


def test_avoid_zone_and_distance_to_a_path():
    from chatbot.dls import distance_to_path_m

    rec = {"parcel_geometry": PARCELS[101], "lat": LAT, "lon": LON}
    zone = eoa.avoid_zone(rec, margin_m=10)
    assert zone[0] == zone[-1] and len(zone) == 5
    assert area_m2({"type": "Polygon", "coordinates": [zone]}) == pytest.approx(40 * 40, rel=0.02)
    assert eoa.anchor(rec) == pytest.approx((LON, LAT), abs=1e-7)
    path = [[LON - 0.001, LAT + 0.0002], [LON + 0.001, LAT + 0.0002]]  # 22 m north of the plot
    assert distance_to_path_m(path, LON, LAT) == pytest.approx(22.1, abs=0.3)


class FakeRouter:
    """Shortest path runs right past NIC-1; with zones to avoid, it swings 100 m north."""

    def __init__(self):
        self.avoid_calls = []

    def route(self, stops, mode, prefer, avoid=None):
        (lat1, lon1), (lat2, lon2) = stops[0], stops[-1]
        if avoid:
            self.avoid_calls.append(avoid)
            mid = [[LON, LAT + 0.0009]]
            km, minutes = 1.1, 13.0
        else:
            mid = [[LON, LAT + 0.0001]]
            km, minutes = 0.9, 11.0
        coords = [[lon1, lat1], *mid, [lon2, lat2]]
        return {"distance_km": km, "duration_min": minutes, "coordinates": coords,
                "legs": [{"distance_km": km, "duration_min": minutes, "directions": []}]}


def test_plan_route_avoids_dangerous_buildings(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from conftest import utc

    ctx = tool_ctx(tmp_path, monkeypatch, [])
    ctx.router = FakeRouter()
    ctx.city = SimpleNamespace(now=lambda: utc(2026, 10, 20, 9))
    stops = [{"lat": LAT, "lon": LON - 0.004}, {"lat": LAT, "lon": LON + 0.0012}]
    res, err = run_tool(ctx, "plan_route", {"stops": stops, "mode": "walk"})
    assert not err, res
    assert res["dangerous_passed"] == 1 and res["dangerous_along_route"][0]["id"] == "NIC-1"
    assert "avoidance" not in res

    res, err = run_tool(ctx, "plan_route", {"stops": stops, "mode": "walk",
                                            "prefer": ["avoid_dangerous_buildings"]})
    assert not err, res
    assert res["avoidance"]["avoided"] == 1 and res["avoidance"]["minutes_added"] == 2.0
    assert res["avoidance"]["usual_route"]["dangerous_passed"] == 1
    assert res["avoidance"]["this_route"]["dangerous_passed"] == 0
    assert res["dangerous_passed"] == 0 and res["travel_km"] == 1.1
    zone = ctx.router.avoid_calls[0][0]
    assert contains({"type": "Polygon", "coordinates": [zone]}, LON, LAT)
    feats = ctx.store.layers[res["layer_ref"]]["features"]
    assert [f["properties"]["state"] for f in feats[:2]] == ["usual route", "route"]
    assert any(f["properties"].get("n") == "!" and f["properties"]["id"] == "NIC-1" for f in feats)


class DetourRouter(FakeRouter):
    """Avoiding NIC-1 swings the route east past NIC-2 and LAR-120 as well: worse."""

    def route(self, stops, mode, prefer, avoid=None):
        res = super().route(stops, mode, prefer, avoid)
        if avoid:
            east = [[LON + 0.002, LAT + 0.0001], [LON + 0.004, LAT + 0.0001]]
            res["coordinates"] = [res["coordinates"][0], [LON, LAT + 0.0001], *east,
                                  res["coordinates"][-1]]
        return res


def test_plan_route_keeps_the_usual_route_when_detours_pass_more(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from conftest import utc

    ctx = tool_ctx(tmp_path, monkeypatch, [])
    ctx.router = DetourRouter()
    ctx.city = SimpleNamespace(now=lambda: utc(2026, 10, 20, 9))
    stops = [{"lat": LAT, "lon": LON - 0.004}, {"lat": LAT, "lon": LON + 0.0012}]
    res, err = run_tool(ctx, "plan_route", {"stops": stops, "mode": "walk",
                                            "prefer": ["avoid_dangerous_buildings"]})
    assert not err, res
    assert ctx.router.avoid_calls  # it tried, and the detours passed 3
    assert res["dangerous_passed"] == 1 and res["travel_km"] == 0.9
    assert "usual route" in res["avoidance"]["note"]
    feats = ctx.store.layers[res["layer_ref"]]["features"]
    assert [f["properties"]["state"] for f in feats[:1]] == ["route"]


def test_namesakes_prefer_the_mapped_landmark_then_the_nearest():
    from chatbot.geo import Places, rank_namesakes

    gate = [{"name": "Famagusta Gate", "category": "attraction",  # a stray node 2.5 km east
             "lat": 35.172799, "lon": 33.398607},
            {"name": "Famagusta Gate", "category": "park", "lat": 35.17303, "lon": 33.371542},
            {"name": "Famagusta Gate", "category": "historic_site",
             "lat": 35.174238, "lon": 33.371194}]
    east = (35.145, 33.41)  # nearer the stray than the gate
    assert rank_namesakes(gate, east)[-1]["category"] == "attraction"
    cafes = [{"name": "Starbucks", "category": "cafe", "lat": 35.163139, "lon": 33.358759},
             {"name": "Starbucks", "category": "cafe", "lat": 35.172142, "lon": 33.36121}]
    assert rank_namesakes(cafes, (35.1707, 33.36))[0]["lat"] == 35.172142
    assert rank_namesakes(cafes, (35.16, 33.358))[0]["lat"] == 35.163139

    places = Places(Settings())
    places._places = gate
    assert places.geocode("famagusta gate")[0]["kind"] in ("park", "historic_site")
    assert places.named("Famagusta Gate", east)[0]["lon"] < 33.38

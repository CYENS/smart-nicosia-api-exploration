"""The 3D city's vector tiles (chatbot/citytiles.py): the vector tile format (chatbot/mvt.py),
OpenStreetMap's buildings read from a base map tile (chatbot/osm3d.py, against the building
layer of a real OpenFreeMap z14 tile over Nicosia, 27 Nov 2026), and the tiles made from a fake
DLS and a fake OpenFreeMap (no network)."""

import asyncio
import gzip
import json
import math
import re
import urllib.parse

import pytest
from fastapi import FastAPI

from chatbot import citytiles, mvt, osm3d
from chatbot.citytiles import CityTiles, project, units_per_m
from chatbot.dls import DLS, building, centroid, lonlat_to_tile, tile_bounds
from chatbot.osm3d import OsmBuildings
from chatbot.routes import buildings as building_routes
from chatbot.settings import ROOT, Settings

from conftest import FIXTURES

# -- the vector tile format ---------------------------------------------------------------------


def test_tiles_round_trip_ids_properties_and_rings():
    outer = [(10, 10), (110, 10), (110, 110), (10, 110)]
    hole = [(40, 40), (40, 60), (60, 60), (60, 40)]
    other = [(200, 200), (300, 200), (300, 300)]
    feats = [mvt.Feature([outer, hole, other], {"h": 12.5, "src": "osm", "f": 3, "x": True}, 77),
             mvt.Feature([[(5, 5), (9, 5), (9, 9)]], {"h": 4.0})]
    layer = mvt.decode(mvt.encode_layer("buildings", feats))["buildings"]
    assert layer.extent == 4096 and len(layer.features) == 2
    first = layer.features[0]
    assert first.id == 77 and first.properties == {"h": 12.5, "src": "osm", "f": 3, "x": True}
    assert first.rings == [outer, hole, other]
    assert layer.features[1].id is None and layer.features[1].properties == {"h": 4.0}
    # an outer ring turns one way, its hole the other: two polygons, the first with its hole
    assert mvt.area2(outer) > 0 > mvt.area2(hole)
    assert mvt.polygons(first.rings) == [[outer, hole], [other]]


def test_simplify_keeps_the_shape_and_drops_what_lies_on_a_line():
    # a 100-unit square with a point every 10 units along its edges, and a small bump
    ring = [(float(x), 0.0) for x in range(0, 100, 10)] + [(100.0, float(y)) for y in range(0, 100, 10)]
    ring += [(float(x), 100.0) for x in range(100, 0, -10)] + [(0.0, float(y)) for y in range(100, 0, -10)]
    ring[3] = (30.0, 0.4)  # within the tolerance: goes
    out = mvt.simplify(ring, 1.0)
    assert sorted(out) == [(0.0, 0.0), (0.0, 100.0), (100.0, 0.0), (100.0, 100.0)]
    assert mvt.simplify(ring, 0) == ring


def test_clip_cuts_a_ring_to_the_box():
    ring = [(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)]
    cut = mvt.cleaned(mvt.clip(ring, 0, 0, 4096, 4096))
    assert sorted(cut) == [(0, 0), (0, 50), (50, 0), (50, 50)]
    assert mvt.clip([(5000.0, 5000.0), (5100.0, 5000.0), (5100.0, 5100.0)], 0, 0, 4096, 4096) == []


# -- OpenStreetMap's buildings from the base map's tiles ------------------------------------


def osm_fixture() -> bytes:
    return (FIXTURES / "ofm_building_14_9710_6481.pbf").read_bytes()


def test_osm_buildings_one_polygon_each_cut_to_the_tile():
    layer = mvt.decode(osm_fixture())["building"]
    merged = sum(len(mvt.polygons(f.rings)) for f in layer.features)
    found = osm3d.buildings_of(layer)
    # OpenMapTiles merges the buildings of one height into a feature: each comes back alone
    assert len(layer.features) < 20 and 900 < len(found) <= merged
    for b in found:
        pts = [b["r"][0][i:i + 2] for i in range(0, len(b["r"][0]), 2)]
        assert all(0 <= x <= 4096 and 0 <= y <= 4096 for x, y in pts)
        assert mvt.area2([tuple(p) for p in pts]) > 0 and b["a"] > 0
        assert b["h"] > 0 and 0 <= b["b"] < b["h"]
    assert {b["h"] for b in found} >= {5.0}  # no height mapped: OpenMapTiles draws 5 m


def test_osm_tiles_are_read_once_and_kept(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(osm3d, "get_json", lambda url, **kw: asked.append(url) or {
        "tiles": ["https://tiles.example/planet/20261122/{z}/{x}/{y}.pbf"]})
    monkeypatch.setattr(osm3d, "get_bytes", lambda url, **kw: asked.append(url) or osm_fixture())
    osm = OsmBuildings(Settings(), cache_dir=tmp_path, interval_s=0)
    assert osm.tile(9710, 6481) is None and not osm.fresh(9710, 6481)
    got = osm.fetch(9710, 6481)
    assert asked[-1] == "https://tiles.example/planet/20261122/14/9710/6481.pbf"
    again = OsmBuildings(Settings(), cache_dir=tmp_path)  # a new process reads the disk copy
    assert again.fresh(9710, 6481) and again.tile(9710, 6481) == got


# -- a small city: a fake DLS and a fake OpenFreeMap -----------------------------------------

X, Y = lonlat_to_tile(33.3615, 35.1725)  # the z16 tile in the old town
W, S, E, N = tile_bounds(X, Y)
CELL_LON, CELL_LAT = (E - W) / 4, (N - S) / 4


def at_cell(col: int, row: int, fx: float = 0.5, fy: float = 0.5) -> tuple[float, float]:
    """A point in one of the tile's 4 x 4 z18 cells (row 0 is the north)."""
    return W + (col + fx) * CELL_LON, N - (row + fy) * CELL_LAT


def square(lon: float, lat: float, half_m: float, n: int = 4) -> list[list[float]]:
    """A closed ring around a point: a square 2 x half_m across, or a circle of that radius
    drawn with n points."""
    dlat = half_m / 110574
    dlon = half_m / (111320 * math.cos(math.radians(lat)))
    if n == 4:
        pts = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat], [lon - dlon, lat + dlat]]
    else:
        pts = [[lon + dlon * math.cos(2 * math.pi * k / n), lat + dlat * math.sin(2 * math.pi * k / n)]
               for k in range(n)]
    return pts + [pts[0]]


def dls_feature(oid: int, ring: list[list[float]], h: float) -> dict:
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"OBJECTID": oid, "H_ROOF_A_1": h, "H_EAVE_B_1": h, "FLOOR_QTY": 3}}


SHED = 9  # a tiny DLS building in a cell OpenStreetMap maps in full
DLS_BUILDINGS = [
    dls_feature(1, square(*at_cell(0, 0, 0.3, 0.3), 5), 14.0),  # cell 0: the DLS's
    dls_feature(2, square(*at_cell(0, 0, 0.7, 0.7), 5), 9.0),
    dls_feature(3, square(*at_cell(1, 1), 30, n=48), 30.0),  # a big round one, tall
    dls_feature(SHED, square(*at_cell(2, 1), 1), 3.0),  # cell 6: 4 m² against OSM's 400
]
OSM_BUILDINGS = [  # (lon, lat, half size in m, height)
    (*at_cell(0, 0, 0.5, 0.5), 6, 5.0),  # in cell 0, where the DLS has buildings: hidden
    (*at_cell(3, 3), 10, 8.0),  # cell 15, no DLS there: drawn
    (*at_cell(2, 1), 10, 5.0),  # cell 6: drawn, the shed hidden
]


class FakeDls(DLS):
    def __init__(self, tmp_path):
        super().__init__(Settings(), cache_dir=tmp_path, interval_s=0)
        self.fetched = []

    def _fetch_tile(self, x, y):
        self.fetched.append((x, y))
        w, s, e, n = tile_bounds(x, y)
        out = []
        for f in DLS_BUILDINGS:
            b = building(f)
            c = centroid(b["geometry"])
            if w <= c[0] < e and s <= c[1] < n:
                out.append(b)
        return out


def osm_tile_bytes(x14: int, y14: int) -> bytes:
    """An OpenMapTiles-like building layer for one z14 tile, from OSM_BUILDINGS."""
    feats = []
    for lon, lat, half, h in OSM_BUILDINGS:
        ring = [project(p[0], p[1], 14, x14, y14) for p in square(lon, lat, half)]
        if not all(0 <= px <= 4096 and 0 <= py <= 4096 for px, py in ring):
            continue
        feats.append(mvt.Feature([mvt.oriented(mvt.cleaned(ring), outer=True)],
                                 {"render_height": h, "render_min_height": 0}))
    return mvt.encode_layer("building", feats)


@pytest.fixture
def city(tmp_path, monkeypatch):
    monkeypatch.setattr(osm3d, "get_json", lambda url, **kw: {"tiles": ["https://ofm/{z}/{x}/{y}.pbf"]})

    def get_bytes(url, **kw):
        z, x, y = map(int, re.search(r"/(\d+)/(\d+)/(\d+)\.pbf$", url).groups())
        return osm_tile_bytes(x, y)

    monkeypatch.setattr(osm3d, "get_bytes", get_bytes)
    w, s, e, n = tile_bounds(X >> 4, Y >> 4, 12)  # greater Nicosia, shrunk to one z12 tile
    fill = (w + 1e-6, s + 1e-6, e - 1e-6, n - 1e-6)
    tiles = CityTiles(FakeDls(tmp_path), OsmBuildings(Settings(), cache_dir=tmp_path, interval_s=0),
                      cache_dir=tmp_path, fill_bbox=fill, center=(33.3615, 35.1725))
    tiles._scan()
    return tiles


def read_all(tiles: CityTiles) -> None:
    """Let the background readers read everything they would (here, in this thread)."""
    while tiles._read_one("osm"):
        pass
    while tiles._read_one("dls"):
        pass


def features(data: bytes) -> list[mvt.Feature]:
    layer = mvt.decode(gzip.decompress(data)).get(citytiles.LAYER)
    return layer.features if layer else []


def test_each_cell_is_drawn_from_the_dls_or_from_openstreetmap(city):
    read_all(city)
    built = city.get(16, X, Y)
    assert built.final and built.complete(16)
    feats = features(built.data)
    dls = {f.id for f in feats if "src" not in f.properties}
    osm = [f for f in feats if f.properties.get("src") == "osm"]
    assert dls == {1, 2, 3}  # the shed's cell is OpenStreetMap's
    assert {f.properties["h"] for f in osm} == {8.0, 5.0}  # not the one where the DLS is
    for f in feats:
        assert mvt.area2(f.rings[0]) > 0  # outer rings as the format asks


def test_a_tile_still_waiting_comes_back_at_once_and_asks_for_what_it_misses(city):
    data, complete = city.tile(16, X, Y)
    assert not complete and features(data) == [] and city.max_age(complete) == citytiles.PARTIAL_MAX_AGE_S
    assert (X, Y) in city._dls_wanted and (X >> 2, Y >> 2) in city._osm_wanted
    assert not city.dls.fetched  # nothing was read while the map waited
    read_all(city)
    data, complete = city.tile(16, X, Y)
    assert complete and len(features(data)) == 5 and city.max_age(complete) == citytiles.FINAL_MAX_AGE_S
    assert (city.dir / "16" / f"{X}_{Y}.mvt.gz").exists()  # kept on disk, as served


def test_wider_tiles_simplify_and_drop_small_buildings(city):
    read_all(city)
    near = {f.id: f for f in features(city.get(16, X, Y).data)}
    far = features(city.get(12, X >> 4, Y >> 4).data)
    assert {f.id for f in far if f.id is not None} == {3}  # small and low ones go far out
    assert sum(1 for f in far if f.properties.get("src") == "osm") == 2  # 400 m² each: kept
    round_far = next(f for f in far if f.id == 3)
    assert len(round_far.rings[0]) < len(near[3].rings[0]) / 2  # the round one, simplified
    upm16, upm12 = units_per_m(16, Y), units_per_m(12, Y >> 4)
    a16 = mvt.area2(near[3].rings[0]) / 2 / upm16 ** 2
    a12 = mvt.area2(round_far.rings[0]) / 2 / upm12 ** 2
    assert a16 == pytest.approx(math.pi * 30 ** 2, rel=0.05) and a12 == pytest.approx(a16, rel=0.25)


def test_tiles_are_made_again_when_their_buildings_change(city, monkeypatch):
    read_all(city)
    for z in range(12, 17):
        assert city.get(z, X >> (16 - z), Y >> (16 - z)).final
    city._changed([(X, Y)])
    assert not (city.dir / "16" / f"{X}_{Y}.mvt.gz").exists()
    assert city.get(16, X, Y).final  # made again at once: it is cheap
    wide = city.get(12, X >> 4, Y >> 4)
    assert wide.stale and not wide.complete(12)  # served as it was, asked for again soon
    monkeypatch.setattr(citytiles, "REBUILD_AFTER_S", {z: 0.0 for z in range(12, 17)})
    assert city.get(12, X >> 4, Y >> 4).final


def test_the_fill_reads_what_is_looked_at_first_then_from_the_centre_out(city):
    first = city._pick("dls")
    assert first == (X, Y)  # the centre of the (shrunk) city
    far = (X + 1, Y + 1)
    city._dls_wanted[far] = 1e12
    assert city._pick("dls") == far
    city._dls_known.add(far)
    assert city._pick("dls") == (X, Y)


def test_outside_greater_nicosia_the_dls_is_asked_for_close_up_only(city):
    away = lonlat_to_tile(33.03, 34.68)  # Limassol
    for z, wants in ((13, False), (15, True)):
        city._dls_wanted.clear()
        x, y = away[0] >> (16 - z), away[1] >> (16 - z)
        _, complete = city.tile(z, x, y)
        assert bool(city._dls_wanted) is wants
    assert city.get(16, 0, 0) is citytiles.EMPTY  # off Cyprus: nothing to ask for


def test_warm_makes_the_whole_pyramid(city):
    read_all(city)
    assert city.warm()
    shift = 16 - 12
    assert (city.dir / "12" / f"{X >> shift}_{Y >> shift}.mvt.gz").exists()
    assert city.status()["fill"] == {"done": 256, "total": 256, "osm_done": 16, "osm_total": 16}


# -- the routes -------------------------------------------------------------------------------


def asgi_get(app, path, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    path, _, query = path.partition("?")
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": query.encode(), "headers": [(k.encode(), v.encode()) for k, v in headers],
             "http_version": "1.1", "scheme": "http", "server": ("test", 80),
             "client": ("test", 1), "root_path": ""}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, body


def test_the_routes_serve_zipped_tiles_with_their_max_age(city):
    read_all(city)
    app = FastAPI()
    app.include_router(building_routes.router)
    app.state.city_tiles = city
    status, head, body = asgi_get(app, f"/api/buildings3d/16/{X}/{Y}.mvt?v=1", [("accept-encoding", "gzip")])
    assert status == 200 and head["content-encoding"] == "gzip"
    assert head["content-type"] == "application/vnd.mapbox-vector-tile"
    assert head["cache-control"] == f"public, max-age={citytiles.FINAL_MAX_AGE_S}"
    assert len(features(body)) == 5
    status, head, body = asgi_get(app, f"/api/buildings3d/15/{X >> 1}/{Y >> 1}.mvt")
    assert status == 200 and "content-encoding" not in head and mvt.decode(body)[citytiles.LAYER]
    assert asgi_get(app, "/api/buildings3d/11/1213/810.mvt")[0] == 404
    status, _, body = asgi_get(app, "/api/buildings3d.json")
    tilejson = json.loads(body)
    assert status == 200 and tilejson["minzoom"] == 12 and tilejson["maxzoom"] == 16
    assert tilejson["tiles"][0].endswith(f"/api/buildings3d/{{z}}/{{x}}/{{y}}.mvt?v={citytiles.VERSION}")
    status, head, body = asgi_get(app, "/api/buildings3d/status")
    assert status == 200 and json.loads(body)["fill"]["done"] == 256 and head["cache-control"] == "no-store"


def test_the_page_asks_for_this_version_of_the_tiles():
    engine = (ROOT / "chatbot" / "web" / "src" / "lib" / "map" / "engine.ts").read_text(encoding="utf-8")
    url = re.search(r'const CITY_TILE_URL = "([^"]+)"', engine).group(1)
    assert url == f"/api/buildings3d/{{z}}/{{x}}/{{y}}.mvt?v={citytiles.VERSION}"
    assert urllib.parse.urlsplit(url).path.endswith(".mvt")
    assert f'const CITY_LAYER = "{citytiles.LAYER}"' in engine

"""Trees for the 3D city (chatbot/trees.py and its route), offline: a trimmed Overpass answer for
greater Nicosia captured 26 Nov 2026 stands in for OpenStreetMap."""

import asyncio
import json
import math
from types import SimpleNamespace

from fastapi import FastAPI

from chatbot.dls import lonlat_to_tile
from chatbot.routes import trees as tree_routes
from chatbot.settings import Settings
from chatbot.trees import ESTIMATED, KINDS, LEAVES, ROW_STEP_M, Trees, leaf_of, parse, query
from chatbot.web import WebError
from conftest import load

OSM = load("osm_trees_nicosia.json")


def metres(a, b):
    k = math.cos(math.radians(a["lat"]))
    return math.hypot((a["lon"] - b["lon"]) * 111320 * k, (a["lat"] - b["lat"]) * 110574)


def test_every_kind_of_tree_osm_gives():
    trees = parse(OSM["elements"])
    kinds = {t["kind"] for t in trees}
    assert kinds == {"mapped", "row", "park", "orchard", "wood"}
    mapped = [t for t in trees if t["kind"] == "mapped"]
    assert len(mapped) == 4 and all(t["osm"].startswith("n") for t in mapped)
    tagged = [t for t in mapped if t["osm"] in ("n12917495241", "n12917495242")]
    assert all(t["h"] == 3.0 and t["r"] < 2.8 for t in tagged)  # tagged 3 m: a small crown too
    plain = [t for t in mapped if t["osm"] == "n8446705484"]
    assert plain[0]["h"] == 7.0 and plain[0]["leaf"] == "broadleaved"  # the usual street tree


def test_a_row_has_a_tree_every_8_m():
    row = [t for t in parse(OSM["elements"]) if t["kind"] == "row"]
    assert len(row) >= 3
    gaps = [metres(a, b) for a, b in zip(row, row[1:])]
    assert all(abs(g - ROW_STEP_M) < 0.5 for g in gaps if g < ROW_STEP_M * 1.5)


def test_areas_are_planted_inside_and_marked_estimated():
    trees = parse(OSM["elements"])
    for kind in ESTIMATED:
        some = [t for t in trees if t["kind"] == kind]
        assert some, kind
    orchard = [t for t in trees if t["kind"] == "orchard"]
    assert all(t["h"] <= 5 for t in orchard)  # olive and citrus, not forest
    # The same answer always gives the same trees (no randomness between restarts).
    assert parse(OSM["elements"]) == trees


def test_an_estimated_tree_never_stands_on_a_mapped_one():
    mapped = {"type": "node", "id": 1, "lat": 35.17, "lon": 33.36, "tags": {"natural": "tree"}}
    park = {"type": "way", "id": 2, "tags": {"leisure": "park"}, "geometry": [
        {"lat": 35.1699, "lon": 33.3599}, {"lat": 35.1699, "lon": 33.3601}, {"lat": 35.1701, "lon": 33.3601},
        {"lat": 35.1701, "lon": 33.3599}, {"lat": 35.1699, "lon": 33.3599}]}
    trees = parse([mapped, park])
    m = next(t for t in trees if t["kind"] == "mapped")
    assert all(metres(m, t) >= 4 for t in trees if t["kind"] == "park")


def test_leaf_types_from_tags():
    assert leaf_of({"leaf_type": "needleleaved"}) == "needleleaved"
    assert leaf_of({"genus": "Phoenix"}) == "palm" and leaf_of({"species": "Washingtonia robusta"}) == "palm"
    assert leaf_of({"genus": "Cupressus"}) == "needleleaved" and leaf_of({"genus": "Pinus"}) == "needleleaved"
    assert leaf_of({}) == "broadleaved"
    assert '"natural"="tree"' in query() and "out tags geom" in query()


def test_read_once_kept_on_disk_and_indexed_by_tile(tmp_path):
    asked = []
    t = Trees(Settings(), cache_dir=tmp_path, fetch=lambda q: asked.append(q) or OSM)
    assert not t.ready
    t.refresh_if_needed()
    t.refresh_if_needed()
    assert t.ready and len(asked) == 1
    again = Trees(Settings(), cache_dir=tmp_path, fetch=lambda q: asked.append(q) or OSM)
    assert again.ready and len(again.trees) == len(t.trees)  # a restart reads the disk
    first = t.trees[0]
    assert first in t.tile(*lonlat_to_tile(first["lon"], first["lat"]))
    near = t.near(first["lat"], first["lon"], 50)
    assert near[0][0] == first and near[0][1] < 0.01 and all(d <= 50 for _, d in near)


def test_a_busy_overpass_is_said_and_tried_later(tmp_path):
    def busy(q):
        raise WebError("overpass-api.de answered HTTP 504")

    t = Trees(Settings(), cache_dir=tmp_path, fetch=busy)
    t.refresh_if_needed()
    assert not t.ready and "504" in t.error


def asgi_get(app, path):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [], "http_version": "1.1", "scheme": "http", "server": ("test", 80),
             "client": ("test", 1), "root_path": ""}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_the_route_serves_compact_rows_or_says_it_is_loading(tmp_path):
    t = Trees(Settings(), cache_dir=tmp_path, fetch=lambda q: OSM)
    app = FastAPI()
    app.include_router(tree_routes.router)
    app.state.agent = SimpleNamespace(trees=t)
    x, y = lonlat_to_tile(OSM["elements"][0]["lon"], OSM["elements"][0]["lat"])
    status, body = asgi_get(app, f"/api/trees/16/{x}/{y}")
    assert status == 200 and body["status"] == "loading" and body["trees"] == []
    t.refresh_if_needed()
    status, body = asgi_get(app, f"/api/trees/16/{x}/{y}")
    assert body["status"] == "ready" and body["kinds"] == list(KINDS) and body["leaves"] == list(LEAVES)
    lon, lat, h, r, kind, leaf = body["trees"][0]
    assert KINDS[kind] in KINDS and LEAVES[leaf] in LEAVES and h > 0 and r > 0
    assert asgi_get(app, "/api/trees/15/1/1")[0] == 404

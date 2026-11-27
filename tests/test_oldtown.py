"""The old town's houses (chatbot/oldtown.py), offline: five DLS 3D footprints of a walled-city
tile and the cadastral outlines standing in them, captured 26 Nov 2026."""

import json

from chatbot.dls import area_m2, tile_bounds
from chatbot.oldtown import (HOUSE_IDS, OID_FIELD, ROOF_RISE_M, OldTown, split_blocks,
                             tile_touches_walled_city)
from conftest import load

TILE = load("dls3d_oldtown_38842_25920.json")
OUTLINES = load("dls_cadastre_buildings_38842_25920.json")["features"]
X, Y = TILE["tile"]
SPLIT = {142761, 142762}  # two blocks the cadastre splits
WHOLE = 143162  # a big footprint the cadastre doesn't split (too few outlines in it)


def test_blocks_become_houses_and_the_rest_stays():
    out = split_blocks(TILE["features"], OUTLINES)
    by_id = {f["id"]: f for f in out}
    assert {f["id"] for f in out if f["properties"].get("split")} == SPLIT
    assert not by_id[WHOLE]["properties"].get("split")
    houses = [f for f in out if f["properties"].get("k") == "house"]
    assert len(houses) >= 6 and all(f["id"] > HOUSE_IDS for f in houses)
    assert {f["properties"]["blk"] for f in houses} == SPLIT
    # The DLS tile itself is left alone: the split mark is on a copy.
    assert not any(f["properties"].get("split") for f in TILE["features"])
    for f in houses:
        p = f["properties"]
        block = by_id[p["blk"]]["properties"]
        flat = "flat" in block["r"].lower()
        assert p["est"] == 1 and p["r"] == ("Flat roof (estimated)" if flat else "Inclined roof (estimated)")
        assert p["h"] <= block["h"] and p["h"] - p["e"] <= ROOF_RISE_M + 0.05
        assert p["g"] == block["g"]


def test_a_house_holds_its_blocks_mean_height():
    out = split_blocks(TILE["features"], OUTLINES)
    for bid in SPLIT:
        block = next(f for f in TILE["features"] if f["id"] == bid)
        mean = block["properties"]["v"] / area_m2(block["geometry"])
        house = next(f for f in out if f["properties"].get("blk") == bid)
        # eave plus a third of the roof (none if flat): the house's mean height
        rise = 0 if "flat" in block["properties"]["r"].lower() else ROOF_RISE_M / 3
        assert abs(house["properties"]["e"] + rise - min(mean, block["properties"]["h"])) < 0.2


def test_only_the_walled_city_is_split():
    assert tile_touches_walled_city(X, Y)
    assert not tile_touches_walled_city(X + 40, Y)  # Strovolos
    moved = [{**f, "geometry": {"type": f["geometry"]["type"], "coordinates": shift(f["geometry"]["coordinates"])}}
             for f in TILE["features"]]
    outlines = [{**o, "geometry": {"type": o["geometry"]["type"], "coordinates": shift(o["geometry"]["coordinates"])}}
                for o in OUTLINES]
    assert split_blocks(moved, outlines) == moved  # the same blocks 2 km south: left whole


def shift(c):
    return [c[0], c[1] - 0.02] if isinstance(c[0], float) else [shift(x) for x in c]


class FakeDls:
    """Answers the cadastre's query in its own shape (a joined layer: the id field carries the
    table's name; coordinates carry Z)."""

    def __init__(self):
        self.asked = []

    def query(self, url, params):
        self.asked.append(params)
        feats = [{"type": "Feature", "id": o["id"], "geometry": with_z(o["geometry"]),
                  "properties": {OID_FIELD: o["id"], "GISPortalMain.GIS.DomainParcelCategory.BLDG_DESC": "Digitized"}}
                 for o in OUTLINES]
        return {"type": "FeatureCollection", "features": feats}


def with_z(g):
    def z(c):
        return [c[0], c[1], 0] if isinstance(c[0], float) else [z(x) for x in c]
    return {"type": g["type"], "coordinates": z(g["coordinates"])}


def test_outlines_are_read_once_per_tile_and_kept(tmp_path):
    fake = FakeDls()
    town = OldTown(fake, cache_dir=tmp_path)
    out = town.tile(X, Y, TILE["features"])
    assert sum(1 for f in out if f["properties"].get("k") == "house") >= 6
    w, s, e, n = tile_bounds(X, Y)
    q = fake.asked[0]
    assert q["outFields"] == OID_FIELD and q["orderByFields"] == OID_FIELD
    box = [float(v) for v in q["geometry"].split(",")]
    assert box[0] < w and box[2] > e  # padded: a block's houses reach past its tile
    town.tile(X, Y, TILE["features"])
    assert len(fake.asked) == 1
    kept = json.loads((tmp_path / "dls3d" / "cadastre16" / f"{X}_{Y}.json").read_text())
    assert len(kept["features"]) == len(OUTLINES)
    assert len(kept["features"][0]["geometry"]["coordinates"][0][0]) == 2  # Z dropped


def test_a_failing_cadastre_leaves_the_blocks_whole(tmp_path):
    class Down:
        def query(self, url, params):
            raise RuntimeError("timed out")

    town = OldTown(Down(), cache_dir=tmp_path)
    assert town.tile(X, Y, TILE["features"]) == TILE["features"]
    assert town.tile(X + 40, Y, TILE["features"]) == TILE["features"]  # outside: never asked


def test_a_flat_roofed_block_gives_flat_houses():
    flat = [{**f, "properties": {**f["properties"], "r": "Flat roof"}} if f["id"] == 142761 else f
            for f in TILE["features"]]
    houses = [f for f in split_blocks(flat, OUTLINES) if f["properties"].get("blk") == 142761]
    assert houses and all(f["properties"]["h"] == f["properties"]["e"] for f in houses)
    assert all(f["properties"]["r"] == "Flat roof (estimated)" for f in houses)

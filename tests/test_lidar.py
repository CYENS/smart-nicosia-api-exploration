"""The 2019 LiDAR flight: the sheets' grid, fetching them, measuring buildings on them, the
comparison with the 2014 survey, and what the twin shows of it. All offline: sheets made here,
and a small crop of two real DLS sheets (tests/fixtures/lidar, CC BY 4.0) around a block of
Nicosia where a five-floor building went between the survey and the flight."""

import io
import json
import math

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, TiffImagePlugin

from chatbot import lidar, sources
from chatbot.entities import Entities
from chatbot.routes import buildings as building_routes
from chatbot.settings import NICOSIA_BBOX, Settings
from chatbot.store import STATE_COLORS, SessionStore
from chatbot.tools import run_tool
from chatbot.tools.base import ToolContext
from conftest import FIXTURES

REAL_SHEETS = FIXTURES / "lidar"  # 2019_DSM and 2019_DTM, one cropped sheet each
REAL_BUILDINGS = json.loads((FIXTURES / "lidar_buildings.json").read_text(encoding="utf-8"))
GONE = 158028  # five floors in the 2014 survey, an empty plot in 2019
EMPTY = -3.4028230607370965e38

# -- the national grid and the sheets' names -------------------------------------------------------


def test_the_grid_puts_the_old_town_on_its_sheet():
    e, n = lidar.to_grid(35.1695, 33.3600)  # Eleftheria Square
    assert abs(float(e) - 232794) < 2 and abs(float(n) - 393262) < 2
    assert lidar.sheet_of(float(e), float(n)) == (2325, 3925)
    assert lidar.sheet_name(2325, 3925) == "ETRS89_2325_3925.tif"
    # Arrays go through as well as numbers.
    es, ns = lidar.to_grid(np.array([35.1695, 35.10]), np.array([33.36, 33.28]))
    assert es.shape == (2,) and ns[0] > ns[1]


def test_greater_nicosia_is_a_block_of_sheets():
    sheets = lidar.sheets_for_bbox(NICOSIA_BBOX)
    assert len(sheets) == 42 and sheets[0] == (2250, 3850) and sheets[-1] == (2400, 3975)
    assert (2325, 3925) in sheets


# -- fetching ---------------------------------------------------------------------------------------


def a_listing(names: dict[str, int]) -> bytes:
    rows = "".join(f' 4/9/2025  6:35 PM {size:>12} <A HREF="/inspire_downloads/EL/rasters/x/{name}">{name}</A><br>'
                   for name, size in names.items())
    return f"<html><body><pre>{rows}</pre></body></html>".encode()


class FakeFolder:
    """Stands in for the DLS folder: its listings, and a sheet (or something else) per file."""

    def __init__(self, files: dict[str, bytes], content_type: str = "image/tiff"):
        self.files, self.content_type, self.asked = files, content_type, []

    def __call__(self, url: str, user_agent: str, timeout: float = 120.0):
        assert url.startswith(lidar.FOLDER) and "smart-nicosia" in user_agent
        self.asked.append(url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1])
        name = url.rsplit("/", 1)[-1]
        body = self.files[name] if name else a_listing({n: len(b) for n, b in self.files.items()})
        resp = io.BytesIO(body)
        resp.headers = {"Content-Type": self.content_type if name else "text/html"}
        return resp


BOX = (33.358, 35.166, 33.362, 35.169)  # inside one sheet, the old town's


def test_only_listed_sheets_are_fetched_and_kept_ones_are_not_asked_again(tmp_path, monkeypatch):
    sheet = b"II*\x00" + b"\x00" * 60
    folder = FakeFolder({"ETRS89_2325_3925.tif": sheet, "ETRS89_9999_9999.tif": sheet})
    monkeypatch.setattr(lidar, "_open", folder)
    said = []
    done = lidar.fetch_sheets(BOX, tmp_path, pause_s=0, say=said.append)
    assert done == {"fetched": 2, "kept": 0, "absent": 0, "bytes": 2 * len(sheet)}
    assert (tmp_path / "2019_DSM" / "ETRS89_2325_3925.tif").read_bytes() == sheet
    assert (tmp_path / "2019_DTM" / "ETRS89_2325_3925.tif").exists() and len(said) == 2
    assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob("ETRS89_9999*"))
    # A second run asks for the two listings and nothing else.
    folder.asked.clear()
    again = lidar.fetch_sheets(BOX, tmp_path, pause_s=0, say=said.append)
    assert again["fetched"] == 0 and again["kept"] == 2
    assert folder.asked == ["2019_DSM/", "2019_DTM/"]


def test_a_sheet_the_flight_did_not_cover_is_counted_not_asked_for(tmp_path, monkeypatch):
    folder = FakeFolder({"ETRS89_2250_3850.tif": b"II*\x00" + b"\x00" * 8})
    monkeypatch.setattr(lidar, "_open", folder)
    done = lidar.fetch_sheets(BOX, tmp_path, products=("dsm",), pause_s=0, say=lambda _: None)
    assert done["fetched"] == 0 and done["absent"] == 1 and folder.asked == ["2019_DSM/"]


@pytest.mark.parametrize("files, content_type", [
    ({"ETRS89_2325_3925.tif": b"<html>Please confirm you are a person</html>"}, "text/html"),
    ({"ETRS89_2325_3925.tif": b"not a tiff at all, whatever it says"}, "image/tiff"),
])
def test_an_answer_that_is_not_a_sheet_stops_the_fetch_and_keeps_nothing(tmp_path, monkeypatch, files, content_type):
    folder = FakeFolder(files, content_type)
    monkeypatch.setattr(lidar, "_open", folder)
    with pytest.raises(lidar.LidarError, match="[Ss]topped"):
        lidar.fetch_sheets(BOX, tmp_path, pause_s=0, say=lambda _: None)
    assert not list(tmp_path.rglob("*.tif")) and not list(tmp_path.rglob("*.part"))
    assert folder.asked == ["2019_DSM/", "2019_DSM/ETRS89_2325_3925.tif"]  # and no further


def test_an_empty_listing_is_taken_for_a_validation_page(monkeypatch):
    monkeypatch.setattr(lidar, "_open", FakeFolder({}))
    with pytest.raises(lidar.LidarError, match="lists no sheets"):
        lidar.listing("dsm", Settings().user_agent)


# -- sheets made here -------------------------------------------------------------------------------

LAT0, LON0 = 35.17, 33.36  # the north-west corner of the made-up sheets
CELL = 5e-6  # degrees: about half a metre


def write_sheet(path, heights: np.ndarray, cell: float = CELL, lon0: float = LON0, lat0: float = LAT0):
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[lidar.TAG_SCALE] = (cell, cell, 0.0)
    ifd[lidar.TAG_TIEPOINT] = (0.0, 0.0, 0.0, lon0, lat0, 0.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(heights.astype(np.float32), mode="F").save(path, compression="tiff_lzw", tiffinfo=ifd)
    return path


def rect(lon_w: float, lat_s: float, lon_e: float, lat_n: float) -> list[list[float]]:
    return [[lon_w, lat_s], [lon_e, lat_s], [lon_e, lat_n], [lon_w, lat_n], [lon_w, lat_s]]


def cells(r0: int, r1: int, c0: int, c1: int) -> list[list[float]]:
    """A ring around whole cells of the made-up surface sheet."""
    return rect(LON0 + c0 * CELL, LAT0 - r1 * CELL, LON0 + c1 * CELL, LAT0 - r0 * CELL)


@pytest.fixture
def made_up(tmp_path):
    """Flat ground at 150 m; a flat roof 10 m up on rows 40-80, columns 40-100, with a stair
    tower 3 m higher on a corner of it; a gap nobody measured; the terrain at half the detail."""
    top = np.full((200, 200), 150.0)
    top[40:80, 40:100] = 160.0
    top[40:46, 40:46] = 163.0
    top[150:160, 150:160] = EMPTY
    surface = lidar.Sheet(write_sheet(tmp_path / "s.tif", top))
    ground = lidar.Sheet(write_sheet(tmp_path / "g.tif", np.full((100, 100), 150.0), cell=2 * CELL))
    return surface, ground


def test_a_sheet_knows_where_its_cells_are(made_up):
    surface, ground = made_up
    assert (surface.rows, surface.cols) == (200, 200) and surface.lon0 == LON0 and surface.lat0 == LAT0
    assert surface.window((LON0 + 40 * CELL, LAT0 - 80 * CELL, LON0 + 100 * CELL, LAT0 - 40 * CELL)) == (40, 80, 40, 100)
    assert surface.window((LON0 - 1, LAT0 - 1, LON0 - 0.5, LAT0 - 0.5)) is None  # off the sheet
    lon = np.array([LON0 + 50.5 * CELL, LON0 + 5.5 * CELL, LON0 - 1.0])
    lat = np.array([LAT0 - 50.5 * CELL, LAT0 - 5.5 * CELL, LAT0])
    got = surface.at(lon, lat)
    assert got[0] == 160.0 and got[1] == 150.0 and got[2] < lidar.EMPTY_BELOW


def test_a_footprints_cells_are_its_heights_above_the_ground(made_up):
    surface, ground = made_up
    geom = {"type": "Polygon", "coordinates": [cells(40, 80, 40, 100)]}
    above, base = lidar.cells_above_ground(geom, surface, ground)
    assert above.size == 40 * 60 and set(np.unique(base)) == {150.0}
    stats = lidar.summarise(above, base, expected_cells=40 * 60)
    assert stats == {"ground": 150.0, "eave": 10.0, "mid": 10.0, "upper": 10.0, "top": 10.0,
                     "peak": 13.0, "built": 1.0, "cover": 1.0}
    # A courtyard is not part of the building, and a second part is.
    yard = {"type": "Polygon", "coordinates": [cells(40, 80, 40, 100), cells(50, 70, 50, 90)]}
    assert lidar.cells_above_ground(yard, surface, ground)[0].size == 40 * 60 - 20 * 40
    two = {"type": "MultiPolygon", "coordinates": [[cells(40, 80, 40, 100)], [cells(100, 110, 100, 110)]]}
    above, _ = lidar.cells_above_ground(two, surface, ground)
    assert above.size == 2400 + 100 and float(above.min()) == 0.0
    # Cells nobody measured are left out, and too few cells say nothing.
    gap = {"type": "Polygon", "coordinates": [cells(145, 165, 145, 165)]}
    above, base = lidar.cells_above_ground(gap, surface, ground)
    assert above.size == 400 - 100
    assert lidar.summarise(above, base, 400)["cover"] == 0.75
    assert lidar.summarise(above[:5], base[:5], 400) is None


def test_a_building_across_two_sheets_is_measured_whole(tmp_path):
    # Two real sheet names side by side; the building sits on the seam between them.
    lat, lo, hi = 35.1695, 33.37, 33.40
    for _ in range(50):  # where easting 235 km, the edge between sheets 2325 and 2350, runs
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if float(lidar.to_grid(lat, mid)[0]) < 235_000 else (lo, mid)
    seam_lon = lo
    west, east = lidar.sheet_of(*map(float, lidar.to_grid(lat, seam_lon - 0.0002))), \
        lidar.sheet_of(*map(float, lidar.to_grid(lat, seam_lon + 0.0002)))
    assert (west, east) == ((2325, 3925), (2350, 3925))
    half = 100 * CELL
    for key, lon0 in ((west, seam_lon - 2 * half), (east, seam_lon)):
        for product, value in (("dsm", 162.0), ("dtm", 150.0)):
            write_sheet(tmp_path / lidar.PRODUCTS[product] / lidar.sheet_name(*key),
                        np.full((200, 200), value), lon0=lon0, lat0=lat + half)
    b = {"type": "Feature", "id": 7, "properties": {"h": 12.0, "a": 500.0},
         "geometry": {"type": "Polygon", "coordinates": [rect(seam_lon - 30 * CELL, lat - 20 * CELL,
                                                                seam_lon + 30 * CELL, lat + 20 * CELL)]}}
    said = []
    got = lidar.measure_buildings([b], tmp_path, say=said.append)
    assert got[7]["top"] == 12.0 and got[7]["built"] == 1.0 and got[7]["cover"] >= 0.95
    assert len(said) == 2
    # With one of the two sheets missing, what the other covers is still measured, and said.
    (tmp_path / "2019_DSM" / lidar.sheet_name(*east)).unlink()
    said.clear()
    half_seen = lidar.measure_buildings([b], tmp_path, say=said.append)
    assert 0.4 < half_seen[7]["cover"] < 0.6 and any("not on disk" in s for s in said)


# -- 2014 against 2019 ------------------------------------------------------------------------------


def measured(**over) -> dict:
    return {"ground": 150.0, "eave": 9.0, "mid": 9.5, "upper": 10.0, "top": 10.0, "peak": 10.5,
            "built": 1.0, "cover": 1.0, **over}


def test_the_comparison_is_cautious():
    assert lidar.compare(10.0, measured()) == {"state": "same", "by_m": 0.0}
    assert lidar.compare(12.5, measured())["state"] == "same"  # within what two surveys differ by
    # A floor added over the building.
    assert lidar.compare(10.0, measured(top=13.4, upper=13.0, peak=13.6)) == {"state": "taller", "by_m": 3.4}
    # A tree leaning over a house is not an extra floor: most of the roof is where it was.
    assert lidar.compare(10.0, measured(top=16.0, upper=10.2, peak=18.0))["state"] == "same"
    # A church with a spire: its top is far below the survey's height, its spire is not.
    assert lidar.compare(47.0, measured(top=26.8, peak=62.6))["state"] == "same"
    assert lidar.compare(20.0, measured(top=12.0, peak=14.0)) == {"state": "lower", "by_m": -8.0}
    # Nothing stands on it any more.
    assert lidar.compare(15.1, measured(top=1.4, upper=0.1, peak=1.6, built=0.01)) == {"state": "gone", "by_m": -13.7}
    # Too little of it was flown to say.
    assert lidar.compare(10.0, measured(cover=0.4, built=0.0))["state"] == "unknown"
    # A shed can go, but a taller shed and a tree over a shed look alike.
    assert lidar.compare(2.5, measured(top=9.0, upper=8.0, peak=11.0), area_m2=18)["state"] == "same"
    assert lidar.compare(2.5, measured(top=0.2, built=0.0), area_m2=18)["state"] == "gone"


# -- the real crop ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    return lidar.measure_buildings(REAL_BUILDINGS, REAL_SHEETS)


def test_the_real_sheets_agree_with_the_survey_where_nothing_changed(real):
    assert len(real) == len(REAL_BUILDINGS) == 11
    by_id = {b["id"]: b["properties"] for b in REAL_BUILDINGS}
    unchanged = [oid for oid in real if oid != GONE]
    # The measured top is the survey's roof height: every unchanged building within a floor...
    assert all(abs(real[oid]["top"] - by_id[oid]["h"]) < 3.7 for oid in unchanged)
    # ...and half of them within a metre or so; the ground agrees too, mostly to half a metre.
    apart = sorted(abs(real[oid]["top"] - by_id[oid]["h"]) for oid in unchanged)
    assert apart[len(apart) // 2] <= 1.5
    off = sorted(abs(real[oid]["ground"] - by_id[oid]["g"]) for oid in real)
    assert off[len(off) // 2] <= 0.5 and off[-1] <= 2.5
    assert all(m["cover"] >= 0.95 and m["built"] >= 0.95 for oid, m in real.items() if oid != GONE)


def test_the_real_sheets_show_the_building_that_went(real):
    m = real[GONE]
    assert m["top"] < 2.0 and m["built"] < 0.05 and m["cover"] == 1.0
    rows, counts = lidar.changes_of(REAL_BUILDINGS, real)
    assert counts == {"same": 10, "gone": 1, "taller": 0, "lower": 0, "unknown": 0}
    assert [r["id"] for r in rows] == [GONE]
    r = rows[0]
    assert (r["state"], r["was_m"], r["now_m"], r["by_m"], r["floors"], r["area_m2"]) == ("gone", 15.1, 1.4, -13.7, 5, 372)
    assert abs(r["lat"] - 35.1673) < 0.0003 and r["geometry"]["type"] == "Polygon"
    assert lidar.change_words(r) == ("Nothing stands on most of it in 2019. The 2014 survey has a "
                                     "15.1 m building here")


# -- the files the twin reads -----------------------------------------------------------------------


@pytest.fixture
def flight(tmp_path, real):
    """The crop's measurements as the two small files, with two changes added by hand."""
    seed, changes = tmp_path / "buildings.json.gz", tmp_path / "changes.json.gz"
    lidar.write_seed(real, seed, {"measured": len(real)})
    rows, counts = lidar.changes_of(REAL_BUILDINGS, real)
    ring = REAL_BUILDINGS[0]["geometry"]
    rows += [
        {"id": 900001, "state": "taller", "by_m": 6.2, "was_m": 9.4, "now_m": 15.6, "built": 0.97,
         "use": "Mixed", "floors": 3, "area_m2": 240, "lon": 33.3600, "lat": 35.1700, "geometry": ring},
        {"id": 900002, "state": "gone", "by_m": -2.6, "was_m": 2.8, "now_m": 0.2, "built": 0.0,
         "use": "Low residential building with flat roof", "floors": 1, "area_m2": 18,
         "lon": 33.3581, "lat": 35.1672, "geometry": ring},  # a shed
    ]
    lidar.write_changes(rows, {**counts, "taller": 1, "gone": 2}, changes, {"measured": len(real)})
    return lidar.Flight(seed, changes)


def test_the_seed_gives_back_what_was_measured(flight, real, tmp_path):
    assert len(flight.measured) == 11 and flight.measured.about == {"measured": 11}
    assert flight.measured.of(GONE) == real[GONE]
    assert flight.measured.of(157822) == real[157822] and flight.measured.of(1) is None
    assert flight.changes.counts["gone"] == 2 and len(flight.changes.rows) == 3
    # The same heights make the same bytes, so a rerun that changes nothing changes no file.
    again = tmp_path / "again.json.gz"
    lidar.write_seed(real, again, {"measured": len(real)})
    assert again.read_bytes() == (tmp_path / "buildings.json.gz").read_bytes()
    # Without the files nothing is measured, and nothing is made up.
    missing = lidar.Flight(tmp_path / "no.gz", tmp_path / "none.gz")
    assert missing.measured.of(GONE) is None and missing.changes.rows == [] and len(missing.measured) == 0


def test_changed_buildings_draw_at_the_right_height(flight):
    feats = {f["properties"]["bid"]: f["properties"] for f in lidar.layer_features(flight.changes.rows, STATE_COLORS)}
    gone, taller = feats[GONE], feats[900001]
    assert gone["id"] == f"B{GONE}" and gone["state"] == "gone" and gone["color"] == STATE_COLORS["gone"]
    assert gone["height_m"] == 15.1  # what went is drawn at the height it had
    assert taller["height_m"] == 15.6 and taller["color"] == STATE_COLORS["taller"]
    assert taller["popup"].splitlines()[1] == "Taller by 6.2 m: 15.6 m at the top in 2019, 9.4 m in the 2014 survey"
    assert not any(dash in p["popup"] for p in feats.values() for dash in "–—")


# -- the tool ---------------------------------------------------------------------------------------


def a_ctx(flight) -> ToolContext:
    return ToolContext(Settings(), None, None, None, SessionStore(), lambda e: None, "c1", lidar=flight)


def test_the_tool_answers_for_the_city_with_a_layer_and_rows(flight):
    ctx = a_ctx(flight)
    res, err = run_tool(ctx, "find_height_changes", {})
    assert not err, res
    assert res["matching"] == 2 and res["by_change"] == {"taller": 1, "gone": 1}  # no shed by default
    assert res["measured_in_nicosia"]["buildings"] == 13 and res["measured_in_nicosia"]["same"] == 10
    assert [r["id"] for r in res["biggest"]] == [f"B{GONE}", "B900001"]  # by the height that went, then by the change
    assert res["biggest"][0] == {"id": f"B{GONE}", "change": "gone", "by_m": -13.7, "height_2014_m": 15.1,
                                 "height_2019_m": 1.4, "standing_pct": 1, "use": "High residential building",
                                 "floors": 5, "footprint_m2": 372}
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["geometry"] == "buildings" and set(layer["meta"]["legend"]) == {"gone", "taller"}
    assert layer["meta"]["sources"] == ["dls_lidar", "dls"]
    data = ctx.store.datasets[res["data_ref"]]
    assert len(data["rows"]) == 2 and data["sources"] == ["dls_lidar", "dls"]
    assert "2 changed buildings in greater Nicosia" in res["_summary"]


def test_the_tool_filters_by_change_place_and_size(flight):
    ctx = a_ctx(flight)
    sheds, _ = run_tool(ctx, "find_height_changes", {"change": "gone", "min_footprint_m2": 0})
    assert sheds["matching"] == 2 and {r["footprint_m2"] for r in sheds["biggest"]} == {372, 18}
    near, err = run_tool(ctx, "find_height_changes", {"near_lat": 35.1673, "near_lon": 33.3577, "radius_m": 100})
    assert not err and [r["id"] for r in near["biggest"]] == [f"B{GONE}"] and near["biggest"][0]["distance_m"] < 20
    assert near["area"]["radius_m"] == 100 and "within 100 m" in near["_summary"]
    big, _ = run_tool(ctx, "find_height_changes", {"change": "taller", "min_change_m": 10})
    assert big["matching"] == 0 and big["_summary"] == "No changed buildings in greater Nicosia"
    _, err = run_tool(ctx, "find_height_changes", {"change": "newer"})
    assert err


def test_the_tool_says_so_when_the_measurements_are_not_there(tmp_path):
    res, err = run_tool(a_ctx(lidar.Flight(tmp_path / "no.gz", tmp_path / "none.gz")), "find_height_changes", {})
    assert err and "aren't in this copy" in res["error"]


def test_the_tool_and_its_source_are_named_everywhere():
    assert sources.TOOL_SOURCES["find_height_changes"] == ("dls_lidar", "dls")
    assert "dls_lidar" in sources.TOOL_SOURCES["get_buildings_3d"]
    src = sources.BY_ID["dls_lidar"]
    assert src.licence == "CC BY 4.0" and src.url == lidar.DATASET_URL and src.credit
    from chatbot import prompt
    from conftest import ROOT
    assert "find_height_changes" in prompt.SYSTEM_PROMPT
    assert "find_height_changes:" in (ROOT / "chatbot/web/src/lib/chat.ts").read_text(encoding="utf-8")


# -- the map's layer and the inspector --------------------------------------------------------------


def test_the_map_layer_leaves_the_sheds_out(flight):
    app = FastAPI()
    app.include_router(building_routes.router)
    app.state.agent = type("A", (), {"lidar": flight})()
    res = TestClient(app).get("/api/buildings/changes")
    assert res.status_code == 200
    data = res.json()
    assert [f["properties"]["bid"] for f in data["features"]] == [GONE, 900001]
    meta = data["meta"]
    assert meta["ref"] == lidar.CHANGES_REF and meta["count"] == 2 and meta["measured"] == 13
    assert meta["by_change"] == {"gone": 1, "taller": 1, "lower": 0}
    assert meta["legend"] == {s: STATE_COLORS[s] for s in ("gone", "taller", "lower")}
    assert meta["sources"] == ["dls_lidar", "dls"]
    app.state.agent = type("A", (), {"lidar": lidar.Flight(FIXTURES / "no.gz", FIXTURES / "none.gz")})()
    assert TestClient(app).get("/api/buildings/changes").status_code == 503


class SomeDLS:
    def __init__(self, feats):
        self.by_id = {b["id"]: b for b in feats}

    def building_by_id(self, oid, near=None):
        return self.by_id.get(oid)

    def cached_tile(self, x, y):
        return []

    tile = cached_tile


def test_a_building_says_what_the_flight_measured(flight):
    ents = Entities(None, None, SomeDLS(REAL_BUILDINGS), None)
    plain = ents.get(f"B{GONE}")
    assert "height_2019_m" not in {r["key"] for r in plain["readings"]}  # no flight, no claim
    ents = Entities(None, None, SomeDLS(REAL_BUILDINGS), None)
    ents.lidar = flight
    gone = ents.get(f"B{GONE}")
    readings = {r["key"]: r for r in gone["readings"]}
    assert readings["height_m"]["value"] == 15.1 and readings["height_2019_m"]["value"] == 1.4
    assert readings["height_2019_m"]["label"] == "Top, measured in 2019" and readings["standing_pct"]["value"] == 1
    attrs = {a["label"]: a["value"] for a in gone["attributes"]}
    assert attrs["Since the 2014 survey"] == "Nothing stands on most of it in 2019"
    assert [s.get("source") for s in gone["sources"]] == ["dls", "dls_lidar"]
    same = ents.get("B158024")
    assert {a["label"]: a["value"] for a in same["attributes"]}["Since the 2014 survey"] == "No change of a floor or more"
    assert math.isclose({r["key"]: r["value"] for r in same["readings"]}["height_2019_m"], 34.6)

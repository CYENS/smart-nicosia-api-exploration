"""Offline tests for Urban Fabric, Summer Morning Ground Heat, Kontur Population, 15-min Access & CyNAP Gradients."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot import entities as E
from chatbot import sources
from chatbot.environment.spot import build_spot
from chatbot.fabric import entities as _fabric_entities  # noqa: F401 - registers fabric_hex
from chatbot.fabric.gradients import (
    NICOSIA_FABRIC_BBOX,
    GradientsStore,
    classify_slope,
    parse_gradients_geojson,
    parse_gradients_xml,
)
from chatbot.fabric.heat import (
    NICOSIA_BBOX,
    QA_EXCLUDE_MASK,
    clear_ground_mask,
    composite_summer_lst,
    decode_heat_grid,
    encode_heat_grid,
    hex_heat_stats,
    latlon_to_utm36n,
    sample_heat_grid,
)
from chatbot.fabric.scoring import SpatialBins, compute_heat_exposure, evaluate_accessibility
from chatbot.fabric.service import Fabric
from chatbot.routes import fabric as fabric_routes
from chatbot.routes import routers
from chatbot.routes import sources as source_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools.base import ToolContext
from chatbot.tools.registry import run_tool

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fabric"


def _make_tiled_geotiff(
    arr: np.ndarray,
    *,
    ox: float,
    oy: float,
    pixel_size: float = 30.0,
    compress: bool = True,
    signed: bool = False,
) -> bytes:
    """Build a minimal valid 1-tile tiled Classic TIFF (uint16 or int16) for offline tests."""
    h, w = arr.shape
    raw_arr = arr.astype("<i2" if signed else "<u2")
    payload = zlib.compress(raw_arr.tobytes()) if compress else raw_arr.tobytes()

    # 14 IFD tags
    n_tags = 14
    ifd_offset = 8
    after_ifd = ifd_offset + 2 + n_tags * 12 + 4
    scale_bytes = struct.pack("<3d", pixel_size, pixel_size, 0.0)
    tie_bytes = struct.pack("<6d", 0.0, 0.0, 0.0, ox, oy, 0.0)
    scale_off = after_ifd
    tie_off = scale_off + len(scale_bytes)
    tile_off = tie_off + len(tie_bytes)

    def _entry(tag: int, typ: int, cnt: int, val: int) -> bytes:
        return struct.pack("<HHII", tag, typ, cnt, val)

    entries = [
        _entry(256, 4, 1, w),
        _entry(257, 4, 1, h),
        _entry(258, 3, 1, 16),
        _entry(259, 3, 1, 8 if compress else 1),
        _entry(262, 3, 1, 1),
        _entry(277, 3, 1, 1),
        _entry(284, 3, 1, 1),
        _entry(317, 3, 1, 1),
        _entry(322, 4, 1, w),
        _entry(323, 4, 1, h),
        _entry(324, 4, 1, tile_off),
        _entry(325, 4, 1, len(payload)),
        _entry(339, 3, 1, 2 if signed else 1),
        _entry(33550, 12, 3, scale_off),
        _entry(33922, 12, 6, tie_off),
    ]
    n_tags = len(entries)
    after_ifd = ifd_offset + 2 + n_tags * 12 + 4
    scale_off = after_ifd
    tie_off = scale_off + len(scale_bytes)
    tile_off = tie_off + len(tie_bytes)
    # rebuild with updated offsets
    entries[10] = _entry(324, 4, 1, tile_off)
    entries[-2] = _entry(33550, 12, 3, scale_off)
    entries[-1] = _entry(33922, 12, 6, tie_off)

    hdr = struct.pack("<2sHI", b"II", 42, ifd_offset)
    ifd = struct.pack("<H", n_tags) + b"".join(entries) + struct.pack("<I", 0)
    return hdr + ifd + scale_bytes + tie_bytes + payload


@pytest.fixture
def fabric_service(tmp_path: Path) -> Fabric:
    bundle = json.loads((FIXTURES / "fabric_bundle.json").read_text(encoding="utf-8"))
    # Encode a clean deterministic 4x2 heat grid inside the fixture
    arr = np.array([[34.0, 38.0, 41.0, 44.0], [36.0, 39.0, 42.0, 43.2]], dtype=np.float32)
    meta = {k: v for k, v in bundle["heat_grid"].items() if k != "grid_b64"}
    bundle["heat_grid"] = encode_heat_grid(arr, NICOSIA_BBOX, meta=meta)

    store = GradientsStore(cache_dir=tmp_path)
    geo = parse_gradients_geojson((FIXTURES / "gradients_sample.geojson").read_bytes())
    store.seed(geo["points"], source_mode="geojson")

    # Minimal mock places + mobility for point-level spot_fabric / access_at
    sample_places = [
        {"id": "osm-p1", "category": "pharmacy", "name": "Ledras Pharmacy", "lat": 35.1745, "lon": 33.3638},
        {"id": "osm-g1", "category": "supermarket", "name": "Central Market", "lat": 35.1728, "lon": 33.3620},
        {"id": "osm-k1", "category": "park", "name": "Moat Park", "lat": 35.1705, "lon": 33.3625},
        {"id": "osm-s1", "category": "school", "name": "Faneromeni School", "lat": 35.1755, "lon": 33.3610},
    ]
    places = SimpleNamespace(ready=True, all=lambda: sample_places, overture=None)
    stops = [SimpleNamespace(id="101", name="Solomou Square", lat=35.1715, lon=33.3612)]
    mobility = SimpleNamespace(timetable=lambda: SimpleNamespace(stops=stops))

    fab = Fabric(cache_dir=tmp_path, gradients=store, places=places, mobility=mobility)
    fab.load_data(bundle)
    return fab


# -- 1. CyNAP Road Gradients --------------------------------------------------------------------


def test_cynap_gradients_xml_and_geojson_clipping_and_classification():
    xml_out = parse_gradients_xml((FIXTURES / "gradients_sample.xml").read_bytes(), NICOSIA_FABRIC_BBOX)
    assert xml_out["source_format"] == "datex2_xml"
    assert xml_out["island_total"] == 4
    assert len(xml_out["points"]) == 3  # excludes 34.71, 33.04 outside Nicosia
    assert xml_out["points"][0]["band"] == "gentle"
    assert xml_out["points"][1]["band"] == "moderate"  # 6.4% > 5%
    assert xml_out["points"][2]["slope_pct"] == 9.3 and xml_out["points"][2]["band"] == "steep"
    assert xml_out["points"][0]["road_name_el"] == "Λεωφ. Λεμεσού"

    geo_out = parse_gradients_geojson((FIXTURES / "gradients_sample.geojson").read_bytes(), NICOSIA_FABRIC_BBOX)
    assert geo_out["source_format"] == "geojson"
    assert len(geo_out["points"]) == 3
    assert classify_slope(5.0) == "gentle"
    assert classify_slope(5.1) == "moderate"
    assert classify_slope(8.1) == "steep"


def test_gradients_store_caches_daily_and_falls_back_to_mirror_and_stale(tmp_path: Path):
    calls: list[str] = []

    def fetch_fallback(url: str) -> bytes:
        calls.append(url)
        if "datasets/" in url:
            raise OSError("XML timed out")
        return (FIXTURES / "gradients_sample.geojson").read_bytes()

    store = GradientsStore(cache_dir=tmp_path, fetch=fetch_fallback)
    pts = store.refresh()
    assert len(pts) == 3
    assert store.source_mode == "geojson"
    assert len(calls) == 2

    # Reloaded from disk within 24 h should NOT hit network
    reloaded = GradientsStore(cache_dir=tmp_path, fetch=lambda u: (_ for _ in ()).throw(AssertionError("should use daily cache")))
    assert len(reloaded.points(refresh_if_stale=True)) == 3

    # Force refresh while upstream fails keeps cached copy & marks health stale
    failing = GradientsStore(cache_dir=tmp_path, fetch=lambda u: (_ for _ in ()).throw(OSError("offline")))
    kept = failing.refresh(force=True)
    assert len(kept) == 3
    assert failing.health()["state"] == "stale"


# -- 2. Landsat Summer Morning Ground Surface Temperature ---------------------------------------


def test_landsat_cog_sampling_masks_clouds_and_supports_rte_fallback():
    e0, n0 = latlon_to_utm36n(NICOSIA_BBOX[3], NICOSIA_BBOX[0])
    ox, oy = float(e0) - 300.0, float(n0) + 300.0
    nx_t, ny_t = 900, 700  # plenty to cover NICOSIA_BBOX at 30 m/px

    # Scene A: direct ST_B10 (lwir11 > 0) with half masked cloudy in QA
    lwir = np.full((ny_t, nx_t), 47364, dtype=np.uint16)  # 47364*0.00341802 + 149 - 273.15 = 37.74 °C
    qa = np.full((ny_t, nx_t), 21824, dtype=np.uint16)    # clear land
    qa[:, : nx_t // 2] |= 1 << 3  # Cloud bit set -> must be masked out

    # Scene B: ASTER GED gap (lwir11 == 0) filled via trad/urad/drad/atran + emis
    zero_lwir = np.zeros((ny_t, nx_t), dtype=np.uint16)
    trad = np.full((ny_t, nx_t), 9839, dtype=np.int16)    # ~40.0 °C at eps~0.975
    urad = np.full((ny_t, nx_t), 2791, dtype=np.int16)
    drad = np.full((ny_t, nx_t), 1309, dtype=np.int16)
    atran = np.full((ny_t, nx_t), 6377, dtype=np.int16)
    emis = np.full((ny_t, nx_t), 9745, dtype=np.int16)

    blobs = {
        "mem://a_lwir.tif": _make_tiled_geotiff(lwir, ox=ox, oy=oy),
        "mem://a_qa.tif": _make_tiled_geotiff(qa, ox=ox, oy=oy),
        "mem://b_lwir.tif": _make_tiled_geotiff(zero_lwir, ox=ox, oy=oy),
        "mem://b_qa.tif": _make_tiled_geotiff(np.full((ny_t, nx_t), 21824, dtype=np.uint16), ox=ox, oy=oy),
        "mem://b_trad.tif": _make_tiled_geotiff(trad, ox=ox, oy=oy, signed=True),
        "mem://b_urad.tif": _make_tiled_geotiff(urad, ox=ox, oy=oy, signed=True),
        "mem://b_drad.tif": _make_tiled_geotiff(drad, ox=ox, oy=oy, signed=True),
        "mem://b_atran.tif": _make_tiled_geotiff(atran, ox=ox, oy=oy, signed=True),
        "mem://b_emis.tif": _make_tiled_geotiff(emis, ox=ox, oy=oy, signed=True),
    }
    scenes = [
        {
            "id": "LC08_L2SP_176036_20260715_02_T1",
            "properties": {"datetime": "2026-07-15T08:22:00Z"},
            "assets": {"lwir11": {"href": "mem://a_lwir.tif"}, "qa_pixel": {"href": "mem://a_qa.tif"}},
        },
        {
            "id": "LC08_L2SP_176036_20260801_02_T1",
            "properties": {"datetime": "2026-08-01T08:22:00Z"},
            "assets": {
                "lwir11": {"href": "mem://b_lwir.tif"},
                "qa_pixel": {"href": "mem://b_qa.tif"},
                "trad": {"href": "mem://b_trad.tif"},
                "urad": {"href": "mem://b_urad.tif"},
                "drad": {"href": "mem://b_drad.tif"},
                "atran": {"href": "mem://b_atran.tif"},
                "emis": {"href": "mem://b_emis.tif"},
            },
        },
    ]

    grid = composite_summer_lst(
        NICOSIA_BBOX,
        nx=24,
        ny=18,
        scenes=scenes,
        range_reader=lambda url, s, e: blobs[url][s : e + 1],
    )
    assert grid["scenes_used"] == 2
    assert grid["temperature_kind"] == "morning_ground_surface"
    assert "not air temperature" in grid["note"].lower()
    decoded = decode_heat_grid(grid)
    assert decoded.shape == (18, 24)
    # Cloud-masked west half gets Scene B (~40 C), east half gets median(37.7, ~40)
    assert 37.0 <= float(np.nanmedian(decoded)) <= 41.5
    assert sample_heat_grid(grid, decoded, 35.15, 33.35) is not None
    assert clear_ground_mask(np.array([45000, 0]), np.array([21824, 21824])).tolist() == [True, False]
    assert clear_ground_mask(np.array([45000]), np.array([21824 | QA_EXCLUDE_MASK])).tolist() == [False]


def test_hex_heat_stats_and_heat_exposure_components():
    arr = np.array([[34.0, 44.0], [36.0, 42.0]], dtype=np.float32)
    grid = encode_heat_grid(arr, NICOSIA_BBOX)
    ring = [[33.30, 35.10], [33.42, 35.10], [33.42, 35.20], [33.30, 35.20], [33.30, 35.10]]
    st = hex_heat_stats(grid, decode_heat_grid(grid), ring, [33.36, 35.15])
    assert st["lst_median_c"] is not None

    hexes = [
        {"id": "h1", "lst_median_c": 44.0, "population": 5000, "green_pct": 2.0, "building_shade_factor": 0.0},
        {"id": "h2", "lst_median_c": 34.0, "population": 200, "green_pct": 35.0, "building_shade_factor": 0.5},
        {"id": "h3", "lst_median_c": 40.0, "population": 0, "green_pct": 0.0, "building_shade_factor": 0.0},
    ]
    compute_heat_exposure(hexes)
    assert hexes[0]["heat_exposure_score"] > hexes[1]["heat_exposure_score"]
    assert hexes[2]["heat_exposure_score"] == 0.0  # zero unpopulated exposure
    assert {"surface_heat_c", "heat_norm", "population_2022", "pop_norm", "green_pct", "lack_of_cool_norm"} <= set(hexes[0]["heat_parts"])


# -- 3. 15-Minute Accessibility & Step-Free Slope Penalty ----------------------------------------


def test_accessibility_15min_penalizes_steep_slopes_in_step_free_mode():
    services = {
        "bus_stop": SpatialBins([{"id": "s1", "name": "Stop", "lat": 35.150, "lon": 33.368, "source": "GTFS"}]),
        "grocery": SpatialBins([{"id": "g1", "name": "Market", "lat": 35.153, "lon": 33.372, "source": "OSM"}]),
        "pharmacy": SpatialBins([{"id": "p1", "name": "Pharm", "lat": 35.151, "lon": 33.369, "source": "OSM"}]),
        "park": SpatialBins([{"id": "k1", "name": "Park", "lat": 35.148, "lon": 33.367, "source": "OSM"}]),
        "school": SpatialBins([{"id": "u1", "name": "School", "lat": 35.154, "lon": 33.365, "source": "OSM"}]),
        "civic": SpatialBins([{"id": "c1", "name": "Bench", "lat": 35.150, "lon": 33.369, "source": "Overture"}]),
    }
    flat_acc = evaluate_accessibility(35.150, 33.369, services, grad_index=None)
    steep_pts = SpatialBins([{"id": "gr1", "lat": 35.1515, "lon": 33.3705, "slope_pct": 9.4}])
    steep_acc = evaluate_accessibility(35.150, 33.369, services, grad_index=steep_pts, local_grad_gt8=1)

    assert flat_acc["access_15min_score"] > 70
    assert steep_acc["access_step_free_score"] < flat_acc["access_step_free_score"]
    assert steep_acc["step_free_gap"] > flat_acc["step_free_gap"]
    assert any(c["slope_barrier"] for c in steep_acc["categories"])


# -- 4. Entities, Spot Enrichment, API Routes, Sources & Tools ----------------------------------


def test_fabric_hex_entity_and_spot_enrichment(fabric_service: Fabric):
    ents = E.Entities(None)
    ents.fabric = fabric_service
    ents.environment = SimpleNamespace(
        spot=lambda lat, lon, at=None: {
            "at": at,
            "sun": {"up": True, "shade": False, "altitude_deg": 45.0, "bearing_deg": 180.0, "from": "south"},
            "air": [],
            "temperature": None,
            "wind": None,
            "sources": [],
        }
    )
    hex_ent = ents.get("hex-883e826001fffff")
    assert hex_ent is not None
    assert hex_ent["kind"] == "fabric_hex"
    assert hex_ent["fabric"]["population_2022"] == 4200
    assert "morning ground" in hex_ent["_for_model"]["note"].lower()

    spot = build_spot(ents, "spot-35.174200-33.363500", None)
    assert spot is not None
    assert spot["fabric"]["hex_id"] == "hex-883e826001fffff"
    assert spot["fabric"]["surface_heat_c"] is not None
    assert "not air temperature" in spot["fabric"]["temperature_note"].lower()
    assert any(r.get("id") == "hex-883e826001fffff" for r in spot["relations"])
    assert any(s.get("source") == "landsat_lst" for s in spot["sources"])


def test_fabric_api_routes_and_sources_probes(fabric_service: Fabric):
    assert fabric_routes.router in routers()
    ents = E.Entities(None)
    ents.fabric = fabric_service
    agent = SimpleNamespace(fabric=fabric_service, entities=ents)
    app = FastAPI()
    app.state.agent = agent
    app.include_router(fabric_routes.router)
    client = TestClient(app)

    r_sum = client.get("/api/fabric/summary")
    assert r_sum.status_code == 200
    assert r_sum.json()["hex_count"] == 2
    assert r_sum.json()["population_total_2022"] == 6050

    r_heat = client.get("/api/fabric/heat")
    assert r_heat.status_code == 200
    assert r_heat.json()["known"] is True
    assert r_heat.json()["temperature_kind"] == "morning_ground_surface"

    r_hex = client.get("/api/fabric/hexagons?metric=access_step_free")
    assert r_hex.status_code == 200
    assert len(r_hex.json()["features"]) == 2
    assert r_hex.json()["meta"]["fabric"]["metric"] == "access_step_free"

    r_grad = client.get("/api/fabric/gradients?min_slope=5")
    assert r_grad.status_code == 200
    assert len(r_grad.json()["features"]) == 2  # 6.4% and 9.3%

    r_spot = client.get("/api/fabric/spot?lat=35.1742&lon=33.3635")
    assert r_spot.status_code == 200
    assert r_spot.json()["hex_id"] == "hex-883e826001fffff"
    assert r_spot.json()["access"]["access_15min_score"] >= 0

    # /api/sources health probes for the 4 new sources
    assert source_routes._cynap_gradients(agent)["state"] == "live"
    assert source_routes._fabric_bundle(agent, "kontur")["state"] == "live"
    assert source_routes._fabric_bundle(agent, "landsat")["state"] == "live"
    assert source_routes._fabric_bundle(agent, "overture")["state"] == "live"
    assert {"heat_exposure", "accessibility_15min", "population_reach"} <= set(sources.TOOL_SOURCES)


def test_fabric_tools_end_to_end(fabric_service: Fabric):
    ctx = ToolContext(Settings(), None, None, None, SessionStore(), [].append, "c1", fabric=fabric_service)

    res_heat, err = run_tool(ctx, "heat_exposure", {"lat": 35.1742, "lon": 33.3635, "radius_m": 2500})
    assert not err
    assert "not air temperature" in res_heat["temperature_kind"].lower()
    assert res_heat["top_hexagons"][0]["parts"]["surface_heat_c"] == 43.2
    assert res_heat["layer_ref"] in ctx.store.layers
    assert ctx.store.layers[res_heat["layer_ref"]]["meta"]["fabric"]["metric"] == "heat_exposure"

    res_acc, err = run_tool(ctx, "accessibility_15min", {"lat": 35.1742, "lon": 33.3635, "mode": "step_free"})
    assert not err
    assert res_acc["point_access"]["hex_id"] == "hex-883e826001fffff"
    assert res_acc["categories_data_ref"] in ctx.store.datasets

    res_pop, err = run_tool(ctx, "population_reach", {"lat": 35.1742, "lon": 33.3635, "radius_m": 600})
    assert not err
    assert res_pop["estimated_population_2022"] > 0
    assert "modelled 2022" in res_pop["population_note"].lower()

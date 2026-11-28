"""Offline repeatable builder for Nicosia's Urban Fabric, Heat & 15-Minute City dataset.

Run:
    uv run python -m chatbot.fabric build

Reads:
- Kontur population Cyprus H3 res-8 GeoPackage (CC BY 4.0) -> cached in data/chatbot_cache/fabric/
- Landsat 8/9 Summer 2026 Collection 2 Level 2 via Planetary Computer STAC (USGS Public Domain)
- Overture Maps base/infrastructure & base/land_use via DuckDB S3 (CDLA-Permissive-2.0)
- OSM places (data/chatbot_cache/places.json) & Overture places (overture_places.json)
- GTFS bus stops (data/chatbot_cache/gtfs/)
- CyNAP road gradients (data/chatbot_cache/fabric/gradients.json)

Writes a compact committed summary dataset:
    data/fabric_nicosia.json.gz
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import time
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

from chatbot.fabric.gradients import NICOSIA_FABRIC_BBOX, GradientsStore, classify_slope
from chatbot.fabric.heat import (
    DEFAULT_SUMMER_RANGE,
    composite_summer_lst,
    decode_heat_grid,
    hex_heat_stats,
)
from chatbot.fabric.scoring import SpatialBins, compute_heat_exposure, evaluate_accessibility
from chatbot.geo import haversine_m
from chatbot.settings import CACHE_DIR, ROOT, Settings

log = logging.getLogger(__name__)

KONTUR_URL = "https://geodata-eu-central-1-kontur-public.s3.amazonaws.com/kontur_datasets/kontur_population_CY_20220630.gpkg.gz"
DEFAULT_OUTPUT = ROOT / "data" / "fabric_nicosia.json.gz"
FALLBACK_OVERTURE_RELEASE = "2026-11-18.0"

GREEN_CLASSES = {"park", "garden", "grass", "orchard", "playground", "recreation_ground", "forest", "wood", "meadow"}
SCHOOL_CLASSES = {"school", "kindergarten", "university", "college"}
INFRA_KEEP = {
    "recycling", "bench", "crossing", "waste_basket", "bicycle_rental",
    "traffic_signals", "kerb", "parking", "bus_stop", "stop",
}


def _fetch_bytes(url: str, user_agent: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def kontur_nicosia(
    cache_dir: Path | None = None,
    *,
    bbox: tuple[float, float, float, float] = NICOSIA_FABRIC_BBOX,
    fetch: Callable[[str], bytes] | None = None,
    user_agent: str = "smart-nicosia-chatbot/0.1",
) -> list[dict]:
    """Load & clip Kontur 2022 Cyprus H3 res-8 population hexagons to Greater Nicosia."""
    import duckdb

    cdir = cache_dir or (CACHE_DIR / "fabric")
    cdir.mkdir(parents=True, exist_ok=True)
    gpkg = cdir / "kontur_population_CY_20220630.gpkg"
    gz = cdir / "kontur_population_CY_20220630.gpkg.gz"
    if not gpkg.exists():
        if not gz.exists():
            raw_gz = (fetch or (lambda u: _fetch_bytes(u, user_agent)))(KONTUR_URL)
            tmp_gz = gz.with_suffix(".gz.tmp")
            tmp_gz.write_bytes(raw_gz)
            tmp_gz.replace(gz)
        raw = gzip.decompress(gz.read_bytes())
        tmp = gpkg.with_suffix(".gpkg.tmp")
        tmp.write_bytes(raw)
        tmp.replace(gpkg)

    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
        w, s, e, n = bbox
        rows = con.execute(
            f"""
            SELECT h3, population,
                   ST_AsGeoJSON(ST_Transform(geom, 'EPSG:3857', 'EPSG:4326', true)) AS gj,
                   ST_X(ST_Centroid(ST_Transform(geom, 'EPSG:3857', 'EPSG:4326', true))) AS lon,
                   ST_Y(ST_Centroid(ST_Transform(geom, 'EPSG:3857', 'EPSG:4326', true))) AS lat,
                   ST_Area_Spheroid(ST_Transform(geom, 'EPSG:3857', 'EPSG:4326', true)) / 1000000.0 AS area_km2
            FROM ST_Read(?)
            WHERE ST_Intersects(
                ST_Transform(geom, 'EPSG:3857', 'EPSG:4326', true),
                ST_MakeEnvelope(?, ?, ?, ?)
            )
            """,
            [gpkg.as_posix(), w, s, e, n],
        ).fetchall()
    finally:
        con.close()

    hexes: list[dict] = []
    for h3_idx, pop, gj_text, lon, lat, area_km2 in rows:
        geom = json.loads(gj_text)
        coords = geom.get("coordinates") or []
        ring = coords[0] if geom.get("type") == "Polygon" else (coords[0][0] if coords else [])
        ring_round = [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in ring]
        pop_val = round(float(pop or 0.0))
        area = round(float(area_km2 or 0.7), 3)
        hexes.append({
            "id": f"hex-{h3_idx}",
            "h3": h3_idx,
            "population": pop_val,
            "area_km2": area,
            "pop_density_km2": round(pop_val / max(area, 0.05)),
            "center": [round(float(lon), 5), round(float(lat), 5)],
            "ring": ring_round,
        })
    hexes.sort(key=lambda h: h["id"])
    return hexes


def overture_nicosia(
    hexagons: list[dict],
    cache_dir: Path | None = None,
    *,
    bbox: tuple[float, float, float, float] = NICOSIA_FABRIC_BBOX,
    refresh: bool = False,
) -> dict:
    """Fetch Overture base/infrastructure & base/land_use for Greater Nicosia, cached locally."""
    import duckdb

    cdir = cache_dir or (CACHE_DIR / "fabric")
    cdir.mkdir(parents=True, exist_ok=True)
    cache_file = cdir / "overture_fabric.json"
    if cache_file.exists() and not refresh:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("hex_land_use") and cached.get("infrastructure") is not None:
                return cached
        except (OSError, ValueError):
            pass

    release = FALLBACK_OVERTURE_RELEASE
    try:
        from chatbot.overture import latest_release
        release = latest_release()
    except Exception:  # noqa: BLE001
        pass

    w, s, e, n = bbox
    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
        infra_rows = con.execute(
            f"""
            SELECT id, subtype, class, names.primary AS name,
                   ST_X(ST_Centroid(geometry)) AS lon,
                   ST_Y(ST_Centroid(geometry)) AS lat
            FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=base/type=infrastructure/*')
            WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
              AND class IN ({','.join(repr(c) for c in sorted(INFRA_KEEP))})
            """,
            [w, e, s, n],
        ).fetchall()

        lu_rows = con.execute(
            f"""
            SELECT id, subtype, class, names.primary AS name,
                   ST_X(ST_Centroid(geometry)) AS lon,
                   ST_Y(ST_Centroid(geometry)) AS lat,
                   ST_Area_Spheroid(geometry) AS area_m2,
                   ST_AsWKB(geometry) AS geom_wkb
            FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=base/type=land_use/*')
            WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
            """,
            [w, e, s, n],
        ).fetchall()

        con.execute("CREATE TEMP TABLE hexes (id VARCHAR, area_km2 DOUBLE, geom GEOMETRY)")
        for h in hexagons:
            gj = json.dumps({"type": "Polygon", "coordinates": [h["ring"]]})
            con.execute("INSERT INTO hexes VALUES (?, ?, ST_GeomFromGeoJSON(?))", [h["id"], h["area_km2"], gj])

        con.execute("CREATE TEMP TABLE landuse (cls VARCHAR, geom GEOMETRY)")
        for _, _, cls, _, _, _, _, wkb in lu_rows:
            if cls in GREEN_CLASSES or cls in {"residential", "commercial", "retail", "industrial", "school", "university", "hospital"}:
                con.execute("INSERT INTO landuse VALUES (?, ST_GeomFromWKB(?))", [cls, bytes(wkb)])

        inter_rows = con.execute(
            """
            SELECT h.id, l.cls,
                   SUM(ST_Area_Spheroid(ST_Intersection(h.geom, l.geom))) AS m2
            FROM hexes h
            JOIN landuse l ON ST_Intersects(h.geom, l.geom)
            GROUP BY h.id, l.cls
            """
        ).fetchall()
    finally:
        con.close()

    infra: list[dict] = []
    for oid, subtype, cls, name, lon, lat in infra_rows:
        if lon is None or lat is None:
            continue
        infra.append({
            "id": f"ovb-{str(oid).replace('-', '')[:12]}",
            "subtype": subtype,
            "class": cls,
            "name": name,
            "lon": round(float(lon), 6),
            "lat": round(float(lat), 6),
        })

    land_use_pts: list[dict] = []
    for oid, subtype, cls, name, lon, lat, area_m2, _ in lu_rows:
        if cls in GREEN_CLASSES or cls in SCHOOL_CLASSES or cls == "hospital":
            land_use_pts.append({
                "id": f"ovlu-{str(oid).replace('-', '')[:12]}",
                "subtype": subtype,
                "class": cls,
                "name": name or cls.replace("_", " ").capitalize(),
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
                "area_ha": round(float(area_m2 or 0.0) / 10000.0, 2),
            })

    hex_lu: dict[str, dict[str, float]] = defaultdict(dict)
    for hid, cls, m2 in inter_rows:
        hex_lu[hid][cls] = round(float(m2 or 0.0), 1)

    out = {
        "release": release,
        "fetched_at": time.time(),
        "infrastructure": infra,
        "land_use_points": land_use_pts,
        "hex_land_use": dict(hex_lu),
    }
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(cache_file)
    return out


def _load_service_bins(overture_data: dict) -> tuple[dict[str, SpatialBins], list[dict]]:
    """Gather everyday destinations from OSM places, Overture places/base, and GTFS stops."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    landmarks: list[dict] = []

    places_path = CACHE_DIR / "places.json"
    if places_path.exists():
        try:
            osm_places = json.loads(places_path.read_text(encoding="utf-8")).get("places") or []
            for p in osm_places:
                cat = p.get("category")
                lat, lon = p.get("lat"), p.get("lon")
                if lat is None or lon is None:
                    continue
                item = {"id": p["id"], "name": p.get("name") or cat, "lat": lat, "lon": lon, "source": "OpenStreetMap"}
                if cat == "pharmacy":
                    by_cat["pharmacy"].append(item)
                elif cat == "supermarket":
                    by_cat["grocery"].append(item)
                elif cat == "park":
                    by_cat["park"].append(item)
                    if p.get("name"):
                        landmarks.append({**item, "priority": 1})
                elif cat in ("toilets", "drinking_water", "bench"):
                    by_cat["civic"].append(item)
                elif cat in ("museum", "historic_site", "monument", "hospital", "theatre") and p.get("name"):
                    landmarks.append({**item, "priority": 2})
        except (OSError, ValueError):
            pass

    ov_path = CACHE_DIR / "overture_places.json"
    if ov_path.exists():
        try:
            ov_places = json.loads(ov_path.read_text(encoding="utf-8")).get("places") or []
            for p in ov_places:
                cat = p.get("category")
                kind = p.get("kind") or ""
                lat, lon = p.get("lat"), p.get("lon")
                if lat is None or lon is None:
                    continue
                pid = p.get("id") or ("ov-" + str(p.get("gers", "")).replace("-", "")[:12])
                item = {"id": pid, "name": p.get("name") or kind, "lat": lat, "lon": lon, "source": "Overture Maps"}
                if cat == "pharmacy":
                    by_cat["pharmacy"].append(item)
                elif cat == "supermarket" or "grocery" in kind or "convenience" in kind:
                    by_cat["grocery"].append(item)
                elif cat == "park":
                    by_cat["park"].append(item)
                elif "school" in kind or "kindergarten" in kind or "university" in kind:
                    by_cat["school"].append(item)
                    if p.get("name"):
                        landmarks.append({**item, "priority": 3})
                if p.get("locality") and p.get("name"):
                    landmarks.append({"name": p["locality"], "lat": lat, "lon": lon, "priority": 4})
        except (OSError, ValueError):
            pass

    # GTFS bus stops
    gtfs_dir = CACHE_DIR / "gtfs"
    if gtfs_dir.exists():
        try:
            from chatbot.mobility.gtfs import FEEDS, Timetable
            zips = [(f, gtfs_dir / f"{f.number}_google_transit.zip") for f in FEEDS if (gtfs_dir / f"{f.number}_google_transit.zip").exists()]
            if zips:
                tt = Timetable.from_zips(zips)
                for s in tt.stops:
                    if NICOSIA_FABRIC_BBOX[0] <= s.lon <= NICOSIA_FABRIC_BBOX[2] and NICOSIA_FABRIC_BBOX[1] <= s.lat <= NICOSIA_FABRIC_BBOX[3]:
                        by_cat["bus_stop"].append({"id": f"stop-{s.id}", "name": s.name, "lat": s.lat, "lon": s.lon, "source": "GTFS"})
        except Exception as e:  # noqa: BLE001
            log.warning("gtfs load skipped during fabric build: %s", e)

    # Overture base infrastructure & land_use
    for it in overture_data.get("infrastructure") or []:
        cls = it["class"]
        item = {"id": it["id"], "name": it.get("name") or cls.replace("_", " ").capitalize(), "lat": it["lat"], "lon": it["lon"], "source": "Overture base"}
        if cls in ("recycling", "bench", "crossing"):
            by_cat["civic"].append(item)
        elif cls in ("bus_stop", "stop"):
            by_cat["bus_stop"].append(item)
    for lu in overture_data.get("land_use_points") or []:
        cls = lu["class"]
        item = {"id": lu["id"], "name": lu.get("name") or cls.replace("_", " ").capitalize(), "lat": lu["lat"], "lon": lu["lon"], "source": "Overture land use"}
        if cls in GREEN_CLASSES:
            by_cat["park"].append(item)
        elif cls in SCHOOL_CLASSES:
            by_cat["school"].append(item)

    return {k: SpatialBins(v) for k, v in by_cat.items()}, landmarks


_AREAS = (
    ("Old Town (Walled City)", 35.1742, 33.3635),
    ("Eleftheria & Centre", 35.1695, 33.3600),
    ("Agios Andreas", 35.1725, 33.3475),
    ("Egkomi", 35.1600, 33.3200),
    ("Makedonitissa", 35.1485, 33.3075),
    ("Strovolos", 35.1400, 33.3450),
    ("Acropolis", 35.1510, 33.3680),
    ("Aglantzia", 35.1490, 33.3950),
    ("Lakatamia", 35.1180, 33.3100),
    ("Latsia", 35.1020, 33.3800),
    ("Geri", 35.1060, 33.4200),
    ("Kaimakli", 35.1820, 33.3880),
    ("Pallouriotissa", 35.1750, 33.3840),
    ("Agios Dometios", 35.1775, 33.3360),
    ("North Nicosia (Lefkoşa)", 35.1920, 33.3600),
    ("Gönyeli", 35.2130, 33.3080),
    ("Hamitköy", 35.2070, 33.3920),
    ("Haspolat / Mia Milia", 35.2050, 33.4350),
    ("Kokkinotrimithia", 35.1530, 33.2450),
    ("Tseri", 35.0850, 33.3250),
    ("Dali / Nisou", 35.0750, 33.4100),
)


def _hex_label(lat: float, lon: float, landmarks: list[dict], lm_bins: SpatialBins | None) -> str:
    area = min(_AREAS, key=lambda a: haversine_m(lat, lon, a[1], a[2]))[0]
    if lm_bins is not None:
        near = lm_bins.nearest(lat, lon, max_m=650.0)
        if near is not None and near[1].get("name"):
            nm = near[1]["name"]
            if nm.lower() not in area.lower():
                return f"{area}, near {nm}"
    return area


def build(
    *,
    output: Path = DEFAULT_OUTPUT,
    cache_dir: Path | None = None,
    settings: Settings | None = None,
    datetime_range: str = DEFAULT_SUMMER_RANGE,
    skip_landsat: bool = False,
    refresh_overture: bool = False,
    refresh_gradients: bool = False,
    say: Callable[[str], None] = print,
) -> dict:
    """Run the repeatable offline build for Greater Nicosia Urban Fabric, Heat & 15-min City."""
    s = settings or Settings()
    cdir = cache_dir or (CACHE_DIR / "fabric")
    cdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    say("1/5 Clipping Kontur 2022 population hexagons to Greater Nicosia...")
    hexagons = kontur_nicosia(cdir, user_agent=s.user_agent)
    say(f"    {len(hexagons)} H3 hexagons, {sum(h['population'] for h in hexagons):,} modelled 2022 residents")

    heat_cache = cdir / "landsat_heat_grid.json"
    if skip_landsat and heat_cache.exists():
        say("2/5 Reusing cached Landsat summer morning LST composite...")
        heat_grid = json.loads(heat_cache.read_text(encoding="utf-8"))
    else:
        say("2/5 Compositing clear Summer Landsat 8/9 surface temperature scenes (Planetary Computer)...")
        heat_grid = composite_summer_lst(
            NICOSIA_FABRIC_BBOX,
            datetime_range=datetime_range,
            user_agent=s.user_agent,
            say=lambda msg: say(f"    {msg}"),
        )
        tmp_hc = heat_cache.with_suffix(".tmp")
        tmp_hc.write_text(json.dumps(heat_grid, separators=(",", ":")), encoding="utf-8")
        tmp_hc.replace(heat_cache)
    heat_arr = decode_heat_grid(heat_grid)
    say(f"    Summer morning ground surface grid: {heat_grid['nx']}x{heat_grid['ny']}, "
        f"{heat_grid.get('min_c')} to {heat_grid.get('max_c')} °C across {heat_grid.get('scenes_used')} scenes")

    say("3/5 Reading Overture Maps base/infrastructure & base/land_use...")
    ov_data = overture_nicosia(hexagons, cdir, refresh=refresh_overture)
    infra_counts = Counter(it["class"] for it in ov_data["infrastructure"])
    say(f"    {len(ov_data['infrastructure'])} infrastructure rows, {len(ov_data['land_use_points'])} green/civic land-use sites (release {ov_data['release']})")

    say("4/5 Loading CyNAP road network gradients...")
    grad_store = GradientsStore(s, cache_dir=cdir)
    grad_pts = grad_store.refresh(force=refresh_gradients)
    grad_bins = SpatialBins(grad_pts)
    say(f"    {len(grad_pts)} gradient points in Greater Nicosia ({sum(1 for p in grad_pts if p['slope_pct'] > 5)} >5%, {sum(1 for p in grad_pts if p['slope_pct'] > 8)} >8%)")

    say("5/5 Scoring 15-minute access (standard & step-free) and heat exposure...")
    service_bins, landmarks = _load_service_bins(ov_data)
    lm_bins = SpatialBins([lm for lm in landmarks if lm.get("lat") is not None]) if landmarks else None
    infra_bins = SpatialBins(ov_data["infrastructure"])
    hex_lu = ov_data.get("hex_land_use") or {}

    for h in hexagons:
        lon, lat = h["center"]
        # Heat stats per hex
        h.update(hex_heat_stats(heat_grid, heat_arr, h["ring"], h["center"]))

        # Green & land-use share per hex
        lu_m2 = hex_lu.get(h["id"]) or {}
        hex_area_m2 = max(1000.0, float(h["area_km2"]) * 1_000_000.0)
        green_m2 = sum(v for k, v in lu_m2.items() if k in GREEN_CLASSES)
        h["green_pct"] = round(min(100.0, 100.0 * green_m2 / hex_area_m2), 1)
        h["building_shade_factor"] = 0.0  # updated dynamically where DLS 3D tiles exist

        # Infrastructure & gradients inside/around hex (within ~500 m hex radius)
        near_infra = infra_bins.within(lat, lon, 520.0)
        icnt = Counter(it["class"] for _, it in near_infra)
        h["infra"] = {
            "crossings": icnt["crossing"],
            "benches": icnt["bench"],
            "recycling": icnt["recycling"],
            "waste_baskets": icnt["waste_basket"],
            "bicycle_rental": icnt["bicycle_rental"],
        }

        near_grad = grad_bins.within(lat, lon, 520.0)
        g_gt5 = sum(1 for _, p in near_grad if p["slope_pct"] > 5.0)
        g_gt8 = sum(1 for _, p in near_grad if p["slope_pct"] > 8.0)
        h["slopes"] = {
            "points": len(near_grad),
            "gt5": g_gt5,
            "gt8": g_gt8,
            "max_pct": max((p["slope_pct"] for _, p in near_grad), default=None),
            "coverage": "measured" if near_grad else "unmeasured",
        }

        acc = evaluate_accessibility(
            lat, lon, service_bins, grad_bins,
            local_grad_gt5=g_gt5, local_grad_gt8=g_gt8, local_crossings=icnt["crossing"],
        )
        h["access_15min_score"] = acc["access_15min_score"]
        h["access_step_free_score"] = acc["access_step_free_score"]
        h["step_free_gap"] = acc["step_free_gap"]
        h["categories_reached_15min"] = acc["categories_reached_15min"]
        h["categories_reached_step_free"] = acc["categories_reached_step_free"]
        # Store compact category nearest info per hex
        h["nearest_services"] = {
            c["category"]: {
                "name": c["nearest"]["name"],
                "distance_m": c["nearest"]["distance_m"],
                "walk_min": c["walk_min"],
                "step_free_min": c["step_free_min"],
                "slope_barrier": c["slope_barrier"],
            }
            for c in acc["categories"] if c.get("nearest")
        }
        h["name"] = _hex_label(lat, lon, landmarks, lm_bins)

    compute_heat_exposure(hexagons)

    # Compact public amenities subset for map/spot lookups (parks, recycling, benches, crossings, schools)
    amenities = [
        {"id": it["id"], "kind": it["class"], "name": it.get("name"), "lat": it["lat"], "lon": it["lon"]}
        for it in ov_data["infrastructure"]
        if it["class"] in ("recycling", "bench", "crossing", "bicycle_rental")
    ]

    bundle = {
        "version": 1,
        "built_at": round(time.time()),
        "bbox": list(NICOSIA_FABRIC_BBOX),
        "caveats": {
            "population": "Modelled 2022 population (Kontur H3 res-8, ~0.7 km² hexagons), built for visualisation across both sides of the buffer zone.",
            "heat": "Summer morning ground surface temperature (Landsat 8/9 ~10:30 local), not shaded air temperature.",
            "slopes": "CyNAP road gradient points (100 m spacing) cover classified roads mostly south of the buffer zone.",
            "walkability": "Proximity along crow-flies x1.25 street detour; OSM and Overture completeness varies by suburb.",
        },
        "overture_release": ov_data["release"],
        "infra_counts": dict(infra_counts),
        "heat_grid": heat_grid,
        "hexagons": hexagons,
        "amenities": amenities,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output.with_suffix(".json.gz.tmp")
    with gzip.open(tmp_out, "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, separators=(",", ":"))
    tmp_out.replace(output)
    size_kb = output.stat().st_size / 1024
    say(f"Done in {time.time() - t0:.1f} s -> {output} ({size_kb:.1f} KB)")
    return bundle

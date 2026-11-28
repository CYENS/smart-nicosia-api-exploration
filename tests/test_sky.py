"""Offline tests for Living Sky (aircraft, ISS passes), CyNAP ITS inventories & Swarco probe,
and live bus archiving / replay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chatbot import overview, sources
from chatbot.entities import AT_RESOLVERS, Entities, EntityNotFound
from chatbot.memory import Memory
from chatbot.routes import sources as sources_routes
from chatbot.server import app
from chatbot.settings import Settings
from chatbot.sky import SkyService
from chatbot.sky.aircraft import (
    ADSB_URL,
    COVERAGE_NOTE,
    HISTORY_HOURS,
    NO_RECORD_NOTE,
    AircraftTracker,
    parse_aircraft,
)
from chatbot.sky.bus_archive import (
    NO_BUS_RECORD_NOTE,
    RETENTION_DAYS,
    BusArchiver,
    archive_status,
    buses_at,
)
from chatbot.sky.iss import TLE_URL, _SGP4, ISSTracker, compute_visible_passes, iss_state, parse_tle
from chatbot.sky.its import CCTV_URL, DETECTORS_URL, SWARCO_URL, ITSInventory, parse_sites_xml, parse_swarco_probe
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from chatbot.tools import sky as sky_tools

SKY_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sky"


def load_sky(name: str):
    path = SKY_FIXTURES / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


# -- Aircraft (adsb.lol + 24 h DuckDB replay) ---------------------------------------------------


def test_parse_aircraft_airborne_and_ground():
    payload = load_sky("adsb_nicosia.json")
    now = datetime.fromtimestamp(payload["now"] / 1000, tz=timezone.utc)
    items = parse_aircraft(payload, now)
    assert len(items) == 4
    by_hex = {a["hex"]: a for a in items}

    air = by_hex["4c2655"]
    assert air["id"] == "ac-4c2655"
    assert air["callsign"] == "PNC8J"
    assert air["type"] == "C56X"
    assert air["on_ground"] is False
    assert air["altitude_ft"] == 9075
    assert air["speed_kmh"] > 500
    assert air["track_deg"] == 92.6
    assert air["distance_km"] == pytest.approx(30.952 * 1.852, abs=0.2)

    ground = by_hex["4bb025"]
    assert ground["on_ground"] is True
    assert ground["altitude_ft"] == 0
    assert ground["altitude_m"] == 0
    assert ground["track_deg"] == 292.5


def test_aircraft_tracker_ttl_archive_prune_and_replay(tmp_path):
    payload = load_sky("adsb_nicosia.json")
    base = datetime.fromtimestamp(payload["now"] / 1000, tz=timezone.utc)
    clock = [base]
    mem = Memory(tmp_path / "mem.duckdb", record=False)

    tracker = AircraftTracker(lambda url: payload, memory=mem, now=lambda: clock[0], ttl_s=30.0)
    first = tracker.snapshot()
    assert first["status"] == "live" and first["count"] == 4
    assert tracker.fetches == 1
    assert COVERAGE_NOTE

    # Within 30 s TTL -> no upstream call
    clock[0] = base + timedelta(seconds=10)
    second = tracker.snapshot()
    assert second["count"] == 4
    assert tracker.fetches == 1

    # Past TTL -> refetches
    clock[0] = base + timedelta(seconds=35)
    tracker.snapshot()
    assert tracker.fetches == 2

    # Historical replay near base hits recorded positions
    rec = tracker.at(base + timedelta(seconds=5))
    assert rec["status"] == "recorded" and rec["known"] is True
    assert rec["count"] == 4
    assert any(a["id"] == "ac-4bb841" for a in rec["aircraft"])

    # Uncovered moment returns empty cleanly
    miss = tracker.at(base - timedelta(hours=3))
    assert miss["status"] == "unavailable" and miss["known"] is False
    assert miss["aircraft"] == []
    assert miss["error"] == NO_RECORD_NOTE

    # Old rows beyond 24 h are pruned when next write triggers prune
    old_ts = base - timedelta(hours=HISTORY_HOURS + 2)
    tracker._last_prune = float("-inf")
    tracker.record([{**first["aircraft"][0], "hex": "000001", "reported": int(old_ts.timestamp())}], old_ts)
    tracker._last_prune = float("-inf")
    later = base + timedelta(minutes=2)
    tracker.record([{**first["aircraft"][0], "reported": int(later.timestamp())}], later)
    with mem.reading() as con:
        remaining_hexes = {r[0] for r in con.execute("SELECT DISTINCT hex FROM aircraft_positions").fetchall()}
    assert "000001" not in remaining_hexes
    assert "4c2655" in remaining_hexes


# -- ISS visible passes (CelesTrak + SGP4) ------------------------------------------------------


def test_iss_tle_sgp4_and_12h_disk_cache(tmp_path):
    tle_text = load_sky("iss_tle.txt")
    tle = parse_tle(tle_text)
    assert tle.name == "ISS (ZARYA)"
    pos = iss_state(_SGP4(tle), tle.epoch + timedelta(minutes=15), 35.17, 33.36)
    assert -60.0 <= pos["lat"] <= 60.0
    assert 380.0 <= pos["altitude_km"] <= 460.0
    assert 7.0 <= pos["speed_kms"] <= 8.2

    passes = compute_visible_passes(tle, tle.epoch, tle.epoch + timedelta(days=8))
    assert passes, "expected at least one visible twilight pass over Nicosia within 8 days"
    for p in passes:
        assert p["max_elevation_deg"] >= 10
        assert p["start_ms"] <= p["peak_ms"] <= p["end_ms"]
        assert p["start_dir"] and p["end_dir"]

    clock = [tle.epoch + timedelta(minutes=30)]
    calls = []

    def fetch_tle(url: str) -> str:
        calls.append(url)
        return tle_text

    t1 = ISSTracker(fetch_tle, cache_dir=tmp_path, now=lambda: clock[0])
    s1 = t1.summary(look_ahead_days=6, look_back_days=2)
    assert s1["status"] == "live" and s1["position"] is not None
    assert len(calls) == 1

    # Second tracker sharing cache_dir uses the 12 h cached TLE without fetching
    t2 = ISSTracker(fetch_tle, cache_dir=tmp_path, now=lambda: clock[0])
    s2 = t2.summary(look_ahead_days=6, look_back_days=2)
    assert s2["status"] == "live"
    assert len(calls) == 1


# -- CyNAP ITS (13 CCTV + 39 detectors + Swarco health probe) -----------------------------------


def test_its_xml_parsing_and_swarco_probe(tmp_path):
    cctv_xml = load_sky("its_cctv.xml")
    det_xml = load_sky("its_traffic_detectors.xml")
    swarco_empty = load_sky("swarco_measured_empty.xml")

    cctv_sites, cctv_pub = parse_sites_xml(cctv_xml, "cctv", nicosia_only=True)
    det_sites, det_pub = parse_sites_xml(det_xml, "detector", nicosia_only=True)
    assert len(cctv_sites) == 13
    assert len(det_sites) == 39
    assert sum(1 for d in det_sites if (d.get("subtype") or "").lower() == "cycle") == 6
    assert sum(1 for d in det_sites if (d.get("subtype") or "").lower() != "cycle") == 33
    assert cctv_pub and det_pub

    sw_empty = parse_swarco_probe(swarco_empty)
    assert sw_empty["site_refs"] == 92
    assert sw_empty["measured_count"] == 0
    assert sw_empty["has_values"] is False

    swarco_live = swarco_empty.replace(
        "</q1:siteMeasurements>",
        '<q1:measuredValue><q1:basicData xsi:type="q1:TrafficFlow"><q1:vehicleFlow>'
        '<q1:vehicleFlowRate>420</q1:vehicleFlowRate></q1:vehicleFlow></q1:basicData>'
        '</q1:measuredValue></q1:siteMeasurements>',
        1,
    )
    sw_live = parse_swarco_probe(swarco_live)
    assert sw_live["site_refs"] == 92
    assert sw_live["measured_count"] >= 1
    assert sw_live["has_values"] is True

    calls = []

    def fetch_text(url: str) -> str:
        calls.append(url)
        if url == CCTV_URL:
            return cctv_xml
        if url == DETECTORS_URL:
            return det_xml
        if url == SWARCO_URL:
            return swarco_empty
        raise AssertionError(url)

    inv1 = ITSInventory(fetch_text, cache_dir=tmp_path)
    fc_cycle = inv1.geojson(kind="detectors", detector_mode="cycle")
    assert len(fc_cycle["features"]) == 6
    assert fc_cycle["meta"]["swarco"]["has_values"] is False
    assert fc_cycle["meta"]["swarco"]["site_refs"] == 92

    # Daily cache reuses CCTV/detector XMLs on disk
    calls.clear()
    inv2 = ITSInventory(fetch_text, cache_dir=tmp_path)
    fc_all = inv2.geojson()
    assert len(fc_all["features"]) == 52  # 13 CCTV + 39 detectors
    assert CCTV_URL not in calls and DETECTORS_URL not in calls


# -- Urgent Live Bus Position Recording & Replay -----------------------------------------------


def test_bus_archiver_dedup_retention_and_replay(tmp_path):
    base = datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc)
    mem = Memory(tmp_path / "mem.duckdb", record=False)

    snap = SimpleNamespace(vehicles=[
        {
            "id": "bus-101", "vehicle": "101", "line": "100", "line_id": "npt:100",
            "color": "#0ea5e9", "text_color": "#0b1220", "headsign": "Solomou Sq",
            "operator": "NPT", "trip_id": "t1", "shape_id": "s1", "along_m": 450.0,
            "lat": 35.1712, "lon": 33.3621, "heading": 90, "speed_kmh": 28.0,
            "delay_s": 60, "delay_source": "realtime", "reported": int(base.timestamp()),
        },
        {
            "id": "bus-202", "vehicle": "202", "line": "150", "line_id": "npt:150",
            "color": "#22c55e", "text_color": "#0b1220", "headsign": "Mall of Cyprus",
            "operator": "NPT", "trip_id": "t2", "shape_id": "s2", "along_m": 1200.0,
            "lat": 35.1505, "lon": 33.3702, "heading": 180, "speed_kmh": 34.0,
            "delay_s": -30, "delay_source": "realtime", "reported": int(base.timestamp()),
        },
    ])

    mob = SimpleNamespace(snapshot=lambda: snap, now=lambda: base)
    arch = BusArchiver(mob, mem)

    assert arch.step() == 2
    assert arch.step() == 0  # deduplicated identical reported_ts

    # Advancing timestamp records a second point
    snap2 = SimpleNamespace(vehicles=[{**snap.vehicles[0], "reported": int((base + timedelta(minutes=1)).timestamp())}])
    assert arch.record_snapshot(snap2, base + timedelta(minutes=1)) == 1

    # Old position beyond 14 days gets pruned when a new flush runs past prune window
    stale_dt = base - timedelta(days=RETENTION_DAYS + 1)
    stale_snap = SimpleNamespace(vehicles=[{**snap.vehicles[1], "vehicle": "999", "id": "bus-999",
                                            "reported": int(stale_dt.timestamp())}])
    arch.record_snapshot(stale_snap, stale_dt)
    arch._last_prune = float("-inf")
    snap3 = SimpleNamespace(vehicles=[{**snap.vehicles[0], "reported": int((base + timedelta(minutes=2)).timestamp())}])
    arch.record_snapshot(snap3, base + timedelta(minutes=2))
    st = archive_status(mem)
    assert st["retention_days"] == 14
    with mem.reading() as con:
        vids = {r[0] for r in con.execute("SELECT DISTINCT vehicle_id FROM bus_positions").fetchall()}
    assert "999" not in vids and "101" in vids

    # Replay hits recorded buses and respects lines filter
    replay = buses_at(mem, base + timedelta(seconds=30), timetable_state="ready")
    assert replay["status"] == "recorded" and replay["known"] is True and replay["count"] == 2
    filtered = buses_at(mem, base + timedelta(seconds=30), lines={"npt:150"}, timetable_state="ready")
    assert filtered["count"] == 1 and filtered["buses"][0]["id"] == "bus-202"

    # Uncovered moment cleanly says no recorded bus positions
    empty = buses_at(mem, base - timedelta(hours=4))
    assert empty["status"] == "unavailable" and empty["known"] is False
    assert empty["buses"] == [] and empty["error"] == NO_BUS_RECORD_NOTE


# -- Entities, tools, routes & sources ---------------------------------------------------------


def _make_service(tmp_path, swarco_xml: str | None = None):
    payload = load_sky("adsb_nicosia.json")
    now_dt = datetime.fromtimestamp(payload["now"] / 1000, tz=timezone.utc)
    tle_text = load_sky("iss_tle.txt")
    cctv_xml = load_sky("its_cctv.xml")
    det_xml = load_sky("its_traffic_detectors.xml")
    sw_xml = swarco_xml or load_sky("swarco_measured_empty.xml")
    mem = Memory(tmp_path / "mem.duckdb", record=False)

    def fetch_json(url: str):
        assert url == ADSB_URL
        return payload

    def fetch_text(url: str) -> str:
        if url == TLE_URL:
            return tle_text
        if url == CCTV_URL:
            return cctv_xml
        if url == DETECTORS_URL:
            return det_xml
        if url == SWARCO_URL:
            return sw_xml
        raise AssertionError(url)

    sky = SkyService(Settings(), memory=mem, fetch_json=fetch_json, fetch_text=fetch_text,
                     now=lambda: now_dt, cache_dir=tmp_path)
    return sky, mem, now_dt


def test_entities_search_and_tools(tmp_path):
    sky, mem, now_dt = _make_service(tmp_path)
    ents = Entities(None, places=None, dls=None, registers=None, memory=mem)
    ents.sky = sky

    sky.aircraft.snapshot()
    sky.its.geojson()

    ac = ents.get("ac-4c2655")
    assert ac["kind"] == "aircraft" and ac["name"] == "PNC8J"
    assert ac["aircraft"]["altitude_ft"] == 9075

    # Unknown aircraft raises cleanly
    with pytest.raises(EntityNotFound):
        ents.get("ac-ffffff")

    # Aircraft at past moment (via registered AT_RESOLVERS["aircraft"])
    at_rec = AT_RESOLVERS["aircraft"](ents, ac, now_dt + timedelta(seconds=5))
    assert at_rec["moment"]["known"] is True and at_rec["name"] == "PNC8J"
    at_miss = AT_RESOLVERS["aircraft"](ents, ac, now_dt - timedelta(hours=3))
    assert at_miss["moment"]["known"] is False and at_miss["state"]["value"] == "unknown"

    cctv_id = sky.its.inventories()["cctv"][0]["id"]
    det_id = sky.its.inventories()["detectors"][0]["id"]
    e_cctv = ents.get(cctv_id)
    e_det = ents.get(det_id)
    assert e_cctv["kind"] == "its_cctv" and "camera" in e_cctv["state"]["label"].lower()
    assert e_det["kind"] == "its_detector" and "Swarco" in e_det["attributes"][-1]["value"]

    # Ctrl K search finds cached aircraft and ITS sites
    hits = ents.search("PNC8J")
    assert any(h["id"] == "ac-4c2655" for h in hits)
    hits_det = ents.search(sky.its.inventories()["detectors"][0]["name"])
    assert any(h["kind"] == "its_detector" for h in hits_det)

    # Tool calls & source tagging
    ctx = ToolContext(
        Settings(), SimpleNamespace(now=lambda: now_dt), None, None, SessionStore(),
        lambda e: None, "conv1", memory=mem, sky=sky,
    )
    res_sky, err_sky = run_tool(ctx, "get_sky", {"what": "all"})
    assert not err_sky, res_sky
    assert res_sky["aircraft"]["count"] == 4
    assert "partial" in res_sky["aircraft"]["coverage_note"].lower()
    assert res_sky["iss"]["next_pass"] is not None
    assert ctx.store.layers[res_sky["layer_ref"]]["meta"]["sources"] == ["adsb_lol", "celestrak_iss", "twin_memory"]

    res_its, err_its = run_tool(ctx, "get_its_infrastructure", {"kind": "all"})
    assert not err_its, res_its
    assert res_its["counts"] == {"total": 52, "cctv": 13, "detectors": 39, "vehicle_detectors": 33, "cycle_detectors": 6}
    assert res_its["swarco"]["has_values"] is False
    assert ctx.store.layers[res_its["layer_ref"]]["meta"]["sources"] == ["cynap_its", "swarco_traffic"]


def test_routes_and_sources_probes(tmp_path, monkeypatch):
    sky, mem, now_dt = _make_service(tmp_path)
    bus_snap = SimpleNamespace(
        status="live", fetched_at=now_dt.timestamp(), feed_time=now_dt.timestamp(), error=None,
        vehicles=[{
            "id": "bus-77", "vehicle": "77", "line": "100", "line_id": "npt:100",
            "color": "#0ea5e9", "text_color": "#0b1220", "headsign": "Makariou",
            "lat": 35.17, "lon": 33.36, "heading": 0, "speed_kmh": 20.0,
            "reported": int(now_dt.timestamp()),
        }],
    )
    mobility = SimpleNamespace(
        snapshot=lambda: bus_snap, now=lambda: now_dt, timetable=lambda w=0: {"ready": True},
        fleet=SimpleNamespace(last=bus_snap),
        roads=SimpleNamespace(health=lambda: None, now=lambda: now_dt),
        source=SimpleNamespace(current=None, error=None, loaded_at=0.0),
    )
    past_bus_dt = now_dt - timedelta(minutes=10)
    past_snap = SimpleNamespace(
        status="live", fetched_at=past_bus_dt.timestamp(), feed_time=past_bus_dt.timestamp(), error=None,
        vehicles=[{**bus_snap.vehicles[0], "reported": int(past_bus_dt.timestamp())}],
    )
    arch = BusArchiver(mobility, mem)
    assert arch.record_snapshot(past_snap, past_bus_dt) == 1
    sky.bus_archiver = arch

    ents = Entities(None, places=None, dls=None, registers=None, memory=mem)
    ents.sky = sky
    fake_agent = SimpleNamespace(
        settings=Settings(),
        city=SimpleNamespace(now=lambda: now_dt),
        places=SimpleNamespace(ready=True),
        mobility=mobility,
        memory=mem,
        sky=sky,
        entities=ents,
        registers=SimpleNamespace(ready=True, fetched_at=0.0, sources=None),
    )

    monkeypatch.setattr(overview, "_cache", {})
    with TestClient(app) as client:
        app.state.agent = fake_agent

        r_ac = client.get("/api/sky/aircraft")
        assert r_ac.status_code == 200 and r_ac.json()["count"] == 4

        # Aircraft entity & past moment route resolution (snapshot is already in memory near now_dt;
        # record an earlier point at now_dt - 10 min for replay)
        past_ac_dt = now_dt - timedelta(minutes=10)
        past_ac = sky.aircraft.snapshot()["aircraft"][0]
        assert sky.aircraft.record([{**past_ac, "reported": int(past_ac_dt.timestamp())}], past_ac_dt) >= 1
        r_ent_ac = client.get(f"/api/entities/{past_ac['id']}", params={"at": (past_ac_dt + timedelta(seconds=10)).isoformat()})
        assert r_ent_ac.status_code == 200 and r_ent_ac.json()["moment"]["known"] is True

        r_iss = client.get("/api/sky/iss")
        assert r_iss.status_code == 200 and r_iss.json()["next_pass"] is not None

        r_its = client.get("/api/sky/its")
        assert r_its.status_code == 200
        its_body = r_its.json()
        assert its_body["meta"]["counts"]["cctv"] == 13
        assert its_body["meta"]["counts"]["detectors"] == 39

        # Replay buses at recorded past moment & at empty moment
        at_ok = (past_bus_dt + timedelta(seconds=15)).isoformat()
        r_bus_ok = client.get("/api/buses", params={"at": at_ok})
        assert r_bus_ok.status_code == 200
        b_ok = r_bus_ok.json()
        assert b_ok["status"] == "recorded" and b_ok["count"] == 1

        at_miss = (now_dt - timedelta(hours=3)).isoformat()
        r_bus_miss = client.get("/api/buses", params={"at": at_miss})
        assert r_bus_miss.status_code == 200
        b_miss = r_bus_miss.json()
        assert b_miss["status"] == "unavailable" and b_miss["error"] == NO_BUS_RECORD_NOTE

        r_arch = client.get("/api/sky/bus-archive")
        assert r_arch.status_code == 200 and r_arch.json()["rows"] >= 1

        # /api/sources shows live adsb, iss, cynap_its and stalled swarco
        src_body = client.get("/api/sources").json()
        by_id = {s["id"]: s for s in src_body["sources"]}
        assert by_id["adsb_lol"]["status"]["state"] == "live"
        assert by_id["celestrak_iss"]["status"]["state"] == "live"
        assert by_id["cynap_its"]["status"]["state"] == "live"
        assert by_id["swarco_traffic"]["status"]["state"] == "stalled"

        # Copy cleanliness: no em/en dashes or "--"
        assert all("--" not in sources_routes.data_sources.__doc__ for _ in (1,))
        for sid in ("adsb_lol", "celestrak_iss", "cynap_its", "swarco_traffic"):
            txt = json.dumps(by_id[sid])
            assert "—" not in txt and "–" not in txt and "--" not in txt

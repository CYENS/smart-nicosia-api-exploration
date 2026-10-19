"""Jobs and the scheduler, end to end against captured payloads served by a fake API."""

import csv
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from collector import db, scheduler
from collector.config import DEFAULT_JOBS
from collector.jobs import JOBS
from collector.status import collect_status, render_status
from conftest import load, sample, utc

NOW = utc(2026, 10, 19, 9, 30)  # 12:30 local: daytime, so the night-only lamp job waits

TYPES = {d["device_name"]: d["device_type"] for d in load("devices_2026-10-19.json")}
LATEST = {
    "Parking": "latest_parking_YP974.json",
    "Lighting": "latest_lamp_YL1015.json",
    "Wastebin": "latest_bin_YW1394.json",
    "smartsense": "latest_air_f60f85f2babb.json",
    "InfoPoints": "latest_infopoint_davilla.json",
    "Camera": "latest_camera_km08.json",
    "ParkingLot": "latest_parkinglot_17.json",
}


def latest_for(params):
    records = load(LATEST[TYPES[params["entityName"]]])
    keys = set(params["keys"].split(",")) if params.get("keys") else None
    return [r for r in records if keys is None or r["key"] in keys]


def range_for(params):
    start, end = int(params["startTs"]), int(params["endTs"])
    if params["keys"] == "toastDesc":
        records = load("tenant_events.json")
    else:
        records = load("tenant_stream.json")
    hits = [r for r in records if start <= int(r["timestamp"]) <= end]
    return sorted(hits, key=lambda r: -int(r["timestamp"]))[:1000]


def traffic_for(params):
    return load("traffic_stream_day.json") if params["group_by"] == "stream" else load(
        "traffic_hour_today.json")


@pytest.fixture
def city(api):
    """Every endpoint the scheduled jobs use, answered from fixtures."""
    api.on("getTenantDevices", load("devices_2026-10-19.json"))
    api.on("getLatestAttribute", lambda p: load("attributes_parking_YP974.json"))
    api.on("getLatestTelemetry", latest_for)
    api.on("getTelemetryRange", range_for)
    api.on("getAlarms", load("alarms_tenant.json"))
    api.on("PredefinedLocationDataPublication", load("nap_travel_times.xml"))
    api.on("getTrafficReports", traffic_for)
    api.on("generalReports", load("speed_five_min.json"))
    api.on("analytics", load("analytics_2026-10-19.json"))
    api.on("object_types", sample("object_types.json"))
    return api


def run(cfg, client, at=NOW, **kw):
    return {s.job: s for s in scheduler.run_pass(cfg, client=client, now_fn=lambda: at, **kw)}


def q(cfg, sql, params=None):
    with db.session(cfg.db_path, read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def test_first_pass_runs_every_daytime_job(cfg, client, city):
    results = run(cfg, client)
    assert set(results) == set(DEFAULT_JOBS) - {"lamps"}
    assert {s.status for s in results.values()} == {"ok"}, {
        j: s.message for j, s in results.items() if s.status != "ok"}

    assert q(cfg, "SELECT count(*), count(*) FILTER (WHERE in_inventory) FROM devices") == [
        (740, 740)]
    assert q(cfg, "SELECT display_name FROM devices WHERE name = 'YP974'") == [
        ("Parking Sensor 451",)]
    assert q(cfg, "SELECT count(*) FROM traffic_cameras WHERE location_source = 'device'") == [
        (29,)]
    assert q(cfg, "SELECT count(*) FROM events") == [(17,)]
    assert q(cfg, "SELECT count(*) FROM alarms") == [(20,)]
    assert q(cfg, "SELECT count(*) FROM travel_times") == [(8,)]
    assert q(cfg, "SELECT count(*) FROM speeds") == [(13,)]
    assert q(cfg, "SELECT count(DISTINCT analytics_id) FROM traffic_counts "
                  "WHERE granularity = 'day'") == [(65,)]
    assert q(cfg, "SELECT label FROM object_types WHERE id = 0") == [("people",)]
    # Stream rows arrive with their own timestamps and populate state_changes.
    assert q(cfg, "SELECT count(*) > 100 FROM telemetry WHERE source = 'stream'") == [(True,)]
    assert q(cfg, "SELECT count(*) > 0 FROM state_changes WHERE key = 'status'") == [(True,)]
    # One request per parking sensor, with the keys filter.
    assert city.count("getLatestTelemetry") == 303 + 139 + 15 + 7 + 31
    parking_call = next(p for e, p in city.calls
                        if e == "getLatestTelemetry" and p["entityName"].startswith("YP"))
    assert parking_call["keys"] == "status,is_pwd,vbatt,temperature"
    runs = q(cfg, "SELECT job, status, requests FROM job_runs WHERE job = 'parking'")
    assert runs == [("parking", "ok", 303)]


def test_camera_csv_is_written_for_hand_geocoding(cfg, client, city):
    run(cfg, client, only=["devices", "cameras_map"])
    with open(cfg.camera_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 39
    assert all(r["lat"] == "" and r["lon"] == "" for r in rows)

    # Fill one in by hand; the next run uses it and keeps the edit.
    rows[0]["lat"], rows[0]["lon"], rows[0]["note"] = "35.1712", "33.3611", "checked on site"
    with open(cfg.camera_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    run(cfg, client, at=NOW + timedelta(days=1), only=["cameras_map"])
    got = q(cfg, "SELECT lat, location_source FROM traffic_cameras WHERE analytics_id = ?",
            [int(rows[0]["analytics_id"])])
    assert got == [(35.1712, "manual")]
    with open(cfg.camera_csv, newline="", encoding="utf-8") as f:
        assert next(csv.DictReader(f))["note"] == "checked on site"


def test_nothing_is_due_right_after_a_pass(cfg, client, city):
    run(cfg, client)
    assert run(cfg, client, at=NOW + timedelta(minutes=1)) == {}
    later = run(cfg, client, at=NOW + timedelta(minutes=5))
    assert set(later) == {"stream", "events", "alarms", "travel_times"}


def test_lamps_run_only_at_night(cfg, client, city):
    run(cfg, client)
    night = utc(2026, 10, 19, 20, 0)
    assert "lamps" in run(cfg, client, at=night)
    assert q(cfg, "SELECT count(DISTINCT device) FROM telemetry t JOIN devices d "
                  "ON d.name = t.device WHERE d.type = 'Lighting' AND t.source = 'latest'") == [
        (240,)]


def test_force_runs_a_named_job_regardless_of_schedule(cfg, client, city):
    run(cfg, client, only=["devices"])
    assert set(run(cfg, client, only=["lamps"], force=True)) == {"lamps"}


def test_api_errors_are_recorded_and_retried_later(cfg, client, city):
    city.on("generalReports", load("speed_error.json"))
    results = run(cfg, client, only=["speed"])
    assert results["speed"].status == "error"
    errors = q(cfg, "SELECT job, kind FROM api_errors")
    assert errors == [("speed", "incoresoft")]
    # Not retried immediately, but after the retry delay.
    assert run(cfg, client, at=NOW + timedelta(minutes=5), only=["speed"]) == {}
    city.on("generalReports", load("speed_five_min.json"))
    assert run(cfg, client, at=NOW + timedelta(minutes=16), only=["speed"])["speed"].status == "ok"


def test_partial_failures_keep_the_rest(cfg, client, city):
    run(cfg, client, only=["devices"])
    original = city.handlers["getLatestTelemetry"]

    def flaky(params):
        if params["entityName"] == "YW1394":
            return load("traffic_error.json")
        return original(params)

    city.on("getLatestTelemetry", flaky)
    result = run(cfg, client, only=["bins"])["bins"]
    assert result.status == "partial"
    assert q(cfg, "SELECT count(DISTINCT device) FROM telemetry") == [(138,)]


def test_traffic_daily_catches_up_then_does_one_day(cfg, client, city):
    run(cfg, client, only=["traffic_daily"])
    first = [p for e, p in city.calls if e == "getTrafficReports"]
    assert len(first) == 1
    span_days = (int(first[0]["end_date"]) - int(first[0]["start_date"]) + 1) / 86_400_000
    assert span_days == 7
    city.calls.clear()
    run(cfg, client, at=NOW + timedelta(days=1), only=["traffic_daily"])
    (p,) = [p for e, p in city.calls if e == "getTrafficReports"]
    assert (int(p["end_date"]) - int(p["start_date"]) + 1) == 86_400_000


def test_traffic_hourly_refetches_yesterday_once(cfg, client, city):
    run(cfg, client, only=["traffic_hourly"])
    assert city.count("getTrafficReports") == 2
    run(cfg, client, at=NOW + timedelta(hours=1), only=["traffic_hourly"])
    assert city.count("getTrafficReports") == 3


def test_alarm_updates_keep_first_seen(cfg, client, city):
    run(cfg, client, only=["alarms"])
    alarms = load("alarms_tenant.json")
    alarms[0] = dict(alarms[0], status="CLEARED_UNACK", clear_ts="1792409999000")
    city.on("getAlarms", alarms)
    run(cfg, client, at=NOW + timedelta(minutes=5), only=["alarms"])
    (row,) = q(cfg, "SELECT status, first_seen, last_seen, clear_ts IS NOT NULL FROM alarms "
                    "WHERE id = ?", [alarms[0]["id"]])
    assert row[0] == "CLEARED_UNACK" and row[1] < row[2] and row[3]


def test_stream_watermark_limits_the_next_window(cfg, client, city):
    run(cfg, client, only=["devices", "stream"])
    wm = q(cfg, "SELECT value FROM watermarks WHERE name = 'stream'")[0][0]
    city.calls.clear()
    run(cfg, client, at=NOW + timedelta(minutes=5), only=["stream"])
    (p,) = [p for e, p in city.calls if e == "getTelemetryRange"]
    assert int(p["startTs"]) == wm - 10 * 60_000


def test_status_reports_health(cfg, client, city):
    run(cfg, client)
    s = collect_status(cfg, NOW + timedelta(minutes=2))
    text = render_status(s, cfg)
    assert "stream" in text and "telemetry" in text and "Errors in the last 24 h: 0" in text
    assert s["requests_24h"]["nokia"] > 700 and s["requests_24h"]["other"] == 1
    by_table = {t["table"]: t["rows"] for t in s["tables"]}
    assert by_table["devices"] == 740 and by_table["telemetry"] > 0
    json.dumps(s, default=str)


def test_status_without_database(cfg):
    assert "No database" in render_status(collect_status(cfg), cfg)


def test_default_budget_stays_under_25k(cfg):
    lines = scheduler.estimate_budget(cfg, now=utc(2026, 12, 21))  # longest night
    nokia = sum(b.requests_per_day for b in lines if b.nokia)
    assert nokia < 25_000
    assert {b.job for b in lines} == set(JOBS)
    assert "Smart Nicosia requests/day" in scheduler.render_budget(cfg, lines)


def test_budget_warns_when_over(cfg):
    jobs = dict(cfg.jobs, parking=replace(cfg.jobs["parking"], every_minutes=5))
    over = replace(cfg, jobs=jobs)
    assert "WARNING" in scheduler.render_budget(over, scheduler.estimate_budget(over))


def test_is_due_slack():
    every = timedelta(minutes=5)
    assert scheduler.is_due(every, NOW, NOW, NOW + timedelta(minutes=4, seconds=31))
    assert not scheduler.is_due(every, NOW, NOW, NOW + timedelta(minutes=4))

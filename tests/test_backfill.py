from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from collector import db
from collector.backfill import BackfillOptions, run_backfill
from collector.http import Client
from conftest import ms, utc

NOW = utc(2026, 10, 19, 9, 30)
TZ = ZoneInfo("Asia/Nicosia")
CAP = 50


def device_history(name: str) -> list[dict]:
    """Three days of a parking sensor: status every 30 min (alternating), vbatt every 4 h."""
    out = []
    t = NOW - timedelta(days=3)
    i = 0
    while t <= NOW:
        out.append({"key": "status", "timestamp": str(ms(t)), "value": str(i % 2)})
        if i % 8 == 0:
            out.append({"key": "vbatt", "timestamp": str(ms(t)), "value": "3.6"})
        t += timedelta(minutes=30)
        i += 1
    return out


EVENTS = [{"key": "toastDesc", "timestamp": str(ms(NOW - timedelta(hours=6 * i))),
           "value": f"YW{i}: Fill level above threshold of 80 %"} for i in range(40)]


def telemetry_range(params):
    start, end = int(params["startTs"]), int(params["endTs"])
    keys = set(params["keys"].split(","))
    source = EVENTS if params["entityType"] == "TENANT" else device_history(params["entityName"])
    hits = [r for r in source if r["key"] in keys and start <= int(r["timestamp"]) <= end]
    return sorted(hits, key=lambda r: -int(r["timestamp"]))[:CAP]


def speed(params):
    start = datetime.fromtimestamp(int(params["start_date"]) / 1000, TZ)
    return [{"label": f"{start:%m/%d/%Y - %H:%M}", "time": params["start_date"], "value": "40.1"}]


def traffic(params):
    start = datetime.fromtimestamp(int(params["start_date"]) / 1000, TZ)
    end = datetime.fromtimestamp(int(params["end_date"]) / 1000, TZ)
    if params["group_by"] == "hour":
        return {"data": {"group_by": [{"key": f"{h:02d}:00", "data": {"0": {"1": 1, "2": 2,
                                                                       "total": 3}}}
                                      for h in range(24)]}, "status": "ok"}
    days = {}
    d = start.date()
    while d <= end.date():
        days[f"{d:%d-%m-%Y}"] = {"31": {"KM Junior OUT V": {"2": {"1": 5, "2": 0, "total": 5}}}}
        d += timedelta(days=1)
    return {"data": {"group_by": days}, "status": "ok"}


@pytest.fixture
def bf_cfg(cfg):
    c = replace(cfg, range_cap=CAP)
    with db.session(c.db_path) as con:
        con.execute("""INSERT INTO devices (name, type, in_inventory) VALUES
                       ('YP1', 'Parking', true), ('YP2', 'Parking', true)""")
    return c


@pytest.fixture
def bf_client(bf_cfg, api):
    api.on("getTelemetryRange", telemetry_range)
    api.on("generalReports", speed)
    api.on("getTrafficReports", traffic)
    return Client(bf_cfg, fetch=api.fetch, sleep=lambda s: None)


def opts(**kw) -> BackfillOptions:
    base = dict(since=NOW - timedelta(days=2), until=NOW, what=("telemetry",), delay_s=0.0,
                flush_rows=40)
    base.update(kw)
    return BackfillOptions(**base)


def expected_rows(since: datetime) -> int:
    return sum(ms(since) <= int(r["timestamp"]) <= ms(NOW) for r in device_history("x")) * 2


def q(cfg, sql, params=None):
    with db.session(cfg.db_path, read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def test_pages_each_device_back_to_since(bf_cfg, bf_client, api):
    report = run_backfill(bf_cfg, opts(concurrency=2), client=bf_client, log_fn=lambda m: None)
    assert report.errors == []
    assert report.devices_done == 2
    assert q(bf_cfg, "SELECT count(*) FROM telemetry") == [(expected_rows(NOW - timedelta(days=2)),)]
    assert q(bf_cfg, "SELECT count(*) FROM backfill_progress WHERE done") == [(2,)]
    # Status alternates every 30 min, so nearly every reading is a change.
    assert q(bf_cfg, "SELECT count(*) > 150 FROM state_changes") == [(True,)]
    assert q(bf_cfg, "SELECT job, status FROM job_runs") == [("backfill", "ok")]
    # Every page after the first ends where the previous one's oldest record was.
    pages = [p for e, p in api.calls if e == "getTelemetryRange" and p["entityName"] == "YP1"]
    ends = [int(p["endTs"]) for p in pages]
    assert ends == sorted(ends, reverse=True) and len(set(ends)) == len(ends)


def test_resumes_after_max_requests_without_duplicates(bf_cfg, bf_client):
    first = run_backfill(bf_cfg, opts(max_requests=3), client=bf_client, log_fn=lambda m: None)
    assert first.stopped_by_budget
    assert first.devices_pending >= 1
    partial = q(bf_cfg, "SELECT count(*) FROM telemetry")[0][0]
    assert 0 < partial < expected_rows(NOW - timedelta(days=2))

    used = bf_client.stats.requests
    second = run_backfill(bf_cfg, opts(), client=bf_client, log_fn=lambda m: None)
    assert not second.stopped_by_budget
    assert q(bf_cfg, "SELECT count(*) FROM telemetry") == [(expected_rows(NOW - timedelta(days=2)),)]
    assert bf_client.stats.requests - used < 15  # resumed, didn't start over

    # A third run has nothing left to do.
    used = bf_client.stats.requests
    third = run_backfill(bf_cfg, opts(), client=bf_client, log_fn=lambda m: None)
    assert bf_client.stats.requests == used
    assert third.devices_done == 2


def test_older_since_continues_from_where_it_stopped(bf_cfg, bf_client):
    run_backfill(bf_cfg, opts(), client=bf_client, log_fn=lambda m: None)
    run_backfill(bf_cfg, opts(since=NOW - timedelta(days=3)), client=bf_client,
                 log_fn=lambda m: None)
    assert q(bf_cfg, "SELECT count(*) FROM telemetry") == [(expected_rows(NOW - timedelta(days=3)),)]


def test_traffic_requests_never_span_more_than_30_days(bf_cfg, bf_client, api):
    run_backfill(bf_cfg, opts(what=("traffic",), since=NOW - timedelta(days=70)),
                 client=bf_client, log_fn=lambda m: None)
    stream = [p for e, p in api.calls if e == "getTrafficReports" and p["group_by"] == "stream"]
    spans = [(int(p["end_date"]) - int(p["start_date"]) + 1) / 86_400_000 for p in stream]
    assert max(spans) <= 30 and len(stream) == 3
    hourly = [p for e, p in api.calls if e == "getTrafficReports" and p["group_by"] == "hour"]
    assert len(hourly) == 70  # one per finished local day
    days = q(bf_cfg, "SELECT count(DISTINCT bucket_start) FROM traffic_counts "
                     "WHERE granularity = 'day'")[0][0]
    assert days == 70

    api.calls.clear()  # everything is stored, so a rerun asks for nothing
    run_backfill(bf_cfg, opts(what=("traffic",), since=NOW - timedelta(days=70)),
                 client=bf_client, log_fn=lambda m: None)
    assert api.count("getTrafficReports") == 0


def test_events_and_speed(bf_cfg, bf_client, api):
    run_backfill(bf_cfg, opts(what=("events", "speed"), since=NOW - timedelta(days=20)),
                 client=bf_client, log_fn=lambda m: None)
    assert q(bf_cfg, "SELECT count(*) FROM events") == [(sum(
        int(e["timestamp"]) >= ms(NOW - timedelta(days=20)) for e in EVENTS),)]
    speed_calls = [p for e, p in api.calls if e == "generalReports"]
    assert len(speed_calls) == 3  # 7-day chunks
    assert all(int(p["end_date"]) - int(p["start_date"]) <= 7 * 86_400_000 for p in speed_calls)


def test_concurrency_is_capped_at_two(bf_cfg, bf_client):
    with pytest.raises(ValueError):
        run_backfill(bf_cfg, opts(concurrency=3), client=bf_client)

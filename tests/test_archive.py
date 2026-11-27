"""The per-line hourly backfill and its archive: every answer kept as received, a rerun that
resumes, an archive-only run that leaves the database alone, and loading the archive without
a single request."""

import gzip
import json
from datetime import timedelta

from collector import archive, db
from collector.backfill import BackfillOptions, run_backfill
from conftest import load, sample, utc

T0 = utc(2026, 10, 18, 9)  # 12:00 in Nicosia
HOURS = 3


def per_line(p):
    """One hour's per-line answer: line 'ST P' counts the hour's number of people."""
    n = (int(p["start_date"]) - int(T0.timestamp() * 1000)) // archive.HOUR_MS + 10
    return {"data": {"group_by": {"18-10-2026": {
        "67": {"ST P": {"0": {"1": n, "2": 1, "total": n + 1}}}}}}}


def setup_api(api, broken_hour=None):
    api.on("analytics", load("analytics_2026-10-19.json"))
    api.on("object_types", sample("object_types.json"))

    def answer(p):
        if broken_hour is not None and int(p["start_date"]) == broken_hour:
            return load("traffic_error.json")
        return per_line(p)
    api.on("getTrafficReports", answer)


def options(tmp_path, **kw):
    return BackfillOptions(since=T0, until=T0 + timedelta(hours=HOURS), what=("traffic_lines",),
                           delay_s=0, archive_dir=tmp_path / "archive", **kw)


def stored(cfg):
    with db.session(cfg.db_path, read_only=True, timezone_name=cfg.timezone) as con:
        return con.execute(
            """SELECT strftime(timezone('Asia/Nicosia', bucket_start), '%H:%M'), line_name,
                      object_type, dir1, dir2, total
               FROM traffic_counts WHERE granularity = 'hour' ORDER BY 1""").fetchall()


def test_each_hour_is_archived_as_received_and_stored(cfg, client, api, tmp_path):
    setup_api(api)
    report = run_backfill(cfg, options(tmp_path), client=client, log_fn=lambda m: None)
    assert report.rows == {"traffic_line_hours": 3, "traffic_counts": 3} and not report.errors
    assert api.count("getTrafficReports") == 3 and api.count("analytics") == 1
    assert stored(cfg) == [("12:00", "ST P", 0, 10, 1, 11), ("13:00", "ST P", 0, 11, 1, 12),
                           ("14:00", "ST P", 0, 12, 1, 13)]
    day = tmp_path / "archive" / "traffic" / "hourly" / "2026" / "10" / "2026-10-18.jsonl.gz"
    with gzip.open(day, "rt", encoding="utf-8") as f:
        first = json.loads(f.readline())
    assert first["params"] == {"va_ids": "[]", "group_by": "stream",
                               "start_date": int(T0.timestamp() * 1000),
                               "end_date": int(T0.timestamp() * 1000) + archive.HOUR_MS - 1}
    assert first["response"] == per_line(first["params"])  # untouched
    assert first["window_start"] == "2026-10-18T09:00:00+00:00"
    assert list((tmp_path / "archive" / "traffic" / "lines").glob("*.json.gz"))

    # A rerun finds every hour archived: no request at all.
    report = run_backfill(cfg, options(tmp_path), client=client, log_fn=lambda m: None)
    assert api.count("getTrafficReports") == 3 and api.count("analytics") == 1


def test_an_hour_that_failed_is_asked_again_next_time(cfg, client, api, tmp_path):
    broken = int(T0.timestamp() * 1000) + archive.HOUR_MS
    setup_api(api, broken_hour=broken)
    report = run_backfill(cfg, options(tmp_path), client=client, log_fn=lambda m: None)
    assert len(report.errors) == 1 and report.rows["traffic_line_hours"] == 2
    assert broken not in archive.archived_hours(tmp_path / "archive")
    setup_api(api)
    run_backfill(cfg, options(tmp_path), client=client, log_fn=lambda m: None)
    assert archive.archived_hours(tmp_path / "archive") == {
        int(T0.timestamp() * 1000) + k * archive.HOUR_MS for k in range(HOURS)}


def test_archive_only_leaves_the_database_alone_and_loads_later(cfg, client, api, tmp_path):
    setup_api(api)
    run_backfill(cfg, options(tmp_path, archive_only=True), client=client, log_fn=lambda m: None)
    assert not cfg.db_path.exists()
    asked = api.count("getTrafficReports")
    report = run_backfill(cfg, options(tmp_path, from_archive=True), client=client,
                          log_fn=lambda m: None)
    assert api.count("getTrafficReports") == asked  # loading asks nothing
    assert report.rows == {"traffic_line_hours": 3, "traffic_counts": 3}
    assert [r[0] for r in stored(cfg)] == ["12:00", "13:00", "14:00"]

from datetime import date
from zoneinfo import ZoneInfo

from collector import parse
from collector.jobs import map_cameras
from conftest import load, sample, utc

TZ = ZoneInfo("Asia/Nicosia")


def test_devices():
    devices = parse.parse_devices(load("devices_2026-10-19.json"))
    assert len(devices) == 740
    yw = next(d for d in devices if d["name"] == "YW1394")
    assert yw == {"name": "YW1394", "type": "Wastebin", "lat": 35.167214, "lon": 33.379694,
                  "label": None}


def test_stream_joins_device_messages_on_timestamp():
    records = load("tenant_stream.json")
    known = {d["device_name"] for d in load("devices_2026-10-19.json")}
    p = parse.parse_stream(records, known)
    assert p.messages > 200
    assert p.ambiguous == 0
    assert p.newest_ts == max(int(r["timestamp"]) for r in records)
    rows = {(d, k, ts): v for d, k, ts, v in p.rows}
    assert rows[("YP801", "status", 1792402024971)] == "0"
    assert rows[("YL1490", "level", 1792402076161)] == "0"
    assert rows[("YL1490", "state", 1792402076161)] == "Online"
    # Parking messages carry state = null on the tenant; nulls are dropped.
    assert ("YP801", "state", 1792402024971) not in rows
    assert all(k != "deviceName" for _, k, _, _ in p.rows)


def test_stream_skips_timestamps_shared_by_two_devices():
    records = [
        {"key": "deviceName", "timestamp": "1000", "value": "A"},
        {"key": "deviceName", "timestamp": "1000", "value": "B"},
        {"key": "status", "timestamp": "1000", "value": "1"},
        {"key": "deviceName", "timestamp": "2000", "value": "A"},
        {"key": "status", "timestamp": "2000", "value": "0"},
        {"key": "deviceName", "timestamp": "3000", "value": "Z"},
        {"key": "status", "timestamp": "3000", "value": "0"},
    ]
    p = parse.parse_stream(records, {"A", "B"})
    assert p.rows == [("A", "status", 2000, "0")]
    assert (p.ambiguous, p.unknown_devices, p.messages) == (1, 1, 1)


def test_events():
    events = parse.parse_events(load("tenant_events.json"))
    assert events[0] == (1792400400000, "b827ebac9601", "b827ebac9601: AQI not reported")
    assert ("Faneromeni", "Faneromeni: Warning issued by the player") in {
        (d, m) for _, d, m in events}


def test_alarm():
    a = parse.parse_alarm(load("alarms_tenant.json")[0])
    assert a["device"] == "b827ebac9601"
    assert a["device_type"] == "smartsense"
    assert a["status"] == "ACTIVE_UNACK"
    assert a["clear_ms"] is None and a["ack_ms"] is None
    assert a["start_ms"] == 1792400443922


def test_alarm_from_january_sample():
    active, cleared = (parse.parse_alarm(a) for a in sample("getAlarms.json")[:2])
    assert active["device"] == "YW1394" and active["type"] == "Lost connection"
    assert active["clear_ms"] is None
    assert (cleared["status"], cleared["clear_ms"]) == ("CLEARED_UNACK", 1766045281338)


def test_traffic_stream_per_camera_and_line():
    rows = parse.parse_traffic_stream(load("traffic_stream_day.json"), TZ)
    assert {r.granularity for r in rows} == {"day"}
    assert {r.bucket_start for r in rows} == {utc(2026, 10, 17, 21, 0)}  # 18 Oct local midnight
    cars_out = next(r for r in rows
                    if (r.analytics_id, r.line_name, r.object_type) == (31, "KM Junior OUT V", 2))
    assert (cars_out.dir1, cars_out.dir2, cars_out.total) == (4760, 0, 4760)
    assert len({r.analytics_id for r in rows}) == 65


def test_traffic_hourly_keys_are_local_time():
    rows = parse.parse_traffic_buckets(load("traffic_hour_today.json"), TZ, day=date(2026, 10, 19))
    first = min(rows, key=lambda r: r.bucket_start)
    assert first.bucket_start == utc(2026, 10, 18, 21, 0)  # "00:00" local = 21:00Z
    assert first.granularity == "hour" and first.analytics_id == 0 and first.line_name == ""
    people = next(r for r in rows if r.bucket_start == first.bucket_start and r.object_type == 0)
    assert (people.dir1, people.dir2, people.total) == (258, 287, 545)


def test_traffic_daily_buckets_from_january_sample():
    rows = parse.parse_traffic_buckets(sample("getTrafficReports.json"), TZ)
    assert {r.granularity for r in rows} == {"day"}
    jan7 = [r for r in rows if r.bucket_start == utc(2026, 1, 6, 22, 0)]
    assert next(r for r in jan7 if r.object_type == 0).total == 26430


def test_speed_labels_are_local():
    rows = parse.parse_speed(load("speed_five_min.json"), TZ)
    assert rows[0].label == "10/19/2026 - 11:25"
    assert rows[0].bucket_start == utc(2026, 10, 19, 8, 25)
    assert rows[0].value == 43.0


def test_speed_from_january_sample_in_winter_time():
    rows = parse.parse_speed(sample("generalReports.json"), TZ)
    assert rows[0].bucket_start == utc(2026, 1, 7, 13, 0)  # 15:00 local, UTC+2
    assert rows[0].value == 29.8


def test_nap_travel_times():
    rows = parse.parse_nap(load("nap_travel_times.xml"))
    assert len(rows) == 8
    first = rows[0]
    assert (first.segment_id, first.segment_name, first.from_site, first.to_site) == (
        "1", "1004->1008", "1004", "1008")
    assert first.travel_time_s == 114.41667 and first.speed_kmh == 33.949596
    assert first.measured_at == utc(2026, 10, 19, 9, 27)
    assert first.publication_time.utcoffset().total_seconds() == 3 * 3600
    missing = [r for r in rows if r.travel_time_s is None]
    assert len(missing) == 2 and all(r.speed_kmh is None for r in missing)


def test_analytics_feeds():
    feeds = parse.parse_analytics(load("analytics_2026-10-19.json"))
    assert len(feeds) == 68
    f = next(f for f in feeds if f.analytics_id == 88)
    assert f.name == "Kallipoleos (Agora Agiou Antoniou)"
    assert len(f.line_names) == 4


def test_camera_mapping_matches_29_by_exact_name():
    feeds = parse.parse_analytics(load("analytics_2026-10-19.json"))
    cameras = {d["name"]: (d["lat"], d["lon"]) for d in parse.parse_devices(
        load("devices_2026-10-19.json")) if d["type"] == "Camera"}
    rows, csv_rows = map_cameras(feeds, cameras, {})
    matched = [r for r in rows if r["location_source"] == "device"]
    assert len(matched) == 29
    assert len(csv_rows) == 39
    assert all(r["lat"] == "" and r["lon"] == "" for r in csv_rows.values())
    km08 = next(r for r in rows if r["name"] == "Kyriakou Matsi cam08")
    assert km08["device_name"] == "Kyriakou Matsi cam08"
    assert km08["lat"] == cameras["Kyriakou Matsi cam08"][0]


def test_camera_mapping_is_case_sensitive_and_uses_hand_coordinates():
    feeds = parse.parse_analytics(load("analytics_2026-10-19.json"))
    cameras = {"kyriakou matsi cam08": (35.0, 33.0)}
    manual = {119: {"analytics_id": "119", "name": "Trikoupi Cam17", "lat": "35.17",
                    "lon": "33.36", "note": "by hand"}}
    rows, csv_rows = map_cameras(feeds, cameras, manual)
    assert not any(r["location_source"] == "device" for r in rows)
    trikoupi = next(r for r in rows if r["analytics_id"] == 119)
    assert (trikoupi["lat"], trikoupi["lon"], trikoupi["location_source"]) == (
        35.17, 33.36, "manual")
    assert csv_rows[119]["note"] == "by hand"


def test_day_range_chunks():
    chunks = parse.day_range(date(2026, 1, 1), date(2026, 3, 5), 30)
    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert chunks[-1][1] == date(2026, 3, 5)
    assert all((b - a).days < 30 for a, b in chunks)

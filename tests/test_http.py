import urllib.error

import pytest

from collector.http import ApiError, Client, page_backwards
from conftest import load


def test_incoresoft_error_with_http_200_raises(client, api):
    api.on("getTrafficReports", load("traffic_error.json"))
    with pytest.raises(ApiError) as exc:
        client.get_json("getTrafficReports", va_ids="[]", group_by="bogus")
    assert exc.value.kind == "incoresoft"
    assert "ERROR_FAILED_TO_PROCESS_REQUEST" in str(exc.value)


def test_speed_enum_error_is_detected(client, api):
    api.on("generalReports", load("speed_error.json"))
    with pytest.raises(ApiError, match="No enum constant"):
        client.get_json("generalReports", group_by="FIVE_MINUTES")


def test_empty_list_for_unknown_device_is_not_an_error(client, api):
    api.on("getLatestTelemetry", [])
    assert client.latest_telemetry("yl1015") == []
    assert api.calls[0][1]["entityName"] == "yl1015"


def test_keys_filter_is_sent(client, api):
    api.on("getLatestTelemetry", [])
    client.latest_telemetry("YP974", ("status", "vbatt"))
    assert api.calls[0][1]["keys"] == "status,vbatt"


def test_retries_network_errors_then_succeeds(cfg, api):
    attempts = []

    def flaky(params):
        attempts.append(1)
        return urllib.error.URLError("reset") if len(attempts) == 1 else []

    api.on("getAlarms", flaky)
    sleeps = []
    client = Client(cfg, fetch=api.fetch, sleep=sleeps.append)
    assert client.get_json("getAlarms") == []
    assert client.stats.requests == 2
    assert sleeps == [2.0]


def test_does_not_retry_client_errors(client, api):
    api.on("getAlarms", lambda p: urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    with pytest.raises(ApiError, match="HTTP 404"):
        client.get_json("getAlarms")
    assert api.count("getAlarms") == 1


def test_invalid_json(client, api):
    api.on("getAlarms", "<html>oops</html>")
    with pytest.raises(ApiError, match="invalid JSON"):
        client.get_json("getAlarms")


def test_requests_are_spaced(cfg, api):
    api.on("getAlarms", [])
    clock = [100.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    client = Client(cfg, fetch=api.fetch, sleep=sleep, clock=lambda: clock[0],
                    request_delay_s=0.5)
    for _ in range(3):
        client.get_json("getAlarms")
    assert sleeps == [0.5, 0.5]


def test_text_requests_are_not_counted_as_nokia(client, api):
    api.on("PredefinedLocationDataPublication", "<payload/>")
    client.get_text("https://example.test/PredefinedLocationDataPublication", "nap")
    assert client.stats.requests == 1 and client.stats.nokia_requests == 0


# -- paging ------------------------------------------------------------------------------------


def fake_range(history: list[dict], cap: int):
    """An API over `history` that returns at most `cap` records, newest first, inclusive ends."""
    calls = []

    def fetch_page(start, end):
        calls.append((start, end))
        hits = [r for r in history if start <= int(r["timestamp"]) <= end]
        return sorted(hits, key=lambda r: -int(r["timestamp"]))[:cap]

    return fetch_page, calls


def history(n_ts: int, keys=("state", "level", "powerstats")):
    return [{"key": k, "timestamp": str(1000 + 10 * i), "value": str(i)}
            for i in range(n_ts) for k in keys]


def test_paging_collects_everything_even_when_a_timestamp_is_split():
    records = history(10)  # 30 records; cap 4 splits timestamps across pages
    fetch_page, calls = fake_range(records, cap=4)
    got = {(r["key"], r["timestamp"]) for page in page_backwards(fetch_page, 0, 10_000, cap=4)
           for r in page}
    assert got == {(r["key"], r["timestamp"]) for r in records}
    assert all(b[1] < a[1] for a, b in zip(calls, calls[1:]))  # strictly moving back


def test_paging_stops_at_short_page_and_respects_start():
    records = history(10)
    fetch_page, calls = fake_range(records, cap=1000)
    pages = list(page_backwards(fetch_page, 1050, 10_000, cap=1000))
    assert len(pages) == 1
    assert min(int(r["timestamp"]) for r in pages[0]) == 1050


def test_paging_max_pages_and_stop():
    records = history(100)
    fetch_page, _ = fake_range(records, cap=10)
    assert len(list(page_backwards(fetch_page, 0, 10_000, cap=10, max_pages=3))) == 3
    assert list(page_backwards(fetch_page, 0, 10_000, cap=10, stop=lambda: True)) == []


def test_paging_steps_past_a_page_of_one_timestamp():
    records = [{"key": f"k{i}", "timestamp": "5000", "value": "x"} for i in range(5)] + history(3)
    fetch_page, calls = fake_range(records, cap=5)
    pages = list(page_backwards(fetch_page, 0, 5000, cap=5))
    assert calls[1][1] == 4999
    got = {(r["key"], r["timestamp"]) for page in pages for r in page}
    assert got == {(r["key"], r["timestamp"]) for r in records}

"""The page's own device layers (the topic chips and the Layers sheet): every bay of a kind as
find_devices draws it, without the stale ones, and the alarms now. Offline, on test_memory's
fake city and the recorded alarms."""

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException

from chatbot import overview
from chatbot.routes import devices as device_routes

from conftest import load, ms
from test_clock import REQUEST, agent  # noqa: F401 - fixtures
from test_memory import NOW, fake_city, memory  # noqa: F401 - fixtures


def test_parking_bays_free_or_taken_without_the_stale_ones(agent, api):
    stale = ms(NOW - timedelta(hours=9))

    def latest(p):
        name = p["entityName"]
        ts = stale if name == "YPB" else ms(NOW - timedelta(minutes=5))
        return [{"key": "status", "timestamp": str(ts), "value": "1" if name == "YPA" else "0"}]

    api.on("getLatestTelemetry", latest)
    body = json.loads(device_routes.devices("parking_sensor", REQUEST).body)
    ids = {f["properties"]["id"]: f["properties"] for f in body["features"]}
    assert "YPB" not in ids  # stopped reporting
    assert ids["YPA"]["state"] == "occupied" and ids["YPA"]["color"] and ids["YPA"]["occupied"] == 1
    meta = body["meta"]
    assert meta["title"] == "Parking bays" and meta["left_out_stale"] == 1
    assert meta["by_state"]["occupied"] == 1 and set(meta["legend"]) <= {"free", "occupied", "unknown"}
    # Read once a minute at most: a second page costs the city API nothing.
    calls = len(api.calls)
    device_routes.devices("parking_sensor", REQUEST)
    assert len(api.calls) == calls


def test_only_the_kinds_the_page_draws(agent):
    with pytest.raises(HTTPException) as e:
        device_routes.devices("info_screen", REQUEST)
    assert e.value.status_code == 400


def test_alarms_now_at_their_devices(agent, api):
    api.on("getAlarms", load("alarms_tenant.json"))
    overview._cache.clear()
    body = json.loads(device_routes.alarms(REQUEST).body)
    assert body["meta"]["title"] == "Alarms now" and body["meta"]["legend"] == {"alarm": "#dc2626"}
    assert body["meta"]["count"] == len(body["features"]) <= body["meta"]["total"]
    for f in body["features"]:
        assert f["properties"]["state"] == "alarm" and f["geometry"]["type"] == "Point"

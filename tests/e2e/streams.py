"""Canned answers for /api/chat, in the event shapes the server streams (chatbot/routes/chat.py,
chatbot/agent.py, chatbot/followups.py) and the page reads (chatbot/web/src/lib/chat.ts).

Each stream is a list of events. The test server writes them one at a time with a short pause,
so the page sees a real stream: the trace, the text arriving, then the follow-ups. The server
adds the opening "conversation" event and the closing "done" itself. `HOLD` in a stream means:
stop here and keep the connection open until the page closes it (the Stop test).
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CID = "c-e2e"
HOLD = {"type": "hold"}

# The layer the parking answer draws: the recorded parking sensors.
_PARKING = json.loads((FIXTURES / "devices" / "parking_sensor.json").read_text(encoding="utf-8"))
LAYERS = {
    "free_bays": {"type": "FeatureCollection", "features": _PARKING["features"]},
}
FREE_BAYS_TITLE = "Parking bays now"

PARKING_NEXT = ["Which street has the most free bays?", "How was parking yesterday?"]
LOCATION_NEXT = ["How full are the car parks?"]
NEAR_ME_TEXT = "The nearest free bays to you are on **Arnaldas**, about 150 m away."
CHART_TITLE = "Free and taken bays"


def text(*parts: str) -> list[dict]:
    return [{"type": "text", "text": p} for p in parts]


def tool(i: str, name: str, summary: str, *between: dict) -> list[dict]:
    return [{"type": "tool_start", "id": i, "name": name}, *between,
            {"type": "tool_end", "id": i, "name": name, "ok": True, "summary": summary}]


def usage(i: int = 1200, o: int = 180) -> dict:
    return {"type": "usage", "input": i, "output": o, "cache_read": 900, "cache_write": 0, "model": "claude-e2e"}


def parking() -> list[dict]:
    """The full answer: thinking, tools, a layer on the map, a chart, text and follow-ups."""
    free = sum(1 for f in _PARKING["features"] if f["properties"]["state"] == "free")
    taken = len(_PARKING["features"]) - free
    layer = {
        "ref": "free_bays", "url": f"/api/layers/{CID}/free_bays", "style": "points", "value_field": None,
        "title": FREE_BAYS_TITLE, "fit": True, "labels": False,
        "legend": {"free": "#16a34a", "occupied": "#dc2626"}, "count": len(_PARKING["features"]),
        "timeseries": False, "sources": ["smart_nicosia"],
    }
    chart = {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json", "width": "container", "height": 160,
        "mark": "bar",
        "encoding": {"x": {"field": "state", "type": "nominal", "title": None},
                     "y": {"field": "bays", "type": "quantitative", "title": "Bays"}},
        "data": {"values": [{"state": "free", "bays": free}, {"state": "occupied", "bays": taken}]},
    }
    return [
        {"type": "thinking_start"},
        {"type": "thinking", "text": "Reading the parking sensors, then drawing them."},
        *tool("t1", "find_devices", f"{len(_PARKING['features'])} parking sensors"),
        *tool("t2", "show_on_map", "map: free_bays", {"type": "ui", "action": "show_layer", "layer": layer}),
        *tool("t3", "show_chart", f"chart: {CHART_TITLE}",
              {"type": "ui", "action": "chart", "title": CHART_TITLE, "spec": chart, "link": None,
               "time_field": None, "scrub": False, "sources": ["smart_nicosia"]}),
        *text(f"**{free} of {len(_PARKING['features'])}** bays ", "are free right now. ",
              "Arnaldas has the most room; the map shows each bay and the chart the totals."),
        usage(),
        {"type": "follow_ups", "next": PARKING_NEXT, "do": ["city_3d"]},
    ]


def needs_location() -> list[dict]:
    return [
        *text("To find the free bays nearest to you I need to know where you are. ",
              "Share your location, or pick your spot on the map."),
        usage(800, 60),
        {"type": "follow_ups", "next": LOCATION_NEXT, "do": ["share_location", "pick_on_map"]},
    ]


def near_me() -> list[dict]:
    return [
        *tool("t1", "where_am_i", "near Arnaldas"),
        *text(NEAR_ME_TEXT),
        usage(900, 40),
        {"type": "follow_ups", "next": ["Show them on the map"]},
    ]


def short(question: str) -> list[dict]:
    return [
        *text(f"You asked: {question} ", "This is a short answer from the test fixtures."),
        usage(600, 30),
        {"type": "follow_ups", "next": ["Tell me more"]},
    ]


def cut() -> list[dict]:
    """A long answer the Stop test cuts mid-way: it starts, then holds the line open."""
    return [
        {"type": "thinking_start"},
        {"type": "thinking", "text": "A month of history: this takes a while."},
        {"type": "tool_start", "id": "t1", "name": "query_history"},
        *text("Reading a month of the twin's memory, "),
        HOLD,
    ]


def pick(message: str, context: dict | None) -> str:
    """Which stream answers a question."""
    m = message.lower()
    if "slowly" in m:
        return "cut"
    if "near me" in m:
        return "near_me" if (context or {}).get("user_location") else "needs_location"
    if "car park" in m:
        return "parking"
    return "short"


def events(name: str, question: str) -> list[dict]:
    return {"parking": parking, "needs_location": needs_location, "near_me": near_me, "cut": cut}.get(
        name, lambda: short(question))()

"""Chatbot: tour solver, polyline decoding, tools against captured payloads, and the agent loop
against a scripted fake of the Claude streaming API (no network, no API key)."""

import asyncio
import itertools
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from chatbot import agent as agent_mod
from chatbot import tools as tools_mod
from chatbot.agent import Agent
from chatbot.city import CityData
from chatbot.routing import best_order, decode_polyline6
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool

from conftest import load, utc

# -- routing ------------------------------------------------------------------------------------


def _cost(order, m, return_to_start):
    c = sum(m[a][b] for a, b in zip(order, order[1:]))
    return c + (m[order[-1]][0] if return_to_start else 0)


@pytest.mark.parametrize("return_to_start,fixed_end", [(False, False), (True, False), (False, True)])
def test_best_order_is_optimal(return_to_start, fixed_end):
    rnd = random.Random(7)
    for n in (3, 5, 7):
        m = [[0 if i == j else rnd.randint(1, 100) for j in range(n)] for i in range(n)]
        got = best_order(m, return_to_start, fixed_end)
        assert got[0] == 0 and sorted(got) == list(range(n))
        if fixed_end:
            assert got[-1] == n - 1
        middle = range(1, n - 1) if fixed_end else range(1, n)
        best = min(
            _cost([0, *p, *([n - 1] if fixed_end else [])], m, return_to_start)
            for p in itertools.permutations(middle)
        )
        assert _cost(got, m, return_to_start) == best


def _encode6(coords):
    out, prev = [], (0, 0)
    for lon, lat in coords:
        cur = (round(lat * 1e6), round(lon * 1e6))
        for v in (cur[0] - prev[0], cur[1] - prev[1]):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = cur
    return "".join(out)


def test_decode_polyline6_round_trip():
    coords = [[33.3615, 35.1725], [33.355753, 35.171733], [33.36217, 35.170614]]
    assert decode_polyline6(_encode6(coords)) == coords


# -- tools --------------------------------------------------------------------------------------


@pytest.fixture
def city(api, client):
    api.on("getTenantDevices", load("devices_2026-10-19.json"))
    api.on("getLatestTelemetry", load("latest_parking_YP974.json"))
    readings_at = max(int(r["timestamp"]) for r in load("latest_parking_YP974.json"))
    now = datetime.fromtimestamp(readings_at / 1000 + 600, tz=timezone.utc)  # 10 min later
    return CityData(Settings(), client=client, now=lambda: now)


def make_ctx(city=None, events=None):
    return ToolContext(Settings(), city, None, None, SessionStore(),
                       (events if events is not None else []).append, "c1")


def test_find_devices_near_a_point_builds_a_layer(city):
    ctx = make_ctx(city)
    res, err = run_tool(ctx, "find_devices", {
        "kind": "parking_sensor", "near_lat": 35.1725, "near_lon": 33.3615, "radius_m": 300})
    assert not err, res
    assert res["devices_of_this_kind"] == 303
    assert res["by_state"] == {"occupied": res["matched"]}  # the fixture says status 1
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["count"] == res["matched"] > 0
    assert all(i["distance_m"] <= 300 for i in res["items"])
    assert res["items"] == sorted(res["items"], key=lambda i: i["distance_m"])


def test_run_tool_validates_input_and_names(city):
    ctx = make_ctx(city)
    res, err = run_tool(ctx, "find_devices", {"kind": "lamp"})
    assert err and "invalid input at kind" in res["error"]
    res, err = run_tool(ctx, "no_such_tool", {})
    assert err and "unknown tool" in res["error"]
    res, err = run_tool(ctx, "Find_Devices", {"kind": "parking_sensor", "state": "parked"})
    assert err and "state for parking_sensor must be one of" in res["error"]


def test_show_chart_fills_data_from_dataset(monkeypatch):
    monkeypatch.setattr(tools_mod, "vega_lite_validator", lambda s: None)
    events = []
    ctx = make_ctx(events=events)
    ref = ctx.store.add_dataset("speed", [{"hour": "10:00", "kmh": 41.2}], title="speed")
    spec = {"mark": "line", "encoding": {"x": {"field": "hour"}, "y": {"field": "kmh"}}}
    res, err = run_tool(ctx, "show_chart", {"title": "Speed", "spec": spec, "data_ref": ref})
    assert not err and res["rows_plotted"] == 1
    sent = events[-1]["spec"]
    assert sent["data"] == {"values": [{"hour": "10:00", "kmh": 41.2}]}
    assert sent["width"] == "container"
    res, err = run_tool(ctx, "show_chart", {"title": "F", "data_ref": ref, "spec": {
        "facet": {"field": "hour"}, "spec": {"mark": "point"}}})
    assert not err and "width" not in events[-1]["spec"]
    res, err = run_tool(ctx, "show_chart", {"title": "x", "spec": spec, "data_ref": "D9_nope"})
    assert err and "unknown data_ref" in res["error"]


def test_show_on_map_checks_style_against_geometry():
    events = []
    ctx = make_ctx(events=events)
    ref = ctx.store.add_layer("bins", [], geometry="point", title="Bins", value_fields=["level_pct"])
    res, err = run_tool(ctx, "show_on_map", {"layer_ref": ref, "style": "route"})
    assert err and "point layer" in res["error"]
    res, err = run_tool(ctx, "show_on_map", {"layer_ref": ref, "style": "columns_3d"})
    assert err and "needs value_field" in res["error"]
    res, err = run_tool(ctx, "show_on_map", {"layer_ref": ref, "style": "columns_3d",
                                              "value_field": "level_pct"})
    assert not err
    assert events[-1]["layer"]["url"] == f"/api/layers/c1/{ref}"


# -- agent loop ---------------------------------------------------------------------------------


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def final(content, stop_reason):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage,
                           model="claude-sonnet-5-5")


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for b in self.message.content:
                yield SimpleNamespace(type="content_block_start", content_block=b)
                if b.type == "text":
                    yield SimpleNamespace(type="content_block_delta",
                                          delta=SimpleNamespace(type="text_delta", text=b.text))
        return gen()

    async def get_final_message(self):
        return self.message


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.script.pop(0))


def run_agent(script, text="hi"):
    fake = FakeClaude(script)
    city = SimpleNamespace(now=lambda: utc(2026, 10, 20, 9, 0))
    agent = Agent(Settings(), city, None, None, client=fake)
    conv = agent.conversation(None)
    events = []

    async def go():
        await agent.run_turn(conv, text, {"map_view": {"center": [33.36, 35.17], "zoom": 14}},
                             events.append)
        await asyncio.sleep(0)  # let thread-safe emits land

    asyncio.run(go())
    return fake, conv, events


def test_agent_runs_tools_and_appends_history_unchanged():
    first = final([block("text", text="Let me look."),
                   block("tool_use", id="t1", name="map_clear", input={})], "tool_use")
    second = final([block("text", text="Done.")], "end_turn")
    fake, conv, events = run_agent([first, second])

    roles = [m["role"] for m in conv.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert conv.messages[1]["content"] is first.content  # appended unchanged
    result = conv.messages[2]["content"][0]
    assert result["tool_use_id"] == "t1" and result["is_error"] is False
    assert "<context>" in conv.messages[0]["content"][0]["text"]

    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds and "tool_end" in kinds and "usage" in kinds
    assert any(e.get("action") == "clear" for e in events)
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Let me look.Done."

    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert call["thinking"]["type"] == "adaptive"
    assert all(t.get("eager_input_streaming") for t in call["tools"] if "input_schema" in t)
    assert "tool_choice" not in call  # forced tool choice is a 400 on Sonnet 5.5


def test_agent_never_runs_a_tool_cut_off_by_max_tokens():
    cut = final([block("tool_use", id="t1", name="map_clear", input={})], "max_tokens")
    _, conv, events = run_agent([cut])
    assert not any(e.get("action") == "clear" for e in events)
    last = conv.messages[-1]
    assert last["role"] == "user" and last["content"][0]["is_error"] is True
    assert any(e["type"] == "error" for e in events)


def test_agent_stops_on_refusal():
    _, conv, events = run_agent([final([], "refusal")])
    assert conv.messages[-1]["role"] == "assistant"
    assert any(e["type"] == "error" and "declined" in e["message"] for e in events)


def test_system_prompt_has_no_per_turn_values():
    # Anything that changes per turn would break the prompt cache.
    assert "{" not in agent_mod.SYSTEM_PROMPT and "2026" not in agent_mod.SYSTEM_PROMPT

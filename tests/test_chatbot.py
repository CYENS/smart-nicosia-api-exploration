"""Chatbot: tour solver, polyline decoding, tools against captured payloads, and the agent loop
against a scripted fake of the Claude streaming API (no network, no API key)."""

import asyncio
import itertools
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from chatbot import agent as agent_mod
from chatbot import history
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
    assert events[-1]["layer"]["timeseries"] is False


# -- history (time-lapse) -----------------------------------------------------------------------


def test_get_history_replays_parking_through_the_interpreter(city, api):
    now = city.now()
    times = history.frame_times(now, 4, 60)
    assert len(times) == 5 and all(t.minute == 0 for t in times) and times[-1] <= now
    ms_at = lambda t: str(int(t.timestamp() * 1000))  # noqa: E731
    records = [  # newest first, as the API returns them
        {"key": "status", "timestamp": ms_at(times[3]), "value": "0"},
        {"key": "status", "timestamp": ms_at(times[1]), "value": "1"},
        {"key": "vbatt", "timestamp": ms_at(times[0]), "value": "3.6"},
    ]
    api.on("getTelemetryRange", lambda p: records if p["keys"] == "status,vbatt" else [])
    events = []
    ctx = make_ctx(city, events)
    res, err = run_tool(ctx, "get_history", {"kind": "parking_sensor", "hours": 4,
                                             "step_minutes": 60, "near_lat": 35.1725,
                                             "near_lon": 33.3615, "radius_m": 300})
    assert not err, res
    assert res["frames"] == 5 and res["devices"] > 0
    layer = ctx.store.layers[res["layer_ref"]]
    fr = layer["frames"]
    names = [[fr["state_names"][i] for i in row] for row in fr["states"]]
    # Before any status the bay is unknown; then occupied for two frames, then free.
    assert [row[0] for row in names] == ["unknown", "occupied", "occupied", "free", "free"]
    assert layer["meta"]["frames"] == 5
    assert layer["features"][0]["properties"]["state"] == "free"  # the last frame
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert [r["occupancy_pct"] for r in rows] == [None, 100.0, 100.0, 0.0, 0.0]
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "frame", "layer": res["layer_ref"]}

    res, err = run_tool(ctx, "show_on_map", {"layer_ref": res["layer_ref"], "style": "points"})
    assert not err and "timelapse" in res and events[-1]["layer"]["timeseries"] is True

    res, err = run_tool(ctx, "get_history", {"kind": "parking_sensor", "hours": 72,
                                             "step_minutes": 5})
    assert err and "frames" in res["error"]


def test_history_lamp_off_by_day_offline_at_night():
    from chatbot.city import Device, DeviceState
    from collector.interpret import Interpretation
    dev = Device("YL1", "Lighting", 35.17, 33.36)
    st = DeviceState(dev, "Street light 1", "Street",
                     Interpretation("Lighting", "off", False, None, None, ""))
    noon, night = utc(2026, 10, 20, 9, 0), utc(2026, 10, 20, 20, 0)  # 12:00 and 23:00 local
    lit_at = utc(2026, 10, 20, 19, 30)
    recs = {"YL1": [{"key": "level", "timestamp": str(int(utc(2026, 10, 19, 3, 0).timestamp() * 1000)),
                     "value": "100"},
                    {"key": "level", "timestamp": str(int(lit_at.timestamp() * 1000)), "value": "100"}]}
    states, values = history.replay("Lighting", [st], recs, [noon, night, utc(2026, 10, 20, 22, 0)])
    assert [s[0] for s in states] == ["off", "on", "offline"]
    assert values["lit"] == [[0], [1], [0]]


# -- chart linking and custom visuals -----------------------------------------------------------


def test_link_chart_fades_by_id_and_draws_a_frame_rule():
    spec = {"mark": "bar", "encoding": {"x": {"field": "id"}, "y": {"field": "level_pct"}}}
    assert tools_mod.link_chart(spec, "id")
    assert spec["encoding"]["opacity"]["condition"]["test"].startswith("_hl == null")
    assert spec["params"] == [{"name": "_hl", "value": None}]
    assert not tools_mod.link_chart(spec, "id")  # never wired twice

    spec = {"mark": "bar", "width": "container",
            "encoding": {"x": {"field": "label", "type": "ordinal"}, "y": {"field": "on"}}}
    assert tools_mod.link_chart(spec, "frame")
    assert "mark" not in spec and spec["width"] == "container"
    view, rule = spec["layer"]
    assert view["mark"] == "bar" and "opacity" not in view["encoding"]
    assert rule["mark"]["type"] == "rule" and rule["encoding"]["x"]["field"] == "label"
    assert spec["params"] == [{"name": "_hl", "value": None}]

    # Over real time the frame link steps aside: add_time_rule draws the moving line.
    timed = {"mark": "line", "encoding": {"x": {"field": "time", "type": "temporal"}}}
    assert not tools_mod.link_chart(timed, "frame")

    facet = {"facet": {"field": "state"}, "spec": {"mark": "point"}}
    assert not tools_mod.link_chart(facet, "id")


def test_add_time_rule_draws_a_line_at_t_on_temporal_charts():
    spec = {"mark": "line", "encoding": {
        "x": {"field": "hour", "type": "temporal", "title": "Hour"}, "y": {"field": "kmh"}}}
    assert tools_mod.add_time_rule(spec) == "hour"
    view, rule = spec["layer"]
    assert view["mark"] == "line"
    assert rule["data"] == {"values": [{}]}
    assert rule["transform"] == [{"filter": "_t != null"}, {"calculate": "_t", "as": "hour"}]
    assert rule["encoding"]["x"] == {"field": "hour", "type": "temporal", "title": "Hour"}
    assert {"name": "_t", "value": None} in spec["params"]
    assert tools_mod.add_time_rule(spec) is None  # never twice
    ordinal = {"mark": "bar", "encoding": {"x": {"field": "camera", "type": "nominal"}}}
    assert tools_mod.add_time_rule(ordinal) is None


def test_linked_specs_pass_the_vega_lite_schema():
    validator = tools_mod.vega_lite_validator(Settings())
    if validator is None:
        pytest.skip("Vega-Lite schema not cached")
    linked = [
        ({"mark": "bar", "encoding": {"x": {"field": "id", "type": "nominal"},
                                      "y": {"field": "level_pct", "type": "quantitative"}}}, "id"),
        ({"mark": "bar", "encoding": {"x": {"field": "label", "type": "ordinal"},
                                      "y": {"field": "on", "type": "quantitative"}}}, "frame"),
    ]
    for spec, key in linked:
        assert tools_mod.link_chart(spec, key)
        assert tools_mod.spec_error(validator, spec, True) is None, spec
    timed = [
        {"mark": {"type": "line", "point": True},
         "encoding": {"x": {"field": "time", "type": "temporal"},
                      "y": {"field": "on", "type": "quantitative"}}},
        {"mark": "bar", "encoding": {"x": {"field": "hour", "timeUnit": "hours"},
                                     "y": {"field": "people", "aggregate": "sum"}}},
        {"layer": [{"mark": "area", "encoding": {"x": {"field": "time", "type": "temporal"},
                                                 "y": {"field": "on", "type": "quantitative"}}},
                   {"mark": "line", "encoding": {"x": {"field": "time", "type": "temporal"},
                                                 "y": {"field": "off", "type": "quantitative"}}}]},
    ]
    for spec in timed:
        assert tools_mod.add_time_rule(spec)
        assert tools_mod.spec_error(validator, spec, True) is None, spec
    # Both on one chart: bins over time, linked by id and with the moving line.
    both = {"mark": "point", "encoding": {"x": {"field": "as_of", "type": "temporal"},
                                          "y": {"field": "level_pct", "type": "quantitative"}}}
    assert tools_mod.link_chart(both, "id") and tools_mod.add_time_rule(both)
    assert tools_mod.spec_error(validator, both, True) is None, both


def test_show_chart_sends_the_link_for_linked_datasets(monkeypatch):
    monkeypatch.setattr(tools_mod, "vega_lite_validator", lambda s: None)
    events = []
    ctx = make_ctx(events=events)
    ref = ctx.store.add_dataset("bins", [{"id": "YW1", "level_pct": 80}], title="b",
                                link={"key": "id"})
    spec = {"mark": "bar", "encoding": {"x": {"field": "id"}, "y": {"field": "level_pct"}}}
    res, err = run_tool(ctx, "show_chart", {"title": "Bins", "spec": spec, "data_ref": ref})
    assert not err and res["linked_to_map"] is True
    assert events[-1]["link"] == {"key": "id"} and "opacity" in events[-1]["spec"]["encoding"]
    assert events[-1]["time_field"] is None
    assert "opacity" not in spec["encoding"]  # the model's spec object is left alone

    speed = ctx.store.add_dataset("speed", [{"hour": "2026-10-20T10:00", "speed_kmh": 41.2}],
                                  title="s")
    spec = {"mark": "line", "encoding": {"x": {"field": "hour", "type": "temporal"},
                                         "y": {"field": "speed_kmh"}}}
    res, err = run_tool(ctx, "show_chart", {"title": "Speed", "spec": spec, "data_ref": speed})
    assert not err and events[-1]["time_field"] == "hour" and events[-1]["link"] is None
    assert events[-1]["spec"]["layer"][1]["mark"]["type"] == "rule"


def test_show_chart_colours_states_like_the_map(monkeypatch):
    monkeypatch.setattr(tools_mod, "vega_lite_validator", lambda s: None)
    events = []
    ctx = make_ctx(events=events)
    rows = [{"id": "a", "state": "full"}, {"id": "b", "state": "low"}, {"id": "c", "state": "high"}]
    ref = ctx.store.add_dataset("bins", rows, title="b")
    spec = {"mark": "bar", "encoding": {"x": {"field": "id"}, "color": {"field": "state"}}}
    run_tool(ctx, "show_chart", {"title": "Bins", "spec": spec, "data_ref": ref})
    scale = events[-1]["spec"]["encoding"]["color"]["scale"]
    assert scale["domain"] == ["low", "high", "full"]  # the bins' own order
    assert scale["range"][2] == tools_mod.STATE_COLORS["full"]
    assert "scale" not in spec["encoding"]["color"]


def test_show_visual_checks_refs_and_sends_data():
    events = []
    ctx = make_ctx(events=events)
    d = ctx.store.add_dataset("speed", [{"hour": "10:00", "kmh": 41.2}], title="s")
    lyr = ctx.store.add_layer("bins", [], geometry="point", title="Bins")
    code = "root.textContent = DATA_LIST[0].length;"
    res, err = run_tool(ctx, "show_visual", {"title": "T", "code": code, "data_refs": ["D9_x"]})
    assert err and "unknown data_ref" in res["error"]
    res, err = run_tool(ctx, "show_visual", {"title": "T", "code": code, "data_refs": [d],
                                             "layer_refs": [lyr], "libraries": ["plot"]})
    assert not err and res["rows"] == {d: 1}
    ev = events[-1]
    assert ev["action"] == "visual" and ev["data"][d] == [{"hour": "10:00", "kmh": 41.2}]
    assert ev["data_order"] == [d] and "meta" not in ev["layers"][lyr]
    assert ev["height"] == 380 and ev["colors"]["full"]
    res, err = run_tool(ctx, "show_visual", {"title": "T", "code": code, "libraries": ["jquery"]})
    assert err and "libraries" in res["error"]


def test_context_reports_visual_errors():
    text = agent_mod.format_context({"visual_errors": ["Clock: d3 is not defined (line 3)"]},
                                    utc(2026, 10, 20, 9, 0))
    assert "visual_errors: Clock: d3 is not defined (line 3)" in text


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


# -- shared password ----------------------------------------------------------------------------


def _basic(user: str, password: str) -> str:
    import base64
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_password_ok_takes_any_username():
    from chatbot.server import password_ok
    assert password_ok(_basic("", "s3cret"), "s3cret")
    assert password_ok(_basic("anyone", "s3cret"), "s3cret")
    assert password_ok(_basic("x", "pa:ss"), "pa:ss")  # only the first colon splits
    for bad in (None, "", _basic("x", "wrong"), "Bearer s3cret", "Basic %%%", "Basic czNjcmV0"):
        assert not password_ok(bad, "s3cret")


def _call(app, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/", "headers": list(headers)}
    asyncio.run(app(scope, receive, send))
    return sent


def test_shared_password_guards_the_site_only_when_set(monkeypatch):
    from chatbot.server import SharedPassword

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = SharedPassword(inner)
    monkeypatch.delenv("CHATBOT_PASSWORD", raising=False)
    assert _call(guarded)[0]["status"] == 200

    monkeypatch.setenv("CHATBOT_PASSWORD", "s3cret")
    denied = _call(guarded)[0]
    assert denied["status"] == 401
    assert any(k == b"www-authenticate" and v.startswith(b"Basic") for k, v in denied["headers"])
    assert _call(guarded, [(b"authorization", _basic("x", "nope").encode())])[0]["status"] == 401
    assert _call(guarded, [(b"authorization", _basic("x", "s3cret").encode())])[0]["status"] == 200

"""The chat check (tools/chat_check) itself, offline: its questions parse and name real tools, it
reads a turn's stream into what reached the page, and its checks catch what they should. It never
asks the model anything here."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("chat_check", ROOT / "tools" / "chat_check" / "chat_check.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

FILES = sorted((ROOT / "tools" / "chat_check").glob("*.toml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_questions_parse_and_have_expectations(path):
    qs = cc.load_questions(path)
    assert qs
    for q in qs:
        assert q["tools"], f"{q['id']}: say which tool it needs"
        for key in q:
            assert key in {"id", "feature", "ask", "tools", "tools_not", "visual", "say", "say_any",
                           "not_say", "ids", "no_chart", "no_next", "args", "clock", "location",
                           "inspecting", "subject", "follows", "map_center", "zoom", "city_3d"}, \
                f"{q['id']}: unknown field {key}"
        for pat in (q.get("say") or []) + (q.get("say_any") or []) + (q.get("not_say") or []):
            __import__("re").compile(pat)
        cc.context_for(q, datetime(2026, 11, 28, 10, 0, tzinfo=cc.TZ), [])


def test_every_feature_has_a_question_that_draws_on_the_map():
    qs = cc.load_questions(ROOT / "tools" / "chat_check" / "questions.toml")
    by_feature: dict[str, list[dict]] = {}
    for q in qs:
        by_feature.setdefault(q["feature"], []).append(q)
    for feature, items in by_feature.items():
        assert any(any(v.startswith(("layer", "inspect")) for v in q.get("visual") or []) for q in items), \
            f"{feature}: no question that should draw on the map"


def test_wall_times_read_like_the_clock():
    now = datetime(2026, 11, 28, 10, 0, tzinfo=cc.TZ)  # a Saturday
    assert cc.wall("yesterday 21:00", now) == "2026-11-27T21:00"
    assert cc.wall("-3d 08:30", now) == "2026-11-25T08:30"
    assert cc.wall("fri 21:00", now) == "2026-11-27T21:00"
    assert cc.wall("sat 21:00", now) == "2026-11-21T21:00"  # the one before today
    assert cc.wall("2026-11-20T07:15", now) == "2026-11-20T07:15"


def test_numbers_in_skips_times_dates_ids_and_small_counts():
    text = "At 21:00 on 2026-11-27, 143 of 240 lamps drew 12.5 kW; YL1015 and road-12 were 2 of them.\n1. one"
    assert cc.numbers_in(text) == ["143", "240", "12.5"]
    pool = cc.numbers_from(['{"on": 143, "total": 240, "kw": 12.49}'])
    assert all(cc.found(n, pool) for n in cc.numbers_in(text))
    assert not cc.found("999", pool)


class FakeBackend:
    def get_json(self, path):
        return {"features": [{"id": 1}, {"id": 2}]}


def stream(*tools, text="Two lamps are on: YL1015 and YL1016.", charts=1, layer_style="points"):
    events = [{"type": "conversation", "id": "c1", "run": "r1"}]
    for i, (name, ok) in enumerate(tools):
        events += [{"type": "tool_start", "id": f"t{i}", "name": name},
                   {"type": "tool_end", "id": f"t{i}", "name": name, "ok": ok, "summary": "2 lamps"}]
    events += [{"type": "ui", "action": "show_layer", "layer": {"ref": "L1_lamps", "url": "/api/layers/c1/L1_lamps",
                                                                "style": layer_style, "title": "Lamps",
                                                                "sources": ["smart_nicosia"], "count": 2}}]
    for _ in range(charts):
        events.append({"type": "ui", "action": "chart", "title": "Lamps", "spec": {"data": {"values": [{"a": 1}]}},
                       "sources": ["smart_nicosia"], "link": {"key": "id"}})
    events += [{"type": "text", "text": text},
               {"type": "usage", "input": 100, "output": 50, "cache_read": 20000, "cache_write": 0, "model": "claude-sonnet-5-5"},
               {"type": "follow_ups", "next": ["Show them in 3D"]}, {"type": "done"}]
    return events


def check(q, events, actions=frozenset({"show_layer", "chart"})):
    d = cc.digest(events, FakeBackend())
    labels = {"find_devices": "Finding devices", "show_on_map": "Drawing on the map", "show_chart": "Drawing a chart"}
    return d, *cc.check(q, d, labels, cc.re.compile(r"\b(Y[LPW]\d{2,6})\b"), set(actions), [])


def test_a_good_answer_passes():
    q = {"id": "x", "tools": ["find_devices"], "visual": ["layer:points", "chart"], "ids": True, "say": ["lamps"]}
    d, problems, warnings = check(q, stream(("find_devices", True), ("show_on_map", True), ("show_chart", True)))
    assert problems == [] and warnings == []
    assert d["layers"][0]["features"] == 2 and d["charts"][0]["rows"] == 1
    assert cc.cost_of(d["usage"]) == pytest.approx((100 * 2 + 50 * 10 + 20000 * 0.1) / 1e6)


def test_a_bad_answer_is_caught():
    q = {"id": "x", "tools": ["get_history", "find_devices|query_history"], "tools_not": ["show_chart"],
         "visual": ["layer:route", "card:watch_confirm"], "ids": True, "not_say": ["is set"], "no_chart": True}
    events = stream(("find_devices", True), ("show_chart", True), ("mystery_tool", False),
                    text="The watch is set — 143 lamps.")
    events.insert(-1, {"type": "ui", "action": "warp"})
    _, problems, warnings = check(q, events)
    found = "\n".join(problems)
    for expected in ("did not call get_history", "called show_chart", "no layer:route", "no card:watch_confirm",
                     "no id", "'is set'", "dash", "no label for mystery_tool", "'warp'"):
        assert expected in found, expected
    assert any("chart" in w for w in warnings)


def test_numbers_without_a_data_tool_are_a_problem():
    q = {"id": "x", "tools": ["show_on_map"]}
    _, problems, _ = check(q, stream(("show_on_map", True), text="There are 143 lamps on.", charts=0))
    assert any("no data tool" in p for p in problems)


def test_traced_results_check_numbers_args_and_ids():
    events = stream(("get_road_speeds", True), text="Road road-12 runs at 23 km/h against 48; BAD-ID-9 is slow.")
    events[2].update(input={"road": "Makariou"}, result=json.dumps({"rows": [{"id": "road-12", "speed_kmh": 23.2},
                                                                              {"id": "BAD-ID-9"}], "free_flow": 51}))
    q = {"id": "x", "tools": ["get_road_speeds"], "args": {"get_road_speeds": ["at"]}}
    d = cc.digest(events, FakeBackend())
    problems, warnings = cc.check(q, d, {"get_road_speeds": "Reading the road speeds"},
                                  cc.re.compile(r"\b(road-\d{1,5})\b"), {"show_layer", "chart"}, [])
    assert "get_road_speeds was not given at" in problems
    assert any("48" in w for w in warnings) and not any("23" in w.split(":")[-1] for w in warnings)
    assert any("BAD-ID-9" in w for w in warnings)


def test_follow_ups_go_after_what_they_follow():
    qs = [{"id": "a"}, {"id": "b", "follows": "a"}, {"id": "c"}, {"id": "d", "follows": "b"}]
    assert [[q["id"] for q in g] for g in cc.chains(qs)] == [["a", "b", "d"], ["c"]]


def test_it_spends_nothing_without_spend(capsys):
    assert cc.main(["--base", "http://127.0.0.1:1"]) == 0
    assert "Nothing was asked" in capsys.readouterr().out


def test_the_page_parsers_read_the_page():
    assert cc.id_in_text().search("see YL1015")
    assert {"chart", "show_layer"} <= cc.page_actions()
    assert cc.tool_labels().get("show_on_map")

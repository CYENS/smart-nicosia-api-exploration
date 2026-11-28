"""Every chat tool keeps one contract with the model and the page, checked over the whole registry
so a tool that arrives in a new domain module is covered without being listed here.

- It has a description and a strict schema that accepts a sensible example and turns away a bad
  one (run_tool answers with an error before the handler runs).
- The trace names it in plain words (TOOL_LABELS in lib/chat.ts) and its layers and datasets say
  where they come from (TOOL_SOURCES, or an exemption with its reason in tool_contracts.py).
- The system prompt is byte-for-byte stable, names no tool that does not exist, and gives every
  tool some guidance.
- Every ui command a tool sends is one the page handles, and some offline test runs it.

What each run returns (size, refs, ids, sources) is checked by the tool_contracts plugin on every
tool run of the whole suite.
"""

from __future__ import annotations

import copy
import importlib
import json
import re
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest

from chatbot import prompt as prompt_mod
from chatbot import sources
from chatbot.tools import registry
from chatbot.tools.registry import TOOLS, run_tool

import tool_contracts as contracts

NAMES = [t.name for t in TOOLS]
BY_NAME = {t.name: t for t in TOOLS}
LABELS = contracts.tool_labels()
TESTS = Path(__file__).resolve().parent
SERVER_TOOLS = {"web_search"}  # run by the API, not the registry

# The order of the domains as it stood when this test was written: new domains go after it,
# because the tool list is part of the prompt cache key.
DOMAIN_ORDER = ("city", "places", "routes", "buildings", "maps", "charts", "memory", "entities",
                "clock", "mobility", "watch", "environment", "views", "scenarios")

# Inputs for tools whose schema alone doesn't give a sensible example (the generated one is
# used for every other tool).
EXAMPLES: dict[str, dict] = {
    "show_chart": {"title": "Free bays by hour", "data_ref": "D1_parking",
                   "spec": {"mark": "line", "encoding": {"x": {"field": "time", "type": "temporal"},
                                                          "y": {"field": "free", "type": "quantitative"}}}},
    "show_visual": {"title": "A day on one clock", "code": "root.textContent = 'ok'",
                    "data_refs": ["D1_parking"]},
    "run_sql": {"sql": "SELECT count(*) AS readings FROM telemetry"},
    "plan_route": {"stops": [{"place": "Eleftheria Square"}, {"place": "Ledra Street"}], "mode": "walk"},
    "map_callouts": {"items": [{"title": "Eleftheria Square", "lat": 35.1700, "lon": 33.3610}]},
}

# Tools the system prompt may leave unnamed, with the reason the model still finds them.
NO_GUIDANCE: dict[str, str] = {}

# Words in the prompt that look like a tool (a verb of a tool name, an underscore) but name
# something else; the schema properties, enum values, context keys and page actions are allowed
# without listing them here. Result fields the prompt points at go here.
CONTEXT_KEYS = {"local_time", "map_view", "layers_on_map", "city_3d", "user_location",
                "inspector_open", "asking_about", "city_clock", "viewing", "visual_errors"}
RESULT_FIELDS = {"sun_pct"}

# Gaps this test found on main that are fixed in a later change: test id -> what is missing.
# Each is an expected failure, strict, so the fix has to take it off this list.
KNOWN_GAPS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _known_gap(request):
    if reason := KNOWN_GAPS.get(request.node.name):
        request.applymarker(pytest.mark.xfail(reason=reason, strict=True))


# -- examples -----------------------------------------------------------------------------------


def example_value(name: str, schema: dict):
    """A sensible value for one property, from its schema and its name."""
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return example_value(name, schema[key][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object":
        return example_object(schema)
    if kind == "array":
        item = example_value(name, schema.get("items") or {"type": "string"})
        return [item] * max(1, schema.get("minItems", 1))
    if kind == "boolean":
        return True
    if kind in ("number", "integer"):
        low = schema.get("minimum", schema.get("exclusiveMinimum", 0) + (1 if kind == "integer" else 0.5))
        high = schema.get("maximum", low + 10)
        value = min(max(low, 1), high)
        return int(value) if kind == "integer" else float(value)
    # A string: what the name suggests.
    n = name.lower()
    if n in ("at", "from_time", "start_time", "until", "time") or n.endswith("_at"):
        return "2026-11-27T21:00"
    if "id" in n.split("_") or n.endswith("_id") or n == "id":
        return "YL1015"
    if "place" in n or n in ("from", "to", "near", "query", "street"):
        return "Eleftheria Square"
    if "ref" in n:
        return "L1_layer"
    value = "test"
    return value + "x" * max(0, schema.get("minLength", 0) - len(value))


def example_object(schema: dict) -> dict:
    props = schema.get("properties") or {}
    return {k: example_value(k, props.get(k, {})) for k in schema.get("required") or []}


def example_for(name: str) -> dict:
    return copy.deepcopy(EXAMPLES.get(name) or example_object(BY_NAME[name].definition["input_schema"]))


class Untouchable:
    """A context no handler may touch: the schema must turn the input away first."""

    def __getattr__(self, item):
        raise AssertionError(f"the handler ran (it read ctx.{item}) on input the schema should refuse")


def wrong_value(schema: dict):
    """A value this property's schema refuses, or None when it takes anything."""
    for candidate in ({"__not_a_value__": 1}, "not a number", 12345, True, ["x"], "no_such_value"):
        try:
            jsonschema.validate(candidate, schema)
        except jsonschema.ValidationError:
            return candidate
    return None


# -- the tool itself ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_tool_has_a_name_and_a_description(name):
    d = BY_NAME[name].definition
    assert re.fullmatch(r"[a-z][a-z0-9_]{2,39}", name), name
    assert len(d["description"]) >= 60, f"{name}: say what it does, what it returns and when to use it"
    assert len(d["description"]) <= 3_500, f"{name}: the description is {len(d['description'])} characters"
    texts = [d["description"]] + [p.get("description", "") for p in d["input_schema"]["properties"].values()]
    for text in texts:
        assert "—" not in text and "–" not in text, f"{name}: a dash in {text[:80]!r}"


@pytest.mark.parametrize("name", NAMES)
def test_schema_is_strict_and_accepts_an_example(name):
    schema = BY_NAME[name].definition["input_schema"]
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get("type") == "object" and schema.get("additionalProperties") is False, \
        f"{name}: use obj(...) so unknown inputs are refused"
    props = schema.get("properties") or {}
    assert set(schema.get("required") or []) <= set(props), f"{name}: requires a property it doesn't define"
    for key, sub in props.items():
        assert any(k in sub for k in ("type", "enum", "const", "anyOf", "oneOf", "$ref")), \
            f"{name}.{key} has no type"
    example = example_for(name)
    jsonschema.validate(example, schema)


@pytest.mark.parametrize("name", NAMES)
def test_run_tool_turns_bad_input_away_before_the_handler(name):
    schema = BY_NAME[name].definition["input_schema"]
    example = example_for(name)
    bad = [example | {"__unknown__": 1}, ["not", "an", "object"]]
    for key in schema.get("required") or []:
        bad.append({k: v for k, v in example.items() if k != key})
    for key, sub in (schema.get("properties") or {}).items():
        wrong = wrong_value(sub)
        if wrong is not None:
            bad.append(example | {key: wrong})
    for args in bad:
        result, is_error = run_tool(Untouchable(), name, args)
        assert is_error, f"{name} accepted {args!r}"
        assert result["error"].startswith(("invalid input", "INVALID_JSON")), result["error"]


# -- the page and the sources -------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_the_trace_names_the_tool_in_plain_words(name):
    label = LABELS.get(name)
    assert label, f"add {name} to TOOL_LABELS in chatbot/web/src/lib/chat.ts"
    assert label[0].isupper() and "_" not in label and not label.endswith("."), label
    assert not any(d in label for d in contracts.DASHES), label
    assert len(label) <= 60, label


def test_every_label_names_a_tool():
    stale = sorted(set(LABELS) - set(NAMES) - SERVER_TOOLS)
    assert not stale, f"TOOL_LABELS names tools that don't exist: {stale}"


@pytest.mark.parametrize("name", NAMES)
def test_the_tool_says_where_its_data_comes_from(name):
    if name in contracts.NO_SOURCES or name in contracts.OWN_SOURCES:
        assert name not in sources.TOOL_SOURCES, f"{name} is both exempt and listed"
        return
    used = sources.TOOL_SOURCES.get(name)
    assert used, (f"add {name} to TOOL_SOURCES in chatbot/sources.py, or to NO_SOURCES in "
                  "tests/tool_contracts.py with the reason it reads no source")
    assert all(i in sources.BY_ID for i in used), used


def test_exemptions_name_real_tools():
    for table in (contracts.NO_SOURCES, contracts.OWN_SOURCES, contracts.MAX_CHARS, NO_GUIDANCE, EXAMPLES):
        assert set(table) <= set(NAMES), sorted(set(table) - set(NAMES))
    tests = {name for name, _ in globals().items() if name.startswith("test_")}
    for gap in KNOWN_GAPS:
        assert gap.split("[")[0] in tests, gap


def emitted_actions() -> dict[str, str]:
    """Every ui action the server's code sends, with the file that sends it."""
    root = Path(registry.__file__).resolve().parent.parent
    found = {}
    for path in root.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if '"type": "ui"' in line:
                for action in re.findall(r'"action":\s*"([a-z_0-9]+)"', line):
                    found[action] = path.relative_to(root.parent).as_posix()
    return found


def test_the_page_handles_every_ui_command():
    handled = contracts.page_ui_actions()
    sent = emitted_actions()
    assert sent, "no ui commands found: has the emit shape changed?"
    missing = {a: f for a, f in sent.items() if a not in handled}
    assert not missing, f"ui actions the page ignores: {missing}"


@pytest.mark.parametrize("name", NAMES)
def test_some_offline_test_runs_the_tool(name):
    # Through run_tool, so the tool contract plugin checks what it returns.
    pattern = re.compile(rf'run_tool\([^"\'\n]*?,\s*["\']{re.escape(name)}["\']')
    mine = {Path(__file__).name, Path(contracts.__file__).name}
    for path in TESTS.glob("test_*.py"):
        if path.name not in mine and pattern.search(path.read_text(encoding="utf-8")):
            return
    pytest.fail(f"no test runs {name} through run_tool: add one on fixtures (tests/test_tool_runs.py)")


# -- the system prompt --------------------------------------------------------------------------


def test_prompt_and_tool_list_are_byte_for_byte_stable():
    before = prompt_mod.SYSTEM_PROMPT
    defs = json.dumps(registry.TOOL_DEFS, ensure_ascii=False)
    importlib.reload(prompt_mod)
    assert prompt_mod.SYSTEM_PROMPT == before
    rebuilt = [t.definition for d in registry.DOMAINS for t in d.TOOLS]
    assert json.dumps(rebuilt, ensure_ascii=False) == defs
    # Nothing that changes per turn or per day: no placeholders, not today's date. (Example
    # dates in descriptions are fine: they never change.)
    today = datetime.now()
    for text in (before, defs):
        assert f"{today:%Y-%m-%d}" not in text and f"{today:%d %B %Y}" not in text
    assert "{" not in before


def test_domains_keep_their_order_and_new_ones_come_last():
    names = tuple(d.__name__.rsplit(".", 1)[-1] for d in registry.DOMAINS)
    assert names[:len(DOMAIN_ORDER)] == DOMAIN_ORDER, names


def schema_words() -> set[str]:
    words = set()

    def walk(s):
        if isinstance(s, dict):
            words.update(k for k in (s.get("properties") or {}))
            for v in s.get("enum") or []:
                if isinstance(v, str):
                    words.add(v)
            for v in s.values():
                walk(v)
        elif isinstance(s, list):
            for v in s:
                walk(v)

    for t in TOOLS:
        walk(t.definition["input_schema"])
    return words


def tool_like_words(text: str) -> set[str]:
    verbs = {n.split("_")[0] for n in NAMES} | {n.split("_")[0] for n in SERVER_TOOLS}
    words = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text))
    return {w for w in words if w.split("_")[0] in verbs}


def test_prompt_names_no_tool_that_does_not_exist():
    allowed = (set(NAMES) | SERVER_TOOLS | schema_words() | CONTEXT_KEYS | RESULT_FIELDS
               | contracts.page_action_keys())
    unknown = sorted(tool_like_words(prompt_mod.SYSTEM_PROMPT) - allowed)
    assert not unknown, f"the prompt names tools that don't exist: {unknown}"


@pytest.mark.parametrize("name", NAMES)
def test_prompt_gives_every_tool_some_guidance(name):
    if name in NO_GUIDANCE:
        return
    assert re.search(rf"\b{re.escape(name)}\b", prompt_mod.SYSTEM_PROMPT), \
        f"the system prompt never names {name}: say which questions it answers and what to show"


# -- the result checker itself ------------------------------------------------------------------


def test_result_checker_finds_what_it_should():
    from chatbot.store import SessionStore, point

    store = SessionStore()
    layer = store.add_layer("bays", [point(33.36, 35.17, {"id": "YP974"})], geometry="point",
                            title="Bays", sources=["smart_nicosia"])
    linked = store.add_dataset("bays", [{"id": "YP974", "free": 1}], title="Bays",
                               link={"key": "id"}, sources=["smart_nicosia"])
    good = {"layer_ref": layer, "data_ref": linked, "_summary": "1 bay"}
    assert contracts.result_problems("find_devices", good, store) == []

    unlinked = store.add_dataset("bays", [{"id": "YP1", "free": 1}], title="Bays", sources=["smart_nicosia"])
    nameless = store.add_dataset("x", [{"v": 1}], title="X", sources=[])
    stray = store.add_dataset("bays", [{"id": "YP2"}], title="Bays", link={"key": "id"},
                              sources=["smart_nicosia"])
    bad = {"layer_ref": "L99_gone", "data_refs": {"a": unlinked, "b": nameless}, "by_data_ref": stray,
           "error": "oops", "rows": ["x" * 13_000], "_summary": "a — b"}
    found = "\n".join(contracts.result_problems("find_devices", bad, store))
    for expected in ("over the bound", "carries `error`", "dash", "L99_gone", "no link",
                     "names no sources"):
        assert expected in found, expected
    found = "\n".join(contracts.result_problems("find_devices", {"layer_ref": layer, "data_ref": stray}, store))
    assert "no layer of this call draws" in found

    actions = {"show_layer", "chart"}
    events = [{"type": "ui", "action": "warp"}, {"type": "ui", "action": "show_layer", "layer": {"ref": "L9"}},
              {"type": "ui", "action": "chart", "spec": {"mark": "bar"}}]
    found = "\n".join(contracts.emit_problems(events, store, actions))
    assert "'warp'" in found and "L9" in found and "no data" in found


def test_page_parsers_find_the_known_actions_and_labels():
    actions = contracts.page_ui_actions()
    assert {"chart", "visual", "clock", "inspect", "show_layer", "fly_to", "clear"} <= actions
    assert "share_location" in contracts.page_action_keys()
    assert LABELS.get("show_on_map")

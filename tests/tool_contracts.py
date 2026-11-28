"""The contract every chat tool keeps, and a pytest plugin that checks it on every tool run.

The static half reads the registry, the system prompt and the page's sources (lib/chat.ts, the
map engine, the turn cards), so a tool that arrives in a new domain module is covered without
anyone listing it here. tests/test_tool_contracts.py runs it.

The running half wraps every handler in the registry. Whenever any test runs a tool through
run_tool, the wrapper checks what came back and what the tool pushed to the page:

- the result is compact JSON (under a size bound) and a successful one carries no `error`;
- every `layer_ref` and `data_ref` in it (and in `layer_refs` and `data_refs`) is in the
  session store, and what they hold names its sources;
- rows that carry an `id` are linked to the map, and a linked dataset's ids are on the layer
  the same call made;
- every `ui` command is one the page handles, and a layer it shows is in the store;
- the short summary the trace shows is plain words.

A test that makes a tool break the contract fails with the reason. tests/conftest.py loads it
for the whole suite. The terminal summary lists the tools no test ran successfully.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from functools import wraps
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "chatbot" / "web" / "src"

# -- what the page knows ------------------------------------------------------------------------


def tool_labels() -> dict[str, str]:
    """TOOL_LABELS in lib/chat.ts: how the trace names each tool."""
    text = (WEB / "lib" / "chat.ts").read_text(encoding="utf-8")
    block = re.search(r"TOOL_LABELS\b[^=]*=\s*\{(.*?)\n\}", text, re.S)
    assert block, "TOOL_LABELS not found in lib/chat.ts"
    return dict(re.findall(r'\b([a-z][a-z0-9_]*)\s*:\s*"([^"]*)"', block.group(1)))


def page_ui_actions() -> set[str]:
    """Every `ui` action the page acts on: the ones lib/chat.ts handles itself, the map
    engine's (applyMapUi) and the cards features register under an answer."""
    chat = (WEB / "lib" / "chat.ts").read_text(encoding="utf-8")
    actions = set(re.findall(r'ev\.action\s*===\s*"([a-z_]+)"', chat))
    engine = (WEB / "lib" / "map" / "engine.ts").read_text(encoding="utf-8")
    body = re.search(r"export function applyMapUi\(.*?\n\}", engine, re.S)
    assert body, "applyMapUi not found in lib/map/engine.ts"
    actions |= set(re.findall(r'case\s+"([a-z_0-9]+)"', body.group(0)))
    for path in WEB.rglob("*.ts*"):
        actions |= set(re.findall(r'registerTurnCard\(\s*"([a-z_0-9]+)"', path.read_text(encoding="utf-8")))
    return actions


def page_action_keys() -> set[str]:
    """The keys of <do> actions (lib/actions.ts): the prompt may name them."""
    text = (WEB / "lib" / "actions.ts").read_text(encoding="utf-8")
    return set(re.findall(r"^\s{2}([a-z_0-9]+):\s*\{", text, re.M))


# -- the exemptions, each with its reason -------------------------------------------------------

# Tools that read no source of their own: what they show was made by another tool, which named
# its sources, or it is the lab's own catalogue.
NO_SOURCES = {
    "show_on_map": "draws a layer another tool made; the layer carries that tool's sources",
    "show_chart": "fills from a dataset another tool made; the chart carries its sources",
    "show_visual": "reads datasets and layers other tools made, with their sources",
    "map_fly_to": "moves the camera and turns the 3D city on; reads no data",
    "map_callouts": "pins labels the model writes; reads no data",
    "map_clear": "takes layers off the map",
    "open_inspector": "opens a thing in the inspector, which names its sources itself",
    "open_scenario": "opens a run run_scenario made, in the lab, which names its sources",
    "list_scenarios": "the scenario lab's own catalogue of what-ifs and their parameters",
}

# Tools whose things each name their own sources, so TOOL_SOURCES can't list them in advance;
# what they store is still checked for sources.
OWN_SOURCES = {
    "get_entity": "every kind of thing lists its sources; its layer and its day carry them",
}

# Fields that make a row a moment in a series: such rows link to the map through the time line
# (a chart over time follows the clock), so they need no id link.
TIME_FIELDS = ("time", "day", "date", "hour", "ts", "at", "start")

# Size bounds, in characters of the JSON the model reads. Tools whose answer is the data itself
# get a wider bound; everything else parks its rows in the store.
DEFAULT_MAX_CHARS = 12_000
MAX_CHARS = {
    "run_sql": 24_000,  # the rows are the answer; LIMIT is in the model's hands
    "get_entity": 16_000,  # one thing with its readings, relations and sources
    "explain_incident": 16_000,  # the evidence and what happened around it
    "list_scenarios": 16_000,  # every scenario with its parameters
}

# Tests that check what happens when a tool goes wrong on purpose (an oversized SQL answer...)
# can opt out with @pytest.mark.no_tool_contract.

REF_KEYS = ("layer_ref", "data_ref")
DASHES = ("—", "–", "--")


# -- the checks ---------------------------------------------------------------------------------


def _refs(value: object, key: str = "") -> list[tuple[str, str]]:
    """(key, ref) for every layer and dataset reference in a result, however deep."""
    out: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            if isinstance(v, str) and (k.endswith(REF_KEYS) or key in ("layer_refs", "data_refs")):
                out.append((k if k.endswith(REF_KEYS) else f"{key}.{k}", v))
            else:
                out += _refs(v, k)
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, str) and key in ("layer_refs", "data_refs"):
                out.append((key, v))
            else:
                out += _refs(v, key)
    return out


def _feature_ids(layer: dict) -> set[str]:
    """The values the page's highlight matches (lib/map/engine.ts highlightOnMap): a feature's
    id, and a bus's line id."""
    ids = set()
    for f in layer.get("features") or []:
        props = f.get("properties") or {}
        for v in (f.get("id"), props.get("id"), props.get("line_id")):
            if v is not None:
                ids.add(str(v))
    return ids


def result_problems(tool: str, result: dict, store) -> list[str]:
    """What is wrong with one successful result of `tool` against the session store."""
    problems: list[str] = []
    text = json.dumps(result, ensure_ascii=False, default=str)
    bound = MAX_CHARS.get(tool, DEFAULT_MAX_CHARS)
    if len(text) > bound:
        problems.append(f"result is {len(text):,} characters of JSON, over the bound of {bound:,}: "
                        "park the rows in the store and return a data_ref")
    if "error" in result:
        problems.append("a successful result carries `error`: raise ToolError instead")
    summary = result.get("_summary")
    if summary is not None:
        if not isinstance(summary, str) or len(summary) > 120:
            problems.append(f"_summary for the trace should be a short line: {summary!r}")
        elif any(d in summary for d in DASHES):
            problems.append(f"_summary has a dash the page would show: {summary!r}")

    layers = getattr(store, "layers", None)
    datasets = getattr(store, "datasets", None)
    if not isinstance(layers, dict) or not isinstance(datasets, dict):
        return problems  # a stand-in store: nothing to resolve against
    made_layers = []
    for key, ref in _refs(result):
        if ref.startswith("L") or "layer" in key:
            layer = layers.get(ref)
            if layer is None:
                problems.append(f"{key} {ref!r} is not in the session store")
                continue
            made_layers.append(layer)
            if tool not in NO_SOURCES and not (layer.get("meta") or {}).get("sources"):
                problems.append(f"layer {ref} names no sources: add {tool} to TOOL_SOURCES "
                                "or pass sources=")
        elif ref.startswith("D") or "data" in key:
            ds = datasets.get(ref)
            if ds is None:
                problems.append(f"{key} {ref!r} is not in the session store")
                continue
            if tool not in NO_SOURCES and not ds.get("sources"):
                problems.append(f"dataset {ref} names no sources: add {tool} to TOOL_SOURCES "
                                "or pass sources=")
            rows = ds.get("rows")
            if not isinstance(rows, list):
                problems.append(f"dataset {ref} has no rows list")
                continue
            if rows and not all(isinstance(r, dict) for r in rows):
                problems.append(f"dataset {ref} has rows that are not objects")
                continue
            with_id = [r for r in rows if r.get("id") not in (None, "")]
            link = ds.get("link") or {}
            in_time = rows and all(any(k in r for k in TIME_FIELDS) for r in rows)
            if with_id and not link and not in_time:
                problems.append(f"dataset {ref} has rows with ids but no link: pass "
                                "link={'key': 'id'} so its charts highlight the map")
            if link.get("key") == "id" and rows and len(with_id) < len(rows):
                problems.append(f"dataset {ref} is linked by id but {len(rows) - len(with_id)} "
                                "of its rows have none")
            ds["_checked_ids"] = {str(r["id"]) for r in with_id}
    # A dataset linked by id and a layer from the same call: its ids must be the layer's, or
    # hovering a bar highlights nothing. (A layer may leave some rows out to stay readable, so
    # only a dataset none of whose ids are drawn is wrong: ids written another way.)
    if made_layers:
        on_map = set().union(*(_feature_ids(layer) for layer in made_layers))
        for key, ref in _refs(result):
            ds = datasets.get(ref)
            if not ds or (ds.get("link") or {}).get("key") != "id":
                continue
            ids = ds.pop("_checked_ids", set())
            if on_map and ids and not ids & on_map:
                sample = sorted(ids)[:3]
                problems.append(f"dataset {ref} is linked by id but no layer of this call draws "
                                f"any of its ids ({sample}): write them as the features do")
    for ds in datasets.values():
        if isinstance(ds, dict):
            ds.pop("_checked_ids", None)
    return problems


def emit_problems(events: list[dict], store, known_actions: set[str]) -> list[str]:
    problems = []
    layers = getattr(store, "layers", None)
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "ui":
            continue
        action = ev.get("action")
        if action not in known_actions:
            problems.append(f"ui action {action!r} is not handled by the page "
                            "(lib/chat.ts, applyMapUi or a registered turn card)")
        if action == "show_layer" and isinstance(layers, dict):
            ref = (ev.get("layer") or {}).get("ref")
            if ref not in layers:
                problems.append(f"show_layer names {ref!r}, which is not in the store")
        if action == "chart" and not ((ev.get("spec") or {}).get("data") or (ev.get("spec") or {}).get("datasets")):
            problems.append("a chart reached the page with no data")
    return problems


# -- the plugin ---------------------------------------------------------------------------------

RUNS: dict[str, list[int]] = defaultdict(list)  # tool -> sizes of its successful results
PROBLEMS: list[tuple[str, str, str]] = []  # (test, tool, problem)
_ACTIONS: set[str] = set()


def _current_test() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]


def checked(name: str, handler):
    """`handler`, checked against the contract each time it returns."""
    if getattr(handler, "_contract", False):
        return handler

    @wraps(handler)
    def run(ctx, args):
        events: list[dict] = []
        emit = getattr(ctx, "emit", None)
        if callable(emit):
            def recording(ev, _emit=emit):
                events.append(ev)
                return _emit(ev)
            try:
                ctx.emit = recording
            except AttributeError:  # a frozen stand-in context
                emit = None
        try:
            result = handler(ctx, args)
        finally:
            if callable(emit):
                ctx.emit = emit
        if isinstance(result, dict):
            store = getattr(ctx, "store", None)
            found = result_problems(name, result, store) + emit_problems(events, store, _ACTIONS)
            RUNS[name].append(len(json.dumps(result, ensure_ascii=False, default=str)))
            PROBLEMS.extend((_current_test(), name, p) for p in found)
        return result

    run._contract = True
    return run


def install() -> None:
    from chatbot.tools import registry

    _ACTIONS.clear()
    _ACTIONS.update(page_ui_actions())
    for name, handler in list(registry.HANDLERS.items()):
        registry.HANDLERS[name] = checked(name, handler)


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "no_tool_contract: don't check this test's tool results "
                                       "against the tool contract (tests/tool_contracts.py)")
    install()


@pytest.fixture(autouse=True)
def _tool_contract(request):
    start = len(PROBLEMS)
    yield
    found = PROBLEMS[start:]
    del PROBLEMS[start:]
    if found and not request.node.get_closest_marker("no_tool_contract"):
        lines = "\n".join(f"  {tool}: {problem}" for _, tool, problem in found)
        pytest.fail(f"a tool broke the tool contract (tests/tool_contracts.py):\n{lines}",
                    pytrace=False)


def pytest_terminal_summary(terminalreporter) -> None:
    from chatbot.tools.registry import HANDLERS

    if not RUNS:
        return
    tr = terminalreporter
    never = sorted(n for n in HANDLERS if n not in RUNS)
    tr.write_sep("-", "tool contract")
    biggest = sorted(RUNS.items(), key=lambda kv: -max(kv[1]))[:5]
    tr.write_line(f"{len(RUNS)} of {len(HANDLERS)} tools ran successfully in these tests; the "
                  "largest results: " + ", ".join(f"{n} {max(s):,} chars" for n, s in biggest))
    if never:
        tr.write_line("never run successfully here: " + ", ".join(never))

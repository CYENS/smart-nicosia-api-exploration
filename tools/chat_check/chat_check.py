"""The chat check: ask a running twin's chat a list of questions and check every answer.

For each question it records the tools the agent called, in order, what reached the page (map
layers, charts, cards, clock and inspector commands), the time, the tokens and their cost, and
the final text, then checks them against the question's expectations (questions.toml) and the
rules every answer keeps:

- the tools it must call (and must not), and the visual it must bring: a layer of some style, a
  chart, a card, the clock, the inspector;
- what the answer must say, and must not;
- every tool has a label for the trace, every layer resolves and names its sources, every chart
  arrives with rows and its sources, every ui command is one the page handles;
- no em or en dash, ids written so the page turns them into chips;
- with CHATBOT_TRACE_RESULTS=1 on the backend (it adds each tool's input and result to the
  stream), the tools that must follow the city clock were given `at`, and the numbers in the
  answer are found in the tools' results.

It spends the owner's Claude API credit, so it never runs in CI or by default: without --spend
it lists the questions it would ask and stops.

    uv run tools/chat_check/chat_check.py --base http://127.0.0.1:8350             # list only
    uv run tools/chat_check/chat_check.py --base http://127.0.0.1:8350 --spend --only lamps-direct
    uv run tools/chat_check/chat_check.py --base ... --spend --feature buses,roads --workers 3
    uv run tools/chat_check/chat_check.py --base ... --spend --failed-from data/chat_check/<run>

Results go to data/chat_check/<run>/ (results.jsonl, one line per question, and results.md).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "chatbot" / "web" / "src"
HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Nicosia")
CENTRE = (35.1725, 33.3615)  # lat, lon (chatbot/settings.py NICOSIA_CENTER)

# USD per million tokens: input, output, cache read, cache write (5 minute). Web search is
# $10 per 1,000 searches. From the Claude API's price list, November 2026.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00),
}
WEB_SEARCH_USD = 0.01
DASHES = ("\u2014", "\u2013")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DATA_FREE = {"show_on_map", "show_chart", "show_visual", "map_fly_to", "map_callouts", "map_clear",
             "open_inspector", "open_scenario"}  # tools that bring no numbers of their own


# -- what the page knows ------------------------------------------------------------------------


def tool_labels() -> dict[str, str]:
    text = (WEB / "lib" / "chat.ts").read_text(encoding="utf-8")
    block = re.search(r"TOOL_LABELS\b[^=]*=\s*\{(.*?)\n\}", text, re.S)
    return dict(re.findall(r'\b([a-z][a-z0-9_]*)\s*:\s*"([^"]*)"', block.group(1))) if block else {}


def id_in_text() -> re.Pattern:
    """The page's own pattern for ids it turns into chips (lib/entities.ts ID_IN_TEXT)."""
    text = (WEB / "lib" / "entities.ts").read_text(encoding="utf-8")
    m = re.search(r"export const ID_IN_TEXT = /(.+)/g", text)
    return re.compile(m.group(1) if m else r"(?!)")


def page_actions() -> set[str]:
    chat = (WEB / "lib" / "chat.ts").read_text(encoding="utf-8")
    acts = set(re.findall(r'ev\.action\s*===\s*"([a-z_]+)"', chat))
    engine = (WEB / "lib" / "map" / "engine.ts").read_text(encoding="utf-8")
    body = re.search(r"export function applyMapUi\(.*?\n\}", engine, re.S)
    acts |= set(re.findall(r'case\s+"([a-z_0-9]+)"', body.group(0) if body else ""))
    for p in WEB.rglob("*.ts*"):
        acts |= set(re.findall(r'registerTurnCard\(\s*"([a-z_0-9]+)"', p.read_text(encoding="utf-8")))
    return acts


# -- questions ----------------------------------------------------------------------------------


def load_questions(path: Path) -> list[dict]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    qs = data.get("question") or []
    seen = set()
    for q in qs:
        assert q.get("id") and q.get("ask") and q.get("feature"), q
        assert q["id"] not in seen, f"two questions are called {q['id']}"
        seen.add(q["id"])
        if q.get("follows"):
            assert q["follows"] in seen, f"{q['id']} follows {q['follows']}, which must come before it"
    return qs


def wall(spec: str | None, now: datetime) -> str | None:
    """A local wall time for the clock: 'yesterday 21:00', 'today 08:00', '-3d 21:00', 'fri 21:00'
    (the latest Friday before today), or an ISO time as it is."""
    if not spec:
        return None
    spec = spec.strip().lower()
    if re.match(r"^\d{4}-\d\d-\d\dt\d\d:\d\d", spec):
        return spec[:16].replace("t", "T")
    day, _, hm = spec.rpartition(" ")
    h, m = (int(x) for x in hm.split(":"))
    base = now.date()
    if day in ("", "today"):
        pass
    elif day == "yesterday":
        base -= timedelta(days=1)
    elif re.fullmatch(r"-\d+d", day):
        base -= timedelta(days=int(day[1:-1]))
    elif day[:3] in WEEKDAYS:
        back = (base.weekday() - WEEKDAYS.index(day[:3])) % 7 or 7
        base -= timedelta(days=back)
    else:
        raise ValueError(f"can't read the time {spec!r}")
    return f"{base:%Y-%m-%d}T{h:02d}:{m:02d}"


def context_for(q: dict, now: datetime, layers: list[str]) -> dict:
    clock = q.get("clock") or {}
    if isinstance(clock, str):
        clock = {"at": clock}
    loc = q.get("location")
    lat, lon = q.get("map_center") or CENTRE
    ctx = {
        "map_view": {"center": [lon, lat], "zoom": q.get("zoom", 14.5)},
        "layers_on_map": layers[-8:],
        "city_3d": bool(q.get("city_3d")),
        "user_location": ({"lat": loc[0], "lon": loc[1], "accuracy_m": 15, "source": "gps"} if loc else None),
        "visual_errors": [],
        "clock": {"at": wall(clock.get("at"), now), "playing": False,
                  "range": [wall(clock["range"][0], now), wall(clock["range"][1], now)] if clock.get("range") else None,
                  "compare": wall(clock.get("compare"), now)},
        "inspecting": q.get("inspecting"),
        "subject": q.get("subject"),
        "viewing": None,
    }
    return ctx


# -- asking -------------------------------------------------------------------------------------


class Backend:
    def __init__(self, base: str, timeout_s: float):
        self.base = base.rstrip("/")
        self.timeout_s = timeout_s
        pw = os.environ.get("CHATBOT_PASSWORD")
        self.headers = {"Content-Type": "application/json"}
        if pw:
            self.headers["Authorization"] = "Basic " + base64.b64encode(f"check:{pw}".encode()).decode()

    def request(self, path: str, body: dict | None = None, timeout: float = 30):
        req = urllib.request.Request(self.base + path, headers=self.headers, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        return urllib.request.urlopen(req, timeout=timeout)

    def get_json(self, path: str):
        with self.request(path) as r:
            return json.loads(r.read())

    def ask(self, message: str, conversation: str | None, context: dict) -> list[dict]:
        """One turn; every event of its stream, in order."""
        events: list[dict] = []
        body = {"message": message, "conversation_id": conversation, "context": context}
        deadline = time.monotonic() + self.timeout_s
        with self.request("/api/chat", body, timeout=self.timeout_s) as r:
            data: list[str] = []
            for raw in r:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data: "):
                    data.append(line[6:])
                elif not line and data:
                    ev = json.loads("\n".join(data))
                    data = []
                    events.append(ev)
                    if ev.get("type") == "done":
                        break
                if time.monotonic() > deadline:
                    cid = next((e["id"] for e in events if e.get("type") == "conversation"), None)
                    run = next((e.get("run") for e in events if e.get("type") == "conversation"), None)
                    if cid and run:
                        self.request(f"/api/stop/{cid}/{run}", {}).close()
                    events.append({"type": "error", "message": f"chat check: no answer within {self.timeout_s:.0f} s"})
                    break
        return events


def cost_of(usage: dict) -> float:
    p = PRICES.get(usage.get("model") or "", PRICES["claude-sonnet-5-5"])
    return (usage["input"] * p[0] + usage["output"] * p[1] + usage["cache_read"] * p[2]
            + usage["cache_write"] * p[3]) / 1e6 + usage.get("web_searches", 0) * WEB_SEARCH_USD


def digest(events: list[dict], backend: Backend) -> dict:
    """What one turn did, from its stream."""
    out = {"conversation": None, "tools": [], "layers": [], "charts": [], "visuals": 0, "cards": [],
           "commands": [], "text": "", "next": [], "choices": [], "do": [], "notes": [], "errors": [],
           "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0,
                     "model": None, "web_searches": 0}, "ui_actions": []}
    open_tools: dict[str, dict] = {}
    for ev in events:
        t = ev.get("type")
        if t == "conversation":
            out["conversation"] = ev["id"]
        elif t == "text":
            out["text"] += ev["text"]
        elif t == "tool_start":
            step = {"name": ev["name"], "ok": None, "summary": None}
            open_tools[ev["id"]] = step
            out["tools"].append(step)
            if ev["name"] == "web_search":
                out["usage"]["web_searches"] += 1
        elif t == "tool_end":
            step = open_tools.get(ev["id"]) or {"name": ev.get("name")}
            step.update(ok=ev.get("ok"), summary=ev.get("summary") or None)
            if "input" in ev:
                step["input"] = ev["input"]
            if "result" in ev:
                step["result"] = ev["result"]
        elif t == "ui":
            action = ev.get("action")
            out["ui_actions"].append(action)
            if action == "show_layer":
                layer = ev.get("layer") or {}
                entry = {"ref": layer.get("ref"), "style": layer.get("style"), "title": layer.get("title"),
                         "count": layer.get("count"), "sources": layer.get("sources") or [],
                         "timeseries": layer.get("timeseries"), "features": None}
                try:
                    data = backend.get_json(layer["url"])
                    entry["features"] = len(data.get("features") or [])
                except (urllib.error.URLError, KeyError, ValueError) as e:
                    entry["features"] = f"failed: {e}"
                out["layers"].append(entry)
            elif action == "chart":
                spec = ev.get("spec") or {}
                rows = (spec.get("data") or {}).get("values")
                out["charts"].append({"title": ev.get("title"), "rows": len(rows) if isinstance(rows, list) else None,
                                      "sources": ev.get("sources") or [], "linked": bool(ev.get("link")),
                                      "time_field": ev.get("time_field")})
            elif action == "visual":
                out["visuals"] += 1
            elif action in ("clock", "inspect", "view", "scenario", "fly_to", "callouts", "city_3d", "clear"):
                out["commands"].append({k: v for k, v in ev.items() if k in ("action", "id", "at", "live", "compare", "range", "run")})
            else:
                out["cards"].append(action)
        elif t == "usage":
            u = out["usage"]
            u["calls"] += 1
            for k in ("input", "output", "cache_read", "cache_write"):
                u[k] += ev.get(k) or 0
            u["model"] = ev.get("model") or u["model"]
        elif t == "follow_ups":
            out["next"], out["choices"], out["do"] = ev.get("next") or [], ev.get("choices") or [], ev.get("do") or []
        elif t == "notice":
            out["notes"].append(ev.get("message"))
        elif t == "error":
            out["errors"].append(ev.get("message"))
    return out


# -- checking -----------------------------------------------------------------------------------

NUM = re.compile(r"(?<![\w.:/-])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w:/])")


def numbers_in(text: str) -> list[str]:
    """The numbers an answer states, without times (21:00), dates, ids and list markers."""
    clean = re.sub(r"\b\d{1,2}:\d\d\b|\b\d{4}-\d\d-\d\d\b|\b[A-Za-z]+[-_]?\d[\w.-]*", " ", text)
    clean = re.sub(r"^\s*\d+\.\s", " ", clean, flags=re.M)
    found = []
    for m in NUM.finditer(clean):
        whole = m.group(1).replace(",", "")
        n = whole + ("." + m.group(2) if m.group(2) else "")
        if float(n) <= 12 and not m.group(2):
            continue  # small counts ("two or three", "1 of 3") are too common to check
        found.append(n)
    return found


def numbers_from(results: list[str]) -> set[float]:
    nums: set[float] = set()
    for r in results:
        for m in re.finditer(r"-?\d+(?:\.\d+)?", r):
            try:
                nums.add(float(m.group(0)))
            except ValueError:
                pass
    return nums


def found(n: str, pool: set[float]) -> bool:
    v = float(n)
    decimals = len(n.split(".")[1]) if "." in n else 0
    tol = 0.5 * 10 ** -decimals + 1e-9
    if any(abs(v - p) <= tol for p in pool):
        return True
    # A rounded answer of a bigger value ("about 2,300" for 2,284.6), or a percentage of a share.
    return any(p and (abs(v - p) / abs(p) < 0.02 or abs(v - p * 100) <= tol) for p in pool)


def alternatives(spec: str) -> list[str]:
    return [s.strip() for s in spec.split("|") if s.strip()]


def check(q: dict, d: dict, labels: dict, ids: re.Pattern, actions: set[str], seen_results: list[str]) -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    called = [t["name"] for t in d["tools"]]
    text = d["text"]
    if d["errors"]:
        problems.append("errors: " + "; ".join(map(str, d["errors"])))
    if not text.strip():
        problems.append("no answer text")
    for spec in q.get("tools") or []:
        if not set(alternatives(spec)) & set(called):
            problems.append(f"did not call {spec}")
    for name in q.get("tools_not") or []:
        if name in called:
            problems.append(f"called {name}, which this question should not need")
    for spec in q.get("visual") or []:
        ok = False
        for alt in alternatives(spec):
            kind, _, style = alt.partition(":")
            if kind == "layer":
                ok |= any(not style or layer["style"] == style for layer in d["layers"])
            elif kind == "chart":
                ok |= bool(d["charts"])
            elif kind == "visual":
                ok |= d["visuals"] > 0
            elif kind == "card":
                ok |= bool(d["cards"]) if not style else style in d["cards"]
            elif kind in ("clock", "inspect", "view", "scenario", "fly_to", "city_3d", "callouts"):
                ok |= any(c["action"] == kind for c in d["commands"])
            elif kind == "none":
                ok |= not d["layers"] and not d["charts"] and not d["visuals"]
        if not ok:
            problems.append(f"no {spec} reached the page")
    if q.get("no_chart") and d["charts"]:
        warnings.append(f"drew {len(d['charts'])} chart(s) where a sentence does")
    for pat in q.get("say") or []:
        if not re.search(pat, text, re.I):
            problems.append(f"answer doesn't say /{pat}/")
    if q.get("say_any") and not any(re.search(p, text, re.I) for p in q["say_any"]):
        problems.append(f"answer says none of {q['say_any']}")
    for pat in q.get("not_say") or []:
        if m := re.search(pat, text, re.I):
            problems.append(f"answer says {m.group(0)!r} (/{pat}/)")
    if q.get("ids") and not ids.search(text):
        problems.append("answer names no id the page turns into a chip")

    # The rules every answer keeps.
    if any(dash in text for dash in DASHES):
        problems.append("an em or en dash in the answer")
    for t in d["tools"]:
        if t["name"] not in labels:
            problems.append(f"the trace has no label for {t['name']}")
        if t.get("ok") is None:
            warnings.append(f"{t['name']} never finished")
    for a in d["ui_actions"]:
        if a not in actions:
            problems.append(f"ui action {a!r} that the page ignores")
    for layer in d["layers"]:
        if not isinstance(layer["features"], int):
            problems.append(f"layer {layer['ref']} did not load: {layer['features']}")
        elif layer["features"] == 0:
            warnings.append(f"layer {layer['ref']} ({layer['title']}) is empty")
        if not layer["sources"]:
            problems.append(f"layer {layer['ref']} ({layer['title']}) names no sources")
    for chart in d["charts"]:
        if not chart["rows"]:
            problems.append(f"chart {chart['title']!r} arrived with no rows")
        if not chart["sources"]:
            problems.append(f"chart {chart['title']!r} names no sources")
    if not d["next"] and not d["choices"] and not q.get("no_next"):
        warnings.append("no follow-up questions")

    traced = [t for t in d["tools"] if "result" in t]
    for name, keys in (q.get("args") or {}).items():
        runs = [t for t in d["tools"] if t["name"] == name]
        if runs and all("input" in t for t in runs):
            for key in keys:
                if not any(key in (t["input"] or {}) for t in runs):
                    problems.append(f"{name} was not given {key}")
    if traced or seen_results:
        pool = numbers_from(seen_results + [t["result"] for t in traced]
                            + [t.get("summary") or "" for t in d["tools"]])
        missing = [n for n in numbers_in(text) if not found(n, pool)]
        if missing:
            warnings.append(f"numbers not found in any tool result: {', '.join(dict.fromkeys(missing))}")
        ids_seen = set()
        for t in traced:
            ids_seen |= set(re.findall(r'"id":\s*"([^"]{3,64})"', t["result"]))
        for i in sorted(ids_seen):
            if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", text) and not ids.fullmatch(i):
                warnings.append(f"id {i} in the answer is not one the page turns into a chip")
    elif numbers_in(text) and not [t for t in d["tools"] if t["name"] not in DATA_FREE and t.get("ok")]:
        problems.append("the answer states numbers but no data tool answered")
    return problems, warnings


# -- running ------------------------------------------------------------------------------------


def chains(qs: list[dict]) -> list[list[dict]]:
    """Questions grouped into conversations: a question that follows another goes after it."""
    by_id: dict[str, list[dict]] = {}
    out: list[list[dict]] = []
    for q in qs:
        if q.get("follows") and q["follows"] in by_id:
            chain = by_id[q["follows"]]
            chain.append(q)
        else:
            chain = [q]
            out.append(chain)
        by_id[q["id"]] = chain
    return out


def run_chain(chain: list[dict], backend: Backend, sink, now: datetime, statics) -> None:
    labels, ids, actions = statics
    conversation = None
    layers: list[str] = []
    seen: list[str] = []
    for q in chain:
        started = time.monotonic()
        try:
            events = backend.ask(q["ask"], conversation, context_for(q, now, layers))
        except (urllib.error.URLError, OSError) as e:
            events = [{"type": "error", "message": f"chat check: {e}"}]
        d = digest(events, backend)
        d["seconds"] = round(time.monotonic() - started, 1)
        conversation = d["conversation"] or conversation
        problems, warnings = check(q, d, labels, ids, actions, seen)
        seen += [t["result"] for t in d["tools"] if "result" in t]
        layers += [f"{layer['ref']} ({layer['title']})" for layer in d["layers"]]
        d["cost_usd"] = round(cost_of(d["usage"]), 4)
        for t in d["tools"]:  # keep the record readable: results only as their size
            if "result" in t:
                t["result_chars"] = len(t.pop("result"))
        sink({"id": q["id"], "feature": q["feature"], "ask": q["ask"], "follows": q.get("follows"),
              "verdict": "fail" if problems else "pass", "problems": problems, "warnings": warnings, **d})


def write_markdown(path: Path, results: list[dict]) -> None:
    lines = ["| Question | Feature | Verdict | Tools | Visuals | Seconds | Tokens in / out | Cost | Problems and warnings |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        u = r["usage"]
        visuals = [f"layer {layer['style']}" for layer in r["layers"]] + ["chart"] * len(r["charts"]) \
            + [f"card {c}" for c in r["cards"]] + [c["action"] for c in r["commands"]]
        notes = r["problems"] + [f"(warning) {w}" for w in r["warnings"]]
        cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        lines.append(f"| {cell(r['id'])}: {cell(r['ask'])} | {r['feature']} | {r['verdict']} | "
                     f"{cell(', '.join(t['name'] + ('' if t.get('ok') else ' (failed)') for t in r['tools']))} | "
                     f"{cell(', '.join(visuals))} | {r['seconds']} | "
                     f"{u['input'] + u['cache_read'] + u['cache_write']:,} / {u['output']:,} | "
                     f"${r['cost_usd']:.3f} | {cell('; '.join(notes))} |")
    total = sum(r["cost_usd"] for r in results)
    passed = sum(r["verdict"] == "pass" for r in results)
    lines += ["", f"{passed} of {len(results)} passed. Spent about ${total:.2f} "
                  f"({sum(r['usage']['input'] + r['usage']['cache_read'] + r['usage']['cache_write'] for r in results):,} "
                  f"input and {sum(r['usage']['output'] for r in results):,} output tokens)."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="the running backend, e.g. http://127.0.0.1:8350")
    ap.add_argument("--questions", type=Path, default=HERE / "questions.toml")
    ap.add_argument("--only", help="question ids, comma separated (a follow-up brings what it follows)")
    ap.add_argument("--feature", help="features, comma separated")
    ap.add_argument("--failed-from", type=Path, help="a run's folder or results.jsonl: ask its failures again")
    ap.add_argument("--out", type=Path, help="where to write (default data/chat_check/<time>)")
    ap.add_argument("--workers", type=int, default=3, help="conversations asked at once")
    ap.add_argument("--timeout", type=float, default=360, help="seconds per question")
    ap.add_argument("--spend", action="store_true", help="really ask: this spends Claude API credit")
    a = ap.parse_args(argv)

    qs = load_questions(a.questions)
    wanted = None
    if a.only:
        wanted = set(a.only.split(","))
    if a.failed_from:
        src = a.failed_from / "results.jsonl" if a.failed_from.is_dir() else a.failed_from
        wanted = (wanted or set()) | {json.loads(line)["id"] for line in src.read_text(encoding="utf-8").splitlines()
                                      if line.strip() and json.loads(line)["verdict"] == "fail"}
    if wanted is not None:
        by_id = {q["id"]: q for q in qs}
        need = set()
        for qid in wanted:
            assert qid in by_id, f"no question {qid}"
            while qid:  # a follow-up needs the questions it follows
                need.add(qid)
                qid = by_id[qid].get("follows")
        qs = [q for q in qs if q["id"] in need]
    if a.feature:
        features = set(a.feature.split(","))
        keep = {q["id"] for q in qs if q["feature"] in features}
        by_id = {q["id"]: q for q in qs}
        for qid in list(keep):
            while by_id[qid].get("follows"):
                qid = by_id[qid]["follows"]
                keep.add(qid)
        qs = [q for q in qs if q["id"] in keep]

    groups = chains(qs)
    if not a.spend:
        for g in groups:
            print(" -> ".join(f"{q['id']} [{q['feature']}]" for q in g))
        print(f"\n{len(qs)} questions in {len(groups)} conversations. Nothing was asked: add --spend "
              "to ask them (it spends Claude API credit, roughly $0.05 to $0.15 a question).")
        return 0

    backend = Backend(a.base, a.timeout)
    try:
        backend.get_json("/api/sources")
    except (urllib.error.URLError, OSError) as e:
        print(f"can't reach {a.base}: {e}", file=sys.stderr)
        return 2
    out = a.out or ROOT / "data" / "chat_check" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    statics = (tool_labels(), id_in_text(), page_actions())
    now = datetime.now(TZ)
    results: list[dict] = []
    lock = threading.Lock()
    order = {q["id"]: i for i, q in enumerate(qs)}

    def sink(r: dict) -> None:
        with lock:
            results.append(r)
            with (out / "results.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            mark = "ok  " if r["verdict"] == "pass" else "FAIL"
            print(f"{mark} {r['id']:<28} {r['seconds']:>6.1f}s ${r['cost_usd']:.3f}  "
                  f"{' > '.join(t['name'] for t in r['tools'])}", flush=True)
            for p in r["problems"]:
                print(f"       {p}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        for f in [pool.submit(run_chain, g, backend, sink, now, statics) for g in groups]:
            f.result()
    results.sort(key=lambda r: order[r["id"]])
    write_markdown(out / "results.md", results)
    passed = sum(r["verdict"] == "pass" for r in results)
    print(f"\n{passed} of {len(results)} passed, about ${sum(r['cost_usd'] for r in results):.2f}. "
          f"Results in {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

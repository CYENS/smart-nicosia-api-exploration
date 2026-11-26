"""Scenario tools: list the lab's scenarios, run one, compare two runs, and open a run in the
Scenario lab on the page. A run's numbers come with their ranges across the assumptions and a
backtest against what the sensors measured; its series go to the session store as a data_ref and
its map of what changes as a layer_ref."""

from __future__ import annotations

from chatbot.scenarios import registry
from chatbot.scenarios.base import NAMED_PERIODS, ScenarioError
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj

TOOLS = ToolSet()
tool = TOOLS.tool

PERIOD = {"type": "string", "enum": list(NAMED_PERIODS),
          "description": "The past period the scenario learns from and backtests on (default "
                         "last_30_days)."}


def _lab(ctx: ToolContext):
    if ctx.scenarios is None:
        raise ToolError("the scenario lab isn't available in this session")
    return ctx.scenarios


def _param_brief(p: dict) -> dict:
    out = {k: p[k] for k in ("title", "type", "default", "minimum", "maximum", "enum") if k in p}
    for k in ("x-unit", "x-assumption", "x-when", "x-group"):
        if k in p:
            out[k[2:]] = p[k]
    return out


def _kpi_brief(k: dict) -> dict:
    out = {x: k.get(x) for x in ("id", "label", "unit", "baseline", "scenario", "delta", "delta_pct",
                                  "better")}
    out["range"] = [k.get("low"), k.get("high")]
    out["delta_range"] = [k.get("delta_low"), k.get("delta_high")]
    if k.get("note"):
        out["note"] = k["note"]
    return out


@tool("list_scenarios",
      "The Scenario lab's scenarios (what-if models run on the twin's memory): each with its "
      "id, what it decides, its parameters (choices, with defaults and limits) and its "
      "assumptions (marked assumption: true, each with a plausible range). Call it before "
      "run_scenario when you don't know a scenario's parameters.",
      obj({}))
def h_list_scenarios(ctx: ToolContext, a: dict) -> dict:
    _lab(ctx)
    return {"scenarios": [{"id": s.id, "title": s.title, "summary": s.summary, "question": s.question,
                           "main_kpi": s.main_kpi,
                           "params": {k: _param_brief(p) for k, p in s.properties().items()}}
                          for s in registry.scenarios()],
            "periods": list(NAMED_PERIODS), "_summary": f"{len(registry.scenarios())} scenarios"}


@tool("run_scenario",
      "Run a scenario of the Scenario lab against the twin's memory and keep the run. Returns "
      "each KPI's baseline (today, as measured), scenario value and change, with ranges across "
      "the assumptions' plausible values; the assumptions; the backtest (today's policy replayed "
      "on the period against what the sensors measured, with its error); the sensitivity of "
      "the headline KPI to each assumption; a data_ref of the series (rows: metric, time, "
      "variant, value; data_refs has one per metric) and a layer_ref of what changes on the "
      "map. Unset parameters take their defaults. open_scenario shows the run in the lab.",
      obj({"scenario": {"type": "string", "description": "A scenario id from list_scenarios."},
           "params": {"type": "object", "description": "Parameters by name (see list_scenarios)."},
           "period": PERIOD}, ["scenario"]))
def h_run_scenario(ctx: ToolContext, a: dict) -> dict:
    lab = _lab(ctx)
    try:
        run_ = lab.run(a["scenario"], a.get("params") or {}, a.get("period"))
    except ScenarioError as e:
        raise ToolError(str(e)) from None
    res = run_["result"]
    sources = res.get("sources") or []
    out: dict = {"run_id": run_["id"], "link": f"#/s/{run_['id']}", "label": run_["label"],
                 "period": run_["period"]["label"], "saved": run_.get("saved", False),
                 "kpis": [_kpi_brief(k) for k in res["kpis"]],
                 "assumptions": [{x: v.get(x) for x in ("id", "label", "value", "unit", "low", "high",
                                                         "source")} for v in res["assumptions"]],
                 "sensitivity": [{x: s.get(x) for x in ("label", "at_low", "at_high", "unit")}
                                 for s in res.get("sensitivity", [])[:4]],
                 "backtest": res.get("backtest"),
                 "notes": res.get("notes", [])}
    if (d := res.get("details", {}).get("summary")) is not None:
        out["details"] = d
    rows = res.get("series") or []
    if rows:
        titles = {c["metric"]: c.get("title", c["metric"]) for c in res.get("charts", [])}
        out["data_refs"] = {}
        for metric in dict.fromkeys(r["metric"] for r in rows):
            out["data_refs"][metric] = ctx.store.add_dataset(
                f"scenario_{metric}", [r for r in rows if r["metric"] == metric],
                title=f"{run_['label']}: {titles.get(metric, metric)}", sources=sources)
        out["data_ref"] = next(iter(out["data_refs"].values()))
        out["charts"] = res.get("charts", [])
    layer = res.get("layers", {}).get("change")
    if layer and layer.get("features"):
        meta = layer.get("meta", {})
        out["layer_ref"] = ctx.store.add_layer(
            f"scenario_{run_['scenario']}", layer["features"],
            geometry=meta.get("geometry", "point"), title=meta.get("title", run_["label"]),
            legend=meta.get("legend"), value_fields=[meta["value_field"]] if meta.get("value_field") else None,
            sources=sources)
    out["_summary"] = f"{run_['label']}: run {run_['id']}"
    return out


@tool("compare_scenarios",
      "Compare two runs of the Scenario lab side by side: each KPI for both runs with its range, "
      "the difference, and which parameters differ. Returns a data_ref (rows: kpi, run, value, "
      "low, high) for a chart. Runs of different scenarios share only the KPIs they both have.",
      obj({"run_a": {"type": "string"}, "run_b": {"type": "string"}}, ["run_a", "run_b"]))
def h_compare_scenarios(ctx: ToolContext, a: dict) -> dict:
    lab = _lab(ctx)
    try:
        ra, rb = lab.get(a["run_a"]), lab.get(a["run_b"])
    except ScenarioError as e:
        raise ToolError(str(e)) from None
    kb = {k["id"]: k for k in rb["result"]["kpis"]}
    rows, table = [], []
    for k in ra["result"]["kpis"]:
        other = kb.get(k["id"])
        if other is None:
            continue
        diff = None if k["scenario"] is None or other["scenario"] is None else round(
            other["scenario"] - k["scenario"], k.get("digits", 1))
        table.append({"kpi": k["label"], "unit": k["unit"], "baseline": k["baseline"],
                      "a": k["scenario"], "a_range": [k["low"], k["high"]],
                      "b": other["scenario"], "b_range": [other["low"], other["high"]],
                      "b_minus_a": diff, "better": k["better"]})
        for r_, kk in ((ra, k), (rb, other)):
            rows.append({"kpi": k["label"], "unit": k["unit"], "run": r_["label"], "run_id": r_["id"],
                         "value": kk["scenario"], "low": kk["low"], "high": kk["high"]})
    params = sorted(set(ra["params"]) | set(rb["params"]))
    differ = {p: [ra["params"].get(p), rb["params"].get(p)] for p in params
              if ra["params"].get(p) != rb["params"].get(p)}
    return {"a": {"run_id": ra["id"], "label": ra["label"], "period": ra["period"]["label"]},
            "b": {"run_id": rb["id"], "label": rb["label"], "period": rb["period"]["label"]},
            "kpis": table, "params_that_differ": differ,
            "data_ref": ctx.store.add_dataset("scenario_compare", rows, title=f"{ra['label']} and {rb['label']}",
                                              sources=list(dict.fromkeys(ra["result"].get("sources", [])
                                                                         + rb["result"].get("sources", [])))),
            "_summary": f"{ra['id']} against {rb['id']}"}


@tool("open_scenario",
      "Open a run in the page's Scenario lab: its KPI cards with ranges, the split map of today "
      "against the scenario, the charts, the assumptions with their sensitivity, the backtest "
      "and the decision brief. With compare_with, the lab shows the two runs side by side.",
      obj({"run_id": {"type": "string"}, "compare_with": {"type": "string"}}, ["run_id"]))
def h_open_scenario(ctx: ToolContext, a: dict) -> dict:
    lab = _lab(ctx)
    try:
        lab.get(a["run_id"])
        if a.get("compare_with"):
            lab.get(a["compare_with"])
    except ScenarioError as e:
        raise ToolError(str(e)) from None
    ctx.emit({"type": "ui", "action": "scenario", "run": a["run_id"], "compare": a.get("compare_with")})
    return {"ok": True, "_summary": f"opened run {a['run_id']}"}

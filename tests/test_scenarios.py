"""The scenario lab's contract: periods, parameters, the runner's KPI ranges and sensitivity,
stored runs, the endpoints and the chat tools. Offline, with a toy scenario."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot.memory import Memory
from chatbot.routes import scenarios as scenario_routes
from chatbot.scenarios import Lab, Period, Result, Scenario, ScenarioContext, ScenarioError, kpi, run
from chatbot.scenarios.registry import _SCENARIOS, register
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


class Toy(Scenario):
    """kWh = lamps × hours × W / 1000; the scenario cuts the hours. Price and factor are
    assumptions, so cost and CO2 have ranges and energy has none."""
    id = "toy"
    title = "Toy"
    main_kpi = "cost"
    params = {"type": "object", "properties": {
        "cut_h": {"type": "number", "default": 1, "minimum": 0, "maximum": 4, "title": "Hours cut"},
        "price": {"type": "number", "default": 0.2, "minimum": 0.1, "maximum": 0.3,
                  "x-assumption": True, "title": "Price", "x-unit": "EUR/kWh", "x-source": "a guess"},
        "factor": {"type": "number", "default": 0.5, "minimum": 0.4, "maximum": 0.6,
                   "x-assumption": True, "title": "CO2", "x-unit": "kg/kWh"}}}
    prepared = 0

    def prepare(self, ctx, period):
        Toy.prepared += 1
        return {"lamps": 10, "hours": 12, "w": 100}

    def evaluate(self, data, p):
        base = data["lamps"] * data["hours"] * data["w"] / 1000
        scen = data["lamps"] * (data["hours"] - p["cut_h"]) * data["w"] / 1000
        return Result(kpis=[kpi("energy", "Energy", "kWh", base, scen),
                            kpi("cost", "Cost", "EUR", base * p["price"], scen * p["price"], digits=2),
                            kpi("co2", "CO2", "kg", base * p["factor"], scen * p["factor"])],
                      series=[{"metric": "kwh", "time": "18:00", "variant": "baseline", "value": base}])

    def backtest(self, ctx, data, params, period):
        return {"metric": "energy", "unit": "kWh", "measured": 10.0, "modelled": 11.0,
                "error_pct": 10.0, "period": period.label, "note": "toy"}


@pytest.fixture(autouse=True)
def toy():
    register(Toy())
    yield
    _SCENARIOS.pop("toy", None)


def ctx(memory=None):
    return ScenarioContext(Settings(), memory=memory, clock=lambda: NOW)


def test_periods_parse_names_and_dates():
    p = Period.parse(None, NOW)
    assert p.name == "last_30_days" and p.end == NOW and p.days == 30
    assert Period.parse("last_7_days", NOW).start == NOW - timedelta(days=7)
    q = Period.parse({"start": "2026-11-01T00:00:00+02:00", "end": "2026-11-08T00:00:00+02:00"}, NOW)
    assert q.days == 7 and q.name is None and "01 Nov 2026" in q.label
    for bad in ("last_year", {"start": "2026-11-08", "end": "2026-11-01"},
                {"start": "2026-01-01", "end": "2026-11-01"}, {"start": "2026-11-20", "end": "2026-12-20"}):
        with pytest.raises(ScenarioError):
            Period.parse(bad, NOW)


def test_params_take_defaults_and_are_checked():
    s = Toy()
    assert s.validated({"cut_h": 2}) == {"cut_h": 2, "price": 0.2, "factor": 0.5}
    with pytest.raises(ScenarioError, match="unknown parameters"):
        s.validated({"cut": 2})
    with pytest.raises(ScenarioError, match="invalid cut_h"):
        s.validated({"cut_h": 9})
    assert s.assumption_ids() == ["price", "factor"]


def test_the_runner_gives_ranges_across_the_assumptions_and_the_sensitivity():
    res, params = run(Toy(), ctx(), {"cut_h": 2}, Period.parse(None, NOW))
    energy, cost, co2 = res.kpis
    assert energy["baseline"] == 12.0 and energy["scenario"] == 10.0 and energy["delta"] == -2.0
    assert energy["low"] == energy["high"] == 10.0  # no assumption moves energy
    assert cost["delta"] == -0.4 and cost["delta_pct"] == -16.7
    assert (cost["delta_low"], cost["delta_high"]) == (-0.6, -0.2)  # price 0.3 and 0.1
    assert (cost["low"], cost["high"]) == (1.0, 3.0)
    assert (co2["low"], co2["high"]) == (4.0, 6.0)
    assert [a["id"] for a in res.assumptions] == ["price", "factor"]
    assert res.assumptions[0] == {"id": "price", "label": "Price", "value": 0.2, "unit": "EUR/kWh",
                                  "source": "a guess", "low": 0.1, "high": 0.3, "description": ""}
    # The headline is cost: price moves it, the CO2 factor doesn't.
    price, factor = res.sensitivity
    assert price["id"] == "price" and (price["at_low"], price["at_high"]) == (-0.2, -0.6)
    assert factor["at_low"] == factor["at_high"] == -0.4
    assert res.backtest["error_pct"] == 10.0


def test_runs_are_kept_in_the_memory_and_outlive_the_lab(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb", record=False)
    lab = Lab(ctx(mem))
    Toy.prepared = 0
    a = lab.run("toy", {"cut_h": 1})
    b = lab.run("toy", {"cut_h": 3}, "last_30_days")
    assert a["saved"] and b["saved"] and Toy.prepared == 1  # the period was read once
    fresh = Lab(ctx(mem))
    got = fresh.get(a["id"])
    assert got["params"]["cut_h"] == 1 and got["result"]["kpis"][0]["scenario"] == 11.0
    assert [r["id"] for r in fresh.runs.recent("toy")] == [b["id"], a["id"]]
    with pytest.raises(ScenarioError):
        fresh.get("nope")


def test_without_a_memory_runs_live_in_the_process():
    lab = Lab(ctx(None))
    r = lab.run("toy", {})
    assert r["saved"] is False and lab.get(r["id"]) is r
    assert lab.runs.recent()[0]["id"] == r["id"]


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.include_router(scenario_routes.router)
    app.state.agent = type("A", (), {"scenarios": Lab(ctx(Memory(tmp_path / "t.duckdb", record=False)))})()
    return TestClient(app)


def test_the_endpoints_list_run_fetch_and_export(client):
    listed = client.get("/api/scenarios").json()
    toy = next(s for s in listed["scenarios"] if s["id"] == "toy")
    assert toy["params"]["properties"]["price"]["x-assumption"] is True
    assert "last_30_days" in [p["name"] for p in listed["periods"]]
    r = client.post("/api/scenarios/toy/run", json={"params": {"cut_h": 2}, "period": "last_7_days"})
    assert r.status_code == 200
    run_ = r.json()
    assert run_["period"]["name"] == "last_7_days" and run_["result"]["kpis"][1]["delta_low"] == -0.6
    assert client.get(f"/api/scenarios/runs/{run_['id']}").json()["id"] == run_["id"]
    assert client.get("/api/scenarios/runs?scenario=toy").json()["runs"][0]["id"] == run_["id"]
    csv = client.get(f"/api/scenarios/runs/{run_['id']}/series.csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[1] == f"{run_['id']},toy,kwh,18:00,baseline,12.0"
    assert client.post("/api/scenarios/toy/run", json={"params": {"cut_h": 99}}).status_code == 400
    assert client.post("/api/scenarios/nope/run", json={}).status_code == 400
    assert client.get("/api/scenarios/runs/nope").status_code == 404


def test_the_chat_tools_run_compare_and_open():
    events = []
    tctx = ToolContext(Settings(), None, None, None, SessionStore(), events.append, "c1",
                       scenarios=Lab(ctx(None)))
    listed, err = run_tool(tctx, "list_scenarios", {})
    assert not err and any(s["id"] == "toy" for s in listed["scenarios"])
    a, err = run_tool(tctx, "run_scenario", {"scenario": "toy", "params": {"cut_h": 1}})
    assert not err and a["kpis"][1]["delta_range"] == [-0.3, -0.1] and a["backtest"]["error_pct"] == 10
    assert a["data_ref"] in tctx.store.datasets and a["link"] == f"#/s/{a['run_id']}"
    b, _ = run_tool(tctx, "run_scenario", {"scenario": "toy", "params": {"cut_h": 3}})
    cmp, err = run_tool(tctx, "compare_scenarios", {"run_a": a["run_id"], "run_b": b["run_id"]})
    assert not err and cmp["params_that_differ"] == {"cut_h": [1, 3]}
    assert cmp["kpis"][0]["b_minus_a"] == -2.0
    bad, err = run_tool(tctx, "run_scenario", {"scenario": "toy", "params": {"cut_h": -1}})
    assert err and "invalid cut_h" in bad["error"]
    ok, err = run_tool(tctx, "open_scenario", {"run_id": a["run_id"], "compare_with": b["run_id"]})
    assert not err and events[-1] == {"type": "ui", "action": "scenario", "run": a["run_id"],
                                      "compare": b["run_id"]}
    missing, err = run_tool(tctx, "open_scenario", {"run_id": "nope"})
    assert err

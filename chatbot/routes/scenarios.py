"""The scenario lab's API: the scenarios with their parameter schemas, runs, saved runs, and a
run's exports (the decision brief and the CSV of its series)."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from chatbot.routes import agent_of
from chatbot.scenarios import brief
from chatbot.scenarios.base import NAMED_PERIODS, PERIOD_LABELS, ScenarioError

router = APIRouter()


def _lab(request: Request):
    lab = getattr(agent_of(request), "scenarios", None)
    if lab is None:
        raise HTTPException(503, "the scenario lab isn't available")
    return lab


@router.get("/api/scenarios")
def list_scenarios(request: Request) -> dict:
    lab = _lab(request)
    return {"scenarios": [s.describe() for s in lab.scenarios()],
            "periods": [{"name": n, "label": PERIOD_LABELS[n], "days": d} for n, d in NAMED_PERIODS.items()]}


@router.post("/api/scenarios/{scenario_id}/run")
def run_scenario(request: Request, scenario_id: str, body: dict = Body(default_factory=dict)) -> dict:
    lab = _lab(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "send a JSON object: {params, period}")
    try:
        return lab.run(scenario_id, body.get("params"), body.get("period"))
    except ScenarioError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/scenarios/runs")
def saved_runs(request: Request, scenario: str | None = None, limit: int = 20) -> dict:
    return {"runs": _lab(request).runs.recent(scenario, max(1, min(limit, 100)))}


def _run(request: Request, run_id: str) -> dict:
    try:
        return _lab(request).get(run_id)
    except ScenarioError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/api/scenarios/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict:
    return _run(request, run_id)


@router.get("/api/scenarios/runs/{run_id}/series.csv")
def run_csv(request: Request, run_id: str) -> PlainTextResponse:
    found = _run(request, run_id)
    return PlainTextResponse(brief.series_csv(found), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="scenario-{run_id}.csv"'})


@router.get("/api/scenarios/runs/{run_id}/brief")
def run_brief(request: Request, run_id: str, compare: str | None = None) -> HTMLResponse:
    found = _run(request, run_id)
    other = _run(request, compare) if compare else None
    return HTMLResponse(brief.brief_html(found, other))

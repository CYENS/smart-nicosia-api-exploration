"""The lab: runs a scenario and keeps the run, for the page (chatbot/routes/scenarios.py) and the
chat tools (chatbot/tools/scenarios.py). One Lab lives on the Agent.

Runs are kept in the twin's memory, table `scenario_runs` (id, scenario, label, params, period,
summary, result, created; JSON as text), so a link to a run (`#/s/<id>`) works after a restart.
When the memory can't be written (missing, busy), the run is still kept in this process and says
`saved: false`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from chatbot.scenarios import registry
from chatbot.scenarios.base import Period, Scenario, ScenarioContext, ScenarioError, run

log = logging.getLogger(__name__)

TABLE = """CREATE TABLE IF NOT EXISTS scenario_runs (
    id VARCHAR PRIMARY KEY,
    scenario VARCHAR NOT NULL,
    label VARCHAR,
    params VARCHAR,     -- JSON
    period VARCHAR,     -- JSON
    summary VARCHAR,    -- JSON: the KPIs without layers or series, for lists
    result VARCHAR,     -- JSON: the whole Result
    created TIMESTAMPTZ
)"""
KEEP_IN_PROCESS = 200
PREPARED_TTL_S = 600.0  # a period's data is read again after 10 minutes


def summary_of(run_: dict) -> dict:
    """What a list of runs shows: the label, the period and the KPIs."""
    res = run_.get("result") or {}
    return {"id": run_["id"], "scenario": run_["scenario"], "label": run_.get("label"),
            "period": run_.get("period"), "created": run_.get("created"),
            "kpis": [{k: x.get(k) for k in ("id", "label", "unit", "baseline", "scenario", "delta",
                                              "delta_pct", "better", "low", "high", "delta_low",
                                              "delta_high", "digits")}
                     for x in res.get("kpis", [])],
            "saved": run_.get("saved", False)}


class RunStore:
    def __init__(self, memory=None):
        self.memory = memory
        self._runs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def _keep(self, run_: dict) -> None:
        with self._lock:
            self._runs[run_["id"]] = run_
            self._runs.move_to_end(run_["id"])
            while len(self._runs) > KEEP_IN_PROCESS:
                self._runs.popitem(last=False)

    def save(self, run_: dict) -> bool:
        """Keep a run here and, when it can, in the twin's memory. Returns whether it was saved."""
        saved = False
        if self.memory is not None:
            try:
                with self.memory.writing(timeout=5.0) as con:
                    con.execute(TABLE)
                    con.execute(
                        "INSERT OR REPLACE INTO scenario_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [run_["id"], run_["scenario"], run_.get("label"), json.dumps(run_["params"]),
                         json.dumps(run_["period"]), json.dumps(summary_of(run_)),
                         json.dumps(run_["result"], separators=(",", ":")),
                         datetime.fromisoformat(run_["created"])])
                saved = True
            except Exception as e:  # noqa: BLE001 - the run still works in this process
                log.warning("scenario run %s not saved to the memory: %s", run_["id"], e)
        run_["saved"] = saved
        self._keep(run_)
        return saved

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            if run_id in self._runs:
                return self._runs[run_id]
        if self.memory is None:
            return None
        try:
            with self.memory.reading() as con:
                row = con.execute(
                    """SELECT id, scenario, label, params, period, result, created
                       FROM scenario_runs WHERE id = ?""", [run_id]).fetchone()
        except duckdb.CatalogException:
            return None
        except Exception as e:  # noqa: BLE001 - Unavailable, or a broken file
            log.warning("scenario run %s not read: %s", run_id, e)
            return None
        if row is None:
            return None
        found = {"id": row[0], "scenario": row[1], "label": row[2], "params": json.loads(row[3]),
                 "period": json.loads(row[4]), "result": json.loads(row[5]),
                 "created": row[6].astimezone(timezone.utc).isoformat(), "saved": True}
        self._keep(found)
        return found

    def recent(self, scenario: str | None = None, limit: int = 20) -> list[dict]:
        """The newest runs first, as summaries."""
        out: dict[str, dict] = {}
        if self.memory is not None:
            try:
                with self.memory.reading() as con:
                    rows = con.execute(
                        """SELECT summary FROM scenario_runs
                           WHERE ? IS NULL OR scenario = ? ORDER BY created DESC LIMIT ?""",
                        [scenario, scenario, int(limit)]).fetchall()
                for (s,) in rows:
                    item = json.loads(s)
                    item["saved"] = True
                    out[item["id"]] = item
            except duckdb.CatalogException:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("scenario runs not listed from the memory: %s", e)
        with self._lock:
            here = [summary_of(r) for r in self._runs.values() if scenario in (None, r["scenario"])]
        for item in here:
            out.setdefault(item["id"], item)
        return sorted(out.values(), key=lambda r: r.get("created") or "", reverse=True)[:limit]


class Lab:
    def __init__(self, ctx: ScenarioContext, store: RunStore | None = None):
        self.ctx = ctx
        self.runs = store or RunStore(ctx.memory)
        self._prepared: dict[tuple, tuple[float, Any]] = {}
        self._prepare_lock = threading.Lock()

    def scenarios(self) -> list[Scenario]:
        return registry.scenarios()

    def _data(self, scenario: Scenario, period: Period) -> Any:
        key = (scenario.id, period.name or (period.start.isoformat(), period.end.isoformat()))
        with self._prepare_lock:  # one read of a period at a time; the next run reuses it
            hit = self._prepared.get(key)
            if hit and time.monotonic() - hit[0] < PREPARED_TTL_S:
                return hit[1]
            data = scenario.prepare(self.ctx, period)
            self._prepared[key] = (time.monotonic(), data)
            for k in [k for k, (t, _) in self._prepared.items() if time.monotonic() - t > PREPARED_TTL_S]:
                del self._prepared[k]
            return data

    def run(self, scenario_id: str, params: dict | None = None, period: dict | str | None = None) -> dict:
        scenario = registry.get(scenario_id)
        per = Period.parse(period, self.ctx.now())
        params = scenario.validated(params)
        result, params = run(scenario, self.ctx, params, per, data=self._data(scenario, per))
        created = datetime.now(timezone.utc)
        run_ = {"id": uuid.uuid4().hex[:10], "scenario": scenario.id, "scenario_title": scenario.title,
                "label": scenario.label(params), "params": params, "period": per.to_dict(),
                "result": result.to_dict(), "created": created.isoformat()}
        self.runs.save(run_)
        return run_

    def get(self, run_id: str) -> dict:
        found = self.runs.get(run_id)
        if found is None:
            raise ScenarioError(f"no scenario run {run_id!r}; runs are listed at /api/scenarios/runs")
        if "scenario_title" not in found:
            try:
                found["scenario_title"] = registry.get(found["scenario"]).title
            except ScenarioError:
                found["scenario_title"] = found["scenario"]
        return found


def default_cache_dir() -> Path:
    from chatbot.settings import CACHE_DIR

    return CACHE_DIR / "scenarios"

"""The scenario lab: try a change on the twin before making it on the street.

- `base.py` is the contract: `Scenario` (prepare, evaluate, backtest), `Period`,
  `ScenarioContext`, `Result`, `kpi()` and the runner that gives every KPI its range across the
  assumptions and draws the sensitivity of the headline KPI.
- `registry.py` keeps the scenarios; each scenario module registers itself when imported here.
- `lab.py` runs a scenario and keeps the run (`scenario_runs` in the twin's memory).
- `brief.py` turns a run (or two) into a printable decision brief and a CSV.

A new scenario: a module here with a `Scenario` subclass that calls `register(...)`, imported
below; its sources in chatbot/sources.py; on the page, optional sections in
`chatbot/web/src/lib/registries/scenarios.ts`.
"""

from chatbot.scenarios import lighting  # noqa: F401  (registers itself)
from chatbot.scenarios.base import Period, Result, Scenario, ScenarioContext, ScenarioError, kpi, run
from chatbot.scenarios.lab import Lab, RunStore
from chatbot.scenarios.registry import get, register, scenarios

__all__ = ["Lab", "Period", "Result", "RunStore", "Scenario", "ScenarioContext", "ScenarioError",
           "get", "kpi", "register", "run", "scenarios"]

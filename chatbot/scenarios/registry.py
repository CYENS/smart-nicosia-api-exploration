"""The scenarios the lab offers, in the order they were registered. A scenario module calls
`register(MyScenario())` at import; chatbot/scenarios/__init__.py imports each module, so the
order is the order of those imports (lighting first, then waste collection)."""

from __future__ import annotations

from chatbot.scenarios.base import Scenario, ScenarioError

_SCENARIOS: dict[str, Scenario] = {}


def register(scenario: Scenario) -> Scenario:
    if not scenario.id or not scenario.title:
        raise ValueError("a scenario needs an id and a title")
    if scenario.id in _SCENARIOS and _SCENARIOS[scenario.id] is not scenario:
        raise ValueError(f"two scenarios share the id {scenario.id!r}")
    _SCENARIOS[scenario.id] = scenario
    return scenario


def scenarios() -> list[Scenario]:
    return list(_SCENARIOS.values())


def get(scenario_id: str) -> Scenario:
    found = _SCENARIOS.get(scenario_id)
    if found is None:
        raise ScenarioError(f"no scenario {scenario_id!r}; the lab has {sorted(_SCENARIOS)}")
    return found

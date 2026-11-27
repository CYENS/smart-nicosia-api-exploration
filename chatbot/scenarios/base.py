"""The scenario lab's contract: what a scenario is, the period it learns from, what it may read,
and what a run gives back.

A scenario is a module in this package that subclasses `Scenario` and registers an instance
(`registry.register`); `chatbot/scenarios/__init__.py` imports it. It has two steps:

- `prepare(ctx, period)` reads what it needs (the twin's memory, the city API, cached feeds)
  once per run. It may be slow; it must not depend on the parameters.
- `evaluate(data, params)` turns that into a `Result` for one set of parameters. It must be
  quick and pure: the runner calls it again for every combination of the assumptions' plausible
  values, to give each KPI its range and to draw the tornado chart.

`backtest(ctx, data, params, period)` replays today's policy over the period and compares it
with what the sensors measured.

Parameters are a JSON schema (`params`) the page turns into a form and the chat tools pass on.
Each property carries `title`, `description`, `default`, and for numbers `minimum` and
`maximum`; `x-unit` names its unit, `x-step` the slider's step. A property with
`"x-assumption": true` is an assumption rather than a choice: its `minimum` and `maximum` are its
plausible range (not just the slider's limits), `x-source` says where the default comes from,
and the runner moves it across that range for the KPI ranges and the sensitivity chart. A
property with `x-group` is drawn under that heading (a policy's switch and its settings share
one); `x-when` names a boolean property that must be on for it to matter.

Everything a run returns is plain JSON (`Result.to_dict`), stored in the `scenario_runs` table.
"""

from __future__ import annotations

import copy
import itertools
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

if TYPE_CHECKING:
    from chatbot.city import CityData
    from chatbot.eoa import Registry
    from chatbot.geo import Places
    from chatbot.memory import Memory
    from chatbot.mobility import Mobility
    from chatbot.routing import Router
    from chatbot.settings import Settings


class ScenarioError(ValueError):
    """A problem the person (or the model) can fix: an unknown scenario, a bad parameter, a
    period with no data. The message says what to change."""


# -- the period a scenario learns from ---------------------------------------------------------

NAMED_PERIODS: dict[str, int] = {"last_7_days": 7, "last_14_days": 14, "last_30_days": 30,
                                 "last_90_days": 90}
PERIOD_LABELS: dict[str, str] = {"last_7_days": "The last 7 days", "last_14_days": "The last 14 days",
                                 "last_30_days": "The last 30 days", "last_90_days": "The last 90 days"}
MAX_PERIOD = timedelta(days=92)


def _aware(value: str) -> datetime:
    """An ISO time; without a zone it is Nicosia's own clock."""
    from zoneinfo import ZoneInfo

    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo("Asia/Nicosia"))


@dataclass(frozen=True)
class Period:
    start: datetime  # aware
    end: datetime
    name: str | None = None  # one of NAMED_PERIODS, or None for explicit dates

    @classmethod
    def parse(cls, value: dict | str | None, now: datetime, default: str = "last_30_days") -> Period:
        """{"name": "last_30_days"}, "last_30_days", or {"start": ISO, "end": ISO}."""
        if value is None or value == {}:
            value = default
        if isinstance(value, str):
            value = {"name": value}
        if not isinstance(value, dict):
            raise ScenarioError("period must be a name like last_30_days or {start, end}")
        name = value.get("name")
        if name:
            if name not in NAMED_PERIODS:
                raise ScenarioError(f"unknown period {name!r}; use one of {sorted(NAMED_PERIODS)} "
                                    "or {start, end}")
            end = now.astimezone(timezone.utc)
            return cls(end - timedelta(days=NAMED_PERIODS[name]), end, name)
        try:
            start, end = _aware(value["start"]), _aware(value["end"])
        except (KeyError, TypeError, ValueError):
            raise ScenarioError("a period needs a name, or ISO start and end times") from None
        if end <= start:
            raise ScenarioError("the period's end must come after its start")
        if end - start > MAX_PERIOD:
            raise ScenarioError("a period can be at most 92 days long")
        if end > now + timedelta(minutes=5):
            raise ScenarioError("the period can't reach into the future")
        return cls(start.astimezone(timezone.utc), end.astimezone(timezone.utc), None)

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400

    @property
    def label(self) -> str:
        if self.name:
            return PERIOD_LABELS[self.name]
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Nicosia")  # the city's own days
        return f"{self.start.astimezone(tz):%d %b %Y} to {self.end.astimezone(tz):%d %b %Y}"

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "days": round(self.days, 2), "label": self.label}


# -- what a scenario may read ------------------------------------------------------------------


@dataclass
class ScenarioContext:
    settings: Settings
    memory: Memory | None = None
    city: CityData | None = None
    router: Router | None = None
    places: Places | None = None
    registers: Registry | None = None  # the dangerous-building registers
    mobility: Mobility | None = None  # bus stops and lines
    clock: Any = None  # a callable returning now (aware); defaults to the city's clock
    # Where a scenario keeps what it fetched (one file per feed and day); None: don't keep it.
    cache_dir: Path | None = None

    def now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        if self.city is not None:
            return self.city.now()
        return datetime.now(timezone.utc)


# -- what a run gives back ---------------------------------------------------------------------


def kpi(id: str, label: str, unit: str, baseline: float | None, scenario: float | None, *,
        better: str = "lower", digits: int = 1, note: str | None = None) -> dict:
    """One KPI. `low`/`high` (the scenario value) and `delta_low`/`delta_high` (the change) are
    the range across the assumptions' plausible values; the runner fills them."""
    if better not in ("lower", "higher"):
        raise ValueError("better is 'lower' or 'higher'")

    def r(v):
        return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(v, digits)

    delta = None if baseline is None or scenario is None else scenario - baseline
    pct = None if delta is None or not baseline else 100 * delta / abs(baseline)
    return {"id": id, "label": label, "unit": unit, "baseline": r(baseline), "scenario": r(scenario),
            "delta": r(delta), "delta_pct": None if pct is None else round(pct, 1), "better": better,
            "low": r(scenario), "high": r(scenario), "delta_low": r(delta), "delta_high": r(delta),
            "digits": digits, "note": note}


@dataclass
class Result:
    """What one run gives back.

    - `kpis`: [{id, label, unit, baseline, scenario, delta, delta_pct, better, low, high,
      delta_low, delta_high, digits, note}] (see `kpi`). The first is the headline unless
      `Scenario.main_kpi` says otherwise.
    - `layers`: GeoJSON FeatureCollections keyed by name. Every feature has properties `id` (a
      device or building id, so charts and the inspector link to it), `state` and `color`;
      `meta` holds {title, legend: {state: colour}, variant: "baseline" | "scenario" | None,
      value_field, unit}. A "baseline" and a "scenario" layer are shown either side of the split
      map; layers without a variant are drawn over both.
    - `series`: chart rows [{metric, time, variant, value}], with `charts` describing each
      metric: [{metric, title, unit, x: "time" | "date" | "month" | "category", kind}].
    - `assumptions`: [{id, label, value, unit, source, low, high}] (the runner fills them from
      the parameters marked x-assumption).
    - `sensitivity`: [{id, label, at_low, at_high, value_low, value_high}]: the main KPI's change
      with that one assumption at its low and at its high, the others as set (the runner).
    - `backtest`: {metric, unit, measured, modelled, error_pct, period, note, rows?}.
    - `details`: what a scenario's own sections on the page read (lists, tables).
    - `sources`: ids in chatbot/sources.py; `notes`: plain sentences, limits first.
    """
    kpis: list[dict]
    layers: dict[str, dict] = field(default_factory=dict)
    series: list[dict] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    sensitivity: list[dict] = field(default_factory=list)
    backtest: dict | None = None
    details: dict = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# -- a scenario --------------------------------------------------------------------------------


class Scenario:
    id: str = ""
    title: str = ""
    summary: str = ""  # one sentence for the list
    question: str = ""  # the decision the brief opens with
    params: dict = {}  # a JSON schema, see the module docstring
    main_kpi: str = ""  # the KPI the tornado chart and the headline use (default: the first)
    sources: tuple[str, ...] = ()
    method: str = ""  # a paragraph for the brief: how the model works

    # -- to implement ------------------------------------------------------------------------

    def prepare(self, ctx: ScenarioContext, period: Period) -> Any:
        raise NotImplementedError

    def evaluate(self, data: Any, params: dict) -> Result:
        raise NotImplementedError

    def backtest(self, ctx: ScenarioContext, data: Any, params: dict, period: Period) -> dict | None:
        return None

    def label(self, params: dict) -> str:
        """A short name for a run with these parameters ("Dim to 50% 01:00 to 05:00")."""
        return self.title

    # -- shared ------------------------------------------------------------------------------

    def properties(self) -> dict[str, dict]:
        return self.params.get("properties", {})

    def defaults(self) -> dict:
        return {k: copy.deepcopy(p["default"]) for k, p in self.properties().items() if "default" in p}

    def assumption_ids(self) -> list[str]:
        return [k for k, p in self.properties().items() if p.get("x-assumption")]

    def validated(self, params: dict | None) -> dict:
        """The defaults with `params` laid over them, checked against the schema."""
        if params is not None and not isinstance(params, dict):
            raise ScenarioError("params must be an object")
        unknown = sorted(set(params or {}) - set(self.properties()))
        if unknown:
            raise ScenarioError(f"unknown parameters {unknown}; this scenario takes "
                                f"{sorted(self.properties())}")
        out = {**self.defaults(), **(params or {})}
        try:
            jsonschema.validate(out, {**self.params, "additionalProperties": False})
        except jsonschema.ValidationError as e:
            where = "/".join(map(str, e.path)) or "params"
            raise ScenarioError(f"invalid {where}: {e.message[:200]}") from None
        return out

    def describe(self) -> dict:
        return {"id": self.id, "title": self.title, "summary": self.summary,
                "question": self.question, "params": self.params,
                "main_kpi": self.main_kpi, "sources": list(self.sources), "method": self.method}


# -- the runner --------------------------------------------------------------------------------

MAX_CORNER_ASSUMPTIONS = 6  # 2^6 evaluations at most; beyond that, one at a time


def _assumptions(scenario: Scenario, params: dict) -> list[dict]:
    out = []
    for k in scenario.assumption_ids():
        p = scenario.properties()[k]
        out.append({"id": k, "label": p.get("title", k), "value": params[k], "unit": p.get("x-unit", ""),
                    "source": p.get("x-source", ""), "low": p.get("minimum"), "high": p.get("maximum"),
                    "description": p.get("description", "")})
    return out


def _main(scenario: Scenario, result: Result) -> dict | None:
    for k in result.kpis:
        if k["id"] == scenario.main_kpi:
            return k
    return result.kpis[0] if result.kpis else None


def run(scenario: Scenario, ctx: ScenarioContext, params: dict | None, period: Period,
        *, data: Any = None) -> tuple[Result, dict]:
    """Run a scenario: prepare, evaluate, then move each assumption across its plausible range
    for the KPI ranges and the sensitivity chart, then backtest. Returns (result, params)."""
    params = scenario.validated(params)
    if data is None:
        data = scenario.prepare(ctx, period)
    result = scenario.evaluate(data, params)
    result.assumptions = _assumptions(scenario, params)
    ids = [a["id"] for a in result.assumptions if a["low"] is not None and a["high"] is not None]
    props = scenario.properties()

    # KPI ranges: every corner of the assumptions' box (both ends of each), or, with many
    # assumptions, each one alone at its ends.
    if len(ids) <= MAX_CORNER_ASSUMPTIONS:
        combos = itertools.product(*[(props[i]["minimum"], props[i]["maximum"]) for i in ids])
        variants = [dict(zip(ids, c)) for c in combos]
    else:
        variants = [{i: props[i][end]} for i in ids for end in ("minimum", "maximum")]
    seen: dict[str, list[tuple[float, float]]] = {}
    for v in variants:
        other = scenario.evaluate(data, {**params, **v})
        for k in other.kpis:
            if k["scenario"] is not None:
                seen.setdefault(k["id"], []).append((k["scenario"], k["delta"]))
    for k in result.kpis:
        vals = seen.get(k["id"], [])
        if k["scenario"] is None or not vals:
            continue
        scen = [s for s, _ in vals] + [k["scenario"]]
        k["low"], k["high"] = round(min(scen), k["digits"]), round(max(scen), k["digits"])
        deltas = [d for _, d in vals if d is not None] + ([k["delta"]] if k["delta"] is not None else [])
        if deltas:
            k["delta_low"], k["delta_high"] = round(min(deltas), k["digits"]), round(max(deltas), k["digits"])

    # Sensitivity: the main KPI's change with one assumption at each end, the rest as set.
    main = _main(scenario, result)
    if main is not None:
        for a in result.assumptions:
            if a["id"] not in ids:
                continue
            ends = []
            for end in ("low", "high"):
                other = scenario.evaluate(data, {**params, a["id"]: a[end]})
                m = next((k for k in other.kpis if k["id"] == main["id"]), None)
                ends.append(m["delta"] if m and m["delta"] is not None else m["scenario"] if m else None)
            result.sensitivity.append({"id": a["id"], "label": a["label"], "kpi": main["id"],
                                       "unit": main["unit"], "value_low": a["low"],
                                       "value_high": a["high"], "at_low": ends[0], "at_high": ends[1],
                                       "base": main["delta"] if main["delta"] is not None else main["scenario"]})
        result.sensitivity.sort(key=lambda s: -abs((s["at_high"] or 0) - (s["at_low"] or 0)))
    result.backtest = scenario.backtest(ctx, data, params, period)
    if not result.sources:
        result.sources = list(scenario.sources)
    return result, params

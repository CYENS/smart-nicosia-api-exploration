"""Waste collection: which bins to empty when, replayed on what the city's bin sensors measured.

Compares a collection policy (every bin on fixed round days, only the bins at or above a level,
or the bins forecast to pass a level before the next round) with the city's own pickups over the
same period: hours the bins sat full or at capacity, collections, lifts of nearly empty bins,
truck kilometres and driver hours (every round routed on the road network), diesel and CO2, and
the bins that would need a bigger container. The model and its backtest are in
waste_model.py, waste_study.py and rounds.py.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from chatbot.memory import Unavailable
from chatbot.scenarios.base import Period, Result, Scenario, ScenarioContext, ScenarioError, kpi
from chatbot.scenarios.registry import register
from chatbot.scenarios.rounds import Crew, Matrix, MatrixSource
from chatbot.scenarios.waste_model import (
    DAY_MS,
    HOUR_MS,
    ROUND_DAYS,
    Bin,
    DayPlan,
    Policy,
    local,
    rate_at,
)
from chatbot.scenarios.waste_study import (
    STATUS_LABELS,
    Outcome,
    Study,
    backtest,
    baseline,
    bin_data,
    midnight_after,
    midnight_before,
    policy_outcome,
    sensor_hours,
)
from chatbot.settings import CACHE_DIR, TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)
KEYS = ("level", "pickup_time", "level_before_pickup", "level_after_pickup")
MIN_DAYS = 7
WEEKS_A_YEAR = 365.25 / 7
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# No depot location is published (checked Nov 2026: the municipality's and Green Dot Cyprus's
# pages name none). The default stands in for one: an industrial area at the city's south-west
# edge, about 4 km from the middle of the bins, where yards of this kind sit.
DEPOTS = {
    "strovolos_industrial": ("Strovolos industrial area (a stand-in: the real depot isn't "
                             "published)", (35.1385, 33.3365)),
    "bins_centre": ("The bin nearest the middle of all the bins (the least driving any depot "
                    "allows)", None),
}

POLICY_TITLES = {
    "threshold": "Only bins at or above a level",
    "forecast": "Bins forecast to pass a level before the next round",
    "fixed": "Every bin on every round day",
}

PARAMS = {
    "type": "object",
    "properties": {
        "policy": {
            "type": "string", "enum": list(POLICY_TITLES), "default": "threshold",
            "x-enum-titles": POLICY_TITLES, "title": "Which bins a round collects",
            "description": "Every bin on each round day; only the bins at or above a level when "
                           "the round leaves; or the bins the model forecasts will pass a level "
                           "before the next round.", "x-group": "policy"},
        "rounds_per_week": {
            "type": "integer", "default": 5, "minimum": 1, "maximum": 7, "x-step": 1,
            "x-unit": "a week", "title": "Round days",
            "description": "Days the trucks go out, spread over the week: 1 is Monday; 2 Monday "
                           "and Thursday; 3 Monday, Wednesday and Friday; 4 Monday, Tuesday, "
                           "Thursday and Friday; 5 Monday to Friday; 6 Monday to Saturday; 7 "
                           "every day.", "x-group": "policy"},
        "threshold": {
            "type": "integer", "default": 70, "minimum": 30, "maximum": 95, "x-step": 5,
            "x-unit": "%", "title": "Collect bins at or above",
            "description": "With 'only bins at or above a level': the fill a bin must have when "
                           "the round leaves to be collected.", "x-group": "policy"},
        "forecast_level": {
            "type": "integer", "default": 90, "minimum": 60, "maximum": 100, "x-step": 5,
            "x-unit": "%", "title": "Collect bins forecast to pass",
            "description": "With the forecast policy: a bin is collected when its usual fill rate "
                           "would take it past this level before the next round has emptied it.",
            "x-group": "policy"},
        "start_hour": {
            "type": "integer", "default": 6, "minimum": 4, "maximum": 10, "x-step": 1,
            "x-unit": "o'clock", "title": "Rounds leave the depot at",
            "description": "The city's own pickups start at 06:00.", "x-group": "rounds"},
        "depot": {
            "type": "string", "enum": list(DEPOTS), "default": "strovolos_industrial",
            "x-enum-titles": {k: v[0] for k, v in DEPOTS.items()}, "title": "Depot",
            "description": "Where every trip starts, unloads and ends.", "x-group": "rounds"},
        "fill_rate": {
            "type": "number", "default": 0, "minimum": -1, "maximum": 1, "x-step": 0.5,
            "x-unit": "of each bin's range", "x-assumption": True, "title": "Fill rates",
            "x-source": "Each bin's own stretches between emptyings in the period: -1 is the "
                        "slow end of what it measured (the lower quartile of its stretches), 0 "
                        "its usual rate (their median), 1 the fast end. Bins emptied at most "
                        "once borrow the spread of the others.",
            "description": "How fast the bins fill, within what each one measured."},
        "truck_m3": {
            "type": "number", "default": 20, "minimum": 15, "maximum": 30, "x-step": 1,
            "x-unit": "m3", "x-assumption": True, "title": "Truck body",
            "x-source": "A planning figure for a crane truck emptying bell and underground "
                        "containers (glass isn't compacted); replace it with the fleet's own.",
            "description": "What one truck carries before it goes back to unload."},
        "service_min": {
            "type": "number", "default": 4, "minimum": 3, "maximum": 6, "x-step": 0.5,
            "x-unit": "min a stop", "x-assumption": True, "title": "Time at each bin",
            "x-source": "A planning figure: position the truck, lift, empty and lower one "
                        "container. Not measured here.",
            "description": "Minutes a stop takes, on top of the driving."},
        "unload_min": {
            "type": "number", "default": 20, "minimum": 15, "maximum": 30, "x-step": 5,
            "x-unit": "min a trip", "x-assumption": True, "title": "Unloading",
            "x-source": "A planning figure for tipping a load at the depot. Not measured here.",
            "description": "Minutes at the depot at the end of each trip."},
        "fuel_l_per_km": {
            "type": "number", "default": 0.7, "minimum": 0.57, "maximum": 1.0, "x-step": 0.01,
            "x-unit": "litres a km", "x-assumption": True, "title": "Diesel",
            "x-source": "EU Joint Research Centre, best practice for waste management (citing "
                        "Ricardo-AEA, 2012): a 26 t refuse truck uses 57 to 141 litres per "
                        "100 km in stop-start collection; bell rounds drive further between "
                        "stops, so the lower part of that range.",
            "description": "Diesel a truck burns per km on a round, lifts included."},
        "co2_kg_per_l": {
            "type": "number", "default": 2.68, "minimum": 2.51, "maximum": 2.70, "x-step": 0.01,
            "x-unit": "kg CO2 a litre", "x-assumption": True, "title": "CO2 from diesel",
            "x-source": "IPCC 2006 Guidelines (vol. 2, ch. 3): 74,100 kg CO2 per TJ of diesel, "
                        "at 43 MJ/kg and 0.84 kg/litre. The low end is a diesel with biofuel "
                        "blended in.",
            "description": "CO2 from burning one litre of diesel."},
    },
}

STATE_COLORS = {"overflows": "#b91c1c", "full_at_times": "#f97316", "fine": "#16a34a",
                "never_emptied": "#7e22ce", "no_model": "#a3a3a3"}
STATE_LABELS = {"overflows": "Reaches capacity (95 % or more)",
                "full_at_times": "Full at times (80 % or more)", "fine": "Never full",
                "never_emptied": "Sat full all period, never emptied",
                "no_model": "Not modelled (offline or too few readings)"}
ROUTE_COLORS = {"glass": "#3d8bff", "underground": "#c084fc"}
ROUTE_LABELS = {"glass": "Glass round", "underground": "Underground round"}


@dataclass
class WasteData:
    study: Study
    bins: dict[str, Bin]  # every bin with a position, modelled or not
    matrices: dict[str, Matrix]
    depots: dict[str, tuple[str, tuple[float, float]]]
    sensor: dict[str, dict]
    lines: MatrixSource
    notes: list[str]
    memo: dict = field(default_factory=dict)


def _crew(p: dict) -> Crew:
    return Crew(float(p["truck_m3"]), float(p["service_min"]), float(p["unload_min"]))


def _policy(p: dict) -> Policy:
    return Policy(p["policy"], ROUND_DAYS[int(p["rounds_per_week"])], float(p["threshold"]),
                  float(p["forecast_level"]), int(p["start_hour"]))


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def _days(rounds: int) -> str:
    names = [WEEKDAYS[d] for d in ROUND_DAYS[rounds]]
    if rounds == 7:
        return "every day"
    if rounds in (5, 6):
        return f"Monday to {names[-1]}"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class Waste(Scenario):
    id = "waste"
    title = "Waste collection"
    summary = ("Which bins to empty when: fixed rounds, only bins above a level, or bins forecast "
               "to fill, replayed on what the city's bin sensors measured, with the routes the "
               "trucks would drive.")
    question = ("How often, and which bins, should the trucks collect, to keep bins from "
                "overflowing with the fewest kilometres?")
    params = PARAMS
    main_kpi = "full_hours"
    today = "the city's own pickups, replayed on the same model"
    sources = ("smart_nicosia", "twin_memory", "valhalla", "openstreetmap")
    method = (
        "Each bin's fill level, read every two hours by its sensor, shows when it was emptied "
        "(its own pickup reports, and falls in its level that the next reading confirms, between "
        "05:00 and 15:00 when the trucks work) and how fast it fills between emptyings (the rise "
        "from the start of each stretch to its end, as medians of three readings so one jumpy "
        "reading can't swing it). When in the day and week bins fill is pooled over each kind of "
        "container. A policy is replayed hour by hour over the period from the levels the bins "
        "read when it began: a round leaves the depot at the set hour on each round day, picks "
        "its bins, splits them into trips that fit the truck (Clarke and Wright's savings, then "
        "2-opt) on road distances and times for a heavy truck from Valhalla, and empties each "
        "bin when the truck reaches it. Today's practice is the city's own pickups replayed on "
        "the same model and routed the same way, so the two sides share the model's biases. "
        "The backtest replays the city's pickups and compares the hours the bins sat full with "
        "what the sensors read, with fill rates from the first half of the period tested on the "
        "second.")

    # -- reading --------------------------------------------------------------------------------

    def prepare(self, ctx: ScenarioContext, period: Period) -> WasteData:
        if ctx.memory is None:
            raise ScenarioError("the twin's memory isn't available, and the waste scenario "
                                "learns only from it")
        start_ms = int(period.start.timestamp() * 1000)
        end_ms = int(period.end.timestamp() * 1000)
        try:
            with ctx.memory.reading() as con:
                rows = con.execute(
                    """SELECT name, display_name, lat, lon, metadata FROM devices
                       WHERE type = 'Wastebin' ORDER BY name""").fetchall()
            devices = [dict(zip(("name", "display_name", "lat", "lon", "metadata"), r)) for r in rows]
            records = ctx.memory.readings([d["name"] for d in devices], KEYS,
                                          start_ms - 6 * HOUR_MS, end_ms)
        except Unavailable as e:
            raise ScenarioError(f"the twin's memory can't be read: {e}") from None
        if not devices:
            raise ScenarioError("the twin's memory holds no bins yet")
        data = bin_data(devices, records, TZ)
        notes = []
        # Start where the memory's bin readings start, when that's later than the period.
        firsts = sorted(next((t for t in d.times if t >= start_ms), None) or end_ms
                        for d in data if any(t >= start_ms for t in d.times))
        if not firsts:
            raise ScenarioError(f"the twin's memory holds no bin readings for {period.label.lower()}")
        cover = firsts[len(firsts) // 2]
        begin = midnight_after(max(start_ms, cover - 2 * HOUR_MS), TZ)
        end = midnight_before(end_ms, TZ)
        days = (end - begin) // DAY_MS
        if days < MIN_DAYS:
            raise ScenarioError(f"the twin's memory holds {max(0, days)} whole days of bin readings "
                                f"in {period.label.lower()}; the waste scenario needs at least "
                                f"{MIN_DAYS}. Try a longer period.")
        if begin - start_ms > DAY_MS:
            notes.append(f"The twin's memory holds bin readings from {_date(begin)}, so the study "
                         f"covers {days} days rather than the {round(period.days)} asked for.")
        study = Study(data, begin, int(days), TZ)
        if not study.models:
            raise ScenarioError("no bin in the period has enough readings to model")
        depots = dict(DEPOTS)
        mids = [(m.bin.lat, m.bin.lon) for m in study.models]
        clat = statistics.mean(p[0] for p in mids)
        clon = statistics.mean(p[1] for p in mids)
        centre = min(mids, key=lambda p: (p[0] - clat) ** 2 + (p[1] - clon) ** 2)
        depots["bins_centre"] = (DEPOTS["bins_centre"][0], centre)
        source = MatrixSource(ctx.settings.valhalla_url, ctx.settings.user_agent, _cache(ctx))
        points = list(dict.fromkeys(mids))
        matrices = {k: source.matrix(at, points) for k, (_, at) in depots.items()}
        estimated = max(m.estimated for m in matrices.values())
        no_road = max(m.no_road for m in matrices.values())
        if no_road:
            notes.append(f"Valhalla finds no road a heavy truck may take for {no_road} pairs of "
                         "places (a bin on a pedestrian street, say); those distances stay "
                         "estimated from straight lines.")
        if estimated:
            n = len(points) + 1
            share = round(100 * estimated / (n * (n - 1)))
            notes.append(f"{share}% of the road distances between bins aren't fetched yet and "
                         "are estimated from "
                         "straight lines (1.35 times longer, at 20 km/h): the twin fetches them "
                         "from Valhalla a few at a time, so kilometres and hours firm up over the "
                         "next runs.")
        bins = {d.bin.id: d.bin for d in data}
        return WasteData(study, bins, matrices, depots, sensor_hours(study), source, notes)

    # -- one set of parameters ------------------------------------------------------------------

    def _outcomes(self, data: WasteData, p: dict) -> tuple[Outcome, Outcome]:
        crew = _crew(p)
        fill = float(p["fill_rate"])
        matrix = data.matrices[p["depot"]]
        if len(data.memo) > 400:  # a long-lived prepared period: keep it bounded
            for k in list(data.memo)[:200]:
                data.memo.pop(k, None)
        bkey = ("base", p["depot"], fill, crew, int(p["start_hour"]))
        base = data.memo.get(bkey)
        if base is None:
            base = data.memo[bkey] = baseline(data.study, matrix, crew, fill, int(p["start_hour"]))
        policy = _policy(p)
        skey = ("policy", policy, p["depot"], fill, crew)
        scen = data.memo.get(skey)
        if scen is None:
            scen = data.memo[skey] = policy_outcome(data.study, policy, matrix, crew, fill)
        return base, scen

    def evaluate(self, data: WasteData, params: dict) -> Result:
        st = data.study
        base, scen = self._outcomes(data, params)
        wk = 7 / st.days
        fuel, co2 = float(params["fuel_l_per_km"]), float(params["co2_kg_per_l"])
        service, unload = float(params["service_min"]), float(params["unload_min"])

        def litres(o: Outcome) -> float:
            return o.km * fuel * wk

        kpis = [
            kpi("full_hours", "Bin-hours full", "bin-hours a week", base.full_hours * wk,
                scen.full_hours * wk, digits=0,
                note="Hours a bin spends at 80 % or more (the city's alarm level), summed over "
                     "the bins"),
            kpi("capacity_hours", "Bin-hours at capacity", "bin-hours a week",
                base.capacity_hours * wk, scen.capacity_hours * wk, digits=0,
                note="Hours at 95 % or more, when waste starts going beside the bin; an upper "
                     "bound (see the backtest)"),
            kpi("bins_at_capacity", "Bins that reach capacity", "bins", base.bins_at_capacity,
                scen.bins_at_capacity, digits=0),
            kpi("collections", "Collections", "a week", base.collections * wk,
                scen.collections * wk, digits=0),
            kpi("low_lifts", "Collections of bins under 30 % full", "a week",
                base.low_lifts * wk, scen.low_lifts * wk, digits=0,
                note="Lifts that could have waited"),
            kpi("truck_km", "Truck kilometres", "km a week", base.km * wk, scen.km * wk, digits=0),
            kpi("driver_hours", "Driver hours", "hours a week",
                base.hours(service, unload) * wk, scen.hours(service, unload) * wk, digits=1),
            kpi("fuel", "Diesel", "litres a week", litres(base), litres(scen), digits=0),
            kpi("co2", "CO2 from the trucks", "tonnes a year",
                litres(base) * WEEKS_A_YEAR * co2 / 1000, litres(scen) * WEEKS_A_YEAR * co2 / 1000,
                digits=1),
            kpi("bigger_bins", "Bins that need a bigger container", "bins", len(base.bigger),
                len(scen.bigger), digits=0,
                note=f"Emptied on every round day, they would still reach capacity in a busy week "
                     f"during the longest wait between two: {_wait(_policy(params))}"),
        ]
        policy = _policy(params)
        days_scen = [self._day(data, plan, int(params["start_hour"])) for plan in scen.plans]
        days_base = [self._day(data, plan, int(params["start_hour"])) for plan in base.plans]
        drawn = max(scen.plans, key=lambda pl: sum(len(t.stops) for t in pl.trips), default=None)
        layers = {
            "baseline": self._bins_layer(data, base, "baseline", "Bins under today's pickups",
                                         fill=float(params["fill_rate"])),
            "change": self._bins_layer(data, scen, "scenario",
                                       f"Bins under {self.label(params).lower()}", base,
                                       fill=float(params["fill_rate"])),
        }
        if drawn is not None and drawn.trips:
            layers["route"] = self._route_layer(data, params, drawn)
        series, charts = self._series(data, base, scen)
        excluded = [{"id": d.bin.id, "name": d.bin.name, "status": d.status,
                     "label": STATUS_LABELS[d.status]}
                    for d in st.data if d.status != "modelled"]
        counts = st.status_counts()
        watch = sorted(st.models, key=lambda m: -base.per_bin[m.bin.id]["full_hours"])[:12]
        details = {
            "summary": {
                "bins_modelled": len(st.models), "bins_in_inventory": len(st.data),
                "not_modelled": {STATUS_LABELS[k]: v for k, v in counts.items() if k != "modelled"},
                "days": st.days, "from": _date(st.start_ms), "to": _date(st.end_ms - DAY_MS),
                "round_days": _days(int(params["rounds_per_week"])),
                "city_round_days": ", ".join(WEEKDAYS[d] for d in st.measured_round_days()),
                "depot": data.depots[params["depot"]][0],
                "route_drawn": days_scen[scen.plans.index(drawn)]["label"] if drawn else None,
            },
            "start_hour": int(params["start_hour"]),
            "depot": {"name": data.depots[params["depot"]][0],
                      "lat": data.depots[params["depot"]][1][0],
                      "lon": data.depots[params["depot"]][1][1]},
            "days": {"scenario": days_scen, "baseline": days_base},
            "route_date": days_scen[scen.plans.index(drawn)]["date"] if drawn else None,
            "watch": [{"id": m.bin.id, "name": m.bin.name,
                       "full_hours_baseline": round(base.per_bin[m.bin.id]["full_hours"] * wk),
                       "full_hours": round(scen.per_bin[m.bin.id]["full_hours"] * wk),
                       "fill_per_day": round(rate_at(m.rate, float(params["fill_rate"])), 1),
                       "collections_baseline": round(base.per_bin[m.bin.id]["collections"] * wk, 1),
                       "collections": round(scen.per_bin[m.bin.id]["collections"] * wk, 1)}
                      for m in watch if base.per_bin[m.bin.id]["full_hours"] > 0],
            "bigger": [{"id": b, "name": data.bins[b].name, "volume_l": data.bins[b].volume_l,
                        "need_l": need} for b, need in sorted(scen.bigger.items())],
            "excluded": excluded,
            "shape": {s: {"hour": list(sh.hour), "weekday": list(sh.weekday), "readings": sh.readings}
                      for s, sh in st.shapes.items()},
        }
        notes = [
            f"{len(st.models)} of the {len(st.data)} bins are modelled; the others are left out of "
            "every number: " + "; ".join(f"{v} {STATUS_LABELS[k].lower()}"
                                         for k, v in counts.most_common() if k != "modelled") + ".",
            "Today's kilometres route the city's own pickups the way every policy's rounds are "
            "routed; the trucks' real routes aren't recorded, so they likely drive more than "
            "shown, and the savings may be larger.",
            f"Depot: {data.depots[params['depot']][0]}. Every policy's kilometres move with it; "
            "the differences between policies much less.",
            "Hours at capacity are an upper bound (see the backtest); hours full are the better "
            "measure.",
            "Glass and underground containers go on separate rounds; which waste the underground "
            "ones hold isn't recorded.",
            "Fill rates are this period's: seasons, holidays and events change them.",
            *data.notes,
        ]
        if policy.kind == "forecast":
            notes.append("The forecast uses each bin's usual fill rate, also when the fill-rate "
                         "assumption is moved: its range shows what a wrong forecast costs.")
        return Result(kpis=kpis, layers=layers, series=series, charts=charts, details=details,
                      sources=list(self.sources), notes=notes)

    def backtest(self, ctx: ScenarioContext, data: WasteData, params: dict,
                 period: Period) -> dict | None:
        if "backtest" not in data.memo:
            data.memo["backtest"] = backtest(data.study)
        rows = data.memo["backtest"]
        if not rows:
            return None
        main = dict(rows[0])
        main["others"] = [{k: v for k, v in r.items() if k != "rows"} for r in rows[1:]]
        return main

    def finish(self, ctx: ScenarioContext, result: Result, params: dict) -> None:
        """Once per run: road lines for the drawn day's trips (one Valhalla request per ten
        stops, kept on disk), in place of the straight lines between stops."""
        layer = result.layers.get("route")
        if not layer:
            return
        source = MatrixSource(ctx.settings.valhalla_url, ctx.settings.user_agent, _cache(ctx))
        failed = False
        for f in layer["features"]:
            pts = [tuple(c) for c in f["properties"].get("_stops", [])]
            if len(pts) < 2 or f["properties"].get("road") or failed:
                continue
            line = source.route_line(pts)
            if line is None:
                failed = True  # Valhalla can't answer: keep the straight lines between stops
                continue
            f["geometry"]["coordinates"] = line
            f["properties"]["road"] = True
            f["properties"].pop("_stops", None)  # only kept for drawing the road line
        road = all(f["properties"].get("road") for f in layer["features"])
        layer["meta"]["note"] = ("Road lines from Valhalla's heavy truck profile." if road else
                                 "Straight lines between stops, in the order the truck visits them.")

    def label(self, params: dict) -> str:
        n = int(params["rounds_per_week"])
        when = _days(n)
        if params["policy"] == "fixed":
            return f"Every bin, {when}"
        if params["policy"] == "threshold":
            return f"Bins at {params['threshold']}% or more, {when}"
        return f"Bins forecast past {params['forecast_level']}%, {when}"

    # -- pieces of the result -------------------------------------------------------------------

    def _day(self, data: WasteData, plan: DayPlan, start_hour: int) -> dict:
        t = local(plan.t_ms, TZ)
        trips = []
        for k, trip in enumerate(plan.trips):
            trips.append({
                "n": k + 1, "stream": trip.stream, "truck": trip.truck + 1,
                "start": _hm(start_hour * 60 + trip.start_min), "end": _hm(start_hour * 60 + trip.end_min),
                "km": round(trip.drive_km, 1),
                "stops": [{"id": s.bin_id, "name": data.bins[s.bin_id].name,
                           "eta": _hm(start_hour * 60 + s.eta_min), "level": round(s.level),
                           "load_m3": round(s.load_m3, 2)} for s in trip.stops]})
        return {"date": t.date().isoformat(), "label": f"{t:%a} {t.day} {t:%b}",
                "trucks": len({tr.truck for tr in plan.trips}), "km": round(plan.km, 1),
                "hours": round(plan.hours, 1), "stops": sum(len(tr.stops) for tr in plan.trips),
                "trips": trips}

    def _bins_layer(self, data: WasteData, o: Outcome, variant: str, title: str,
                    base: Outcome | None = None, fill: float = 0.0) -> dict:
        st = data.study
        wk = 7 / st.days
        models = {m.bin.id: m for m in st.models}
        feats = []
        for d in st.data:
            b = d.bin
            props: dict[str, Any] = {"id": b.id, "label": b.name, "stream": b.stream,
                                     "volume_l": b.volume_l}
            if b.id in models:
                pb = o.per_bin[b.id]
                state = ("overflows" if pb["capacity_hours"] else
                         "full_at_times" if pb["full_hours"] else "fine")
                m = models[b.id]
                props |= {"full_hours": round(pb["full_hours"] * wk),
                          "capacity_hours": round(pb["capacity_hours"] * wk),
                          "collections": round(pb["collections"] * wk, 1),
                          "fill_per_day": round(rate_at(m.rate, fill), 1),
                          "fill_range": [round(m.rate.low, 1), round(m.rate.high, 1)],
                          "read_full_hours": round(data.sensor[b.id]["full_hours"] * wk)}
                if base is not None:
                    pbb = base.per_bin[b.id]
                    props |= {"full_hours_today": round(pbb["full_hours"] * wk),
                              "collections_today": round(pbb["collections"] * wk, 1)}
                if b.id in o.bigger:
                    props["bigger_l"] = o.bigger[b.id]
            else:
                state = "never_emptied" if d.status == "sat_full" else "no_model"
                props["status"] = STATUS_LABELS[d.status]
            props |= {"state": state, "color": STATE_COLORS[state]}
            feats.append({"type": "Feature", "geometry": {"type": "Point",
                                                          "coordinates": [b.lon, b.lat]},
                          "properties": props})
        used = dict.fromkeys(f["properties"]["state"] for f in feats)
        return {"type": "FeatureCollection", "features": feats,
                "meta": {"title": title, "variant": variant, "geometry": "point",
                         "legend": {s: STATE_COLORS[s] for s in STATE_COLORS if s in used},
                         "legend_labels": {s: STATE_LABELS[s] for s in STATE_LABELS if s in used},
                         "value_field": "full_hours", "unit": "bin-hours full a week"}}

    def _route_layer(self, data: WasteData, params: dict, plan: DayPlan) -> dict:
        depot = data.depots[params["depot"]][1]
        start = int(params["start_hour"])
        t = local(plan.t_ms, TZ)
        feats = []
        for k, trip in enumerate(plan.trips):
            stops = [(depot[0], depot[1]), *[(data.bins[s.bin_id].lat, data.bins[s.bin_id].lon)
                                             for s in trip.stops], (depot[0], depot[1])]
            cached = data.lines.cached_line(stops)
            feats.append({"type": "Feature", "geometry": {
                "type": "LineString",
                "coordinates": cached or [[lon, lat] for lat, lon in stops]},
                "properties": {
                    "id": f"trip-{t.date().isoformat()}-{k + 1}", "state": trip.stream,
                    "color": ROUTE_COLORS.get(trip.stream, "#3d8bff"),
                    "label": f"{ROUTE_LABELS.get(trip.stream, 'Round')}, truck {trip.truck + 1}, "
                             f"trip {k + 1}: {len(trip.stops)} stops, {trip.drive_km:.1f} km, "
                             f"{_hm(start * 60 + trip.start_min)} to {_hm(start * 60 + trip.end_min)}",
                    "trip": k + 1, "truck": trip.truck + 1, "stops": len(trip.stops),
                    "km": round(trip.drive_km, 1), "road": bool(cached),
                    "order": [s.bin_id for s in trip.stops], "_stops": [list(p) for p in stops]}})
        used = dict.fromkeys(f["properties"]["state"] for f in feats)
        return {"type": "FeatureCollection", "features": feats,
                "meta": {"title": f"This scenario's rounds on {t:%a} {t.day} {t:%b}", "variant": None, "show": True,
                         "geometry": "route", "chat": True,
                         "legend": {s: ROUTE_COLORS[s] for s in used if s in ROUTE_COLORS},
                         "legend_labels": {s: ROUTE_LABELS[s] for s in used if s in ROUTE_LABELS},
                         "value_field": "km", "unit": "km"}}

    def _series(self, data: WasteData, base: Outcome, scen: Outcome) -> tuple[list[dict], list[dict]]:
        st = data.study
        rows = []
        for variant, o in (("baseline", base), ("scenario", scen)):
            for d in o.daily:
                day = local(st.start_ms + d["day"] * DAY_MS + 12 * HOUR_MS, TZ).date().isoformat()
                rows.append({"metric": "full_hours", "time": day, "variant": variant,
                             "value": d["full_hours"]})
                rows.append({"metric": "collections", "time": day, "variant": variant,
                             "value": d["collections"]})
                rows.append({"metric": "truck_km", "time": day, "variant": variant,
                             "value": d["km"]})
        for stream, sh in st.shapes.items():
            for i, f in enumerate(sh.hour):
                rows.append({"metric": "fill_by_hour", "time": f"{2 * i:02d}:00", "variant": stream,
                             "value": round(f, 2)})
            for i, f in enumerate(sh.weekday):
                rows.append({"metric": "fill_by_weekday", "time": WEEKDAYS[i][:3], "variant": stream,
                             "value": round(f, 2)})
        charts = [
            {"metric": "full_hours", "title": "Bin-hours full each day", "unit": "bin-hours",
             "x": "date", "kind": "line"},
            {"metric": "collections", "title": "Collections each day", "unit": "collections",
             "x": "date", "kind": "bar"},
            {"metric": "truck_km", "title": "Truck kilometres each day", "unit": "km", "x": "date",
             "kind": "bar"},
            {"metric": "fill_by_hour", "title": "When bins fill, by time of day",
             "unit": "times the average", "x": "category", "kind": "bar",
             "note": "Pooled over each kind of container and pulled towards 1 where the sensors "
                     "are too noisy to tell"},
            {"metric": "fill_by_weekday", "title": "When bins fill, by day of the week",
             "unit": "times the average", "x": "category", "kind": "bar"},
        ]
        return rows, charts


def _wait(policy: Policy) -> str:
    """The longest wait between round days, in words: "3 days (Friday to Monday)"."""
    days = sorted(set(policy.round_days))
    if len(days) <= 1:
        return "a week"
    gaps = [((days[(i + 1) % len(days)] - d) % 7 or 7, d, days[(i + 1) % len(days)])
            for i, d in enumerate(days)]
    n, a, b = max(gaps)
    span = f"{n} {'day' if n == 1 else 'days'}"
    return span if len({g for g, _, _ in gaps}) == 1 else f"{span} ({WEEKDAYS[a]} to {WEEKDAYS[b]})"


def _cache(ctx: ScenarioContext):
    return (ctx.cache_dir / "waste") if ctx.cache_dir else (CACHE_DIR / "scenarios" / "waste")


def _date(t_ms: int) -> str:
    t = local(t_ms, TZ)
    return f"{t.day} {t:%b %Y}"


WASTE = register(Waste())

__all__ = ["WASTE", "Waste", "WasteData"]

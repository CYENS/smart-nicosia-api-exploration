"""The waste collection scenario: pickups, fill rates, when bins fill, each policy's replay, the
routes, the KPIs and the backtest. Offline: 28 days of ten real bins (tests/fixtures), small
synthetic series and matrices, and Valhalla stubbed out."""

import itertools
import json
import math
import random
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from chatbot.routing import best_order
from chatbot.scenarios import Lab, Period, ScenarioContext, run
from chatbot.scenarios import rounds as rounds_mod
from chatbot.scenarios.rounds import Crew, Matrix, MatrixSource, plan_round, route_cost, solve
from chatbot.scenarios.waste import WASTE
from chatbot.scenarios.waste_model import (
    DAY_MS,
    HOUR_MS,
    ROUND_DAYS,
    Bin,
    BinModel,
    FillRate,
    Pickup,
    Policy,
    Shape,
    bigger_container,
    detect_pickups,
    fill_rate,
    net_rise,
    pooled_spread,
    rate_at,
    simulate,
    stretches,
    weekly_shape,
)
from chatbot.scenarios.waste_study import Study, backtest, bin_data, parse_bin, parse_records
from chatbot.settings import Settings
from chatbot.web import WebError
from tests.conftest import load

TZ = ZoneInfo("Asia/Nicosia")
FIX = load("waste_bins_2026-11.json")
START = int(datetime.fromisoformat(FIX["start"]).timestamp() * 1000)  # local midnight, 29 Oct
DAYS = 28


def at(day: int, hour: float = 0) -> int:
    return START + day * DAY_MS + int(hour * HOUR_MS)


def series(points: list[tuple[float, float]]) -> tuple[list[int], list[float]]:
    """[(hours from START, level)] -> times, levels."""
    return [START + int(h * HOUR_MS) for h, _ in points], [v for _, v in points]


def fixture_data():
    devices = [{"name": b["id"], "display_name": b["name"], "lat": b["lat"], "lon": b["lon"],
                "metadata": b["metadata"]} for b in FIX["bins"]]
    return bin_data(devices, {b["id"]: b["records"] for b in FIX["bins"]}, TZ)


# -- pickups ------------------------------------------------------------------------------------


def test_the_fixture_bins_parse_with_their_kind_and_size():
    b = parse_bin({"name": "YW563", "display_name": "Glass 269", "lat": 35.1, "lon": 33.3,
                   "metadata": json.dumps({"extra": {"trashtype": "Glass", "volume": "2500"}})})
    assert (b.stream, b.volume_l) == ("glass", 2500.0)
    under = parse_bin({"name": "u", "lat": 35.1, "lon": 33.3,
                       "metadata": {"extra": {"trashtype": "Underground", "volume": "5000"}}})
    assert under.stream == "underground"
    assert parse_bin({"name": "x", "lat": 35.1, "lon": 33.3,
                      "metadata": {"extra": {"volume": "25000"}}}).volume_l == 2500  # the slip
    assert parse_bin({"name": "nowhere", "lat": None, "lon": None}) is None


def test_level_falls_find_most_of_the_pickups_the_bins_report():
    found = missed = 0
    for b in FIX["bins"]:
        times, levels, reported = parse_records(b["records"])
        by_levels = detect_pickups(times, levels, (), tz=TZ)
        for p in reported:
            if not 5 <= datetime.fromtimestamp(p.t_ms / 1000, TZ).hour < 15:
                continue
            if any(abs(p.t_ms - d.t_ms) < 4 * HOUR_MS for d in by_levels):
                found += 1
            else:
                missed += 1
    assert found == 9 and missed <= 3  # the misses: lifts of bins under 25 % full


def test_a_spike_down_is_not_a_pickup_but_a_confirmed_fall_is():
    times, levels = series([(0, 60), (2, 62), (4, 5), (6, 61), (8, 63)])
    assert detect_pickups(times, levels) == []
    times, levels = series([(0, 60), (2, 62), (4, 5), (6, 8), (8, 10)])
    (p,) = detect_pickups(times, levels)
    assert (p.t_ms, p.before, p.after, p.source) == (times[2], 60, 5, "level")
    # one spike up before a normal reading is not a fall
    times, levels = series([(0, 52), (2, 94), (4, 56), (6, 56)])
    assert detect_pickups(times, levels) == []


def test_some_containers_read_40_after_a_lift_and_nights_dont_count():
    times, levels = series([(30, 95), (31, 95), (32, 40), (34, 40), (36, 41)])  # 06:00 to 12:00
    assert [p.after for p in detect_pickups(times, levels, tz=TZ)] == [40]
    night = series([(46, 80), (48, 82), (50, 3), (52, 4)])  # 22:00 to 04:00
    assert detect_pickups(*night, tz=TZ) == []
    reported_at_night = Pickup(START + 50 * HOUR_MS, 80, 3, "sensor")
    assert detect_pickups(*night, [reported_at_night], tz=TZ) == []
    reported = Pickup(START + 31 * HOUR_MS, 95, 0, "sensor")
    out = detect_pickups(times, levels, [reported], tz=TZ)
    assert [p.source for p in out] == ["sensor"]  # the fall is the same pickup


# -- fill rates ---------------------------------------------------------------------------------


def test_net_rise_counts_a_step_in_full_and_ignores_one_jumpy_reading():
    # 2 % a day, a crate of bottles (+30) on day 5, and one wild reading
    pts = [(START + h * HOUR_MS, 10 + 2 * h / 24 + (30 if h >= 120 else 0)) for h in range(0, 240, 2)]
    pts[40] = (pts[40][0], 99.0)
    rate = net_rise(pts)
    assert 4.5 < rate < 5.5  # (10 + 20 + 30 - 10) over 10 days, not the 2 a robust slope sees


def test_stretches_end_at_two_readings_in_a_row_at_the_top():
    times, levels = series([(h, min(100, 20 + h)) for h in range(0, 100, 4)])
    (s,) = stretches(times, levels, [])
    assert s[-1][1] >= 90 and s[-2][1] >= 90 and len(s) < len(times)
    times, levels = series([(h, 50 if h != 8 else 95) for h in range(0, 60, 4)])  # one spike
    (s,) = stretches(times, levels, [])
    assert len(s) == len(times)


def test_fill_rates_take_quartiles_with_three_stretches_and_borrow_otherwise():
    r = fill_rate([(4.0, 3), (6.0, 2), (5.0, 4), (12.0, 1)], pooled_spread=0.25)
    assert r.basis == "stretches" and r.stretches == 4 and r.low <= 5.0 <= r.high
    assert r.per_day == 5.0  # the duration-weighted median
    one = fill_rate([(8.0, 5)], pooled_spread=0.25)
    assert (one.per_day, one.low, one.high, one.basis) == (8.0, 6.0, 10.0, "pooled")
    assert fill_rate([], 0.25) is None
    assert pooled_spread([[(4, 1), (6, 1), (8, 1)], [(1, 1)]]) == pytest.approx(1 / 6)
    assert rate_at(one, -1) == 6.0 and rate_at(one, 0.5) == 9.0


def test_the_weekly_shape_finds_when_bins_fill_and_stays_flat_on_noise():
    rng = random.Random(1)
    busy = []
    for b in range(20):
        lv, pts = 0.0, []
        for h in range(0, 24 * 21, 2):
            hour = datetime.fromtimestamp((START + h * HOUR_MS) / 1000, TZ).hour
            lv += 3.0 if hour in (8, 10) else 0.1  # the rises from 06:00 to 10:00
            pts.append((h, min(85.0, lv % 80)))
        t, v = series(pts)
        busy.append((t, v, detect_pickups(t, v)))
    shape = weekly_shape(busy, TZ)
    peak = sorted(range(12), key=lambda i: -shape.hour[i])[:2]
    assert sorted(peak) == [3, 4] and shape.hour[3] > 3  # the blocks from 06:00 to 10:00
    noisy = []
    for b in range(3):
        t, v = series([(h, 40 + rng.gauss(0, 12)) for h in range(0, 24 * 14, 2)])
        noisy.append((t, [min(89.0, max(0.0, x)) for x in v], []))
    flat = weekly_shape(noisy, TZ)
    assert all(abs(f - 1) < 0.5 for f in flat.hour + flat.weekday)


# -- policies -----------------------------------------------------------------------------------


def model(id_: str, per_day: float, start: float = 0.0, stream: str = "glass",
          lat: float = 35.17, lon: float = 33.36) -> BinModel:
    b = Bin(id_, id_, lat, lon, stream, 2500.0)
    return BinModel(b, FillRate(per_day, per_day * 0.5, per_day * 1.5, 3, "stretches"), 0.0, start)


def flat(_stream: str) -> Shape:
    return Shape.flat()


def test_a_bin_never_collected_counts_its_hours_full_and_at_capacity():
    sim = simulate([model("a", 10.0)], START, 14, Policy("fixed", ()), shape_of=flat, tz=TZ)
    o = sim.bins["a"]
    assert o.collections == 0
    assert o.full_hours == 14 * 24 - 192 + 1  # reaches 80 % at the end of hour 192
    assert o.capacity_hours == 14 * 24 - 228 + 1
    assert sum(d["full_hours"] for d in sim.daily) == o.full_hours


def test_fixed_rounds_collect_every_bin_on_every_round_day():
    bins = [model("a", 10.0), model("b", 1.0)]
    sim = simulate(bins, START, 14, Policy("fixed", ROUND_DAYS[2]), shape_of=flat, tz=TZ)
    # 29 Oct 2026 is a Thursday: Thursdays and Mondays in 14 days, four rounds
    assert len(sim.plans) == 4 and sim.bins["a"].collections == 4 and sim.bins["b"].collections == 4
    assert sim.bins["b"].low_lifts == 4  # a slow bin is always lifted nearly empty
    assert sim.bins["a"].full_hours == 0


def test_a_threshold_takes_only_bins_at_the_level_and_a_forecast_takes_them_early():
    bins = [model("fast", 20.0), model("slow", 2.0)]
    th = simulate(bins, START, 14, Policy("threshold", ROUND_DAYS[5], threshold=70), shape_of=flat, tz=TZ)
    assert th.bins["slow"].collections == 0 and th.bins["fast"].collections >= 3
    assert th.bins["fast"].low_lifts == 0
    fc = simulate(bins, START, 14, Policy("forecast", ROUND_DAYS[5], forecast_level=90),
                  shape_of=flat, tz=TZ)
    assert fc.bins["slow"].collections == 0
    assert fc.bins["fast"].capacity_hours <= th.bins["fast"].capacity_hours
    assert fc.bins["fast"].full_hours < 14 * 24


def test_the_forecast_predicts_with_the_usual_rate_whatever_the_bins_do():
    bins = [model("a", 10.0)]
    usual = simulate(bins, START, 14, Policy("forecast", ROUND_DAYS[5]), shape_of=flat, tz=TZ)
    fast = simulate(bins, START, 14, Policy("forecast", ROUND_DAYS[5]), shape_of=flat, tz=TZ, fill=1)
    assert fast.bins["a"].full_hours > usual.bins["a"].full_hours


def test_the_city_pickups_replay_and_known_hours_limit_the_count():
    bins = [model("a", 10.0)]
    picks = {"a": [Pickup(at(5, 8), 50, 0, "sensor"), Pickup(at(12, 8), 70, 0, "level")]}
    sim = simulate(bins, START, 14, Policy("measured"), shape_of=flat, tz=TZ, measured=picks)
    assert sim.bins["a"].collections == 2 and sim.bins["a"].full_hours == 0
    known = {"a": [h < 24 for h in range(14 * 24)]}
    half = simulate(bins, START, 14, Policy("fixed", ()), shape_of=flat, tz=TZ, known=known)
    assert half.bins["a"].known_hours == 24 and half.bins["a"].full_hours == 0


def test_a_bin_that_fills_between_rounds_needs_a_bigger_container():
    m = model("a", 15.0)  # 22.5 % a day at its fast end
    weekly = Policy("fixed", ROUND_DAYS[1])
    assert bigger_container(m, weekly, (1500, 2500, 3200, 5000)) == pytest.approx(5000.0) or \
        bigger_container(m, weekly, (1500, 2500, 3200, 5000)) > 5000
    assert bigger_container(m, Policy("fixed", ROUND_DAYS[7]), (1500, 2500, 3200, 5000)) is None
    assert weekly.max_gap_days() == 7 and Policy("fixed", ROUND_DAYS[2]).max_gap_days() == 4


# -- routes -------------------------------------------------------------------------------------


def grid_matrix(n: int, seed: int = 3) -> Matrix:
    rng = random.Random(seed)
    pts = [(35.17, 33.36)] + [(35.15 + rng.random() * 0.04, 33.32 + rng.random() * 0.06)
                              for _ in range(n)]
    km, sec = rounds_mod.straight_matrix(pts)
    return Matrix(pts, km, sec, "straight lines")


def test_savings_and_2opt_come_close_to_the_exact_best_tour():
    for seed in range(5):
        m = grid_matrix(8, seed)
        nodes = list(range(1, 9))
        (trip,) = solve(nodes, {i: 1.0 for i in nodes}, 100.0, m.seconds)
        exact = best_order(m.seconds, return_to_start=True)
        assert sorted(trip) == nodes
        assert route_cost(trip, m.seconds) <= 1.05 * route_cost(exact[1:], m.seconds)


def test_trips_respect_the_truck_and_cover_every_bin_once():
    m = grid_matrix(12)
    nodes = list(range(1, 13))
    trips = solve(nodes, {i: 2.0 for i in nodes}, 7.0, m.seconds)
    assert sorted(itertools.chain(*trips)) == nodes
    assert all(len(t) * 2.0 <= 7.0 for t in trips) and len(trips) >= 4


def test_a_round_is_timed_from_the_depot_and_shared_between_trucks():
    m = grid_matrix(10)
    bins = [(Bin(f"b{i}", f"b{i}", *m.points[i], "glass", 2500.0), 80.0, 2.0) for i in range(1, 11)]
    trips = plan_round("glass", bins, m, Crew(truck_m3=6.0, service_min=4, unload_min=20, shift_h=1.0))
    assert sum(len(t.stops) for t in trips) == 10 and all(len(t.stops) <= 3 for t in trips)
    for t in trips:
        etas = [s.eta_min for s in t.stops]
        assert etas == sorted(etas) and t.start_min <= etas[0] and t.end_min > etas[-1]
        assert t.service_min == 4 * len(t.stops) and t.unload_min == 20
    assert len({t.truck for t in trips}) >= 2  # a one-hour shift can't fit them all


def test_the_matrix_is_cached_pair_by_pair_and_estimated_while_missing(tmp_path, monkeypatch):
    calls = []

    def fake_get_json(url, *, user_agent, data=None, params=None, timeout=30):
        body = json.loads(data)
        calls.append((len(body["sources"]), len(body["targets"])))
        return {"sources_to_targets": [[{"from_index": 0, "to_index": j, "distance": 1.0 + j,
                                         "time": 60.0 * (j + 1)} for j in range(len(body["targets"]))]]}

    monkeypatch.setattr(rounds_mod, "get_json", fake_get_json)
    monkeypatch.setattr(rounds_mod, "REQUEST_GAP_S", 0)
    pts = [(35.15 + i * 0.001, 33.33) for i in range(5)]
    src = MatrixSource("http://valhalla", "test", tmp_path, fetch_per_run=2)
    first = src.matrix(pts[0], pts[1:])
    assert len(calls) == 2 and first.source == "partly estimated" and 0 < first.estimated < 20
    assert all(s == 1 and t <= rounds_mod.MAX_PAIRS for s, t in calls)
    again = MatrixSource("http://valhalla", "test", tmp_path, fetch_per_run=100)
    full = again.matrix(pts[0], pts[1:])
    assert full.source == "valhalla" and full.estimated == 0 and len(calls) == 5
    assert MatrixSource("http://valhalla", "test", tmp_path).matrix(pts[0], pts[1:]).estimated == 0
    assert len(calls) == 5  # every pair from disk now

    def down(*a, **k):
        raise WebError("valhalla unreachable")

    monkeypatch.setattr(rounds_mod, "get_json", down)
    off = MatrixSource("http://valhalla", "test", tmp_path / "other").matrix(pts[0], pts[1:])
    assert off.source == "straight lines" and off.km[0][1] > 0


# -- the study, the KPIs and the backtest ------------------------------------------------------


def test_the_study_models_the_fixture_bins_and_says_why_it_leaves_one_out():
    st = Study(fixture_data(), START, DAYS, TZ)
    status = {d.bin.id: d.status for d in st.data}
    assert status["YW1056"] == "sat_full"  # at 71 to 100 % all month, never emptied
    assert sum(s == "modelled" for s in status.values()) >= 8
    fast = next(m for m in st.models if m.bin.id == "YW170")
    slow = next(m for m in st.models if m.bin.id == "YW575")
    assert fast.rate.per_day > 5 * slow.rate.per_day
    assert set(st.shapes) == {"glass", "underground"}


def test_the_backtest_compares_modelled_and_read_hours_in_and_out_of_sample():
    st = Study(fixture_data(), START, DAYS, TZ)
    rows = backtest(st)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["out_of_sample", "in_sample", "capacity"]
    for r in rows:
        assert r["measured"] > 0 and r["low"] <= r["modelled"] <= r["high"]
        assert r["error_pct"] == round(100 * (r["modelled"] - r["measured"]) / r["measured"], 1)
    assert len(rows[0]["rows"]) == DAYS - DAYS // 2
    assert abs(rows[1]["error_pct"]) < 60  # the fixture's ten bins, in sample


class FakeMemory:
    """The two reads the waste scenario makes, from the fixture."""

    def __init__(self):
        self.devices = [(b["id"], b["name"], b["lat"], b["lon"], json.dumps(b["metadata"]))
                        for b in FIX["bins"]]

    @contextmanager
    def reading(self, timeout=None):
        devices = self.devices

        class Con:
            def execute(self, sql, params=None):
                return type("R", (), {"fetchall": lambda _self: devices})()

        yield Con()

    def readings(self, devices, keys, start_ms, end_ms):
        return {b["id"]: [r for r in b["records"] if start_ms <= r["timestamp"] <= end_ms
                          and r["key"] in keys] for b in FIX["bins"]}


@pytest.fixture
def offline(monkeypatch):
    def down(*a, **k):
        raise WebError("offline")

    monkeypatch.setattr(rounds_mod, "get_json", down)


def waste_ctx(tmp_path):
    now = datetime(2026, 11, 26, 9, tzinfo=timezone.utc)
    return ScenarioContext(Settings(), memory=FakeMemory(), clock=lambda: now, cache_dir=tmp_path)


def test_a_run_gives_kpis_with_ranges_layers_series_and_a_backtest(tmp_path, offline):
    ctx = waste_ctx(tmp_path)
    period = Period.parse("last_30_days", ctx.now())
    res, params = run(WASTE, ctx, {"policy": "threshold", "threshold": 70}, period)
    k = {x["id"]: x for x in res.kpis}
    assert list(k)[0] == "full_hours" and WASTE.main_kpi == "full_hours"
    for x in res.kpis:
        assert x["low"] is not None and x["low"] <= x["scenario"] <= x["high"], x["id"]
    assert k["fuel"]["scenario"] == pytest.approx(k["truck_km"]["scenario"] * 0.7, abs=1)
    assert k["co2"]["scenario"] == pytest.approx(k["fuel"]["scenario"] * 365.25 / 7 * 2.68 / 1000, abs=0.1)
    assert k["fuel"]["low"] < k["fuel"]["scenario"] < k["fuel"]["high"]  # the diesel assumption
    assert k["low_lifts"]["scenario"] == 0  # nothing under 70 % is lifted
    assert {a["id"] for a in res.assumptions} == {"fill_rate", "truck_m3", "service_min",
                                                  "unload_min", "fuel_l_per_km", "co2_kg_per_l"}
    assert res.sensitivity[0]["id"] == "fill_rate"  # what moves hours full most
    assert res.layers["baseline"]["meta"]["variant"] == "baseline"
    assert res.layers["change"]["meta"]["variant"] == "scenario"
    feats = res.layers["change"]["features"]
    assert len(feats) == len(FIX["bins"]) and all({"id", "state", "color"} <= set(f["properties"])
                                                   for f in feats)
    assert next(f for f in feats if f["properties"]["id"] == "YW1056")["properties"]["state"] == "never_emptied"
    route = res.layers["route"]
    assert route["meta"]["geometry"] == "line" and route["meta"]["chat"] is True
    assert all(f["geometry"]["type"] == "LineString" for f in route["features"])
    assert {r["metric"] for r in res.series} >= {"full_hours", "collections", "truck_km", "fill_by_hour"}
    assert {c["metric"] for c in res.charts} == {r["metric"] for r in res.series}
    day = res.details["days"]["scenario"][0]
    assert day["trips"][0]["stops"][0]["eta"] >= "06:00"
    assert res.backtest["kind"] == "out_of_sample" and len(res.backtest["others"]) == 2
    assert any("estimated from straight lines" in n for n in res.notes)
    assert "Monday to Friday" in WASTE.label(params)


def test_policies_trade_hours_full_against_kilometres(tmp_path, offline):
    ctx = waste_ctx(tmp_path)
    data = WASTE.prepare(ctx, Period.parse("last_30_days", ctx.now()))
    p = WASTE.validated({"policy": "fixed", "rounds_per_week": 1})
    weekly = {x["id"]: x for x in WASTE.evaluate(data, p).kpis}
    p7 = WASTE.validated({"policy": "fixed", "rounds_per_week": 7})
    daily = {x["id"]: x for x in WASTE.evaluate(data, p7).kpis}
    assert daily["full_hours"]["scenario"] < weekly["full_hours"]["scenario"]
    assert daily["truck_km"]["scenario"] > weekly["truck_km"]["scenario"]
    assert daily["collections"]["scenario"] == pytest.approx(7 * weekly["collections"]["scenario"], rel=0.1)
    assert weekly["bigger_bins"]["scenario"] >= daily["bigger_bins"]["scenario"]
    # the baseline doesn't depend on the policy
    assert daily["full_hours"]["baseline"] == weekly["full_hours"]["baseline"]


def test_finish_draws_the_route_on_the_roads(tmp_path, offline, monkeypatch):
    ctx = waste_ctx(tmp_path)
    res, params = run(WASTE, ctx, {}, Period.parse("last_30_days", ctx.now()))
    monkeypatch.setattr(MatrixSource, "route_line",
                        lambda self, stops: [[lon + 0.0001, lat] for lat, lon in stops])
    WASTE.finish(ctx, res, params)
    f = res.layers["route"]["features"][0]
    assert f["properties"]["road"] is True and f["geometry"]["coordinates"][0][0] != f["properties"]["_stops"][0][1]


def test_too_short_a_memory_says_so(tmp_path, offline):
    ctx = waste_ctx(tmp_path)
    with pytest.raises(Exception, match="at least 7"):
        WASTE.prepare(ctx, Period.parse("last_7_days", datetime(2026, 11, 3, 9, tzinfo=timezone.utc)))


def test_the_lab_lists_and_runs_the_waste_scenario(tmp_path, offline):
    lab = Lab(waste_ctx(tmp_path))
    assert "waste" in [s.id for s in lab.scenarios()]
    r = lab.run("waste", {"policy": "forecast"}, "last_30_days")
    assert r["label"].startswith("Bins forecast past 90%") and r["result"]["kpis"][0]["id"] == "full_hours"
    assert not math.isnan(r["result"]["kpis"][0]["scenario"])

"""The street lighting scenario on a synthetic store: four lamps with known schedules and power,
so the inference, every policy, the KPIs and their ranges, the backtest, the footfall, the
safety check and the tool can be checked against numbers worked out by hand."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatbot.memory import Memory
from chatbot.scenarios import Lab, Period, ScenarioContext, ScenarioError
from chatbot.scenarios import lighting, lighting_model as lm
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from collector import db

from conftest import load

TZ = lm.TZ
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=TZ)
DAYS = 14

# id, name, lat, lon, watts, standby, reports every (min, None: hourly at night only), on and off
# minutes after sunset and sunrise, cost
LAMPS = [
    ("YL1", "NEMA 01 (S1)", 35.16880, 33.36630, 29.0, 0.6, 10, -20, 5, "0.2"),
    ("YL2", "Makariou 3", 35.16670, 33.36160, 54.0, 0.0, None, 10, -10, "0"),
    ("YL3", "Makariou 5", 35.16750, 33.36240, 82.0, 0.0, None, 10, -10, "0.32"),
    ("YL4", "Davilla 2", 35.16890, 33.36360, None, 0.0, None, 10, -10, "0.2"),
]
CAMERAS = [  # Makariou has a camera that counts people; Davilla none within reach
    {"analytics_id": 44, "name": "Makariou Avenue cam04", "lat": 35.16680, "lon": 33.36170,
     "lines": json.dumps([{"id": 0, "types": [0]}])},
    {"analytics_id": 52, "name": "Makariou cars only", "lat": 35.16680, "lon": 33.36170,
     "lines": json.dumps([{"id": 0, "types": [2]}])},
]


def _records(lamp):
    lid, _, _, _, watts, standby, every, on_m, off_m, _ = lamp
    start = (NOW - timedelta(days=DAYS + 1)).date()
    rows, energy = [], 1000.0
    last_t = None
    for k in range(DAYS + 1):
        n = lm.night(start + timedelta(days=k))
        a, b = n.sunset + on_m * 60, n.sunrise + off_m * 60
        if every:  # day and night, every `every` minutes
            t, stop, step = n.sunset - 6 * 3600, n.sunset + 18 * 3600, every * 60
        else:  # hourly while powered, the first report 20 minutes after switching on
            t, stop, step = a + 20 * 60, b, 3600
        while t < stop:
            if t >= NOW.timestamp():
                break
            lit = a <= t < b
            if last_t is not None and watts:
                lit_s = lm.overlap(last_t, t, a, b) + lm.overlap(last_t, t, a - 86400, b - 86400)
                energy += (watts * lit_s + standby * (t - last_t - lit_s)) / 3600
            last_t = t
            rows.append((t, "level", "100" if lit else "0"))
            if watts:
                rows.append((t, "powerstats", json.dumps({"last_pwr_act_avg": watts if lit else standby,
                                                          "last_energy_act": round(energy)})))
            t += step
    return rows


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("lighting") / "twin.duckdb"
    con = db.connect(path)
    for lamp in LAMPS:
        lid, name, lat, lon, *_, cost = lamp
        con.execute("INSERT INTO devices (name, type, lat, lon, display_name, metadata, in_inventory) "
                    "VALUES (?, 'Lighting', ?, ?, ?, ?, true)",
                    [lid, lat, lon, name, json.dumps({"extra": {"cost": cost}})])
        db.bulk(con, {"device": "VARCHAR", "key": "VARCHAR", "us": "BIGINT", "value": "VARCHAR"},
                [(lid, key, int(t * 1e6), value) for t, key, value in _records(lamp)],
                "INSERT OR REPLACE INTO telemetry SELECT device, key, make_timestamptz(us), value, "
                "NULL, 'test' FROM __SRC__")
    for c in CAMERAS:
        con.execute("INSERT INTO traffic_cameras (analytics_id, name, lat, lon, lines) VALUES (?, ?, ?, ?, ?)",
                    [c["analytics_id"], c["name"], c["lat"], c["lon"], c["lines"]])
    # The people-counting camera counted on each of the 30 days before today.
    midnight = datetime.combine(NOW.date(), datetime.min.time(), TZ)
    for k in range(1, 31):
        con.execute("INSERT INTO traffic_counts VALUES ('day', ?, 44, '', 0, 0, 0, 900, ?)",
                    [midnight - timedelta(days=k), midnight])
    con.close()
    return Memory(path, record=False)


class FakeCity:
    """The camera list and the footfall request (the hour-of-day fixture), counted."""

    def __init__(self):
        self.requests = 0
        self.client = self

    def now(self):
        return NOW.astimezone(timezone.utc)

    def traffic_cameras(self):
        return CAMERAS

    def get_json(self, endpoint, **params):
        assert endpoint == "getTrafficReports" and params["group_by"] == "hour"
        assert params["va_ids"] == "[44]"  # only the camera that counts people
        self.requests += 1
        return load("traffic_hour_profile_102.json")

    def each(self, fn, items):
        return [fn(x) for x in items]


STOP = SimpleNamespace(name="Makariou stop", lat=35.16672, lon=33.36163)  # about 4 m from YL2
REGISTER = {"id": "NIC-1", "address": "Makariou 5", "status": "dangerous", "lat": 35.16752, "lon": 33.36242}


def make_ctx(store, tmp_path, city=None):
    mobility = SimpleNamespace(timetable=lambda wait_s=0: SimpleNamespace(stops=[STOP]))
    registers = SimpleNamespace(records=lambda wait_s=0: [REGISTER])
    return ScenarioContext(Settings(), memory=store, city=city or FakeCity(), registers=registers,
                           mobility=mobility, clock=lambda: NOW.astimezone(timezone.utc),
                           cache_dir=tmp_path)


@pytest.fixture
def data(store, tmp_path):
    return lighting.prepare(make_ctx(store, tmp_path), Period.parse("last_14_days", NOW))


def params(**kw):
    return lighting.LIGHTING.validated(kw)


def kwh(res, field="kwh_scenario"):
    """A result's yearly kWh summed over its lamps (the KPIs round to 0.1 MWh)."""
    return sum(f["properties"][field] for f in res.layers["change"]["features"])


def test_street_names_and_matching():
    assert lighting.street_of("Makariou 13") == "Makariou"
    assert lighting.street_of("DAVILLA NEW 17") == "Davilla"
    assert lighting.street_of("NEMA 12S (S254)") is None
    assert lighting.street_of("Light Controller d035") is None
    assert lighting._same_street("Zina Kanther", "Zinas Kanther")
    assert lighting._same_street("Theofanous Theodotou", "Theophanous Theodotou")
    assert not lighting._same_street("Stasikratous", "Stasandrou")


def test_each_lamp_schedule_and_power_are_learnt(data):
    s1, s2, s3, s4 = (data.schedules[k] for k in ("YL1", "YL2", "YL3", "YL4"))
    # The 10-minute lamp: its switches are read directly, within 5 minutes.
    assert s1.basis == "measured" and abs(s1.on_min + 20) <= 6 and abs(s1.off_min - 5) <= 6
    assert s1.watts == 29.0 and s1.standby == 0.6
    # The hourly lamps: their counters say how long they were lit across each silent day.
    assert s2.on_basis == "meter" and s2.off_basis == "meter"
    assert abs((s2.on_min - s2.off_min) - 20) <= 2  # lit 20 minutes less than sunset to sunrise
    assert s2.watts == 54.0 and s2.standby == 0.0 and s3.watts == 82.0
    # A lamp that never reported its power can't be modelled.
    assert s4.watts is None and data.no_power == ["YL4"]
    assert data.city["lamps_on"] == 1 and abs(data.city["on_min"] + 20) <= 6


def test_the_backtest_replays_today_against_the_meters(data):
    bt = lighting.backtest(data)
    assert bt["lamps"] == 3 and bt["measured"] > 0
    assert abs(bt["error_pct"]) < 3 and abs(bt["holdout"]["error_pct"]) < 3
    rows = [r for r in lighting.evaluate(data, params()).series if r["metric"] == "backtest_kwh"]
    assert {r["variant"] for r in rows} == {"measured", "modelled"}


def test_todays_policy_changes_nothing(data):
    res = lighting.evaluate(data, params())
    energy, cost, co2, safety = res.kpis
    assert energy["delta"] == 0 and cost["delta"] == 0 and safety["scenario"] == 0
    # About (29 + 54 + 82) W for ~4,300 hours a year, plus YL1's standby.
    total = kwh(res, "kwh_base")
    assert 650 < total < 760 and energy["baseline"] == round(total / 1000, 1)
    assert cost["baseline"] == pytest.approx(total * 0.20, abs=1)
    assert co2["baseline"] == pytest.approx(total * 0.55 / 1000, abs=0.01)
    assert {f["properties"]["state"] for f in res.layers["change"]["features"]} == {"No change"}


def test_switching_by_the_sun(data):
    base = lighting.evaluate(data, params())
    later = lighting.evaluate(data, params(switch_by_sun=True, dusk_offset_min=0, dawn_offset_min=0))
    # Each lamp is then lit from civil dusk to civil dawn (about 50 minutes less than sunset to
    # sunrise) instead of its own schedule: YL1 loses about 80 minutes a night, YL2 and YL3 about 30.
    saved = kwh(base) - kwh(later)
    expect = 0.0
    for lid in ("YL1", "YL2", "YL3"):
        s = data.schedules[lid]
        lost_s = sum((n.sunrise + s.off_min * 60 - n.sunset - s.on_min * 60) - (n.dawn - n.dusk)
                     for n in data.year)
        expect += (s.watts - s.standby) * lost_s / 3600 / 1000
    assert 38 < expect < 48 and saved == pytest.approx(expect, rel=0.01)
    assert later.kpis[3]["scenario"] == 0  # twilight isn't counted as dimmed or dark


def test_dimming_saves_in_proportion_and_its_range_follows_the_power_floor(store, tmp_path):
    from chatbot.scenarios.base import run

    res, _ = run(lighting.LIGHTING, make_ctx(store, tmp_path),
                 {"dim": True, "dim_level": 50, "dim_from": "01:00", "dim_to": "05:00"},
                 Period.parse("last_14_days", NOW))
    energy, cost = res.kpis[0], res.kpis[1]
    # 4 hours a night at 55% of full power (10% kept, half of the rest).
    expect = 365 * 4 * (29 + 54 + 82) * 0.45 / 1000
    assert abs(kwh(res, "kwh_saved") - expect) / expect < 0.02
    # At 0% kept it saves half the power, at 25% kept 37.5%; with the price at 0.32 or 0.20
    # EUR, the cost saving spans both.
    assert cost["delta_low"] == pytest.approx(-365 * 4 * 165 * 0.5 * 0.32 / 1000, abs=1)
    assert cost["delta_high"] == pytest.approx(-365 * 4 * 165 * 0.375 * 0.20 / 1000, abs=1)
    assert energy["delta_low"] <= energy["delta"] <= energy["delta_high"]
    tornado = {s["id"]: s for s in res.sensitivity}
    assert res.sensitivity[0]["id"] == "price_eur_kwh"  # moves the headline most
    assert tornado["co2_kg_kwh"]["at_low"] == tornado["co2_kg_kwh"]["at_high"]
    # The Makariou camera counts more than 5 people an hour at night: the dimmed lamps there
    # are flagged, YL2 by its bus stop too and YL3 by its dangerous building.
    flagged = {r["id"]: r for r in res.details["safety"]}
    assert set(flagged) == {"YL2", "YL3"}
    assert flagged["YL2"]["reasons"] == ["Night pedestrians", "Bus stop"]
    assert flagged["YL3"]["reasons"] == ["Night pedestrians", "Dangerous building"]
    assert res.kpis[3]["scenario"] == pytest.approx(2 * 365 * 4, abs=2)
    assert res.details["unmeasured"] == ["YL1"]  # the NEMA lamp's street has no camera
    assert any(f["geometry"]["type"] == "LineString" for f in res.layers["safety"]["features"])


def test_part_night_only_darkens_quiet_counted_streets(data):
    quiet = lighting.evaluate(data, params(part_night=True, quiet_max_people=20))
    lamps = {f["properties"]["id"]: f["properties"] for f in quiet.layers["change"]["features"]}
    assert lamps["YL2"]["kwh_saved"] > 0 and lamps["YL3"]["kwh_saved"] > 0
    assert lamps["YL1"]["kwh_saved"] == 0  # no camera: it stays lit
    busy = lighting.evaluate(data, params(part_night=True, quiet_max_people=5))
    assert kwh(busy, "kwh_saved") == 0  # the camera counts 16 people an hour, 01:00 to 05:00
    both = lighting.evaluate(data, params(part_night=True, quiet_max_people=20, include_unmeasured=True))
    assert {f["properties"]["id"]: f["properties"] for f in both.layers["change"]["features"]}["YL1"]["kwh_saved"] > 0


def test_the_led_retrofit_and_its_payback(data):
    none = lighting.evaluate(data, params(retrofit=True))
    assert none.details["retrofit"] == [] and any("show none to replace" in n for n in none.notes)
    res = lighting.evaluate(data, params(retrofit=True, retrofit_min_w=80, led_power_pct=55, led_cost_eur=400))
    ids = {k["id"]: k for k in res.kpis}
    assert [r["id"] for r in res.details["retrofit"]] == ["YL3"]
    assert ids["investment"]["scenario"] == 400
    saved_kwh = kwh(res, "kwh_saved")
    assert saved_kwh == pytest.approx(82 * 0.45 * 4300 / 1000, rel=0.08)
    assert ids["payback"]["scenario"] == pytest.approx(400 / (saved_kwh * 0.20), rel=0.02)


def test_times_must_run_forward_through_the_night():
    with pytest.raises(ScenarioError, match="must end after it starts"):
        params(dim=True, dim_from="05:00", dim_to="01:00")
    assert params(dim=True, dim_from="23:00", dim_to="05:00")["dim_to"] == "05:00"
    assert lighting.LIGHTING.label(params(dim=True, switch_by_sun=True)) == \
        "By the sun (+0 min at dusk, +0 at dawn), Dim to 50% 01:00 to 05:00"


def test_footfall_is_one_request_per_counting_camera_and_kept_for_the_day(store, tmp_path):
    city = FakeCity()
    ctx = make_ctx(store, tmp_path, city)
    cams, note = lighting.footfall(ctx, NOW, lighting._camera_rows(ctx))
    assert note is None and city.requests == 1 and cams[0]["id"] == 44
    assert cams[0]["hours"][1] == round(939 / 30, 2)  # the fixture's 01:00 people over 30 days
    again, _ = lighting.footfall(ctx, NOW, lighting._camera_rows(ctx))
    assert city.requests == 1 and again[0]["hours"] == cams[0]["hours"]


def test_a_camera_down_most_of_the_month_measures_nothing_and_a_failure_isnt_kept(store, tmp_path):
    class Patchy(FakeCity):
        def __init__(self, fail=False, days=3):
            super().__init__()
            self.fail, self.days = fail, days

        def get_json(self, endpoint, **params):
            if params["group_by"] == "day":  # how many days it counted
                return {"data": {"group_by": [{"key": f"{d:02d}-11-2026", "data": {"0": {"total": 5}}}
                                              for d in range(1, self.days + 1)]}}
            if self.fail:
                raise RuntimeError("timeout")
            return super().get_json(endpoint, **params)

    other = ScenarioContext(Settings(), memory=None, city=Patchy(), clock=lambda: NOW, cache_dir=tmp_path)
    cams = [{"id": 44, "name": "cam", "lat": 35.1668, "lon": 33.3617}]
    got, note = lighting.footfall(other, NOW, cams)
    assert got == [] and note  # 3 days counted: too few to say a street is quiet
    failing = ScenarioContext(Settings(), memory=None, city=Patchy(fail=True, days=30), clock=lambda: NOW,
                              cache_dir=tmp_path / "f")
    assert lighting.footfall(failing, NOW, cams)[0] == []
    assert not list((tmp_path / "f").glob("footfall_*.json"))  # nothing kept: asked again next run


def test_without_the_memory_it_says_so(tmp_path):
    ctx = ScenarioContext(Settings(), memory=None, clock=lambda: NOW)
    with pytest.raises(ScenarioError, match="memory isn't available"):
        lighting.prepare(ctx, Period.parse("last_14_days", NOW))


def test_the_run_tool_returns_kpis_a_layer_and_the_series(store, tmp_path):
    lab = Lab(make_ctx(store, tmp_path))
    tctx = ToolContext(Settings(), None, None, None, SessionStore(), [].append, "c1", scenarios=lab)
    res, err = run_tool(tctx, "run_scenario", {"scenario": "lighting", "period": "last_14_days",
                                               "params": {"dim": True}})
    assert not err, res
    assert [k["id"] for k in res["kpis"]] == ["energy", "cost", "co2", "safety"]
    assert res["backtest"]["holdout"]["error_pct"] is not None
    layer = tctx.store.layers[res["layer_ref"]]
    assert {f["properties"]["id"] for f in layer["features"]} == {"YL1", "YL2", "YL3"}
    assert set(res["data_refs"]) == {"night_kw", "monthly_mwh", "backtest_kwh"}
    assert res["details"]["flagged_lamps"] == 2
    run_ = lab.get(res["run_id"])
    assert run_["label"] == "Dim to 50% 01:00 to 05:00" and run_["saved"]


def test_the_brief_prints_the_question_ranges_backtest_assumptions_and_sources(store, tmp_path):
    from chatbot.scenarios import brief

    lab = Lab(make_ctx(store, tmp_path))
    a = lab.run("lighting", {"dim": True}, "last_14_days")
    b = lab.run("lighting", {"switch_by_sun": True}, "last_14_days")
    page = brief.brief_html(a, b)
    assert lighting.LIGHTING.question in page
    assert "Dim to 50% 01:00 to 05:00" in page and "By the sun" in page
    assert "Grid emission factor" in page and "0.47 to 0.63" in page  # an assumption and its range
    assert "the sensors measured" in page and "Learnt only until" in page  # the backtest
    assert "What moves the result" in page and "<svg" in page  # the tornado
    assert "Safety check" in page and "Makariou" in page
    assert "CO2 per kWh of the Cyprus grid" in page  # a source
    assert "\u2014" not in page and "\u2013" not in page  # no em or en dashes
    assert brief.change_words({"delta": -2280, "delta_low": -3790, "delta_high": -2145, "digits": 0,
                               "unit": "EUR", "better": "lower", "scenario": 1}) == "saves 2,280 EUR (2,145 to 3,790)"
    csv = brief.series_csv(a).splitlines()
    assert csv[0] == "run,scenario,metric,time,variant,value" and len(csv) > 100

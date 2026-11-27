"""The island's grid: the operator's page read defensively and politely, recorded into the
twin's memory and replayed, the carbon estimate, the tool, the entity, the source probe, and the
measured carbon in the lamps' CO2 and the lighting scenario. Offline, on two captured pages:
the live page of Fri 27 Nov 2026 at 22:12 and the archive of 24 to 26 Nov."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chatbot import history
from chatbot.energy import Grid, carbon, plants, quarter_shares, stored
from chatbot.energy.tso import TZ, LayoutChanged, archive_url, parse_page
from chatbot.memory import Memory
from chatbot.settings import Settings
from chatbot.web import WebError

from conftest import load

LIVE = load("tso_generation_2026-11-27.html")
ARCHIVE = load("tso_archive_2026-11-24_3days.html")
FETCHED = datetime(2026, 11, 27, 22, 12, tzinfo=TZ)


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


class Feed:
    """Stands in for the operator's site: today's page and the archive, counting requests."""

    def __init__(self, live=LIVE, archive=ARCHIVE):
        self.live, self.archive = live, archive
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        body = self.archive if "archive" in url else self.live
        if isinstance(body, Exception):
            raise body
        return body


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t.astimezone(timezone.utc)


def make_grid(tmp_path, feed=None, clock=None, memory=None, record=True):
    return Grid(Settings(record=record), memory, cache_dir=tmp_path / "grid", fetch_text=feed or Feed(),
                clock=clock or Clock(FETCHED))


# -- the page ----------------------------------------------------------------------------------


def test_the_live_page_gives_every_published_quarter_hour_and_the_installed_capacity():
    page = parse_page(LIVE)
    assert len(page.readings) == 89 and page.pending == 7 and page.skipped == 0
    first, last = page.readings[0], page.latest
    assert first.t == local(2026, 11, 27, 0, 0) and first.demand_mw == 486
    assert last.t == local(2026, 11, 27, 22, 0)
    assert (last.demand_mw, last.wind_mw, last.distributed_mw, last.conventional_mw, last.capacity_mw) == (
        599, 21, 15, 563, 925)
    # 20:00, the evening the research probed: demand 682 MW.
    assert next(r for r in page.readings if r.t == local(2026, 11, 27, 20, 0)).demand_mw == 682
    assert page.installed == {"conventional_mw": 1478, "wind_mw": 167.1, "solar_mw": 1037.95, "biomass_mw": 12.4}
    assert set(page.columns) == {"time", "capacity_mw", "wind_mw", "distributed_mw", "demand_mw", "conventional_mw"}


def test_the_archive_gives_whole_days():
    page = parse_page(ARCHIVE)
    days = {r.t.astimezone(TZ).date().day for r in page.readings}
    assert len(page.readings) == 288 and days == {24, 25, 26}
    assert archive_url(local(2026, 11, 12).date(), 15).endswith("?startdt=12-11-2026&enddt=%2B15days")
    with pytest.raises(ValueError):
        archive_url(local(2026, 11, 12).date(), 5)


def _table(headers, rows, table_id="production_graph_data"):
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<html><body><table id='{table_id}'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>"


def test_just_after_midnight_the_table_is_empty_and_that_is_no_layout_change(tmp_path):
    page = parse_page(load("tso_generation_2026-11-28_0032.html"))
    assert page.readings == [] and page.pending == 0 and "demand_mw" in page.columns
    feed, clock = Feed(), Clock(FETCHED)
    grid = make_grid(tmp_path, feed, clock, record=False)
    grid.refresh()
    feed.live = load("tso_generation_2026-11-28_0032.html")
    clock.t = local(2026, 11, 28, 0, 32)
    assert grid.refresh() and not grid.layout_changed and grid.error is None
    # Yesterday's last row stands until today's first comes; 22:00 is too old to call live.
    snap = grid.snapshot()
    assert snap["now"]["t"] == "2026-11-27T22:00+02:00"
    assert snap["day_span"] == "last_24_hours" and snap["day"][-1]["t"] == "2026-11-27T22:00+02:00"
    assert snap["status"] == "stale" and snap["error"] is None


def test_columns_are_found_by_meaning_in_any_order_and_language_and_under_a_new_id():
    html = _table(["Total Demand", "Conventional Generation", "Timestamp", "Wind Generation",
                   "Estimated Distributed Generation", "Total Available Capacity of Conventional Generation"],
                  [["700", "640", "2026-11-27 18:00:00", "40", "20", "925"]], table_id="renamed")
    r = parse_page(html).latest
    assert (r.demand_mw, r.conventional_mw, r.wind_mw, r.distributed_mw, r.capacity_mw) == (700, 640, 40, 20, 925)


def test_a_changed_layout_is_said_not_guessed():
    with pytest.raises(LayoutChanged):
        parse_page("<html><body><p>Maintenance</p></body></html>")
    with pytest.raises(LayoutChanged, match="demand"):
        parse_page(_table(["Timestamp", "Wind"], [["2026-11-27 18:00:00", "40"]]))
    # An impossible value is unknown, the rest of the row still read.
    page = parse_page(_table(["Timestamp", "Συνολική Ζήτηση", "Αιολική Παραγωγή"],
                             [["2026-11-27 18:00:00", "70000", "12"], ["2026-11-27 18:15:00", "650", "x"]]))
    assert [r.demand_mw for r in page.readings] == [650] and page.readings[0].wind_mw is None


def test_the_clock_changes_are_read_as_instants():
    # 25 Oct 2026: clocks go back at 04:00, so 03:00 to 03:45 come twice; 29 Mar: 03:00 to 03:45 never happen.
    back = [[f"2026-10-25 03:{m:02d}:00", "500"] for m in (0, 15, 30, 45)] * 2
    page = parse_page(_table(["Timestamp", "Demand"], back))
    times = [r.t for r in page.readings]
    assert len(set(times)) == 8 and times[4] - times[0] == timedelta(hours=1)
    forward = [["2026-03-29 02:45:00", "500"], ["2026-03-29 03:00:00", "500"], ["2026-03-29 04:00:00", "500"]]
    page = parse_page(_table(["Timestamp", "Demand"], forward))
    assert len(page.readings) == 2 and page.skipped == 1


def test_carbon_is_the_oil_fired_share_times_the_stations_factor():
    assert carbon.intensity(563, 599) == pytest.approx(563 / 599 * 0.70)
    assert carbon.intensity(None, 599) is None and carbon.intensity(500, 0) is None
    assert carbon.renewable_share(21, 15, 599) == pytest.approx(36 / 599)
    assert "estimate" in carbon.METHOD.lower() and "—" not in carbon.METHOD


# -- reading it politely -----------------------------------------------------------------------


def test_the_page_is_read_at_most_once_per_publication(tmp_path):
    feed, clock = Feed(), Clock(FETCHED)
    grid = make_grid(tmp_path, feed, clock, record=False)
    assert grid.refresh() and len(feed.calls) == 1
    clock.t = FETCHED + timedelta(minutes=5)
    assert not grid.refresh() and len(feed.calls) == 1  # not 15 minutes yet
    clock.t = local(2026, 11, 27, 22, 26)
    assert not grid.refresh() and len(feed.calls) == 1  # the 22:15 row can't be out yet
    clock.t = local(2026, 11, 27, 22, 28)
    assert grid.refresh() and len(feed.calls) == 2
    snap = grid.snapshot()
    assert snap["status"] == "live" and len(feed.calls) == 2  # a snapshot reads only when due


def test_a_failure_backs_off_and_a_changed_page_keeps_the_last_good_copy(tmp_path):
    feed, clock = Feed(), Clock(FETCHED)
    grid = make_grid(tmp_path, feed, clock, record=False)
    grid.refresh()
    feed.live = "<html><body>New theme</body></html>"
    clock.t = local(2026, 11, 27, 22, 30)
    assert not grid.refresh() and grid.layout_changed and len(feed.calls) == 2
    snap = grid.snapshot()
    assert snap["status"] == "stale" and "layout" in snap["error"]
    assert snap["now"]["demand_mw"] == 599 and snap["now"]["age_min"] == 30
    clock.t = local(2026, 11, 27, 22, 50)  # backing off: 30 minutes after a failure
    grid.refresh()
    assert len(feed.calls) == 2
    feed.live = WebError("tsoc.org.cy unreachable: timed out")
    clock.t = local(2026, 11, 27, 23, 1)
    grid.refresh()
    assert len(feed.calls) == 3 and "unreachable" in grid.error


def test_a_restart_reads_the_copy_on_disk_not_the_page(tmp_path):
    make_grid(tmp_path, record=False).refresh()
    feed = Feed()
    again = make_grid(tmp_path, feed, Clock(FETCHED + timedelta(minutes=3)), record=False)
    snap = again.snapshot()
    assert feed.calls == [] and snap["now"]["demand_mw"] == 599 and snap["installed"]["wind_mw"] == 167.1


def test_with_nothing_read_the_grid_is_unavailable(tmp_path):
    feed = Feed(live=WebError("tsoc.org.cy answered HTTP 503"))
    snap = make_grid(tmp_path, feed, record=False).snapshot()
    assert snap["status"] == "unavailable" and snap["now"] is None and "503" in snap["error"]


# -- the memory and the clock ------------------------------------------------------------------


@pytest.fixture
def recorded(tmp_path):
    """A memory holding the three archived days and today up to 22:00, as the twin wrote them."""
    mem = Memory(tmp_path / "twin.duckdb", record=False)
    grid = make_grid(tmp_path, memory=mem)
    assert grid.backfill_if_needed() == 288
    grid.refresh()
    assert grid.flush()
    return mem, grid


def test_every_row_read_goes_into_the_memory_and_the_clock_replays_it(recorded, tmp_path):
    mem, grid = recorded
    rows = stored(mem, local(2026, 11, 24), local(2026, 11, 28))
    assert len(rows) == 288 + 89
    # A fresh twin with no copy on disk answers a past moment from the memory alone.
    feed = Feed()
    other = Grid(Settings(), mem, cache_dir=None, fetch_text=feed, clock=Clock(FETCHED))
    then = other.snapshot(local(2026, 11, 26, 19, 7))
    assert feed.calls == [] and then["status"] == "known" and not then["live"]
    assert then["now"]["t"] == "2026-11-26T19:00+02:00" and then["now"]["demand_mw"] > 0
    assert then["day"][0]["t"] == "2026-11-26T00:00+02:00" and then["day"][-1]["t"] == "2026-11-26T19:00+02:00"
    assert then["peak"]["demand_mw"] == max(p["demand_mw"] for p in then["day"])
    gone = other.snapshot(local(2026, 11, 10, 12, 0))
    assert gone["status"] == "unknown" and gone["now"] is None and "no record" in gone["reason"]


def test_the_back_fill_runs_once_a_day_and_only_when_the_memory_lacks_it(recorded, tmp_path):
    mem, grid = recorded
    feed = Feed()
    grid.fetch_text = feed
    assert grid.backfill_if_needed() == 0 and feed.calls == []  # the marker: done today
    assert make_grid(tmp_path, record=False).backfill_if_needed() == 0  # recording off: never


def test_the_usual_day_builds_up_from_the_weekdays_recorded(recorded):
    _, grid = recorded
    usual, days = grid.usual(local(2026, 11, 27, 22, 0))
    assert days == 3  # Tue to Thu; the weekend would be compared with weekends
    nine = next(u for u in usual if u["t"] == "21:00")
    past = [r.demand_mw for r in grid.series(local(2026, 11, 24), local(2026, 11, 26, 23, 59))
            if r.t.astimezone(TZ).strftime("%H:%M") == "21:00"]
    assert nine["demand_mw"] == round(sorted(past)[1])
    assert grid.usual(local(2026, 11, 28, 12, 0))[1] == 0  # a Saturday: no weekend recorded yet


def test_the_carbon_of_each_moment_for_the_lamps(recorded):
    _, grid = recorded
    at = [local(2026, 11, 27, 22, 0), local(2026, 11, 27, 22, 10), local(2026, 11, 20, 3, 0)]
    c = grid.carbon_at(at)
    assert c[0] == pytest.approx(563 / 599 * 0.70) and c[1] == c[0] and c[2] is None


def test_the_night_shares_the_lighting_scenario_weights_with(recorded):
    mem, _ = recorded
    shares, days = quarter_shares(mem, local(2026, 11, 24), local(2026, 11, 27, 23, 59))
    assert days == 4 and 0.9 < shares[0] <= 1.0  # midnight: almost all oil-fired
    assert shares[12 * 60] < shares[0]  # noon: the sun is up
    from chatbot.scenarios.lighting import Data

    data = SimpleNamespace(grid_days=days, grid_share=shares)
    slots = [local(2026, 11, 26, 18).timestamp() + 900 * k for k in range(4 * 12)]
    share, measured = Data.oil_share(data, slots, [100.0] * len(slots))
    assert measured and 0.9 < share < 1.0
    share, measured = Data.oil_share(SimpleNamespace(grid_days=0, grid_share={}), slots, [1.0] * len(slots))
    assert not measured and share == carbon.YEARLY_FOSSIL_SHARE


def test_the_lamps_co2_metric_weights_each_moment_and_never_guesses(monkeypatch):
    metric = history.METRICS["street_light"]["co2_kg"]
    times = [local(2026, 11, 27, 20, 0), local(2026, 11, 27, 20, 15), local(2026, 11, 27, 12, 0)]
    states = [["on"], ["on"], ["off"]]
    values = {"power_w": [[100.0], [100.0], [0.0]]}
    monkeypatch.setitem(history.FACTORS, "grid_co2", lambda ts: [0.6, None, None])
    s = history.device_series(metric, times, 0.25, ["YL1"], states, values)
    # 0.1 kW for a quarter hour at 0.6 kg a kWh; no grid record is unknown; a dark lamp emits nothing.
    assert s.values[0] == [pytest.approx(0.1 * 0.25 * 0.6), None, 0.0]
    assert "tso_grid" in metric.sources and "estimate" in metric.note


# -- what people and the model see --------------------------------------------------------------


def test_the_tool_charts_what_met_demand_and_maps_where_it_is_made(recorded):
    from chatbot.store import SessionStore
    from chatbot.tools import ToolContext, run_tool

    _, grid = recorded
    city = SimpleNamespace(now=lambda: FETCHED.astimezone(timezone.utc))
    ctx = ToolContext(Settings(), city, None, None, SessionStore(), [].append, "c1",
                      entities=SimpleNamespace(grid=grid))
    res, err = run_tool(ctx, "get_grid", {})
    assert not err, res
    assert res["now"]["demand_mw"] == 599 and res["now"]["renewable_pct"] == pytest.approx(6.0)
    assert res["usual_days"] == 3 and res["usual_demand_mw_at_this_time"] > 0
    assert "not Nicosia" in res["where"] and "Never draw" in res["map_hint"]
    mix = ctx.store.datasets[res["data_ref"]]
    assert {r["source"] for r in mix["rows"]} == {"Oil-fired stations", "Wind", "Solar and small plants, estimated"}
    assert len(mix["rows"]) == 89 * 3 and "tso_grid" in mix["sources"]
    layer = ctx.store.layers[res["layer_ref"]]
    names = {f["properties"]["label"] for f in layer["features"]}
    assert {"Vasilikos Power Station", "Dhekelia Power Station", "Moni Power Station"} <= names
    assert any("563 MW now from all oil-fired stations together" in f["properties"]["popup"] for f in layer["features"])
    week, err = run_tool(ctx, "get_grid", {"at": "2026-11-26T21:00", "days": 3})
    assert not err and week["then"]["demand_mw"] > 0 and week["range"]["quarter_hours"] == 3 * 96 - 11
    _, err = run_tool(ctx, "get_grid", {"at": "2026-11-01T21:00"})
    assert err


def test_just_after_midnight_the_tool_charts_the_last_24_hours(recorded):
    from chatbot.store import SessionStore
    from chatbot.tools import ToolContext, run_tool

    _, grid = recorded
    grid.clock = Clock(local(2026, 11, 28, 0, 40))
    grid.fetch_text = Feed(live=load("tso_generation_2026-11-28_0032.html"))
    city = SimpleNamespace(now=grid.clock)
    ctx = ToolContext(Settings(), city, None, None, SessionStore(), [].append, "c1",
                      entities=SimpleNamespace(grid=grid))
    res, err = run_tool(ctx, "get_grid", {})
    assert not err, res
    assert res["peak_and_cleanest_over"] == "the last 24 hours" and res["range"]["quarter_hours"] > 80
    assert res["now"]["t"] == "2026-11-27T22:00+02:00"


def test_the_grid_is_an_entity_with_a_series_and_a_moment(recorded):
    from chatbot.entities import Entities

    _, grid = recorded
    ents = Entities(None)
    ents.grid = grid
    e = ents.get("grid-cyprus")
    assert e["kind"] == "power_grid" and e["geometry"] is None and e["state"]["value"] == "live"
    assert e["state"]["label"] == "599 MW, 6% from wind and sun"
    readings = {r["label"]: r["value"] for r in e["readings"]}
    assert readings["Oil-fired stations"] == 563 and readings["Carbon, estimated"] == 658  # g CO2 per kWh
    s = ents.series("grid-cyprus")
    assert s["unit"] == "MW" and len(s["points"]) >= 90
    from chatbot.energy.entities import grid_at

    then = grid_at(ents, e, local(2026, 11, 25, 8, 0))
    assert then["moment"]["known"] and then["grid"]["now"]["t"] == "2026-11-25T08:00+02:00"
    assert any(i["id"] == "grid-cyprus" for i in ents.search("electricity"))


def test_the_sources_panel_says_how_fresh_the_grid_is(tmp_path):
    from chatbot.routes.sources import _grid
    from chatbot.sources import BY_ID, TOOL_SOURCES

    feed = Feed()
    grid = make_grid(tmp_path, feed, record=False)
    agent = SimpleNamespace(grid=grid)
    assert _grid(agent)["state"] == "waiting"
    grid.refresh()
    st = _grid(agent)
    assert st["state"] == "live" and "22:00" in st["note"]
    feed.live = "<html><body>gone</body></html>"
    grid.refresh(force=True)
    assert _grid(agent)["state"] == "stale" and _grid(agent)["layout_changed"]
    src = BY_ID["tso_grid"]
    assert "No licence stated" in src.licence and src.group == "Energy" and src.credit
    assert set(TOOL_SOURCES["get_grid"]) >= {"tso_grid", "oil_co2"}


def test_the_plants_are_the_oil_fired_stations_and_wind_farms():
    ps = plants.load()
    kinds = {p["kind"] for p in ps}
    assert kinds == {"oil", "wind"}
    oil = sum(p["capacity_mw"] for p in ps if p["kind"] == "oil")
    assert oil == 1478  # exactly the operator's installed conventional capacity
    assert "not drawn" in plants.SOLAR_NOTE

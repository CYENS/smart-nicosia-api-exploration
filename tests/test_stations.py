"""Measured weather and reference air (chatbot/environment/stations.py), offline: the Met
Department's XML and the EEA's hourly Parquet files as fetched on 27 November 2026."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chatbot import entities as E
from chatbot import sources
from chatbot.environment import station_kinds  # noqa: F401 - registers the two kinds
from chatbot.environment import stations as S
from chatbot.environment.service import Environment
from chatbot.environment.surfaces import METRICS, Field, Station
from chatbot.environment.weather import ModelGrids
from chatbot.memory import Memory
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools.base import ToolContext
from chatbot.tools.registry import run_tool

FIX = Path(__file__).parent / "fixtures" / "stations"
TZ = ZoneInfo("Asia/Nicosia")
NOW = datetime(2026, 11, 27, 22, 5, tzinfo=TZ)  # five minutes after the Met file's readings
FILES = sorted(p.stem for p in FIX.glob("SPO-*.parquet"))


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


class FakeHttp:
    """The Met site, the EEA listing and its blobs, from the fixtures, with ETags."""

    def __init__(self, fail: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or set()
        self.met = (FIX / "met_cydom.xml").read_bytes()

    def __call__(self, url, *, user_agent, headers=None, data=None, timeout=30.0):
        headers = headers or {}
        self.calls.append((url, headers))
        assert "smart-nicosia" in user_agent
        if any(f in url for f in self.fail):
            raise RuntimeError("the host answered HTTP 503")
        if url == S.MET_URL:
            body, tag = self.met, '"met-1"'
        elif url == S.EEA_LIST_URL:
            assert b'"Lefkosia"' in data
            return S.Response(200, b"\xef\xbb\xbf" + (FIX / "eea_urls_cy.csv").read_bytes(), {})
        else:
            name = url.rsplit("/", 1)[-1]
            body, tag = (FIX / name).read_bytes(), f'"{name}"'
        if headers.get("If-None-Match") == tag:
            return S.Response(304, b"", {})
        return S.Response(200, body, {"etag": tag})

    def count(self, part: str) -> int:
        return sum(1 for u, _ in self.calls if part in u)


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "twin.duckdb")


@pytest.fixture
def measured(tmp_path, memory):
    http = FakeHttp()
    m = S.Measured(Settings(), memory, http_get=http, cache_dir=tmp_path / "cache", now=lambda: NOW)
    m.http = http
    return m


# -- parsing -------------------------------------------------------------------------------------


def test_the_met_file_gives_the_five_stations_around_nicosia_in_metres_per_second():
    stations, readings = S.parse_met((FIX / "met_cydom.xml").read_bytes())
    assert sorted(stations) == ["ATHALASSA", "KEPNIC", "LEFKOSIA", "MAMMARI", "VISITOR"]
    assert stations["LEFKOSIA"].name == "Lefkosia" and stations["LEFKOSIA"].kind == "weather_station"
    by = {(sid, k): (ts, v) for sid, k, ts, v in readings}
    ts, temp = by[("met-LEFKOSIA", "temperature_c")]
    assert temp == 12.1 and ts == ms(datetime(2026, 11, 27, 22, 0, tzinfo=TZ))
    assert by[("met-ATHALASSA", "wind_ms")][1] == pytest.approx(1.8 * 0.514444, abs=0.01)  # knots
    assert by[("met-ATHALASSA", "wind_2m_ms")][1] == 0.2  # already m/s
    assert by[("met-MAMMARI", "radiation_wm2")][1] == 4.0
    assert {k for sid, k in by if sid == "met-KEPNIC"} == {"rain_10min_mm"}  # a rain gauge only
    assert not any(sid == "met-ACHNA" for sid, _ in by)  # 40 km off


def test_a_broken_reading_is_left_out_not_guessed():
    xml = """<meteorology><stations><station><station_code>LEFKOSIA</station_code>
      <station_latitude>35.1643667</station_latitude><station_longitude>33.3560722</station_longitude>
      </station></stations><observations><station_code>LEFKOSIA</station_code>
      <date_time>2026-11-27 22:00 (Local Time)</date_time>
      <observation><observation_name>Air Temperature (1.2m)</observation_name>
        <observation_value>n/a</observation_value><observation_unit>Celcius</observation_unit></observation>
      <observation><observation_name>Relative Humidity (1.2m)</observation_name>
        <observation_value>81</observation_value><observation_unit>%</observation_unit></observation>
      <observation><observation_name>Something new</observation_name>
        <observation_value>3</observation_value><observation_unit>?</observation_unit></observation>
      </observations></meteorology>"""
    _, readings = S.parse_met(xml)
    assert [(k, v) for _, k, _, v in readings] == [("humidity_pct", 81.0)]
    assert S.met_stamp("2026-11-27 22:00 (Local Time)") == datetime(2026, 11, 27, 22, 0, tzinfo=TZ)
    assert S.met_stamp("soon") is None


def test_the_eea_listing_keeps_the_nicosia_files_the_twin_reads():
    text = "﻿" + (FIX / "eea_urls_cy.csv").read_text(encoding="utf-8")
    assert S.parse_eea_listing(text) == FILES  # NO2 and O3 at both, PM10 at the traffic station
    assert S.file_station("SPO-CY0004A_00008_100")[0].name == "Nicosia traffic station"
    assert S.file_station("SPO-CY0002R_00008_100") is None  # Agia Marina, outside Nicosia


def test_an_eea_file_gives_valid_hours_at_their_start_in_nicosia_time():
    rows = S.parse_eea_parquet(FIX / "SPO-CY0004A_00008_100.parquet")
    sid, key, ts, value = rows[-1]
    assert (sid, key, value) == ("aq-CY0004A", "no2", 86.4)
    # 18:00 to 19:00 in UTC+1 is 19:00 to 20:00 in Nicosia.
    assert datetime.fromtimestamp(ts / 1000, tz=TZ) == datetime(2026, 11, 27, 19, 0, tzinfo=TZ)
    hours = {r[2] for r in rows}
    assert ms(datetime(2026, 11, 27, 14, 0, tzinfo=TZ)) not in hours  # flagged not valid (-1)
    since = datetime(2026, 11, 27, tzinfo=TZ)
    assert all(r[2] >= ms(since) for r in S.parse_eea_parquet(FIX / "SPO-CY0004A_00008_100.parquet", since))


# -- reading, recording, replaying -----------------------------------------------------------------


def test_the_met_file_is_read_once_until_it_changes_and_kept_in_the_memory(measured, memory):
    assert measured.refresh_met() > 20
    assert measured.refresh_met() == 0  # If-None-Match: 304, nothing downloaded again
    assert measured.http.calls[-1][1]["If-None-Match"] == '"met-1"'
    assert (measured.cache / "met.xml").exists()
    now = {r["id"]: r for r in measured.readings_at(None)}
    lef = now["met-LEFKOSIA"]
    assert lef["known"] and lef["readings"]["temperature_c"]["value"] == 12.1
    assert lef["readings"]["temperature_c"]["age_min"] == 5 and lef["readings"]["temperature_c"]["unit"] == "°C"
    # Recorded: the clock replays it, and a moment the memory holds nothing for is unknown.
    at = datetime(2026, 11, 27, 22, 20, tzinfo=TZ)
    then = {r["id"]: r for r in measured.readings_at(at)}
    assert then["met-LEFKOSIA"]["readings"]["temperature_c"]["value"] == 12.1
    later = {r["id"]: r for r in measured.readings_at(datetime(2026, 11, 27, 23, 30, tzinfo=TZ))}
    assert not later["met-LEFKOSIA"]["known"]
    with memory.reading() as con:
        n = con.execute("SELECT count(*) FROM station_readings WHERE source = 'met'").fetchone()[0]
    assert n > 20


def test_the_eea_files_fill_the_memory_with_their_weeks_and_say_their_hour(measured):
    assert measured.refresh_eea() > 500
    assert measured.http.count("ParquetFile/urls") == 1 and measured.eea_status.files == len(FILES)
    assert measured.refresh_eea() == 0  # every file unchanged
    rows = {r["id"]: r for r in measured.readings_at(None)}
    no2 = rows["aq-CY0004A"]["readings"]["no2"]
    assert no2["value"] == 86.4 and no2["as_of"] == "2026-11-27T19:00" and no2["until"] == "2026-11-27T20:00"
    assert no2["age_min"] == 125  # from the end of its hour
    assert "pm10" in rows["aq-CY0004A"]["readings"] and "pm10" not in rows["aq-CY0006A"]["readings"]
    # A past moment reads the hour then, from the memory.
    noon = {r["id"]: r for r in measured.readings_at(datetime(2026, 11, 25, 12, 30, tzinfo=TZ))}
    assert noon["aq-CY0004A"]["readings"]["no2"]["as_of"] == "2026-11-25T12:00"


def test_a_feed_that_fails_says_so_and_keeps_what_it_had(tmp_path, memory):
    http = FakeHttp(fail={"dom.org.cy"})
    m = S.Measured(Settings(), memory, http_get=http, cache_dir=tmp_path / "c", now=lambda: NOW)
    assert m.refresh_met() == 0 and "503" in m.met_status.error
    assert not any(r["known"] for r in m.readings_at(None, "weather_station"))
    assert {s.code for s in m.stations("weather_station")} >= {"LEFKOSIA", "MAMMARI"}  # where they stand


def test_the_copies_on_disk_show_the_stations_at_once_after_a_restart(measured, tmp_path):
    measured.refresh_met()
    again = S.Measured(Settings(), None, http_get=FakeHttp(), cache_dir=measured.cache, now=lambda: NOW)
    again.load_cached()
    assert {r["id"]: r for r in again.readings_at(None)}["met-LEFKOSIA"]["known"]


def test_the_summary_names_the_centre_thermometer_and_the_traffic_station(measured):
    measured.refresh_met()
    measured.refresh_eea()
    s = measured.summary(None)
    assert s["temperature"]["station"] == "Lefkosia" and s["temperature"]["value_c"] == 12.1
    assert s["wind"]["station"] == "Athalassa" and s["sunshine"]["station"] == "Mammari"
    assert s["air"][0]["station"] == "Nicosia traffic station" and s["air"][0]["no2"]["hour"] == "19:00 to 20:00"
    assert s["rain_last_hour"]["mm"] == 0.0


# -- the city's sensors against the reference ---------------------------------------------------------


def add_city_sensor(memory, name, rows):
    """A city air sensor's hourly readings, as the recorder keeps them."""
    recs = [{"key": key, "timestamp": ts, "value": str(v)} for key, ts, v in rows]
    memory.recorder.readings("smartsense", {name: recs}, [(name, rows[0][1], rows[-1][1])])
    assert memory.recorder.flush()


SENSOR = ("f60f85f2babb", "Centre", 35.1682, 33.3713)


def test_a_city_sensor_reading_a_fifth_high_is_told_so(measured, memory):
    measured.refresh_eea()
    measured.refresh_met()
    ref = measured.stored(["aq-CY0004A"], ["no2"], NOW - timedelta(days=7), NOW)
    add_city_sensor(memory, SENSOR[0], [("NO2", ts, v * 1.2) for _, _, ts, v in ref]
                    + [("T", ms(datetime(2026, 11, 27, 22, 0, tzinfo=TZ)), 14.6)])
    rows = measured.compare([SENSOR], ("no2", "temperature_c"))
    no2 = next(r for r in rows if r["metric"] == "no2")
    # 2.8 km from the traffic station, 5.8 km from the residential one: the nearest is compared.
    assert no2["reference_id"] == "aq-CY0004A" and 2500 < no2["distance_m"] < 3000
    assert no2["bias_pct"] == 20 and no2["r"] == 1.0 and no2["hours"] == len(ref)
    assert no2["text"].startswith("reads 20% above the traffic station over 7 days")
    temp = next(r for r in rows if r["metric"] == "temperature_c")
    assert temp["reference"] == "Lefkosia" and temp["hours"] == 1 and "too few" in temp["note"]
    station, pairs = measured.pairs(SENSOR, "no2")
    assert station.id == "aq-CY0004A" and len(pairs) == len(ref)


def test_the_comparison_pairs_hour_by_hour(measured, memory):
    measured.refresh_eea()
    ref = measured.stored(["aq-CY0004A"], ["no2"], NOW - timedelta(days=7), NOW)
    near_traffic = ("s1", "By the traffic station", 35.1525, 33.3480)
    add_city_sensor(memory, "s1", [("NO2", ts + 20 * 60_000, v * 0.5) for _, _, ts, v in ref])
    c = next(r for r in measured.compare([near_traffic], ("no2",)) if r["metric"] == "no2")
    assert c["reference_id"] == "aq-CY0004A" and c["distance_m"] < 100
    assert c["bias_pct"] == -50 and c["hours"] == len(ref) and "reads 50% below the traffic station" in c["text"]


# -- the surfaces rest on the reference ------------------------------------------------------------


def air_model(v):
    from chatbot.environment.weather import AIR_LATS, AIR_LONS
    n = len(AIR_LATS) * len(AIR_LONS)
    return {"points": [], "time": "2026-11-27T19:00", "pm2_5": [v] * n, "pm10": [v] * n,
            "nitrogen_dioxide": [v] * n, "ozone": [v] * n}


def test_a_reference_station_sets_the_models_miss_and_is_surer_than_a_city_sensor():
    city = Station("c1", "City", 35.172, 33.355, 60.0)
    ref = Station("aq-CY0004A", "Traffic", 35.152, 33.348, 40.0, reference=True, error=4.0)
    f = Field(METRICS["no2"], [city, ref], air_model(20.0))
    assert f.bias == pytest.approx(20.0)  # the reference's miss alone, not the mean with the city's
    at_ref = f.at(ref.lat, ref.lon)
    at_city = f.at(city.lat, city.lon)
    assert at_ref["value"] == pytest.approx(40.0, abs=0.5) and at_ref["nearest"]["reference"]
    assert at_ref["uncertainty"] < at_city["uncertainty"]
    far = f.at(35.25, 33.22)
    assert far["uncertainty"] > at_ref["uncertainty"]


class FakeCity:
    def __init__(self, now=NOW):
        self._now = now

    def now(self):
        return self._now

    def states(self, type_):
        return [SimpleNamespace(device=SimpleNamespace(name=SENSOR[0], lat=SENSOR[2], lon=SENSOR[3]),
                                label="Air station Centre",
                                interp=SimpleNamespace(detail={"no2": 50.0, "temperature_c": 15.0}, stale=False,
                                                       state="ok", as_of=self._now))]

    def devices_of(self, type_):
        return [SimpleNamespace(name=SENSOR[0], lat=SENSOR[2], lon=SENSOR[3])]

    def cached_label(self, d):
        return "Air station Centre", None


def open_meteo(url, user_agent=None, params=None, timeout=None):
    n = len(params["latitude"].split(","))
    day = params["start_date"]
    times = [f"{day}T{h:02d}:00" for h in range(24)]
    series = {"pm2_5": 12.0, "pm10": 24.0, "nitrogen_dioxide": 20.0, "ozone": 60.0, "temperature_2m": 14.0,
              "wind_speed_10m": 4.0, "wind_direction_10m": 45.0, "wind_gusts_10m": 8.0}
    return [{"hourly": {"time": times, **{k: [v] * 24 for k, v in series.items() if k in params["hourly"].split(",")}}}
            for _ in range(n)]


@pytest.fixture
def env(measured, memory):
    s = Settings()
    city = FakeCity()
    e = Environment(s, city=city, memory=memory, models=ModelGrids(s, fetch=open_meteo, now=city.now),
                    fetch=open_meteo, measured=measured)
    measured.refresh_met()
    measured.refresh_eea()
    return e


def test_the_temperature_surface_rests_on_the_met_stations(env):
    surf = env.surface("temperature_c", None)
    refs = [s for s in surf["stations"] if s["reference"]]
    assert {s["name"] for s in refs} >= {"Lefkosia", "Athalassa"} and surf["rests_on"]["reference"]
    # The city sensor has no day beside a Met station yet: it is left out rather than trusted.
    assert surf["rests_on"]["city_sensors"] == 0 and any("left out" in n for n in surf["notes"])
    mean = sum(s["value"] for s in refs) / len(refs)
    assert surf["model_bias"] == pytest.approx(mean - 14.0, abs=0.05)  # the thermometers' miss alone
    assert surf["sources"][-1]["name"] == "Department of Meteorology stations"


def test_an_air_surface_now_says_the_reference_hour_is_too_old_and_rests_on_it_then(env):
    now = env.surface("no2", None)
    assert not now["rests_on"]["reference"] and now["rests_on"]["left_out"]
    assert any("reference stations aren't used" in n for n in now["notes"])
    then = env.surface("no2", datetime(2026, 11, 27, 19, 30, tzinfo=TZ))
    assert {r["id"] for r in then["rests_on"]["reference"]} == {"aq-CY0004A", "aq-CY0006A"}
    traffic = next(s for s in then["stations"] if s["id"] == "aq-CY0004A")
    assert traffic["value"] == 86.4 and traffic["reference"] and traffic["as_of"] == "2026-11-27T19:00"


# -- the entities ----------------------------------------------------------------------------------


@pytest.fixture
def ents(env):
    e = E.Entities(None)
    e.environment = env
    return e


def test_a_weather_station_opens_with_its_readings_and_its_day(ents):
    e = ents.get("met-LEFKOSIA")
    assert e["kind"] == "weather_station" and e["kind_label"] == "Weather station" and e["name"] == "Lefkosia"
    assert e["state"]["label"].startswith("12.1 °C") and e["state"]["tone"] == "live"
    assert {r["key"] for r in e["readings"]} >= {"temperature_c", "humidity_pct", "rain_10min_mm"}
    assert any(s["source"] == "met_stations" and "Copyright" in s["detail"] for s in e["sources"])
    assert ents.series("met-LEFKOSIA") is None  # one reading is no day yet
    then = station_kinds.station_at(ents, e, datetime(2026, 11, 27, 23, 30, tzinfo=TZ))
    assert then["moment"]["known"] is False and then["state"]["value"] == "unknown"


def test_a_reference_air_station_opens_in_its_band_with_its_day(ents):
    e = ents.get("aq-CY0004A")
    assert e["kind"] == "reference_air_station" and e["state"]["label"].startswith("Fair: NO₂ 86.4")
    assert any(a["label"] == "Type" and a["value"] == "Traffic station" for a in e["attributes"])
    s = ents.series("aq-CY0004A")
    assert s["metric"] == "no2" and s["step_min"] == 60 and s["summary"]["last"] == 86.4
    assert s["threshold"]["value"] == 200
    found = ents.search("traffic station")
    assert found and found[0]["id"] == "aq-CY0004A"
    assert any(i["id"] == "met-LEFKOSIA" for i in ents.search("lefkosia"))


# -- the tools -------------------------------------------------------------------------------------


def ctx_for(env):
    return ToolContext(Settings(), env.city, None, None, SessionStore(), lambda e: None, "c1",
                       environment=env)


def test_measured_stations_sets_the_thermometers_beside_the_model(env):
    ctx = ctx_for(env)
    res, err = run_tool(ctx, "measured_stations", {"metric": "temperature_c"})
    assert not err, res
    lef = next(s for s in res["stations"] if s["id"] == "met-LEFKOSIA")
    assert lef["value"] == 12.1 and lef["model_there"] == 14.0 and lef["measured_minus_model"] == -1.9
    layer = ctx.store.layers[res["layer_ref"]]
    assert {f["properties"]["id"] for f in layer["features"]} >= {"met-LEFKOSIA", "met-MAMMARI"}
    assert set(layer["meta"]["sources"]) >= {"met_stations", "eea_air"}
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert rows and {"time", "id", "station", "value"} <= set(rows[0])
    air, err = run_tool(ctx, "measured_stations", {"metric": "no2"})
    assert not err and next(s for s in air["stations"] if s["id"] == "aq-CY0004A")["hour"] == "19:00 to 20:00"
    wind, err = run_tool(ctx, "measured_stations", {"metric": "wind"})
    assert not err and next(s for s in wind["stations"] if s["id"] == "met-ATHALASSA")["value"] == 3


def test_compare_with_reference_answers_with_a_layer_and_a_chart(env, memory):
    ref = env.measured.stored(["aq-CY0006A", "aq-CY0004A"], ["no2"], NOW - timedelta(days=7), NOW)
    add_city_sensor(memory, SENSOR[0], [("NO2", ts, v * 1.2) for sid, _, ts, v in ref
                                        if sid == env.measured.nearest(SENSOR[2], SENSOR[3], "no2")[0].id])
    ctx = ctx_for(env)
    res, err = run_tool(ctx, "compare_with_reference", {"metric": "no2"})
    assert not err, res
    assert res["comparisons"][0]["bias_pct"] == 20
    assert ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    one, err = run_tool(ctx, "compare_with_reference", {"sensor": "Centre"})
    assert not err, one
    assert {r["series"] for r in ctx.store.datasets[one["data_ref"]]["rows"]} == {
        "Centre (city sensor)", env.measured.nearest(SENSOR[2], SENSOR[3], "no2")[0].name}
    bad, err = run_tool(ctx, "compare_with_reference", {"sensor": "nowhere"})
    assert err and "Centre" in bad["error"]


def test_the_sources_and_their_freshness(env):
    from chatbot.routes.sources import PROBES

    assert {"met_stations", "eea_air"} <= set(sources.BY_ID)
    assert "Copyright" in sources.BY_ID["met_stations"].gives and sources.BY_ID["eea_air"].licence == "CC BY 4.0"
    assert set(sources.TOOL_SOURCES["measured_stations"]) >= {"met_stations", "eea_air"}
    agent = SimpleNamespace(environment=env)
    met = PROBES["met_stations"](agent)
    assert met["state"] == "live" and "22:00" in met["note"]
    assert PROBES["eea_air"](agent)["note"].endswith("19:00")
    fresh = SimpleNamespace(environment=Environment(Settings()))
    assert PROBES["met_stations"](fresh)["state"] == "waiting"


def test_the_stations_endpoint_follows_the_clock(env):
    from chatbot.routes import environment as routes

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(environment=env, city=env.city))))
    now = routes.stations(req, at=None)
    lef = next(s for s in now["stations"] if s["id"] == "met-LEFKOSIA")
    assert lef["readings"]["temperature_c"]["value"] == 12.1 and lef["rain_last_hour_mm"] == 0.0
    assert now["feeds"]["met"]["ok"]
    past = routes.stations(req, at="2026-11-25T12:30")
    assert not next(s for s in past["stations"] if s["id"] == "met-LEFKOSIA")["known"]
    assert next(s for s in past["stations"] if s["id"] == "aq-CY0004A")["known"]
    assert routes.compare(req, sensor=None, station=None, at=None, days=7)["rows"]

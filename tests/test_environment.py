"""The environment twin, offline: shade and sun hours from a made-up block of the 3D city, the
air surfaces from fake stations over a fake air model, the wind, the rain radar, a spot, the
chat tools, and a walk that keeps to the shade."""

import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from chatbot import sources
from chatbot.environment import sun as S
from chatbot.environment.service import TZ, Environment, compass, wind_from
from chatbot.environment.shade import Shade, TILE_PX, px_of
from chatbot.environment.spot import build_spot, parse, spot_id
from chatbot.environment.surfaces import METRICS, Station, Field, band_of
from chatbot.environment.weather import AIR_LATS, AIR_LONS, WIND_LATS, WIND_LONS, ModelGrids
from chatbot.routes import sources as source_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools.base import ToolContext
from chatbot.tools.registry import run_tool
from chatbot.tools.routes import shaded_choice

# A block 20 m square and 20 m tall, just off Eleftheria Square.
LAT, LON = 35.1700, 33.3600
M_LAT = 1 / 110574
M_LON = 1 / (111320 * math.cos(math.radians(LAT)))
# A winter afternoon (sun low in the south-west) and a winter night.
AFTERNOON = datetime(2026, 12, 21, 14, 0, tzinfo=TZ)
NIGHT = datetime(2026, 12, 21, 21, 0, tzinfo=TZ)
NOW = datetime(2026, 11, 26, 13, 0, tzinfo=TZ)


def at_m(east: float, north: float) -> tuple[float, float]:
    """(lat, lon) this many metres east and north of the block's middle."""
    return LAT + north * M_LAT, LON + east * M_LON


def block(east=0.0, north=0.0, half=10.0, h=20.0) -> dict:
    lat, lon = at_m(east, north)
    dx, dy = half * M_LON, half * M_LAT
    ring = [[lon - dx, lat - dy], [lon + dx, lat - dy], [lon + dx, lat + dy], [lon - dx, lat + dy], [lon - dx, lat - dy]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {"h": h}}


class FakeDLS:
    """The DLS's tiles: each building goes to the tile holding its first corner."""

    def __init__(self, buildings):
        self.by_tile: dict[tuple[int, int], list] = {}
        for b in buildings:
            x, y = px_of(*b["geometry"]["coordinates"][0][0])
            self.by_tile.setdefault((int(x // TILE_PX), int(y // TILE_PX)), []).append(b)
        self.asked = 0

    def tile(self, x, y):
        self.asked += 1
        return self.by_tile.get((x, y), [])

    def cached_tile(self, x, y):
        return self.by_tile.get((x, y), [])


@pytest.fixture
def shade():
    return Shade(FakeDLS([block()]))


# -- sun and shade ----------------------------------------------------------------------------


def test_the_sun_is_low_in_the_south_west_on_a_winter_afternoon():
    sun = S.position(AFTERNOON, LAT, LON)
    assert 15 < sun.altitude < 30 and compass(sun.bearing) == "south-west"
    assert S.position(NIGHT, LAT, LON).altitude < 0 and S.shadow_step(S.position(NIGHT, LAT, LON)) is None
    times = S.sun_times(AFTERNOON.date(), LAT, LON)
    assert times["sunrise"] < "07:00" < "16:30" < times["sunset"] and 9.5 < times["daylight_h"] < 10.5


def test_a_block_shades_the_ground_away_from_the_sun(shade):
    area = shade.area(LAT, LON, 60)
    mask = area.shade(AFTERNOON)
    lee, sunny = area.point_index(*reversed(at_m(18, 18))), area.point_index(*reversed(at_m(-18, -18)))
    assert mask[lee] and not mask[sunny]
    roof = area.point_index(*reversed(at_m(0, 0)))
    assert area.roofs[roof] and not mask[roof]  # a roof is never "in shade"
    assert area.shade(NIGHT) is None  # the sun is down: everything is in shade
    assert shade.at_point(*at_m(18, 18), AFTERNOON) is True
    assert shade.at_point(*at_m(-18, -18), AFTERNOON) is False


def test_sun_hours_are_fewer_in_the_lee_of_the_block(shade):
    area = shade.area(LAT, LON, 60)
    day = area.sun_hours(AFTERNOON.date())
    hours = day["hours"]
    north = hours[area.point_index(*reversed(at_m(0, 14)))]  # in its winter shadow all day
    south = hours[area.point_index(*reversed(at_m(0, -40)))]
    assert south > 8 and north < 1
    assert len(day["profile"]) == 96 and max(p["sun_pct"] for p in day["profile"]) <= 100
    assert day["open_cells"] == int((~area.roofs).sum())


def test_a_walk_reads_its_shade_as_it_goes(shade):
    lee_line = [list(reversed(at_m(-40, 15))), list(reversed(at_m(40, 15)))]
    sunny_line = [list(reversed(at_m(-40, -40))), list(reversed(at_m(40, -40)))]
    lee = shade.along(lee_line, AFTERNOON, 1.3)
    sunny = shade.along(sunny_line, AFTERNOON, 1.3)
    assert lee["sun_up"] and lee["shade_pct"] > sunny["shade_pct"] and sunny["shade_pct"] == 0
    assert lee["samples"][0]["along_m"] == 0 and lee["samples"][-1]["along_m"] == 80
    assert shade.along(lee_line, NIGHT, 1.3)["shade_pct"] == 100


def test_the_environment_serves_sun_hours_and_a_shade_grid(shade):
    env = Environment(Settings(), dls=None)
    env.shade = shade
    hours = env.sun_hours(LAT, LON, 60, AFTERNOON.date())
    assert hours["unit"] == "quarter_hours" and hours["nx"] == hours["ny"] and len(hours["bounds"]) == 4
    assert hours["hours"]["min"] < 1 < 8 < hours["hours"]["max"] and hours["sun"]["daylight_h"] == round(hours["sun"]["daylight_h"], 1)
    grid = env.shade_grid(LAT, LON, 60, AFTERNOON)
    assert grid["sun_up"] and 0 < grid["shade_pct"] < 100 and grid["sun"]["from"] == "south-west"
    night = env.shade_grid(LAT, LON, 60, NIGHT)
    assert not night["sun_up"] and night["shade_pct"] == 100


# -- surfaces -----------------------------------------------------------------------------------


def air_model(pm=12.0):
    """The air model's hour on its 3 by 3 lattice: the same everywhere."""
    pts = [(la, lo) for la in AIR_LATS for lo in AIR_LONS]
    return {"points": pts, "time": "2026-11-26T13:00", "pm2_5": [pm] * 9, "pm10": [pm * 2] * 9,
            "nitrogen_dioxide": [20.0] * 9, "ozone": [60.0] * 9}


STATIONS = [Station("a1", "Centre", 35.170, 33.360, 30.0), Station("a2", "South", 35.140, 33.380, 22.0)]


def test_a_surface_is_the_station_near_it_and_the_corrected_model_far_away():
    f = Field(METRICS["pm2_5"], [Station(**vars(s)) for s in STATIONS], air_model(12.0))
    assert f.has_model and f.bias == pytest.approx(14.0)  # the stations' mean miss
    near = f.at(35.1701, 33.3601)
    far = f.at(35.25, 33.22)
    assert near["value"] == pytest.approx(30.0, abs=1.0) and near["confidence"] > 0.8
    assert far["value"] == pytest.approx(26.0, abs=0.5) and far["confidence"] < 0.01
    assert far["uncertainty"] > near["uncertainty"] and near["nearest"]["id"] == "a1"
    g = f.grid()
    assert len(g["values"]) == g["nx"] * g["ny"] == len(g["confidence"]) and max(g["confidence"]) > 80
    assert band_of(f.metric, 30.0) == "Poor"


def test_noise_is_known_only_near_its_stations():
    f = Field(METRICS["noise_db"], [Station("a1", "Centre", 35.170, 33.360, 62.0)], None)
    assert not f.has_model and f.known
    assert f.at(35.170, 33.360)["confidence"] > 0.8
    assert f.at(35.20, 33.42)["confidence"] < 0.01  # a few km off: nothing is measured there
    g = f.grid()
    assert min(g["confidence"]) == 0 and g["max"] == pytest.approx(62.0, abs=0.5)


class FakeCity:
    def __init__(self, now=NOW, rows=None):
        self._now = now
        self.rows = rows if rows is not None else [("f60f85f2babb", "Air station Centre", 35.170, 33.360, {"pm2_5": 30.0, "noise_db": 62.0, "temperature_c": 19.5, "caqi": 54}),
                                                   ("a2", "Air station South", 35.140, 33.380, {"pm2_5": 22.0, "noise_db": 55.0, "temperature_c": 20.5, "caqi": 40})]

    def now(self):
        return self._now

    def states(self, type_):
        return [SimpleNamespace(device=SimpleNamespace(name=i, lat=la, lon=lo), label=label,
                                interp=SimpleNamespace(detail=d, stale=False, state="ok", as_of=self._now))
                for i, label, la, lo, d in self.rows]

    def devices_of(self, type_):
        return [SimpleNamespace(name=i, lat=la, lon=lo) for i, _, la, lo, _ in self.rows]


def open_meteo(fail=False):
    """Open-Meteo's answer for a lattice: one hourly series per point."""
    calls = []

    def fetch(url, user_agent=None, params=None, timeout=None):
        calls.append((url, params))
        if fail:
            from chatbot.web import WebError
            raise WebError("Open-Meteo is down")
        n = len(params["latitude"].split(","))
        day = params["start_date"]
        times = [f"{day}T{h:02d}:00" for h in range(24)]
        series = {"pm2_5": 12.0, "pm10": 24.0, "nitrogen_dioxide": 20.0, "ozone": 60.0, "temperature_2m": 18.0,
                  "wind_speed_10m": 4.0, "wind_direction_10m": 45.0, "wind_gusts_10m": 8.0}
        return [{"hourly": {"time": times, **{k: [v] * 24 for k, v in series.items() if k in params["hourly"].split(",")}}}
                for _ in range(n)]
    return fetch, calls


def make_env(city=None, fail=False, radar=None, shade=None):
    fetch, calls = open_meteo(fail)
    s = Settings()
    city = city or FakeCity()

    def get(url, user_agent=None, params=None, timeout=None):
        if "rainviewer" in url:
            if isinstance(radar, Exception):
                raise radar
            return radar
        return fetch(url, user_agent=user_agent, params=params, timeout=timeout)
    env = Environment(s, city=city, models=ModelGrids(s, fetch=get, now=city.now), fetch=get)
    if shade is not None:
        env.shade = shade
    return env, calls


def test_the_air_surface_from_the_stations_and_the_model():
    env, calls = make_env()
    surf = env.surface("pm2_5", None)
    assert surf["known"] and surf["background"] == "model" and surf["model_bias"] == pytest.approx(14.0)
    assert {s["id"] for s in surf["stations"]} == {"f60f85f2babb", "a2"} and surf["highest_station"]["value"] == 30.0
    assert [b["label"] for b in surf["bands"]][:3] == ["Good", "Fair", "Moderate"] and surf["bands"][-1]["to"] is None
    assert [x["name"] for x in surf["sources"]][0] == "Smart Nicosia air stations"
    n = len(calls)
    env.surface("pm2_5", None)
    assert len(calls) == n  # the field is kept for a minute
    noise = env.surface("noise_db", None)
    assert noise["background"] == "stations" and noise["model_bias"] is None


def test_without_the_model_the_stations_still_make_a_surface():
    env, _ = make_env(fail=True)
    surf = env.surface("pm2_5", None)
    assert surf["known"] and surf["background"] == "stations"
    assert any("didn't answer" in n for n in surf["notes"])


def test_nothing_known_is_said_not_guessed():
    env, _ = make_env(city=FakeCity(rows=[]), fail=True)
    surf = env.surface("noise_db", None)
    assert surf["known"] is False and "no air station" in surf["reason"]


def test_the_wind_blows_from_where_the_model_says():
    env, _ = make_env()
    w = env.wind(None)
    assert w["known"] and len(w["u"]) == len(WIND_LATS) * len(WIND_LONS) and w["centre"]["from"] == "north-east"
    assert w["u"][0] < 0 and w["v"][0] < 0  # from the north-east: it moves south-west
    here = env.wind_at(LAT, LON, None)
    assert here["from"] == "north-east" and here["speed_kmh"] == round(4.0 * 3.6)
    assert compass(wind_from(-1.0, -1.0)) == "north-east"


RADAR = {"host": "https://tilecache.rainviewer.com", "radar": {"past": [
    {"time": 1795692600, "path": "/v2/radar/aaa"}, {"time": 1795693200, "path": "/v2/radar/bbb"}]}}


def test_the_rain_radar_frames_are_read_once_in_five_minutes():
    env, _ = make_env(radar=RADAR)
    agent = SimpleNamespace(environment=env)
    assert source_routes._radar(agent)["state"] == "waiting"
    r = env.radar()
    assert r["known"] and r["frames"][-1]["time"] == 1795693200 * 1000
    assert r["frames"][0]["tiles"] == "https://tilecache.rainviewer.com/v2/radar/aaa/256/{z}/{x}/{y}/2/1_1.png"
    assert env.radar() is r  # cached
    assert source_routes._radar(agent)["state"] == "live"


def test_a_radar_that_does_not_answer_says_so():
    from chatbot.web import WebError
    env, _ = make_env(radar=WebError("timed out"))
    r = env.radar()
    assert not r["known"] and "isn't answering" in r["reason"] and r["frames"] == []
    assert source_routes._radar(SimpleNamespace(environment=env))["state"] == "unavailable"


# -- a spot -----------------------------------------------------------------------------------


def test_spot_ids_say_where_and_stay_in_greater_nicosia():
    assert spot_id(35.17, 33.3606) == "spot-35.17000-33.36060"
    assert parse("spot-35.17000-33.36060") == (35.17, 33.3606)
    assert parse("spot-34.70000-33.02000") is None  # Limassol: not this twin's
    assert parse("spot-35.1-33") is None


def test_a_spot_reads_sun_air_and_weather_there(shade):
    env, _ = make_env(city=FakeCity(now=AFTERNOON), shade=shade)
    ents = SimpleNamespace(environment=env, city=env.city, places_within=lambda *a, **k: [{"name": "Eleftheria Square", "id": "p1"}])
    lat, lon = at_m(18, 18)
    e = build_spot(ents, spot_id(lat, lon), None)
    assert e["kind"] == "spot" and e["name"] == "Near Eleftheria Square"
    assert e["state"]["value"] == "shade" and e["environment"]["sun"]["shade"] is True
    assert e["environment"]["sun"]["hours_today"] is not None and len(e["environment"]["sun"]["day"]) == 96
    pm = next(a for a in e["environment"]["air"] if a["metric"] == "pm2_5")
    assert pm["value"] is not None and pm["uncertainty"] > 0 and pm["nearest"]["id"] == "f60f85f2babb"
    assert e["_for_model"]["environment"]["in_shade"] is True
    assert any(r["label"] == "Nearest air station" for r in e["relations"])
    night = build_spot(ents, spot_id(lat, lon), None, NIGHT)
    assert night["state"]["value"] == "night"


# -- the chat tools ---------------------------------------------------------------------------


class StubRouter:
    """Two ways between the same places: the quickest along the sunny side, and one a little
    longer in the lee of the block."""

    def __init__(self, ways):
        self.ways = ways

    def alternatives(self, stops, mode, prefer):
        return self.ways

    def route(self, stops, mode, prefer, avoid=None):
        return self.ways[0]


def way(points, minutes):
    return {"distance_km": round(minutes * 0.08, 2), "duration_min": minutes,
            "coordinates": [list(reversed(p)) for p in points],
            "legs": [{"distance_km": round(minutes * 0.08, 2), "duration_min": minutes}]}


def ctx_for(env, router=None):
    places = SimpleNamespace(ready=False, geocode=lambda text, limit=3: [{"name": "Eleftheria Square", "lat": LAT, "lon": LON}])
    events = []
    return ToolContext(Settings(), env.city, places, router or StubRouter([]), SessionStore(), events.append, "c1",
                       environment=env)


def test_sun_and_shade_at_a_moment_and_over_a_day(shade):
    env, _ = make_env(shade=shade)
    ctx = ctx_for(env)
    res, err = run_tool(ctx, "sun_and_shade", {"lat": LAT, "lon": LON, "radius_m": 60, "date": "2026-12-21", "time": "14:00"})
    assert not err, res
    assert res["mode"] == "moment" and res["sun_up"] and 0 < res["shade_pct"] < 100
    assert res["shade_by_side_pct"]["north"] > res["shade_by_side_pct"]["south"]
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["environment"]["kind"] == "shade" and layer["meta"]["sources"] == ["dls"]
    assert ctx.store.datasets[res["data_ref"]]["rows"][0].keys() == {"time", "sun_pct", "sun_altitude_deg"}
    day, err = run_tool(ctx, "sun_and_shade", {"place": "Eleftheria Square", "radius_m": 60, "date": "2026-12-21"})
    assert not err and day["mode"] == "day" and day["place"] == "Eleftheria Square"
    assert day["hours_of_direct_sun"]["min"] < 1 and ctx.store.layers[day["layer_ref"]]["meta"]["environment"]["kind"] == "sun_hours"
    bad, err = run_tool(ctx, "sun_and_shade", {"lat": LAT, "lon": LON, "date": "21/12"})
    assert err and "YYYY-MM-DD" in bad["error"]


def test_environment_surface_names_the_highest_place_and_its_uncertainty():
    env, _ = make_env()
    ctx = ctx_for(env)
    res, err = run_tool(ctx, "environment_surface", {"metric": "pm2_5"})
    assert not err, res
    assert res["highest_station"]["id"] == "f60f85f2babb" and res["stations"] == 2
    assert "±" in res["uncertainty"]["near_a_station"] and "corrected" in res["background"]
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["environment"]["kind"] == "surface" and set(layer["meta"]["sources"]) == {"smart_nicosia", "open_meteo"}
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert {r["id"] for r in rows} == {"f60f85f2babb", "a2"}
    wind, err = run_tool(ctx, "environment_surface", {"metric": "wind"})
    assert not err and wind["over_nicosia"]["from"] == "north-east"
    assert ctx.store.layers[wind["layer_ref"]]["meta"]["sources"] == ["open_meteo"]
    _, err = run_tool(ctx, "environment_surface", {"metric": "pm2_5", "at": "2027-01-01T10:00"})
    assert err


def test_the_tools_say_when_the_environment_is_off():
    env, _ = make_env()
    ctx = ctx_for(env)
    ctx.environment = None
    res, err = run_tool(ctx, "environment_surface", {"metric": "pm2_5"})
    assert err and "isn't available" in res["error"]


def test_the_tools_name_their_sources():
    assert sources.TOOL_SOURCES["sun_and_shade"] == ("dls",)
    assert set(sources.TOOL_SOURCES["environment_surface"]) == {"smart_nicosia", "open_meteo"}
    assert {"rainviewer", "terrain_tiles"} <= set(sources.BY_ID)


# -- walking in the shade -----------------------------------------------------------------------


SUNNY = way([at_m(-40, -40), at_m(40, -40)], 10.0)
SHADY = way([at_m(-40, -40), at_m(-40, 15), at_m(40, 15), at_m(40, -40)], 12.0)
TOO_LONG = way([at_m(-40, -40), at_m(-40, 15), at_m(40, 15), at_m(40, -40)], 30.0)


def test_a_shaded_walk_takes_the_shadier_way_within_a_third_more_time(shade):
    env, _ = make_env(shade=shade)
    ctx = ctx_for(env)
    route, info = shaded_choice(ctx, [SUNNY, SHADY], AFTERNOON, 2, "walk")
    assert route is SHADY and info["sun_up"] and info["minutes_added"] == 2.0
    assert info["options"][0]["shade_pct"] < info["options"][1]["shade_pct"] and info["options"][1]["chosen"]
    assert info["stretches"] and any(sh for sh, _ in info["stretches"]) and len(info["others"]) == 1
    route, _ = shaded_choice(ctx, [SUNNY, TOO_LONG], AFTERNOON, 2, "walk")
    assert route is SUNNY  # three times as long is not worth the shade
    route, info = shaded_choice(ctx, [SUNNY, SHADY], NIGHT, 2, "walk")
    assert route is SUNNY and "every way is in shade" in info["note"]


def test_plan_route_with_shade_draws_the_walk_in_shade_and_sun(shade):
    env, _ = make_env(city=FakeCity(now=AFTERNOON - timedelta(minutes=10)), shade=shade)
    ctx = ctx_for(env, StubRouter([SUNNY, SHADY]))
    a, b = at_m(-40, -40), at_m(40, -40)
    res, err = run_tool(ctx, "plan_route", {"stops": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
                                            "mode": "walk", "prefer": ["shade"], "start_time": "14:00"})
    assert not err, res
    assert res["shade"]["chosen"]["shade_pct"] > 0 and res["shade"]["walk_starts"].endswith("14:00")
    layer = ctx.store.layers[res["layer_ref"]]
    states = {f["properties"]["state"] for f in layer["features"]}
    assert {"in shade", "usual route"} <= states and "dls" in layer["meta"]["sources"]
    assert "in shade" in layer["meta"]["legend"]


# -- the page's endpoints -----------------------------------------------------------------------


@pytest.fixture
def request_for():

    def make(env):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(environment=env, city=env.city))))
    return make


def test_the_endpoints_answer_and_refuse_plainly(request_for, shade):
    from fastapi import HTTPException

    from chatbot.routes import environment as env_routes

    env, _ = make_env(shade=shade)
    req = request_for(env)
    assert env_routes.surface(req, metric="no2", at=None)["known"]
    with pytest.raises(HTTPException) as e:
        env_routes.surface(req, metric="pollen", at=None)
    assert e.value.status_code == 400
    assert env_routes.wind(req, at=None)["centre"]["from"] == "north-east"
    grid = env_routes.shade(req, lat=LAT, lon=LON, radius_m=60, at="2026-12-21T14:00")
    assert grid["sun_up"] and grid["at"] == "2026-12-21T14:00"
    with pytest.raises(HTTPException) as e:
        env_routes.shade(req, lat=LAT, lon=LON, radius_m=60, at="at three")
    assert e.value.status_code == 400
    hours = env_routes.sun_hours(req, lat=LAT, lon=LON, radius_m=60, day="2026-06-21")
    assert hours["date"] == "2026-06-21" and hours["sun"]["daylight_h"] > 14
    with pytest.raises(HTTPException) as e:
        env_routes.sun_hours(req, lat=LAT, lon=LON, radius_m=60, day="June")
    assert e.value.status_code == 400


def test_two_close_stations_keep_their_own_readings():
    # A market and the town hall, 750 m apart, read very differently: neither is averaged away.
    market, hall = (35.1682, 33.3713), (35.1732, 33.3659)
    f = Field(METRICS["pm2_5"], [Station("m", "Market", *market, 27.7), Station("h", "Town hall", *hall, 9.9)], air_model(12.0))
    assert f.at(*market)["value"] == pytest.approx(27.7, abs=0.3) and f.at(*market)["confidence"] > 0.99
    assert f.at(*hall)["value"] == pytest.approx(9.9, abs=0.3)
    mid = f.at((market[0] + hall[0]) / 2, (market[1] + hall[1]) / 2)["value"]
    assert 9.9 < mid < 27.7
    assert f.grid()["max"] > 24  # the market's cell, on the map too


def test_a_drive_is_not_read_for_shade(shade, monkeypatch):
    from chatbot import context
    monkeypatch.setattr(context, "speed", lambda city, weeks: {"rows": []})
    env, _ = make_env(city=FakeCity(now=AFTERNOON - timedelta(minutes=10)), shade=shade)
    ctx = ctx_for(env, StubRouter([SUNNY, SHADY]))
    a, b = at_m(-40, -40), at_m(40, -40)
    res, err = run_tool(ctx, "plan_route", {"stops": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
                                            "mode": "drive", "prefer": ["shade"]})
    assert not err, res
    assert res["shade"] == {"note": "shade is read for walking and cycling, not driving"}
    states = {f["properties"]["state"] for f in ctx.store.layers[res["layer_ref"]]["features"]}
    assert "in shade" not in states and "in sun" not in states

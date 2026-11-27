"""The weather the map draws: cloud by layer, how far one sees, rain and wet streets, wind and
Saharan dust, now and at past moments of the city clock. Offline, on Open-Meteo answers trimmed
into tests/fixtures (the showers of 1 Oct 2026 and the fog of 16 Oct; `current` as read on
26 Nov, with a shower made up around it)."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from chatbot import context, overview, server
from chatbot.context import MAP_KEYS, dust_history, map_weather, weather, weather_history
from chatbot.routes import live as live_routes
from chatbot.settings import Settings
from chatbot.web import WebError

from conftest import load
from test_memory import TZ, fake_city, memory  # noqa: F401 - fixtures


def answers(monkeypatch, **by_url):
    """Open-Meteo stood in by fixtures, by which of its two addresses is asked; the params of
    every call are kept."""
    calls = []

    def get_json(url, *, user_agent, params, timeout):
        calls.append(params)
        assert user_agent == Settings().user_agent
        key = "air" if "air-quality" in url else "forecast"
        return load(by_url[key])

    monkeypatch.setattr(context, "get_json", get_json)
    return calls


def test_map_weather_reads_one_row():
    row = {"cloud_cover": 99.6, "cloud_cover_low": 81, "cloud_cover_mid": 96, "cloud_cover_high": 32,
           "visibility": 40040.0, "precipitation": 0.0, "rain": 0.2, "showers": 0.0, "snowfall": 0.0,
           "wind_speed_10m": 10.8, "wind_direction_10m": 4, "weather_code": 3}
    w = map_weather(row, wet_mm=1.4)
    assert w == {"cloud_pct": 100, "cloud_low_pct": 81, "cloud_mid_pct": 96, "cloud_high_pct": 32,
                 "visibility_m": 40040, "rain_mm": 0.2, "snow_cm": 0.0, "wet_mm": 1.4,
                 "wind_kmh": 10.8, "wind_from_deg": 4, "code": 3}
    # Missing readings stay missing (the page draws nothing it doesn't know), never 0% cloud.
    empty = map_weather({"visibility": None})
    assert empty["cloud_pct"] is None and empty["visibility_m"] is None
    assert empty["rain_mm"] == 0.0 and empty["wet_mm"] == 0.0


def test_the_history_carries_what_the_map_draws(monkeypatch):
    calls = answers(monkeypatch, forecast="open_meteo_history.json")
    past = weather_history(Settings())
    p = calls[0]
    assert p["past_days"] == context.WEATHER_PAST_DAYS and "cloud_cover_low" in p["hourly"]
    assert "visibility" in p["hourly"] and "wind_direction_10m" in p["hourly"]
    shower = past["2026-10-01T17:00"]
    assert shower["sky"] == "thunderstorm" and shower["rain_mm"] == 2.5
    assert shower["cloud_low_pct"] == 49 and shower["wind_from_deg"] == 288
    # Wet: what fell this hour and the two before (0.7 + 1.7 + 2.5).
    assert shower["wet_mm"] == pytest.approx(4.9)
    after = past["2026-10-01T19:00"]
    assert after["rain_mm"] == 0.0 and after["wet_mm"] > 0  # the rain stopped; the streets are wet
    fog = past["2026-10-16T06:00"]
    assert fog["visibility_m"] == 40 and fog["sky"] == "fog"
    assert set(MAP_KEYS) - set(fog) == {"dust_ugm3", "aod"}  # those come from the air model


def test_dust_history(monkeypatch):
    calls = answers(monkeypatch, air="open_meteo_dust_history.json")
    dust = dust_history(Settings())
    assert calls[0]["past_days"] == context.WEATHER_PAST_DAYS
    assert calls[0]["hourly"] == "dust,aerosol_optical_depth"
    assert dust["2026-10-01T17:00"] == {"dust_ugm3": 180.0, "aod": 0.9}


def test_weather_now_gives_the_rate_and_wet_streets(monkeypatch):
    calls = answers(monkeypatch, forecast="open_meteo_now.json", air="open_meteo_air_now.json")
    res = weather(Settings(), now=datetime(2026, 11, 26, 15, 45, tzinfo=TZ))
    assert calls[0]["past_days"] == 1 and "cloud_cover_high" in calls[0]["current"]
    assert "aerosol_optical_depth" in calls[1]["current"]
    now = res["now"]
    assert now["sky"] == "overcast" and now["cloud_pct"] == 100 and now["cloud_high_pct"] == 100
    assert now["cloud_low_pct"] == 14 and now["visibility_m"] == 39680
    # `current` sums 15 minutes (0.15 mm); the hour now falls in (15:00 to 16:00) fell 0.6 mm.
    assert now["rain_mm"] == 0.6
    assert now["wet_mm"] == pytest.approx(0.4 + 1.2 + 0.6)
    assert now["wind_kmh"] == 10.3 and now["wind_from_deg"] == 54
    assert now["dust_ugm3"] == 0.0 and now["aod"] == 0.07
    # The hours to come are as before: from this hour on, twelve of them.
    assert res["next_12_hours"][0]["time"] == "2026-11-26T15:00" and len(res["next_12_hours"]) == 12


@pytest.fixture
def agent(fake_city, memory, monkeypatch):  # noqa: F811 - pytest fixtures
    a = SimpleNamespace(settings=Settings(), city=fake_city, places=None, router=None,
                        memory=memory, registers=SimpleNamespace(ready=False))
    monkeypatch.setattr(server.app.state, "agent", a, raising=False)
    overview._cache.clear()
    yield a
    overview._cache.clear()


REQUEST = SimpleNamespace(app=server.app)


def test_the_strip_says_what_the_map_draws_then_and_now(agent, monkeypatch):
    answers(monkeypatch, forecast="open_meteo_history.json", air="open_meteo_dust_history.json")
    then = live_routes.overview(REQUEST, at="2026-10-01T17:15")["weather"]
    assert then["sky"] == "thunderstorm" and then["rain_mm"] == 2.5
    assert then["dust_ugm3"] == 180.0 and then["aod"] == 0.9  # the dust of that hour joins it
    fog = live_routes.overview(REQUEST, at="2026-10-16T06:00")["weather"]
    assert fog["visibility_m"] == 40 and fog["dust_ugm3"] == 0.0

    overview._cache.clear()
    answers(monkeypatch, forecast="open_meteo_now.json", air="open_meteo_air_now.json")
    monkeypatch.setattr(overview, "weather", lambda s: weather(s, now=datetime(2026, 11, 26, 15, 45, tzinfo=TZ)))
    live = live_routes.overview(REQUEST, at=None)["weather"]
    assert set(MAP_KEYS) <= set(live) and live["sky"] == "overcast" and live["temp_c"] == 18.3


def test_a_moment_without_dust_still_has_its_weather(agent, monkeypatch):
    def broken(s):
        raise WebError("air quality down")

    answers(monkeypatch, forecast="open_meteo_history.json")
    monkeypatch.setattr(overview, "dust_history", broken)
    then = live_routes.overview(REQUEST, at="2026-10-01T17:00")["weather"]
    assert then["rain_mm"] == 2.5 and then["dust_ugm3"] is None and then["aod"] is None

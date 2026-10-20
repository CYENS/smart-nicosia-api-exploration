"""Live data the page shows without being asked, cached briefly: the strip of city-wide readouts
across the top, and the street lamps that light the map at night."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from collector.interpret import caqi_band

from chatbot.context import weather
from chatbot.store import point

if TYPE_CHECKING:
    from chatbot.agent import Agent

log = logging.getLogger("chatbot")

CITY_TTL_S = 60
WEATHER_TTL_S = 15 * 60

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl_s: float, fn: Callable[[], Any]) -> Any:
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl_s:
            return hit[1]
    try:
        value = fn()
    except Exception as e:  # noqa: BLE001 - one failing source leaves the other readouts
        log.warning("live strip: %s failed: %s", key, e)
        value = None
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def live_strip(agent: Agent) -> dict:
    """Lamps lit, free parking, full bins, air quality, the weather and the registers. A source
    that fails is left out, so the page shows what it can."""
    city = _cached("city", CITY_TTL_S, agent.city.overview) or {}
    out: dict[str, Any] = {}

    def by_state(kind: str) -> tuple[dict, int] | None:
        entry = city.get(kind) or {}
        return (entry["by_state"], entry["devices"]) if "by_state" in entry else None

    if lights := by_state("street_light"):
        out["lights"] = {"on": lights[0].get("on", 0), "total": lights[1],
                         "as_of": city["street_light"].get("newest_reading")}
    if parking := by_state("parking_sensor"):
        known = parking[0].get("free", 0) + parking[0].get("occupied", 0)
        out["parking"] = {"free": parking[0].get("free", 0), "total": known}
    if bins := by_state("waste_bin"):
        out["bins"] = {"full": bins[0].get("full", 0), "total": bins[1]}
    stations = [s["caqi"] for s in (city.get("air_station") or {}).get("stations", [])
                if isinstance(s.get("caqi"), (int, float))]
    if stations:
        median = statistics.median(stations)
        out["air"] = {"caqi": round(median), "band": caqi_band(median), "stations": len(stations)}

    sky = _cached("weather", WEATHER_TTL_S, lambda: weather(agent.settings))
    if sky:
        now = sky["now"]
        out["weather"] = {"temp_c": now["temp_c"], "sky": now["sky"], "daylight": now["daylight"]}

    if agent.registers.ready:
        out["registers"] = {"buildings": len(agent.registers.records())}
    return out


def live_lamps(agent: Agent) -> dict:
    """Every smart street lamp and whether it is lit, for the map's night lighting."""
    states = _cached("lamps", CITY_TTL_S, lambda: agent.city.states("Lighting")) or []
    return {"type": "FeatureCollection", "features": [
        point(s.device.lon, s.device.lat, {"id": s.device.name, "label": s.label,
                                           "state": s.interp.state,
                                           "power_w": s.interp.detail.get("power_w")})
        for s in states if s.device.lat is not None and s.device.lon is not None]}

"""Live data the page shows without being asked, cached briefly: the strip of city-wide readouts
across the top (lamps, parking, bins, air, weather, registers, buses running and the slowest
road), and the street lamps that light the map at night."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from collector.interpret import caqi_band

from chatbot.context import weather, weather_history
from chatbot.moments import Moments
from chatbot.settings import TIMEZONE
from chatbot.store import point

TZ = ZoneInfo(TIMEZONE)

if TYPE_CHECKING:
    from chatbot.agent import Agent

log = logging.getLogger("chatbot")

CITY_TTL_S = 60
WEATHER_TTL_S = 15 * 60

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def cached(key: str, ttl_s: float, fn: Callable[[], Any],
           keep: Callable[[Any], bool] | None = None) -> Any:
    """fn()'s value, reused for ttl_s. A failure is logged and kept as None for ttl_s too, so
    a broken source isn't asked again on every request."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl_s:
            return hit[1]
    try:
        value = fn()
    except Exception as e:  # noqa: BLE001 - one failing source leaves the other readouts
        log.warning("%s failed: %s", key, e)
        value = None
    with _lock:
        if keep is None or keep(value):
            _cache[key] = (time.time(), value)
    return value


def fresh(agent: Agent, device_type: str) -> Counter | None:
    """The type's states now, counting only devices whose reading can be trusted (cached)."""
    return cached(f"fresh:{device_type}", CITY_TTL_S, lambda: Counter(
        s.interp.state for s in agent.city.states(device_type) if not s.interp.stale))


def live_strip(agent: Agent) -> dict:
    """Lamps lit, free parking, full bins, air quality, the weather and the registers. A source
    that fails is left out, so the page shows what it can."""
    city = cached("city", CITY_TTL_S, agent.city.overview) or {}
    out: dict[str, Any] = {}

    def by_state(kind: str) -> tuple[dict, int] | None:
        entry = city.get(kind) or {}
        return (entry["by_state"], entry["devices"]) if "by_state" in entry else None

    if lights := by_state("street_light"):
        out["lights"] = {"on": lights[0].get("on", 0), "total": lights[1],
                         "as_of": city["street_light"].get("newest_reading")}
    # Bays and bins that stopped reporting are left out, as find_devices and every replay of
    # the twin's memory do, so the strip now and the strip at another moment compare.
    if by_state("parking_sensor") and (parking := fresh(agent, "Parking")) is not None:
        known = parking.get("free", 0) + parking.get("occupied", 0)
        out["parking"] = {"free": parking.get("free", 0), "total": known}
    if by_state("waste_bin") and (bins := fresh(agent, "Wastebin")) is not None:
        out["bins"] = {"full": bins.get("full", 0), "total": sum(bins.values())}
    stations = [s["caqi"] for s in (city.get("air_station") or {}).get("stations", [])
                if isinstance(s.get("caqi"), (int, float))]
    if stations:
        median = statistics.median(stations)
        out["air"] = {"caqi": round(median), "band": caqi_band(median), "stations": len(stations)}

    sky = cached("weather", WEATHER_TTL_S, lambda: weather(agent.settings))
    if sky:
        now = sky["now"]
        out["weather"] = {"temp_c": now["temp_c"], "sky": now["sky"], "daylight": now["daylight"]}

    if agent.registers.ready:
        out["registers"] = {"buildings": len(agent.registers.records())}

    mob = getattr(agent, "mobility", None)
    moving = cached("mobility_summary", 30, mob.summary) if mob is not None else None
    buses = (moving or {}).get("buses") or {}
    if buses.get("status") in ("live", "stale"):
        out["buses"] = {"running": buses["running"], "lines": buses.get("lines"), "late": buses.get("late")}
    worst = ((moving or {}).get("roads") or {}).get("slowest") or []
    if worst and worst[0]["share_of_free_flow"] is not None:
        r = worst[0]
        out["slowest_road"] = {"id": r["id"], "name": r["short"], "segment": r["name"],
                               "speed_kmh": r["speed_kmh"], "free_flow_kmh": r["free_flow_kmh"],
                               "state": r["state"]}
    return out


def moments_of(agent) -> Moments:
    """The agent's view of past moments (made on first use, for agents built without one)."""
    m = getattr(agent, "moments", None)
    if m is None:
        m = Moments(agent.city, agent.memory)
        agent.moments = m
    return m


def strip_at(agent: Agent, at: datetime) -> dict:
    """The strip as it was at a past moment, from the twin's memory: lamps lit, free parking,
    full bins, air and the modelled weather of that hour. Registers have no history, so they
    read as they are now."""
    past = cached("weather_history", 3600, lambda: weather_history(agent.settings)) or {}
    hour = at.astimezone(TZ).replace(minute=0).strftime("%Y-%m-%dT%H:%M")
    out = moments_of(agent).strip_at(at, past.get(hour))
    if agent.registers and agent.registers.ready:
        out["registers"] = {"buildings": len(agent.registers.records())}
    return out


def live_lamps(agent: Agent) -> dict:
    """Every smart street lamp and whether it is lit, for the map's night lighting."""
    states = cached("lamps", CITY_TTL_S, lambda: agent.city.states("Lighting")) or []
    return {"type": "FeatureCollection", "features": [
        point(s.device.lon, s.device.lat, {"id": s.device.name, "label": s.label,
                                           "state": s.interp.state,
                                           "power_w": s.interp.detail.get("power_w")})
        for s in states if s.device.lat is not None and s.device.lon is not None]}

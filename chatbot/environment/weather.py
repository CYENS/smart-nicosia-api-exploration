"""Modelled weather and air over Nicosia from Open-Meteo, as small grids of points, hour by hour.

- The air model (CAMS Europe, about 10 km cells, through Open-Meteo's air quality API) and the
  temperature model on a 3 by 3 lattice over Greater Nicosia: the background the city's seven
  stations are laid over (surfaces.py).
- Wind at 10 m on a 5 by 5 lattice about 55 km across, so the particles on the map move with the
  real flow around the city (the sea breeze, the Kyrenia range).

One request per grid and local day, cached (an hour for today, a day for the past). Open-Meteo is
free and keyless; requests carry the app's user agent.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from chatbot.settings import TIMEZONE, Settings
from chatbot.web import get_json

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

# The background lattice: 3 by 3 points over Greater Nicosia (lat south to north, lon west to
# east), about 9 km apart, CAMS's own spacing.
AIR_LATS = (35.08, 35.165, 35.25)
AIR_LONS = (33.22, 33.36, 33.50)
# The wind lattice: 5 by 5 points, about 14 km apart.
WIND_LATS = (34.92, 35.045, 35.17, 35.295, 35.42)
WIND_LONS = (33.06, 33.21, 33.36, 33.51, 33.66)
AIR_VARS = {"pm2_5": "pm2_5", "pm10": "pm10", "no2": "nitrogen_dioxide", "o3": "ozone"}
PAST_DAYS = 92  # how far back Open-Meteo's forecast archive reaches with start_date
TTL_TODAY_S = 3600
TTL_PAST_S = 24 * 3600
CACHE_DAYS = 64


def lattice(lats: tuple[float, ...], lons: tuple[float, ...]) -> list[tuple[float, float]]:
    """Points row by row from the south, west to east."""
    return [(la, lo) for la in lats for lo in lons]


class ModelGrids:
    """Open-Meteo hourly values on the lattices, by local day."""

    def __init__(self, settings: Settings, fetch=get_json, now=None):
        self.s = settings
        self._fetch = fetch
        self._now = now or (lambda: datetime.now().astimezone())
        self._cache: dict[tuple[str, date], tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, date], threading.Lock] = {}

    def _day(self, which: str, day: date) -> dict:
        """{"times": [local hour, ...], "points": [(lat, lon)], <variable>: [[hour][point]]}"""
        key = (which, day)
        today = self._now().astimezone(TZ).date()
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < (TTL_TODAY_S if day >= today else TTL_PAST_S):
                return hit[1]
            lock = self._inflight.setdefault(key, threading.Lock())
        with lock:  # one request per grid and day, however many ask at once
            with self._lock:
                hit = self._cache.get(key)
                if hit and time.time() - hit[0] < (TTL_TODAY_S if day >= today else TTL_PAST_S):
                    return hit[1]
            data = self._request(which, day)
            with self._lock:
                self._cache[key] = (time.time(), data)
                if len(self._cache) > CACHE_DAYS:
                    oldest = min(self._cache, key=lambda k: self._cache[k][0])
                    self._cache.pop(oldest, None)
            return data

    def _request(self, which: str, day: date) -> dict:
        if which == "air":
            pts, url = lattice(AIR_LATS, AIR_LONS), self.s.air_quality_url
            hourly = ",".join(AIR_VARS.values())
        elif which == "temperature":
            pts, url = lattice(AIR_LATS, AIR_LONS), self.s.open_meteo_url
            hourly = "temperature_2m"
        else:
            pts, url = lattice(WIND_LATS, WIND_LONS), self.s.open_meteo_url
            hourly = "wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        params = {
            "latitude": ",".join(f"{p[0]:.3f}" for p in pts),
            "longitude": ",".join(f"{p[1]:.3f}" for p in pts),
            "hourly": hourly, "timezone": TIMEZONE,
            "start_date": day.isoformat(), "end_date": day.isoformat(),
        }
        if which == "wind":
            params["wind_speed_unit"] = "ms"
        raw = self._fetch(url, user_agent=self.s.user_agent, params=params, timeout=20)
        items = raw if isinstance(raw, list) else [raw]
        if len(items) != len(pts):
            raise ValueError(f"Open-Meteo returned {len(items)} points for {len(pts)}")
        times = [t[:16] for t in items[0]["hourly"]["time"]]
        out: dict = {"times": times, "points": pts}
        for name in hourly.split(","):
            out[name] = [[it["hourly"][name][h] for it in items] for h in range(len(times))]
        return out

    def hour(self, which: str, at: datetime) -> dict | None:
        """The lattice's values at the local hour holding `at`: {"points", "time", <var>:
        [per point]}. None when Open-Meteo has nothing for that hour."""
        local = at.astimezone(TZ)
        if local.date() < self._now().astimezone(TZ).date() - timedelta(days=PAST_DAYS):
            return None
        data = self._day(which, local.date())
        stamp = local.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
        if stamp not in data["times"]:
            return None
        k = data["times"].index(stamp)
        out = {"points": data["points"], "time": stamp}
        for name, rows in data.items():
            if name in ("times", "points"):
                continue
            out[name] = rows[k]
        return out


def bilinear(lats: tuple[float, ...], lons: tuple[float, ...], values: list[float | None],
             lat: float, lon: float) -> float | None:
    """A value at (lat, lon) from a regular lattice (row by row from the south), clamped to its
    edge outside it. Missing corners fall back to the mean of the ones there are."""
    def idx(axis: tuple[float, ...], v: float) -> tuple[int, float]:
        if v <= axis[0]:
            return 0, 0.0
        if v >= axis[-1]:
            return len(axis) - 2, 1.0
        for i in range(len(axis) - 1):
            if axis[i] <= v <= axis[i + 1]:
                return i, (v - axis[i]) / (axis[i + 1] - axis[i])
        return len(axis) - 2, 1.0

    i, fy = idx(lats, lat)
    j, fx = idx(lons, lon)
    n = len(lons)
    corners = [(values[i * n + j], (1 - fy) * (1 - fx)), (values[i * n + j + 1], (1 - fy) * fx),
               (values[(i + 1) * n + j], fy * (1 - fx)), (values[(i + 1) * n + j + 1], fy * fx)]
    known = [(v, w) for v, w in corners if v is not None]
    if not known:
        return None
    total = sum(w for _, w in known)
    if total <= 0:
        return sum(v for v, _ in known) / len(known)
    return sum(v * w for v, w in known) / total

"""The environment twin's one service, shared by the page's endpoints, the tools and the
inspector (`agent.environment`): station readings at a moment, the surfaces, the wind field,
the rain radar's frames, sun hours and shade, and the readout at any point."""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from collector import interpret

from chatbot.environment import sun as S
from chatbot.environment.shade import NO_DATA, Shade, downsample, encode_grid
from chatbot.environment.surfaces import METRICS, Field, band_of, build_field
from chatbot.environment.weather import WIND_LATS, WIND_LONS, ModelGrids, bilinear
from chatbot.settings import TIMEZONE, Settings
from chatbot.web import WebError, get_json

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

AIR_TYPE = "smartsense"
# What a station reads, by the interpreter's detail keys.
STATION_KEYS = ("caqi", "pm2_5", "pm10", "no2", "o3", "noise_db", "temperature_c")
# The memory's raw keys behind them (history.HISTORY_KEYS plus history.AIR_KEYS).
RAW_KEYS = ("caqi", "caqi_pollutant", "Sound_level", "PM2.5", "PM10", "NO2", "O3", "T", "RH")
STATION_LOOKBACK = timedelta(hours=3)
FIELD_TTL_LIVE_S = 60
FIELD_TTL_PAST_S = 3600
RADAR_URL = "https://api.rainviewer.com/public/weather-maps.json"
RADAR_TTL_S = 300
RADAR_MAX_ZOOM = 7  # RainViewer's free tiles stop at zoom 7; the map stretches them beyond
SUN_GRID_MAX = 256  # cells across a sun hours grid before it is averaged down
SOURCES = {
    "stations": {"name": "Smart Nicosia air stations", "url": "https://nokia.smartnicosia.eu/api-docs"},
    "air_model": {"name": "Open-Meteo air quality (CAMS Europe)",
                  "url": "https://open-meteo.com/en/docs/air-quality-api"},
    "weather_model": {"name": "Open-Meteo weather forecast", "url": "https://open-meteo.com/en/docs"},
    "dls": {"name": "DLS 3D buildings (INSPIRE)", "url": "https://eservices.dls.moi.gov.cy/inspire/"},
    "radar": {"name": "RainViewer radar", "url": "https://www.rainviewer.com/api.html"},
}


def wind_from(u: float, v: float) -> float:
    """The compass direction a wind blows from, given where it moves (u east, v north)."""
    return (math.degrees(math.atan2(-u, -v)) + 360) % 360


COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west",
           "north-west")


def compass(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


class Environment:
    def __init__(self, settings: Settings, city=None, moments=None, dls=None, memory=None,
                 models: ModelGrids | None = None, fetch=get_json):
        self.s = settings
        self.city = city
        self.moments = moments
        self.memory = memory
        self.shade = Shade(dls)
        self.models = models or ModelGrids(settings, fetch=fetch)
        self._fetch = fetch
        self._fields: dict[tuple, tuple[float, Field, str | None, list[str]]] = {}
        self._radar: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def now(self) -> datetime:
        if self.city is not None:
            return self.city.now()
        return datetime.now(timezone.utc)

    # -- the stations ----------------------------------------------------------------------------

    def stations(self, at: datetime | None) -> tuple[list[dict], str | None]:
        """Each air station's readings at a moment (None: now), by the live rules: a station
        that had gone quiet by then is left out. Returns (stations, a note when none)."""
        if self.city is None:
            return [], "the city's stations are not connected"
        if at is None:
            try:
                states = self.city.states(AIR_TYPE)
            except Exception as e:  # noqa: BLE001 - the model still makes a surface
                return [], f"the city's air stations didn't answer ({e})"
            out = [self._row(s.device, s.label, s.interp) for s in states
                   if not s.interp.stale and s.interp.state != "unknown"]
            return out, None if out else "no air station has reported in the last hours"
        return self._stations_at(at)

    @staticmethod
    def _row(device, label: str, interp) -> dict:
        d = interp.detail
        name = label.removeprefix("Air station ").strip() or device.name
        return {"id": device.name, "name": name, "lat": device.lat, "lon": device.lon,
                **{k: d.get(k) for k in STATION_KEYS if d.get(k) is not None},
                "as_of": interp.as_of.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if interp.as_of else None}

    def _stations_at(self, at: datetime) -> tuple[list[dict], str | None]:
        if self.memory is None or self.moments is None:
            return [], "the twin's memory is off, so the stations' past readings aren't known"
        devices = self.moments.devices(AIR_TYPE)
        names = [d.device.name for d in devices]
        end = int(at.timestamp() * 1000)
        start = int((at - STATION_LOOKBACK).timestamp() * 1000)
        try:
            records = self.memory.readings(names, RAW_KEYS, start, end)
        except Exception as e:  # noqa: BLE001 - say so rather than guess
            return [], f"the twin's memory couldn't be read ({e})"
        out = []
        for d in devices:
            recs = records.get(d.device.name)
            if not recs:
                continue
            interp = interpret.interpret(AIR_TYPE, interpret.latest_from_records(recs), at)
            if interp.stale or interp.state == "unknown":
                continue
            out.append(self._row(d.device, d.label, interp))
        return out, None if out else "the twin's memory holds no station readings for that moment"

    # -- surfaces --------------------------------------------------------------------------------

    def field(self, metric: str, at: datetime | None) -> tuple[Field, str | None, list[str]]:
        """A metric's field at a moment (None: now), with why the model is missing (if it is)
        and notes on the stations."""
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}; one of {sorted(METRICS)}")
        moment = at.replace(second=0, microsecond=0) if at else None
        key = (metric, moment)
        ttl = FIELD_TTL_LIVE_S if at is None else FIELD_TTL_PAST_S
        with self._lock:
            hit = self._fields.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1], hit[2], list(hit[3])
        rows, note = self.stations(at)
        field, problem = build_field(metric, rows, self.models, at or self.now())
        notes = [note] if note else []
        with self._lock:
            self._fields[key] = (time.time(), field, problem, notes)
            if len(self._fields) > 200:
                self._fields.pop(next(iter(self._fields)))
        return field, problem, notes

    def surface(self, metric: str, at: datetime | None) -> dict:
        """What the page draws for a metric at a moment: the grid of estimates and how sure
        each is, the stations, the bands and where it all comes from."""
        field, problem, notes = self.field(metric, at)
        m = field.metric
        if not field.known:
            reason = problem or (notes[0] if notes else "nothing is known for that moment")
            return {"metric": metric, "label": m.label, "unit": m.unit, "known": False,
                    "reason": reason, "at": _local(at)}
        grid = field.grid()
        stations = []
        for s in field.stations:
            est = field.at(s.lat, s.lon)
            stations.append({"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon,
                             "value": round(s.value, m.decimals),
                             "model": round(s.model, m.decimals) if s.model is not None and field.has_model else None,
                             "band": band_of(m, s.value),
                             "estimate": round(est["value"], m.decimals) if est["value"] is not None else None})
        hi = max(stations, key=lambda r: r["value"]) if stations else None
        return {
            "metric": metric, "label": m.label, "unit": m.unit, "known": True, "at": _local(at),
            "model_hour": field.model["time"] if field.has_model and field.model else None,
            "grid": grid, "stations": stations,
            "bands": [{"to": None if math.isinf(top) else top, "label": name} for top, name in m.bands],
            "background": "model" if field.has_model else "stations",
            # How far the regional model was from the stations, which the surface corrects.
            "model_bias": round(field.bias, m.decimals) if field.has_model else None,
            "spread": round(field.spread, m.decimals),
            "reach_m": m.reach_m,
            "highest_station": hi,
            "notes": notes + ([problem] if problem and m.model else []),
            "sources": [SOURCES["stations"]] + (
                [SOURCES["air_model"] if m.model != "temperature" else SOURCES["weather_model"]]
                if field.has_model else []),
        }

    def estimate(self, metric: str, lat: float, lon: float, at: datetime | None) -> dict | None:
        field, _, _ = self.field(metric, at)
        if not field.known:
            return None
        m = field.metric
        est = field.at(lat, lon)
        if est["value"] is None or (not field.has_model and est["confidence"] < 0.06):
            return {"metric": metric, "label": m.label, "unit": m.unit, "value": None,
                    "why": "no station is near enough to say" if not field.has_model else None,
                    "nearest": est["nearest"]}
        return {"metric": metric, "label": m.label, "unit": m.unit,
                "value": round(est["value"], m.decimals),
                "uncertainty": round(est["uncertainty"], m.decimals if m.decimals else 0),
                "confidence": round(est["confidence"], 2), "band": band_of(m, est["value"]),
                "from": ("the nearest station" if est["confidence"] > 0.7 else
                         "the stations and the model" if field.has_model and est["confidence"] > 0.15 else
                         "the model" if field.has_model else "the stations, far off"),
                "nearest": est["nearest"]}

    # -- wind --------------------------------------------------------------------------------------

    def wind(self, at: datetime | None) -> dict:
        """The modelled wind at 10 m on its lattice, for the particles: u (east) and v (north)
        in m/s, and where it blows from."""
        h = self.models.hour("wind", at or self.now())
        if h is None:
            return {"known": False, "reason": "the weather model has no wind for that hour", "at": _local(at)}
        u, v, speeds, dirs = [], [], h["wind_speed_10m"], h["wind_direction_10m"]
        for sp, d in zip(speeds, dirs):
            if sp is None or d is None:
                u.append(None)
                v.append(None)
                continue
            r = math.radians(d)
            u.append(round(-sp * math.sin(r), 2))
            v.append(round(-sp * math.cos(r), 2))
        known = [s for s in speeds if s is not None]
        mid = len(speeds) // 2
        centre = {"speed_ms": speeds[mid], "from_deg": dirs[mid],
                  "from": compass(dirs[mid]) if dirs[mid] is not None else None,
                  "gusts_ms": h.get("wind_gusts_10m", [None] * len(speeds))[mid]}
        return {"known": True, "at": _local(at), "model_hour": h["time"],
                "lats": list(WIND_LATS), "lons": list(WIND_LONS), "u": u, "v": v,
                "speed_ms": speeds, "from_deg": dirs,
                "max_ms": max(known) if known else None, "centre": centre,
                "sources": [SOURCES["weather_model"]]}

    def wind_at(self, lat: float, lon: float, at: datetime | None) -> dict | None:
        w = self.wind(at)
        if not w.get("known"):
            return None
        u = bilinear(WIND_LATS, WIND_LONS, w["u"], lat, lon)
        v = bilinear(WIND_LATS, WIND_LONS, w["v"], lat, lon)
        if u is None or v is None:
            return None
        speed = math.hypot(u, v)
        frm = wind_from(u, v)
        return {"speed_ms": round(speed, 1), "speed_kmh": round(speed * 3.6), "from_deg": round(frm),
                "from": compass(frm)}

    # -- rain radar ----------------------------------------------------------------------------

    def radar(self) -> dict:
        """RainViewer's radar frames of the last two hours (10 minutes apart), with the tile
        address to draw each. Cached for five minutes, so pages share one request."""
        with self._lock:
            if self._radar and time.time() - self._radar[0] < RADAR_TTL_S:
                return self._radar[1]
        try:
            raw = self._fetch(RADAR_URL, user_agent=self.s.user_agent, timeout=15)
            host = raw["host"]
            frames = [{"time": int(f["time"]) * 1000, "path": f["path"],
                       "tiles": f"{host}{f['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"}
                      for f in raw.get("radar", {}).get("past", [])]
            out = {"known": bool(frames), "frames": frames, "max_zoom": RADAR_MAX_ZOOM,
                   "reason": None if frames else "the radar has no recent frames",
                   "source": SOURCES["radar"]}
        except (WebError, KeyError, TypeError, ValueError) as e:
            out = {"known": False, "frames": [], "reason": f"the rain radar isn't answering ({e})",
                   "source": SOURCES["radar"]}
        with self._lock:
            self._radar = (time.time(), out)
        return out

    def radar_status(self) -> tuple[float, dict] | None:
        """When the radar was last read and what it said, without asking it (None: not yet)."""
        with self._lock:
            return self._radar

    # -- sun and shade ---------------------------------------------------------------------------

    def sun_hours(self, lat: float, lon: float, radius_m: float, day: date) -> dict:
        """Hours of direct sun per cell over a day around a point: the grid (quarter hours,
        255 on a roof) and the day's profile."""
        area = self.shade.area(lat, lon, radius_m)
        res = area.sun_hours(day)
        q = res["quarters"]
        k = max(1, math.ceil(q.shape[0] / SUN_GRID_MAX))
        grid_q = downsample(q.astype(float), k)
        roofs = downsample(res["roofs"].astype(float), k) > 0.5
        coded = grid_q.round().clip(0, 254).astype("uint8")
        coded[roofs] = NO_DATA
        open_hours = res["hours"][~res["roofs"]]
        times = S.sun_times(day, lat, lon)
        return {
            "date": day.isoformat(), "center": [lon, lat], "radius_m": radius_m,
            "bounds": area.bounds(), "nx": int(coded.shape[1]), "ny": int(coded.shape[0]),
            "cell_m": round(area.cell * k, 2), "grid": encode_grid(coded), "unit": "quarter_hours",
            "profile": res["profile"], "sun": times,
            "open_cells": res["open_cells"],
            "hours": _hours_summary(open_hours, times["daylight_h"]),
            "tiles_missing": area.tiles_missing,
        }

    def shade_grid(self, lat: float, lon: float, radius_m: float, at: datetime) -> dict:
        """Which cells of open ground are in shade at a moment (0 sun, 1 shade, 255 roof)."""
        area = self.shade.area(lat, lon, radius_m)
        sun = S.position(at, lat, lon)
        mask = area.shade(at)
        roofs = area.roofs
        coded = np.ones(roofs.shape, dtype="uint8") if mask is None else mask.astype("uint8")
        k = max(1, math.ceil(coded.shape[0] / SUN_GRID_MAX))
        coded = (downsample(coded.astype(float), k) > 0.5).astype("uint8")
        roof_k = downsample(roofs.astype(float), k) > 0.5
        coded[roof_k] = NO_DATA
        open_n = int((~roofs).sum())
        shaded = open_n if mask is None else int(mask.sum())
        return {"at": _local(at), "center": [lon, lat], "radius_m": radius_m,
                "bounds": area.bounds(), "nx": int(coded.shape[1]), "ny": int(coded.shape[0]),
                "cell_m": round(area.cell * k, 2), "grid": encode_grid(coded), "unit": "shade",
                "sun_up": mask is not None,
                "sun": {"altitude_deg": round(sun.altitude, 1), "bearing_deg": round(sun.bearing),
                        "from": compass(sun.bearing)},
                "shade_pct": round(100 * shaded / open_n, 1) if open_n else None,
                "tiles_missing": area.tiles_missing}

    # -- what it is like at a point ----------------------------------------------------------

    def spot(self, lat: float, lon: float, at: datetime | None) -> dict:
        """Everything the environment twin knows at a point and a moment."""
        when = at or self.now()
        local = when.astimezone(TZ)
        sun = S.position(when, lat, lon)
        out: dict = {"lat": lat, "lon": lon, "at": _local(at),
                     "sun": {"altitude_deg": round(sun.altitude, 1), "bearing_deg": round(sun.bearing),
                             "from": compass(sun.bearing), "up": sun.up}}
        try:
            area = self.shade.area(lat, lon, 30)
            idx = area.point_index(lon, lat)
            roof = bool(idx and area.roofs[idx])
            mask = area.shade(when)
            out["sun"]["shade"] = None if mask is None else (False if roof else bool(mask[idx]) if idx else False)
            out["sun"]["on_roof"] = roof
            day = area.sun_hours(local.date())
            out["sun"]["hours_today"] = float(day["hours"][idx]) if idx and not roof else None
            out["sun"]["day"] = [{"time": p["time"][11:], "altitude_deg": p["sun_altitude_deg"],
                                  "sun": area.lit(idx, _mid(p["time"]))}
                                 for p in day["profile"]] if idx and not roof else None
            out["sun"]["times"] = S.sun_times(local.date(), lat, lon)
        except Exception as e:  # noqa: BLE001 - the rest of the readout still stands
            log.info("shade at %s,%s failed: %s", lat, lon, e)
            out["sun"]["shade"] = None
            out["sun"]["error"] = "the 3D buildings around here couldn't be read"
        out["air"] = [e for m in ("caqi", "pm2_5", "no2", "noise_db")
                      if (e := self._safe_estimate(m, lat, lon, at))]
        out["temperature"] = self._safe_estimate("temperature_c", lat, lon, at)
        try:
            out["wind"] = self.wind_at(lat, lon, at)
        except Exception as e:  # noqa: BLE001
            log.info("wind at %s,%s failed: %s", lat, lon, e)
            out["wind"] = None
        return out

    def _safe_estimate(self, metric: str, lat: float, lon: float, at: datetime | None) -> dict | None:
        try:
            return self.estimate(metric, lat, lon, at)
        except Exception as e:  # noqa: BLE001 - one missing reading leaves the others
            log.info("estimate %s failed: %s", metric, e)
            return None


def _mid(stamp: str) -> datetime:
    """The middle of the 15 minutes from a local stamp, where they read the sun."""
    return datetime.fromisoformat(stamp).replace(tzinfo=TZ) + timedelta(minutes=7.5)


def _hours_summary(hours, daylight_h: float) -> dict:
    if not len(hours):
        return {"mean": None, "min": None, "max": None, "daylight_h": daylight_h}
    return {"mean": round(float(np.mean(hours)), 1), "min": round(float(np.min(hours)), 2),
            "max": round(float(np.max(hours)), 2), "daylight_h": daylight_h,
            "under_2h_pct": round(100 * float((hours < 2).mean()), 1),
            "over_6h_pct": round(100 * float((hours >= 6).mean()), 1)}


def _local(at: datetime | None) -> str | None:
    return at.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if at else None

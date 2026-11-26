"""Air, noise and temperature as surfaces over Nicosia, with honest uncertainty.

Seven Smart Nicosia stations measure the air and the noise; a regional model (CAMS through
Open-Meteo, about 10 km cells) knows the air and the temperature everywhere, coarsely. A
surface lays one over the other:

    estimate(x) = model(x) + sum_i w_i(x) * (station_i - model(station_i)) / (w0 + sum_i w_i(x))
    w_i(x) = exp(-d_i(x)^2 / (2 L^2))

so near a station the estimate is what the station measures, and far from every station it
falls back to the model (or, for what the model doesn't know, to the stations' median). The
confidence c(x) = sum w / (w0 + sum w) is 1 at a station and 0 far away; the page draws it as
opacity, so the map fades where the estimate is only the model, and the uncertainty is
sqrt(c * sensor^2 + (1 - c) * spread^2): the sensors' own error near them, the model's miss
(how far it is from today's stations) or the stations' spread away from them.

CAQI is the stations' own index (the model has no such scale) and noise is only measured, so
both are stations only and fade to nothing between stations; noise is very local (a street, a
square) and fades within a few hundred metres.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from chatbot.environment.weather import AIR_LATS, AIR_LONS, AIR_VARS, ModelGrids, bilinear

# The surface's extent: Greater Nicosia (south), lon_min, lat_min, lon_max, lat_max.
BBOX = (33.29, 35.115, 33.44, 35.215)
NX, NY = 90, 72  # about 150 m cells
PRIOR = 0.18  # w0: how firmly the background holds against a station (in station weights)
FADE_BELOW = 0.06  # stations-only surfaces are not drawn where the confidence is below this


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    model: str | None  # the background's variable: an AIR_VARS key, "temperature" or None
    reach_m: float  # L, how far a station's reading carries
    sensor: float  # the sensors' own error, as a share of the reading
    floor: float  # the least uncertainty there is, in the metric's unit
    # Bands for the colours and the legend: upper bound and name, rising.
    bands: tuple[tuple[float, str], ...]
    decimals: int = 1


METRICS: dict[str, Metric] = {
    "pm2_5": Metric("pm2_5", "PM2.5", "µg/m³", "pm2_5", 1200, 0.25, 2.0,
                    ((10, "Good"), (20, "Fair"), (25, "Moderate"), (50, "Poor"), (75, "Very poor"),
                     (math.inf, "Extremely poor"))),
    "pm10": Metric("pm10", "PM10", "µg/m³", "pm10", 1200, 0.25, 3.0,
                   ((20, "Good"), (40, "Fair"), (50, "Moderate"), (100, "Poor"), (150, "Very poor"),
                    (math.inf, "Extremely poor"))),
    "no2": Metric("no2", "NO₂", "µg/m³", "no2", 900, 0.2, 5.0,
                  ((40, "Good"), (90, "Fair"), (120, "Moderate"), (230, "Poor"), (340, "Very poor"),
                   (math.inf, "Extremely poor"))),
    "o3": Metric("o3", "O₃", "µg/m³", "o3", 2000, 0.15, 5.0,
                 ((50, "Good"), (100, "Fair"), (130, "Moderate"), (240, "Poor"), (380, "Very poor"),
                  (math.inf, "Extremely poor"))),
    "caqi": Metric("caqi", "Air quality index (CAQI)", "", None, 1500, 0.1, 3.0,
                   ((25, "Very good"), (50, "Good"), (75, "Moderate"), (100, "Poor"),
                    (math.inf, "Very poor")), 0),
    "noise_db": Metric("noise_db", "Noise", "dB", None, 250, 0.04, 1.5,
                       ((45, "Quiet"), (55, "Moderate"), (65, "Loud"), (75, "Very loud"),
                        (math.inf, "Harmful")), 0),
    "temperature_c": Metric("temperature_c", "Temperature", "°C", "temperature", 2500, 0.05, 0.5,
                            ((10, "Cold"), (18, "Cool"), (25, "Mild"), (32, "Warm"), (38, "Hot"),
                             (math.inf, "Very hot"))),
}
EU_BANDS_NOTE = "bands of the European Air Quality Index (EEA) for one hour"


def band_of(metric: Metric, value: float | None) -> str | None:
    if value is None:
        return None
    return next(name for top, name in metric.bands if value < top)


def _xy(lat0: float):
    kx = 111320 * math.cos(math.radians(lat0))
    return lambda lat, lon: ((lon - BBOX[0]) * kx, (lat - BBOX[1]) * 110574)


@dataclass
class Station:
    id: str
    name: str
    lat: float
    lon: float
    value: float
    model: float | None = None  # the background at the station


class Field:
    """One metric at one moment: the background and the stations, ready to read anywhere."""

    def __init__(self, metric: Metric, stations: list[Station], model: dict | None):
        self.metric = metric
        self.stations = [s for s in stations if s.value is not None and math.isfinite(s.value)]
        self.model = model  # {"points", "time", <var>: [per point]} or None
        self._xy = _xy((BBOX[1] + BBOX[3]) / 2)
        vals = [s.value for s in self.stations]
        self.median = statistics.median(vals) if vals else None
        self.var = (AIR_VARS.get(metric.model) if metric.model in AIR_VARS else
                    "temperature_2m" if metric.model == "temperature" else None)
        self.has_model = bool(model and self.var and any(v is not None for v in model[self.var]))
        # The regional model often misses a city's level as a whole (CAMS cells are 10 km wide):
        # when two or more stations agree, it is shifted by their mean miss first, so the
        # surface doesn't fall off a cliff between the stations and the countryside.
        self.bias = 0.0
        if self.has_model:
            raw = [s.value - m for s in self.stations
                   if (m := bilinear(AIR_LATS, AIR_LONS, self.model[self.var], s.lat, s.lon)) is not None]
            if len(raw) >= 2:
                self.bias = statistics.fmean(raw)
        for s in self.stations:
            s.model = self.background(s.lat, s.lon)
        residuals = [s.value - s.model for s in self.stations if s.model is not None]
        if self.has_model and residuals:
            # How far the corrected model is from today's stations: its miss where there is no
            # station.
            self.spread = max(metric.floor, math.sqrt(sum(r * r for r in residuals) / len(residuals)))
        elif len(vals) >= 2:
            self.spread = max(metric.floor, statistics.pstdev(vals))
        else:
            self.spread = max(metric.floor, 0.3 * abs(vals[0])) if vals else metric.floor

    @property
    def known(self) -> bool:
        return bool(self.stations) or self.has_model

    def background(self, lat: float, lon: float) -> float | None:
        """The model at a point (shifted by the stations' mean miss), or the stations' median
        for what the model doesn't know."""
        if self.has_model:
            v = bilinear(AIR_LATS, AIR_LONS, self.model[self.var], lat, lon)
            return None if v is None else v + self.bias
        return self.median

    def at(self, lat: float, lon: float) -> dict:
        """The estimate at a point with its uncertainty, how sure it is, and the nearest
        station."""
        x, y = self._xy(lat, lon)
        wsum = 0.0
        acc = 0.0
        nearest = None
        for s in self.stations:
            sx, sy = self._xy(s.lat, s.lon)
            d = math.hypot(x - sx, y - sy)
            w = math.exp(-d * d / (2 * self.metric.reach_m ** 2))
            wsum += w
            acc += w * (s.value - (s.model if s.model is not None else s.value))
            if nearest is None or d < nearest[1]:
                nearest = (s, d)
        bg = self.background(lat, lon)
        if bg is None:
            return {"value": None, "uncertainty": None, "confidence": 0.0, "nearest": None}
        value = bg + (acc / (PRIOR + wsum) if wsum else 0.0)
        conf = wsum / (PRIOR + wsum) if wsum else 0.0
        sensor = max(self.metric.floor, self.metric.sensor * abs(value))
        sigma = math.sqrt(conf * sensor ** 2 + (1 - conf) * self.spread ** 2)
        return {"value": value, "uncertainty": sigma, "confidence": conf,
                "nearest": {"id": nearest[0].id, "name": nearest[0].name,
                            "metres": round(nearest[1])} if nearest else None}

    def grid(self) -> dict:
        """The surface on the NX by NY grid (rows from the north), as the page draws it."""
        lon = np.linspace(BBOX[0], BBOX[2], NX + 1)
        lat = np.linspace(BBOX[3], BBOX[1], NY + 1)
        lonc = (lon[:-1] + lon[1:]) / 2
        latc = (lat[:-1] + lat[1:]) / 2
        LON, LAT = np.meshgrid(lonc, latc)
        kx = 111320 * math.cos(math.radians((BBOX[1] + BBOX[3]) / 2))
        X = (LON - BBOX[0]) * kx
        Y = (LAT - BBOX[1]) * 110574
        wsum = np.zeros_like(X)
        acc = np.zeros_like(X)
        for s in self.stations:
            sx, sy = self._xy(s.lat, s.lon)
            w = np.exp(-((X - sx) ** 2 + (Y - sy) ** 2) / (2 * self.metric.reach_m ** 2))
            wsum += w
            acc += w * (s.value - (s.model if s.model is not None else s.value))
        if self.has_model:
            bg = np.array([[_nan(self.background(la, lo)) for lo in lonc] for la in latc])
        else:
            bg = np.full_like(X, self.median if self.median is not None else np.nan)
        value = bg + np.where(wsum > 0, acc / (PRIOR + wsum), 0.0)
        conf = np.where(wsum > 0, wsum / (PRIOR + wsum), 0.0)
        if not self.has_model:  # stations only: nothing between them
            conf = np.where(conf < FADE_BELOW, 0.0, conf)
        d = self.metric.decimals
        flat = [None if not math.isfinite(v) else round(float(v), d) for v in value.ravel()]
        finite = [v for v, c in zip(flat, conf.ravel())
                  if v is not None and (self.has_model or c > 0.02)]
        return {
            "bbox": list(BBOX), "nx": NX, "ny": NY,
            "values": flat,
            "confidence": [int(round(100 * float(c))) for c in conf.ravel()],
            "min": min(finite) if finite else None, "max": max(finite) if finite else None,
        }


def _nan(v: float | None) -> float:
    return np.nan if v is None else v


def stations_from(readings: list[dict], key: str) -> list[Station]:
    """Stations with a reading of `key`, from [{id, name, lat, lon, <key>: value}]."""
    out = []
    for r in readings:
        v = r.get(key)
        if isinstance(v, (int, float)) and r.get("lat") is not None and r.get("lon") is not None:
            out.append(Station(r["id"], r.get("name") or r["id"], r["lat"], r["lon"], float(v)))
    return out


def build_field(metric_key: str, readings: list[dict], models: ModelGrids | None,
                at: datetime) -> tuple[Field, str | None]:
    """The field of a metric at `at` from station readings and the model. The second value is
    why the model is missing, when it should be there and isn't."""
    metric = METRICS[metric_key]
    model, problem = None, None
    if metric.model and models is not None:
        try:
            model = models.hour("air" if metric.model in AIR_VARS else "temperature", at)
            if model is None:
                problem = "the model has no values for that hour"
        except Exception as e:  # noqa: BLE001 - the stations alone still make a surface
            problem = f"the model didn't answer ({e})"
    return Field(metric, stations_from(readings, metric_key), model), problem

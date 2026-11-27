"""Environment tools: sun and shade from the 3D city, and the air, noise, temperature and wind
as surfaces over the city (chatbot/environment)."""

from __future__ import annotations

import math
from datetime import date, datetime, time

import statistics

from chatbot.environment.service import TZ, compass
from chatbot.environment.shade import MAX_RADIUS_M
from chatbot.environment.surfaces import METRICS
from chatbot.moments import OutOfRange, parse_at
from chatbot.store import point
from chatbot.tools.base import ToolContext, ToolError, ToolSet, local_now, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool

# Colours of the night map (the page recolours them for the day map); validated as one-hue
# ordinal ramps with the dataviz skill.
SUN_HOURS_BANDS = [(2, "under 2 h"), (4, "2 to 4 h"), (6, "4 to 6 h"), (8, "6 to 8 h"),
                   (10, "8 to 10 h"), (math.inf, "10 h or more")]
SUN_HOURS_COLORS = ["#714909", "#8f5f15", "#ae7521", "#ce8d2d", "#efa539", "#ffbe46"]
SHADE_COLORS = {"in shade": "#3987e5", "in sun": "#c98500"}
AIR_COLORS = ["#7e3261", "#974677", "#af598d", "#c96da3", "#e382bb", "#fd97d2"]
NOISE_COLORS = ["#006363", "#0e8484", "#27a7a8", "#3dcccc", "#53f2f2"]
TEMP_COLORS = ["#83c1ff", "#4c88d3", "#5b6577", "#c9593d", "#f07b5e", "#ff9e80"]
SURFACE_METRICS = [*METRICS, "wind"]


def band_colors(metric: str) -> list[str]:
    if metric == "noise_db":
        return NOISE_COLORS
    if metric == "temperature_c":
        return TEMP_COLORS
    return AIR_COLORS


def _env(ctx: ToolContext):
    if ctx.environment is None:
        raise ToolError("the environment twin isn't available on this server")
    return ctx.environment


def _date(text: str | None, ctx: ToolContext) -> date:
    if not text:
        return local_now(ctx).date()
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise ToolError("date must be YYYY-MM-DD") from None


def _hhmm(text: str) -> time:
    try:
        h, m = map(int, text.strip().split(":"))
        return time(h, m)
    except ValueError:
        raise ToolError("time must be HH:MM (local)") from None


def square(center: tuple[float, float], radius_m: float) -> list[list[float]]:
    lat, lon = center
    dlat = radius_m / 110574
    dlon = radius_m / (111320 * math.cos(math.radians(lat)))
    return [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]


def _sides(mask, roofs) -> dict:
    """The share of open ground in shade on each side of a square (its outer thirds)."""
    n = mask.shape[0]
    third = max(1, n // 3)
    parts = {"north": (slice(0, third), slice(None)), "south": (slice(n - third, n), slice(None)),
             "west": (slice(None), slice(0, third)), "east": (slice(None), slice(n - third, n))}
    out = {}
    for side, (r, c) in parts.items():
        open_ = ~roofs[r, c]
        total = int(open_.sum())
        out[side] = round(100 * float((mask[r, c] & open_).sum()) / total) if total else None
    return out


@tool("sun_and_shade",
      "Sun and shade on the ground around a square or a street, from the DLS 3D buildings "
      "(every building at its real height) and the sun's true path. With time (HH:MM, local): "
      "which open ground is in a building's shadow at that moment, how much, and on which "
      "side. Without time: the hours of direct sun each 2 m cell gets over the whole day (a "
      "sun hours map). Any date works, past or future (default today): 'in July' means 15 "
      "July, 'midsummer' 21 June, 'in winter' 21 December. Returns a layer_ref (show it with "
      "'area'; the page draws the grid with its own legend) and a data_ref of the day every "
      "15 minutes: time, sun_pct (the share of open ground in direct sun) and "
      "sun_altitude_deg, for an area chart of the day. Only buildings cast shade here: no "
      "trees, awnings or arcades.",
      obj({
          "place": {"type": "string", "description": "A square or street, e.g. 'Eleftheria Square'."},
          "lat": {"type": "number"},
          "lon": {"type": "number"},
          "radius_m": {"type": "number", "minimum": 30, "maximum": MAX_RADIUS_M,
                       "description": "Half the side of the square looked at (default 150)."},
          "date": {"type": "string", "description": "YYYY-MM-DD (default today)."},
          "time": {"type": "string", "description": "HH:MM local; leave out for the whole day."},
      }))
def h_sun_and_shade(ctx: ToolContext, a: dict) -> dict:
    env = _env(ctx)
    where = resolve_point(ctx, a.get("place"), a.get("lat"), a.get("lon"))
    if where is None:
        raise ToolError("give a place, or lat and lon")
    lat, lon, name = where
    radius = float(a.get("radius_m") or 150)
    day = _date(a.get("date"), ctx)
    hours = env.sun_hours(lat, lon, radius, day)  # the day's profile, in both modes
    times = hours["sun"]
    res: dict = {"place": name, "lat": round(lat, 6), "lon": round(lon, 6), "date": day.isoformat(),
                 "radius_m": radius, "sun": times}
    outline = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [square((lat, lon), radius)]},
               "properties": {"id": "area", "label": name, "state": "area", "color": "#ffae45"}}
    if a.get("time"):
        at = datetime.combine(day, _hhmm(a["time"]), TZ)
        grid = env.shade_grid(lat, lon, radius, at)
        area = env.shade.area(lat, lon, radius)
        mask = area.shade(at)  # kept on the area: shade_grid has just worked it out
        res |= {"mode": "moment", "time": at.strftime("%H:%M"),
                "sun_up": grid["sun_up"], "shade_pct": grid["shade_pct"],
                "sun_now": grid["sun"]}
        if mask is not None:
            res["shade_by_side_pct"] = _sides(mask, area.roofs)
        else:
            res["note"] = "the sun is down at that time: everything is in shade"
        raster = {"kind": "shade", **{k: grid[k] for k in ("bounds", "nx", "ny", "cell_m", "grid", "unit")},
                  "at": grid["at"], "date": day.isoformat()}
        title = f"Shade around {name}, {at.strftime('%d %b %H:%M')}"
        legend = dict(SHADE_COLORS)
    else:
        res |= {"mode": "day", "hours_of_direct_sun": hours["hours"],
                "note": "hours of direct sun per cell of open ground (roofs left out), "
                        "every 15 minutes from sunrise to sunset"}
        raster = {"kind": "sun_hours", **{k: hours[k] for k in ("bounds", "nx", "ny", "cell_m", "grid", "unit")},
                  "date": day.isoformat()}
        title = f"Sun hours around {name}, {day.strftime('%d %b %Y')}"
        legend = {label: c for (_, label), c in zip(SUN_HOURS_BANDS, SUN_HOURS_COLORS)}
    profile = hours["profile"]
    up = [p for p in profile if p["sun_altitude_deg"] > 8]  # the sun well up, not just rising
    if up:
        least = min(up, key=lambda p: p["sun_pct"])
        most = max(up, key=lambda p: p["sun_pct"])
        res["day"] = {"most_shade_at": least["time"][11:], "sun_pct_then": least["sun_pct"],
                      "most_sun_at": most["time"][11:], "sun_pct_most": most["sun_pct"]}
    if hours["tiles_missing"]:
        res["buildings_missing"] = "some 3D buildings around couldn't be read, so shade may be missing"
    layer = ctx.store.add_layer("sun", [outline], geometry="area", title=title, legend=legend)
    ctx.store.layers[layer]["meta"]["environment"] = raster
    rows = [{"time": p["time"], "sun_pct": p["sun_pct"], "sun_altitude_deg": p["sun_altitude_deg"]}
            for p in profile if p["sun_altitude_deg"] > -6]
    data = ctx.store.add_dataset("sun_day", rows, title=f"Sun on open ground around {name}, {day.isoformat()}")
    res |= {"layer_ref": layer, "data_ref": data, "data_rows": len(rows),
            "_summary": (f"{name}: {res['shade_pct']}% in shade at {res['time']}" if a.get("time")
                         else f"{name}: {hours['hours']['mean']} h of sun on average")}
    return res


def _at(ctx: ToolContext, text: str | None) -> datetime | None:
    try:
        return parse_at(text, ctx.city.now())
    except OutOfRange as e:
        raise ToolError(str(e)) from None


# Places worth naming a spot of the city by: not a bench, a cash machine or a bike stand.
LANDMARKS = {"park", "place_of_worship", "hospital", "monument", "museum", "theatre", "attraction",
             "historic_site", "gallery", "hotel", "bus_station", "viewpoint", "supermarket"}


def _place_near(ctx: ToolContext, lat: float, lon: float, stations: list[dict]) -> str | None:
    if ctx.entities is not None:
        near = ctx.entities.places_within(lat, lon, 400, limit=40)
        named = next((p for p in near if p.get("detail", "").replace(" ", "_") in LANDMARKS), None)
        if named:
            return f"near {named['name']}"
    if stations:
        s = min(stations, key=lambda s: (s["lat"] - lat) ** 2 + (s["lon"] - lon) ** 2)
        return f"near the {s['name']} station"
    return None


def _extremes(ctx: ToolContext, surf: dict) -> dict:
    """The highest and lowest estimates where the surface is reasonably sure (confidence at
    least half), with where they are."""
    g = surf["grid"]
    w, s, e, n = g["bbox"]
    best_hi = best_lo = None
    for k, (v, c) in enumerate(zip(g["values"], g["confidence"])):
        if v is None or c < 50:
            continue
        if best_hi is None or v > best_hi[0]:
            best_hi = (v, k, c)
        if best_lo is None or v < best_lo[0]:
            best_lo = (v, k, c)
    out = {}
    for name, hit in (("highest", best_hi), ("lowest", best_lo)):
        if hit is None:
            continue
        v, k, c = hit
        row, col = divmod(k, g["nx"])
        lat = n - (row + 0.5) * (n - s) / g["ny"]
        lon = w + (col + 0.5) * (e - w) / g["nx"]
        out[name] = {"value": v, "lat": round(lat, 5), "lon": round(lon, 5),
                     "where": _place_near(ctx, lat, lon, surf["stations"]), "confidence_pct": c}
    return out


@tool("environment_surface",
      "Air, noise or temperature as a smooth surface over Nicosia at a moment, or the wind. "
      "The city's air stations are laid over the Open-Meteo regional model (CAMS for the "
      "air, about 10 km cells), so the surface is the stations' reading near them and the "
      "model's (corrected by how far it is from the stations) away from them, with an "
      "uncertainty that grows with distance. metric: pm2_5, pm10, no2, o3 (µg/m³), caqi "
      "(the stations' own index, stations only), noise_db (stations only; noise is very "
      "local, so the surface stays close to them), temperature_c, or wind (the modelled "
      "wind at 10 m, drawn as moving particles). at: local time (default now; when "
      "city_clock in <context> shows a past moment, use it). Returns a layer_ref (show it "
      "with 'points': the page draws the surface faded where no station is near, and the "
      "stations as dots), a data_ref of the stations (id, name, value, model, estimate, "
      "band; linked to the map by id), the highest and lowest places with their "
      "uncertainty, and the bands.",
      obj({
          "metric": {"type": "string", "enum": SURFACE_METRICS},
          "at": {"type": "string", "description": "Local time, e.g. 2026-11-21T21:00 (default now)."},
      }, ["metric"]))
def h_environment_surface(ctx: ToolContext, a: dict) -> dict:
    env = _env(ctx)
    metric = a["metric"]
    at = _at(ctx, a.get("at"))
    when = (at or ctx.city.now()).astimezone(TZ)
    if metric == "wind":
        return _wind(ctx, env, at, when)
    surf = env.surface(metric, at)
    m = METRICS[metric]
    if not surf.get("known"):
        raise ToolError(f"nothing is known for {m.label} at {when:%d %b %H:%M}: {surf.get('reason')}")
    colors = band_colors(metric)
    bands = list(zip(surf["bands"], colors))
    feats = [point(s["lon"], s["lat"], {
        "id": s["id"], "label": f"{s['name']}: {s['value']:g} {m.unit}".strip(), "state": s["band"] or "unknown",
        "color": next((c for b, c in bands if b["label"] == s["band"]), "#a3a3a3"),
        metric: s["value"], "popup": f"{s['name']}\n{m.label} {s['value']:g} {m.unit}".strip()})
        for s in surf["stations"]]
    title = f"{m.label}{' (' + m.unit + ')' if m.unit else ''}, {'now' if at is None else when.strftime('%a %d %b %H:%M')}"
    legend = {b["label"]: c for b, c in bands}
    rests = surf.get("rests_on") or {}
    refs = rests.get("reference") or []
    extra = ["met_stations" if metric == "temperature_c" else "eea_air"] if refs else []
    layer = ctx.store.add_layer(f"env_{metric}", feats, geometry="point", title=title, legend=legend,
                                value_fields=[metric],
                                **({"sources": ["smart_nicosia", "open_meteo", *extra]} if extra else {}))
    ctx.store.layers[layer]["meta"]["environment"] = {
        "kind": "surface", **{k: surf[k] for k in ("metric", "label", "unit", "at", "grid", "bands",
                                                  "background")}}
    rows = [{"id": s["id"], "station": s["name"], "value": s["value"], "unit": m.unit,
             "model": s["model"], "estimate": s["estimate"], "band": s["band"]} for s in surf["stations"]]
    data = ctx.store.add_dataset(f"stations_{metric}", rows, title=f"{m.label} at the stations",
                                 link={"key": "id"})
    ex = _extremes(ctx, surf)
    res = {
        "metric": metric, "label": m.label, "unit": m.unit,
        "at": surf["at"] or "now", "model_hour": surf["model_hour"],
        "stations": len(rows),
        "highest_station": surf["highest_station"] and {k: surf["highest_station"][k]
                                                         for k in ("id", "name", "value", "band")},
        "highest_area": ex.get("highest"), "lowest_area": ex.get("lowest"),
        "bands": [f"{b['label']}: up to {b['to']}" if b["to"] is not None else f"{b['label']}: above"
                  for b in surf["bands"]],
        "uncertainty": {"near_a_station": f"about ±{_sensor(m, rows):g} {m.unit}".strip(),
                        "far_from_stations": f"about ±{round(math.hypot(_sensor(m, rows), surf['spread']), m.decimals or 0):g} {m.unit}".strip()},
        "background": ("the Open-Meteo model, corrected by the stations' mean difference "
                       f"({surf['model_bias']:+g} {m.unit})".strip()
                       if surf["background"] == "model" else
                       "the stations' median (the model doesn't measure this): the surface fades out between stations"),
        "layer_ref": layer, "data_ref": data,
        "_summary": f"{m.label} surface, {len(rows)} stations",
    }
    if refs or rests.get("left_out"):
        res["rests_on"] = {"reference_stations": [f"{r['name']} ({r['as_of']})" for r in refs],
                           "city_sensors": rests.get("city_sensors"),
                           "reference_left_out": [f"{r['name']}: {r['why']}" for r in rests.get("left_out") or []]}
    if surf["notes"]:
        res["notes"] = surf["notes"]
    return res


def _sensor(m, rows: list[dict]) -> float:
    """The sensors' own error at a typical reading."""
    typical = statistics.median([r["value"] for r in rows]) if rows else 0.0
    return round(max(m.floor, m.sensor * abs(typical)), m.decimals or 0)


def _wind(ctx: ToolContext, env, at: datetime | None, when: datetime) -> dict:
    w = env.wind(at)
    if not w.get("known"):
        raise ToolError(w.get("reason") or "no wind for that hour")
    feats, rows = [], []
    for k, (u, v, sp, d) in enumerate(zip(w["u"], w["v"], w["speed_ms"], w["from_deg"])):
        la, lo = w["lats"][k // len(w["lons"])], w["lons"][k % len(w["lons"])]
        if sp is None:
            continue
        rows.append({"id": f"wind{k}", "lat": la, "lon": lo, "speed_ms": sp, "speed_kmh": round(sp * 3.6),
                     "from_deg": d, "from": compass(d)})
        feats.append(point(lo, la, {"id": f"wind{k}", "label": f"{round(sp * 3.6)} km/h from the {compass(d)}",
                                    "state": "wind", "color": "#8798b0", "speed_ms": sp}))
    title = f"Wind, {'now' if at is None else when.strftime('%a %d %b %H:%M')}"
    layer = ctx.store.add_layer("wind", feats, geometry="point", title=title,
                                legend={}, value_fields=["speed_ms"], sources=["open_meteo"])
    ctx.store.layers[layer]["meta"]["environment"] = {"kind": "wind", "at": w["at"]}
    data = ctx.store.add_dataset("wind", rows, title="Modelled wind at 10 m", link={"key": "id"},
                                 sources=["open_meteo"])
    c = w["centre"]
    return {"metric": "wind", "at": w["at"] or "now", "model_hour": w["model_hour"],
            "over_nicosia": {"speed_kmh": round(c["speed_ms"] * 3.6) if c["speed_ms"] is not None else None,
                             "from": c["from"], "from_deg": c["from_deg"],
                             "gusts_kmh": round(c["gusts_ms"] * 3.6) if c.get("gusts_ms") is not None else None},
            "strongest_kmh_around": round(w["max_ms"] * 3.6) if w["max_ms"] is not None else None,
            "source": "Open-Meteo weather model, a 5 by 5 lattice about 55 km across",
            "layer_ref": layer, "data_ref": data,
            "_summary": f"wind {c['from']} {round((c['speed_ms'] or 0) * 3.6)} km/h"}


# -- measured weather and reference air (chatbot/environment/stations.py) ------------------------

MEASURED_METRICS = {
    # tool metric -> (station key, label, unit, which stations)
    "temperature_c": ("temperature_c", "Air temperature", "°C", "weather_station"),
    "rain": ("rain_10min_mm", "Rain", "mm", "weather_station"),
    "wind": ("wind_ms", "Wind at 10 m", "km/h", "weather_station"),
    "sunshine": ("radiation_wm2", "Sunshine (global radiation)", "W/m²", "weather_station"),
    "humidity": ("humidity_pct", "Humidity", "%", "weather_station"),
    "no2": ("no2", "NO₂", "µg/m³", "reference_air_station"),
    "o3": ("o3", "O₃", "µg/m³", "reference_air_station"),
    "pm10": ("pm10", "PM10", "µg/m³", "reference_air_station"),
}
STATION_COLORS = {"weather_station": "#8fb8e8", "reference_air_station": "#ffffff"}
AGREE_COLORS = {"agrees": "#4fbf8f", "somewhat off": "#e0b43a", "far off": "#ff8a3d", "not compared": "#8798b0"}
COMPARE_METRICS = ("no2", "o3", "pm10", "temperature_c")


def _model_there(env, metric: str, lat: float, lon: float, when: datetime) -> float | None:
    """The Open-Meteo model at a point and hour, for measured beside modelled."""
    from chatbot.environment.weather import AIR_LATS, AIR_LONS, AIR_VARS, bilinear

    try:
        if metric == "temperature_c":
            h = env.models.hour("temperature", when)
            var = "temperature_2m"
        elif metric in AIR_VARS:
            h = env.models.hour("air", when)
            var = AIR_VARS[metric]
        else:
            return None
    except Exception:  # noqa: BLE001 - the measurement stands without the model
        return None
    if not h or var not in h:
        return None
    v = bilinear(AIR_LATS, AIR_LONS, h[var], lat, lon)
    return None if v is None else round(v, 1)


def _band_color(metric: str, value: float | None) -> str:
    if value is None or metric not in METRICS:
        return "#8798b0"
    m = METRICS[metric]
    colors = band_colors(metric)
    k = next((i for i, (top, _) in enumerate(m.bands) if value < top), len(m.bands) - 1)
    return colors[min(k, len(colors) - 1)]


@tool("measured_stations",
      "What real instruments measure in and around Nicosia, now or at a moment: the Department of "
      "Meteorology's weather stations (Lefkosia in the centre, Athalassa and its park, KEPNIC, Mammari; "
      "air temperature at 1.2 m, humidity, rain every 10 minutes, wind at 10 m, sunshine) and the "
      "national network's two reference air stations (Nicosia traffic and residential; hourly NO2, O3 "
      "and PM10, two to three hours behind, not yet verified). This is ground truth beside the twin's "
      "model and its low-cost city sensors: use it for 'how hot is it really', 'did it rain', 'what do "
      "the official stations say'. metric: temperature_c, rain, wind, sunshine, humidity, no2, o3 or "
      "pm10 (default temperature_c). at: local time (default now; when city_clock in <context> shows a "
      "past moment, use it). hours: history for the chart (default 24, at most 72). Returns a layer_ref "
      "of the stations (show it with 'points'), a data_ref of the metric at each station over the hours "
      "(time, id, station, value; linked by id, temporal x: a line chart coloured by station), each "
      "station's newest reading with its age, and model_there, the Open-Meteo model at the same place "
      "and hour, to set measured beside modelled.",
      obj({
          "metric": {"type": "string", "enum": list(MEASURED_METRICS)},
          "at": {"type": "string", "description": "Local time, e.g. 2026-11-27T21:00 (default now)."},
          "hours": {"type": "integer", "minimum": 1, "maximum": 72},
      }))
def h_measured_stations(ctx: ToolContext, a: dict) -> dict:
    env = _env(ctx)
    metric = a.get("metric") or "temperature_c"
    key, label, unit, kind = MEASURED_METRICS[metric]
    at = _at(ctx, a.get("at"))
    when = (at or ctx.city.now()).astimezone(TZ)
    hours = int(a.get("hours") or 24)
    m = env.measured
    rows = m.readings_at(at, kind)
    rain = m.rain_last_hour(at) if metric == "rain" else {}
    feats, stations = [], []
    for r in rows:
        rd = r["readings"].get(key)
        value = None if rd is None else (rd["kmh"] if metric == "wind" else rd["value"])
        if metric == "rain":
            value = rain.get(r["id"])
        item = {"id": r["id"], "station": r["name"], "where": r["detail"], "value": value, "unit": unit,
                "as_of": rd["as_of"] if rd else None, "age_min": rd["age_min"] if rd else None}
        if rd and kind == "reference_air_station":
            item["hour"] = f"{rd['as_of'][11:16]} to {rd['until'][11:16]}"
        if metric == "wind" and r["readings"].get("wind_from_deg"):
            item["from"] = compass(r["readings"]["wind_from_deg"]["value"])
        if value is not None and metric in ("temperature_c", "no2", "o3", "pm10") and rd:
            item["model_there"] = _model_there(env, metric, r["lat"], r["lon"],
                                               datetime.fromisoformat(rd["as_of"]).replace(tzinfo=TZ))
            if item["model_there"] is not None:
                item["measured_minus_model"] = round(value - item["model_there"], 1)
        if value is None and not r["readings"]:
            item["note"] = "no reading recorded then"
        elif value is None:
            item["note"] = f"this station doesn't measure {label.lower()}"
        stations.append(item)
        color = ((_band_color(metric, value) if metric in METRICS else STATION_COLORS[kind])
                 if value is not None else "#8798b0")
        text = "no reading" if value is None else f"{value:g} {unit}"
        feats.append(point(r["lon"], r["lat"], {
            "id": r["id"], "label": f"{r['name']}: {text}", "state": "measured" if value is not None else "unknown",
            "color": color, **({metric: value} if value is not None else {}),
            "popup": f"{r['name']}\n{label} {text}" + (f"\n{item['as_of'][11:16]}" if item["as_of"] else "")}))
    known = [s for s in stations if s["value"] is not None]
    if not known and not any(r["readings"] for r in rows):
        what = "the weather stations" if kind == "weather_station" else "the reference air stations"
        raise ToolError(f"the twin holds no readings from {what} for {when:%d %b %H:%M}: they are recorded "
                        "from when the twin started reading them (the Met stations every 10 minutes; the air "
                        "stations' files hold weeks)")
    title = f"{label}, measured, {'now' if at is None else when.strftime('%a %d %b %H:%M')}"
    legend = ({b: c for (_, b), c in zip(METRICS[metric].bands, band_colors(metric))} if metric in METRICS
              else {"weather station": STATION_COLORS["weather_station"]})
    layer = ctx.store.add_layer(f"measured_{metric}", feats, geometry="point", title=title, legend=legend,
                                value_fields=[metric])
    # The chart: each station's readings over the hours (rain summed by the hour).
    end = at or ctx.city.now()
    series_rows = []
    for r in rows:
        pts = m.series(r["id"], key, end, hours)
        if metric == "rain":
            by_hour: dict[str, float] = {}
            for ts, v in pts:
                h = datetime.fromtimestamp(ts / 1000, tz=TZ).strftime("%Y-%m-%dT%H:00")
                by_hour[h] = round(by_hour.get(h, 0.0) + v, 1)
            series_rows += [{"time": h, "id": r["id"], "station": r["name"], "value": v}
                            for h, v in sorted(by_hour.items())]
            continue
        series_rows += [{"time": datetime.fromtimestamp(ts / 1000, tz=TZ).strftime("%Y-%m-%dT%H:%M"),
                         "id": r["id"], "station": r["name"],
                         "value": round(v * 3.6) if metric == "wind" else v} for ts, v in pts]
    data = ctx.store.add_dataset(f"measured_{metric}", series_rows,
                                 title=f"{label} at the stations, {hours} hours ({unit})", link={"key": "id"})
    res = {"metric": metric, "label": label, "unit": unit,
           "at": "now" if at is None else when.strftime("%Y-%m-%dT%H:%M"),
           "stations": stations, "layer_ref": layer, "data_ref": data, "data_rows": len(series_rows),
           "cadence": ("every 10 minutes, about 10 minutes behind" if kind == "weather_station"
                       else "hourly means, two to three hours behind, not yet verified"),
           "_summary": f"{label} at {len(known)} station{'s' if len(known) != 1 else ''}"}
    if metric == "rain":
        res["value_is"] = "rain in the hour up to the moment, mm"
    if not series_rows:
        res["note"] = "the twin's memory holds no history of these stations yet; the newest readings stand alone"
    return res


def _agreement(c: dict) -> str:
    if "bias" not in c:
        return "not compared"
    off = abs(c["bias_pct"]) if "bias_pct" in c else abs(c["bias"]) * 10  # 1 °C off counts as 10%
    return "agrees" if off < 10 else "somewhat off" if off < 25 else "far off"


def _find_sensor(sensors: list[tuple], text: str) -> tuple | None:
    t = text.strip().lower()
    for s in sensors:
        if t in (s[0].lower(), s[1].lower()) or (len(t) > 3 and t in s[1].lower()):
            return s
    return None


@tool("compare_with_reference",
      "How the city's low-cost air sensors (the 7 Smart Nicosia stations) agree with the official "
      "instruments over the past days, from the twin's memory: each sensor against its nearest "
      "reference air station for NO2, O3 and PM10 (the national network's traffic and residential "
      "stations), and for temperature against the nearest Met Department station. Gives the bias, the "
      "bias in % (pollutants), RMSE, correlation r, the hours paired and how far apart they stand; a "
      "sensor kilometres from its reference also differs for real (a busy street against a school "
      "yard), so say so. sensor: one city sensor's id or name for its hours beside the reference (else "
      "every sensor). metric: no2, o3, pm10 or temperature_c (default all; no2 with a sensor). days: 1 to "
      "28 (default 7). Returns a layer_ref (the city sensors coloured by how well they agree, the "
      "reference stations with them; show it with 'points') and a data_ref: without sensor, one row per "
      "sensor and metric (id, sensor, metric, bias_pct, bias, rmse, r, hours, reference; linked by id) "
      "for a bar chart of bias_pct; with sensor, the hours (time, series, value: the sensor and the "
      "reference) for a line chart.",
      obj({
          "sensor": {"type": "string", "description": "A city air sensor's id (e.g. f60f85f2babb) or name."},
          "metric": {"type": "string", "enum": list(COMPARE_METRICS)},
          "days": {"type": "integer", "minimum": 1, "maximum": 28},
      }))
def h_compare_with_reference(ctx: ToolContext, a: dict) -> dict:
    env = _env(ctx)
    sensors = env.city_sensors()
    if not sensors:
        raise ToolError("the city's air sensors aren't listed right now; try again in a minute")
    days = int(a.get("days") or 7)
    m = env.measured
    one = None
    if a.get("sensor"):
        one = _find_sensor(sensors, a["sensor"])
        if one is None:
            raise ToolError(f"no city air sensor is called {a['sensor']!r}; they are: "
                            + ", ".join(f"{s[1]} ({s[0]})" for s in sensors))
    metrics = (a["metric"],) if a.get("metric") else (("no2",) if one else COMPARE_METRICS)
    rows = m.compare([one] if one else sensors, metrics, days=days)
    feats, seen_refs = [], {}
    order = list(AGREE_COLORS)
    for s in ([one] if one else sensors):
        mine = [c for c in rows if c["sensor_id"] == s[0]]
        known = [_agreement(c) for c in mine if "bias" in c]
        worst = max(known, key=order.index) if known else "not compared"
        says = "; ".join(f"{c['label']}: {c.get('text') or c.get('note')}" for c in mine)
        feats.append(point(s[3], s[2], {"id": s[0], "label": f"{s[1]}: {worst}", "state": worst,
                                        "color": AGREE_COLORS[worst], "popup": f"{s[1]}\n{says}"}))
        for c in mine:
            seen_refs[c["reference_id"]] = c["reference"]
    for sid in seen_refs:
        st = m.station(sid)
        if st:
            feats.append(point(st.lon, st.lat, {"id": st.id, "label": f"{st.name} (reference)", "state": "reference",
                                                "color": STATION_COLORS[st.kind],
                                                "popup": f"{st.name}\nReference: {st.detail}"}))
    legend = dict(AGREE_COLORS) | {"reference": "#ffffff"}
    layer = ctx.store.add_layer("against_reference", feats, geometry="point",
                                title=f"City sensors against the reference, {days} days", legend=legend)
    if one:
        metric = metrics[0]
        ref, pairs = m.pairs(one, metric, days=days)
        name = ref.name if ref else "reference"
        chart = []
        for h, x, y in pairs:
            t = datetime.fromtimestamp(h / 1000, tz=TZ).strftime("%Y-%m-%dT%H:%M")
            chart += [{"time": t, "series": f"{one[1]} (city sensor)", "value": round(x, 1)},
                      {"time": t, "series": name, "value": round(y, 1)}]
        data = ctx.store.add_dataset(f"against_reference_{one[0]}", chart,
                                     title=f"{one[1]} beside {name}, {metric}")
    else:
        table = [{"id": c["sensor_id"], "sensor": c["sensor"], "metric": c["label"], "reference": c["reference"],
                  **{k: c.get(k) for k in ("bias_pct", "bias", "rmse", "r", "hours", "distance_m")}}
                 for c in rows]
        data = ctx.store.add_dataset("against_reference", table,
                                     title=f"City sensors against the reference, {days} days", link={"key": "id"})
    compared = [c for c in rows if "bias" in c]
    keep = ("sensor", "sensor_id", "metric", "reference", "distance_m", "hours", "bias", "bias_pct", "rmse", "r",
            "text", "note")
    res = {"days": days, "comparisons": [{k: c[k] for k in keep if c.get(k) is not None} for c in rows],
           "layer_ref": layer, "data_ref": data,
           "_summary": f"{len(compared)} of {len(rows)} comparisons made"}
    if not compared:
        res["note"] = ("the twin's memory doesn't yet hold enough hours of both the city sensors and the "
                       "reference stations; the Met stations' history starts when the twin starts reading them")
    return res


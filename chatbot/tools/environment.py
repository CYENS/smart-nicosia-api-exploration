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
        mask = area.shade(at)
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


def _place_near(ctx: ToolContext, lat: float, lon: float, stations: list[dict]) -> str | None:
    if ctx.entities is not None:
        near = ctx.entities.places_within(lat, lon, 400, limit=1)
        if near:
            return f"near {near[0]['name']}"
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
    layer = ctx.store.add_layer(f"env_{metric}", feats, geometry="point", title=title, legend=legend,
                                value_fields=[metric])
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


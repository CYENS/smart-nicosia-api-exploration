"""The measuring stations as the twin's entities: a weather station of the Department of
Meteorology (met-LEFKOSIA) and a reference air station of the national network (aq-CY0004A),
with their readings at the clock's moment, their day, the city sensors compared with them and
where it all comes from. The city's own air sensors (kind air_station) learn here how they
compare with the reference, for the assistant (get_entity); the page reads the same from
/api/environment/compare."""

from __future__ import annotations

from datetime import datetime, timedelta

from chatbot import entities as E
from chatbot.environment import stations as S
from chatbot.environment.surfaces import METRICS, band_of

E.KIND_LABELS["weather_station"] = "Weather station"
E.KIND_LABELS["reference_air_station"] = "Reference air station"
KINDS = ("weather_station", "reference_air_station")
TTL_S = 120
HEADLINE = {"weather_station": "temperature_c", "reference_air_station": "no2"}
WEATHER_COLOR = "#8fb8e8"
# The European Air Quality Index bands, as colours for a reference station's dot.
BAND_COLORS = {"Good": "#4fbf8f", "Fair": "#a3c853", "Moderate": "#e0b43a", "Poor": "#ff8a3d",
               "Very poor": "#ff5a4e", "Extremely poor": "#c2185b"}
BAND_TONES = {"Good": "ok", "Fair": "ok", "Moderate": "muted", "Poor": "warn", "Very poor": "alarm",
              "Extremely poor": "alarm"}
OPERATOR = {"weather_station": "Department of Meteorology, Republic of Cyprus",
            "reference_air_station": "Department of Labour Inspection (the national air network)"}
CADENCE = {"weather_station": "Every 10 minutes, about 10 minutes behind",
           "reference_air_station": "Hourly means, two to three hours behind, not yet verified"}
COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def compass(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def _measured(ents):
    env = getattr(ents, "environment", None)
    return getattr(env, "measured", None)


def _iso(local: str | None) -> str | None:
    """'2026-11-27T22:00' (Nicosia) -> '2026-11-27T22:00+02:00', as the page reads times."""
    return datetime.fromisoformat(local).replace(tzinfo=S.TZ).isoformat(timespec="minutes") if local else None


def _age_text(minutes: int | None) -> str:
    if minutes is None:
        return ""
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{minutes} minutes ago"
    return f"{round(minutes / 60)} hours ago"


def reading_text(row: dict, key: str) -> str | None:
    """'12.1 °C, 10 minutes ago' or 'NO₂ 86.4 µg/m³, 19:00 to 20:00'."""
    r = row["readings"].get(key)
    if r is None:
        return None
    if row["kind"] == "reference_air_station":
        return f"{r['label']} {r['value']:g} {r['unit']}, {r['as_of'][11:16]} to {r['until'][11:16]}"
    return f"{r['value']:g} {r['unit']}, {_age_text(r['age_min'])}"


def _state(row: dict, now: datetime) -> dict:
    kind = row["kind"]
    if not row["known"]:
        return {"value": "unknown", "label": "No reading then" if row.get("_past") else "No reading yet",
                "color": E.STATE_COLORS["unknown"], "tone": "muted", "stale": True,
                "reason": "nothing recorded within the last half hour" if kind == "weather_station"
                          else "nothing recorded within the last three hours",
                "local": None, "as_of": None, "age_s": None}
    key = HEADLINE[kind]
    r = row["readings"].get(key) or next(iter(row["readings"].values()))
    if kind == "weather_station":
        text = reading_text(row, key if key in row["readings"] else next(iter(row["readings"])))
        value, color, tone = "reporting", WEATHER_COLOR, "live" if (row["age_min"] or 0) <= 30 else "muted"
        label = text if key in row["readings"] else f"Reporting: {text}"
    else:
        band = band_of(METRICS[key], r["value"]) if key in METRICS else None
        value, color, tone = (band or "unknown").lower().replace(" ", "_"), BAND_COLORS.get(band, WEATHER_COLOR), BAND_TONES.get(band, "muted")
        label = f"{band}: {reading_text(row, key)}" if band else reading_text(row, key)
    return {"value": value, "label": label, "color": color, "tone": tone, "stale": False,
            "reason": CADENCE[kind], "local": None, "as_of": _iso(row["reported"]),
            "age_s": row["age_min"] * 60 if row["age_min"] is not None else None}


def _readings(row: dict) -> list[dict]:
    out = []
    for key, r in row["readings"].items():
        if key == "wind_ms":
            out.append({"key": "wind_kmh", "label": "Wind at 10 m", "value": r["kmh"], "unit": "km/h",
                        "as_of": r["as_of"], "stale": False})
            continue
        if key == "wind_2m_ms":
            out.append({"key": "wind_2m_kmh", "label": "Wind at 2 m", "value": round(r["value"] * 3.6),
                        "unit": "km/h", "as_of": r["as_of"], "stale": False})
            continue
        if key == "wind_from_deg":
            continue  # told in words among the attributes
        label = r["label"] if row["kind"] == "weather_station" else f"{r['label']}, {r['as_of'][11:16]} to {r['until'][11:16]}"
        out.append({"key": key, "label": label, "value": r["value"], "unit": r["unit"], "as_of": r["as_of"],
                    "stale": False})
    return out


def comparisons(ents, station_id: str | None = None, at: datetime | None = None) -> list[dict]:
    """How each city air sensor compares with its nearest reference over the past week, or
    only those compared with one station."""
    m = _measured(ents)
    env = getattr(ents, "environment", None)
    if m is None or env is None:
        return []
    rows = m.compare(env.city_sensors(), end=at)
    return [c for c in rows if station_id is None or c["reference_id"].lower() == station_id.lower()]


def build_station(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    m = _measured(ents)
    if m is None:
        return None
    info = m.station(eid)
    if info is None:
        return None
    now = at or m.now()
    row = next((r for r in m.readings_at(at, info.kind) if r["id"] == info.id), None)
    if row is None:
        return None
    row["_past"] = at is not None
    attributes = [E.attr("Operator", OPERATOR[info.kind]), E.attr("Station code", info.code),
                  E.attr("Where", info.detail), E.attr("Readings", CADENCE[info.kind])]
    if info.type:
        attributes.append(E.attr("Type", f"{info.type.capitalize()} station"))
    wd = row["readings"].get("wind_from_deg")
    if wd:
        attributes.append(E.attr("Wind from", f"the {compass(wd['value'])} ({wd['value']:g}°)"))
    if info.kind == "weather_station":
        rain = m.rain_last_hour(at).get(info.id)
        if rain is not None:
            attributes.append(E.attr("Rain in the last hour", f"{rain:g} mm"))
    centre_m = S.haversine_m(S.CENTRE[0], S.CENTRE[1], info.lat, info.lon)
    attributes.append(E.attr("From Eleftheria Square", S._dist(centre_m)))
    try:
        every = comparisons(ents, info.id, at)
    except Exception:  # noqa: BLE001 - the station stands without its comparison
        every = []
    compared = every
    relations = []
    # One line per city sensor, on the station's headline (NO2, or temperature): the inspector's
    # own section and the model read the rest.
    compared = [c for c in compared if c["metric"] == HEADLINE[info.kind] and "bias" in c]
    for c in sorted(compared, key=lambda c: ("bias" not in c, c["distance_m"])):
        relations.append({"relation": "compared", "label": f"City sensor, {c['label']}", "id": c["sensor_id"],
                          "kind": "air_station", "name": c["sensor"], "distance_m": c["distance_m"],
                          "detail": c.get("text") or c.get("note")})
    others = sorted((s for s in m.stations() if s.id != info.id),
                    key=lambda s: S.haversine_m(info.lat, info.lon, s.lat, s.lon))[:2]
    for o in others:
        relations.append({"relation": "nearest", "label": f"Nearby {E.KIND_LABELS[o.kind].lower()}", "id": o.id,
                          "kind": o.kind, "name": o.name, "lon": o.lon, "lat": o.lat,
                          "distance_m": round(S.haversine_m(info.lat, info.lon, o.lat, o.lon))})
    if info.kind == "weather_station":
        sources = [{"source": "met_stations", "name": "Department of Meteorology", "detail": S.CREDIT_MET,
                    "url": S.MET_PAGE, "as_of": _iso(row["reported"])}]
    else:
        sources = [{"source": "eea_air", "name": "National air quality network, through the EEA",
                    "detail": "hourly means, not yet verified; EEA, CC BY 4.0", "url": S.EEA_PAGE,
                    "as_of": _iso(row["reported"])}]
    if at is not None:
        sources.insert(0, {"source": "twin_memory", "name": "The twin's memory",
                           "detail": f"readings recorded up to {at.astimezone(S.TZ):%H:%M}", "as_of": _iso(row["reported"])})
    entity = E.new_entity(
        info.id, info.kind, info.name, subtitle=info.detail,
        geometry=E.point_geom(info.lon, info.lat), center=[info.lon, info.lat],
        state=_state(row, now), readings=_readings(row), attributes=attributes, relations=relations,
        sources=sources, as_of=_iso(row["reported"]), has_series=True)
    entity["station"] = {k: v for k, v in row.items() if not k.startswith("_")}
    entity["comparison"] = every
    entity["_for_model"] = {
        "measured": {k: f"{r['value']:g} {r['unit']} at {r['as_of'][11:16]}".strip() for k, r in row["readings"].items()},
        "operator": OPERATOR[info.kind], "cadence": CADENCE[info.kind],
        "city_sensors_compared": [{"sensor": c["sensor"], "id": c["sensor_id"], "metric": c["metric"],
                                   "says": c.get("text") or c.get("note")} for c in every],
    }
    entity["_source_ids"] = [sources[-1]["source"], *(["twin_memory"] if at is not None else [])]
    return entity


def station_at(ents, entity: dict, at: datetime) -> dict:
    """The station as the twin's memory recorded it at a moment of the city clock."""
    from chatbot.moments import label

    e = build_station(ents, entity["id"], None, at) or dict(entity)
    e["moment"] = {"at": at.isoformat(timespec="minutes"), "label": label(at),
                   "known": bool((e.get("station") or {}).get("known")), "applies": True}
    return e


def station_series(ents, entity: dict, end: datetime | None = None) -> dict | None:
    """The headline reading over the 24 hours up to `end`: temperature every 10 minutes at a
    weather station, NO2 hour by hour at an air station."""
    m = _measured(ents)
    if m is None:
        return None
    kind = entity["kind"]
    key = HEADLINE[kind]
    if kind == "weather_station" and key not in ((entity.get("station") or {}).get("readings") or {}):
        key = "rain_10min_mm"  # a rain gauge
    until = end or m.now()
    pts = m.series(entity["id"], key, until, 24)
    if len(pts) < 2:
        return None
    step = 10 if kind == "weather_station" else 60
    by = {S._local(ts): v for ts, v in pts}
    start = (until - timedelta(hours=24)).astimezone(S.TZ)
    start = start.replace(minute=start.minute - start.minute % step, second=0, microsecond=0)
    points = []
    t = start
    while t <= until:
        k = t.strftime("%Y-%m-%dT%H:%M")
        points.append({"t": k, "v": by.get(k)})
        t += timedelta(minutes=step)
    vals = [p["v"] for p in points if p["v"] is not None]
    if not vals:
        return None
    label, unit, _ = S.READING_INFO[key]
    out = {"metric": key, "label": label, "unit": unit, "draw": "line", "step_min": step, "points": points,
           "from": points[0]["t"], "to": points[-1]["t"], "summary": E.summary(vals), "source": "memory"}
    if key == "no2":
        out["threshold"] = {"value": 200, "label": "EU hourly limit 200 µg/m³"}
    return out


def city_sensor_details(ents, entity: dict, at: datetime | None) -> dict:
    """A city air sensor as the assistant reads it: how it compares with the reference."""
    try:
        comp = [c for c in comparisons(ents, None, at) if c["sensor_id"] == entity["id"]]
    except Exception:  # noqa: BLE001 - the sensor stands without it
        return entity
    if not comp:
        return entity
    e = dict(entity)
    e["_for_model"] = {**(entity.get("_for_model") or {}),
                       "against_reference": [{"metric": c["metric"], "reference": c["reference"],
                                              "reference_id": c["reference_id"], "says": c.get("text") or c.get("note"),
                                              **{k: c[k] for k in ("bias", "bias_pct", "rmse", "r", "hours") if k in c}}
                                             for c in comp]}
    e["_source_ids"] = list(dict.fromkeys([*(entity.get("_source_ids") or ["smart_nicosia"]), "eea_air",
                                           "met_stations", "twin_memory"]))
    return e


def search_items(ents) -> list[dict]:
    m = _measured(ents)
    if m is None:
        return []
    items = []
    for s in m.stations():
        group = "Weather stations" if s.kind == "weather_station" else "Air reference stations"
        words = (["weather station", "met", "meteorology", "temperature", "rain", "wind", "thermometer"]
                 if s.kind == "weather_station" else
                 ["air station", "reference", "official", "air quality", "no2", "pm10", "ozone", s.type])
        items.append(E.index_item(s.id, s.kind, s.name, group, s.detail, s.lat, s.lon,
                                  [s.name, s.code, s.detail, *words]))
    return items


for _kind in KINDS:
    E.register_kind(E.Kind(_kind, (lambda k: lambda i: i.lower().startswith("met-" if k == "weather_station" else "aq-"))(_kind),
                           build_station, ttl_s=TTL_S), first=True)
    E.register_series(_kind, station_series)
    E.register_at(_kind, station_at)
E.register_details("air_station", city_sensor_details)
E.register_search(search_items, {"Weather stations": 3, "Air reference stations": 2})

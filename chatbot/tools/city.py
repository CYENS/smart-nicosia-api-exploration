"""City data tools: the live overview, devices by kind and place, their history, one device,
alarms, traffic counts, the city's speed and the weather."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from collector.http import ApiError

from chatbot import context, history, memory_tools
from chatbot.city import KINDS
from chatbot.geo import haversine_m, norm
from chatbot.store import STATE_COLORS, legend_for, point
from chatbot.tools.base import (NEAR, TZ, ToolContext, ToolError, ToolSet, local_now, obj,
                                resolve_point)
from chatbot.web import WebError

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

KIND_STATES = {
    "street_light": ["on", "off", "offline", "unknown"],
    "parking_sensor": ["free", "occupied", "unknown"],
    "waste_bin": ["low", "medium", "high", "full", "unknown"],
    "air_station": ["very_low", "low", "medium", "high", "very_high", "unknown"],
    "info_screen": ["ok", "no_heartbeat", "not_reported", "unknown"],
    "camera": ["connected", "disconnected", "unknown"],
}
HISTORY_KINDS = ["street_light", "parking_sensor", "waste_bin", "air_station"]
HISTORY_STEPS = [5, 10, 15, 30, 60, 120, 180, 240, 360]


@tool("city_overview",
      "One-glance status of the whole city right now: street lights on/off/offline and the "
      "power they draw, parking sensors free/occupied, waste bins by fill level, air and noise "
      "at every station, info screens and cameras. Use it for 'how is the city doing' and as "
      "a first look before drilling down.",
      obj({}))
def h_city_overview(ctx: ToolContext, a: dict) -> dict:
    return {"as_of": local_now(ctx).strftime("%Y-%m-%d %H:%M"), **ctx.city.overview(),
            "_summary": "city overview"}


def filter_by_place(ctx: ToolContext, states: list, a: dict):
    """Apply the `street` and near_* filters. Returns (states, center or None, distance by id),
    nearest first when a center is given."""
    if a.get("street"):
        q = norm(a["street"])
        states = [s for s in states if q in norm(s.street) or q in norm(s.label)]
    center = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    dist: dict[str, float] = {}
    if center:
        radius = a.get("radius_m") or 500
        for s in states:
            if s.device.lat is not None:
                dist[s.device.name] = haversine_m(center[0], center[1], s.device.lat, s.device.lon)
        states = [s for s in states if dist.get(s.device.name, 1e12) <= radius]
        states.sort(key=lambda s: dist[s.device.name])
    return states, center, dist


def device_feature(s, summ: dict | None = None) -> dict | None:
    """A device as a map point coloured by its state (None when it has no position). The page's
    own device layers (routes/devices.py) draw the same points as find_devices."""
    if s.device.lat is None or s.device.lon is None:
        return None
    as_of = (summ or s.summary()).get("as_of")
    popup = f"{s.label}\n{s.interp.state} · {s.interp.reason}" + (f"\nas of {as_of}" if as_of else "")
    return point(s.device.lon, s.device.lat, {
        "id": s.device.name, "label": s.street or s.label, "state": s.interp.state,
        "color": STATE_COLORS.get(s.interp.state, STATE_COLORS["unknown"]), "popup": popup,
        **{k: v for k, v in s.values().items() if v is not None},
    })


@tool("find_devices",
      "Find smart-city devices by kind, state, street or distance from a place, with their "
      "live interpreted state. Returns counts, the nearest or first matches, a `layer_ref` "
      "(every match, ready for show_on_map) and a `data_ref` (rows for show_chart).\n"
      "Kinds and states: street_light on|off|offline|unknown (a lamp is 'on' only if it "
      "reported in the last 70 minutes; lamps are unpowered and silent by day); "
      "parking_sensor free|occupied (on-street bays; disabled_bay marks reserved bays); "
      "waste_bin low|medium|high|full (fill %, full is 80%+); air_station very_low..very_high "
      "(CAQI band) with pm2_5, pm10, no2, o3, noise_db, temperature_c; info_screen; camera.",
      obj({
          "kind": {"type": "string", "enum": list(KINDS)},
          "state": {"type": "string", "description": "Only devices in this state."},
          "street": {"type": "string",
                     "description": "Street name to match, e.g. 'Mnasiadou' (street lights, "
                                    "air stations and cameras carry street names)."},
          **NEAR,
          "include_stale": {"type": "boolean",
                            "description": "Keep sensors that have stopped reporting (default "
                                           "false: their state can't be trusted). Street "
                                           "lights are always kept, since they are silent by "
                                           "day."},
          "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                    "description": "How many matches to list in the reply (default 12). The "
                                   "map layer always holds every match."},
      }, ["kind"]))
def h_find_devices(ctx: ToolContext, a: dict) -> dict:
    kind = a["kind"]
    states = ctx.city.states(KINDS[kind])
    total = len(states)
    stale_dropped = 0
    if kind != "street_light" and not a.get("include_stale"):
        fresh = [s for s in states if not s.interp.stale]
        stale_dropped = len(states) - len(fresh)
        states = fresh
    if a.get("state"):
        want = a["state"].strip().lower()
        if want not in KIND_STATES[kind]:
            raise ToolError(f"state for {kind} must be one of {KIND_STATES[kind]}")
        states = [s for s in states if s.interp.state == want]
    states, center, dist = filter_by_place(ctx, states, a)

    features, rows = [], []
    for s in states:
        summ = s.summary()
        if center:
            summ["distance_m"] = round(dist[s.device.name])
        rows.append(summ)
        if (f := device_feature(s, summ)) is not None:
            features.append(f)
    by_state = Counter(s.interp.state for s in states)
    title = kind.replace("_", " ") + "s" + (f": {a['state']}" if a.get("state") else "")
    if center:
        title += f" near {center[2]}"
    layer = ctx.store.add_layer(f"{kind}_{a.get('state', 'all')}", features, geometry="point",
                                title=title, legend=legend_for(list(by_state)),
                                value_fields=sorted({k for s in states for k in s.values()}))
    data = ctx.store.add_dataset(kind, rows, title=title, link={"key": "id"})
    limit = a.get("limit") or 12
    age = ctx.city.snapshot_age_s(KINDS[kind])
    result = {
        "kind": kind, "devices_of_this_kind": total, "matched": len(states),
        "by_state": dict(by_state.most_common()),
        "left_out_stale": stale_dropped,
        "readings_fetched_min_ago": round(age / 60, 1) if age is not None else None,
        "layer_ref": layer, "data_ref": data,
        "items": rows[:limit],
    }
    if center:
        result["near"] = {"name": center[2], "lat": center[0], "lon": center[1],
                          "radius_m": a.get("radius_m") or 500}
    result["_summary"] = f"{len(states)} of {total} {kind.replace('_', ' ')}s"
    return result


def history_step(hours: int) -> int:
    for step in (15 if hours <= 24 else 30 if hours <= 48 else 60, *HISTORY_STEPS):
        if step >= 15 and hours * 60 // step <= history.MAX_FRAMES:
            return step
    return HISTORY_STEPS[-1]


@tool("get_history",
      "Replay how devices changed over the last hours or days as a map time-lapse: street "
      "lights switching on at dusk, parking filling up, bins filling, air and noise through "
      "the day. Every frame interprets the past readings with the same rules as "
      "find_devices. Up to 72 hours back the city API fills whatever the twin's memory "
      "lacks; further back (up to 31 days, `until` for a past window) only the memory "
      "answers, and moments it holds nothing for show as unknown. Returns "
      "a layer_ref (show it with show_on_map: 'points' coloured by state, or 'heatmap' / "
      "'columns_3d' with a value_field; a time slider with play appears on the map) and a "
      "data_ref with one row per frame: frame, time, label, a count per state, plus "
      "lit_power_kw (lights), occupancy_pct (parking), avg_level_pct (bins) or avg_caqi and "
      "avg_noise_db (air). A chart of that data_ref with x = time stays in sync with the "
      "slider. Filter by street or near a place like find_devices. source and "
      "coverage_pct say where the readings came from and how complete they are.",
      obj({
          "kind": {"type": "string", "enum": HISTORY_KINDS},
          "hours": {"type": "integer", "minimum": 1, "maximum": 744,
                    "description": "How long a window to replay (default 24)."},
          "until": {"type": "string",
                    "description": "End of the window, local time ('2026-11-21T23:00'); "
                                   "default now."},
          "step_minutes": {"type": "integer", "enum": HISTORY_STEPS,
                           "description": "Minutes between frames (default 15 up to 24 h, "
                                          "30 up to 48 h, 60 up to 8 days, then the "
                                          "smallest step that keeps 200 frames or fewer)."},
          "street": {"type": "string", "description": "Street name to match."},
          **NEAR,
      }, ["kind"]))
def h_get_history(ctx: ToolContext, a: dict) -> dict:
    kind = a["kind"]
    device_type = KINDS[kind]
    hours = a.get("hours") or 24
    step = a.get("step_minutes") or history_step(hours)
    if hours * 60 // step > history.MAX_FRAMES:
        raise ToolError(f"{hours} h every {step} min is more than {history.MAX_FRAMES} frames; "
                        "use a larger step_minutes")
    now = ctx.city.now()
    end = now
    if a.get("until"):
        until = memory_tools.parse_local(a["until"], end=True)
        if until > now + timedelta(minutes=1):
            raise ToolError("until is in the future; leave it out to replay up to now")
        end = min(until, now)
    states, center, _ = filter_by_place(ctx, ctx.city.states(device_type), a)
    states = [s for s in states if s.device.lat is not None]
    if not states:
        raise ToolError(f"no {kind.replace('_', ' ')}s match those filters")
    times = history.frame_times(end, hours, step)
    try:
        rd = history.readings(ctx.city, ctx.memory, device_type, [s.device.name for s in states],
                              history.HISTORY_KEYS[device_type], int(times[0].timestamp() * 1000),
                              int(end.timestamp() * 1000),
                              lookback_ms=int(history.LOOKBACK[device_type].total_seconds() * 1000))
    except history.NoHistory as e:
        raise ToolError(str(e)) from None
    states = [s for s in states if s.device.name in rd.records or rd.covered.get(s.device.name)]
    frame_states, frame_values = history.replay(device_type, states, rd.records, times, rd.covered)
    fields = [f for f, rows in frame_values.items()
              if any(v is not None for row in rows for v in row)]
    names = sorted({st for row in frame_states for st in row})
    index = {n: i for i, n in enumerate(names)}
    frames = {
        "times": [t.astimezone(TZ).strftime("%a %d %b %H:%M") for t in times],
        "iso": [t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") for t in times],
        "state_names": names,
        "states": [[index[st] for st in row] for row in frame_states],
        "values": {f: frame_values[f] for f in fields},
    }
    last = frame_states[-1]
    features = [point(s.device.lon, s.device.lat, {
        "id": s.device.name, "label": s.street or s.label, "name": s.label, "state": last[j],
        "color": STATE_COLORS.get(last[j], STATE_COLORS["unknown"]),
        **{f: frame_values[f][-1][j] for f in fields if frame_values[f][-1][j] is not None},
    }) for j, s in enumerate(states)]
    window = (f"last {hours} h" if end is now else
              f"{hours} h to {end.astimezone(TZ):%a %d %b %H:%M}")
    title = f"{kind.replace('_', ' ')}s, {window}" + (f" near {center[2]}" if center else "")
    layer = ctx.store.add_layer(f"{kind}_history", features, geometry="point", title=title,
                                legend=legend_for(names), value_fields=fields, frames=frames)
    rows = history.frame_rows(device_type, times, frame_states, frame_values)
    data = ctx.store.add_dataset(f"{kind}_history", rows, title=title,
                                 link={"key": "frame", "layer": layer})
    every = max(1, len(rows) // 24)
    sampled = rows[::every] + ([rows[-1]] if (len(rows) - 1) % every else [])
    return {
        "kind": kind, "devices": len(states), "hours": hours, "step_minutes": step,
        "frames": len(times), "from": rows[0]["label"], "to": rows[-1]["label"],
        "value_fields": fields, "layer_ref": layer, "data_ref": data,
        "rows_sampled": sampled, "source": rd.source, "coverage_pct": rd.coverage_pct(),
        **({"notes": rd.notes} if rd.notes else {}),
        "_summary": f"{len(states)} {kind.replace('_', ' ')}s × {len(times)} frames from "
                    f"{rd.source}",
    }


@tool("get_device",
      "Everything about one device by its id (e.g. 'YL1015', 'YW1394'): fresh readings, "
      "interpretation, attributes and location.",
      obj({"id": {"type": "string"}}, ["id"]))
def h_get_device(ctx: ToolContext, a: dict) -> dict:
    try:
        state, attrs = ctx.city.device_state(a["id"].strip())
    except KeyError:
        raise ToolError(f"no device named {a['id']!r} (ids look like YL1015, YP974, YW1394)")
    keep = ("name", "street", "city", "location", "postalcode", "lastActivityTime",
            "lastConnectTime", "lastDisconnectTime", "metadata", "make", "model")
    return {**state.summary(), "detail": state.interp.detail,
            "attributes": {k: v for k, v in attrs.items() if k in keep},
            "_summary": state.device.name}


def alarm_features(alarms: list[dict]) -> list[dict]:
    """Active alarms as map points at their devices (the page's Alarms layer draws the same)."""
    return [point(x["lon"], x["lat"], {
        "id": x["device"], "label": x["alarm"], "state": "alarm", "color": STATE_COLORS["alarm"],
        "popup": f"{x['device']} ({x['device_type']})\n{x['alarm']} · {x['severity']}\nsince {x['since']}",
    }) for x in alarms if x["lat"] is not None and x["lon"] is not None]


@tool("get_alarms",
      "The newest active, unacknowledged alarms in the city (lost connections, full bins, "
      "lamp faults, air-quality warnings), with locations and a layer_ref.",
      obj({"limit": {"type": "integer", "minimum": 1, "maximum": 20}}))
def h_get_alarms(ctx: ToolContext, a: dict) -> dict:
    res = context.alarms(ctx.city, a.get("limit") or 20)
    feats = alarm_features(res["alarms"])
    res["layer_ref"] = ctx.store.add_layer("alarms", feats, geometry="point", title="Active alarms",
                                           legend={"alarm": STATE_COLORS["alarm"]})
    res["_summary"] = f"{len(res['alarms'])} alarms"
    return res


TRAFFIC_FIELDS = [*context.OBJECT_TYPES.values(), "total"]


def camera_street(ctx: ToolContext, camera: str | None,
                  points: list[tuple[float, float]]) -> dict | None:
    """The street a camera filter names, as the walking line through its cameras, for the map
    to highlight. None without a filter, when the cameras don't line up on one street, or
    when the router can't be reached (the answer does without the highlight)."""
    if not camera or ctx.router is None:
        return None
    try:
        line = ctx.router.street_through(points)
    except WebError as e:
        log.info("no street line for %r: %s", camera, e)
        return None
    name = camera.strip()
    return {"name": name[:1].upper() + name[1:], "coordinates": line} if line else None


def add_traffic_replay(ctx: ToolContext, a: dict, res: dict) -> None:
    """Give a one-day, hour-by-hour traffic result its map side: a time-lapse of the cameras
    (a frame per hour, a value per object type), the street they sit along, and the rows per
    camera and hour behind it. The frames' totals are the result's own rows, so the map and a
    chart of them always agree."""
    rows = res["rows"]
    try:
        cams = context.traffic_camera_hours(ctx.city, a["period"], a.get("camera"))
    except ApiError as e:  # the answer still has its rows; it only loses the replay
        log.warning("no per-camera traffic for the replay: %s", e)
        return
    if not rows or not cams:
        return
    first, last = (datetime.strptime(rows[i]["hour"], context.HOUR_FMT) for i in (0, -1))
    times = [first + timedelta(hours=k) for k in range(int((last - first) / timedelta(hours=1)) + 1)]
    keys = [t.strftime(context.HOUR_FMT) for t in times]

    def counts(cam: dict, field: str) -> bool:
        return any(field in h for h in cam["hours"].values())

    name = (a.get("camera") or "").strip()
    name = name[:1].upper() + name[1:]
    title = f"{name + ' cameras' if name else 'Traffic cameras'}, hour by hour {a['period']}"
    headline = "people" if any(r.get("people") for r in rows) else "total"

    def day(cam: dict) -> dict:
        by = headline if counts(cam, headline) else "total"
        busiest = max(cam["hours"], key=lambda k: cam["hours"][k].get(by, 0))
        return {"camera": cam["camera"],
                **{f: sum(h.get(f, 0) for h in cam["hours"].values())
                   for f in TRAFFIC_FIELDS if counts(cam, f)},
                "busiest_hour": busiest[11:], "on_map": cam["lat"] is not None}

    # The day per camera, busiest first, so the answer can name them without another call.
    res["by_camera"] = sorted(map(day, cams), key=lambda r: -r.get(headline, 0))[:20]
    res["by_camera_data_ref"] = ctx.store.add_dataset(
        "traffic_camera_hours",
        [{"camera": c["camera"], "hour": k,
          **{f: c["hours"].get(k, {}).get(f, 0) for f in TRAFFIC_FIELDS if counts(c, f)}}
         for c in cams for k in keys],
        title=title, link={"key": "camera"})
    placed = [c for c in cams if c["lat"] is not None]
    if not placed:
        return
    fields = [f for f in TRAFFIC_FIELDS if any(counts(c, f) for c in placed)]
    by_hour = {r["hour"]: r for r in rows}
    # A camera that counts a type has 0 in a quiet hour; one that never counts it has no value.
    values = {f: [[c["hours"].get(k, {}).get(f, 0) if counts(c, f) else None for c in placed]
                  for k in keys] for f in fields}
    frames = {
        "times": [t.strftime("%a %d %b %H:%M") for t in times],
        "iso": keys,
        "state_names": ["camera"],
        "states": [[0] * len(placed) for _ in keys],
        "values": values,
        "totals": {f: [by_hour.get(k, {}).get(f, 0) for k in keys]
                   for f in TRAFFIC_FIELDS if any(f in r for r in rows)},
        "total_field": headline,
    }
    feats = [point(c["lon"], c["lat"], {
        "id": c["camera"], "label": c["camera"], "name": c["camera"], "state": "camera",
        "color": STATE_COLORS["camera"],
        **{f: values[f][-1][j] for f in fields if values[f][-1][j] is not None},
    }) for j, c in enumerate(placed)]
    street = camera_street(ctx, a.get("camera"), [(c["lat"], c["lon"]) for c in placed])
    res["layer_ref"] = ctx.store.add_layer(
        "traffic_hours", feats, geometry="point", title=title,
        legend={"camera": STATE_COLORS["camera"]}, value_fields=fields, frames=frames,
        street=street)
    res["map_replay"] = (
        f"layer_ref replays these {len(placed)} cameras hour by hour (value fields: "
        f"{', '.join(fields)}). Show it with show_on_map style 'columns_3d' and the value_field "
        "asked about, next to a chart of data_ref over `hour` (temporal): hovering the chart "
        "takes the map to that hour"
        + (f", and {street['name']} is drawn on the map, glowing with the hour's total"
           if street else "")
        + ". by_camera_data_ref has a row per camera and hour, linked to the cameras on the map.")


@tool("get_traffic",
      "Counts of people, bikes, cars, motorcycles, buses and trucks from the city's video "
      "analytics cameras. group_by 'day' (one row per day), 'hour_of_day' (per hour; over "
      "several days it is a summed hour-of-day profile) or 'camera' (totals per camera with "
      "coordinates and a layer_ref, good for a heatmap of where people walk). Optionally "
      "filter to cameras whose name contains `camera` (e.g. 'Ledras', 'Makariou'). "
      "'hour_of_day' for today or yesterday also returns a layer_ref that replays every "
      "camera hour by hour on the map (show it with 'columns_3d' and a value_field such as "
      "people) and a by_camera_data_ref (one row per camera and hour). With a camera "
      "filter along one street, the layers also draw that street on the map.",
      obj({
          "period": {"type": "string", "enum": list(context.PERIODS)},
          "group_by": {"type": "string", "enum": ["day", "hour_of_day", "camera"]},
          "camera": {"type": "string"},
      }, ["period", "group_by"]))
def h_get_traffic(ctx: ToolContext, a: dict) -> dict:
    res = context.traffic(ctx.city, a["period"], a["group_by"], a.get("camera"))
    if "error" in res:
        raise ToolError(f"{res['error']}; cameras: {', '.join(res['cameras'][:40])}")
    res["data_ref"] = ctx.store.add_dataset(
        f"traffic_{a['group_by']}", res["rows"], title=f"Traffic {a['period']} by {a['group_by']}",
        link={"key": "camera"} if a["group_by"] == "camera" else None)
    if a["group_by"] == "camera":
        feats = [point(r["lon"], r["lat"], {
            "id": r["camera"], "label": r["camera"], "state": "camera",
            "color": STATE_COLORS["camera"],
            "popup": f"{r['camera']}\npeople {r.get('people', 0):,} · cars {r.get('car', 0):,}",
            **{k: v for k, v in r.items() if isinstance(v, int)},
        }) for r in res["rows"] if r.get("lat") is not None]
        street = camera_street(ctx, a.get("camera"), [
            (f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]) for f in feats])
        res["layer_ref"] = ctx.store.add_layer(
            "traffic_cameras", feats, geometry="point", title=f"Traffic counts, {a['period']}",
            legend={"camera": STATE_COLORS["camera"]}, value_fields=TRAFFIC_FIELDS, street=street)
        res["rows"] = res["rows"][:15]
    elif res["group_by"] == "hour":  # one real day (today or yesterday), hour by hour
        add_traffic_replay(ctx, a, res)
    res["_summary"] = f"traffic {a['period']} by {a['group_by']}"
    return res


@tool("get_speed",
      "City-wide average traffic speed (km/h) per hour for the last N hours, next to the "
      "same hours one week earlier. There is no per-street speed in this data.",
      obj({"hours": {"type": "integer", "minimum": 1, "maximum": 48}}))
def h_get_speed(ctx: ToolContext, a: dict) -> dict:
    res = context.speed(ctx.city, a.get("hours") or 12)
    res["data_ref"] = ctx.store.add_dataset("speed", res["rows"], title="City average speed")
    res["_summary"] = f"{len(res['rows'])} hours of speed"
    return res


@tool("get_weather",
      "Weather now and for the next 12 hours in Nicosia (temperature, feels-like, rain "
      "chance, UV, sky), sunrise and sunset, and the regional air-quality model including "
      "Saharan dust.",
      obj({}))
def h_get_weather(ctx: ToolContext, a: dict) -> dict:
    res = context.weather(ctx.settings)
    res["data_ref"] = ctx.store.add_dataset("weather", res["next_12_hours"], title="Next 12 hours")
    res["_summary"] = f"{res['now']['temp_c']} °C, {res['now']['sky']}"
    return res

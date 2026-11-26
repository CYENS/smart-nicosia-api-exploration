"""The mobility kinds of the twin's entities: buses, bus stops, bus lines and road segments.

Ids say their kind: bus-5006 (a vehicle, by its fleet number), stop-1297 (a stop, by its
national stop id), line-npt-26 (an operator's line: npt, ic for Intercity, pame for Pame
Express) and road-12 (a Bluetooth road segment, by its national path id). They open in the
inspector like everything else, and stops, lines and roads are searchable from Ctrl K.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from chatbot import entities as E
from chatbot.mobility import planner
from chatbot.mobility.gtfs import LICENCE as GTFS_LICENCE
from chatbot.mobility.gtfs import TZ, line_order, simplify, slice_shape
from chatbot.mobility.realtime import LICENCE as RT_LICENCE
from chatbot.mobility.realtime import delay_text
from chatbot.mobility.roads import BAND_LABELS, LICENCE as ROAD_LICENCE
from chatbot.mobility.roads import SPEED_COLORS

E.KIND_LABELS.update({"bus": "Bus", "bus_stop": "Bus stop", "bus_line": "Bus line",
                      "road_segment": "Road segment"})

GTFS_PAGE = "https://traffic4cyprus.org.cy/dataset/publictransportstatic"
RT_PAGE = "https://traffic4cyprus.org.cy/dataset/publictransportrealtime_gtfs_rt"
ROAD_PAGE = "https://traffic4cyprus.org.cy/dataset/bluetooth"
BUS_TTL_S = 10
STOP_TTL_S = 30
ROAD_TTL_S = 60
ROAD_TONES = {"moving": "ok", "busy": "muted", "slow": "warn", "very_slow": "warn", "no_reading": "muted"}


def _mobility(ents):
    return getattr(ents, "mobility", None)


def _now(ents) -> datetime:
    m = _mobility(ents)
    return m.now() if m is not None else datetime.now(timezone.utc)


def _iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, timezone.utc)
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def _state(value: str, label: str, color: str, tone: str, as_of=None, now=None, reason=None) -> dict:
    age = (now - as_of).total_seconds() if isinstance(as_of, datetime) and now else None
    return {"value": value, "label": label, "color": color, "tone": tone, "stale": False,
            "reason": reason, "local": None, "as_of": _iso(as_of), "age_s": round(age) if age is not None else None}


def _sources_bus(ents, as_of=None) -> list[dict]:
    tt = _mobility(ents).timetable()
    versions = ", ".join(f"{f['operator']} {f['version']}" for f in (tt.feeds if tt else []) if f.get("version"))
    out = []
    if as_of is not None:
        out.append({"name": "GTFS-RT live feed", "detail": f"vehicle positions, {RT_LICENCE}",
                    "url": RT_PAGE, "as_of": _iso(as_of)})
    out.append({"name": "Bus timetables (GTFS)", "detail": f"{versions or 'NPT, Intercity, Pame Express'}, "
                                                           f"{GTFS_LICENCE}", "url": GTFS_PAGE})
    return out


# -- buses -----------------------------------------------------------------------------------


def bus_state(bus: dict, now: datetime) -> dict:
    as_of = datetime.fromtimestamp(bus["reported"], timezone.utc)
    color = bus.get("color") or "#8798b0"
    if bus.get("finished"):
        return _state("finished", "At its last stop", color, "muted", as_of, now)
    if bus.get("waiting"):
        return _state("waiting", f"Leaves at {bus['waiting']}", color, "muted", as_of, now)
    d = bus.get("delay_s")
    if d is None:
        return _state("running", "On the road", color, "live", as_of, now)
    text = delay_text(d) or "on time"
    if d >= 300:
        tone = "warn"
    elif d <= -120:
        tone = "ok"
    else:
        tone = "live"
    est = " (estimated)" if bus.get("delay_source") == "timetable" else ""
    return _state("late" if d >= 120 else "early" if d <= -120 else "on_time", text.capitalize() + est,
                  color, tone, as_of, now)


def build_bus(ents, eid: str, near) -> dict | None:
    m = _mobility(ents)
    if m is None:
        return None
    bus = m.bus(eid)
    if bus is None:
        snap = m.snapshot()
        why = ("the bus feed is unavailable right now" if snap.status == "unavailable"
               else "it isn't on the road now (buses leave the feed when their trip ends)")
        raise E.EntityNotFound(f"no bus {eid} in the live feed: {why}")
    tt = m.timetable()
    now = _now(ents)
    line = tt.lines.get(bus.get("line_id") or "") if tt else None
    trip = tt.trips[bus["trip"]] if tt and bus.get("trip") is not None else None
    title = f"Bus {bus['line']}" + (f" to {bus['headsign']}" if bus.get("headsign") else "")
    readings = []
    if bus.get("speed_kmh") is not None:
        readings.append({"key": "speed_kmh", "label": "Speed", "value": bus["speed_kmh"], "unit": "km/h",
                         "as_of": _iso(bus["reported"]), "stale": False})
    if bus.get("delay_s") is not None:
        readings.append({"key": "delay_min", "label": "Late by" if bus["delay_s"] >= 0 else "Early by",
                         "value": abs(round(bus["delay_s"] / 60)), "unit": "min", "as_of": _iso(bus["reported"])})
    nxt = bus.get("next_stops") or []
    relations = []
    if line:
        relations.append({"relation": "line", "label": "Line", "id": line.id, "kind": "bus_line",
                          "name": f"Line {line.short}", "detail": line.name, "color": line.color})
    geometry = {"type": "Point", "coordinates": [bus["lon"], bus["lat"]]}
    if tt and trip and bus.get("along_m") is not None and trip.shape_id in tt.shapes:
        shape = tt.shapes[trip.shape_id]
        ahead = slice_shape(shape, bus["along_m"], shape.length_m)
        if len(ahead) >= 2:
            geometry = {"type": "LineString", "coordinates": [list(c) for c in simplify([tuple(c) for c in ahead], 3)]}
    attrs = [E.attr("Line", f"{bus['line']}, {line.name}" if line else bus["line"]),
             E.attr("Towards", bus.get("headsign")), E.attr("Operator", bus.get("operator")),
             E.attr("Vehicle", bus["vehicle"]), E.attr("Plate", bus.get("plate")),
             E.attr("Trip", f"left {trip.dep[0] // 3600:02d}:{trip.dep[0] % 3600 // 60:02d}" if trip and trip.dep else None),
             E.attr("Stop", f"{bus['progress']['stop'] + 1} of {bus['progress']['stops']}" if bus.get("progress") else None),
             E.attr("Delay from", {"feed": "the operator's live feed", "timetable": "its position against the timetable"}
                    .get(bus.get("delay_source") or ""))]
    return E.new_entity(
        bus["id"], "bus", title,
        subtitle=", ".join(x for x in (bus.get("operator"), f"vehicle {bus['vehicle']}") if x),
        geometry=geometry, center=[bus["lon"], bus["lat"]],
        state=bus_state(bus, now), readings=readings, attributes=attrs, relations=relations,
        sources=_sources_bus(ents, bus["reported"]), as_of=_iso(bus["reported"]),
        live={"line_id": bus.get("line_id"), "line": bus["line"], "color": bus.get("color"),
              "text_color": bus.get("text_color"), "heading": bus.get("heading"),
              "headsign": bus.get("headsign"), "waiting": bus.get("waiting"),
              "progress": bus.get("progress"), "delay_source": bus.get("delay_source")},
        next_stops=nxt,
        _for_model={"next_stops": [{k: s.get(k) for k in ("id", "name", "scheduled", "expected") if s.get(k)}
                                   for s in nxt]})


# -- stops -----------------------------------------------------------------------------------


def build_stop(ents, eid: str, near) -> dict | None:
    m = _mobility(ents)
    if m is None:
        return None
    tt = m.require_timetable(10)
    stop = tt.stop(eid.split("-", 1)[1].strip())
    if stop is None:
        return None
    now = _now(ents)
    try:
        snap = m.snapshot()
    except Exception:  # noqa: BLE001 - the timetable still answers
        snap = None
    deps = planner.departures(tt, [stop], now, 90, 10, snap=snap)
    lines = tt.lines_at(stop)
    if deps:
        d = deps[0]
        mins = d["in_min"]
        label = "Next bus now" if mins == 0 else f"Next bus in {mins} min"
        state = _state("served", label, "#ffae45", "live")
    else:
        state = _state("quiet", "No buses in the next 90 min", "#8798b0", "muted")
    relations = [{"relation": "line", "label": "Line", "id": ln.id, "kind": "bus_line",
                  "name": f"Line {ln.short}", "detail": ln.name, "color": ln.color} for ln in lines[:8]]
    relations += ents.places_within(stop.lat, stop.lon, 150, limit=2)
    public = planner.public(deps)
    return E.new_entity(
        f"stop-{stop.id}", "bus_stop", stop.name,
        subtitle=f"Stop {stop.code}" + (f", lines {', '.join(ln.short for ln in lines[:6])}" if lines else ""),
        geometry=E.point_geom(stop.lon, stop.lat), center=[stop.lon, stop.lat], state=state,
        attributes=[E.attr("Stop code", stop.code), E.attr("In Greek", stop.name_el),
                    E.attr("Lines", ", ".join(ln.short for ln in lines) or None),
                    E.attr("Operators", ", ".join(dict.fromkeys(ln.operator for ln in lines)) or None)],
        relations=relations, sources=_sources_bus(ents, snap.fetched_at if snap and snap.status != "unavailable" else None),
        as_of=_iso(now), departures=public,
        feed_status=snap.status if snap else "unavailable",
        _for_model={"next_departures": [{k: d[k] for k in ("line", "headsign", "scheduled", "expected", "in_min")
                                          if d.get(k) is not None} for d in public[:8]]})


# -- lines -----------------------------------------------------------------------------------


def line_shapes(tt, line) -> list[list[list[float]]]:
    """One shape per direction (the one most of its trips run), simplified for the page."""
    count: dict[tuple[int, str], int] = {}
    for t in tt.trips:
        if t.line_id == line.id and t.shape_id in tt.shapes:
            k = (t.direction, t.shape_id)
            count[k] = count.get(k, 0) + 1
    best: dict[int, tuple[int, str]] = {}
    for (d, sid), n in count.items():
        if d not in best or n > best[d][0]:
            best[d] = (n, sid)
    return [[[round(x, 6), round(y, 6)] for x, y in simplify(tt.shapes[sid].coords, 4)]
            for _, (n, sid) in sorted(best.items())]


def build_line(ents, eid: str, near) -> dict | None:
    m = _mobility(ents)
    if m is None:
        return None
    tt = m.require_timetable(10)
    line = tt.lines.get(eid.strip().lower())
    if line is None:
        return None
    now = _now(ents)
    try:
        snap, buses = m.buses({line.id})
    except Exception:  # noqa: BLE001
        snap, buses = None, []
    running = [b for b in buses if not b.get("finished")]
    today = now.astimezone(TZ).date()
    trips = [t for t in tt.trips if t.line_id == line.id and tt.runs(t, today) and t.dep]
    shapes = line_shapes(tt, line)
    geometry = ({"type": "MultiLineString", "coordinates": shapes} if len(shapes) > 1 else
                {"type": "LineString", "coordinates": shapes[0]} if shapes else None)
    center = shapes[0][len(shapes[0]) // 2] if shapes else None
    if running:
        state = _state("running", f"{len(running)} bus{'es' if len(running) != 1 else ''} running", line.color, "live")
    elif snap is not None and snap.status == "unavailable":
        state = _state("unknown", "Live feed unavailable", "#8798b0", "muted")
    else:
        state = _state("idle", "None running now", "#8798b0", "muted")
    first = min((t.dep[0] for t in trips), default=None)
    last = max((t.dep[0] for t in trips), default=None)

    def clock(sec):
        return f"{sec // 3600 % 24:02d}:{sec % 3600 // 60:02d}" if sec is not None else None

    ends = []
    for t in trips[:1] + [x for x in trips if x.direction != (trips[0].direction if trips else 0)][:1]:
        a, b = tt.stops[t.stops[0]], tt.stops[t.stops[-1]]
        ends.append(f"{a.name} to {b.name}")
    on_road = [{"id": b["id"], "name": f"Bus {b['line']} to {b.get('headsign') or '?'}",
                "detail": delay_text(b.get("delay_s")) or "on the road", "lon": b["lon"], "lat": b["lat"]}
               for b in sorted(running, key=lambda x: -(x.get("delay_s") or 0))[:10]]
    relations = []
    for t in trips[:1]:
        for pos in (0, len(t.stops) - 1):
            s = tt.stops[t.stops[pos]]
            relations.append({"relation": "terminus", "label": "First stop" if pos == 0 else "Last stop",
                              "id": f"stop-{s.id}", "kind": "bus_stop", "name": s.name,
                              "lon": s.lon, "lat": s.lat})
    return E.new_entity(
        line.id, "bus_line", f"Line {line.short}", subtitle=f"{line.operator}, {line.name}",
        geometry=geometry, center=center, state=state,
        readings=[{"key": "running", "label": "Running now", "value": len(running), "unit": "",
                   "as_of": _iso(snap.fetched_at) if snap and snap.status != "unavailable" else None},
                  {"key": "trips_today", "label": "Trips today", "value": len(trips), "unit": ""}],
        attributes=[E.attr("Operator", line.operator), E.attr("Route", line.name),
                    E.attr("In Greek", line.name_el), E.attr("Runs", " / ".join(ends) or None),
                    E.attr("First departure today", clock(first)), E.attr("Last departure today", clock(last))],
        relations=relations, sources=_sources_bus(ents, snap.fetched_at if snap and running else None),
        as_of=_iso(now), live={"line_id": line.id, "line": line.short, "color": line.color,
                               "text_color": line.text_color},
        running=on_road, _for_model={"running_now": [{"id": b["id"], "to": b["name"], "delay": b["detail"]}
                                                     for b in on_road]})


# -- road segments ---------------------------------------------------------------------------


def road_row(m, seg) -> dict | None:
    rows = m.roads.speeds()["segments"]
    return next((r for r in rows if r["id"] == seg.id), None)


def build_road(ents, eid: str, near) -> dict | None:
    m = _mobility(ents)
    if m is None:
        return None
    seg = m.roads.segment(eid)
    if seg is None:
        return None
    row = road_row(m, seg) or {}
    now = _now(ents)
    state_value = row.get("state", "no_reading")
    measured = datetime.fromisoformat(row["measured_at"]) if row.get("measured_at") else None
    speed = row.get("speed_kmh")
    free, how = row.get("free_flow_kmh"), row.get("free_flow")
    label = BAND_LABELS[state_value]
    if speed is not None and free:
        label += f", {round(100 * speed / free)}% of free flow"
    state = _state(state_value, label, SPEED_COLORS[state_value], ROAD_TONES[state_value], measured, now)
    # Free flow and the usual sit on the speed meter (the road section); the model reads them below.
    readings = []
    if speed is not None:
        readings.append({"key": "speed_kmh", "label": "Speed now", "value": speed, "unit": "km/h",
                         "as_of": row.get("measured_at")})
    if row.get("travel_time_s") is not None:
        readings.append({"key": "travel_min", "label": "Time to drive it", "value": round(row["travel_time_s"] / 60, 1), "unit": "min"})
    how_text = {"measured": "the speed most drivers make here when it is clear (85th percentile of 4 weeks)",
                "estimated": "estimated from the road network's typical speeds, until the twin has a day of readings",
                "assumed": "assumed 50 km/h, the urban limit, while the estimate loads"}.get(how or "")
    opposite = next((s for s in m.roads.segments() if s.from_site == seg.to_site and s.to_site == seg.from_site), None)
    relations = []
    if opposite:
        orow = road_row(m, opposite) or {}
        relations.append({"relation": "opposite", "label": "The other way", "id": opposite.id,
                          "kind": "road_segment", "name": opposite.name,
                          "detail": f"{round(orow['speed_kmh'])} km/h" if orow.get("speed_kmh") is not None else "no reading",
                          "color": SPEED_COLORS[orow.get("state", "no_reading")]})
    mid = seg.middle
    cams = [r for r in ents.nearest_devices(mid[1], mid[0], max_m=500) if r["kind"] == "camera"]
    relations += cams
    usual_note = (f"{row['usual_kmh']} km/h at this hour on this weekday, over {row['usual_days']} day"
                  f"{'s' if row['usual_days'] != 1 else ''}" if row.get("usual_kmh") is not None else
                  "the twin is still learning this road's usual speeds")
    return E.new_entity(
        seg.id, "road_segment", seg.name,
        subtitle=", ".join(x for x in (seg.road, f"{seg.length_m / 1000:.1f} km one way") if x),
        geometry={"type": "LineString", "coordinates": [list(c) for c in seg.coords]},
        center=list(mid), state=state, readings=readings,
        attributes=[E.attr("From", seg.from_name), E.attr("To", seg.to_name),
                    E.attr("Length", f"{round(seg.length_m)} m"), E.attr("Free flow", how_text),
                    E.attr("Usual", usual_note),
                    E.attr("Detectors", f"Bluetooth sites {seg.from_site} and {seg.to_site}")],
        relations=relations,
        sources=[{"name": "Bluetooth travel times", "detail": f"National Access Point, {ROAD_LICENCE}",
                  "url": ROAD_PAGE, "as_of": row.get("measured_at")}],
        as_of=row.get("measured_at"),
        road={"speed_kmh": speed, "free_flow_kmh": free, "free_flow": how, "usual_kmh": row.get("usual_kmh"),
              "share": row.get("share_of_free_flow")},
        _for_model={"free_flow_kmh": free, "free_flow_from": how, "usual_kmh_this_hour": row.get("usual_kmh"),
                    "share_of_free_flow": row.get("share_of_free_flow")})


def road_series(ents, entity: dict, end: datetime | None = None) -> dict | None:
    """The road's speed in the 24 hours up to `end` (the city clock's moment; default now)."""
    m = _mobility(ents)
    seg = m.roads.segment(entity["id"]) if m else None
    if seg is None:
        return None
    now = end or _now(ents)
    pts = m.roads.series(seg.nap_id, 24, end=now)
    if len(pts) < 2:
        return None
    by = {t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"): v for t, v in pts}
    start = (now - timedelta(hours=24)).astimezone(TZ)
    start = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
    points = []
    t = start
    while t <= now:
        k = t.strftime("%Y-%m-%dT%H:%M")
        points.append({"t": k, "v": by.get(k)})
        t += timedelta(minutes=15)
    vals = [p["v"] for p in points if p["v"] is not None]
    free = (entity.get("road") or {}).get("free_flow_kmh")
    out = {"metric": "speed_kmh", "label": "Speed", "unit": "km/h", "draw": "line", "step_min": 15,
           "points": points, "from": points[0]["t"], "to": points[-1]["t"], "summary": E.summary(vals),
           "source": "memory"}
    if free:
        out["threshold"] = {"value": free, "label": f"free flow {round(free)} km/h"}
    return out


# -- search ----------------------------------------------------------------------------------

_index: dict[str, tuple[float, int, list[dict]]] = {}


def search_items(ents) -> list[dict]:
    m = _mobility(ents)
    if m is None:
        return []
    tt = m.timetable()
    items: list[dict] = []
    if tt is not None:
        hit = _index.get("bus")
        if hit is None or hit[1] != id(tt):
            built = []
            for s in tt.stops:
                lines = tt.lines_at(s)
                if not lines:
                    continue
                item = E.index_item(
                    f"stop-{s.id}", "bus_stop", s.name, "Bus stops",
                    f"Stop {s.code}, lines {', '.join(ln.short for ln in lines[:5])}{'…' if len(lines) > 5 else ''}",
                    s.lat, s.lon, [s.name, s.name_el, s.code, "bus stop"])
                item["_aliases"] = {f"stop {s.code}", f"stop-{s.id}"}  # the code on the pole
                built.append(item)
            for ln in sorted(tt.lines.values(), key=line_order):
                item = E.index_item(
                    ln.id, "bus_line", f"Line {ln.short}", "Bus lines", f"{ln.operator}, {ln.name}",
                    None, None, [ln.short, f"line {ln.short}", f"bus {ln.short}", ln.name, ln.name_el,
                                 ln.operator, "bus line"])
                # A number is a bus line before it is a register file number ("26", "bus 26").
                short = ln.short.lower()
                item["_aliases"] = {short, f"line {short}", f"bus {short}", f"λεωφορειο {short}"}
                built.append(item)
            _index["bus"] = hit = (time.time(), id(tt), built)
        items += hit[2]
    hit = _index.get("roads")
    if hit is None or time.time() - hit[0] > 3600:
        try:
            segs = m.roads.segments()
        except Exception:  # noqa: BLE001 - the inventory isn't there yet
            segs = []
        built = [E.index_item(s.id, "road_segment", s.name, "Roads",
                              ", ".join(x for x in (s.road, "road speed") if x), s.middle[1], s.middle[0],
                              [s.name, s.road, s.from_name, s.to_name, "road speed traffic"])
                 for s in segs]
        _index["roads"] = hit = (time.time(), 0, built)
    items += hit[2]
    return items


E.register_kind(E.Kind("bus", lambda i: i.lower().startswith("bus-"), build_bus, ttl_s=BUS_TTL_S))
E.register_kind(E.Kind("bus_stop", lambda i: i.lower().startswith("stop-"), build_stop, ttl_s=STOP_TTL_S))
E.register_kind(E.Kind("bus_line", lambda i: i.lower().startswith("line-"), build_line, ttl_s=STOP_TTL_S))
E.register_kind(E.Kind("road_segment", lambda i: i.lower().startswith("road-"), build_road, ttl_s=ROAD_TTL_S))
E.register_series("road_segment", road_series)
E.register_search(search_items, {"Bus stops": 6, "Bus lines": 4, "Roads": 4})

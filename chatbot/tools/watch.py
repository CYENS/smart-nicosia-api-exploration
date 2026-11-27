"""Watch tools: the incident inbox, what else happened around an incident, and watches (which
the chatbot can only propose: a person's click in the page saves one). They read the Agent's
Watcher (chatbot/watch), shared with the inbox's endpoints."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from chatbot.city import KINDS
from chatbot.geo import haversine_m
from chatbot.memory import Unavailable
from chatbot.overview import cached
from chatbot.store import point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, obj, resolve_point
from chatbot.watch.detectors import CITY_KINDS, city_kind
from chatbot.watch.incidents import TZ, local
from chatbot.watch.service import states_of
from chatbot.watch.watches import METRICS, STATES, Rule, WatchError

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

SEVERITY_COLORS = {"high": "#ff5a4e", "medium": "#f5a524", "low": "#8798b0"}
SEVERITY_LEGEND = {"high severity": SEVERITY_COLORS["high"],
                   "medium severity": SEVERITY_COLORS["medium"],
                   "low severity": SEVERITY_COLORS["low"]}
CATEGORY = {"outage": "outage", "lit_by_day": "lighting", "lamp_power": "power",
            "parking_sensor": "parking", "bins": "bins", "air": "air", "city_alarm": "other",
            "data_feed": "feed"}
ALL_METRICS = sorted({m for ms in METRICS.values() for m in ms})
ALL_STATES = sorted({s for ss in STATES.values() for s in ss})


def watcher_of(ctx: ToolContext):
    if ctx.watcher is None:
        raise ToolError("the twin's watch isn't running in this deployment")
    return ctx.watcher


def _features(rows: list[dict], details: list[dict]) -> list[dict]:
    feats = []
    for r, d in zip(rows, details):
        color = SEVERITY_COLORS.get(r["severity"], SEVERITY_COLORS["low"])
        for m in d["members"]:
            if m.get("lat") is None:
                continue
            feats.append(point(m["lon"], m["lat"], {
                "id": m["id"], "label": m["label"], "state": f"{r['severity']} severity",
                "color": color, "incident": r["id"],
                "popup": f"{m['label']}\n{m['what']}\nPart of {r['id']}: {r['title']}"}))
        if r.get("lat") is not None:
            feats.append(point(r["lon"], r["lat"], {
                "id": r["id"], "label": r["title"], "state": f"{r['severity']} severity",
                "color": color, "incident": r["id"], "size": 2,
                "popup": f"{r['id']}: {r['title']}\n{r['severity'].capitalize()} severity, "
                         f"{r['status']}\nSince {r['start'].replace('T', ' ')}, "
                         f"{r['devices']} device{'s' if r['devices'] != 1 else ''}"}))
    return feats


@tool("list_incidents",
      "The twin's incident inbox: what is going wrong in the city. Incidents come from the "
      "twin's own detectors over its memory (lamps dark at night, lamps lit by day, lamps "
      "drawing power out of line with similar lamps on their street, parking bays stuck "
      "occupied for days, bins whose fill jumps or that stop reporting, air stations gone "
      "quiet, the national road detectors' feed stalling) and from the city's own alarms, grouped by place and time: three lamps on one "
      "street going silent together are one probable power cut, not three faults. Each has an "
      "id (INC-12), a severity (high, medium, low), a status (open, acknowledged, snoozed, "
      "resolved), a place, when it began, whether it is still happening and the devices "
      "involved. Filter by status (default unresolved: everything not resolved), the least "
      "severity, category, a street, near a place, one device id, or the last hours. The "
      "layer_ref draws each incident at its place and its devices, coloured by severity (show "
      "it with 'points'); the data_ref has one row per incident (id, title, severity, status, "
      "category, place, start, devices, still_happening, lat, lon), linked to the map by id.",
      obj({"status": {"type": "string", "enum": ["unresolved", "open", "acknowledged", "snoozed",
                                                 "resolved", "all"]},
           "severity": {"type": "string", "enum": ["high", "medium", "low"],
                        "description": "The least severe to include (high: only high)."},
           "category": {"type": "string", "enum": list(CATEGORY)},
           "street": {"type": "string", "description": "A street name, e.g. 'Mnasiadou'."},
           "entity": {"type": "string", "description": "One device id, e.g. 'YL1099'."},
           "since_hours": {"type": "integer", "minimum": 1, "maximum": 1488,
                           "description": "Only incidents seen in the last this many hours."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 50},
           **NEAR}))
def h_list_incidents(ctx: ToolContext, a: dict) -> dict:
    w = watcher_of(ctx)
    near = None
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if pt is not None:
        near = (pt[0], pt[1], float(a.get("radius_m") or 500))
    now = w.now()
    rows = w.book.query(status=a.get("status") or "unresolved", severity=a.get("severity"),
                        family=CATEGORY.get(a.get("category") or ""), entity=a.get("entity"),
                        street=a.get("street"), near=near,
                        since=now - timedelta(hours=a["since_hours"]) if a.get("since_hours") else None,
                        limit=a.get("limit") or 20)
    details = [w.book.detail(w.book.get(r["id"]), now) for r in rows]
    out_rows = [{"id": r["id"], "title": r["title"], "severity": r["severity"],
                 "status": r["status"], "category": r["category"], "place": r["place"],
                 "start": r["start"], "last_seen": r["last_seen"], "devices": r["devices"],
                 "still_happening": r["active"], "lat": r["lat"], "lon": r["lon"]} for r in rows]
    res: dict = {
        "counts": w.book.counts(),
        "evaluated_at": local(int(w.evaluated_at.timestamp() * 1000)) if w.evaluated_at else None,
        "incidents": [{**o, "summary": d["summary"], "ids": r["ids"],
                       "last_action": r["last_action"]} for o, d, r in zip(out_rows, details, rows)],
    }
    if not rows:
        res["note"] = "no incidents match; the inbox and the counts say what there is"
        res["_summary"] = "no incidents"
        return res
    res["layer_ref"] = ctx.store.add_layer("incidents", _features(rows, details), geometry="point",
                                           title="Incidents", legend=SEVERITY_LEGEND)
    res["data_ref"] = ctx.store.add_dataset("incidents", out_rows, title="Incidents",
                                            link={"key": "id"})
    res["_summary"] = f"{len(rows)} incidents"
    return res


def _event_rows(ctx: ToolContext, start: datetime, end: datetime) -> list[tuple]:
    """(time, device, message, type, lat, lon) of the city's events in a window, from memory."""
    if ctx.memory is None:
        return []
    with ctx.memory.reading() as con:
        return con.execute(
            """SELECT e.ts, e.device, e.message, d.type, d.lat, d.lon FROM events e
               LEFT JOIN devices d ON d.name = e.device
               WHERE e.ts >= ? AND e.ts <= ? ORDER BY e.ts""", [start, end]).fetchall()


def _changes_near(ctx: ToolContext, start: datetime, end: datetime) -> list[tuple]:
    """(time, device, type, old_state, new_state, lat, lon) of state changes in a window."""
    if ctx.memory is None:
        return []
    with ctx.memory.reading() as con:
        return con.execute(
            """SELECT s.ts, s.device, s.device_type, s.old_state, s.new_state, d.lat, d.lon
               FROM state_changes s JOIN devices d ON d.name = s.device
               WHERE s.ts >= ? AND s.ts <= ? AND d.lat IS NOT NULL""", [start, end]).fetchall()


@tool("explain_incident",
      "What else happened around one incident (INC-12), from the twin's memory, to explain it: "
      "the incident's own evidence (readings and alarms with times) and what people did; the "
      "city's alarms nearby in the hours around its start, and how many of the same alarm the "
      "whole city raised then (local or city-wide?); devices nearby that changed state then "
      "(lamps going dark, bins filling, bays emptying); other incidents at the same time; how "
      "much of the city's lamps were dark at that moment; and the weather that hour. Use it "
      "for 'why', 'what caused', 'is it just here'. The layer_ref draws the incident's devices "
      "and what happened nearby (show it with 'points'); the data_ref is a timeline (time, "
      "device, kind, what, source: incident | nearby | elsewhere, distance_m) for a strip or "
      "dot chart over time.",
      obj({"id": {"type": "string", "description": "The incident's id, e.g. INC-12."},
           "radius_m": {"type": "number", "minimum": 50, "maximum": 3000,
                        "description": "How far around counts as nearby (default 400)."},
           "hours": {"type": "number", "minimum": 0.5, "maximum": 24,
                     "description": "Hours before and after its start to look at (default 2)."}},
          ["id"]))
def h_explain_incident(ctx: ToolContext, a: dict) -> dict:
    w = watcher_of(ctx)
    x = w.book.get(a["id"])
    if x is None or not x["members"]:
        known = ", ".join(r["id"] for r in w.book.query(status="all", limit=8)) or "none yet"
        raise ToolError(f"no incident {a['id']!r}; recent ones: {known}")
    now = w.now()
    d = w.book.detail(x, now)
    radius = float(a.get("radius_m") or 400)
    hours = float(a.get("hours") or 2)
    start = datetime.fromtimestamp(x["start"] / 1000, tz=timezone.utc)
    lo, hi = start - timedelta(hours=hours), min(now, start + timedelta(hours=hours))
    mine = {m["id"] for m in d["members"]}
    pts = [(m["lat"], m["lon"]) for m in d["members"] if m.get("lat") is not None]

    def dist(lat, lon) -> float | None:
        if lat is None or not pts:
            return None
        return min(haversine_m(lat, lon, a_, b_) for a_, b_ in pts)

    timeline, feats, notes = [], [], []
    color = SEVERITY_COLORS.get(x["severity"], SEVERITY_COLORS["low"])
    for m in d["members"]:
        if m.get("lat") is not None:
            feats.append(point(m["lon"], m["lat"], {
                "id": m["id"], "label": m["label"], "state": "in the incident", "color": color,
                "popup": f"{m['label']}\n{m['what']}"}))
    for e in d["evidence"]:
        timeline.append({"time": e["time"][:16], "id": e.get("device"), "device": e.get("device"),
                         "kind": e.get("kind"), "what": e["text"], "source": "incident",
                         "distance_m": 0})
    nearby_events, same_kind_elsewhere = [], Counter()
    # The alarm this incident is made of, to count the same alarm elsewhere in the city then.
    my_kinds = {words for _, _, _, words in CITY_KINDS
                if any(words in m["what"] for m in d["members"])}
    if any(m["kind"] == "street_light" and "dark" in m["what"] for m in d["members"]):
        my_kinds.add("lost connection")
    try:
        for ts, device, message, dtype, lat, lon in _event_rows(ctx, lo, hi):
            if device in mine:
                continue
            words = city_kind(message.partition(": ")[2] or message)[2]
            dm = dist(lat, lon)
            if dm is not None and dm <= radius:
                nearby_events.append({"time": ts.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"),
                                      "device": device, "what": words,
                                      "distance_m": round(dm)})
                timeline.append({"time": ts.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"),
                                 "id": device, "device": device, "kind": "event", "what": words,
                                 "source": "nearby", "distance_m": round(dm)})
                feats.append(point(lon, lat, {"id": device, "label": words, "state": "alarm nearby",
                                              "color": "#ffae45", "popup": f"{device}\n{words}"}))
            elif words in my_kinds:
                same_kind_elsewhere[words] += 1
        changes = Counter()
        changed_devices = []
        for ts, device, dtype, old, new, lat, lon in _changes_near(ctx, lo, hi):
            if device in mine:
                continue
            dm = dist(lat, lon)
            if dm is None or dm > radius:
                continue
            changes[(KINDS_BY_TYPE.get(dtype, dtype), old, new)] += 1
            if (dtype == "Lighting" and new == "dark") or (dtype == "Wastebin" and new == "full"):
                changed_devices.append((ts, device, dtype, old, new, round(dm)))
        for ts, device, dtype, old, new, dm in changed_devices[:15]:
            timeline.append({"time": ts.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"), "id": device,
                             "device": device, "kind": "change", "what": f"{old} to {new}",
                             "source": "nearby", "distance_m": dm})
    except Unavailable as e:
        notes.append(f"the twin's memory was not available for the surroundings ({e})")
        changes = Counter()
    others = []
    for r in w.book.query(status="all", limit=300):
        if r["id"] == x["id"] or r["start_ms"] > hi.timestamp() * 1000 or r["last_seen_ms"] < lo.timestamp() * 1000:
            continue
        dm = dist(r["lat"], r["lon"])
        others.append({"id": r["id"], "title": r["title"], "severity": r["severity"],
                       "start": r["start"], "distance_m": round(dm) if dm is not None else None})
        if r.get("lat") is not None:
            feats.append(point(r["lon"], r["lat"], {
                "id": r["id"], "label": r["title"], "state": "other incident then",
                "color": SEVERITY_COLORS.get(r["severity"], "#8798b0"),
                "popup": f"{r['id']}: {r['title']}"}))
    others.sort(key=lambda o: (o["distance_m"] is None, o["distance_m"] or 0))
    lamps_then = None
    if ctx.moments is not None and any(m["kind"] == "street_light" for m in d["members"]):
        try:
            snap = ctx.moments.snapshot("street_light", start)
            if snap.known:
                c = Counter(snap.states)
                lamps_then = {"at": local(int(snap.at.timestamp() * 1000)), "offline": c.get("offline", 0),
                              "on": c.get("on", 0), "lamps": len(snap.states)}
        except Exception as e:  # noqa: BLE001 - the rest of the answer stands without it
            log.info("explain: lamps at %s unavailable: %s", start, e)
    from chatbot.context import weather_history
    past = cached("weather_history", 3600, lambda: weather_history(ctx.settings)) or {}
    weather = past.get(start.astimezone(TZ).replace(minute=0).strftime("%Y-%m-%dT%H:%M"))
    timeline.sort(key=lambda r: r["time"])
    res = {
        "incident": {k: d[k] for k in ("id", "title", "summary", "severity", "status", "category",
                                       "place", "start", "last_seen", "active", "source")},
        "devices": [{k: m.get(k) for k in ("id", "kind", "label", "street", "what", "start", "end",
                                           "active")} for m in d["members"]][:30],
        "evidence": d["evidence"][:30],
        "actions": d["actions"],
        "window": {"from": local(int(lo.timestamp() * 1000)), "to": local(int(hi.timestamp() * 1000)),
                   "radius_m": radius},
        "alarms_nearby": nearby_events[:20],
        "same_alarm_elsewhere_in_the_city": dict(same_kind_elsewhere),
        "state_changes_nearby": [{"kind": k, "from": o, "to": n, "devices": c}
                                 for (k, o, n), c in changes.most_common(10)],
        "other_incidents_then": others[:8],
        "lamps_city_wide_then": lamps_then,
        "weather_that_hour": weather,
        "notes": notes or None,
        "layer_ref": ctx.store.add_layer("incident_context", feats, geometry="point",
                                         title=f"{x['id']} and around it",
                                         legend={"in the incident": color,
                                                 "alarm nearby": "#ffae45",
                                                 "other incident then": "#8798b0"}),
        "data_ref": ctx.store.add_dataset("incident_timeline", timeline,
                                          title=f"{x['id']}: what happened around it",
                                          link={"key": "id"}),
        "_summary": f"{x['id']}: {len(nearby_events)} alarms nearby, {len(others)} other incidents",
    }
    return {k: v for k, v in res.items() if v is not None}


KINDS_BY_TYPE = {t: k for k, t in KINDS.items()}


@tool("create_watch",
      "Propose a watch: a rule the twin checks whenever fresh readings arrive, raising a toast "
      "in every open page (and a browser notification if the user allowed them) the moment it "
      "comes true, once per crossing. Examples: bin YW1394 above 90% (kind waste_bin, ids, "
      "metric fill_pct, op above, value 90); air turns poor near me (kind air_station, "
      "near_place 'my location', states high and very_high); any lamp on Mnasiadou goes "
      "offline (kind street_light, street, states offline); any new high incident (kind "
      "incident, severity high). This tool NEVER saves anything: it returns a proposal and the "
      "page shows a confirmation card under the answer. Only the user's click on Save starts "
      "the watch. Tell them to press Save, say what it will watch and what holds now; never "
      "say the watch is set.",
      obj({"kind": {"type": "string", "enum": [*KINDS, "incident"]},
           "ids": {"type": "array", "maxItems": 20, "items": {"type": "string"},
                   "description": "Device ids, e.g. ['YW1394']."},
           "street": {"type": "string", "description": "Every device of the kind on this street."},
           "metric": {"type": "string", "enum": ALL_METRICS,
                      "description": "waste_bin: fill_pct; air_station: caqi, noise_db, pm2_5, "
                                     "pm10, no2; street_light: power_w."},
           "op": {"type": "string", "enum": ["above", "below"]},
           "value": {"type": "number"},
           "states": {"type": "array", "maxItems": 5, "items": {"type": "string", "enum": ALL_STATES},
                      "description": "Fires when the state becomes one of these. Air 'poor' is "
                                     "high and very_high; a lamp out is offline; a full bin full."},
           "severity": {"type": "string", "enum": ["high", "medium", "low"],
                        "description": "For kind incident: the least severity that counts."},
           **NEAR},
          ["kind"]))
def h_create_watch(ctx: ToolContext, a: dict) -> dict:
    w = watcher_of(ctx)
    near = None
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if pt is not None:
        near = {"lat": pt[0], "lon": pt[1], "radius_m": float(a.get("radius_m") or 500),
                "label": pt[2]}
    rule = Rule(kind=a["kind"], ids=[i.strip() for i in a.get("ids") or []] or None,
                street=(a.get("street") or "").strip() or None, near=near,
                metric=a.get("metric"), op=a.get("op"), value=a.get("value"),
                states=a.get("states") or None, severity=a.get("severity"))
    try:
        proposal = w.watches.propose(rule, states_of(ctx.city, rule.kind))
    except WatchError as e:
        raise ToolError(str(e)) from None
    ctx.emit({"type": "ui", "action": "watch_confirm", "proposal": proposal})
    return {"status": "waiting_for_the_users_click", **proposal,
            "note": "Nothing is saved yet. The page shows a confirmation card under your "
                    "answer with Save and Not now; only the user's click on Save starts the "
                    "watch. Say what it will watch and what holds now, and ask them to press Save.",
            "_summary": f"proposed: {proposal['label']}"}

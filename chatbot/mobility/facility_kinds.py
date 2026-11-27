"""The twin's kinds for where people park, charge and hire (chatbot/mobility/facilities.py).

Ids say their kind: carpark-31 (a car park, by its national id), pnr-0 (a park and ride site),
charger-1a2b3c4d (an EV charging point: the feed has no id, so a hash of its name), nextbike-6495339
(a Nextbike station, by Nextbike's own station id), bolt-dac81462 (a Bolt scooter zone, the start
of Bolt's id) and rack-5 (a bicycle rack). They open in the inspector like everything else, with
their attributes, where they come from and what is near: the nearest bus stop, the street, the
other kinds around them, and for a car park the city's parking bay sensors at or beside it.

The inventories are not readings: at a past moment of the city clock a facility is as it is now,
and says how old its inventory is. Two things in it are live: the bays a car park's sensors see
(now from the last snapshot, then from the twin's memory) and the bikes at a Nextbike station
(now only).
"""

from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from chatbot import entities as E
from chatbot.geo import haversine_m
from chatbot.mobility.facilities import (COLORS, CONNECTORS, FEEDS, GBFS_LICENCE, GBFS_PAGE, ID_PREFIX,
                                         KINDS, LABELS, LICENCE, Facilities, Facility)
from chatbot.mobility.gtfs import TZ
from chatbot.store import STATE_COLORS

E.KIND_LABELS.update({k: v for k, v in LABELS.items()})

BAY_M = 60  # a bay sensor this close to a car park's point is at it or beside it
OSM_M = 40  # the OpenStreetMap car park at the same place
NEAR_OTHER_M = 400  # the nearest of each other kind within this
STOP_M = 400
BAY_ROWS = 8
TTL_S = 60  # a facility is read again after a minute (its bays and bikes move)
SOURCE_NAMES = {
    "nap_public_works": "Public Works Department inventory",
    "nap_chargers": "EV charger inventory (EMS)",
    "nap_nextbike": "Nextbike station inventory",
    "nap_bolt": "Bolt scooter zone inventory",
}
NEAREST_LABELS = {"car_park": "Nearest car park", "park_and_ride": "Nearest park and ride",
                  "charger": "Nearest EV charger", "bike_station": "Nearest Nextbike station",
                  "scooter_zone": "Nearest Bolt scooter zone", "bike_rack": "Nearest bicycle rack"}
STATUS_WORDS = {"operational": ("listed_in_service", "Listed as in service", "ok"),
                "unavailable": ("listed_out_of_service", "Listed as out of service", "warn")}


def facilities_of(ents) -> Facilities | None:
    m = getattr(ents, "mobility", None)
    return getattr(m, "facilities", None) if m is not None else None


def _iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, timezone.utc)
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def day(dt: datetime | None) -> str | None:
    return f"{dt.astimezone(TZ):%d %b %Y}".lstrip("0") if dt else None


def _state(value: str, label: str, color: str, tone: str, as_of=None, reason=None, now=None) -> dict:
    age = (now - as_of).total_seconds() if isinstance(as_of, datetime) and now else None
    return {"value": value, "label": label, "color": color, "tone": tone, "stale": False,
            "reason": reason, "local": None, "as_of": _iso(as_of), "age_s": round(age) if age is not None else None}


# -- where it comes from ------------------------------------------------------------------------


def inventory_note(fac: Facilities, kind: str) -> dict:
    """How old the inventory is, for the page and the model."""
    inv = fac.cached(kind)
    feed = FEEDS[kind]
    if inv is None:
        return {"source": feed.source, "page": feed.page}
    return {"source": feed.source, "page": feed.page, "updated": _iso(inv.updated),
            "published": _iso(inv.published), "read_at": _iso(inv.read_at), "error": inv.error,
            "updated_label": day(inv.updated), "published_label": day(inv.published)}


def inventory_source(fac: Facilities, kind: str) -> dict:
    inv = fac.cached(kind)
    feed = FEEDS[kind]
    when = None
    if inv is not None:
        when = (f"last changed {day(inv.updated)}, " if inv.updated else "") + f"republished {day(inv.published) or 'nightly'}"
    detail = ", ".join(x for x in ("National Access Point", LICENCE.split(",")[0], when,
                                   "an older copy: the last refresh failed" if inv and inv.error else None) if x)
    return {"source": feed.source, "name": SOURCE_NAMES[feed.source], "detail": detail, "url": feed.page,
            "as_of": _iso(inv.read_at) if inv else None}


# -- what is near ---------------------------------------------------------------------------------


def nearest_of_each(fac: Facilities, f: Facility, bikes=None) -> list[dict]:
    fac.warm()
    out = []
    for kind in KINDS:
        if kind == f.kind:
            continue
        inv = fac.cached(kind)
        if inv is None:
            continue
        hit = fac.near(f.lat, f.lon, NEAR_OTHER_M, (kind,), limit=1, items=inv.items)
        if not hit:
            continue
        d, g = hit[0]
        rel = {"relation": "nearby", "label": NEAREST_LABELS[kind], "id": g.id, "kind": kind,
               "name": g.name, "distance_m": round(d), "lon": g.lon, "lat": g.lat, "color": COLORS[kind]}
        if kind == "bike_station" and bikes is not None and (st := bikes.at(g.info.get("station_id", ""))):
            rel["detail"] = bike_words(st)
        elif kind == "charger":
            rel["detail"] = charger_line(g)
        out.append(rel)
    return out


def street_relation(address: str | None) -> list[dict]:
    first = (address or "").split(",")[0].strip()
    street = E.street_of(first)
    if not street or street.lower() in ("lefkosia", "nicosia", "λευκωσία") or len(street) < 3:
        return []
    return [{"relation": "street", "label": "Street", "name": street}]


def osm_car_park(ents, f: Facility) -> dict | None:
    """The OpenStreetMap car park at the same place (it says whether parking is paid)."""
    places = getattr(ents, "places", None)
    if places is None or not places.ready:
        return None
    best = None
    for p in places.all():
        if p.get("category") != "parking_lot" or abs(p["lat"] - f.lat) > 0.0005 or abs(p["lon"] - f.lon) > 0.0006:
            continue
        d = haversine_m(f.lat, f.lon, p["lat"], p["lon"])
        if d <= OSM_M and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


# -- a car park's bay sensors -----------------------------------------------------------------------


def bay_devices(ents, f: Facility, fetch: bool = False) -> list[tuple[float, object]]:
    """The city's parking bay sensors at the car park or beside it (within BAY_M of its point,
    or inside a park and ride's polygon), nearest first."""
    city = getattr(ents, "city", None)
    if city is None:
        return []
    devices = city.cached_devices()
    if not devices and fetch:
        try:
            devices = city.devices()
        except Exception:  # noqa: BLE001 - no inventory: no bays known
            devices = []
    fac = facilities_of(ents)
    cache = fac.search_cache.setdefault("_bays", {}) if fac is not None else {}
    key = (f.id, id(devices), len(devices))
    if key in cache:
        return cache[key]
    out = []
    reach = BAY_M if not f.rings else max(BAY_M, _radius(f) + 30)
    for d in devices:
        if d.type != "Parking" or d.lat is None:
            continue
        if abs(d.lat - f.lat) > 0.003 or abs(d.lon - f.lon) > 0.004:
            continue
        dist = haversine_m(f.lat, f.lon, d.lat, d.lon)
        if dist <= reach:
            out.append((dist, d))
    out.sort(key=lambda t: t[0])
    cache[key] = out
    return out


def _radius(f: Facility) -> float:
    return max((haversine_m(f.lat, f.lon, y, x) for ring in f.rings for x, y in ring), default=0.0)


def bays(ents, f: Facility, at: datetime | None = None, fetch: bool = False) -> dict | None:
    """What the bay sensors at a car park say now (the last snapshot; with fetch, read it when
    there is none) or at a past moment (the twin's memory). None where it has no sensors."""
    near = bay_devices(ents, f, fetch=fetch)
    if not near:
        return None
    names = {d.name for _, d in near}
    states: dict[str, tuple[str, bool, datetime | None]] = {}
    origin = "live"
    error = None
    if at is None:
        city = ents.city
        cached = city.cached_states("Parking")
        if not cached and fetch:
            try:
                cached = {s.device.name: s for s in city.states("Parking")}
            except Exception as e:  # noqa: BLE001 - say the bays aren't known, not that they are free
                error = f"the city's parking sensors didn't answer ({str(e)[:120]})"
        for name, st in cached.items():
            if name in names:
                states[name] = (st.interp.state, st.interp.stale, st.interp.as_of)
    else:
        origin = "memory"
        moments = getattr(ents, "moments", None)
        if moments is None or moments.memory is None:
            error = "the twin's memory is off here, so this moment can't be read"
        else:
            try:
                snap = moments.snapshot("parking_sensor", at)
                error = snap.error
                for d, st in zip(snap.devices, snap.states):
                    if d.device.name in names:
                        states[d.device.name] = (st, st == "unknown", snap.at)
            except Exception as e:  # noqa: BLE001
                error = f"the twin's memory couldn't be read ({str(e)[:120]})"
    rows = []
    counts: Counter = Counter()
    newest = None
    for dist, d in near:
        st, stale, as_of = states.get(d.name, ("unknown", True, None))
        word = st if st in ("free", "occupied") and not stale else "no_reading"
        counts[word] += 1
        if as_of and word != "no_reading" and (newest is None or as_of > newest):
            newest = as_of
        rows.append({"id": d.name, "kind": "parking_sensor", "name": f"Parking bay {d.name}",
                     "distance_m": round(dist), "lon": d.lon, "lat": d.lat, "state": word,
                     "color": STATE_COLORS.get({"no_reading": "unknown"}.get(word, word), STATE_COLORS["unknown"])})
    known = counts["free"] + counts["occupied"]
    return {"bays": len(near), "free": counts["free"], "taken": counts["occupied"],
            "no_reading": counts["no_reading"], "known": known > 0, "as_of": newest, "origin": origin,
            "error": error, "rows": rows, "radius_m": BAY_M}


def bays_words(b: dict) -> str:
    if not b["known"]:
        return f"{b['bays']} bay sensor{'s' if b['bays'] != 1 else ''} beside it, no recent reading"
    if b["free"] + b["taken"] == 1:
        return f"The one sensed bay beside it is {'free' if b['free'] else 'taken'}"
    if b["free"] == 0:
        return f"All {b['free'] + b['taken']} sensed bays beside it taken"
    return f"{b['free']} of {b['free'] + b['taken']} sensed bays beside it free"


# -- the words for a charger and for a station's bikes -----------------------------------------------


def charger_line(f: Facility) -> str:
    i = f.info
    kw = i.get("kw")
    parts = [f"{_num(kw)} kW" if kw else None, "fast" if i.get("speed") == "fast" else None,
             (i.get("power_type") or "").upper() or None,
             ", ".join(CONNECTORS.get(t, t) for t in i.get("connector_types") or []) or None]
    return ", ".join(p for p in parts if p)


def _num(v: float | None) -> str:
    if v is None:
        return "?"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def bike_words(st: dict) -> str:
    if not st.get("installed", True) or not st.get("renting", True):
        return "not renting now"
    n, e = st["bikes"], st.get("ebikes") or 0
    if n == 0:
        return "no bikes now"
    return f"{n} bike{'s' if n != 1 else ''} now" + (f", {e} electric" if e else "")


# -- building one facility ----------------------------------------------------------------------------


def build_facility(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    fac = facilities_of(ents)
    if fac is None:
        return None
    try:
        f = fac.get(eid)
    except Exception as e:  # noqa: BLE001 - the feed is down and nothing is on disk
        raise E.EntityNotFound(f"the national inventory for {eid} couldn't be read just now ({str(e)[:120]})") from None
    if f is None:
        return None
    now = at or ents.now()
    builder = {"car_park": _car_park, "park_and_ride": _park_and_ride, "charger": _charger,
               "bike_station": _bike_station, "scooter_zone": _scooter_zone, "bike_rack": _bike_rack}[f.kind]
    e = builder(ents, fac, f, now, at)
    e.setdefault("sources", []).insert(0, inventory_source(fac, f.kind))
    e["inventory"] = inventory_note(fac, f.kind)
    e["facility_kind"] = f.kind
    e["color"] = COLORS[f.kind]
    if e.get("center") is None:
        e["center"] = [f.lon, f.lat]
    if e.get("geometry") is None:
        e["geometry"] = ({"type": "MultiPolygon", "coordinates": [[[list(p) for p in r]] for r in f.rings]}
                         if f.rings else E.point_geom(f.lon, f.lat))
    e.setdefault("_for_model", {})["inventory"] = (
        f"national inventory, last changed {e['inventory'].get('updated_label') or 'on an unknown date'}"
        f", republished nightly: not live")
    return e


def _relations(ents, fac: Facilities, f: Facility, bikes=None, address: str | None = None) -> list[dict]:
    rel = street_relation(address)
    rel += E.nearest_stop(ents, f.lat, f.lon, STOP_M)
    rel += nearest_of_each(fac, f, bikes)
    return rel


def _car_park(ents, fac: Facilities, f: Facility, now: datetime, at: datetime | None) -> dict:
    b = bays(ents, f, at)
    osm = osm_car_park(ents, f)
    readings, relations = [], []
    sources = []
    if b and b["known"]:
        free_color = STATE_COLORS["free"] if b["free"] else STATE_COLORS["occupied"]
        state = _state("bays_free" if b["free"] else "bays_taken", bays_words(b), free_color,
                       "ok" if b["free"] else "warn", b["as_of"], now=now,
                       reason=f"the city's bay sensors within {BAY_M} m of it; its own spaces aren't counted")
        readings = [{"key": "free_bays", "label": "Sensed bays free", "value": b["free"], "unit": "",
                     "as_of": _iso(b["as_of"])},
                    {"key": "taken_bays", "label": "Sensed bays taken", "value": b["taken"], "unit": "",
                     "as_of": _iso(b["as_of"])}]
    elif b:
        state = _state("bays_unknown", bays_words(b), STATE_COLORS["unknown"], "muted",
                       reason=b["error"] or "none of its bay sensors has reported lately")
    else:
        state = _state("inventory", "No live occupancy", COLORS["car_park"], "muted",
                       reason="no city parking sensor at it or beside it")
    if b:
        relations += [{"relation": "bay", "label": "Bay sensor beside it", **{k: r[k] for k in
                       ("id", "kind", "name", "distance_m", "lon", "lat", "state", "color")}}
                      for r in b["rows"][:BAY_ROWS]]
        sources.append({"source": "twin_memory" if b["origin"] == "memory" else "smart_nicosia",
                        "name": "The twin's memory" if b["origin"] == "memory" else "Smart Nicosia platform",
                        "detail": f"{b['bays']} bay sensor{'s' if b['bays'] != 1 else ''} within {BAY_M} m",
                        "as_of": _iso(b["as_of"])})
    fee = (osm or {}).get("fee")
    if osm is not None:
        relations.insert(0, {"relation": "same", "label": "Same car park on OpenStreetMap", "id": osm["id"],
                             "kind": "place", "name": osm.get("name") or "Car park",
                             "detail": {"yes": "paid parking", "no": "free parking"}.get(fee or ""),
                             "lon": osm["lon"], "lat": osm["lat"]})
        sources.append({"source": "openstreetmap", "name": "OpenStreetMap", "detail": "whether parking is paid"})
    name = f.name
    if not f.named and osm and osm.get("name"):
        name = osm["name"]
    municipal = "(municipal)" in f.name.lower() or "(municpal)" in f.name.lower()
    attrs = [E.attr("In Greek", f.name_el),
             E.attr("Run by", "The municipality" if municipal else None),
             E.attr("Price", {"yes": "Paid, as OpenStreetMap has it", "no": "Free, as OpenStreetMap has it"}.get(fee or "")),
             E.attr("Spaces", "Not given in the national inventory"),
             E.attr("Live occupancy", f"From {b['bays']} city bay sensor{'s' if b['bays'] != 1 else ''} within {BAY_M} m"
                    if b else "None: no city sensor at it or beside it"),
             E.attr("Mapped by", "OpenStreetMap, through the national inventory" if not f.named else None)]
    readings = []  # the headline shows the bays (components/facilities/sections.tsx)
    return E.new_entity(
        f.id, "car_park", name,
        subtitle=", ".join(x for x in ("Municipal" if municipal else None, f.name_el if f.named else "Mapped on OpenStreetMap, no name",
                                        bays_words(b).lower() if b and b["known"] else None) if x) or None,
        state=state, readings=readings, attributes=attrs,
        relations=relations + _relations(ents, fac, f), sources=sources,
        as_of=_iso(b["as_of"]) if b and b["known"] else None,
        # The free bays of the last 24 hours come from the twin's memory, where it holds them.
        has_series=bool(b) and remembers(ents, {r["id"] for r in b["rows"]}, at or now),
        bays={k: v for k, v in b.items() if k != "as_of"} | {"as_of": _iso(b["as_of"])} if b else None,
        fee=fee,
        _for_model={"live_bays": ({k: b[k] for k in ("bays", "free", "taken", "no_reading")} |
                                  {"note": f"the city's bay sensors within {BAY_M} m; the car park's own spaces aren't counted"})
                    if b else "no live occupancy here", "paid": {"yes": True, "no": False}.get(fee or "")})


def _park_and_ride(ents, fac: Facilities, f: Facility, now: datetime, at: datetime | None) -> dict:
    lines = []
    tt = ents.mobility.timetable() if ents.mobility is not None else None
    if tt is not None:
        seen = {}
        for _, s in tt.near(f.lat, f.lon, max(300.0, _radius(f) + 150), limit=8):
            for ln in tt.lines_at(s):
                seen.setdefault(ln.id, ln)
        lines = list(seen.values())
    rel = [{"relation": "line", "label": "Bus line", "id": ln.id, "kind": "bus_line",
            "name": f"Line {ln.short}", "detail": ln.name, "color": ln.color} for ln in lines[:6]]
    area = _area_m2(f)
    served = ", ".join(ln.short for ln in lines[:4])
    state = _state("park_and_ride", f"Park and ride, lines {served}" if served else "Park and ride",
                   COLORS["park_and_ride"], "muted")
    return E.new_entity(
        f.id, "park_and_ride", f.name, subtitle=f"Buses {served}" if served else f.name_el,
        state=state,
        attributes=[E.attr("In Greek", f.name_el), E.attr("Area", f"about {area / 10_000:.1f} hectares" if area else None),
                    E.attr("Buses from here", ", ".join(f"{ln.short} ({ln.operator})" for ln in lines[:6]) or None),
                    E.attr("Spaces", "Not given in the national inventory")],
        relations=rel + _relations(ents, fac, f),
        _for_model={"bus_lines_here": [ln.short for ln in lines]})


def _area_m2(f: Facility) -> float:
    total = 0.0
    for ring in f.rings:
        kx = 111_320 * math.cos(math.radians(f.lat))
        pts = [(x * kx, y * 110_574) for x, y in ring]
        total += abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:]))) / 2
    return total


def _charger(ents, fac: Facilities, f: Facility, now: datetime, at: datetime | None) -> dict:
    i = f.info
    value, label, tone = STATUS_WORDS.get(i.get("status") or "", ("not_listed", "No status listed", "muted"))
    color = COLORS["charger"] if tone == "ok" else "#f59e0b" if tone == "warn" else "#8798b0"
    # No as_of: the listing isn't a reading (the inventory note says how old it is).
    state = _state(value, label, color, tone,
                   reason="the national inventory's status, republished nightly: not whether a connector is free now")
    kw, total = i.get("kw"), i.get("max_kw")
    readings = []
    if kw:
        readings.append({"key": "kw", "label": "Power", "value": float(kw), "unit": "kW"})
    if i.get("connectors"):
        readings.append({"key": "connectors", "label": "Connectors", "value": i["connectors"], "unit": ""})
    power = (f"{_num(kw)} kW per connector" + (f", {_num(total)} kW in all" if total and kw and total > kw else "")) if kw else None
    contact = "; ".join(x for c in i.get("contacts") or [] for x in (c.get("phone"), c.get("email")) if x) or None
    mode_note = None
    if i.get("mode") == "fastCharging" and i.get("speed") != "fast":
        mode_note = "The listing says fast charging, but gives less than 43 kW"
    attrs = [E.attr("Power", power), E.attr("Speed", "Fast (43 kW or more)" if i.get("speed") == "fast" else "Standard"),
             E.attr("Current", {"ac": "AC", "dc": "DC"}.get(i.get("power_type") or "")),
             E.attr("Connectors", ", ".join(CONNECTORS.get(t, t) for t in i.get("connector_types") or []) or None),
             E.attr("Note", mode_note), E.attr("Operator", i.get("operator")), E.attr("Owner", i.get("owner")),
             E.attr("Hours", i.get("hours")), E.attr("Access", i.get("access")),
             E.attr("Parking spaces", i.get("spaces")), E.attr("Accessible spaces", i.get("accessible_spaces")),
             E.attr("Address", f.address), E.attr("Support", contact), E.attr("Listed since", i.get("since")),
             E.attr("Charger code", i.get("code"))]
    readings = []  # the headline shows the power and connectors
    return E.new_entity(
        f.id, "charger", f.name, subtitle=charger_line(f) or None,
        state=state, readings=readings, attributes=attrs,
        relations=_relations(ents, fac, f, address=f.address),
        charger={k: i.get(k) for k in ("kw", "max_kw", "speed", "power_type", "connector_types", "connectors", "status", "operator", "hours")},
        _for_model={"listed_status": i.get("status"), "kw_per_connector": kw, "fast": i.get("speed") == "fast",
                    "connectors": i.get("connector_types"), "current": i.get("power_type"),
                    "status_note": "the inventory's nightly status, not live availability"})


def _bike_station(ents, fac: Facilities, f: Facility, now: datetime, at: datetime | None) -> dict:
    sid = f.info.get("station_id", "")
    bikes = st = None
    if at is None:
        try:
            bikes = fac.bikes()
        except Exception:  # noqa: BLE001
            bikes = None
        st = bikes.at(sid) if bikes else None
    info = fac.station_info(sid) or {}
    cap = f.capacity or info.get("capacity")
    readings = []
    if at is not None:
        state = _state("live_only", "Live bikes are shown for now only", COLORS["bike_station"], "muted",
                       reason="the twin doesn't keep Nextbike's counts, so this moment isn't known")
    elif st is None:
        why = (bikes.error if bikes and bikes.error else "this station isn't in Nextbike's live feed")
        state = _state("no_live", "Live bikes unavailable", "#8798b0", "muted", reason=why)
    else:
        reported = datetime.fromtimestamp(st["reported"], timezone.utc) if st.get("reported") else None
        words = bike_words(st)
        tone = "live" if st["bikes"] and st.get("renting", True) else "warn"
        state = _state("bikes" if st["bikes"] else "empty", words[0].upper() + words[1:], "#ffae45" if tone == "live" else "#f59e0b",
                       tone, reported, now=now, reason="Nextbike's live feed")
        readings = [{"key": "bikes", "label": "Bikes", "value": st["bikes"], "unit": "", "as_of": _iso(reported)},
                    {"key": "ebikes", "label": "Electric", "value": st.get("ebikes") or 0, "unit": "", "as_of": _iso(reported)}]
        if st.get("docks") is not None and not info.get("virtual"):
            readings.append({"key": "docks", "label": "Free docks", "value": st["docks"], "unit": "", "as_of": _iso(reported)})
    sources = []
    if st is not None:
        sources.append({"source": "nextbike_live", "name": "Nextbike live feed (GBFS)", "detail": GBFS_LICENCE,
                        "url": GBFS_PAGE, "as_of": _iso(bikes.feed_time or bikes.fetched_at)})
    attrs = [E.attr("Docks", cap), E.attr("Station number", info.get("short_name")),
             E.attr("Kind", "A marked spot with no docks: leave the bike there and lock it" if info.get("virtual") else None),
             E.attr("Also listed as", ", ".join(f.info.get("also_listed") or []) or None),
             E.attr("Rent one", info.get("web")),
             E.attr("Run by", "Nextbike Cyprus")]
    readings = []  # the headline shows the bikes now
    return E.new_entity(
        f.id, "bike_station", f.name, subtitle=bike_words(st).capitalize() if st else "Nextbike Cyprus",
        state=state, readings=readings, attributes=attrs, relations=_relations(ents, fac, f, bikes),
        sources=sources, as_of=state.get("as_of"),
        bikes=({**st, "virtual": bool(info.get("virtual")), "capacity": cap} if st else None),
        _for_model={"live": ({k: st.get(k) for k in ("bikes", "ebikes", "docks", "renting")} if st
                             else "unknown"), "docks": cap})


def _scooter_zone(ents, fac: Facilities, f: Facility, now: datetime, at: datetime | None) -> dict:
    area = _area_m2(f)
    state = _state("zone", f"Parking for {f.capacity} scooters" if f.capacity else "Scooter parking zone",
                   COLORS["scooter_zone"], "muted",
                   reason="Bolt's zone from the national inventory; where the scooters are now isn't published")
    return E.new_entity(
        f.id, "scooter_zone", f.name, subtitle=f"Holds {f.capacity} scooters" if f.capacity else "Bolt",
        state=state,
        readings=[{"key": "capacity", "label": "Scooters it holds", "value": f.capacity, "unit": ""}] if f.capacity else [],
        attributes=[E.attr("Holds", f"{f.capacity} scooters" if f.capacity else None),
                    E.attr("Size", f"about {round(area)} m²" if area else None),
                    E.attr("Run by", "Bolt"),
                    E.attr("Where the scooters are", "Not published openly: Bolt's app shows them")],
        relations=_relations(ents, fac, f))


def _bike_rack(ents, fac: Facilities, f: Facility, now: datetime, at: datetime | None) -> dict:
    state = _state("rack", "Bicycle rack", COLORS["bike_rack"], "muted", reason="for your own bike")
    return E.new_entity(
        f.id, "bike_rack", f.name, subtitle=f.address or f.name_el,
        state=state, attributes=[E.attr("In Greek", f.name_el), E.attr("Address", f.address),
                                 E.attr("Spaces", "Not given in the national inventory")],
        relations=_relations(ents, fac, f, address=f.name))


# -- the city clock --------------------------------------------------------------------------------


def facility_at(ents, entity: dict, at: datetime) -> dict:
    """A facility at a past moment: the inventory as it is (it has no history), a car park's
    bays from the twin's memory then, and a station's bikes unknown then."""
    from chatbot.moments import label

    kind = entity["kind"]
    e = build_facility(ents, entity["id"], None, at=at) or dict(entity)
    applies = kind in ("car_park", "bike_station") and (kind != "car_park" or bool(e.get("bays")))
    known = bool((e.get("bays") or {}).get("known")) if kind == "car_park" else False
    e["moment"] = {"at": _iso(at), "label": label(at), "known": known, "applies": applies}
    if kind == "car_park" and e.get("bays") and not known:
        e["state"] = {**e["state"], "label": "No bay readings recorded then"}
    return e


def remembers(ents, names: set[str], at: datetime) -> bool:
    """Whether the twin's memory holds readings of these bays in the six hours around `at` (one
    block of the city clock, which it reads anyway)."""
    moments = getattr(ents, "moments", None)
    if moments is None or moments.memory is None or not names:
        return False
    try:
        b = moments.block("Parking", at)
    except Exception:  # noqa: BLE001 - not known: don't promise a chart
        return False
    idx = [j for j, d in enumerate(b.devices) if d.device.name in names]
    return any(b.covered[k] > 0 and any(b.states[k][j] in ("free", "occupied") for j in idx)
               for k in range(len(b.times)) if b.covered)


def car_park_series(ents, entity: dict, end: datetime | None = None) -> dict | None:
    """Free bays beside a car park over the 24 hours up to `end`, from the twin's memory."""
    fac = facilities_of(ents)
    moments = getattr(ents, "moments", None)
    if fac is None or moments is None or moments.memory is None:
        return None
    f = fac.get(entity["id"])
    near = bay_devices(ents, f) if f else []
    if not near:
        return None
    names = {d.name for _, d in near}
    end = end or ents.now()
    start = end - timedelta(hours=24)
    by: dict[datetime, tuple[int, int]] = {}
    t = start
    seen_blocks = set()
    while t <= end + timedelta(hours=6):
        b = moments.block("Parking", min(t, end))
        t += timedelta(hours=6)
        if id(b) in seen_blocks:
            continue
        seen_blocks.add(id(b))
        idx = [j for j, d in enumerate(b.devices) if d.device.name in names]
        for k, when in enumerate(b.times):
            if when < start or when > end or not b.covered or b.covered[k] <= 0:
                continue
            states = [b.states[k][j] for j in idx]
            free = sum(1 for s in states if s == "free")
            known = sum(1 for s in states if s in ("free", "occupied"))
            if known:
                by[when] = (free, known)
    if len(by) < 2:
        return None
    points = []
    step = timedelta(minutes=15)
    t = min(by)
    while t <= max(by):
        v = by.get(t)
        points.append({"t": t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"), "v": v[0] if v else None})
        t += step
    vals = [p["v"] for p in points if p["v"] is not None]
    return {"metric": "free_bays", "label": "Sensed bays free beside it", "unit": "bays", "draw": "line",
            "step_min": 15, "points": points, "from": points[0]["t"], "to": points[-1]["t"],
            "summary": E.summary(vals), "source": "memory",
            "threshold": {"value": len(near), "label": f"{len(near)} sensed bays"}}


# -- search ------------------------------------------------------------------------------------------

GROUPS = {"car_park": "Car parks", "park_and_ride": "Park and ride", "charger": "EV chargers",
          "bike_station": "Bike hire", "scooter_zone": "Scooter zones", "bike_rack": "Bicycle racks"}
SEARCH_WORDS = {"car_park": "car park parking", "park_and_ride": "park and ride park & ride",
                "charger": "ev charger charging electric car", "bike_station": "nextbike bike hire bicycle",
                "scooter_zone": "bolt scooter e-scooter", "bike_rack": "bicycle rack bike parking"}


def search_items(ents) -> list[dict]:
    """The facilities already read, for Ctrl K (never waits for a feed: the first search starts
    reading them in the background)."""
    fac = facilities_of(ents)
    if fac is None:
        return []
    hit = fac.search_cache.get("items")
    if hit is not None and time.time() - hit[0] < 600:
        return hit[1]
    missing = [k for k in KINDS if fac.cached(k) is None]
    if missing:
        fac.warm(missing)
    items = []
    for kind in KINDS:
        inv = fac.cached(kind)
        if inv is None:
            continue
        for f in inv.items:
            if not f.in_nicosia or (kind == "car_park" and not f.named):
                continue
            detail = {"charger": charger_line(f), "scooter_zone": f"{f.capacity} scooters" if f.capacity else None,
                      "bike_rack": f.address}.get(kind)
            items.append(E.index_item(f.id, kind, f.name, GROUPS[kind],
                                      ", ".join(x for x in (LABELS[kind], detail) if x), f.lat, f.lon,
                                      [*f.search_words(), SEARCH_WORDS[kind]]))
    if not missing:
        fac.search_cache["items"] = (time.time(), items)
    return items


def _is_facility(eid: str) -> bool:
    low = eid.strip().lower()
    return any(low.startswith(p) for p in ID_PREFIX.values())


for _kind in KINDS:
    E.register_kind(E.Kind(_kind, lambda i, k=_kind: i.strip().lower().startswith(ID_PREFIX[k]),
                           build_facility, ttl_s=TTL_S), first=True)
    E.register_at(_kind, facility_at)
E.register_series("car_park", car_park_series)
E.register_search(search_items, {"Car parks": 4, "Park and ride": 2, "EV chargers": 4, "Bike hire": 4,
                                 "Scooter zones": 3, "Bicycle racks": 2})

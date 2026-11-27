"""The street rules' kinds of the twin's entities, each in the one shape the inspector opens:

- speed_limit: a speed limit sign (limit-295) with the stretch it governs, or a street whose
  limit only OpenStreetMap gives (limitway-24885278);
- slow_zone: a 20 or 30 km/h zone (zone-7), and slow_zone_entrance: one of its entrances;
- traffic_signal (signal-12), pedestrian_crossing (xing-154), enforcement_camera (ecam-30);
- cycleway (cycle-38) and bus_lane (buslane-11);
- school (school-w123456): a school with how it is served for crossing.

They are inventories: at any moment of the city clock they read as they are, and say so. They
relate to their street (OpenStreetMap), the Bluetooth road segments beside them
(chatbot/mobility/roads.py), the zone they lie in, and the schools and stops near them.
"""

from __future__ import annotations

from datetime import datetime

from chatbot import entities as E
from chatbot.geo import haversine_m
from chatbot.streets import analysis as A
from chatbot.streets.feeds import DATASET_PAGE, FEEDS
from chatbot.streets.layers import (BUS_LANE_COLOR, CAMERA_COLOR, CROSSING_COLOR, CYCLE_COLOR,
                                    SIGNAL_COLOR, TIER_LABELS, ZONE_COLOR, limit_color)
from chatbot.streets.network import CLASS_LABELS, cumulative, project

E.KIND_LABELS.update({
    "speed_limit": "Speed limit", "slow_zone": "Slow zone", "slow_zone_entrance": "Zone entrance",
    "traffic_signal": "Traffic signals", "pedestrian_crossing": "Pedestrian crossing",
    "enforcement_camera": "Enforcement camera", "cycleway": "Cycleway", "bus_lane": "Bus lane",
    "school": "School",
})
OSM_WAY = "https://www.openstreetmap.org/way/"
INVENTORY_NOTE = "an inventory, not a reading: the same at every moment of the clock"


def _streets(ents):
    s = getattr(ents, "streets", None)
    if s is None:
        raise E.EntityNotFound("the street rules aren't available in this deployment")
    return s


def day(iso: str | None) -> str | None:
    """'2025-03-10' -> '10 Mar 2025'."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso[:10])
    except ValueError:
        return iso[:10]
    return f"{d.day} {d:%b %Y}"


def source(st, key: str) -> dict:
    """The inventory a record comes from, with how old it is."""
    f = FEEDS[key]
    age = st.age(key)
    detail = ", ".join(x for x in (f.publisher, f"updated {day(age['updated'])}" if age["updated"] else None,
                                   "a saved copy" if age["copy"] == "saved" and age["error"] else None,
                                   "CC BY 4.0") if x)
    return {"source": SOURCE_IDS[key], "name": f"{f.title}, National Access Point", "detail": detail,
            "url": DATASET_PAGE + f.dataset, "as_of": (age["updated"] or (age["published"] or "")[:10]) or None}


SOURCE_IDS = {"speed_limits": "speed_limits", "zones": "slow_zones", "zone_entrances": "slow_zones",
              "signals": "traffic_signals", "crossings": "pedestrian_crossings", "cycleways": "cycleways",
              "bus_lanes": "bus_lanes", "cameras": "enforcement_cameras"}


def _state(value: str, label: str, color: str, tone: str = "muted", reason: str | None = None) -> dict:
    return {"value": value, "label": label, "color": color, "tone": tone, "stale": False,
            "reason": reason, "local": None, "as_of": None, "age_s": None}


def _osm_source(st) -> dict:
    net = st.network.current()
    return {"source": "openstreetmap", "name": "OpenStreetMap streets", "detail": "names, classes and mapped limits, ODbL",
            "url": "https://www.openstreetmap.org/copyright",
            "as_of": (net.osm_base or "")[:10] if net and net.osm_base else None}


# -- relations shared by every kind -----------------------------------------------------------------


def street_rel(st, lon: float, lat: float) -> list[dict]:
    name = st.street_label_at(lon, lat)
    return [{"relation": "street", "label": "Street", "name": name}] if name else []


def road_rels(ents, coords: list[list[float]], max_m: float = 60.0, limit: int = 2) -> list[dict]:
    """The Bluetooth road segments running along these points (the road speeds)."""
    m = getattr(ents, "mobility", None)
    if m is None:
        return []
    try:
        segs = m.roads.segments()
    except Exception:  # noqa: BLE001 - the road inventory isn't in yet
        return []
    found = []
    for s in segs:
        best = min((project(p, s.coords[i], s.coords[i + 1])[1] for p in coords[:: max(1, len(coords) // 6)] or coords
                    for i in range(len(s.coords) - 1)), default=1e9)
        if best <= max_m:
            found.append((best, s))
    found.sort(key=lambda t: t[0])
    return [{"relation": "road", "label": "Road speeds here", "id": s.id, "kind": "road_segment",
             "name": s.name, "detail": f"{round(d)} m away" if d > 15 else "along it"} for d, s in found[:limit]]


def zone_rel(st, lon: float, lat: float) -> list[dict]:
    z = st.zone_at(lon, lat)
    if not z:
        return []
    return [{"relation": "zone", "label": "In the zone", "id": z["id"], "kind": "slow_zone",
             "name": f"{z['kind']}, {z['name']}", "detail": z["status"], "color": ZONE_COLOR}]


def near_rels(st, lat: float, lon: float, skip: str | None = None, max_m: float = 250) -> list[dict]:
    out = []
    for key, kind, label in (("crossings", "pedestrian_crossing", "Nearest crossing"),
                             ("signals", "traffic_signal", "Nearest signals")):
        hits = [(d, r) for d, r in A.nearest(st.records(key), lat, lon, max_m) if r["id"] != skip]
        if hits:
            d, r = hits[0]
            out.append({"relation": key, "label": label, "id": r["id"], "kind": kind,
                        "name": r.get("name") or r.get("junction") or E.KIND_LABELS[kind],
                        "distance_m": round(d), "lon": r["lon"], "lat": r["lat"]})
    sch = [(d, s) for d, s in A.nearest(st.schools(), lat, lon, max_m) if s["id"] != skip]
    if sch:
        d, s = sch[0]
        out.append({"relation": "school", "label": "Nearest school", "id": s["id"], "kind": "school",
                    "name": s["name"], "distance_m": round(d), "lon": s["lon"], "lat": s["lat"]})
    return out


def _inventory(e: dict) -> dict:
    e["inventory"] = True
    return e


# -- speed limits --------------------------------------------------------------------------------------


def build_limit(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    if eid.lower().startswith("limitway-"):
        return build_mapped(ents, st, eid)
    hit = st.by_id(eid)
    if hit is None:
        return None
    _, s = hit
    lim = st.limits()
    pieces = [p for p in (lim.pieces if lim else []) if p["sign"] == s["id"]]
    snap = st.sign_snap(s["id"])
    lon, lat = (snap.point if snap else (s["lon"], s["lat"]))
    lines = [p["coords"] for p in pieces]
    geometry = ({"type": "MultiLineString", "coordinates": lines} if lines else E.point_geom(lon, lat))
    by_tier: dict[str, float] = {}
    for p in pieces:
        by_tier[p["tier"]] = by_tier.get(p["tier"], 0) + p["length_m"]
    alts = sorted({p["alt_kmh"] for p in pieces if p.get("alt_kmh")})
    street = st.street_label_at(lon, lat) if snap else None
    osm = next((p["osm_kmh"] for p in pieces if p.get("osm_kmh")), None)
    readings = [{"key": "kmh", "label": "Posted limit", "value": s["kmh"], "unit": "km/h"}]
    if pieces:
        readings.append({"key": "stretch_m", "label": "Stretch it governs", "value": round(sum(by_tier.values())), "unit": "m"})
    attrs = [E.attr("On", street), E.attr("National road", s.get("road_ref")),
             E.attr("Placed", f"on the street, {round(snap.dist)} m from where the inventory puts it" if snap
                    else "not on any street within 35 m; shown where the inventory puts it"),
             E.attr("Posted", f"{round(by_tier.get('signed', 0))} m near the sign or between signs that agree" if by_tier.get("signed") else None),
             E.attr("Likely", f"{round(by_tier.get('likely', 0))} m further along" if by_tier.get("likely") else None),
             E.attr("Uncertain", f"{round(by_tier.get('uncertain', 0))} m where it may change to {', '.join(map(str, alts))}"
                    if by_tier.get("uncertain") else None),
             E.attr("OpenStreetMap says", f"{osm} km/h" + (" too" if osm == s["kmh"] else "") if osm else None),
             E.attr("How the stretch is drawn", "carried along the street from the sign, through junctions while the "
                    "street keeps its name, up to 800 m or the next sign; the inventory doesn't say which way a sign faces")]
    rels = street_rel(st, lon, lat) if not street else [{"relation": "street", "label": "Street", "name": street}]
    rels += road_rels(ents, lines[0] if lines else [[lon, lat]])
    rels += zone_rel(st, lon, lat)
    rels += near_rels(st, lat, lon)
    tier = "signed" if by_tier.get("signed") else "likely" if by_tier.get("likely") else "unplaced"
    return _inventory(E.new_entity(
        s["id"], "speed_limit", f"{s['kmh']} km/h" + (f" on {street}" if street else " sign"),
        subtitle=f"Sign on national road {s['road_ref']}" if s.get("road_ref") else "Sign",
        geometry=geometry, center=[round(lon, 6), round(lat, 6)],
        state=_state("posted", f"Posted {s['kmh']} km/h", limit_color(s["kmh"]), "ok",
                     reason=TIER_LABELS.get(tier)),
        readings=readings, attributes=attrs, relations=rels,
        sources=[source(st, "speed_limits"), _osm_source(st)],
        limit={"kmh": s["kmh"], "tiers": {k: round(v) for k, v in by_tier.items()}, "alt_kmh": alts},
        _for_model={"posted_kmh": s["kmh"], "stretch_by_tier_m": {k: round(v) for k, v in by_tier.items()},
                    "may_change_to": alts or None, "osm_kmh": osm, "note": INVENTORY_NOTE}))


def build_mapped(ents, st, eid: str) -> dict | None:
    try:
        way = int(eid.split("-", 1)[1])
    except ValueError:
        return None
    lim = st.limits()
    pieces = [p for p in (lim.pieces if lim else []) if p["tier"] == "mapped" and p["way"] == way]
    if not pieces:
        return None
    p = pieces[0]
    mid = p["coords"][len(p["coords"]) // 2]
    return _inventory(E.new_entity(
        eid, "speed_limit", f"{p['kmh']} km/h on {p['street'] or 'this street'}",
        subtitle=f"Mapped on OpenStreetMap, {CLASS_LABELS.get(p['hw'], p['hw']).lower()}",
        geometry={"type": "MultiLineString", "coordinates": [x["coords"] for x in pieces]}, center=mid,
        state=_state("mapped", f"{p['kmh']} km/h, as mapped", limit_color(p["kmh"]), "muted", reason=TIER_LABELS["mapped"]),
        readings=[{"key": "kmh", "label": "Mapped limit", "value": p["kmh"], "unit": "km/h"},
                  {"key": "length_m", "label": "Length", "value": sum(x["length_m"] for x in pieces), "unit": "m"}],
        attributes=[E.attr("Street", p["street"]), E.attr("Local name", p.get("name_local")),
                    E.attr("Where it comes from", "OpenStreetMap's maxspeed tag: no sign in the national inventory reaches this street")],
        relations=road_rels(ents, p["coords"]) + zone_rel(st, mid[0], mid[1]),
        sources=[{**_osm_source(st), "url": f"{OSM_WAY}{way}"}],
        limit={"kmh": p["kmh"], "tiers": {"mapped": sum(x["length_m"] for x in pieces)}, "alt_kmh": []},
        _for_model={"mapped_kmh": p["kmh"], "note": "from OpenStreetMap, not a national sign"}))


# -- zones -----------------------------------------------------------------------------------------------


def build_zone(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    hit = st.by_id(eid)
    if hit is None:
        return None
    key, z = hit
    if key == "zone_entrances":
        return build_entrance(ents, st, z)
    c = A.zone_contents(st, z)
    xs = [p[0] for p in z["ring"]]
    ys = [p[1] for p in z["ring"]]
    center = [round(sum(xs) / len(xs), 6), round(sum(ys) / len(ys), 6)]
    planned = z["status"] == "planned"
    signs = c["signs_by_kmh"]
    rels = [{"relation": "street", "label": "Street inside", "name": s["street"], "detail": f"{s['length_m']} m"}
            for s in c["streets"][:6]]
    rels += [{"relation": "school", "label": "School inside", "id": s["id"], "kind": "school", "name": s["name"],
              "lon": s["lon"], "lat": s["lat"]} for s in c["schools"][:4]]
    rels += [{"relation": "entrance", "label": "Entrance", "id": e["id"], "kind": "slow_zone_entrance",
              "name": f"Entrance {e['n']}", "lon": e["lon"], "lat": e["lat"]} for e in c["entrances"][:3]]
    rels += road_rels(ents, z["ring"], max_m=10, limit=3)
    return _inventory(E.new_entity(
        z["id"], "slow_zone", f"{z['kind']}, {z['name']}", subtitle=", ".join(x for x in (z.get("town"), f"{z['kmh']} km/h zone") if x),
        geometry={"type": "Polygon", "coordinates": [z["ring"]]}, center=center,
        state=_state(z["status"], "Planned, not yet in force" if planned else z["status"].capitalize(), ZONE_COLOR,
                     "muted" if planned else "ok",
                     reason="the national inventory lists it as planned" if planned else None),
        readings=[{"key": "kmh", "label": "Limit inside", "value": z["kmh"], "unit": "km/h"},
                  {"key": "area_km2", "label": "Area", "value": c["area_km2"], "unit": "km²"},
                  {"key": "street_km", "label": "Streets inside", "value": c["street_km"], "unit": "km"},
                  {"key": "entrances", "label": "Entrances", "value": len(c["entrances"]), "unit": ""}],
        attributes=[E.attr("In Greek", z.get("name_el")), E.attr("Status", z["status"].capitalize()),
                    E.attr("Speed signs posted inside", ", ".join(f"{n} at {k} km/h" for k, n in signs.items())
                           or "none in the national inventory yet"),
                    E.attr("Crossings inside", len(c["crossings"]) or "none in the inventory"),
                    E.attr("Signals inside", len(c["signals"]) or "none in the inventory"),
                    E.attr("Schools inside", len(c["schools"]) or None)],
        relations=rels, sources=[source(st, "zones"), _osm_source(st)],
        _for_model={"status": z["status"], "streets_inside": [s["street"] for s in c["streets"][:15]],
                    "signs_inside_by_kmh": signs, "note": INVENTORY_NOTE}))


def build_entrance(ents, st, e: dict) -> dict:
    zone = next((z for z in st.records("zones") if z["name"] == e.get("zone_name")), None)
    rels = ([{"relation": "zone", "label": "Into", "id": zone["id"], "kind": "slow_zone",
              "name": f"{zone['kind']}, {zone['name']}", "color": ZONE_COLOR}] if zone else [])
    rels += street_rel(st, e["lon"], e["lat"])
    return _inventory(E.new_entity(
        e["id"], "slow_zone_entrance", f"Entrance to {e['kind']} {e.get('zone_name') or ''}".strip(),
        subtitle=e.get("town"), geometry=E.point_geom(e["lon"], e["lat"]), center=[e["lon"], e["lat"]],
        state=_state(e["status"], e["status"].capitalize(), ZONE_COLOR),
        attributes=[E.attr("Zone", e.get("zone_name")), E.attr("Limit beyond it", f"{e['kmh']} km/h" if e.get("kmh") else None)],
        relations=rels, sources=[source(st, "zone_entrances")]))


# -- points: signals, crossings, cameras ---------------------------------------------------------------


def build_signal(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    hit = st.by_id(eid)
    if hit is None:
        return None
    _, s = hit
    piece = st.limit_at(s["lon"], s["lat"], 40)
    rels = [{"relation": "street", "label": "Street", "name": x} for x in s["streets"][:4]]
    cams = [c for c in st.cameras() if c.get("signal") == s["id"] or haversine_m(s["lat"], s["lon"], c["lat"], c["lon"]) <= 80]
    rels += [{"relation": "camera", "label": "Enforcement camera", "id": c["id"], "kind": "enforcement_camera",
              "name": c["name"], "lon": c["lon"], "lat": c["lat"], "color": CAMERA_COLOR} for c in cams[:4]]
    rels += road_rels(ents, [[s["lon"], s["lat"]]], max_m=80)
    rels += zone_rel(st, s["lon"], s["lat"]) + near_rels(st, s["lat"], s["lon"], skip=s["id"])
    return _inventory(E.new_entity(
        s["id"], "traffic_signal", s["junction"] or "Traffic signals", subtitle="Signalled junction" + (f", {s['code']}" if s.get("code") else ""),
        geometry=E.point_geom(s["lon"], s["lat"]), center=[s["lon"], s["lat"]],
        state=_state("signalled", "Signalled junction", SIGNAL_COLOR),
        readings=[{"key": "kmh", "label": "Street limit here", "value": piece["kmh"], "unit": "km/h"}] if piece else [],
        attributes=[E.attr("Run by", s.get("operator")), E.attr("In Greek", s.get("operator_el")),
                    E.attr("Junction code", s.get("code")),
                    E.attr("Live state", "not published: the inventory says where the signals are, not their phases")],
        relations=rels, sources=[source(st, "signals")],
        _for_model={"note": INVENTORY_NOTE}))


def build_crossing(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    hit = st.by_id(eid)
    if hit is None:
        return None
    _, x = hit
    piece = st.limit_at(x["lon"], x["lat"], 40)
    sig = A.nearest(st.records("signals"), x["lat"], x["lon"], 60)
    rels = street_rel(st, x["lon"], x["lat"])
    rels += road_rels(ents, [[x["lon"], x["lat"]]], max_m=80)
    rels += zone_rel(st, x["lon"], x["lat"]) + near_rels(st, x["lat"], x["lon"], skip=x["id"])
    return _inventory(E.new_entity(
        x["id"], "pedestrian_crossing", x["name"], subtitle=x.get("street") or "Marked crossing",
        geometry=E.point_geom(x["lon"], x["lat"]), center=[x["lon"], x["lat"]],
        state=_state("signalled" if sig else "crossing", "At signals" if sig else "Pedestrian crossing", CROSSING_COLOR),
        readings=[{"key": "kmh", "label": "Traffic limit here", "value": piece["kmh"], "unit": "km/h"}] if piece else [],
        attributes=[E.attr("Street", x.get("street")), E.attr("Near", x.get("near")),
                    E.attr("Signals", f"at a signalled junction {round(sig[0][0])} m away" if sig else "none within 60 m")],
        relations=rels, sources=[source(st, "crossings")], _for_model={"note": INVENTORY_NOTE}))


def build_camera(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    hit = st.by_id(eid)
    if hit is None:
        return None
    _, c = hit
    piece = st.limit_at(c["lon"], c["lat"], 50)
    placed = {"feed": "where the inventory puts it",
              "junction": "at the signalled junction its name gives: the inventory gives its position only to about a kilometre",
              "rough": "only to about a kilometre: the inventory writes its position to two decimals"}[c.get("placed", "feed")]
    rels = [{"relation": "street", "label": "Watches", "name": c["street"]}] if c.get("street") else []
    if c.get("at"):
        rels.append({"relation": "street", "label": "At", "name": c["at"]})
    if c.get("signal"):
        sg = st.by_id(c["signal"])
        if sg:
            rels.append({"relation": "signal", "label": "Junction", "id": sg[1]["id"], "kind": "traffic_signal",
                         "name": sg[1]["junction"], "lon": sg[1]["lon"], "lat": sg[1]["lat"]})
    rels += road_rels(ents, [[c["lon"], c["lat"]]], max_m=80)
    return _inventory(E.new_entity(
        c["id"], "enforcement_camera", c["name"],
        subtitle=", ".join(x for x in (f"Camera {c['code']}" if c.get("code") else "Enforcement camera", c.get("unit"),
                                       c.get("direction")) if x),
        geometry=E.point_geom(c["lon"], c["lat"]), center=[c["lon"], c["lat"]],
        state=_state("rough" if c.get("placed") == "rough" else "listed",
                     "Position rough" if c.get("placed") == "rough" else "Enforcement camera", CAMERA_COLOR,
                     "warn" if c.get("placed") == "rough" else "muted"),
        readings=[{"key": "kmh", "label": "Street limit here", "value": piece["kmh"], "unit": "km/h"}] if piece else [],
        attributes=[E.attr("Watches traffic", c.get("direction")), E.attr("In Greek", c.get("name_el")),
                    E.attr("Placed", placed),
                    E.attr("What it checks", "not said: the inventory lists it as a traffic enforcement camera "
                           "(the fixed cameras check speed and red lights)"),
                    E.attr("Unit", c.get("unit"))],
        relations=rels, sources=[source(st, "cameras")], _for_model={"note": INVENTORY_NOTE}))


# -- lines: cycleways and bus lanes -----------------------------------------------------------------------


def _line_center(lines) -> list[float]:
    longest = max(lines, key=lambda line: cumulative(line)[-1])
    return A.centre_of(longest)


def build_cycleway(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    hit = st.by_id(eid)
    if hit is None:
        return None
    _, c = hit
    net = A.cycle_network(st)
    pieces = sorted({net["piece_no"][net["comp_of"][i]] for i, (rec, _) in enumerate(net["lines"]) if rec["id"] == c["id"]})
    ends = [e for e in net["loose_ends"] if e["id"] == c["id"]]
    gaps = [g for g in net["gaps"] if set(g["between"]) & set(pieces)]
    center = _line_center(c["lines"])
    rels = [{"relation": "street", "label": "Along", "name": n} for n in
            dict.fromkeys(st.street_label_at(*A.centre_of(line), 40) for line in c["lines"][:8]) if n][:4]
    for p in net["pieces"]:
        if set(p["ids"]) - {c["id"]} and p["piece"] in pieces:
            rels += [{"relation": "joined", "label": "Joined to", "id": i, "kind": "cycleway",
                      "name": next(r["name"] for r, _ in net["lines"] if r["id"] == i)} for i in p["ids"] if i != c["id"]][:3]
    rels += road_rels(ents, center and [center], max_m=60)
    length = A.length_of(c["lines"])
    return _inventory(E.new_entity(
        c["id"], "cycleway", c["name"], subtitle=f"Cycle route, {length / 1000:.1f} km",
        geometry={"type": "MultiLineString", "coordinates": c["lines"]}, center=center,
        state=_state("cycleway", f"Part of piece {', '.join(map(str, pieces))} of {len(net['pieces'])}", CYCLE_COLOR),
        readings=[{"key": "length_km", "label": "Length", "value": round(length / 1000, 2), "unit": "km"},
                  {"key": "loose_ends", "label": "Loose ends", "value": len(ends), "unit": ""}],
        attributes=[E.attr("In Greek", c.get("name_el")), E.attr("Sections", len(c["lines"])),
                    E.attr("Nearest gap", f"{gaps[0]['length_m']} m" + (f" along {gaps[0]['street']}" if gaps[0].get("street") else "")
                           if gaps else None),
                    E.attr("How pieces are joined", "lines whose ends come within 25 m of another line")],
        relations=rels, sources=[source(st, "cycleways"), _osm_source(st)],
        _for_model={"pieces": pieces, "gaps": [{k: g[k] for k in ("between", "length_m", "street")} for g in gaps[:5]],
                    "note": INVENTORY_NOTE}))


def build_bus_lane(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    hit = st.by_id(eid)
    if hit is None:
        return None
    _, b = hit
    length = A.length_of(b["lines"])
    center = _line_center(b["lines"])
    active = b["status"] == "active"
    rels = road_rels(ents, b["lines"][0], max_m=40) + zone_rel(st, center[0], center[1])
    return _inventory(E.new_entity(
        b["id"], "bus_lane", b["name"], subtitle=f"Bus lane, {length / 1000:.1f} km",
        geometry={"type": "MultiLineString", "coordinates": b["lines"]}, center=center,
        state=_state(b["status"], "In use" if active else "Planned", BUS_LANE_COLOR, "ok" if active else "muted"),
        readings=[{"key": "length_km", "label": "Length", "value": round(length / 1000, 2), "unit": "km"}],
        attributes=[E.attr("In Greek", b.get("name_el")), E.attr("Status", "In use" if active else "Planned (future)")],
        relations=rels, sources=[source(st, "bus_lanes")], _for_model={"note": INVENTORY_NOTE}))


# -- schools ---------------------------------------------------------------------------------------------


def build_school(ents, eid: str, near) -> dict | None:
    st = _streets(ents)
    s = st.school(eid)
    if s is None:
        return None
    rep = A.crossing_report(st, s["lat"], s["lon"], A.SCHOOL_WALK_M)
    rels = [{"relation": "crossing", "label": "Crossing", "id": x["id"], "kind": "pedestrian_crossing",
             "name": x["name"], "distance_m": x["distance_m"]} for x in rep["crossings"][:3]]
    rels += [{"relation": "signal", "label": "Signals", "id": x["id"], "kind": "traffic_signal",
              "name": x["name"], "distance_m": x["distance_m"]} for x in rep["signals"][:2]]
    rels += zone_rel(st, s["lon"], s["lat"]) + E.nearest_stop(ents, s["lat"], s["lon"], 400)
    covered = A.covered(st, s["lat"], s["lon"])
    verdict = rep["verdict"] if covered else "quiet"
    return E.new_entity(
        s["id"], "school", s["name"], subtitle="Kindergarten" if s["kind"] == "kindergarten" else "School",
        geometry=E.point_geom(s["lon"], s["lat"]), center=[s["lon"], s["lat"]],
        state=_state(verdict, A.VERDICTS[verdict] if covered else "Outside the national inventories",
                     A.VERDICT_COLORS[verdict], "warn" if verdict == "gap" else "ok" if verdict == "served" else "muted"),
        readings=[r for r in (
            {"key": "crossing_m", "label": "Nearest crossing", "value": rep["nearest_crossing_m"], "unit": "m"} if rep["nearest_crossing_m"] is not None else None,
            {"key": "signal_m", "label": "Nearest signals", "value": rep["nearest_signal_m"], "unit": "m"} if rep["nearest_signal_m"] is not None else None,
            {"key": "kmh", "label": "Main road limit", "value": rep["main_road_limit_kmh"], "unit": "km/h"} if rep["main_road_limit_kmh"] else None) if r],
        attributes=[E.attr("Main road beside it", f"{rep['main_road']}, {rep['main_road_m']} m away" if rep["main_road"] else "none within 60 m"),
                    E.attr("Zone", rep["zone"]),
                    E.attr("Distances", "in a straight line, not a walk"),
                    E.attr("Local name", s.get("name_local") if s.get("name_local") != s["name"] else None)],
        relations=rels,
        sources=[{"source": s["source"], "name": "OpenStreetMap" if s["source"] == "openstreetmap" else "Overture Maps",
                  "detail": "where the school is"},
                 source(st, "crossings"), source(st, "signals")],
        crossing=rep, _for_model={"verdict": verdict, **{k: rep[k] for k in ("nearest_crossing_m", "nearest_signal_m",
                                                                              "main_road", "main_road_limit_kmh", "zone")}})


# -- at a moment of the city clock: inventories stand at every moment ------------------------------------------


def inventory_at(ents, entity: dict, at: datetime) -> dict:
    from chatbot.moments import label
    e = dict(entity)
    e["moment"] = {"at": at.isoformat(timespec="minutes"), "label": label(at), "known": True, "applies": False,
                   "note": "An inventory: it stands at every moment of the clock"}
    return e


# -- search --------------------------------------------------------------------------------------------


def search_items(ents) -> list[dict]:
    st = getattr(ents, "streets", None)
    if st is None:
        return []
    cache = st.__dict__.setdefault("_search", {})
    key = st.feeds.version
    if not st.started and st.feeds.peek("signals") is None:
        return []  # search never fetches: the inventories come with the server's start
    if cache.get("key") == key:
        return cache["items"]
    items = []
    try:
        for z in st.records("zones"):
            items.append(E.index_item(z["id"], "slow_zone", f"{z['kind']}, {z['name']}", "Street rules",
                                      f"{z['status'].capitalize()} {z['kmh']} km/h zone", None, None,
                                      [z["name"], z.get("name_el"), z["kind"], "zone 30 slow zone"]))
        for c in st.cameras():
            items.append(E.index_item(c["id"], "enforcement_camera", c["name"], "Street rules",
                                      f"Enforcement camera {c.get('code') or ''}".strip(), c["lat"], c["lon"],
                                      [c["name"], c.get("name_el"), c.get("code"), "speed camera enforcement camera"]))
        for b in st.records("bus_lanes"):
            items.append(E.index_item(b["id"], "bus_lane", b["name"], "Street rules",
                                      "Bus lane, " + ("in use" if b["status"] == "active" else "planned"), None, None,
                                      [b["name"], b.get("name_el"), "bus lane"]))
        for c in st.records("cycleways"):
            items.append(E.index_item(c["id"], "cycleway", c["name"], "Street rules", "Cycleway", None, None,
                                      [c["name"], c.get("name_el"), "cycle lane cycleway bike"]))
        for s in st.records("signals"):
            items.append(E.index_item(s["id"], "traffic_signal", s["junction"] or "Traffic signals", "Street rules",
                                      "Traffic signals", s["lat"], s["lon"], [s["junction"], *s["streets"], "traffic lights signals"]))
        for x in st.records("crossings"):
            items.append(E.index_item(x["id"], "pedestrian_crossing", x["name"], "Street rules", "Pedestrian crossing",
                                      x["lat"], x["lon"], [x["name"], "crossing zebra"]))
    except Exception:  # noqa: BLE001 - search never waits for a feed
        return []
    cache.update(key=key, items=items)
    return items


KINDS = (("speed_limit", ("limit-", "limitway-"), build_limit), ("slow_zone", ("zone-", "zentry-"), build_zone),
         ("traffic_signal", ("signal-",), build_signal), ("pedestrian_crossing", ("xing-",), build_crossing),
         ("enforcement_camera", ("ecam-",), build_camera), ("cycleway", ("cycle-",), build_cycleway),
         ("bus_lane", ("buslane-",), build_bus_lane), ("school", ("school-",), build_school))
for _name, _prefixes, _build in KINDS:
    E.register_kind(E.Kind(_name, lambda i, p=_prefixes: i.lower().startswith(p), _build, ttl_s=300))
    E.register_at(_name, inventory_at)
E.register_at("slow_zone_entrance", inventory_at)
E.register_search(search_items, {"Street rules": 5})

__all__ = ["search_items", "source", "day", "SOURCE_IDS", "BUS_LANE_COLOR", "CYCLE_COLOR"]

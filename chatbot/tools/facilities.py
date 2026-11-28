"""Park, charge and hire: car parks (with the city's bay sensors at them), park and ride, EV
chargers, Nextbike stations (with their live bikes), Bolt scooter zones and bicycle racks near a
place. They read chatbot/mobility/facilities.py, which keeps one copy of each national inventory
for every page and answer."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from chatbot.geo import haversine_m
from chatbot.mobility.facilities import COLORS, CONNECTORS, FEEDS, LABELS, Facility
from chatbot.mobility.facility_kinds import (BAY_M, bays, bike_words, charger_line, day, facilities_of,
                                             osm_car_park)
from chatbot.mobility.gtfs import TZ
from chatbot.mobility.roads import parse_moment
from chatbot.store import STATE_COLORS, point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, my_location, obj, resolve_point

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

WALK_M_PER_MIN = 75  # an easy pace, and streets are never straight: straight line x 1.3
CENTRE = (35.1725, 33.3650, "the old town")  # Eleftheria Square, when no place is given
ON_STREET_M = 250
PLACE_COLOR = "#e4ebf4"


def _walk_min(d: float) -> int:
    return max(1, round(d * 1.3 / WALK_M_PER_MIN))


def _fac(ctx: ToolContext):
    fac = facilities_of(ctx.entities) if ctx.entities is not None else None
    if fac is None and ctx.mobility is not None:
        fac = getattr(ctx.mobility, "facilities", None)
    if fac is None:
        raise ToolError("the national inventories aren't available in this deployment")
    return fac


def _where(ctx: ToolContext, a: dict, required: bool) -> tuple[float, float, str] | None:
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if pt is not None:
        return pt
    if ctx.user_location:
        loc = my_location(ctx)
        return loc["lat"], loc["lon"], loc["name"]
    if required:
        return CENTRE
    return None


def _inventory_line(fac, kinds: list[str]) -> list[dict]:
    out = []
    for k in kinds:
        inv = fac.cached(k)
        if inv is not None:
            out.append({"what": LABELS[k], "last_changed": day(inv.updated), "republished": day(inv.published),
                        **({"note": inv.error} if inv.error else {})})
    return out


def _items(fac, kinds: list[str]) -> list[Facility]:
    out = []
    failed = []
    for k in kinds:
        try:
            out += fac.inventory(k).items
        except Exception as e:  # noqa: BLE001 - one feed failing leaves the others, said below
            log.warning("%s inventory unavailable: %s", k, e)
            failed.append(LABELS[k])
    if failed and not out:
        raise ToolError(f"the national inventory of {', '.join(failed).lower()} couldn't be read just now. "
                        "Say so plainly; don't guess")
    return out


def _place_feature(lat: float, lon: float, name: str) -> dict:
    return point(lon, lat, {"id": "origin", "label": name, "state": "you are here", "color": PLACE_COLOR,
                            "popup": name})


def _feature(f: Facility, d: float | None, props: dict, popup: str) -> dict:
    return point(f.lon, f.lat, {"id": f.id, "label": f.name, "state": LABELS[f.kind].lower(),
                                "color": COLORS[f.kind], "entity_kind": f.kind, "facility_kind": f.kind,
                                **({"distance_m": round(d)} if d is not None else {}), **props,
                                "popup": popup})


def _mark(ctx: ToolContext, ref: str, kinds: list[str]) -> None:
    # The page draws these with the inventories' own icons (lib/facilities).
    ctx.store.layers[ref]["meta"]["facilities"] = {"kinds": kinds}


# -- parking ---------------------------------------------------------------------------------------


@tool("find_parking",
      "Car parks and park and ride sites near a place from the national inventory (Public "
      "Works Department), with whether there is space where the city can tell: the Smart "
      "Nicosia bay sensors at or beside a car park (within 60 m) and on the streets around the "
      "place. Call it for 'where can I park near X', 'is there space at the Faneromeni car "
      "park', 'car parks in the old town', 'park and ride for the centre'. The inventory gives "
      "no capacity or price: a car park's free count is its sensed bays only, and whether it "
      "is paid comes from OpenStreetMap where mapped. Returns car parks nearest first (id, "
      "name, distance_m, walk_min, sensed_bays, free_bays, paid), the free on-street bays "
      "around the place, and a layer_ref (show it with 'points': each car park with its "
      "parking icon, the sensed bays free in green and taken in red) and a data_ref linked to "
      "the map by id. Car park ids (carpark-31, pnr-0) open in the inspector. Pass at for a "
      "past moment the twin recorded (the bays then).",
      obj({**NEAR,
           "park_and_ride": {"type": "boolean", "description": "Also list park and ride sites (default: only when one is within radius_m)."},
           "with_space": {"type": "boolean", "description": "Only car parks whose sensed bays have one free."},
           "at": {"type": "string", "description": "A past local moment, ISO (2026-11-26T08:30): the bays as the twin's memory recorded them."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Car parks to list (default 8)."}}))
def h_find_parking(ctx: ToolContext, a: dict) -> dict:
    fac = _fac(ctx)
    try:
        at = parse_moment(a.get("at"), ctx.city.now())
    except ValueError as e:
        raise ToolError(str(e)) from None
    lat, lon, place = _where(ctx, a, required=True)
    radius = float(a.get("radius_m") or 600)
    items = _items(fac, ["car_park", "park_and_ride"])
    near = fac.near(lat, lon, radius, ("car_park",), limit=200, items=items)
    pnr = fac.near(lat, lon, 30_000 if a.get("park_and_ride") else radius, ("park_and_ride",), limit=4, items=items)
    ents = ctx.entities
    rows, feats, listed = [], [_place_feature(lat, lon, place)], []
    for d, f in near + pnr:
        b = bays(ents, f, at, fetch=True)
        osm = osm_car_park(ents, f) if f.kind == "car_park" else None
        fee = (osm or {}).get("fee")
        name = f.name if f.named or not (osm and osm.get("name")) else osm["name"]
        free = b["free"] if b and b["known"] else None
        if a.get("with_space") and not free:
            continue
        row = {"id": f.id, "name": name, "kind": LABELS[f.kind], "distance_m": round(d), "walk_min": _walk_min(d),
               "sensed_bays": b["bays"] if b else 0, "free_bays": free,
               "taken_bays": b["taken"] if b and b["known"] else None,
               "paid": {"yes": True, "no": False}.get(fee or ""), "named": f.named or bool(osm and osm.get("name"))}
        rows.append(row)
        label = f"{free} free" if free is not None else ""
        popup = f"{name}\n{LABELS[f.kind]}, {round(d)} m" + (f"\n{free} of {b['free'] + b['taken']} sensed bays free" if free is not None else
                                                               "\nNo live occupancy" if not b else "\nBay sensors, no recent reading")
        feats.append(_feature(f, d, {"label": name if not label else f"{name}: {label}", "free_bays": free}, popup))
        if b:
            for r in b["rows"]:
                listed.append(r["id"])
                feats.append(point(r["lon"], r["lat"], {"id": r["id"], "label": "", "state": r["state"].replace("_", " "),
                                                        "color": r["color"], "entity_kind": "parking_sensor",
                                                        "popup": f"Parking bay {r['id']}\n{ {'free': 'Free', 'occupied': 'Taken'}.get(r['state'], 'No recent reading') }"}))
    car_parks = [r for r in rows if r["kind"] == LABELS["car_park"]]
    limit = a.get("limit") or 8
    # The streets around the place: free bays the city's sensors see, outside any car park.
    on_street = _on_street(ctx, lat, lon, at, set(listed))
    for r in on_street.pop("_rows"):
        feats.append(point(r["lon"], r["lat"], {"id": r["id"], "label": "", "state": r["state"], "color": r["color"],
                                                "entity_kind": "parking_sensor",
                                                "popup": f"Parking bay {r['id']}\n{'Free' if r['state'] == 'free' else 'Taken'}, on the street"}))
    when = at.astimezone(TZ).strftime("%a %H:%M") if at else "now"
    title = f"Parking near {place}" + (f", {when}" if at else "")
    legend = {"car park": COLORS["car_park"], "bay free": STATE_COLORS["free"], "bay taken": STATE_COLORS["occupied"]}
    ref = ctx.store.add_layer("parking_near", feats, geometry="point", title=title, legend=legend,
                              value_fields=["free_bays"], count=len(rows))
    _mark(ctx, ref, ["car_park", "park_and_ride"])
    data = ctx.store.add_dataset("parking_near", rows, title=title, link={"key": "id"})
    with_live = [r for r in car_parks if r["free_bays"] is not None]
    res = {
        "place": place, "radius_m": round(radius), "as_of": when, "car_parks_within": len(car_parks),
        "car_parks": car_parks[:limit],
        "park_and_ride": [r for r in rows if r["kind"] == LABELS["park_and_ride"]],
        "with_live_occupancy": len(with_live), "on_street": on_street,
        "notes": "No car park in the national inventory gives its spaces or price. free_bays counts only the city's "
                 f"bay sensors within {BAY_M} m of a car park (null: no sensor there); paid is OpenStreetMap's. "
                 "Unnamed car parks are mapped on OpenStreetMap without a name.",
        "inventory": _inventory_line(fac, ["car_park", "park_and_ride"]),
        "layer_ref": ref, "data_ref": data,
    }
    if not car_parks:
        res["none_within"] = f"no car park in the inventory within {round(radius)} m of {place}; try a larger radius_m"
    res["_summary"] = f"{len(car_parks)} car parks, {len(with_live)} with live bays"
    return res


def _on_street(ctx: ToolContext, lat: float, lon: float, at: datetime | None, skip: set[str]) -> dict:
    """The city's bay sensors within ON_STREET_M of the place that aren't at a car park."""
    city = ctx.city
    out: dict = {"radius_m": ON_STREET_M, "free": 0, "taken": 0, "no_reading": 0, "_rows": []}
    try:
        if at is None:
            # The strip's last snapshot when it is recent; the city's API only when there is none.
            live = list(city.cached_states("Parking").values()) or city.states("Parking")
            states = {s.device.name: (s.interp.state, s.interp.stale, s.device) for s in live}
        else:
            moments = ctx.moments or getattr(ctx.entities, "moments", None)
            if moments is None:
                raise LookupError("the twin's memory is off")
            snap = moments.snapshot("parking_sensor", at)
            states = {d.device.name: (st, st == "unknown", d.device) for d, st in zip(snap.devices, snap.states)}
    except Exception as e:  # noqa: BLE001 - the car parks still answer
        out["note"] = f"the city's parking sensors couldn't be read ({str(e)[:120]})"
        return out
    found = []
    for name, (st, stale, dev) in states.items():
        if name in skip or dev.lat is None or abs(dev.lat - lat) > 0.003 or abs(dev.lon - lon) > 0.004:
            continue
        d = haversine_m(lat, lon, dev.lat, dev.lon)
        if d > ON_STREET_M:
            continue
        word = st if st in ("free", "occupied") and not stale else "no_reading"
        out[{"free": "free", "occupied": "taken"}.get(word, "no_reading")] += 1
        if word != "no_reading":
            found.append((d, {"id": name, "lon": dev.lon, "lat": dev.lat, "state": word, "color": STATE_COLORS[word]}))
    found.sort(key=lambda t: t[0])
    out["_rows"] = [r for _, r in found]
    out["nearest_free"] = [{"id": r["id"], "distance_m": round(d)} for d, r in found if r["state"] == "free"][:3]
    return out


# -- chargers --------------------------------------------------------------------------------------

CONNECTOR_KEYS = {"type2": ["type2"], "ccs": ["comboType2", "comboType1"], "chademo": ["chademo"]}


@tool("find_chargers",
      "Electric vehicle charging points from the national inventory (Department of Electrical "
      "and Mechanical Services): power per connector in kW, fast (43 kW or more) or standard, "
      "AC or DC, connector types, operator, hours and access, nearest first. Call it for "
      "'nearest fast charger to the old town', 'where can I charge my car near X', 'CCS "
      "chargers in Strovolos', 'how many chargers does Nicosia have'. The inventory's status "
      "(listed in service or out of service) is republished nightly: it is not whether a "
      "connector is free now, and nothing says that. Without a place it lists Greater "
      "Nicosia's. Returns chargers (id, name, distance_m, kw, fast, current, connectors, "
      "operator, hours, listed_status), counts, a layer_ref (show it with 'points': each "
      "charger with its bolt icon) and a data_ref linked to the map by id, for a sorted bar of "
      "power or distance. Charger ids (charger-1a2b3c4d) open in the inspector.",
      obj({**NEAR,
           "fast_only": {"type": "boolean", "description": "Only fast chargers (43 kW or more)."},
           "connector": {"type": "string", "enum": ["type2", "ccs", "chademo"]},
           "include_out_of_service": {"type": "boolean", "description": "Also list chargers the inventory lists as out of service (default false)."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Chargers to list (default 8)."}}))
def h_find_chargers(ctx: ToolContext, a: dict) -> dict:
    fac = _fac(ctx)
    items = _items(fac, ["charger"])
    where = _where(ctx, a, required=False)
    radius = float(a.get("radius_m") or 3000)
    pool = [f for f in items if f.in_nicosia] if where is None else items
    out_of_service = 0
    picked: list[tuple[float | None, Facility]] = []
    for f in pool:
        i = f.info
        if a.get("fast_only") and i.get("speed") != "fast":
            continue
        if a.get("connector") and not set(CONNECTOR_KEYS[a["connector"]]) & set(i.get("connector_types") or []):
            continue
        if i.get("status") == "unavailable" and not a.get("include_out_of_service"):
            out_of_service += 1
            continue
        d = haversine_m(where[0], where[1], f.lat, f.lon) if where else None
        if d is not None and d > radius:
            continue
        picked.append((d, f))
    picked.sort(key=lambda t: (t[0] if t[0] is not None else 0, -(t[1].info.get("kw") or 0)))
    feats = [_place_feature(where[0], where[1], where[2])] if where else []
    rows = []
    for d, f in picked:
        i = f.info
        row = {"id": f.id, "name": f.name, "distance_m": round(d) if d is not None else None,
               "kw": i.get("kw"), "fast": i.get("speed") == "fast", "current": (i.get("power_type") or "").upper() or None,
               "connectors": ", ".join(CONNECTORS.get(t, t) for t in i.get("connector_types") or []),
               "operator": i.get("operator"), "hours": i.get("hours"), "listed_status": i.get("status"),
               "address": f.address}
        rows.append(row)
        feats.append(_feature(f, d, {"kw": i.get("kw"), "fast": row["fast"], "label": f"{f.name}: {charger_line(f)}"},
                              f"{f.name}\n{charger_line(f)}" + ("\nListed as out of service" if i.get("status") == "unavailable" else "")))
    what = "Fast chargers" if a.get("fast_only") else "EV chargers"
    title = f"{what} near {where[2]}" if where else f"{what} in Greater Nicosia"
    ref = ctx.store.add_layer("chargers", feats, geometry="point", title=title,
                              legend={"EV charger": COLORS["charger"]}, value_fields=["kw"], count=len(rows))
    _mark(ctx, ref, ["charger"])
    data = ctx.store.add_dataset("chargers", rows, title=title, link={"key": "id"})
    limit = a.get("limit") or 8
    res = {"where": where[2] if where else "Greater Nicosia", "found": len(rows),
           "fast": sum(1 for r in rows if r["fast"]), "chargers": rows[:limit],
           "left_out_listed_out_of_service": out_of_service,
           "by_operator": dict(Counter(r["operator"] for r in rows if r["operator"]).most_common(6)),
           "notes": "listed_status is the inventory's, republished nightly; it isn't live availability. "
                    "kw is per connector. fast means 43 kW or more.",
           "inventory": _inventory_line(fac, ["charger"]), "layer_ref": ref, "data_ref": data}
    if not rows:
        res["none_found"] = (f"no charger matches within {round(radius)} m of {where[2]}; try a larger radius_m"
                             if where else "no charger in Greater Nicosia matches")
    res["_summary"] = f"{len(rows)} chargers, {res['fast']} fast"
    return res


# -- bikes, scooters and racks ------------------------------------------------------------------------

WANTS = {"hire_bike": ["bike_station"], "e_bike": ["bike_station"], "return_bike": ["bike_station"],
         "scooter": ["scooter_zone"], "rack": ["bike_rack"],
         "any": ["bike_station", "scooter_zone", "bike_rack"]}


@tool("find_bikes",
      "Nextbike stations with the bikes standing there now (live from Nextbike's own feed: "
      "bikes, how many are electric, free docks), Bolt scooter parking zones (with how many "
      "scooters each holds; where the scooters are isn't published) and public bicycle racks "
      "for your own bike, near a place, nearest first. Call it for 'where can I pick up a bike "
      "near the university', 'is there an e-bike near me', 'where can I return a Nextbike', "
      "'where do I park a Bolt scooter', 'a bike rack near X'. want: hire_bike (stations with "
      "a bike now), e_bike (with an electric one), return_bike (with a free dock, or a "
      "dockless spot), scooter, rack, or any. To get somewhere without a car, combine it: a "
      "station near the start and one near the end, then plan_route with mode 'bike' between "
      "them. Returns rows (id, name, kind, distance_m, walk_min, bikes, ebikes, docks), a "
      "layer_ref (show it with 'points': each kind with its own icon) and a data_ref linked to "
      "the map by id. Ids (nextbike-6495339, bolt-dac81462, rack-5) open in the inspector.",
      obj({**NEAR,
           "want": {"type": "string", "enum": list(WANTS), "description": "What for (default hire_bike)."},
           "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Places to list (default 8)."}}))
def h_find_bikes(ctx: ToolContext, a: dict) -> dict:
    fac = _fac(ctx)
    want = a.get("want") or "hire_bike"
    kinds = WANTS[want]
    lat, lon, place = _where(ctx, a, required=True)
    radius = float(a.get("radius_m") or 600)
    items = _items(fac, kinds)
    near = fac.near(lat, lon, radius, tuple(kinds), limit=300, items=items)
    live = None
    if "bike_station" in kinds:
        try:
            live = fac.bikes()
        except Exception as e:  # noqa: BLE001
            log.info("Nextbike live feed failed: %s", e)
    rows, feats = [], [_place_feature(lat, lon, place)]
    skipped = Counter()
    for d, f in near:
        row = {"id": f.id, "name": f.name, "kind": LABELS[f.kind], "distance_m": round(d), "walk_min": _walk_min(d)}
        popup = f"{f.name}\n{LABELS[f.kind]}, {round(d)} m"
        props: dict = {}
        if f.kind == "bike_station":
            st = live.at(f.info.get("station_id", "")) if live else None
            info = fac.station_info(f.info.get("station_id", "")) or {}
            virtual = bool(info.get("virtual"))
            if st is not None:
                row |= {"bikes": st["bikes"], "ebikes": st.get("ebikes") or 0,
                        "docks": None if virtual else st.get("docks"), "renting": st.get("renting", True),
                        "dockless": virtual}
                popup += f"\n{bike_words(st).capitalize()}"
                props = {"bikes": st["bikes"], "label": f"{f.name}: {st['bikes']}"}
            else:
                row |= {"bikes": None, "live": "unknown"}
            if live is not None and live.status != "unavailable":
                if want == "hire_bike" and not (row.get("bikes") and row.get("renting", True)):
                    skipped["no bike now"] += 1
                    continue
                if want == "e_bike" and not row.get("ebikes"):
                    skipped["no e-bike now"] += 1
                    continue
                if want == "return_bike" and not (virtual or (row.get("docks") or 0) > 0):
                    skipped["no free dock"] += 1
                    continue
        elif f.kind == "scooter_zone":
            row["holds"] = f.capacity
            popup += f"\nHolds {f.capacity} scooters" if f.capacity else ""
        elif f.kind == "bike_rack":
            row["address"] = f.address
        rows.append(row)
        feats.append(_feature(f, d, props, popup))
    title = {"hire_bike": "Bikes to hire", "e_bike": "E-bikes to hire", "return_bike": "Where to return a bike",
             "scooter": "Bolt scooter zones", "rack": "Bicycle racks", "any": "Bikes, scooters and racks"}[want] + f" near {place}"
    ref = ctx.store.add_layer("bikes_near", feats, geometry="point", title=title,
                              legend={LABELS[k]: COLORS[k] for k in kinds}, value_fields=["bikes"], count=len(rows))
    _mark(ctx, ref, kinds)
    data = ctx.store.add_dataset("bikes_near", rows, title=title, link={"key": "id"})
    limit = a.get("limit") or 8
    res = {"place": place, "radius_m": round(radius), "want": want, "found": len(rows), "places": rows[:limit],
           "inventory": _inventory_line(fac, kinds), "layer_ref": ref, "data_ref": data}
    if skipped:
        res["left_out"] = dict(skipped)
    if "bike_station" in kinds:
        if live is None or live.status == "unavailable":
            res["live_note"] = ("Nextbike's live feed isn't answering, so stations are listed without their bikes: "
                                "say so" + (f" ({live.error})" if live and live.error else ""))
        else:
            res["live_as_of"] = datetime.fromtimestamp(live.feed_time or live.fetched_at, TZ).strftime("%H:%M")
            if live.status == "stale":
                res["live_note"] = "the live feed failed just now: these counts are a few minutes old"
    if not rows:
        res["none_within"] = f"nothing within {round(radius)} m of {place}; try a larger radius_m"
    res["_summary"] = f"{len(rows)} {'places' if want == 'any' else LABELS[kinds[0]].lower() + 's'} near {place}"
    return res


__all__ = ["TOOLS", "FEEDS"]

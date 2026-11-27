"""Park, charge and hire for the page: each inventory as GeoJSON (cached by the browser, it
changes once a day at most), what is live on them now or at a moment of the city clock (the bays
the city's sensors see beside each car park, the bikes at each Nextbike station), and a summary
for the Layers sheet and the Parking card. Every page shares one copy of each feed."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from chatbot.mobility.facilities import COLORS, KINDS, LABELS, PLURALS, Facility
from chatbot.mobility.facility_kinds import bays, bay_devices, charger_line, facilities_of, inventory_note
from chatbot.mobility.roads import parse_moment
from chatbot.overview import cached
from chatbot.routes import agent_of

router = APIRouter()


def _fac(request: Request):
    fac = facilities_of(agent_of(request).entities)
    if fac is None:
        raise HTTPException(503, "the national inventories aren't available in this deployment")
    return fac


def _props(f: Facility) -> dict:
    p = {"id": f.id, "name": f.name, "kind": f.kind, "color": COLORS[f.kind], "entity_kind": f.kind,
         "named": f.named}
    if f.capacity is not None:
        p["capacity"] = f.capacity
    if f.kind == "charger":
        i = f.info
        p |= {"kw": i.get("kw"), "fast": i.get("speed") == "fast", "listed": i.get("status") or "unknown",
              "detail": charger_line(f)}
    elif f.kind == "bike_station":
        p["station_id"] = f.info.get("station_id")
    return p


def feature_collection(fac, kind: str, ents=None) -> dict:
    inv = fac.inventory(kind)
    feats = []
    sensed = 0
    for f in inv.items:
        props = _props(f)
        if kind == "car_park" and ents is not None and bay_devices(ents, f):
            props["sensed"] = True
            sensed += 1
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [f.lon, f.lat]},
                      "properties": props})
        for ring in f.rings:
            feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]},
                          "properties": {**props, "part": "area"}})
    meta = {**inv.public(), **inventory_note(fac, kind), "label": LABELS[kind], "plural": PLURALS[kind],
            "color": COLORS[kind]}
    if kind == "car_park":
        meta["sensed"] = sensed
    elif kind == "charger":
        here = [f for f in inv.items if f.in_nicosia]
        meta["fast_in_nicosia"] = sum(1 for f in here if f.info.get("speed") == "fast")
        meta["listed_out_of_service_in_nicosia"] = sum(1 for f in here if f.info.get("status") == "unavailable")
    elif kind in ("bike_station", "scooter_zone"):
        meta["capacity_in_nicosia"] = sum(f.capacity or 0 for f in inv.items if f.in_nicosia)
    return {"type": "FeatureCollection", "features": feats, "meta": meta}


@router.get("/api/facilities/live")
def live(request: Request, at: str | None = Query(None, max_length=40),
         kinds: str = Query("car_park,bike_station", max_length=80)) -> JSONResponse:
    """What is live on the inventories: the sensed bays beside each car park (now from the last
    snapshot, or at `at` from the twin's memory) and the bikes at each Nextbike station (now only)."""
    agent = agent_of(request)
    fac = _fac(request)
    try:
        moment = parse_moment(at, agent.city.now())
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    want = {k for k in kinds.split(",") if k in KINDS}
    body: dict = {"at": moment.isoformat() if moment else None}
    if "car_park" in want:
        def build():
            out, newest, errors = {}, None, set()
            for f in fac.inventory("car_park").items:
                b = bays(agent.entities, f, moment)
                if b is None:
                    continue
                out[f.id] = {k: b[k] for k in ("bays", "free", "taken", "no_reading", "known")}
                if b["as_of"] and (newest is None or b["as_of"] > newest):
                    newest = b["as_of"]
                if b.get("error"):
                    errors.add(b["error"])
            return {"car_parks": out, "as_of": newest.isoformat() if newest else None,
                    "origin": "memory" if moment else "live", "error": next(iter(errors), None)}
        key = f"facility_bays:{int(moment.timestamp()) // 900 if moment else 'now'}"
        body["bays"] = cached(key, 600 if moment else 60, build) or {
            "car_parks": {}, "error": "the car park inventory couldn't be read just now"}
    if "bike_station" in want:
        if moment is not None:
            body["bikes"] = {"status": "past", "stations": {},
                             "note": "Live bikes are known now only: the twin doesn't keep them"}
        else:
            b = fac.bikes()
            try:
                stations = fac.inventory("bike_station").items
            except Exception:  # noqa: BLE001
                stations = []
            by_station = {f.info.get("station_id"): f.id for f in stations}
            here = [b.stations[f.info.get("station_id")] for f in stations
                    if f.in_nicosia and f.info.get("station_id") in b.stations]
            body["bikes"] = {"status": b.status, "error": b.error,
                             "in_nicosia": {"stations": len(here), "bikes": sum(s["bikes"] for s in here),
                                            "ebikes": sum(s.get("ebikes") or 0 for s in here)},
                             "as_of": datetime.fromtimestamp(b.feed_time or b.fetched_at, timezone.utc).isoformat(),
                             "stations": {by_station[sid]: {k: v for k, v in st.items() if k in ("bikes", "ebikes", "docks", "renting")}
                                          for sid, st in b.stations.items() if sid in by_station}}
    return JSONResponse(body, headers={"Cache-Control": "max-age=30" if moment is None else "max-age=600"})


@router.get("/api/facilities")
def summary(request: Request) -> JSONResponse:
    """Each inventory as last read (counts, how old it is), without reading anything new."""
    fac = _fac(request)
    out = {}
    for k in KINDS:
        inv = fac.cached(k)
        out[k] = ({**inv.public(), **inventory_note(fac, k), "label": LABELS[k], "plural": PLURALS[k],
                   "color": COLORS[k]} if inv else {"label": LABELS[k], "plural": PLURALS[k], "color": COLORS[k],
                                                    "count": None})
    b = fac.last_bikes()
    return JSONResponse({"kinds": out, "bikes": {"status": b.status, "error": b.error} if b else None},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/facilities/{kind}")
def one_kind(kind: str, request: Request) -> JSONResponse:
    """One inventory as GeoJSON: a point per facility, and the polygon of each park and ride site
    and scooter zone (`part: area`)."""
    if kind not in KINDS:
        raise HTTPException(404, f"no inventory {kind!r}; there are {', '.join(KINDS)}")
    fac = _fac(request)
    agent = agent_of(request)
    try:
        body = feature_collection(fac, kind, agent.entities)
    except Exception as e:  # noqa: BLE001 - the feed is down and nothing is on disk
        raise HTTPException(502, f"the national inventory of {PLURALS[kind]} couldn't be read: {str(e)[:160]}") from None
    return JSONResponse(body, headers={"Cache-Control": "max-age=600"})


__all__ = ["router", "feature_collection"]

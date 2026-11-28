"""A spot: any point of the city, opened in the inspector for "What's it like here?".

Its id says where it is: spot-35.17000-33.36060 (latitude, longitude, five decimals, about a
metre). It reads the environment twin there at the clock's moment: sun or shade (from the DLS
3D buildings), the hours of direct sun that day, the air and the noise estimated from the
stations and the model with their uncertainty, the temperature and the wind.
"""

from __future__ import annotations

import re
from datetime import datetime

from chatbot import entities as E
from chatbot.settings import NICOSIA_BBOX

SPOT_ID = re.compile(r"^spot-(\d{1,2}\.\d{1,6})-(\d{1,2}\.\d{1,6})$", re.IGNORECASE)
SPOT_TTL_S = 60
E.KIND_LABELS["spot"] = "Spot"

TONE = {"sun": "live", "shade": "ok", "night": "muted", "unknown": "muted"}
COLOR = {"sun": "#ffae45", "shade": "#7aa2d6", "night": "#5b6b85", "unknown": "#a3a3a3"}
LABEL = {"sun": "In the sun", "shade": "In shade", "night": "The sun is down",
         "unknown": "Sun or shade unknown"}


def spot_id(lat: float, lon: float) -> str:
    return f"spot-{lat:.5f}-{lon:.5f}"


def parse(eid: str) -> tuple[float, float] | None:
    m = SPOT_ID.match(eid.strip())
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    w, s, e, n = NICOSIA_BBOX
    pad = 0.15  # Greater Nicosia and a little around it
    if not (s - pad <= lat <= n + pad and w - pad <= lon <= e + pad):
        return None
    return lat, lon


def _environment(ents):
    return getattr(ents, "environment", None)


def _num(v: float | None, unit: str, sigma: float | None = None) -> str:
    if v is None:
        return "unknown"
    text = f"{v:g}" if isinstance(v, float) else str(v)
    if sigma:
        text += f" ± {sigma:g}"
    return f"{text} {unit}".strip()


def build_spot(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    where = parse(eid)
    env = _environment(ents)
    if where is None or env is None:
        return None
    lat, lon = where
    data = env.spot(lat, lon, at)
    sun = data["sun"]
    if sun.get("shade") is None and sun.get("up"):
        state = "unknown"
    elif not sun.get("up"):
        state = "night"
    else:
        state = "shade" if sun["shade"] else "sun"
    places = ents.places_within(lat, lon, 150, limit=1)
    name = f"Near {places[0]['name']}" if places else f"{lat:.5f}, {lon:.5f}"
    # The inspector's own section reads out the sun, the air and the weather; the attributes
    # only add what it doesn't.
    attributes = [E.attr("On a roof", "yes, the readout is for the roof" if sun.get("on_roof") else None)]
    relations = []
    nearest = next((a["nearest"] for a in data["air"] if a.get("nearest")), None)
    if nearest:
        station = next((s for s in ents.city.devices_of("smartsense") if s.name == nearest["id"]), None) \
            if ents.city is not None else None
        relations.append({"relation": "device", "label": "Nearest air station", "id": nearest["id"],
                          "kind": "air_station", "name": nearest["name"], "distance_m": nearest["metres"],
                          **({"lon": station.lon, "lat": station.lat} if station else {})})
    relations += places
    entity = E.new_entity(
        spot_id(lat, lon), "spot", name,
        subtitle="Sun, shade, air and weather here",
        geometry=E.point_geom(lon, lat), center=[lon, lat],
        state={"value": state, "label": LABEL[state], "color": COLOR[state], "tone": TONE[state],
               "stale": False, "reason": None, "local": None, "as_of": data["at"], "age_s": None},
        # The readout is the inspector's own section (it reads `environment` whole); the model
        # gets it through _for_model, so the generic readings stay empty.
        readings=[], attributes=attributes, relations=relations,
        sources=[{"name": "DLS 3D buildings", "detail": "the shade cast by every building around, on flat ground",
                  "url": E.DLS_3D_URL},
                 {"name": "Smart Nicosia air stations", "detail": "the measured air and noise", "url": E.SMART_NICOSIA},
                 {"name": "Open-Meteo", "detail": "the CAMS air model, the temperature and the wind",
                  "url": "https://open-meteo.com/"}],
        as_of=data["at"],
    )
    entity["environment"] = data  # the inspector's own section reads it whole
    entity["_for_model"] = {"environment": _for_model(data)}
    fab = getattr(ents, "fabric", None)
    if fab is not None and fab.ready:
        sf = fab.spot_fabric(lat, lon)
        entity["fabric"] = sf
        entity["_for_model"]["fabric"] = sf
        if sf.get("hex_id"):
            entity["relations"].append({
                "relation": "fabric_hex",
                "label": "Enclosing H3 hexagon",
                "id": sf["hex_id"],
                "kind": "fabric_hex",
                "name": sf.get("hex_name") or sf["hex_id"],
                "detail": f"{sf.get('population_2022', 0):,} modelled residents (2022)",
                "lon": lon,
                "lat": lat,
            })
        entity["sources"] = entity["sources"] + [
            {"source": "landsat_lst", "name": "USGS Landsat 8/9", "detail": "summer morning ground surface temp (~10:30 local)", "url": "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2"},
            {"source": "kontur_population", "name": "Kontur population (2022)", "detail": "H3 res-8 modelled population (CC BY 4.0)", "url": "https://data.humdata.org/dataset/kontur-population-cyprus"},
            {"source": "cynap_gradients", "name": "CyNAP road gradients", "detail": "100 m road slopes (CC BY 4.0)", "url": "https://traffic4cyprus.org.cy/dataset/road_network_gradients"},
        ]
    return entity


def _for_model(data: dict) -> dict:
    sun = data["sun"]
    out = {"sun_up": sun.get("up"), "in_shade": sun.get("shade"),
           "sun_hours_that_day": sun.get("hours_today"), "sun_altitude_deg": sun.get("altitude_deg")}
    for a in data["air"]:
        out[a["metric"]] = (None if a.get("value") is None else
                            {"value": a["value"], "plus_minus": a.get("uncertainty"), "band": a.get("band"),
                             "from": a.get("from")})
    t = data.get("temperature")
    if t:
        out["temperature_c"] = {"value": t.get("value"), "plus_minus": t.get("uncertainty")}
    out["wind"] = data.get("wind")
    return out


def spot_at(ents, entity: dict, at: datetime) -> dict:
    """The spot at a moment of the city clock: everything read again for then."""
    from chatbot.moments import label

    e = build_spot(ents, entity["id"], None, at) or entity
    e["moment"] = {"at": at.isoformat(timespec="minutes"), "label": label(at), "known": True,
                   "applies": True}
    return e


def spot_details(ents, entity: dict, at: datetime | None) -> dict:
    """The spot as the assistant reads it (get_entity): its record and "What's here?", the
    sections the page shows under it (chatbot/here.py), at the clock's moment."""
    from chatbot import here

    where = parse(entity["id"])
    if where is None:
        return entity
    secs = here.sections(ents, where[0], where[1], at)
    e = dict(entity)
    e["relations"] = entity["relations"] + here.relations(secs)
    e["_for_model"] = {**(entity.get("_for_model") or {}), "here": here.for_model(secs)}
    names = {x["name"] for x in entity["sources"]}
    e["sources"] = entity["sources"] + [x for s in secs for x in s["sources"]
                                        if x["name"] not in names and not names.add(x["name"])]
    # For the layer get_entity draws: the environment's readout and what the sections read.
    e["_source_ids"] = list(dict.fromkeys(["dls", "smart_nicosia", "open_meteo", *here.source_ids(secs)]))
    return e


E.register_kind(E.Kind("spot", lambda i: i.lower().startswith("spot-"), build_spot, ttl_s=SPOT_TTL_S),
                first=True)
E.register_at("spot", spot_at)
E.register_details("spot", spot_details)

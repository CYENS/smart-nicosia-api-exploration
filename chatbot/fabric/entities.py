"""Entity kind ``fabric_hex`` (hex-<h3_id>) and Ctrl+K search registration."""

from __future__ import annotations

import re

from chatbot import entities as E
from chatbot.fabric.gradients import DATASET_URL as CYNAP_URL
from chatbot.fabric.scoring import ACCESS_COLORS, HEAT_EXPOSURE_COLORS, access_band
from chatbot.geo import haversine_m

HEX_ID = re.compile(r"^hex-[0-9a-f]{10,16}$", re.IGNORECASE)
E.KIND_LABELS["fabric_hex"] = "City hexagon"

KONTUR_URL = "https://data.humdata.org/dataset/kontur-population-cyprus"
LANDSAT_URL = "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2"
OVERTURE_ATTR = "https://docs.overturemaps.org/attribution/"

TONES = {"low": "ok", "moderate": "muted", "high": "warn", "very_high": "alarm"}


def _fabric(ents: E.Entities):
    return getattr(ents, "fabric", None)


def build_fabric_hex(ents: E.Entities, eid: str, near) -> dict | None:
    fab = _fabric(ents)
    if fab is None:
        return None
    h = fab.hex_by_id(eid)
    if h is None:
        return None

    lon, lat = h["center"]
    band = h.get("heat_band") or "low"
    band_label = h.get("heat_band_label") or "Low"
    color = HEAT_EXPOSURE_COLORS.get(band, HEAT_EXPOSURE_COLORS["low"])
    lst_c = h.get("lst_median_c")
    pop = h.get("population") or 0
    acc = h.get("access_15min_score")
    sf = h.get("access_step_free_score")
    slopes = h.get("slopes") or {}
    infra = h.get("infra") or {}

    readings = [
        {"key": "heat_exposure_score", "label": "Heat exposure score", "value": h.get("heat_exposure_score") or 0, "unit": "/ 100"},
        {"key": "lst_median_c", "label": "Morning ground surface temp (~10:30)", "value": lst_c if lst_c is not None else "unknown", "unit": "°C" if lst_c is not None else ""},
        {"key": "population", "label": "Modelled 2022 population", "value": pop, "unit": "people"},
        {"key": "access_15min_score", "label": "15-min walk access", "value": acc if acc is not None else "unknown", "unit": "/ 100" if acc is not None else ""},
        {"key": "access_step_free_score", "label": "Step-free 15-min access", "value": sf if sf is not None else "unknown", "unit": "/ 100" if sf is not None else ""},
    ]

    attributes = [
        E.attr("H3 index", h.get("h3")),
        E.attr("Area", f"{h.get('area_km2', 0.7):.2f} km²"),
        E.attr("Population density (2022)", f"{h.get('pop_density_km2', 0):,} / km²"),
        E.attr("Morning ground heat range", f"{h.get('lst_min_c')} to {h.get('lst_max_c')} °C (~10:30 local)" if h.get("lst_min_c") is not None else None),
        E.attr("Green land-use share", f"{h.get('green_pct', 0)}%"),
        E.attr("CyNAP road slopes measured", f"{slopes.get('points', 0)} points ({slopes.get('gt5', 0)} over 5%, {slopes.get('gt8', 0)} over 8%)" if slopes.get("points") else "No CyNAP road gradient points inside"),
        E.attr("Crossings / benches / recycling", f"{infra.get('crossings', 0)} / {infra.get('benches', 0)} / {infra.get('recycling', 0)}"),
        E.attr("Caveat", "Population is Kontur modelled 2022; surface temperature is Landsat ~10:30 morning ground skin temperature, not air temperature."),
    ]

    relations = ents.places_within(lat, lon, 600, limit=3)
    if getattr(ents, "mobility", None) is not None and (tt := ents.mobility.timetable()):
        for dist, stop in tt.near(lat, lon, 600, limit=2):
            relations.append({
                "relation": "bus_stop",
                "label": "Bus stop inside or nearby",
                "id": f"stop-{stop.id}",
                "kind": "bus_stop",
                "name": stop.name,
                "distance_m": round(dist),
                "lon": stop.lon,
                "lat": stop.lat,
            })

    # Nearest steep (>5%) slope point if present
    steep_pts = [
        (haversine_m(lat, lon, p["lat"], p["lon"]), p)
        for p in fab.gradients.points()
        if p["slope_pct"] > 5.0 and abs(p["lat"] - lat) < 0.008 and abs(p["lon"] - lon) < 0.009
    ]
    steep_pts.sort(key=lambda t: t[0])
    if steep_pts and steep_pts[0][0] <= 650:
        d_s, sp = steep_pts[0]
        relations.append({
            "relation": "slope",
            "label": f"Steepest measured grade ({sp['slope_pct']}%)",
            "name": f"{sp.get('road_name') or sp.get('road_code')} ({sp['slope_pct']}%)",
            "distance_m": round(d_s),
            "lon": sp["lon"],
            "lat": sp["lat"],
            "color": "#ff5a4e" if sp["slope_pct"] > 8.0 else "#f5a524",
        })

    sources = [
        {"source": "kontur_population", "name": "Kontur population (2022)", "detail": "H3 res-8 modelled population (CC BY 4.0)", "url": KONTUR_URL},
        {"source": "landsat_lst", "name": "USGS Landsat 8/9", "detail": "summer morning ground surface temp (~10:30 local)", "url": LANDSAT_URL},
        {"source": "overture_base", "name": "Overture Maps", "detail": "base infrastructure and green land use", "url": OVERTURE_ATTR},
        {"source": "cynap_gradients", "name": "CyNAP road gradients", "detail": "100 m road slopes (CC BY 4.0)", "url": CYNAP_URL},
    ]

    entity = E.new_entity(
        h["id"],
        "fabric_hex",
        h["name"],
        subtitle=f"H3 res-8 hexagon ({h.get('area_km2', 0.7):.2f} km²)",
        geometry={"type": "Polygon", "coordinates": [h["ring"]]},
        center=[lon, lat],
        state={
            "value": band,
            "label": f"{band_label} heat exposure",
            "color": color,
            "tone": TONES.get(band, "muted"),
            "stale": False,
            "reason": None,
            "local": None,
            "as_of": None,
            "age_s": None,
        },
        readings=readings,
        attributes=attributes,
        relations=relations,
        sources=sources,
    )
    entity["fabric"] = {
        "heat_exposure_score": h.get("heat_exposure_score"),
        "heat_band_label": band_label,
        "heat_parts": h.get("heat_parts") or {},
        "lst_median_c": lst_c,
        "lst_min_c": h.get("lst_min_c"),
        "lst_max_c": h.get("lst_max_c"),
        "population_2022": pop,
        "pop_density_km2": h.get("pop_density_km2"),
        "green_pct": h.get("green_pct"),
        "access_15min_score": acc,
        "access_step_free_score": sf,
        "step_free_gap": h.get("step_free_gap"),
        "access_color": ACCESS_COLORS[access_band(acc)],
        "step_free_color": ACCESS_COLORS[access_band(sf)],
        "nearest_services": h.get("nearest_services") or {},
        "slopes": slopes,
        "infra": infra,
    }
    entity["_for_model"] = {
        "fabric": entity["fabric"],
        "note": "Modelled 2022 Kontur population; Landsat morning ground surface temperature (~10:30 local), not air temperature.",
    }
    return entity


def search_hexes(ents: E.Entities) -> list[dict]:
    fab = _fabric(ents)
    if fab is None or not fab.ready:
        return []
    cached = getattr(fab, "_search_cache", None)
    if cached is not None:
        return cached
    items = []
    for h in sorted(fab.hexagons(), key=lambda x: -(x.get("population") or 0)):
        if (h.get("population") or 0) < 20 and (h.get("heat_exposure_score") or 0) < 20:
            continue
        lon, lat = h["center"]
        detail = f"{h['population']:,} people (2022) · {h.get('lst_median_c', '?')} °C ground (~10:30) · {h.get('access_15min_score', '?')}/100 walk"
        items.append(
            E.index_item(
                h["id"],
                "fabric_hex",
                h["name"],
                "City hexagons",
                detail,
                lat,
                lon,
                [h["name"], h["id"], h.get("h3"), "hexagon", "heat", "population", "walkability"],
            )
        )
    fab._search_cache = items
    return items


E.register_kind(E.Kind("fabric_hex", lambda i: bool(HEX_ID.match(i)), build_fabric_hex))
E.register_search(search_hexes, {"City hexagons": 5})

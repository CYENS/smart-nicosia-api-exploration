"""Tools for Living Sky (aircraft, ISS visible passes) and CyNAP ITS infrastructure (PWD CCTV
cameras and urban loop detectors)."""

from __future__ import annotations

from datetime import datetime, timezone

from chatbot.geo import haversine_m
from chatbot.moments import OutOfRange, parse_at
from chatbot.sky.aircraft import COVERAGE_NOTE
from chatbot.sky.entities import AC_COLOR, AC_GROUND_COLOR
from chatbot.sky.its import CCTV_COLOR, DETECTOR_CYCLE_COLOR, DETECTOR_VEH_COLOR
from chatbot.store import point
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, obj, resolve_point
from chatbot.web import WebError

TOOLS = ToolSet()
tool = TOOLS.tool


def _sky(ctx: ToolContext):
    sky = getattr(ctx, "sky", None)
    if sky is None:
        raise ToolError("the sky and ITS service isn't available on this server")
    return sky


@tool("get_sky",
      "What is flying over Nicosia right now (or at a past moment of the city clock, `at`), "
      "and when the International Space Station (ISS) next passes visibly overhead. Aircraft "
      "positions come from adsb.lol (40 nautical miles around Nicosia) and are recorded for "
      "24 hours so the city clock can replay them. Note: community receiver coverage around "
      "Cyprus is partial, especially at low altitude and on quiet evenings. Returns live or "
      "recorded aircraft, ISS current position and next visible twilight passes over Nicosia, "
      "a layer_ref of aircraft (show with style 'points') and a data_ref for tables or charts.",
      obj({
          "what": {"type": "string", "enum": ["all", "aircraft", "iss"],
                   "description": "What to read: 'all' (default), 'aircraft' only, or 'iss' passes only."},
          "at": {"type": "string",
                 "description": "Past local ISO moment (e.g. 2026-11-27T19:00) for recorded aircraft and ISS."},
          "days": {"type": "integer", "minimum": 1, "maximum": 7,
                   "description": "Days ahead to look for visible ISS passes (default 4)."},
      }))
def h_get_sky(ctx: ToolContext, a: dict) -> dict:
    sky = _sky(ctx)
    what = a.get("what") or "all"
    at = None
    if a.get("at"):
        try:
            at = parse_at(a["at"], ctx.city.now())
        except OutOfRange as e:
            raise ToolError(str(e)) from None

    res: dict = {"what": what, "at": at.isoformat() if at else "now"}
    feats: list[dict] = []
    rows: list[dict] = []

    if what in ("all", "aircraft"):
        ac_snap = sky.aircraft.at(at) if at is not None else sky.aircraft.snapshot()
        aircraft = ac_snap["aircraft"]
        for item in aircraft:
            color = AC_GROUND_COLOR if item.get("on_ground") else AC_COLOR
            state = "on_ground" if item.get("on_ground") else "airborne"
            alt_text = "ground" if item.get("on_ground") else (
                f"{item['altitude_ft']:,} ft" if item.get("altitude_ft") is not None else "?"
            )
            spd_text = f"{round(item['speed_kmh'])} km/h" if item.get("speed_kmh") is not None else "?"
            feats.append(point(item["lon"], item["lat"], {
                "id": item["id"],
                "label": item["callsign"],
                "state": state,
                "color": color,
                "entity_kind": "aircraft",
                "heading": item.get("track_deg"),
                "altitude_ft": item.get("altitude_ft"),
                "speed_kmh": item.get("speed_kmh"),
                "on_ground": item.get("on_ground"),
                "popup": f"{item['callsign']} ({item.get('type') or 'Aircraft'})\n{alt_text}, {spd_text}",
            }))
            rows.append({
                "id": item["id"],
                "callsign": item["callsign"],
                "type": item.get("type"),
                "registration": item.get("registration"),
                "altitude_ft": item.get("altitude_ft"),
                "speed_kmh": item.get("speed_kmh"),
                "heading": item.get("track_deg"),
                "on_ground": item.get("on_ground"),
                "distance_km": item.get("distance_km"),
            })
        res["aircraft"] = {
            "status": ac_snap["status"],
            "count": len(aircraft),
            "airborne": sum(1 for x in aircraft if not x.get("on_ground")),
            "on_ground": sum(1 for x in aircraft if x.get("on_ground")),
            "coverage_note": COVERAGE_NOTE,
            "items": [{k: x[k] for k in ("id", "callsign", "type", "registration", "altitude_ft",
                                         "speed_kmh", "track_deg", "on_ground", "distance_km")
                       if x.get(k) is not None} for x in aircraft[:15]],
        }
        if ac_snap.get("error"):
            res["aircraft"]["note"] = ac_snap["error"]

    if what in ("all", "iss"):
        days = int(a.get("days") or 4)
        iss = sky.iss.summary(at=at, look_ahead_days=days)
        res["iss"] = {
            "status": iss["status"],
            "tle_epoch": iss.get("tle_epoch"),
            "position": iss.get("position"),
            "next_pass": iss.get("next_pass"),
            "upcoming_passes": iss.get("upcoming") or [],
        }
        if iss.get("error"):
            res["iss"]["note"] = iss["error"]
        if what == "iss" and not rows and iss.get("upcoming"):
            rows = [{
                "start": p["start"],
                "peak": p["peak"],
                "duration_min": p["duration_min"],
                "max_elevation_deg": p["max_elevation_deg"],
                "direction": f"{p['start_dir']} to {p['end_dir']}",
            } for p in iss["upcoming"]]

    if feats:
        title = "Recorded aircraft around Nicosia" if at is not None else "Aircraft around Nicosia"
        legend = {"airborne": AC_COLOR, "on_ground": AC_GROUND_COLOR}
        layer_ref = ctx.store.add_layer(
            "sky_aircraft", feats, geometry="point", title=title, legend=legend,
            value_fields=["altitude_ft", "speed_kmh"],
        )
        ctx.store.layers[layer_ref]["meta"]["sky"] = {
            "kind": "aircraft", "at": at.isoformat() if at else None,
        }
        res["layer_ref"] = layer_ref

    if rows:
        data_title = "Aircraft around Nicosia" if what != "iss" else "Visible ISS passes over Nicosia"
        link = {"key": "id"} if any("id" in r for r in rows) else None
        res["data_ref"] = ctx.store.add_dataset("sky", rows, title=data_title, link=link)

    ac_n = (res.get("aircraft") or {}).get("count", 0)
    nxt = (res.get("iss") or {}).get("next_pass")
    if what == "iss":
        res["_summary"] = (
            f"ISS next pass {nxt['start'][11:16]} ({nxt['max_elevation_deg']}°)"
            if nxt else "no visible ISS pass soon"
        )
    else:
        res["_summary"] = f"{ac_n} aircraft" + (f", ISS pass {nxt['start'][11:16]}" if nxt else "")
    return res


@tool("get_its_infrastructure",
      "The Public Works Department (PWD) road-monitoring infrastructure across Greater Nicosia "
      "from the Cyprus National Access Point: 13 CCTV camera sites and 39 urban loop and "
      "cycle traffic detectors, plus whether the Swarco MeasuredDataPublication feed is "
      "currently publishing live flow counts. These are static site locations (no camera "
      "video is published, and Swarco currently publishes 92 site references without measured "
      "values). Filter by kind ('cctv', 'detectors', or 'all'), detector_mode ('vehicles' or "
      "'cycle'), or near a place. Returns sites, counts, Swarco status, a layer_ref (show with "
      "style 'points') and a linked data_ref.",
      obj({
          "kind": {"type": "string", "enum": ["all", "cctv", "detectors"]},
          "detector_mode": {"type": "string", "enum": ["all", "vehicles", "cycle"]},
          **NEAR,
      }))
def h_get_its_infrastructure(ctx: ToolContext, a: dict) -> dict:
    sky = _sky(ctx)
    kind = a.get("kind") or "all"
    mode = a.get("detector_mode") or "all"
    try:
        fc = sky.its.geojson(kind=kind, detector_mode=mode)
    except WebError as e:
        raise ToolError(f"couldn't read the CyNAP ITS inventory: {e}") from None

    feats = fc["features"]
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    if pt is not None:
        lat, lon, place_name = pt
        radius = float(a.get("radius_m") or 1000)
        kept = []
        for f in feats:
            flon, flat = f["geometry"]["coordinates"]
            dist = haversine_m(lat, lon, flat, flon)
            if dist <= radius:
                props = {**f["properties"], "distance_m": round(dist)}
                kept.append({**f, "properties": props})
        kept.sort(key=lambda f: f["properties"]["distance_m"])
        feats = kept
        where = f" within {round(radius)} m of {place_name}"
    else:
        where = " in Nicosia"

    legend = {}
    for f in feats:
        st = f["properties"]["state"]
        legend[st] = f["properties"]["color"]

    rows = [{
        "id": f["properties"]["id"],
        "name": f["properties"]["label"],
        "kind": "CCTV" if f["properties"]["kind"] == "its_cctv" else "Traffic detector",
        "subtype": f["properties"].get("subtype"),
        "direction": f["properties"].get("direction"),
        **({"distance_m": f["properties"]["distance_m"]} if "distance_m" in f["properties"] else {}),
        "lat": f["geometry"]["coordinates"][1],
        "lon": f["geometry"]["coordinates"][0],
    } for f in feats]

    title = f"PWD traffic detectors and CCTV{where}"
    layer_ref = ctx.store.add_layer(
        "its_sites", feats, geometry="point", title=title, legend=legend,
        value_fields=["distance_m"] if pt is not None else [],
    )
    data_ref = ctx.store.add_dataset("its_sites", rows, title=title, link={"key": "id"})

    sw = fc["meta"]["swarco"]
    sw_note = (
        f"Swarco MeasuredDataPublication reports {sw['measured_count']} live measured values"
        if sw.get("has_values")
        else f"Swarco MeasuredDataPublication returns {sw.get('site_refs', 92)} site references with no measured flow values yet"
    )
    cctv_n = sum(1 for r in rows if r["kind"] == "CCTV")
    det_n = sum(1 for r in rows if r["kind"] == "Traffic detector")
    return {
        "counts": {
            "total": len(rows),
            "cctv": cctv_n,
            "detectors": det_n,
            "vehicle_detectors": sum(1 for r in rows if r["kind"] == "Traffic detector" and (r.get("subtype") or "").lower() != "cycle"),
            "cycle_detectors": sum(1 for r in rows if r["kind"] == "Traffic detector" and (r.get("subtype") or "").lower() == "cycle"),
        },
        "swarco": {**sw, "note": sw_note},
        "note": "Static PWD site locations (CC BY 4.0). Live CCTV video is not published.",
        "sites": rows[:20],
        "layer_ref": layer_ref,
        "data_ref": data_ref,
        "_summary": f"{cctv_n} CCTV, {det_n} detectors",
    }

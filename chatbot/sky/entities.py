"""Entity kinds for Living Sky & CyNAP ITS: `aircraft` (ac-<hex>), `its_cctv` (cctv-<id>),
and `its_detector` (det-<id>)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from chatbot import entities as E
from chatbot.settings import CACHE_DIR, TIMEZONE
from chatbot.sky.aircraft import COVERAGE_NOTE
from chatbot.sky.its import CCTV_COLOR, DETECTOR_CYCLE_COLOR, DETECTOR_VEH_COLOR, LICENCE

TZ = ZoneInfo(TIMEZONE)

E.KIND_LABELS.update({
    "aircraft": "Aircraft",
    "its_cctv": "PWD CCTV camera",
    "its_detector": "PWD traffic detector",
})

AC_COLOR = "#38bdf8"
AC_GROUND_COLOR = "#8798b0"
CCTV_DATASET_URL = "https://traffic4cyprus.org.cy/dataset/cctv"
DET_DATASET_URL = "https://traffic4cyprus.org.cy/dataset/traffic_detectors"
SWARCO_DATASET_URL = "https://traffic4cyprus.org.cy/dataset/trafficsensors"


def _sky(ents):
    return getattr(ents, "sky", None)


def _now(ents) -> datetime:
    s = _sky(ents)
    if s is not None:
        return s.now()
    return ents.now() if hasattr(ents, "now") else datetime.now(timezone.utc)


def _iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, timezone.utc)
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def _state(value: str, label: str, color: str, tone: str, as_of=None, now=None, reason=None) -> dict:
    age = (now - as_of).total_seconds() if isinstance(as_of, datetime) and now else None
    return {
        "value": value,
        "label": label,
        "color": color,
        "tone": tone,
        "stale": False,
        "reason": reason,
        "local": None,
        "as_of": _iso(as_of),
        "age_s": round(age) if age is not None else None,
    }


def _aircraft_entity(ac: dict, now: datetime, *, recorded: bool = False) -> dict:
    as_of_dt = datetime.fromtimestamp(ac["reported"], timezone.utc)
    if ac.get("on_ground"):
        st = _state("on_ground", "On the ground", AC_GROUND_COLOR, "muted", as_of_dt, now)
    else:
        vr = ac.get("vertical_rate_fpm") or 0
        if vr >= 300:
            st = _state("climbing", "Climbing", AC_COLOR, "live", as_of_dt, now)
        elif vr <= -300:
            st = _state("descending", "Descending", AC_COLOR, "live", as_of_dt, now)
        else:
            st = _state("airborne", "Airborne", AC_COLOR, "live", as_of_dt, now)

    readings = []
    if ac.get("altitude_ft") is not None:
        readings.append({
            "key": "altitude_ft", "label": "Altitude", "value": ac["altitude_ft"],
            "unit": "ft", "as_of": _iso(as_of_dt), "stale": False,
        })
    if ac.get("speed_kmh") is not None:
        readings.append({
            "key": "speed_kmh", "label": "Ground speed", "value": round(ac["speed_kmh"]),
            "unit": "km/h", "as_of": _iso(as_of_dt), "stale": False,
        })
    if ac.get("vertical_rate_fpm") is not None and not ac.get("on_ground"):
        readings.append({
            "key": "vertical_rate_fpm", "label": "Vertical rate", "value": ac["vertical_rate_fpm"],
            "unit": "ft/min", "as_of": _iso(as_of_dt), "stale": False,
        })
    if ac.get("track_deg") is not None:
        readings.append({
            "key": "track_deg", "label": "Track", "value": round(ac["track_deg"]),
            "unit": "°", "as_of": _iso(as_of_dt), "stale": False,
        })

    subtitle = ", ".join(x for x in (ac.get("type"), ac.get("registration")) if x) or f"ICAO {ac['hex'].upper()}"
    attrs = [
        E.attr("Callsign", ac.get("callsign")),
        E.attr("Registration", ac.get("registration")),
        E.attr("Aircraft type", ac.get("type")),
        E.attr("Altitude (m)", f"{ac['altitude_m']:,} m" if ac.get("altitude_m") is not None else None),
        E.attr("Ground speed (kt)", f"{round(ac['ground_speed_kt'])} kt" if ac.get("ground_speed_kt") is not None else None),
        E.attr("Squawk", ac.get("squawk")),
        E.attr("Category", ac.get("category")),
        E.attr("Distance from centre", f"{ac['distance_km']} km" if ac.get("distance_km") is not None else None),
        E.attr("ICAO hex", ac["hex"].upper()),
        E.attr("Coverage", COVERAGE_NOTE),
    ]
    sources = [{"source": "adsb_lol", "name": "adsb.lol ADS-B", "detail": "community aircraft receivers, ODbL",
                "url": "https://adsb.lol", "as_of": _iso(as_of_dt)}]
    if recorded:
        sources.insert(0, {"source": "twin_memory", "name": "The twin's memory",
                           "detail": "recorded aircraft positions (24 h)", "as_of": _iso(as_of_dt)})
    return E.new_entity(
        ac["id"], "aircraft", ac["callsign"],
        subtitle=subtitle,
        geometry=E.point_geom(ac["lon"], ac["lat"]),
        center=[ac["lon"], ac["lat"]],
        state=st,
        readings=readings,
        attributes=attrs,
        relations=[],
        sources=sources,
        as_of=_iso(as_of_dt),
        aircraft={
            "hex": ac["hex"],
            "callsign": ac["callsign"],
            "type": ac.get("type"),
            "registration": ac.get("registration"),
            "altitude_ft": ac.get("altitude_ft"),
            "speed_kmh": ac.get("speed_kmh"),
            "track_deg": ac.get("track_deg"),
            "on_ground": ac.get("on_ground"),
            "coverage_note": COVERAGE_NOTE,
        },
    )


def build_aircraft(ents, eid: str, near) -> dict | None:
    sky = _sky(ents)
    if sky is None:
        return None
    ac = sky.aircraft.aircraft(eid)
    if ac is not None:
        return _aircraft_entity(ac, _now(ents))
    archived = sky.aircraft.latest_recorded(eid)
    if archived is not None:
        e = _aircraft_entity(archived, _now(ents), recorded=True)
        e["state"] = _state(
            "out_of_range", "Out of live range", AC_GROUND_COLOR, "muted",
            datetime.fromtimestamp(archived["reported"], timezone.utc), _now(ents),
            reason="last seen earlier in the twin's memory",
        )
        return e
    raise E.EntityNotFound(f"no aircraft {eid} in range right now (aircraft leave once out of receiver range)")


def aircraft_at(ents, entity: dict, at: datetime) -> dict:
    from chatbot.moments import label

    sky = _sky(ents)
    ac = sky.aircraft.aircraft(entity["id"], at=at) if sky is not None else None
    if ac is not None:
        out = _aircraft_entity(ac, at, recorded=True)
        out["moment"] = {"at": _iso(at), "label": label(at), "known": True, "applies": True}
        return out
    out = dict(entity)
    out["moment"] = {"at": _iso(at), "label": label(at), "known": False, "applies": True}
    out["readings"] = []
    out["state"] = _state("unknown", "No record at that moment", AC_GROUND_COLOR, "muted", reason="no recorded position")
    out["sources"] = [
        {"source": "twin_memory", "name": "The twin's memory", "detail": "no record of this aircraft then"},
        *entity.get("sources", []),
    ]
    return out


def build_cctv(ents, eid: str, near) -> dict | None:
    sky = _sky(ents)
    if sky is None:
        return None
    site = sky.its.site(eid)
    if site is None or site["kind"] != "its_cctv":
        return None
    relations = ents.nearest_devices(site["lat"], site["lon"], max_m=500)
    relations += ents.places_within(site["lat"], site["lon"], 150, limit=2)
    return E.new_entity(
        site["id"], "its_cctv", site["name"],
        subtitle=f"PWD CCTV camera, site {site['site_id']}",
        geometry=E.point_geom(site["lon"], site["lat"]),
        center=[site["lon"], site["lat"]],
        state=_state("inventory", f"{site.get('subtype') or 'CCTV'} camera site", CCTV_COLOR, "ok"),
        attributes=[
            E.attr("Equipment", site.get("equipment_type") or "CCTV"),
            E.attr("Camera type", site.get("subtype") or "PTZ"),
            E.attr("Operator", "Public Works Department (PWD)"),
            E.attr("Site id", site["site_id"]),
            E.attr("Live feed", "Site inventory only; live video is not published openly"),
        ],
        relations=relations,
        sources=[{"source": "cynap_its", "name": "Cyprus National Access Point (PWD)",
                  "detail": LICENCE, "url": CCTV_DATASET_URL,
                  "as_of": site.get("publication_time")}],
        as_of=site.get("publication_time"),
        its={"site_id": site["site_id"], "kind": "cctv", "subtype": site.get("subtype"),
             "live_values": False},
    )


def build_detector(ents, eid: str, near) -> dict | None:
    sky = _sky(ents)
    if sky is None:
        return None
    site = sky.its.site(eid)
    if site is None or site["kind"] != "its_detector":
        return None
    sub = site.get("subtype") or "Vehicles"
    is_cycle = sub.lower() == "cycle"
    color = DETECTOR_CYCLE_COLOR if is_cycle else DETECTOR_VEH_COLOR
    sw = sky.its.swarco_health()
    if sw and sw.get("has_values"):
        meas_note = f"Swarco MeasuredDataPublication reports live values ({sw.get('measured_count')} values)"
    else:
        refs = (sw or {}).get("site_refs") or 92
        meas_note = f"Site inventory only; Swarco feed publishes {refs} site references with no flow counts yet"
    relations = ents.nearest_devices(site["lat"], site["lon"], max_m=500)
    relations += ents.places_within(site["lat"], site["lon"], 150, limit=2)
    return E.new_entity(
        site["id"], "its_detector", site["name"],
        subtitle=f"PWD {sub.lower()} detector, site {site['site_id']}",
        geometry=E.point_geom(site["lon"], site["lat"]),
        center=[site["lon"], site["lat"]],
        state=_state("inventory", f"{sub} detector site", color, "ok"),
        attributes=[
            E.attr("Equipment", site.get("equipment_type") or "Traffic Detector"),
            E.attr("Mode", sub),
            E.attr("Direction", site.get("direction")),
            E.attr("Operator", "Public Works Department (PWD)"),
            E.attr("Site id", site["site_id"]),
            E.attr("Flow counts", meas_note),
        ],
        relations=relations,
        sources=[
            {"source": "cynap_its", "name": "Cyprus National Access Point (PWD)",
             "detail": LICENCE, "url": DET_DATASET_URL, "as_of": site.get("publication_time")},
            {"source": "swarco_traffic", "name": "Swarco MeasuredDataPublication",
             "detail": meas_note, "url": SWARCO_DATASET_URL},
        ],
        as_of=site.get("publication_time"),
        its={"site_id": site["site_id"], "kind": "detector", "subtype": sub,
             "direction": site.get("direction"), "live_values": bool(sw and sw.get("has_values"))},
    )


def search_sky(ents) -> list[dict]:
    """Cached aircraft (when active) and cached ITS sites for Ctrl K search. Never fetches live."""
    sky = _sky(ents)
    if sky is None:
        return []
    items: list[dict] = []
    last_ac = sky.aircraft._last
    if last_ac:
        for a in last_ac.get("aircraft") or []:
            detail = " · ".join(x for x in (
                a.get("type"),
                f"{a['altitude_ft']:,} ft" if a.get("altitude_ft") is not None else None,
                f"{round(a['speed_kmh'])} km/h" if a.get("speed_kmh") is not None else None,
            ) if x)
            items.append(E.index_item(
                a["id"], "aircraft", a["callsign"], "Aircraft", detail or "Live ADS-B",
                a["lat"], a["lon"], [a["callsign"], a.get("registration"), a.get("type"), a["hex"], "aircraft flight plane"],
            ))
    inv = sky.its._sites[1] if sky.its._sites else None
    if inv is None:
        cdir = sky.its.cache_dir or CACHE_DIR
        if (cdir / "its_cctv.xml").exists() and (cdir / "its_detectors.xml").exists():
            try:
                inv = sky.its.inventories()
            except Exception:  # noqa: BLE001
                inv = None
    if inv:
        for s in inv.get("cctv") or []:
            items.append(E.index_item(
                s["id"], "its_cctv", s["name"], "PWD CCTV cameras",
                f"PWD {s.get('subtype') or 'CCTV'} camera", s["lat"], s["lon"],
                [s["name"], s["site_id"], s.get("subtype"), "cctv camera pwd"],
            ))
        for s in inv.get("detectors") or []:
            sub = s.get("subtype") or "Vehicles"
            items.append(E.index_item(
                s["id"], "its_detector", s["name"], "PWD traffic detectors",
                f"PWD {sub.lower()} detector" + (f" ({s['direction']})" if s.get("direction") else ""),
                s["lat"], s["lon"],
                [s["name"], s["site_id"], sub, s.get("direction"), "detector loop traffic pwd"],
            ))
    return items


E.register_kind(E.Kind("aircraft", lambda i: i.lower().startswith("ac-"), build_aircraft, ttl_s=15))
E.register_kind(E.Kind("its_cctv", lambda i: i.lower().startswith("cctv-"), build_cctv, ttl_s=600))
E.register_kind(E.Kind("its_detector", lambda i: i.lower().startswith("det-"), build_detector, ttl_s=600))
E.register_at("aircraft", aircraft_at)
E.register_search(search_sky, {"Aircraft": 4, "PWD CCTV cameras": 4, "PWD traffic detectors": 4})

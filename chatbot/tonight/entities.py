"""Entity definitions for Tonight in Nicosia: `duty_pharmacy` and `city_event`.

Ids:
- `duty-<YYYY-MM-DD>-<reg_no>` (e.g. `duty-2026-11-27-252`) or `duty-<reg_no>`
- `event-<id>` (e.g. `event-794231`)

Both register with chatbot/entities.py (kinds, clock resolvers, Ctrl K search).
PRIVACY CRITICAL: Never expose any personal home phone field in attributes, subtitle, or search.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from chatbot import entities as E
from chatbot.settings import TIMEZONE
from chatbot.tonight.events import EVENTS_URL, split_events_for_moment
from chatbot.tonight.pharmacies import LEYKOSIA_PAGE, duty_date_for

TZ = ZoneInfo(TIMEZONE)

DUTY_ID = re.compile(r"^duty-(?:\d{4}-\d{2}-\d{2}-)?[a-z0-9]+$", re.IGNORECASE)
EVENT_ID = re.compile(r"^event-[a-z0-9]+$", re.IGNORECASE)

E.KIND_LABELS.update({
    "duty_pharmacy": "Pharmacy on duty",
    "city_event": "City event",
})

DUTY_COLOR = "#10b981"
EVENT_COLOR = "#ec4899"


def _tonight(ents):
    return getattr(ents, "tonight", None)


def build_duty_pharmacy(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    t = _tonight(ents)
    if t is None:
        return None
    p = t.get_pharmacy(eid, allow_network=(at is None))
    if p is None:
        return None
    now = at or t.now()
    active_date = duty_date_for(now).isoformat()
    on_duty = p["duty_date"] == active_date

    lat, lon = p.get("lat"), p.get("lon")
    center = [lon, lat] if lat is not None and lon is not None else None
    geometry = E.point_geom(lon, lat)
    state = E.state_block(
        "duty_pharmacy",
        "on_duty" if on_duty else "off_duty",
        reason=f"duty shift {p['duty_date']} (until 08:00 next morning)",
    )
    if state:
        state["label"] = "On duty tonight" if on_duty else f"On duty {p['duty_date']}"
        state["color"] = DUTY_COLOR if on_duty else "#8798b0"
        state["tone"] = "live" if on_duty else "muted"

    precision_text = {
        "exact": f"At the pharmacy ({p.get('located_by')})",
        "street": f"On the street ({p.get('located_by')})",
        "area": f"Approximate municipality centre ({p.get('located_by')})",
        "unplaced": "Address not placed on the map",
    }.get(p.get("precision") or "unplaced", p.get("located_by"))

    attributes = [
        E.attr("Pharmacist", p.get("pharmacist")),
        E.attr("Registration", p.get("reg_no")),
        E.attr("Duty shift", f"{p['duty_date']} (through 08:00 next morning)"),
        E.attr("Address", p.get("address")),
        E.attr("Municipality", p.get("municipality")),
        E.attr("Directions", p.get("directions")),
        E.attr("Pharmacy phone", p.get("pharmacy_tel")),
        E.attr("On the map", precision_text),
    ]

    relations: list[dict] = []
    if p.get("osm_id") and ents.places is not None and ents.places.by_id(p["osm_id"]):
        osm = ents.places.by_id(p["osm_id"])
        relations.append({
            "relation": "place",
            "label": "OpenStreetMap pharmacy",
            "id": osm["id"],
            "kind": "place",
            "name": osm.get("name") or p["name"],
        })
    if lat is not None and lon is not None:
        relations += E.nearest_stop(ents, lat, lon)
        b = ents.building_at(lat, lon)
        if b and p.get("precision") == "exact":
            relations.append({
                "relation": "building",
                "label": "In the DLS building",
                "id": f"B{b['id']}",
                "kind": "building_3d",
                "name": b["properties"].get("t") or "Building",
                "detail": f"{b['properties']['h']} m",
            })
        relations += ents.nearest_devices(lat, lon, max_m=400)

    sources = [
        {
            "source": "duty_pharmacies",
            "name": "Ministry of Health duty roster",
            "detail": "Nicosia quarterly schedule, data.gov.cy",
            "url": p.get("source_url") or LEYKOSIA_PAGE,
        },
        {
            "source": "dls_addresses" if "DLS" in (p.get("located_by") or "") else "openstreetmap",
            "name": p.get("located_by") or "Address lookup",
            "detail": p.get("precision_label"),
        },
    ]

    subtitle = " · ".join(x for x in (p.get("municipality"), p.get("address")) if x)
    entity = E.new_entity(
        p["id"],
        "duty_pharmacy",
        p["name"],
        subtitle=subtitle or "Pharmacy on duty",
        geometry=geometry,
        center=center,
        state=state,
        attributes=attributes,
        relations=relations,
        sources=sources,
        as_of=t.roster.fetched_at and datetime.fromtimestamp(t.roster.fetched_at, tz=TZ).isoformat(timespec="minutes") or None,
        tonight={
            "kind": "duty_pharmacy",
            "duty_date": p["duty_date"],
            "on_duty": on_duty,
            "address": p.get("address"),
            "directions": p.get("directions"),
            "municipality": p.get("municipality"),
            "pharmacy_tel": p.get("pharmacy_tel"),
            "precision": p.get("precision"),
            "precision_label": p.get("precision_label"),
            "located_by": p.get("located_by"),
        },
        _for_model={
            "duty_date": p["duty_date"],
            "on_duty_at_moment": on_duty,
            "precision": p.get("precision"),
            "located_by": p.get("located_by"),
            "directions": p.get("directions"),
            "pharmacy_tel": p.get("pharmacy_tel"),
        },
    )
    return entity


def build_city_event(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    t = _tonight(ents)
    if t is None:
        return None
    ev = t.get_event(eid, allow_network=(at is None))
    if ev is None:
        return None
    now = at or t.now()
    tonight_list, _ = split_events_for_moment([ev], now)
    ev_state = tonight_list[0]["event_state"] if tonight_list else "upcoming"

    lat, lon = ev.get("lat"), ev.get("lon")
    center = [lon, lat] if lat is not None and lon is not None else None
    geometry = E.point_geom(lon, lat)

    state = E.state_block("city_event", ev_state)
    if state:
        state["label"] = {
            "happening_now": "On now",
            "tonight": "On tonight",
            "upcoming": "Upcoming",
        }.get(ev_state, "Scheduled")
        state["color"] = EVENT_COLOR if ev_state in ("happening_now", "tonight") else "#8798b0"
        state["tone"] = "live" if ev_state in ("happening_now", "tonight") else "muted"

    when_text = f"{ev['start_ts'].replace('T', ' ')} to {ev['end_ts'].replace('T', ' ')}"
    attributes = [
        E.attr("When", when_text),
        E.attr("Venue", ev.get("venue_name")),
        E.attr("Address", ", ".join(x for x in (ev.get("venue_address"), ev.get("municipality")) if x)),
        E.attr("Categories", ev.get("categories")),
        E.attr("About", ev.get("description")),
        E.attr("Event page", ev.get("url")),
        E.attr("On the map", f"{ev.get('precision_label')} ({ev.get('located_by')})"),
    ]

    relations: list[dict] = []
    if lat is not None and lon is not None:
        relations += E.nearest_stop(ents, lat, lon)
        relations += ents.nearest_devices(lat, lon, max_m=400)

    sources = [
        {
            "source": "visit_cyprus",
            "name": "Visit Cyprus events calendar",
            "detail": "Deputy Ministry of Tourism",
            "url": ev.get("url") or "https://www.visitcyprus.com/events/",
        },
    ]

    return E.new_entity(
        ev["id"],
        "city_event",
        ev["title"],
        subtitle=" · ".join(x for x in (ev.get("venue_name"), ev.get("municipality")) if x),
        geometry=geometry,
        center=center,
        state=state,
        attributes=attributes,
        relations=relations,
        sources=sources,
        tonight={
            "kind": "city_event",
            "start_ts": ev["start_ts"],
            "end_ts": ev["end_ts"],
            "venue_name": ev.get("venue_name"),
            "venue_address": ev.get("venue_address"),
            "municipality": ev.get("municipality"),
            "categories": ev.get("categories"),
            "url": ev.get("url"),
            "description": ev.get("description"),
            "precision": ev.get("precision"),
            "precision_label": ev.get("precision_label"),
            "located_by": ev.get("located_by"),
        },
        _for_model={
            "when": when_text,
            "venue": ev.get("venue_name"),
            "precision": ev.get("precision"),
            "located_by": ev.get("located_by"),
        },
    )


def duty_pharmacy_at(ents, entity: dict, at: datetime) -> dict:
    from chatbot.moments import label
    built = build_duty_pharmacy(ents, entity["id"], None, at=at)
    e = built if built is not None else dict(entity)
    e["moment"] = {
        "at": at.astimezone(TZ).isoformat(timespec="minutes"),
        "label": label(at),
        "known": built is not None,
        "applies": built is not None,
    }
    return e


def city_event_at(ents, entity: dict, at: datetime) -> dict:
    from chatbot.moments import label
    built = build_city_event(ents, entity["id"], None, at=at)
    e = built if built is not None else dict(entity)
    e["moment"] = {
        "at": at.astimezone(TZ).isoformat(timespec="minutes"),
        "label": label(at),
        "known": built is not None,
        "applies": built is not None,
    }
    return e


def search_tonight(ents) -> list[dict]:
    t = _tonight(ents)
    if t is None:
        return []
    out: list[dict] = []
    today = duty_date_for(t.now())
    for r in t.roster.for_date(today):
        # Ensure coordinates are enriched lazily from cache/OSM without blocking search keystrokes
        p = t._enrich_pharmacy(r, None, allow_geocode=False)
        out.append(
            E.index_item(
                p["id"],
                "duty_pharmacy",
                p["name"],
                "Duty pharmacies",
                f"On duty tonight · {p.get('municipality')} · {p.get('address')}",
                p.get("lat"),
                p.get("lon"),
                [
                    p["id"],
                    p["name"],
                    p.get("pharmacist"),
                    p.get("address"),
                    p.get("municipality"),
                    "pharmacy on duty",
                    "duty pharmacy",
                    "φαρμακείο",
                    "διημερεύον",
                    "tonight",
                ],
                state="on_duty",
            )
        )
    for ev in t.calendar.events:
        out.append(
            E.index_item(
                ev["id"],
                "city_event",
                ev["title"],
                "Events tonight",
                f"{ev['start_ts'].replace('T', ' ')} · {ev.get('venue_name')}",
                ev.get("lat"),
                ev.get("lon"),
                [
                    ev["id"],
                    ev["title"],
                    ev.get("venue_name"),
                    ev.get("municipality"),
                    ev.get("categories"),
                    "event tonight",
                    "whats on",
                    "tonight",
                ],
                state="event",
            )
        )
    return out


E.register_kind(E.Kind("duty_pharmacy", lambda i: bool(DUTY_ID.match(i)), build_duty_pharmacy, ttl_s=120), first=True)
E.register_kind(E.Kind("city_event", lambda i: bool(EVENT_ID.match(i)), build_city_event, ttl_s=300), first=True)
E.register_at("duty_pharmacy", duty_pharmacy_at)
E.register_at("city_event", city_event_at)
E.register_search(search_tonight, {"Duty pharmacies": 6, "Events tonight": 4})

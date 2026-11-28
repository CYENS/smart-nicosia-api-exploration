"""Road reports as things in the twin: report-waze-1a2b3c4d (a Waze driver's report),
report-jam-1a2b3c4d (a Waze jam line) and report-works-639274141179895317 (a Public Works
Department event) open in the inspector, relate to the measured road segment they sit on
(road-12) and to the reports around them, are searchable from Ctrl K, and read as they were
at a moment of the city clock. A report that has left its feed still opens, from the twin's
memory, saying when it was last seen."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from chatbot import entities as E
from chatbot.geo import haversine_m
from chatbot.mobility.gtfs import TZ
from chatbot.mobility.reports import (KIND_COLORS, KINDS, LICENCE, PAGES, SOURCE_NAMES, RoadReport, age_words,
                                      local_text, when_text)

E.KIND_LABELS["road_report"] = "Road report"
REPORT_TTL_S = 60
NEARBY_M = 300
TONES = {"accident": "alarm", "closure": "warn", "jam": "warn", "hazard": "warn", "roadworks": "muted",
         "diversion": "muted"}
SOURCE_DETAIL = {
    "waze_alerts": "what drivers report in the Waze app, through FixCyprus",
    "waze_traffic": "Waze's jam lines, through FixCyprus",
    "traffic_events": "entered by its traffic operators",
}


def _mobility(ents):
    return getattr(ents, "mobility", None)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(TZ).isoformat(timespec="minutes") if t else None


def _day(t: datetime | None) -> str | None:
    return t.astimezone(TZ).strftime("%a %d %b %Y, %H:%M") if t else None


def title_of(r: RoadReport) -> str:
    """"Road closed: Agiou Georgiou", "Standstill traffic on Laerti", "Construction work"."""
    if r.street:
        return f"{r.kind_label}: {r.street}" if r.kind == "closure" else f"{r.label} on {r.street}"
    return r.label or r.kind_label


def build_report(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    m = _mobility(ents)
    if m is None or getattr(m, "reports", None) is None:
        return None
    r, listed = m.reports.find(eid, at)
    if r is None:
        raise E.EntityNotFound(f"no road report {eid}: it may have left its feed before the twin "
                               "recorded it. get_road_reports lists the reports now")
    now = at or m.now()
    status = r.status(now)
    who = SOURCE_NAMES.get(r.source, r.source)
    age = age_words(r.since, now)
    fetched = m.reports.fetched_at(r.source) if at is None else at
    if not listed:
        last = r.last_seen
        label = f"No longer reported{f', last seen {age_words(last, now)}' if last else ''}"
        state = {"value": "gone", "label": label, "color": "#8798b0", "tone": "muted"}
    elif status == "planned":
        state = {"value": "planned", "label": f"{r.label}, {when_text(r, status, now)}", "color": r.color,
                 "tone": "muted"}
    else:
        when = when_text(r, status, now) if r.source == "traffic_events" else age
        state = {"value": r.kind, "label": f"{r.label}" + (f", {when}" if when else ""), "color": r.color,
                 "tone": TONES[r.kind]}
    # The label says how old it is; no "last report" age beside it (that reads as the feed's).
    state |= {"stale": False, "reason": None, "local": None, "as_of": None, "age_s": None}
    readings = []
    d = r.details
    if d.get("length_m"):
        readings.append({"key": "length_m", "label": "Length closed" if r.kind == "closure" else "Length",
                         "value": d["length_m"], "unit": "m"})
    if d.get("speed_kmh") is not None:
        readings.append({"key": "speed_kmh", "label": "Speed in the jam", "value": d["speed_kmh"], "unit": "km/h"})
    if d.get("delay_s") is not None:
        readings.append({"key": "delay_min", "label": "Delay", "value": round(d["delay_s"] / 60, 1), "unit": "min"})
    if r.since and status != "planned" and r.source != "traffic_events":  # works say their dates instead
        days = (now - r.since).total_seconds() / 86400
        if days >= 2:
            readings.append({"key": "age_days", "label": "Reported", "value": round(days), "unit": "days ago"})
    relations = []
    for seg, dist in m.reports.on_road(r):
        relations.append({"relation": "on_road", "label": "On the measured road", "id": seg.id,
                          "kind": "road_segment", "name": seg.name, "detail": f"{round(dist)} m away"})
    others = [x for x in (m.reports.current()["reports"] if at is None else m.reports.at(at)["reports"])
              if x.id != r.id and haversine_m(r.lat, r.lon, x.lat, x.lon) <= NEARBY_M]
    others.sort(key=lambda x: haversine_m(r.lat, r.lon, x.lat, x.lon))
    for x in others[:4]:
        relations.append({"relation": "nearby", "label": "Nearby report", "id": x.id, "kind": "road_report",
                          "name": title_of(x), "detail": age_words(x.since, now), "color": x.color,
                          "distance_m": round(haversine_m(r.lat, r.lon, x.lat, x.lon)), "lon": x.lon, "lat": x.lat})
    relations += ents.places_within(r.lat, r.lon, 150, limit=2)
    works = r.source == "traffic_events"
    # The operators' own words (Greek, and English where they gave it) are in `report`, for the
    # inspector's "As published" section, which marks their language.
    attrs = [
        E.attr("What", r.label),
        E.attr("Street", r.street),
        E.attr("Starts" if works else "Reported", _day(r.since)),
        E.attr("Ends", _day(r.until)),
        E.attr("Lanes affected", d.get("affectedLanes")),
        E.attr("Closed points", d.get("points") if (d.get("points") or 0) > 1 else None),
        E.attr("Waze type", (d.get("subtype") or d.get("type")) if r.source == "waze_alerts" else None),
        E.attr("Jam level", f"{d['level']} of 5" if d.get("level") is not None else None),
        E.attr("Record", d.get("record")),
        E.attr("Seen by the twin", f"{_day(r.first_seen)} to {_day(r.last_seen)}" if r.first_seen and r.last_seen else None),
    ]
    sources = [{"source": r.source, "name": who, "detail": f"{SOURCE_DETAIL[r.source]}; {LICENCE}",
                "url": PAGES[r.source], "as_of": _iso(fetched)}]
    return E.new_entity(
        r.id, "road_report", title_of(r),
        subtitle=f"{KINDS[r.kind][0]}, from {who}" + (f", {age}" if age and not works else ""),
        geometry=r.geometry(), center=[r.lon, r.lat], state=state, readings=readings, attributes=attrs,
        relations=relations, sources=sources, as_of=_iso(fetched),
        report={"kind": r.kind, "kind_label": r.kind_label, "label": r.label, "status": status if listed else "gone",
                "source": r.source, "since": local_text(r.since), "until": local_text(r.until), "age": age,
                "text": r.text, "text_en": d.get("text_en"), "listed": listed, "color": r.color,
                "line": [list(c) for c in r.line] if r.line else None},
        _for_model={"kind": r.kind, "reported_by": who, "since": local_text(r.since), "until": local_text(r.until),
                    "age": age, "still_reported": listed, "status": status if listed else "gone",
                    "text": r.text, "text_en": d.get("text_en")})


def report_at(ents, entity: dict, at: datetime) -> dict:
    """The report as the twin's memory saw it at a moment of the city clock."""
    from chatbot.moments import label  # noqa: PLC0415 - the clock's words for a moment

    m = _mobility(ents)
    then = m.reports.at(at)
    known = then["status"] == "recorded" and then["feeds"].get(entity.get("report", {}).get("source"), {}).get("state") == "recorded"
    held = any(r.id == entity["id"] for r in then["reports"])
    e = build_report(ents, entity["id"], None, at=at) if held else dict(entity)
    e["moment"] = {"at": _iso(at), "label": label(at), "known": known, "applies": True}
    if not held:
        e["state"] = {**(e.get("state") or {}), "value": "unknown" if not known else "absent",
                      "label": "The twin's memory holds nothing for this moment" if not known
                      else "Not reported at that moment", "tone": "muted", "color": "#8798b0"}
    return e


def search_items(ents) -> list[dict]:
    m = _mobility(ents)
    if m is None or getattr(m, "reports", None) is None:
        return []
    cache = m.search_cache
    hit = cache.get("road_reports")
    reports = m.reports.cached()
    if hit is None or time.time() - hit[0] > 60 or hit[1] != len(reports):
        now = m.now() if callable(getattr(m, "now", None)) else datetime.now(timezone.utc)
        built = []
        for r in reports:
            item = E.index_item(r.id, "road_report", title_of(r), "Road reports",
                                ", ".join(x for x in (r.label if r.street else None, age_words(r.since, now)) if x),
                                r.lat, r.lon, [r.street, r.label, r.kind_label, KINDS[r.kind][2], r.text,
                                               r.details.get("text_en"), "road report traffic"])
            item["color"] = KIND_COLORS[r.kind]
            built.append(item)
        cache["road_reports"] = hit = (time.time(), len(reports), built)
    return hit[2]


E.register_kind(E.Kind("road_report", lambda i: i.lower().startswith("report-"), build_report, ttl_s=REPORT_TTL_S))
E.register_at("road_report", report_at)
E.register_search(search_items, {"Road reports": 5})

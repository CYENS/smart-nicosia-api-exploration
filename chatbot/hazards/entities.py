"""Hazards as things in the twin: an earthquake (eq-20251112_0000180), a weather warning
(wx-cy-250723170828-maxtemp-000) and a day's fire danger in the Nicosia district (fire-2025-07-23;
fire-nicosia is today's). The inspector opens them, search finds the recent ones and `get_entity`
reads them, from the Agent's Hazards (reached as ents.watcher.hazards)."""

from __future__ import annotations

import re
from datetime import date, timedelta

from chatbot import entities as E
from chatbot.hazards import feeds as F
from chatbot.hazards.feeds import TZ, Hazard

QUAKE_ID = re.compile(r"^eq-\d{8}_\d{7}$")
WARNING_ID = re.compile(r"^wx-[a-z0-9-]{3,64}$")
FIRE_ID = re.compile(r"^fire-(\d{4}-\d{2}-\d{2}|nicosia)$")
TONES = {"high": "alarm", "medium": "warn", "low": "muted"}
COLORS = {"high": "#ff5a4e", "medium": "#f5a524", "low": "#8798b0"}
LEVEL_COLORS = {"yellow": "#e8c547", "orange": "#ff8a3d", "red": "#ff5a4e"}
FIRE_COLORS = {"low": "#8798b0", "moderate": "#9fb0c6", "high": "#f5a524", "very_high": "#ff8a3d",
               "extreme": "#ff5a4e", "very_extreme": "#c81e3c"}
SOURCES = {
    "emsc": {"source": "emsc", "name": "EMSC, with the Cyprus Geological Survey Department",
             "detail": "the quake's solution, and the felt reports people sent EMSC"},
    "meteoalarm": {"source": "meteoalarm", "name": "Meteoalarm, from the Department of Meteorology",
                   "detail": "the official warning, as issued", "url": "https://meteoalarm.org/en/live/region/CY"},
    "effis": {"source": "effis", "name": "EFFIS fire danger forecast (Copernicus)",
              "detail": "the fire weather index over the Nicosia district",
              "url": "https://forest-fire.emergency.copernicus.eu/apps/effis_current_situation/"},
}

E.KIND_LABELS.update({"earthquake": "Earthquake", "warning": "Weather warning", "fire_danger": "Fire danger"})


def hazards_of(ents: E.Entities):
    return getattr(getattr(ents, "watcher", None), "hazards", None)


def _incidents(ents: E.Entities, device: str) -> list[dict]:
    w = getattr(ents, "watcher", None)
    if w is None:
        return []
    rows = w.book.query(status="all", entity=device, limit=4)
    return [{"relation": "incident", "label": "Raised as", "id": r["id"], "kind": "incident",
             "name": f"{r['id']}: {r['title']}", "detail": f"{r['severity']} severity, {r['status']}"}
            for r in rows]


def _src(h: Hazard, url: str | None = None) -> list[dict]:
    s = dict(SOURCES[h.source])
    s["as_of"] = F.wall(h.issued)
    if url:
        s["url"] = url
    return [s]


def build_quake(ents: E.Entities, eid: str, near) -> dict | None:
    hz = hazards_of(ents)
    h = hz.get(eid) if hz is not None else None
    if h is None:
        return None
    d, felt = h.details, h.details.get("felt") or {}
    raises, severity, why = F.quake_rules(h)
    label = {"high": "Felt in Nicosia" if (felt.get("near_nicosia") or 0) >= F.FELT_PAGES else "Strong, near",
             "medium": "Likely felt", "low": "Minor"}[severity] if raises else "Too small or far to matter here"
    readings = [{"key": "magnitude", "label": f"Magnitude ({d.get('magtype') or 'M'})", "value": h.value, "unit": ""},
                {"key": "depth", "label": "Depth", "value": d.get("depth_km"), "unit": "km"},
                {"key": "distance", "label": "From Nicosia's centre", "value": d.get("distance_km"), "unit": "km"}]
    if felt.get("count") is not None:
        readings.append({"key": "felt", "label": "Felt reports", "value": felt["count"], "unit": ""})
    if felt.get("near_nicosia") is not None:
        readings.append({"key": "felt_near", "label": f"Felt within {F.FELT_NEAR_KM:.0f} km of Nicosia",
                         "value": felt["near_nicosia"], "unit": ""})
    i = felt.get("near_intensity")
    return E.new_entity(
        h.id, "earthquake", h.title, subtitle=f"{h.start.astimezone(TZ):%a %d %b %Y, %H:%M}, {d.get('where')}",
        geometry=E.point_geom(h.lon, h.lat), center=[h.lon, h.lat],
        state={"value": severity if raises else "minor", "label": label,
               "color": COLORS[severity] if raises else COLORS["low"],
               "tone": TONES[severity] if raises else "muted", "stale": False, "reason": why,
               "local": None, "as_of": None, "age_s": None},
        readings=readings,
        attributes=[E.attr("Time", f"{h.start.astimezone(TZ):%Y-%m-%d %H:%M:%S}"),
                    E.attr("Where", F.where(h.lat, h.lon)["words"] if h.lat is not None else None),
                    E.attr("Direction from Nicosia", d.get("direction")),
                    E.attr("District", d.get("district")), E.attr("At sea", d.get("at_sea")),
                    E.attr("Located by", d.get("located_by")), E.attr("Region", d.get("region")),
                    E.attr("How it felt near Nicosia",
                           f"{F.INTENSITY_WORDS.get(i, '?')} (intensity {F.ROMAN.get(i, i)}), the median report"
                           if i else None),
                    E.attr("Last revised", (F.wall(h.issued) or "").replace("T", " ") or None)],
        relations=_incidents(ents, h.id), sources=_src(h, d.get("url")), as_of=E._iso(h.issued))


def build_warning(ents: E.Entities, eid: str, near) -> dict | None:
    hz = hazards_of(ents)
    h = hz.get(eid) if hz is not None else None
    if h is None or h.kind != "warning":
        return None
    d = h.details
    now = hz.now()
    in_force = F.warning_in_force(h, now)
    over = h.end is not None and h.end <= now
    state = "In force" if in_force else ("Over" if over else "Issued for later")
    level = h.level or "yellow"
    return E.new_entity(
        h.id, "warning", h.title, subtitle=f"{state}: {F.wall(h.start).replace('T', ' ')} to "
                                           f"{(F.wall(h.end) or '?').replace('T', ' ')}",
        geometry=E.point_geom(h.lon, h.lat), center=[h.lon, h.lat] if h.lat is not None else None,
        state={"value": level, "label": f"{level.capitalize()}, {state.lower()}",
               "color": LEVEL_COLORS.get(level, "#e8c547"),
               "tone": "alarm" if level == "red" else ("warn" if in_force else "muted"), "stale": False,
               "reason": d.get("headline"), "local": None, "as_of": None, "age_s": None},
        attributes=[E.attr("Event", d.get("event")), E.attr("Awareness level", d.get("level_word")),
                    E.attr("Area", d.get("area")), E.attr("Covers Nicosia", d.get("covers_nicosia")),
                    E.attr("From", F.wall(h.start).replace("T", " ")),
                    E.attr("Until", (F.wall(h.end) or "").replace("T", " ") or None),
                    E.attr("Issued", (F.wall(h.issued) or "").replace("T", " ") or None),
                    E.attr("Withdrawn", (d.get("withdrawn_at") or "").replace("T", " ") or None),
                    E.attr("What it says", d.get("description")),
                    E.attr("What to do", d.get("instruction")),
                    E.attr("In Greek", d.get("description_local"))],
        relations=_incidents(ents, h.id), sources=_src(h), as_of=E._iso(h.issued))


def build_fire(ents: E.Entities, eid: str, near) -> dict | None:
    hz = hazards_of(ents)
    if hz is None:
        return None
    m = FIRE_ID.match(eid)
    day = hz.now().astimezone(TZ).date() if m.group(1) == "nicosia" else date.fromisoformat(m.group(1))
    h = hz.fire_day(day)
    if h is None:
        return None
    d = h.details
    shares = d.get("shares") or {}
    readings = [{"key": k, "label": F.FIRE_WORDS[k], "value": round(100 * v), "unit": "% of the district"}
                for k, v in sorted(shares.items(), key=lambda kv: -F.FIRE_CLASSES.index(kv[0]))]
    ahead = [hz.known.get(f"fire-{(day + timedelta(days=k)).isoformat()}") for k in (1, 2)]
    return E.new_entity(
        h.id if m.group(1) != "nicosia" else eid, "fire_danger", h.title,
        subtitle=f"{h.start.astimezone(TZ):%a %d %b %Y}" + (", a forecast" if d.get("forecast") else ""),
        geometry=E.point_geom(h.lon, h.lat), center=[h.lon, h.lat],
        state={"value": h.level, "label": F.FIRE_WORDS.get(h.level, h.level),
               "color": FIRE_COLORS.get(h.level, "#8798b0"),
               "tone": "alarm" if F.fire_index(h) >= 4 else ("warn" if F.fire_index(h) >= 2 else "muted"),
               "stale": False, "reason": f"fire weather index {d.get('fwi')} over at least a quarter of the district",
               "local": None, "as_of": None, "age_s": None},
        readings=readings,
        attributes=[E.attr("Day", d.get("day")), E.attr("District", d.get("district")),
                    E.attr("At Nicosia's centre", F.FIRE_WORDS.get(d.get("city_level") or "", None)),
                    E.attr("Read on", d.get("read_on")),
                    *[E.attr(f"Forecast for {x.start.astimezone(TZ):%a %d %b}", F.FIRE_WORDS.get(x.level))
                      for x in ahead if x is not None and m.group(1) == "nicosia"]],
        relations=_incidents(ents, "fire-nicosia"), sources=_src(h), as_of=E._iso(h.issued))


def search_items(ents: E.Entities) -> list[dict]:
    hz = hazards_of(ents)
    if hz is None:
        return []
    now = hz.now()
    out = []
    for h in hz.earthquakes(now, days=14):
        if not F.quake_rules(h)[0]:
            continue
        item = E.index_item(h.id, "earthquake", h.title, "Hazards",
                            f"{h.start.astimezone(TZ):%a %d %b %H:%M}, {h.details.get('depth_km', 0):.0f} km deep",
                            h.lat, h.lon, [h.id, h.title, "earthquake quake seismic"])
        out.append(item)
    for h in hz.warnings(now):
        out.append(E.index_item(h.id, "warning", h.title, "Hazards", F.wall(h.start).replace("T", " "),
                                h.lat, h.lon, [h.id, h.title, "weather warning meteoalarm"]))
    return out


E.register_kind(E.Kind("earthquake", lambda i: bool(QUAKE_ID.match(i)), build_quake, ttl_s=60), first=True)
E.register_kind(E.Kind("warning", lambda i: bool(WARNING_ID.match(i)), build_warning, ttl_s=60), first=True)
E.register_kind(E.Kind("fire_danger", lambda i: bool(FIRE_ID.match(i)), build_fire, ttl_s=300), first=True)
E.register_search(search_items, {"Hazards": 4})

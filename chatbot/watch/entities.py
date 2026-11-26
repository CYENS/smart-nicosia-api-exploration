"""Incidents as things in the twin: an id (INC-12) opens it, search finds it, chips link it.
The page opens an incident in its inbox; `get_entity` and search read it from here."""

from __future__ import annotations

import re

from chatbot import entities as E

INCIDENT_ID = re.compile(r"^INC-\d{1,7}$", re.IGNORECASE)
TONE = {"high": "alarm", "medium": "warn", "low": "muted"}
COLORS = {"high": "#ff5a4e", "medium": "#f5a524", "low": "#8798b0"}
STATUS = {"open": "Open", "acknowledged": "Acknowledged", "snoozed": "Snoozed",
          "resolved": "Resolved"}


def _watcher(ents: E.Entities):
    return getattr(ents, "watcher", None)


def build_incident(ents: E.Entities, eid: str, near) -> dict | None:
    w = _watcher(ents)
    if w is None:
        return None
    x = w.book.get(eid)
    if x is None or not x["members"]:
        return None
    d = w.book.detail(x, w.now())
    center = [d["lon"], d["lat"]] if d.get("lat") is not None else None
    return E.new_entity(
        d["id"], "incident", d["title"], subtitle=f"{d['category']}, {d['place']}",
        geometry=E.point_geom(d.get("lon"), d.get("lat")), center=center,
        state={"value": d["severity"], "label": f"{d['severity'].capitalize()} severity",
               "color": COLORS[d["severity"]], "tone": TONE[d["severity"]], "stale": False,
               "reason": d["summary"], "local": STATUS.get(d["status"]), "as_of": None,
               "age_s": None},
        readings=[{"key": "devices", "label": "Devices", "value": d["devices"], "unit": ""}],
        attributes=[E.attr("Status", STATUS.get(d["status"], d["status"])),
                    E.attr("Began", d["start"].replace("T", " ")),
                    E.attr("Still happening", "yes" if d["active"] else "no"),
                    E.attr("Category", d["category"]), E.attr("Place", d["place"])],
        relations=[{"relation": "involves", "label": m["what"], "id": m["id"], "kind": m["kind"],
                    "name": m["label"]} for m in d["members"][:20]],
        sources=[{"name": "The twin's watch",
                  "detail": "its detectors over the twin's memory, and the city's alarms"}],
    )


def search_items(ents: E.Entities) -> list[dict]:
    w = _watcher(ents)
    if w is None:
        return []
    out = []
    for r in w.book.query(status="unresolved", limit=60):
        item = E.index_item(r["id"], "incident", f"{r['id']}: {r['title']}", "Incidents",
                            f"{r['severity'].capitalize()} severity, {r['status']}, {r['place']}",
                            r.get("lat"), r.get("lon"), [r["id"], r["title"], r["place"]])
        item["color"] = COLORS[r["severity"]]
        out.append(item)
    return out


E.KIND_LABELS["incident"] = "Incident"
E.register_kind(E.Kind("incident", lambda i: bool(INCIDENT_ID.match(i)), build_incident,
                       ttl_s=15), first=True)
E.register_search(search_items, {"Incidents": 5})

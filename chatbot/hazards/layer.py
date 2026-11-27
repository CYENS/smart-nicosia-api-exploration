"""Hazards as a map layer, one shape for the page's Hazards row and the chat tool's layer_ref: the
features (quakes as points sized by magnitude, their felt reports as cells, a warning's area) and
`meta.hazards` (the fire danger grid over the island, the moment, what is in force). The page
draws them with its own renderer (lib/hazards/map.ts), which claims a layer by `meta.hazards`."""

from __future__ import annotations

from chatbot.hazards import feeds as F

SEVERITY_COLORS = {"high": "#ff5a4e", "medium": "#f5a524", "low": "#8798b0", "minor": "#5d6f88"}
QUAKE_WORDS = {"high": "felt strongly here", "medium": "likely felt here", "low": "near, minor",
               "minor": "too small or far to matter here"}
LEVEL_COLORS = {"yellow": "#e8c547", "orange": "#ff8a3d", "red": "#ff5a4e"}
FIRE_COLORS = {"low": "#8798b0", "moderate": "#9fb0c6", "high": "#f5a524", "very_high": "#ff8a3d",
               "extreme": "#ff5a4e", "very_extreme": "#c81e3c"}
FELT_COLORS = {2: "#8798b0", 3: "#e8c547", 4: "#f5a524", 5: "#ff5a4e"}


def felt_color(i: float) -> str:
    return FELT_COLORS[min(5, max(2, round(i)))]


def quake_severity(q: dict) -> str:
    # The rules read the magnitude, the distance and the felt reports, not the time.
    h = F.Hazard(q["id"], "earthquake", "emsc", F.parse_time(q["start"]), None, q["title"],
                 value=q.get("value"), details=q)
    raises, severity, _ = F.quake_rules(h)
    return severity if raises else "minor"


def quake_popup(q: dict) -> str:
    felt = q.get("felt") or {}
    lines = [f"{q['title']}", f"{q['start'].replace('T', ' ')}, {q.get('depth_km', 0):.0f} km deep",
             f"Located by {q.get('located_by')}"]
    if felt.get("count"):
        lines.append(f"Felt by {felt['count']:,} people" + (f" ({felt['near_nicosia']:,} near Nicosia)"
                                                          if felt.get("near_nicosia") else ""))
    return "\n".join(lines)


def features(m: dict, *, incidents: dict[str, str] | None = None, felt: bool = True) -> list[dict]:
    """The features of a moment (Hazards.moment): quakes newest last, so they draw on top."""
    incidents = incidents or {}
    out = []
    for w in m.get("warnings") or []:
        area = F.area_feature(w.get("area_id") or "cyprus") if w.get("covers_nicosia") else None
        if area is None:
            continue
        level = w.get("level") or "yellow"
        area["properties"] |= {
            "id": w["id"], "kind": "warning", "level": level, "color": LEVEL_COLORS.get(level, "#e8c547"),
            "label": w["title"], "in_force": bool(w.get("in_force")), "incident": incidents.get(w["id"]),
            "state": f"{level} warning", "popup": f"{w['title']}\n{w['start'].replace('T', ' ')} to "
                                                  f"{(w.get('end') or '?').replace('T', ' ')}"
                                                  + ("\nIn force" if w.get("in_force") else "\nIssued for later")}
        out.append(area)
    fire = m.get("fire")
    if fire:
        district = F.area_feature(fire.get("area_id") or "nicosia")
        if district is not None:
            level = fire.get("level") or "low"
            district["properties"] |= {"id": fire["id"], "kind": "fire_district", "level": level,
                                       "color": FIRE_COLORS.get(level, "#8798b0"), "label": fire["title"],
                                       "state": f"fire danger {F.FIRE_WORDS.get(level, level).lower()}",
                                       "incident": incidents.get("fire-nicosia"),
                                       "popup": f"{fire['title']}\n{fire.get('day')}"
                                                + (" (a forecast)" if fire.get("forecast") else "")}
            out.append(district)
    quakes = sorted(m.get("earthquakes") or [], key=lambda q: q["start"])
    for q in quakes:
        if felt:
            for c in (q.get("felt") or {}).get("cells") or []:
                out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
                            "properties": {"kind": "felt", "quake": q["id"], "n": c["n"], "i": c["i"],
                                           "color": felt_color(c["i"])}})
    for q in quakes:
        sev = quake_severity(q)
        out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [q["lon"], q["lat"]]},
                    "properties": {"id": q["id"], "kind": "earthquake", "mag": q.get("value"),
                                   "severity": sev, "color": SEVERITY_COLORS[sev], "t": q["start"],
                                   "label": f"M{q.get('value', 0):.1f}", "incident": incidents.get(q["id"]),
                                   "state": QUAKE_WORDS[sev], "popup": quake_popup(q)}})
    return out


def legend(feats: list[dict], fire: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    sev = {f["properties"]["severity"] for f in feats if f["properties"].get("kind") == "earthquake"}
    for s in ("high", "medium", "low", "minor"):
        if s in sev:
            out[f"Earthquake, {QUAKE_WORDS[s]}"] = SEVERITY_COLORS[s]
    if any(f["properties"].get("kind") == "felt" for f in feats):
        out["Felt reports, weak to strong"] = FELT_COLORS[4]
    for f in feats:
        if f["properties"].get("kind") == "warning":
            level = f["properties"]["level"]
            out[f"{level.capitalize()} weather warning"] = LEVEL_COLORS.get(level, "#e8c547")
    if fire:
        level = fire.get("level") or "low"
        out[f"Fire danger: {F.FIRE_WORDS.get(level, level).lower()} in the district"] = FIRE_COLORS.get(level, "#8798b0")
    return out


def meta(m: dict) -> dict:
    """What the renderer needs beyond the features: the fire grid and the moment."""
    fire = m.get("fire") or None
    return {"at": m.get("at"), "live": m.get("live"),
            "fire": ({k: fire.get(k) for k in ("id", "day", "level", "grid", "size", "bbox", "forecast", "title")}
                     if fire else None),
            "fire_colors": FIRE_COLORS, "classes": list(F.FIRE_CLASSES)}


def incident_ids(watcher) -> dict[str, str]:
    """Each hazard's newest incident: hazard id -> INC-12."""
    if watcher is None:
        return {}
    out: dict[str, tuple[int, str]] = {}
    with watcher.book.lock:
        xs = list(watcher.book.incidents.values())
    for x in xs:
        for mem in x["members"]:
            if mem.get("family") in ("quake", "warning", "fire"):
                if mem["device"] not in out or out[mem["device"]][0] < x["number"]:
                    out[mem["device"]] = (x["number"], x["id"])
    return {k: v[1] for k, v in out.items()}

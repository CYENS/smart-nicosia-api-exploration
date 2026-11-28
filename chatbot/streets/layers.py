"""The street rules as map features, shared by the page's own layers (/api/streets/...) and the
chat tools' layers, so both draw alike. Every feature carries an `id` the inspector opens and
the charts link by, and a `layer` the page's streets renderer styles it by."""

from __future__ import annotations

from chatbot.streets.analysis import VERDICT_COLORS, VERDICTS, centre_of

# Posted limits, coolest for the slowest: calm hues that read on the night map without shouting
# (sodium and signal red stay for what is live and what is wrong). The page steps them for Day.
LIMIT_COLORS = {20: "#8be3c9", 30: "#4fc39a", 40: "#44b3c2", 50: "#5b8fd9", 65: "#8f7ae0",
                80: "#b46fd6", 100: "#d86bb3"}
TIER_LABELS = {"signed": "Posted: near a sign, or between signs that agree",
               "likely": "Likely: further along from one sign",
               "uncertain": "Changes somewhere here: the signs either side disagree",
               "mapped": "From OpenStreetMap, where no national sign reaches"}
ZONE_COLOR = "#4fc39a"
CYCLE_COLOR = "#74d68a"
BUS_LANE_COLOR = "#f08fb8"
CAMERA_COLOR = "#c4a2ff"
SIGNAL_COLOR = "#d6dee9"
CROSSING_COLOR = "#e4ebf4"
GAP_COLOR = "#ff9a71"


def limit_color(kmh: int | None) -> str:
    if not kmh:
        return "#8798b0"
    return LIMIT_COLORS.get(kmh) or min(LIMIT_COLORS.items(), key=lambda t: abs(t[0] - kmh))[1]


def _line(coords, props) -> dict:
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": props}


def _multi(lines, props) -> dict:
    geom = ({"type": "LineString", "coordinates": lines[0]} if len(lines) == 1 else
            {"type": "MultiLineString", "coordinates": lines})
    return {"type": "Feature", "geometry": geom, "properties": props}


def _point(lon, lat, props) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def limit_features(pieces: list[dict]) -> list[dict]:
    out = []
    for p in pieces:
        alt = f" or {p['alt_kmh']}" if p.get("alt_kmh") else ""
        how = {"signed": "posted", "likely": "likely", "uncertain": "changes here",
               "mapped": "from OpenStreetMap"}[p["tier"]]
        out.append(_line(p["coords"], {
            "id": p["sign"] or f"limitway-{p['way']}", "layer": "limits", "kmh": p["kmh"], "tier": p["tier"],
            "alt_kmh": p.get("alt_kmh"), "label": str(p["kmh"]), "color": limit_color(p["kmh"]),
            "state": f"{p['kmh']} km/h", "street": p.get("street"), "length_m": p["length_m"],
            "entity_kind": "speed_limit",
            "popup": f"{p.get('street') or 'Street'}\n{p['kmh']}{alt} km/h, {how}"}))
    return out


def sign_features(signs: list[dict], snaps: list | None = None) -> list[dict]:
    out = []
    for k, s in enumerate(signs):
        snap = snaps[k] if snaps and k < len(snaps) else None
        lon, lat = (snap.point if snap else (s["lon"], s["lat"]))
        out.append(_point(round(lon, 6), round(lat, 6), {
            "id": s["id"], "layer": "signs", "kmh": s["kmh"], "label": str(s["kmh"]),
            "color": limit_color(s["kmh"]), "state": f"{s['kmh']} km/h sign", "entity_kind": "speed_limit",
            "popup": f"Speed limit sign\n{s['kmh']} km/h"}))
    return out


def zone_features(zones: list[dict], entrances: list[dict] | None = None) -> list[dict]:
    out = []
    for z in zones:
        out.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [z["ring"]]},
                    "properties": {"id": z["id"], "layer": "zones", "kmh": z["kmh"], "label": f"{z['kind']}, {z['name']}",
                                   "name": z["name"], "status": z["status"], "color": ZONE_COLOR,
                                   "state": z["status"], "entity_kind": "slow_zone",
                                   "popup": f"{z['kind']}, {z['name']}\n{z['status'].capitalize()}"}})
    for e in entrances or []:
        out.append(_point(e["lon"], e["lat"], {
            "id": e["id"], "layer": "zone_entrances", "label": "", "color": ZONE_COLOR, "state": "entrance",
            "entity_kind": "slow_zone_entrance", "popup": f"Entrance to {e['kind']} {e.get('zone_name') or ''}".strip()}))
    return out


def signal_features(signals: list[dict]) -> list[dict]:
    return [_point(s["lon"], s["lat"], {
        "id": s["id"], "layer": "signals", "label": s["junction"] or "", "color": SIGNAL_COLOR,
        "state": "traffic signal", "entity_kind": "traffic_signal",
        "popup": f"Traffic signals\n{s['junction'] or ''}".strip()}) for s in signals]


def crossing_features(crossings: list[dict]) -> list[dict]:
    return [_point(x["lon"], x["lat"], {
        "id": x["id"], "layer": "crossings", "label": x["name"], "color": CROSSING_COLOR,
        "state": "pedestrian crossing", "entity_kind": "pedestrian_crossing",
        "popup": f"Pedestrian crossing\n{x['name']}"}) for x in crossings]


def camera_features(cameras: list[dict]) -> list[dict]:
    out = []
    for c in cameras:
        where = {"feed": "", "junction": "\nPlaced at the junction its name gives",
                 "rough": "\nThe feed places it only to about a kilometre"}[c.get("placed", "feed")]
        out.append(_point(c["lon"], c["lat"], {
            "id": c["id"], "layer": "cameras", "label": c.get("code") or "", "color": CAMERA_COLOR,
            "state": "rough position" if c.get("placed") == "rough" else "enforcement camera",
            "direction": c.get("direction"), "entity_kind": "enforcement_camera",
            "popup": f"Enforcement camera {c.get('code') or ''}\n{c['name']}{where}".strip()}))
    return out


def cycle_features(cycleways: list[dict], gaps: list[dict] | None = None,
                   loose_ends: list[dict] | None = None) -> list[dict]:
    out = [_multi(c["lines"], {"id": c["id"], "layer": "cycleways", "label": c["name"], "color": CYCLE_COLOR,
                               "state": "cycleway", "entity_kind": "cycleway", "popup": c["name"]})
           for c in cycleways]
    for g in gaps or []:
        out.append(_line([g["from"], g["to"]], {
            "id": g["id"], "layer": "cycle_gaps", "label": f"{g['length_m']} m gap", "color": GAP_COLOR,
            "state": "gap", "length_m": g["length_m"],
            "popup": f"A {g['length_m']} m gap in the cycle network" + (f"\nalong {g['street']}" if g.get("street") else "")}))
    for k, e in enumerate(loose_ends or []):
        out.append(_point(e["lon"], e["lat"], {
            "id": e["id"], "layer": "cycle_ends", "label": "", "color": GAP_COLOR, "state": "loose end",
            "entity_kind": "cycleway", "popup": f"{e['route']} ends here"}))
    return out


def bus_lane_features(lanes: list[dict]) -> list[dict]:
    return [_multi(b["lines"], {
        "id": b["id"], "layer": "bus_lanes", "label": b["name"], "color": BUS_LANE_COLOR, "state": b["status"],
        "status": b["status"], "entity_kind": "bus_lane",
        "popup": f"Bus lane, {b['name']}\n{'In use' if b['status'] == 'active' else 'Planned'}"}) for b in lanes]


def school_features(rows: list[dict]) -> list[dict]:
    return [_point(r["lon"], r["lat"], {
        "id": r["id"], "layer": "schools", "label": r["name"], "color": VERDICT_COLORS[r["verdict"]],
        "state": r["verdict"], "entity_kind": "school",
        "popup": f"{r['name']}\n{VERDICTS[r['verdict']]}"}) for r in rows]


def limit_legend() -> dict[str, str]:
    return {f"{k} km/h": v for k, v in LIMIT_COLORS.items() if k != 40}


__all__ = ["limit_features", "sign_features", "zone_features", "signal_features", "crossing_features",
           "camera_features", "cycle_features", "bus_lane_features", "school_features", "limit_color",
           "LIMIT_COLORS", "TIER_LABELS", "limit_legend", "centre_of"]

"""Tool for Tonight in Nicosia: pharmacies on duty, city events, and places open now."""

from __future__ import annotations

from datetime import datetime

from chatbot.settings import NICOSIA_CENTER
from chatbot.store import point
from chatbot.tonight.service import TZ
from chatbot.tools.base import NEAR, ToolContext, ToolError, ToolSet, obj, resolve_point

TOOLS = ToolSet()
tool = TOOLS.tool

DUTY_COLOR = "#10b981"
EVENT_COLOR = "#ec4899"
OPEN_COLOR = "#f59e0b"

TONIGHT_LEGEND = {
    "on duty": DUTY_COLOR,
    "event": EVENT_COLOR,
    "open now": OPEN_COLOR,
}


def _tonight(ctx: ToolContext):
    if ctx.tonight is None:
        raise ToolError("Tonight in Nicosia is not available on this server")
    return ctx.tonight


def _parse_at(text: str | None) -> datetime | None:
    if not text or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip())
        return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt).astimezone(TZ)
    except ValueError:
        raise ToolError("at must be an ISO local date/time like YYYY-MM-DDTHH:MM") from None


def _build_features(
    pharmacies: list[dict],
    events: list[dict],
    open_now: list[dict],
    focus: str,
) -> tuple[list[dict], list[dict]]:
    features: list[dict] = []
    rows: list[dict] = []

    if focus in ("all", "pharmacies"):
        for p in pharmacies:
            walk = f"{p['walk_min']} min walk ({p['distance_m']} m)" if p.get("walk_min") else ""
            if p.get("lat") is not None and p.get("lon") is not None:
                popup_lines = [
                    p["name"],
                    f"On duty {p['duty_date']} (until 08:00 next morning)",
                    p.get("address") or p.get("municipality") or "",
                    p.get("directions") or "",
                    f"Phone: {p['pharmacy_tel']}" if p.get("pharmacy_tel") else "",
                    f"{walk} · {p.get('precision_label', '')}".strip(" ·"),
                ]
                features.append(point(p["lon"], p["lat"], {
                    "id": p["id"],
                    "label": p["name"],
                    "kind": "duty_pharmacy",
                    "state": "on duty",
                    "color": DUTY_COLOR,
                    "precision": p.get("precision"),
                    "popup": "\n".join(line for line in popup_lines if line),
                }))
            rows.append({
                "id": p["id"],
                "type": "duty_pharmacy",
                "name": p["name"],
                "area": p.get("municipality"),
                "address": p.get("address"),
                "status": "On duty until 08:00",
                "phone": p.get("pharmacy_tel"),
                "precision": p.get("precision"),
                "walk_min": p.get("walk_min"),
                "distance_m": p.get("distance_m"),
                "lat": p.get("lat"),
                "lon": p.get("lon"),
            })

    if focus in ("all", "events"):
        for ev in events:
            when = ev["start_ts"][11:16] if len(ev.get("start_ts", "")) >= 16 else ""
            if ev.get("lat") is not None and ev.get("lon") is not None:
                popup_lines = [
                    ev["title"],
                    f"Starts {when}" if when else "Event tonight",
                    ev.get("venue_name") or ev.get("municipality") or "",
                    ev.get("precision_label") or "",
                ]
                features.append(point(ev["lon"], ev["lat"], {
                    "id": ev["id"],
                    "label": ev["title"],
                    "kind": "city_event",
                    "state": "event",
                    "color": EVENT_COLOR,
                    "precision": ev.get("precision"),
                    "popup": "\n".join(line for line in popup_lines if line),
                }))
            rows.append({
                "id": ev["id"],
                "type": "city_event",
                "name": ev["title"],
                "area": ev.get("municipality") or ev.get("venue_name"),
                "address": ev.get("venue_address") or ev.get("venue_name"),
                "status": ev.get("event_state", "tonight").replace("_", " ").capitalize(),
                "phone": None,
                "precision": ev.get("precision"),
                "walk_min": ev.get("walk_min"),
                "distance_m": ev.get("distance_m"),
                "lat": ev.get("lat"),
                "lon": ev.get("lon"),
            })

    if focus in ("all", "open_now"):
        for pl in open_now:
            features.append(point(pl["lon"], pl["lat"], {
                "id": pl["id"],
                "label": pl["name"],
                "kind": "place",
                "state": "open now",
                "color": OPEN_COLOR,
                "precision": "exact",
                "popup": "\n".join(x for x in (
                    pl["name"],
                    pl.get("category", "").replace("_", " ").title(),
                    pl.get("status_text") or "Open now",
                    f"{pl['walk_min']} min walk ({pl['distance_m']} m)" if pl.get("walk_min") else "",
                ) if x),
            }))
            rows.append({
                "id": pl["id"],
                "type": pl.get("category") or "place",
                "name": pl["name"],
                "area": pl.get("street"),
                "address": pl.get("street"),
                "status": pl.get("status_text") or "Open now",
                "phone": pl.get("phone"),
                "precision": "exact",
                "walk_min": pl.get("walk_min"),
                "distance_m": pl.get("distance_m"),
                "lat": pl["lat"],
                "lon": pl["lon"],
            })

    return features, rows


@tool(
    "whats_on_tonight",
    "What is on tonight or open right now in Nicosia: (1) the official Ministry of Health "
    "pharmacies on overnight duty until 08:00 next morning (with landmark directions, pharmacy "
    "phone, and map placement precision from OSM, Overture and DLS official addresses), "
    "(2) cultural and city events from Visit Cyprus tonight and coming up, and (3) OSM places "
    "currently open from their opening_hours (including 24/7 and late-night venues). Give "
    "near_place ('me', a landmark or square) for walking distances and minutes. Returns a "
    "layer_ref (draw with 'points') and a data_ref table linked by id.",
    obj({
        "focus": {
            "type": "string",
            "enum": ["all", "pharmacies", "events", "open_now"],
            "description": "What to emphasize (default 'all').",
        },
        "category": {
            "type": "string",
            "description": "Optional OSM category for open_now, e.g. restaurant, cafe, bar, supermarket, pharmacy.",
        },
        "at": {
            "type": "string",
            "description": "Optional local ISO date/time (YYYY-MM-DDTHH:MM) for a past night or future schedule check.",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 40},
        **NEAR,
    }),
)
def h_whats_on_tonight(ctx: ToolContext, a: dict) -> dict:
    t = _tonight(ctx)
    focus = a.get("focus") or "all"
    when = _parse_at(a.get("at"))
    pt = resolve_point(ctx, a.get("near_place"), a.get("near_lat"), a.get("near_lon"))
    origin = (pt[0], pt[1]) if pt else (
        (ctx.user_location["lat"], ctx.user_location["lon"]) if ctx.user_location else None
    )
    origin_label = pt[2] if pt else ("your location" if ctx.user_location else None)

    snap = t.snapshot(
        at=when,
        near=origin,
        category=a.get("category"),
        radius_m=float(a["radius_m"]) if a.get("radius_m") else None,
        open_limit=int(a.get("limit") or 16),
        allow_network=(when is None),
        include_walking=(focus in ("all", "pharmacies")),
        origin_label=origin_label,
    )

    pharmacies = snap["pharmacies"]
    events = snap["events"]
    open_now = snap["open_now"]
    features, rows = _build_features(pharmacies, events, open_now, focus)

    res: dict = {
        "at": snap["at"],
        "duty_date": snap["duty_date"],
        "shift_note": snap["shift_note"],
        "known": snap["known"],
        "walk_origin": snap["walk_origin"] or {"lat": NICOSIA_CENTER[0], "lon": NICOSIA_CENTER[1], "label": "Nicosia centre"},
        "status": snap["status"],
        "counts": snap["counts"],
        "pharmacies": pharmacies if focus in ("all", "pharmacies") else [],
        "events": events if focus in ("all", "events") else [],
        "upcoming_events": snap["upcoming_events"] if focus in ("all", "events") else [],
        "open_now": open_now if focus in ("all", "open_now") else [],
    }
    if features:
        res["layer_ref"] = ctx.store.add_layer(
            "tonight",
            features,
            geometry="point",
            title="Tonight in Nicosia",
            legend=TONIGHT_LEGEND,
        )
    if rows:
        res["data_ref"] = ctx.store.add_dataset(
            "tonight",
            rows,
            title="Tonight in Nicosia",
            link={"key": "id"},
        )
    res["_summary"] = (
        f"{len(pharmacies)} duty pharmacies, {len(events)} events tonight, {len(open_now)} open now"
    )
    return res

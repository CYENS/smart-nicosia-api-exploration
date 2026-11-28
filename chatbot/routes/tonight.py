"""Tonight endpoints: duty pharmacies, VisitCyprus events, open-now OSM places, and walking times."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from chatbot.routes import agent_of
from chatbot.routes.live import AT
from chatbot.settings import NICOSIA_CENTER
from chatbot.tonight.service import TZ, Tonight

router = APIRouter()


def tonight_of(request: Request) -> Tonight:
    t = getattr(agent_of(request), "tonight", None)
    if t is None:
        raise HTTPException(503, "Tonight in Nicosia is not enabled on this server")
    return t


def _parse_at(text: str | None) -> datetime | None:
    """Parse ?at= as epoch milliseconds or ISO-8601 local/offset datetime, or None for live now."""
    if not text or not text.strip():
        return None
    s = text.strip()
    try:
        if s.lstrip("-").isdigit():
            return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc).astimezone(TZ)
        dt = datetime.fromisoformat(s)
        return (dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt).astimezone(TZ)
    except (ValueError, OSError, OverflowError):
        raise HTTPException(400, "at must be epoch ms or an ISO date/time like 2026-11-27T21:00") from None


@router.get("/api/tonight")
def get_tonight(
    request: Request,
    at: str | None = AT,
    lat: float | None = Query(None, ge=34.5, le=35.8),
    lon: float | None = Query(None, ge=32.2, le=34.7),
    origin_label: str | None = Query(None, max_length=80),
    category: str | None = Query(None, max_length=40),
    radius_m: float | None = Query(None, ge=50, le=10000),
    walk: bool = Query(True),
    open_limit: int = Query(24, ge=1, le=60),
) -> dict:
    """Duty pharmacies, VisitCyprus events, and currently open OSM places for tonight or ?at=."""
    t = tonight_of(request)
    when = _parse_at(at)
    near = (lat, lon) if lat is not None and lon is not None else None
    return t.snapshot(
        at=when,
        near=near,
        category=category or None,
        radius_m=radius_m,
        open_limit=open_limit,
        allow_network=(when is None),
        include_walking=walk,
        origin_label=origin_label or None,
    )


@router.get("/api/tonight/walk")
def get_walk(
    request: Request,
    id: str = Query(..., max_length=80),
    from_lat: float | None = Query(None, ge=34.5, le=35.8),
    from_lon: float | None = Query(None, ge=32.2, le=34.7),
    from_label: str | None = Query(None, max_length=80),
) -> dict:
    """Walking time, distance, and Valhalla directions text to a duty pharmacy, event, or place."""
    t = tonight_of(request)
    origin = (
        (from_lat, from_lon)
        if from_lat is not None and from_lon is not None
        else NICOSIA_CENTER
    )
    label = from_label or ("your location" if from_lat is not None and from_lon is not None else "Nicosia centre")

    target = None
    low = id.strip().lower()
    if low.startswith("duty-"):
        target = t.get_pharmacy(id, allow_network=False)
    elif low.startswith("event-"):
        target = t.get_event(id, allow_network=False)
    elif getattr(t, "places", None) is not None and t.places.ready:
        osm = t.places.by_id(id)
        if osm is not None:
            target = {
                "id": osm["id"],
                "name": osm.get("name") or id,
                "lat": osm["lat"],
                "lon": osm["lon"],
                "precision": "exact",
                "precision_label": "OpenStreetMap place",
                "directions": osm.get("street"),
            }

    if target is None:
        raise HTTPException(404, f"no tonight destination {id!r}")

    if target.get("lat") is None or target.get("lon") is None:
        return {
            "id": target["id"],
            "name": target.get("name") or id,
            "placed": False,
            "precision": target.get("precision") or "unplaced",
            "precision_label": target.get("precision_label") or "Not placed on map",
            "landmark_directions": target.get("directions"),
            "duration_min": None,
            "distance_m": None,
            "distance_km": None,
            "directions": [],
            "from_label": label,
            "source": None,
        }

    w = t.walk_to(origin[0], origin[1], target["lat"], target["lon"], from_label=label)
    return {
        "id": target["id"],
        "name": target.get("name") or id,
        "placed": True,
        "precision": target.get("precision") or "exact",
        "precision_label": target.get("precision_label"),
        "landmark_directions": target.get("directions"),
        **w,
    }

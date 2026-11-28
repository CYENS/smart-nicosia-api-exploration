"""Urban fabric, summer morning ground heat, population, 15-min walkability, and CyNAP street slopes API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from chatbot.routes import agent_of

router = APIRouter()
VALID_METRICS = {"heat_exposure", "surface_heat", "people", "access_15min", "access_step_free"}


def _fabric(request: Request):
    fab = getattr(agent_of(request), "fabric", None)
    if fab is None:
        raise HTTPException(503, "Urban fabric service is not initialized.")
    return fab


@router.get("/api/fabric/summary")
def fabric_summary(request: Request) -> dict:
    """Summary counts, ranges, and caveats for urban fabric, heat, population, slopes, and 15-min access."""
    return _fabric(request).summary()


@router.get("/api/fabric/heat")
def fabric_heat(request: Request) -> dict:
    """Landsat summer morning (~10:30 local) ground surface temperature grid over Greater Nicosia."""
    return _fabric(request).heat_grid_payload()


@router.get("/api/fabric/hexagons")
def fabric_hexagons(
    request: Request,
    metric: str = Query("heat_exposure", description="heat_exposure, surface_heat, people, access_15min, or access_step_free"),
    min_pop: int = Query(0, ge=0, le=50000),
    limit: int | None = Query(None, ge=1, le=1000),
) -> dict:
    """Greater Nicosia H3 resolution-8 hexagons colored by the chosen urban fabric metric."""
    if metric not in VALID_METRICS:
        raise HTTPException(400, f"metric must be one of {', '.join(sorted(VALID_METRICS))}")
    return _fabric(request).hexagons_geojson(metric=metric, min_pop=min_pop, limit=limit)


@router.get("/api/fabric/gradients")
def fabric_gradients(
    request: Request,
    min_slope: float = Query(0.0, ge=0.0, le=50.0),
) -> dict:
    """CyNAP road gradient points (100 m spacing) clipped to Greater Nicosia."""
    return _fabric(request).gradients_geojson(min_slope=min_slope, refresh_if_stale=True)


@router.get("/api/fabric/spot")
def fabric_spot(
    request: Request,
    lat: float = Query(..., ge=34.5, le=35.8),
    lon: float = Query(..., ge=32.2, le=34.7),
) -> dict:
    """Urban fabric, morning ground heat, population, slopes, and 15-min access at a point."""
    fab = _fabric(request)
    return {**fab.spot_fabric(lat, lon), "access": fab.access_at(lat, lon)}

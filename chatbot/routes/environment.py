"""The environment twin's endpoints: the air, noise and temperature surfaces and the wind at
the clock's moment, the rain radar's frames, the sun hours of a square or street on a date, and
which ground is in shade at a moment."""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from chatbot.environment.service import SOURCES, TZ
from chatbot.environment.surfaces import METRICS
from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment
from chatbot.web import WebError

router = APIRouter()
RADIUS = Query(150, ge=30, le=400, description="Half the side of the square, in metres.")


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, ValueError):
        return HTTPException(400, str(e))
    return HTTPException(502, f"a data source didn't answer: {e}")


@router.get("/api/environment/surface")
def surface(request: Request, metric: str = Query("pm2_5"), at: str | None = AT) -> dict:
    """A metric over Greater Nicosia at a moment: a grid of estimates with how sure each one
    is, the stations it comes from, and the bands for the legend."""
    if metric not in METRICS:
        raise HTTPException(400, f"metric is one of {', '.join(METRICS)}")
    when = moment(request, at)
    try:
        return agent_of(request).environment.surface(metric, when)
    except (WebError, ValueError) as e:
        raise _fail(e) from None


@router.get("/api/environment/wind")
def wind(request: Request, at: str | None = AT) -> dict:
    """The modelled wind on its lattice at the hour of a moment, for the particles."""
    when = moment(request, at)
    try:
        return agent_of(request).environment.wind(when)
    except (WebError, ValueError) as e:
        raise _fail(e) from None


@router.get("/api/environment/radar")
def radar(request: Request) -> dict:
    """The rain radar's frames of the last two hours, with their tile addresses."""
    return agent_of(request).environment.radar()


@router.get("/api/environment/stations")
def stations(request: Request, at: str | None = AT) -> dict:
    """The measuring stations (the Met Department's weather stations and the national air
    network's reference stations) with their readings at a moment, from what the twin has read
    and recorded (never asking the services), the rain of the last hour at each Met station, and
    where it comes from."""
    when = moment(request, at)
    m = agent_of(request).environment.measured
    rows = m.readings_at(when)
    rain = m.rain_last_hour(when)
    for r in rows:
        if r["id"] in rain:
            r["rain_last_hour_mm"] = rain[r["id"]]
    return {"at": when.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if when else None, "stations": rows,
            "feeds": {"met": _feed(m.met_status), "eea": _feed(m.eea_status)},
            "sources": [SOURCES["met_stations"], SOURCES["eea_air"]]}


def _feed(st) -> dict:
    return {"read_at": st.read_at and datetime.fromtimestamp(st.read_at, tz=timezone.utc).astimezone(TZ).isoformat(timespec="minutes"),
            "ok": st.error is None and st.ok_at is not None, "error": st.error}


@router.get("/api/environment/compare")
def compare(request: Request, sensor: str | None = Query(None, max_length=60),
            station: str | None = Query(None, max_length=40), at: str | None = AT,
            days: int = Query(7, ge=1, le=28)) -> dict:
    """How the city's air sensors compare with the reference stations over the past days: one
    sensor's (sensor=), those compared with one station (station=), or all."""
    when = moment(request, at)
    env = agent_of(request).environment
    rows = env.measured.compare(env.city_sensors(), days=days, end=when)
    if sensor:
        rows = [r for r in rows if r["sensor_id"].lower() == sensor.lower()]
    if station:
        rows = [r for r in rows if r["reference_id"].lower() == station.lower()]
    return {"days": days, "rows": rows}


def _day(text: str | None, request: Request) -> date:
    if not text:
        return agent_of(request).city.now().astimezone(TZ).date()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, "date is YYYY-MM-DD") from None


@router.get("/api/environment/sun-hours")
def sun_hours(request: Request, lat: float = Query(..., ge=34.5, le=35.8),
              lon: float = Query(..., ge=32.2, le=34.7), radius_m: float = RADIUS,
              day: str | None = Query(None, alias="date")) -> dict:
    """Hours of direct sun per cell of open ground over a day, around a point."""
    try:
        return agent_of(request).environment.sun_hours(lat, lon, radius_m, _day(day, request))
    except (WebError, ValueError) as e:
        raise _fail(e) from None


@router.get("/api/environment/shade")
def shade(request: Request, lat: float = Query(..., ge=34.5, le=35.8),
          lon: float = Query(..., ge=32.2, le=34.7), radius_m: float = RADIUS,
          at: str | None = Query(None, max_length=40)) -> dict:
    """Which open ground is in shade at a moment (any moment, the future too: the sun is
    known), around a point."""
    agent = agent_of(request)
    when = _any_moment(at, agent.city.now())
    try:
        return agent.environment.shade_grid(lat, lon, radius_m, when)
    except (WebError, ValueError) as e:
        raise _fail(e) from None


def _any_moment(text: str | None, now: datetime) -> datetime:
    """A moment for the sun, which is known any day: epoch ms or local time."""
    if not text:
        return now
    try:
        if text.strip().lstrip("-").isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        t = datetime.fromisoformat(text)
        return t.replace(tzinfo=TZ) if t.tzinfo is None else t
    except ValueError:
        raise HTTPException(400, "at is epoch ms or local time like 2026-07-15T15:00") from None

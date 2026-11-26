"""Where the data comes from, for the page's data sources panel: every source of
chatbot/sources.py with how fresh it is now. Freshness is read from what the twin already
fetched; this route never asks a source anything."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chatbot.overview import last_read, moments_of
from chatbot.routes import agent_of
from chatbot.settings import TIMEZONE
from chatbot.sources import GROUPS, SOURCES

log = logging.getLogger(__name__)
router = APIRouter()

STALE_AFTER_S = 20 * 60  # a feed the page polls that hasn't been read for this long is idle
DISTRICTS = {"nicosia": "Nicosia", "limassol": "Limassol", "larnaca": "Larnaca", "famagusta": "Famagusta"}


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")


def status(state: str | None, at: float | None = None, note: str | None = None, **more) -> dict:
    """state: live (read and fine), stale (the last read failed, an older copy stands in),
    unavailable (nothing to show), waiting (not read yet since the server started)."""
    return {"state": state, "as_of": _iso(at), "note": note, **more}


def _cached(key: str) -> dict:
    hit = last_read(key)
    if hit is None:
        return status("waiting", note="read when a page asks for it")
    at, ok = hit
    return status("live" if ok else "unavailable", at, None if ok else "the last read failed")


def _bus_live(agent) -> dict:
    mob = getattr(agent, "mobility", None)
    snap = mob.fleet.last if mob is not None else None
    if snap is None:
        return status("waiting", note="read while the buses are on the map")
    return status(snap.status, snap.fetched_at, snap.error if snap.status != "live" else
                  f"{len(snap.vehicles)} buses in and around Nicosia")


def _timetable(agent) -> dict:
    mob = getattr(agent, "mobility", None)
    if mob is None:
        return status(None)
    tt = mob.timetable()
    if tt is None:
        return status("unavailable" if mob.source.error else "waiting", note=mob.source.error)
    operators = [f["operator"] for f in tt.feeds if f.get("operator")]
    return status("stale" if mob.source.error else "live", mob.source.loaded_at,
                  mob.source.error or (f"{len(operators)} operators" if operators else None))


def _roads(agent) -> dict:
    mob = getattr(agent, "mobility", None)
    hit = mob.roads.last_live() if mob is not None else None
    if hit is None:
        return status("waiting", note="read while road speeds are on the map")
    at, err = hit
    return status("stale" if err else "live", at, err)


def _registers(agent) -> dict:
    reg = agent.registers
    parts = []
    for key, s in (reg.sources or {}).items():
        copy = s.get("copy")
        parts.append({
            "name": DISTRICTS.get(key, key.title()),
            "count": s.get("count"),
            "state": "live" if copy == "live" else "stale",
            "as_of": s.get("as_of"),
            "note": None if copy == "live" else "a saved copy: the live register couldn't be read",
        })
    if not parts:
        return status("waiting" if not reg.ready else None)
    every_live = all(p["state"] == "live" for p in parts)
    return status("live" if every_live else "stale", reg.fetched_at,
                  None if every_live else "some districts show a saved copy", parts=parts)


def _memory(agent) -> dict:
    if agent.memory is None:
        return status("unavailable", note="the twin's memory is off")
    earliest = (moments_of(agent).track(31) or {}).get("earliest")
    return status("live", note=None, holds_from=earliest)


PROBES: dict[str, Callable] = {
    "smart_nicosia": lambda a: _cached("city"),
    "open_meteo": lambda a: _cached("weather"),
    "bus_live": _bus_live,
    "bus_timetable": _timetable,
    "road_speeds": _roads,
    "registers": _registers,
    "twin_memory": _memory,
}


@router.get("/api/sources")
def data_sources(request: Request) -> JSONResponse:
    """Every source with its publisher, licence and link, and how fresh it is now."""
    agent = agent_of(request)
    out = []
    for s in SOURCES:
        probe = PROBES.get(s.id)
        try:
            st = probe(agent) if probe else None
        except Exception as e:  # noqa: BLE001 - one source's state never hides the list
            log.info("no status for %s: %s", s.id, e)
            st = None
        out.append({**s.public(), "status": st})
    return JSONResponse({"groups": list(GROUPS), "sources": out}, headers={"Cache-Control": "no-store"})

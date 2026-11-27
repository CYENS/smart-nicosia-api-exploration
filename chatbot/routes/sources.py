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

from chatbot.mobility.roads import is_stalled, since_words
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
    stalled (it answers, but with nothing new: its newest measurement is old), unavailable
    (nothing to show), waiting (not read yet since the server started)."""
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
    tt = mob.source.current  # never starts a load
    if tt is None:
        return status("unavailable" if mob.source.error else "waiting", note=mob.source.error)
    operators = [f["operator"] for f in tt.feeds if f.get("operator")]
    return status("stale" if mob.source.error else "live", mob.source.loaded_at,
                  mob.source.error or (f"{len(operators)} operators" if operators else None))


def _roads(agent) -> dict:
    """Judged by the newest measurement in the feed, not by whether it answered: a feed that
    answers with an old measurement has stalled."""
    mob = getattr(agent, "mobility", None)
    h = mob.roads.health() if mob is not None else None
    if h is None:
        return status("waiting", note="read while road speeds are on the map")
    newest, now = h["newest"], mob.roads.now()
    measured = {"measured_at": newest.astimezone(ZoneInfo(TIMEZONE)).isoformat(timespec="minutes")} if newest else {}
    fresh = newest is not None and not is_stalled(newest, now)
    if h["error"]:
        # Not answering: an older copy stands in only while its measurements still count.
        if fresh:
            return status("stale", h["read_ok_at"], h["error"], **measured)
        return status("unavailable", h["read_at"], h["error"], **measured)
    if newest is None:
        return status("unavailable", h["read_at"], "the feed answered with no measurements")
    if not fresh:
        return status("stalled", h["read_at"], f"the detectors have sent nothing new since "
                      f"{since_words(newest, now)}; the feed still answers with that measurement",
                      **measured)
    return status("live", h["read_at"], f"newest measurement {since_words(newest, now)}", **measured)


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


def _overture(agent) -> dict:
    """The release the twin's copy was cut from, and whether the last look for a newer one
    worked."""
    ov = getattr(getattr(agent, "places", None), "overture", None)
    if ov is None:
        return status(None)
    if not ov.ready:
        return status("unavailable" if ov.error else "waiting",
                      note=ov.error or "the first copy is being cut from the latest release")
    # "Read" is when the twin last looked at Overture's bucket; the release has its own date.
    date = ov.release_date
    note = (f"release {ov.release}" + (f" of {date.day} {date:%b %Y}" if date else "")
            + f", {len(ov.all()):,} places")
    if ov.error:
        return status("stale", ov.checked_at, f"{note}; a newer release couldn't be read "
                      f"({ov.error})", release=ov.release)
    return status("live", ov.checked_at, note, release=ov.release)


def _radar(agent) -> dict:
    env = getattr(agent, "environment", None)
    hit = env.radar_status() if env is not None else None
    if hit is None:
        return status("waiting", note="read while the rain radar is on the map")
    at, out = hit
    if not out.get("known"):
        return status("unavailable", at, out.get("reason"))
    newest = out["frames"][-1]["time"] / 1000
    return status("live", at, f"newest frame {_iso(newest)[11:16]}" if newest else None)


def _grid(agent) -> dict:
    grid = getattr(agent, "grid", None)
    if grid is None:
        return status(None)
    h = grid.health()
    if h["fetched_at"] is None:
        return status("unavailable" if h["error"] else "waiting",
                      note=h["error"] or "read when the Outside card or a question asks for it")
    newest = h["latest"].astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M") if h["latest"] else None
    note = f"newest quarter hour {newest}" + (f", {h['latest_mw']:,.0f} MW" if h["latest_mw"] else "") if newest else None
    if h["layout_changed"]:
        return status("stale", h["fetched_at"].timestamp(), h["error"], layout_changed=True)
    if h["error"]:
        return status("stale", h["fetched_at"].timestamp(), f"{note}; the last read failed ({h['error']})")
    return status("live", h["fetched_at"].timestamp(), note)


PROBES: dict[str, Callable] = {
    "smart_nicosia": lambda a: _cached("city"),
    "open_meteo": lambda a: _cached("weather"),
    "bus_live": _bus_live,
    "bus_timetable": _timetable,
    "road_speeds": _roads,
    "registers": _registers,
    "twin_memory": _memory,
    "overture": _overture,
    "rainviewer": _radar,
    "tso_grid": _grid,
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

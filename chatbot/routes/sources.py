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


def _road_reports(source: str) -> Callable:
    """A road report feed, judged by the times inside what it sent (its publication time, its
    newest report), not by its HTTP status: a feed can answer 200 for weeks without news."""
    def probe(agent) -> dict:
        mob = getattr(agent, "mobility", None)
        rep = getattr(mob, "reports", None)
        if rep is None or source not in rep.state:
            return status(None)
        st = rep.state[source]
        if st.parsed is None:
            if st.error:
                return status("unavailable", st.tried_at, st.error)
            return status("waiting", note="read while road reports are on the map or the watch listens")
        problem = st.fresh_problem(source, rep.now())
        n = len(st.parsed.reports)
        newest = st.parsed.newest
        note = f"{n} in Greater Nicosia" + (f", newest {_iso(newest.timestamp())[11:16]}" if newest and source != "traffic_events" else "")
        if st.error:
            return status("stale", st.ok_at, f"the last read failed ({st.error}); {note}")
        if problem:
            return status("stale", st.ok_at, f"{problem}; {note}")
        return status("live", st.ok_at, note)
    return probe


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


def _street_feed(*keys: str) -> Callable:
    """A street inventory's state: when the publisher last changed it, from the catalogue."""
    def probe(agent) -> dict:
        st = getattr(agent, "streets", None)
        if st is None:
            return status(None)
        invs = [inv for k in keys if (inv := st.feeds.peek(k)) is not None]
        if not invs:
            return status("waiting", note="read when the server starts, then once a day")
        bad = [i for i in invs if i.error]
        updated = max((i.updated for i in invs if i.updated), default=None)
        count = sum(len(i.records) for i in invs[:1])
        note = (f"inventory updated {updated}, " if updated else "") + f"{count} in Greater Nicosia"
        if bad:
            return status("stale" if any(i.records for i in invs) else "unavailable", max(i.read_at for i in invs),
                          bad[0].error, updated=updated)
        return status("live", max(i.read_at for i in invs), note, updated=updated)
    return probe


def _inventories(*kinds: str):
    """Park, charge and hire: when the twin read each inventory, and how old it is."""
    def probe(agent) -> dict:
        from chatbot.mobility.facilities import PLURALS
        from chatbot.mobility.facility_kinds import day, facilities_of

        fac = facilities_of(agent.entities)
        if fac is None:
            return status(None)
        invs = [(k, fac.cached(k)) for k in kinds]
        read = [(k, i) for k, i in invs if i is not None]
        if not read:
            return status("waiting", note="read when the layer is on or the assistant asks")
        parts = []
        for k, i in read:
            when = f"last changed {day(i.updated)}" if i.updated else f"republished {day(i.published)}"
            parts.append(f"{i.public()['in_nicosia']:,} {PLURALS[k]} in Greater Nicosia, {when}")
        errors = [i.error for _, i in read if i.error]
        return status("stale" if errors else "live", max(i.read_at for _, i in read),
                      "; ".join(errors[:1] + parts))
    return probe


def _nextbike_live(agent) -> dict:
    from chatbot.mobility.facility_kinds import facilities_of

    fac = facilities_of(agent.entities)
    b = fac.last_bikes() if fac is not None else None
    if b is None:
        return status("waiting", note="read while bike stations are on the map")
    if b.status == "unavailable":
        return status("unavailable", b.fetched_at, b.error)
    total = sum(s["bikes"] for s in b.stations.values())
    return status(b.status, b.feed_time or b.fetched_at,
                  b.error if b.status == "stale" else f"{total:,} bikes at {len(b.stations):,} stations in Cyprus")


def _station_feed(agent, which: str) -> dict:
    """The Met file or the EEA files: when last read, whether it worked, the newest reading."""
    env = getattr(agent, "environment", None)
    st = env.measured.health(which) if env is not None else None
    if st is None or st.read_at is None:
        return status("waiting", note="read in the background every few minutes")
    newest = (datetime.fromtimestamp(st.newest_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(TIMEZONE))
              .strftime("%H:%M") if st.newest_ms else None)
    what = "newest reading" if which == "met" else "newest hour from"
    if st.error and st.ok_at is None:
        return status("unavailable", st.read_at, st.error)
    note = f"{what} {newest}" if newest else None
    if st.error:
        return status("stale", st.ok_at, f"{note + '; ' if note else ''}the last read failed: {st.error}")
    return status("live", st.ok_at or st.read_at, note)


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


def _hazards(feed: str) -> Callable:
    """Earthquakes, warnings or fire danger, as the hazard feeds' last read left them."""
    def probe(agent) -> dict:
        hz = getattr(agent, "hazards", None)
        if hz is None:
            return status(None)
        state, at, note = hz.feed_state(feed)
        return status(state, at, note)
    return probe


def _duty_pharmacies(agent) -> dict:
    t = getattr(agent, "tonight", None)
    if t is None:
        return status(None)
    r = t.roster
    if not r.rows:
        return status("unavailable" if r.error else "waiting",
                      note=r.error or "loading Nicosia quarterly duty roster")
    note = f"{len(r.rows)} duty shifts" + (f"; refresh failed ({r.error})" if r.error else "")
    return status("stale" if r.error else "live", r.fetched_at or None, note)


def _dls_addresses(agent) -> dict:
    t = getattr(agent, "tonight", None)
    if t is None:
        return status(None)
    c = t.dls_addr
    if not c._streets and not c.last_read_at:
        return status("waiting", note="queried per street when matching duty addresses")
    note = f"{len(c._streets)} street lookups cached"
    if c.last_error:
        return status("stale", c.last_read_at or None, f"{note}; {c.last_error}")
    return status("live", c.last_read_at or None, note)


def _visit_cyprus(agent) -> dict:
    t = getattr(agent, "tonight", None)
    if t is None:
        return status(None)
    cal = t.calendar
    if not cal.fetched_at and not cal.error and not cal.events:
        return status("waiting", note="loading Visit Cyprus Nicosia events calendar")
    if cal.error and not cal.events:
        return status("unavailable", note=cal.error)
    note = f"{len(cal.events)} upcoming Nicosia events"
    if cal.error:
        return status("stale", cal.fetched_at or None, f"{note}; {cal.error}")
    return status("live", cal.fetched_at or None, note)


def _adsb(agent) -> dict:
    sky = getattr(agent, "sky", None)
    hit = sky.aircraft.health() if sky is not None else None
    if hit is None:
        return status("waiting", note="read while the aircraft layer is on the map")
    if hit.get("error"):
        at = hit.get("read_ok_at") or hit.get("read_at")
        return status("stale" if hit.get("read_ok_at") else "unavailable", at, hit["error"])
    return status("live", hit.get("read_ok_at"),
                  f"{hit['count']} aircraft within 40 nm (partial community coverage)")


def _iss(agent) -> dict:
    sky = getattr(agent, "sky", None)
    hit = sky.iss.health() if sky is not None else None
    if hit is None:
        return status("waiting", note="read for the Outside card and City Clock ISS marks")
    if hit.get("error"):
        at = hit.get("read_ok_at") or hit.get("read_at")
        return status("stale" if hit.get("read_ok_at") else "unavailable", at, hit["error"])
    epoch = hit.get("tle_epoch")
    return status("live", hit.get("read_ok_at"), f"TLE epoch {epoch}" if epoch else None)


def _cynap_its(agent) -> dict:
    sky = getattr(agent, "sky", None)
    hit = sky.its.health() if sky is not None else None
    if hit is None:
        return status("waiting", note="read when PWD traffic detectors and CCTV are on the map")
    if hit.get("error"):
        has_sites = bool(hit.get("cctv") or hit.get("detectors"))
        at = hit.get("read_ok_at") if has_sites else hit.get("read_at")
        return status("stale" if has_sites else "unavailable", at, hit["error"])
    return status("live", hit.get("read_ok_at"),
                  f"{hit['cctv']} CCTV and {hit['detectors']} detector sites in Nicosia")


def _swarco(agent) -> dict:
    sky = getattr(agent, "sky", None)
    hit = sky.its.swarco_health() if sky is not None else None
    if hit is None:
        return status("waiting", note="checked when the PWD ITS inventory is read")
    if hit.get("error"):
        return status("unavailable", hit.get("read_at"), hit["error"])
    if hit.get("has_values"):
        return status("live", hit.get("read_at"), f"{hit['measured_count']} live flow values arriving")
    return status("stalled", hit.get("read_at"),
                  f"{hit.get('site_refs', 0)} site references published, no measured values yet")


PROBES: dict[str, Callable] = {
    "met_stations": lambda a: _station_feed(a, "met"),
    "eea_air": lambda a: _station_feed(a, "eea"),
    "smart_nicosia": lambda a: _cached("city"),
    "open_meteo": lambda a: _cached("weather"),
    "bus_live": _bus_live,
    "bus_timetable": _timetable,
    "road_speeds": _roads,
    "waze_alerts": _road_reports("waze_alerts"),
    "traffic_events": _road_reports("traffic_events"),
    "waze_traffic": _road_reports("waze_traffic"),
    "cynap_its": _cynap_its,
    "swarco_traffic": _swarco,
    "adsb_lol": _adsb,
    "celestrak_iss": _iss,
    "registers": _registers,
    "twin_memory": _memory,
    "overture": _overture,
    "rainviewer": _radar,
    # The street rules' inventories (chatbot/streets).
    "speed_limits": _street_feed("speed_limits"),
    "slow_zones": _street_feed("zones", "zone_entrances"),
    "traffic_signals": _street_feed("signals"),
    "pedestrian_crossings": _street_feed("crossings"),
    "cycleways": _street_feed("cycleways"),
    "bus_lanes": _street_feed("bus_lanes"),
    "enforcement_cameras": _street_feed("cameras"),
    "nap_public_works": _inventories("car_park", "park_and_ride", "bike_rack"),
    "nap_chargers": _inventories("charger"),
    "nap_nextbike": _inventories("bike_station"),
    "nap_bolt": _inventories("scooter_zone"),
    "nextbike_live": _nextbike_live,
    "tso_grid": _grid,
    "emsc": _hazards("earthquakes"),
    "meteoalarm": _hazards("warnings"),
    "effis": _hazards("fire"),
    "duty_pharmacies": _duty_pharmacies,
    "dls_addresses": _dls_addresses,
    "visit_cyprus": _visit_cyprus,
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

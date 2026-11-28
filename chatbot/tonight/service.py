"""Tonight in Nicosia: duty pharmacies, city events, open-now OSM places, and walking times."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chatbot.geo import haversine_m
from chatbot.settings import NICOSIA_CENTER, TIMEZONE, Settings
from chatbot.tonight import store as db_store
from chatbot.tonight.events import EventsCalendar, split_events_for_moment
from chatbot.tonight.geocode import PRECISION_LABELS, DLSAddressClient, TonightGeocoder
from chatbot.tonight.hours import evaluate_opening_hours
from chatbot.tonight.pharmacies import DutyRosterStore, duty_date_for
from chatbot.web import WebError

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

# Practical everyday places when showing "open now" without an explicit category filter.
EVENING_CATEGORIES = {
    "pharmacy", "restaurant", "cafe", "bar", "fast_food", "ice_cream",
    "supermarket", "theatre", "hospital", "hotel", "museum", "gallery",
}
WALK_SPEED_M_PER_MIN = 75.0  # ~4.5 km/h fallback when Valhalla is offline
WALK_CACHE_MAX = 128
WALK_TTL_S = 600


def _est_walk_min(dist_m: float) -> int:
    return max(1, round(dist_m / WALK_SPEED_M_PER_MIN))


class Tonight:
    """Shared coordinator for duty pharmacies, events calendar, and open-now places."""

    def __init__(
        self,
        settings: Settings,
        places,
        memory=None,
        router=None,
        *,
        overture=None,
        roster: DutyRosterStore | None = None,
        calendar: EventsCalendar | None = None,
        geocoder: TonightGeocoder | None = None,
        cache_dir: Path | None = None,
        now=None,
    ):
        self.s = settings
        self.places = places
        self.memory = memory
        self.router = router
        self.overture = overture or getattr(places, "overture", None)
        self._now = now or (lambda: datetime.now(TZ))
        self.roster = roster or DutyRosterStore(settings, cache_dir=cache_dir)
        self.calendar = calendar or EventsCalendar(settings, cache_dir=cache_dir)
        self.dls_addr = DLSAddressClient(settings, cache_dir=cache_dir)
        self.geocoder = geocoder or TonightGeocoder(
            settings, places, overture=self.overture, dls_addr=self.dls_addr, cache_dir=cache_dir
        )
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._last_roster_sync: float = 0.0
        self._last_events_sync: float = 0.0
        self._walk_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()

    def now(self) -> datetime:
        return self._now().astimezone(TZ)

    def warm_up(self) -> None:
        """Best-effort startup warm-up: load tonight's duty pharmacies, events, and sync DuckDB."""
        try:
            self.ensure_roster(allow_geocode=True)
        except Exception as e:  # noqa: BLE001 - best effort on startup
            log.warning("tonight warm_up roster failed: %s", e)
        try:
            self.ensure_events(allow_geocode=True)
        except Exception as e:  # noqa: BLE001
            log.warning("tonight warm_up events failed: %s", e)

    def ensure_roster(self, *, allow_network: bool = True, allow_geocode: bool = True) -> list[dict]:
        """Refresh duty roster if stale, geocode today's/near-term nights, and sync to DuckDB."""
        rows = self.roster.refresh() if allow_network else self.roster.rows
        if not rows:
            return []
        today = duty_date_for(self.now())
        # Geocode today's, yesterday's, and tomorrow's duty pharmacies immediately (max ~18 places);
        # earlier/later rows keep their existing coordinates until queried or synced.
        near_dates = {
            (today + timedelta(days=delta)).isoformat()
            for delta in (-1, 0, 1, 2)
        }
        enriched: list[dict] = []
        changed = False
        for r in rows:
            item = dict(r)
            if item.get("lat") is None and item.get("duty_date") in near_dates:
                geo = self.geocoder.locate_pharmacy(item, allow_network=allow_geocode)
                item.update({
                    "lat": geo.get("lat"),
                    "lon": geo.get("lon"),
                    "precision": geo["precision"],
                    "precision_label": geo["precision_label"],
                    "located_by": geo["located_by"],
                    **({"osm_id": geo["osm_id"]} if geo.get("osm_id") else {}),
                    **({"overture_id": geo["overture_id"]} if geo.get("overture_id") else {}),
                })
                changed = True
            elif item.get("lat") is not None and not item.get("precision_label"):
                item["precision_label"] = PRECISION_LABELS.get(item.get("precision") or "exact", "Location")
            enriched.append(item)

        if self.roster.fetched_at > self._last_roster_sync or changed:
            with self._sync_lock:
                # Merge geocoded coords back into roster rows in memory and persist to DuckDB
                by_id = {r["id"]: r for r in enriched}
                self.roster.rows = [by_id.get(r["id"], r) for r in self.roster.rows]
                if db_store.sync_pharmacies(self.memory, self.roster.rows, self.roster.fetched_at):
                    self._last_roster_sync = max(self._last_roster_sync, self.roster.fetched_at or time.time())
        return enriched

    def ensure_events(self, *, allow_network: bool = True, allow_geocode: bool = True) -> list[dict]:
        """Refresh VisitCyprus events if stale, place venues, and sync to DuckDB."""
        events = self.calendar.refresh(self.now()) if allow_network else self.calendar.events
        if not events:
            return []
        enriched: list[dict] = []
        changed = False
        for ev in events:
            item = dict(ev)
            if item.get("lat") is None:
                if item.get("venue_lat") is not None and item.get("venue_lon") is not None:
                    item.update({
                        "lat": round(float(item["venue_lat"]), 6),
                        "lon": round(float(item["venue_lon"]), 6),
                        "precision": "exact",
                        "precision_label": PRECISION_LABELS["exact"],
                        "located_by": "Visit Cyprus venue coordinates",
                    })
                else:
                    geo = self.geocoder.locate_venue(
                        item.get("venue_name") or "",
                        item.get("venue_address"),
                        item.get("municipality"),
                        allow_network=allow_geocode,
                    )
                    item.update({
                        "lat": geo.get("lat"),
                        "lon": geo.get("lon"),
                        "precision": geo["precision"],
                        "precision_label": geo["precision_label"],
                        "located_by": geo["located_by"],
                        **({"osm_id": geo["osm_id"]} if geo.get("osm_id") else {}),
                        **({"overture_id": geo["overture_id"]} if geo.get("overture_id") else {}),
                    })
                changed = True
            enriched.append(item)
        if self.calendar.fetched_at > self._last_events_sync or changed:
            with self._sync_lock:
                self.calendar.events = enriched
                if db_store.sync_events(self.memory, enriched, self.calendar.fetched_at):
                    self._last_events_sync = max(self._last_events_sync, self.calendar.fetched_at or time.time())
        return enriched

    def _enrich_pharmacy(self, row: dict, origin: tuple[float, float] | None, *, allow_geocode: bool = True) -> dict:
        out = dict(row)
        if out.get("lat") is None:
            geo = self.geocoder.locate_pharmacy(out, allow_network=allow_geocode)
            out.update({
                "lat": geo.get("lat"),
                "lon": geo.get("lon"),
                "precision": geo["precision"],
                "precision_label": geo["precision_label"],
                "located_by": geo["located_by"],
                **({"osm_id": geo["osm_id"]} if geo.get("osm_id") else {}),
                **({"overture_id": geo["overture_id"]} if geo.get("overture_id") else {}),
            })
        elif not out.get("precision_label"):
            out["precision_label"] = PRECISION_LABELS.get(out.get("precision") or "exact", "Exact location")
        if origin and out.get("lat") is not None and out.get("lon") is not None:
            d = round(haversine_m(origin[0], origin[1], out["lat"], out["lon"]))
            out["distance_m"] = d
            out["walk_min"] = _est_walk_min(d)
        surname = out.get("surname") or out.get("pharmacist") or "Duty pharmacy"
        first = out.get("first_name") or ""
        out.setdefault("name", f"Pharmacy {first} {surname}".strip() if first else f"Pharmacy {surname}".strip())
        return out

    def _enrich_event(self, ev: dict, origin: tuple[float, float] | None, *, allow_geocode: bool = True) -> dict:
        out = dict(ev)
        if out.get("lat") is None:
            geo = self.geocoder.locate_venue(
                out.get("venue_name") or "", out.get("venue_address"), out.get("municipality"),
                allow_network=allow_geocode,
            )
            out.update({
                "lat": geo.get("lat"),
                "lon": geo.get("lon"),
                "precision": geo["precision"],
                "precision_label": geo["precision_label"],
                "located_by": geo["located_by"],
            })
        elif not out.get("precision_label"):
            out["precision_label"] = PRECISION_LABELS.get(out.get("precision") or "exact", "Exact location")
        if origin and out.get("lat") is not None and out.get("lon") is not None:
            d = round(haversine_m(origin[0], origin[1], out["lat"], out["lon"]))
            out["distance_m"] = d
            out["walk_min"] = _est_walk_min(d)
        return out

    def open_now_places(
        self,
        at: datetime,
        origin: tuple[float, float] | None = None,
        *,
        category: str | None = None,
        radius_m: float | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """OSM places whose `opening_hours` tag says they are open at `at`."""
        if self.places is None or not self.places.ready:
            return []
        ref = origin or NICOSIA_CENTER
        found: list[dict] = []
        for p in self.places.all():
            oh = p.get("opening_hours")
            if not oh or not p.get("name"):
                continue
            cat = p.get("category") or ""
            if category and cat != category:
                continue
            if not category and cat not in EVENING_CATEGORIES:
                continue
            ev = evaluate_opening_hours(oh, at)
            if ev.is_open is not True:
                continue
            dist = round(haversine_m(ref[0], ref[1], p["lat"], p["lon"]))
            if radius_m and dist > radius_m:
                continue
            found.append({
                "id": p["id"],
                "name": p["name"],
                "category": cat,
                "street": p.get("street"),
                "opening_hours": oh,
                "status_text": ev.detail,
                "until": ev.until,
                "always_open": ev.always_open,
                "lat": p["lat"],
                "lon": p["lon"],
                "distance_m": dist,
                "walk_min": _est_walk_min(dist),
                "phone": p.get("phone"),
                "website": p.get("website"),
            })
        # Prefer places with scheduled closing hours tonight ahead of 24/7 entries, then nearest.
        found.sort(key=lambda x: (bool(x["always_open"]), x["distance_m"]))
        return found[:limit]

    def walk_to(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        *,
        from_label: str = "your location",
    ) -> dict:
        """Compute walking time, distance, and step directions via Valhalla (or straight-line fallback)."""
        key = (round(from_lat, 4), round(from_lon, 4), round(to_lat, 5), round(to_lon, 5))
        with self._lock:
            hit = self._walk_cache.get(key)
            if hit and time.time() - hit[0] < WALK_TTL_S:
                self._walk_cache.move_to_end(key)
                return {**hit[1], "from_label": from_label}

        straight_m = round(haversine_m(from_lat, from_lon, to_lat, to_lon))
        result: dict | None = None
        if self.router is not None:
            try:
                route = self.router.route([(from_lat, from_lon), (to_lat, to_lon)], "walk", ["lit_streets"])
                leg = (route.get("legs") or [{}])[0]
                result = {
                    "distance_km": route["distance_km"],
                    "distance_m": round(route["distance_km"] * 1000),
                    "duration_min": max(1, round(route["duration_min"])),
                    "directions": leg.get("directions") or [],
                    "source": "valhalla",
                }
            except (WebError, OSError, ValueError, KeyError) as e:
                log.info("Valhalla walking route fallback (%s)", e)

        if result is None:
            result = {
                "distance_km": round(straight_m / 1000, 2),
                "distance_m": straight_m,
                "duration_min": _est_walk_min(straight_m),
                "directions": [],
                "source": "estimate",
            }
        with self._lock:
            self._walk_cache[key] = (time.time(), result)
            while len(self._walk_cache) > WALK_CACHE_MAX:
                self._walk_cache.popitem(last=False)
        return {**result, "from_label": from_label}

    def snapshot(
        self,
        at: datetime | None = None,
        near: tuple[float, float] | None = None,
        *,
        category: str | None = None,
        radius_m: float | None = None,
        open_limit: int = 24,
        allow_network: bool = True,
        include_walking: bool = False,
        origin_label: str | None = None,
    ) -> dict:
        """Everything happening on this evening/moment: duty pharmacies, events, and open places."""
        when = (at or self.now()).astimezone(TZ)
        is_past = at is not None
        d_date = duty_date_for(when)
        origin = near or NICOSIA_CENTER
        walk_from_label = origin_label or ("your location" if near else "Nicosia centre")

        if is_past:
            # Past moments read exclusively from the twin's DuckDB memory (never guess).
            raw_ph = db_store.read_pharmacies_for_date(self.memory, d_date)
            known = raw_ph is not None and len(raw_ph) > 0
            raw_ph = raw_ph or []
            ev_rows = db_store.read_events_range(
                self.memory,
                when - timedelta(days=1),
                when + timedelta(days=14),
            ) or []
            ph_status = "live" if known else "unavailable"
            ev_status = "live" if ev_rows else "unavailable"
            ph_error = None if known else "The twin's memory holds no duty pharmacy roster for that night."
        else:
            if allow_network:
                self.ensure_roster(allow_network=True, allow_geocode=True)
                self.ensure_events(allow_network=True, allow_geocode=True)
            raw_ph = self.roster.for_date(d_date)
            # If the memory holds rows for today while the live feed hasn't loaded, read memory.
            if not raw_ph and (mem_ph := db_store.read_pharmacies_for_date(self.memory, d_date)):
                raw_ph = mem_ph
            known = bool(raw_ph)
            ev_rows = self.calendar.events or (
                db_store.read_events_range(self.memory, when - timedelta(days=1), when + timedelta(days=14)) or []
            )
            ph_status = self.roster.status
            ev_status = self.calendar.status
            ph_error = self.roster.error

        pharmacies = [
            self._enrich_pharmacy(r, origin, allow_geocode=allow_network and not is_past)
            for r in raw_ph
        ]
        # Persist any newly geocoded coordinates back into DuckDB when live
        if not is_past and pharmacies:
            with self._sync_lock:
                by_id = {p["id"]: p for p in pharmacies}
                self.roster.rows = [by_id.get(r["id"], r) for r in self.roster.rows]
                db_store.sync_pharmacies(self.memory, pharmacies, self.roster.fetched_at or time.time())

        pharmacies.sort(key=lambda p: p.get("distance_m") if p.get("distance_m") is not None else 999_999)

        if include_walking:
            for p in pharmacies[:6]:
                if p.get("lat") is not None and p.get("lon") is not None:
                    w = self.walk_to(origin[0], origin[1], p["lat"], p["lon"], from_label=walk_from_label)
                    p["walk_min"] = w["duration_min"]
                    p["distance_m"] = w["distance_m"]
                    p["walking"] = w

        tonight_ev, upcoming_ev = split_events_for_moment(ev_rows, when)
        tonight_events = [self._enrich_event(e, origin, allow_geocode=allow_network and not is_past) for e in tonight_ev]
        upcoming_events = [self._enrich_event(e, origin, allow_geocode=allow_network and not is_past) for e in upcoming_ev[:8]]

        open_now = self.open_now_places(when, origin, category=category, radius_m=radius_m, limit=open_limit)

        overnight = when.hour < 8
        shift_note = (
            f"Overnight duty shift for {d_date.strftime('%a %d %b %Y')} (through 08:00)"
            if overnight
            else f"Duty shift for {d_date.strftime('%a %d %b %Y')} (through 08:00 next morning)"
        )

        return {
            "at": when.isoformat(timespec="minutes"),
            "duty_date": d_date.isoformat(),
            "shift_note": shift_note,
            "is_past": is_past,
            "known": known,
            "status": {
                "pharmacies": ph_status,
                "events": ev_status,
                "error": ph_error,
            },
            "walk_origin": {
                "lat": round(origin[0], 6),
                "lon": round(origin[1], 6),
                "label": walk_from_label,
                "shared": near is not None,
            },
            "pharmacies": pharmacies,
            "events": tonight_events,
            "upcoming_events": upcoming_events,
            "open_now": open_now,
            "counts": {
                "pharmacies": len(pharmacies),
                "events": len(tonight_events),
                "upcoming_events": len(upcoming_events),
                "open_now": len(open_now),
            },
            "source_url": self.roster.source_url,
            "fetched_at": datetime.fromtimestamp(self.roster.fetched_at, tz=TZ).isoformat(timespec="seconds")
            if self.roster.fetched_at else None,
        }

    def get_pharmacy(self, eid: str, *, allow_network: bool = True) -> dict | None:
        """Resolve `duty-<YYYY-MM-DD>-<reg_no>` or `duty-<reg_no>`."""
        key = (eid or "").strip().lower()
        if allow_network and not self.roster.rows:
            self.ensure_roster(allow_network=True, allow_geocode=True)
        hit = self.roster.get(key)
        if hit is None:
            parts = key.split("-")
            if len(parts) == 2 and parts[0] == "duty":
                today_id = f"duty-{duty_date_for(self.now()).isoformat()}-{parts[1]}"
                hit = self.roster.get(today_id) or next(
                    (r for r in self.roster.rows if r["reg_no"].lower() == parts[1]), None
                )
            elif len(parts) == 5 and parts[0] == "duty":
                d_str = "-".join(parts[1:4])
                try:
                    d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
                    mem_rows = db_store.read_pharmacies_for_date(self.memory, d_obj) or []
                    hit = next((r for r in mem_rows if r["id"].lower() == key), None)
                except ValueError:
                    hit = None
        if hit is None:
            return None
        return self._enrich_pharmacy(hit, None, allow_geocode=allow_network)

    def get_event(self, eid: str, *, allow_network: bool = True) -> dict | None:
        key = (eid or "").strip().lower()
        if allow_network and not self.calendar.events:
            self.ensure_events(allow_network=True, allow_geocode=True)
        hit = next((e for e in self.calendar.events if e["id"].lower() == key), None)
        if hit is None:
            hit = db_store.read_event_by_id(self.memory, key)
        if hit is None:
            return None
        return self._enrich_event(hit, None, allow_geocode=allow_network)

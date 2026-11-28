"""Events calendar in Nicosia from Visit Cyprus (The Events Calendar WordPress API)."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chatbot.geo import in_bbox, norm
from chatbot.settings import CACHE_DIR, TIMEZONE, Settings
from chatbot.tonight.geocode import DLS_BBOX
from chatbot.tonight.pharmacies import duty_date_for, sanitize_copy
from chatbot.web import Throttle, WebError, get_json

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

EVENTS_URL = "https://www.visitcyprus.com/wp-json/tribe/events/v1/events"
EVENTS_TTL_S = 3600
MAX_PAGES = 3

NICOSIA_CITIES = (
    "nicosia", "lefkosia", "lefkoşa", "λευκωσια",
    "strovolos", "στροβολος",
    "egkomi", "engomi", "εγκωμη",
    "aglantzia", "αγλαντζια",
    "lakatamia", "lakatameia", "lakatemeia", "λακαταμεια",
    "latsia", "λατσια",
    "geri", "γερι",
    "agios dometios", "αγιος δομετιος",
    "tseri", "τσερι",
    "dali", "δαλι",
    "kaimakli", "καιμακλι",
    "pallouriotissa", "παλλουριωτισσα",
)

HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(raw: str | None, limit: int = 300) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(str(raw))
    plain = HTML_TAG_RE.sub(" ", unescaped)
    return sanitize_copy(plain)[:limit]


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    s = str(text).strip()
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except ValueError:
        return None


OTHER_DISTRICTS = (
    "limassol", "lemesos", "λεμεσος",
    "larnaca", "larnaka", "λαρνακα",
    "paphos", "pafos", "παφος", "geroskipou", "γεροσκηπου", "polis chrysochous",
    "famagusta", "ammochostos", "αμμοχωστος", "ayia napa", "agia napa", "protaras", "paralimni",
)


def is_nicosia_venue(venue: object) -> bool:
    if not isinstance(venue, dict):
        return False
    try:
        lat = float(venue.get("geo_lat"))
        lon = float(venue.get("geo_lng"))
        return in_bbox(lat, lon, DLS_BBOX)
    except (TypeError, ValueError):
        pass
    hay = norm(" ".join(str(venue.get(k) or "") for k in ("city", "address", "venue", "stateprovince")))
    if any(other in hay for other in OTHER_DISTRICTS):
        return False
    return any(norm(c) in hay for c in NICOSIA_CITIES)


def parse_visitcyprus_events(payload: dict) -> list[dict]:
    """Extract sanitized Nicosia-area events from a VisitCyprus Tribe Events API response."""
    out: list[dict] = []
    for raw in payload.get("events") or []:
        if not isinstance(raw, dict):
            continue
        venue = raw.get("venue") if isinstance(raw.get("venue"), dict) else {}
        if not is_nicosia_venue(venue):
            continue
        start_dt = _parse_dt(raw.get("start_date"))
        if start_dt is None:
            continue
        end_dt = _parse_dt(raw.get("end_date")) or (start_dt + timedelta(hours=2))
        cats = [
            sanitize_copy(html.unescape(c.get("name")))
            for c in (raw.get("categories") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        eid = f"event-{raw.get('id')}"
        title = _clean_html(raw.get("title"), 180)
        venue_name = _clean_html(venue.get("venue"), 120) or "Nicosia venue"
        venue_addr = _clean_html(venue.get("address"), 160)
        city = _clean_html(venue.get("city"), 80) or "Nicosia"
        desc = _clean_html(raw.get("excerpt") or raw.get("description"), 240)
        geo_lat = geo_lon = None
        try:
            if venue.get("geo_lat") is not None and venue.get("geo_lng") is not None:
                geo_lat = float(venue["geo_lat"])
                geo_lon = float(venue["geo_lng"])
        except (TypeError, ValueError):
            geo_lat = geo_lon = None
        out.append({
            "id": eid,
            "title": title,
            "start_ts": start_dt.isoformat(timespec="seconds"),
            "end_ts": end_dt.isoformat(timespec="seconds"),
            "all_day": bool(raw.get("all_day")),
            "categories": ", ".join(dict.fromkeys(cats)),
            "venue_name": venue_name,
            "venue_address": venue_addr,
            "municipality": city,
            "url": str(raw.get("url") or "").strip() or None,
            "description": desc,
            "venue_lat": geo_lat,
            "venue_lon": geo_lon,
            "source": "visit_cyprus",
        })
    return out


class EventsCalendar:
    """Throttled, cached Visit Cyprus calendar for Nicosia."""

    def __init__(self, settings: Settings, cache_dir: Path | None = None, *, fetch_json=None):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "tonight"
        self.cache_path = self.dir / "events.json"
        self._fetch_json = fetch_json or (
            lambda url, params: get_json(url, user_agent=self.s.user_agent, params=params, timeout=25)
        )
        self._throttle = Throttle(1.0)
        self.events: list[dict] = []
        self.fetched_at: float = 0.0
        self.status: str = "waiting"
        self.error: str | None = None
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            items = data.get("events") or []
            if isinstance(items, list):
                self.events = items
                self.fetched_at = float(data.get("fetched_at") or 0.0)
                self.status = "live" if time.time() - self.fetched_at < EVENTS_TTL_S else "stale"
        except (OSError, ValueError, TypeError):
            pass

    def _save_cache(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps({"fetched_at": self.fetched_at, "events": self.events}, ensure_ascii=False),
            encoding="utf-8",
        )

    def refresh(self, now: datetime | None = None, *, force: bool = False) -> list[dict]:
        if not force and self.events and time.time() - self.fetched_at < EVENTS_TTL_S:
            self.status = "live"
            self.error = None
            return self.events
        ref = (now or datetime.now(TZ)).astimezone(TZ).date() - timedelta(days=7)
        try:
            by_id: dict[str, dict] = {}
            for page in range(1, MAX_PAGES + 1):
                self._throttle.wait()
                payload = self._fetch_json(EVENTS_URL, {
                    "per_page": 50,
                    "page": page,
                    "start_date": ref.isoformat(),
                })
                for ev in parse_visitcyprus_events(payload):
                    by_id[ev["id"]] = ev
                total_pages = int(payload.get("total_pages") or 1)
                if page >= total_pages:
                    break
            self.events = sorted(by_id.values(), key=lambda e: e["start_ts"])
            self.fetched_at = time.time()
            self.status = "live"
            self.error = None
            self._save_cache()
            return self.events
        except (WebError, OSError, ValueError, TypeError) as e:
            self.error = str(e)
            self.status = "stale" if self.events else "unavailable"
            log.info("Visit Cyprus events fetch skipped (%s); status=%s", e, self.status)
            return self.events


def split_events_for_moment(events: list[dict], at: datetime) -> tuple[list[dict], list[dict]]:
    """Return (tonight_events, upcoming_events) relative to `at` in Asia/Nicosia."""
    local = at.astimezone(TZ)
    night_d = duty_date_for(local)
    next_d = night_d + timedelta(days=1)
    tonight: list[dict] = []
    upcoming: list[dict] = []

    for ev in events:
        start = _parse_dt(ev.get("start_ts"))
        end = _parse_dt(ev.get("end_ts")) or start
        if start is None or end is None:
            continue
        # Active at `at` OR happening on this evening/night window
        spans_now = start <= local <= end
        starts_tonight = (
            (start.date() == night_d and (start.hour >= 12 or ev.get("all_day")))
            or (start.date() == next_d and start.hour < 4)
            or (start.date() <= night_d <= end.date() and ev.get("all_day"))
        )
        if spans_now or starts_tonight:
            status = "happening_now" if spans_now else "tonight"
            tonight.append({**ev, "event_state": status})
        elif 0 < (start.date() - local.date()).days <= 14:
            upcoming.append({**ev, "event_state": "upcoming"})

    return tonight, upcoming

"""Pharmacies on duty in Nicosia (data.gov.cy Ministry of Health quarterly CSV).

PRIVACY CRITICAL: The upstream CSV contains a personal home phone column (`House Tel. No.`).
It is stripped immediately during CSV row parsing and is NEVER stored, cached, logged, or shown.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chatbot.settings import CACHE_DIR, TIMEZONE, Settings
from chatbot.web import Throttle, WebError, get_bytes, request

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

LEYKOSIA_PAGE = "https://www.data.gov.cy/el/resource/leykosia"
FALLBACK_CSV_URL = "https://www.data.gov.cy/el/resource/4226/download/file"
ROSTER_TTL_S = 6 * 3600
DISCOVERY_TTL_S = 6 * 3600

# Column name patterns that MUST NEVER be stored, cached, logged, or displayed.
HOME_PHONE_COL = re.compile(r"(house|home|οικ|προσωπ|private)", re.IGNORECASE)
DOWNLOAD_LINK_RE = re.compile(
    r'href=["\']((?:https?://www\.data\.gov\.cy)?/(?:el|en)/resource/\d+/download/file)["\']',
    re.IGNORECASE,
)
SHORTLINK_RE = re.compile(
    r'<link[^>]+rel=["\']shortlink["\'][^>]+href=["\'](?:https?://www\.data\.gov\.cy)?/(?:el|en)/resource/(\d+)["\']',
    re.IGNORECASE,
)
DASHES_RE = re.compile(r"\s*[—–]\s*|--+")


def sanitize_copy(text: str | None) -> str:
    """Clean whitespace and replace em/en dashes or '--' so UI copy stays compliant."""
    if not text:
        return ""
    s = re.sub(r"\s+", " ", str(text)).strip()
    s = DASHES_RE.sub(", ", s)
    return re.sub(r"\s+,", ",", s).strip(" ,")


def duty_date_for(dt: datetime) -> date:
    """Cyprus duty shifts run from 08:00 until 08:00 the next morning."""
    local = dt.astimezone(TZ)
    return (local - timedelta(days=1)).date() if local.hour < 8 else local.date()


def duty_id(duty_date: str | date, reg_no: str) -> str:
    d = duty_date.isoformat() if isinstance(duty_date, date) else str(duty_date).strip()
    reg = re.sub(r"[^a-zA-Z0-9]+", "", str(reg_no or "").strip()) or "0"
    return f"duty-{d}-{reg}"


def discover_csv_url(html: str, base_url: str = LEYKOSIA_PAGE) -> str | None:
    """Extract the current quarter's CSV download link from the data.gov.cy resource page."""
    if not html:
        return None
    if m := DOWNLOAD_LINK_RE.search(html):
        return urllib.parse.urljoin(base_url, m.group(1))
    if m := SHORTLINK_RE.search(html):
        return urllib.parse.urljoin(base_url, f"/el/resource/{m.group(1)}/download/file")
    return None


def _parse_date(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _col_map(fieldnames: list[str] | tuple[str, ...] | None) -> dict[str, str]:
    """Map logical field names to CSV column names, dropping any home-phone column."""
    cols = [c for c in (fieldnames or ()) if c and not HOME_PHONE_COL.search(c)]
    lower = {re.sub(r"\s+", " ", c).strip().lower(): c for c in cols}

    def pick(*candidates: str) -> str | None:
        for cand in candidates:
            if cand in lower:
                return lower[cand]
        return None

    def pick_substr(*needles: str) -> str | None:
        for low, orig in lower.items():
            if any(n in low for n in needles):
                return orig
        return None

    return {
        "date": pick("date", "ημερομηνία") or pick_substr("date"),
        "day": pick("day", "ημέρα") or pick_substr("day"),
        "reg_no": pick("reg. no.", "reg no", "reg. no", "α.μ.") or pick_substr("reg"),
        # Upstream CSV spells Surname as "Surmame"
        "surname": pick("surname", "surmame", "επώνυμο") or pick_substr("surm", "surn"),
        "name": pick("name", "όνομα"),
        "address": pick("address", "διεύθυνση") or pick_substr("address"),
        "directions": pick("additional address info", "directions", "οδηγίες") or pick_substr("additional"),
        # Upstream CSV spells Municipality as "Muniuciplity / Community"
        "municipality": pick("municipality / community", "muniuciplity / community", "municipality", "δήμος")
        or pick_substr("munic", "muniuc", "communit"),
        "pharmacy_tel": pick("pharmacy tel. no.", "pharmacy tel", "τηλέφωνο φαρμακείου")
        or pick_substr("pharmacy tel"),
    }


def parse_duty_csv(text: str, source_url: str | None = None) -> list[dict]:
    """Parse Nicosia duty pharmacies CSV, immediately stripping any personal home phone column."""
    clean_text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(clean_text))
    cmap = _col_map(reader.fieldnames)
    rows: list[dict] = []
    for raw_row in reader:
        # Build strictly from non-home-phone columns only.
        safe = {
            logical: sanitize_copy(raw_row.get(col))
            for logical, col in cmap.items()
            if col and not HOME_PHONE_COL.search(col)
        }
        iso_date = _parse_date(safe.get("date"))
        reg_no = re.sub(r"\s+", "", safe.get("reg_no") or "")
        if not iso_date or not reg_no:
            continue
        surname = safe.get("surname") or ""
        first = safe.get("name") or ""
        pharmacist = " ".join(x for x in (first, surname) if x).strip()
        address = safe.get("address") or ""
        directions = safe.get("directions") or ""
        municipality = safe.get("municipality") or ""
        tel = re.sub(r"[^\d+ ]+", "", safe.get("pharmacy_tel") or "").strip()
        rows.append({
            "id": duty_id(iso_date, reg_no),
            "duty_date": iso_date,
            "reg_no": reg_no,
            "pharmacist": pharmacist,
            "surname": surname,
            "first_name": first,
            "address": address,
            "directions": directions,
            "municipality": municipality,
            "pharmacy_tel": tel,
            "source_url": source_url or FALLBACK_CSV_URL,
        })
    return rows


class DutyRosterStore:
    """Fetches, sanitizes, and caches the quarterly Nicosia duty pharmacy roster."""

    def __init__(
        self,
        settings: Settings,
        cache_dir: Path | None = None,
        *,
        fetch_text=None,
        fetch_bytes=None,
    ):
        self.s = settings
        self.dir = (cache_dir or CACHE_DIR) / "tonight"
        self.cache_path = self.dir / "duty_roster.json"
        self._fetch_text = fetch_text or (
            lambda url: request(url, user_agent=self.s.user_agent, timeout=25, accept="text/html,*/*")
        )
        self._fetch_bytes = fetch_bytes or (
            lambda url: get_bytes(url, user_agent=self.s.user_agent, timeout=30)
        )
        self._throttle = Throttle(1.0)
        self.rows: list[dict] = []
        self.fetched_at: float = 0.0
        self.source_url: str = FALLBACK_CSV_URL
        self.status: str = "waiting"  # live | stale | unavailable | waiting
        self.error: str | None = None
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            rows = data.get("rows") or []
            if isinstance(rows, list) and rows:
                # Defensive scrub in case an older file had anything extra.
                self.rows = [{k: v for k, v in r.items() if not HOME_PHONE_COL.search(k)} for r in rows]
                self.fetched_at = float(data.get("fetched_at") or 0.0)
                self.source_url = str(data.get("source_url") or FALLBACK_CSV_URL)
                self.status = "live" if time.time() - self.fetched_at < ROSTER_TTL_S else "stale"
        except (OSError, ValueError, TypeError):
            pass

    def _save_cache(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Never persist any key matching HOME_PHONE_COL.
        safe_rows = [{k: v for k, v in r.items() if not HOME_PHONE_COL.search(k)} for r in self.rows]
        self.cache_path.write_text(
            json.dumps(
                {
                    "fetched_at": self.fetched_at,
                    "source_url": self.source_url,
                    "rows": safe_rows,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def refresh(self, force: bool = False) -> list[dict]:
        """Return sanitized duty pharmacy rows, refreshing from data.gov.cy if stale."""
        if not force and self.rows and time.time() - self.fetched_at < ROSTER_TTL_S:
            self.status = "live"
            self.error = None
            return self.rows
        try:
            self._throttle.wait()
            csv_url = self.source_url or FALLBACK_CSV_URL
            try:
                html = self._fetch_text(LEYKOSIA_PAGE)
                if found := discover_csv_url(html, LEYKOSIA_PAGE):
                    csv_url = found
            except (WebError, OSError, ValueError) as e:
                log.info("duty roster discovery page skipped (%s), using %s", e, csv_url)

            self._throttle.wait()
            raw = self._fetch_bytes(csv_url)
            text = raw.decode("utf-8-sig", "replace")
            parsed = parse_duty_csv(text, source_url=csv_url)
            if not parsed:
                raise WebError("data.gov.cy duty CSV returned no rows")
            self.rows = parsed
            self.source_url = csv_url
            self.fetched_at = time.time()
            self.status = "live"
            self.error = None
            self._save_cache()
            return self.rows
        except (WebError, OSError, ValueError) as e:
            self.error = str(e)
            self.status = "stale" if self.rows else "unavailable"
            log.warning("duty pharmacy fetch failed (%s); status=%s", e, self.status)
            return self.rows

    def for_date(self, d: date) -> list[dict]:
        iso = d.isoformat()
        return [r for r in self.rows if r.get("duty_date") == iso]

    def get(self, eid: str) -> dict | None:
        key = (eid or "").strip().lower()
        return next((r for r in self.rows if r["id"].lower() == key), None)

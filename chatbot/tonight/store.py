"""DuckDB persistence for Tonight in Nicosia (`duty_pharmacies` and `city_events`).

Tables are created on demand via `memory.writing()` so `collector/db.py` stays untouched.
PRIVACY CRITICAL: `duty_pharmacies` has no home-phone column and rejects any forbidden key.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import duckdb

from chatbot.memory import Memory, Unavailable
from chatbot.settings import TIMEZONE
from chatbot.tonight.pharmacies import HOME_PHONE_COL

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

TONIGHT_DDL = """
CREATE TABLE IF NOT EXISTS duty_pharmacies (
    duty_date DATE NOT NULL,
    reg_no VARCHAR NOT NULL,
    pharmacist VARCHAR,
    address VARCHAR,
    directions VARCHAR,
    municipality VARCHAR,
    pharmacy_tel VARCHAR,
    lat DOUBLE,
    lon DOUBLE,
    precision VARCHAR,
    located_by VARCHAR,
    osm_id VARCHAR,
    overture_id VARCHAR,
    source_url VARCHAR,
    fetched_at TIMESTAMPTZ,
    PRIMARY KEY (duty_date, reg_no)
);
CREATE TABLE IF NOT EXISTS city_events (
    event_id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    start_ts TIMESTAMPTZ NOT NULL,
    end_ts TIMESTAMPTZ,
    all_day BOOLEAN,
    categories VARCHAR,
    venue_name VARCHAR,
    venue_address VARCHAR,
    municipality VARCHAR,
    lat DOUBLE,
    lon DOUBLE,
    precision VARCHAR,
    located_by VARCHAR,
    url VARCHAR,
    description VARCHAR,
    source VARCHAR,
    fetched_at TIMESTAMPTZ
);
"""


def _dt_utc(epoch: float | None) -> datetime:
    if epoch:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _parse_ts(val: object) -> datetime | None:
    if val is None or isinstance(val, datetime):
        return val
    try:
        dt = datetime.fromisoformat(str(val).strip().replace(" ", "T"))
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt
    except ValueError:
        return None


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(TONIGHT_DDL)


def sync_pharmacies(memory: Memory | None, rows: list[dict], fetched_at: float | None = None) -> int:
    """Persist sanitized duty pharmacy rows into `duty_pharmacies`. Returns rows written."""
    if memory is None or not rows:
        return 0
    ts = _dt_utc(fetched_at)
    params = []
    for r in rows:
        # Defense in depth: never allow any personal home phone field near DuckDB.
        if any(HOME_PHONE_COL.search(k) for k in r):
            r = {k: v for k, v in r.items() if not HOME_PHONE_COL.search(k)}
        params.append((
            r["duty_date"],
            r["reg_no"],
            r.get("pharmacist") or "",
            r.get("address") or "",
            r.get("directions") or "",
            r.get("municipality") or "",
            r.get("pharmacy_tel") or "",
            r.get("lat"),
            r.get("lon"),
            r.get("precision"),
            r.get("located_by"),
            r.get("osm_id"),
            r.get("overture_id"),
            r.get("source_url"),
            ts,
        ))
    try:
        with memory.writing() as con:
            ensure_tables(con)
            con.executemany(
                """INSERT OR REPLACE INTO duty_pharmacies (
                       duty_date, reg_no, pharmacist, address, directions, municipality,
                       pharmacy_tel, lat, lon, precision, located_by, osm_id, overture_id,
                       source_url, fetched_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
        return len(params)
    except Unavailable as e:
        log.info("duty_pharmacies sync skipped: %s", e)
        return 0


def sync_events(memory: Memory | None, events: list[dict], fetched_at: float | None = None) -> int:
    """Persist city events calendar into `city_events`. Returns rows written."""
    if memory is None or not events:
        return 0
    ts = _dt_utc(fetched_at)
    params = [
        (
            ev["id"],
            ev["title"],
            _parse_ts(ev["start_ts"]),
            _parse_ts(ev.get("end_ts")),
            bool(ev.get("all_day")),
            ev.get("categories") or "",
            ev.get("venue_name") or "",
            ev.get("venue_address") or "",
            ev.get("municipality") or "",
            ev.get("lat"),
            ev.get("lon"),
            ev.get("precision"),
            ev.get("located_by"),
            ev.get("url"),
            ev.get("description") or "",
            ev.get("source") or "visit_cyprus",
            ts,
        )
        for ev in events
    ]
    try:
        with memory.writing() as con:
            ensure_tables(con)
            con.executemany(
                """INSERT OR REPLACE INTO city_events (
                       event_id, title, start_ts, end_ts, all_day, categories,
                       venue_name, venue_address, municipality, lat, lon, precision,
                       located_by, url, description, source, fetched_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
        return len(params)
    except Unavailable as e:
        log.info("city_events sync skipped: %s", e)
        return 0


def read_pharmacies_for_date(memory: Memory | None, d: date) -> list[dict] | None:
    """Read stored duty pharmacies for `d` from DuckDB, or None if unavailable/missing table."""
    if memory is None or not memory.exists():
        return None
    try:
        with memory.reading() as con:
            rows = con.execute(
                """SELECT duty_date, reg_no, pharmacist, address, directions, municipality,
                          pharmacy_tel, lat, lon, precision, located_by, osm_id, overture_id,
                          source_url
                   FROM duty_pharmacies
                   WHERE duty_date = ?
                   ORDER BY reg_no""",
                [d.isoformat()],
            ).fetchall()
    except (Unavailable, duckdb.CatalogException):
        return None
    out = []
    for (
        duty_date, reg_no, pharmacist, address, directions, municipality,
        pharmacy_tel, lat, lon, precision, located_by, osm_id, overture_id, source_url,
    ) in rows:
        iso = duty_date.isoformat() if hasattr(duty_date, "isoformat") else str(duty_date)
        out.append({
            "id": f"duty-{iso}-{reg_no}",
            "duty_date": iso,
            "reg_no": reg_no,
            "pharmacist": pharmacist or "",
            "address": address or "",
            "directions": directions or "",
            "municipality": municipality or "",
            "pharmacy_tel": pharmacy_tel or "",
            "lat": lat,
            "lon": lon,
            "precision": precision or ("exact" if lat is not None else "unplaced"),
            "located_by": located_by or "The twin's memory",
            "osm_id": osm_id,
            "overture_id": overture_id,
            "source_url": source_url,
        })
    return out


def read_events_range(memory: Memory | None, start_dt: datetime, end_dt: datetime) -> list[dict] | None:
    """Read stored events overlapping [start_dt, end_dt] from DuckDB, or None if missing."""
    if memory is None or not memory.exists():
        return None
    try:
        with memory.reading() as con:
            rows = con.execute(
                """SELECT event_id, title, start_ts, end_ts, all_day, categories,
                          venue_name, venue_address, municipality, lat, lon, precision,
                          located_by, url, description, source
                   FROM city_events
                   WHERE coalesce(end_ts, start_ts) >= ? AND start_ts <= ?
                   ORDER BY start_ts""",
                [start_dt, end_dt],
            ).fetchall()
    except (Unavailable, duckdb.CatalogException):
        return None
    out = []
    for (
        eid, title, start_ts, end_ts, all_day, categories,
        venue_name, venue_address, municipality, lat, lon, precision,
        located_by, url, description, source,
    ) in rows:
        st = start_ts.astimezone(TZ).isoformat(timespec="minutes") if isinstance(start_ts, datetime) else str(start_ts)
        en = end_ts.astimezone(TZ).isoformat(timespec="minutes") if isinstance(end_ts, datetime) else (str(end_ts) if end_ts else st)
        out.append({
            "id": eid,
            "title": title,
            "start_ts": st,
            "end_ts": en,
            "all_day": bool(all_day),
            "categories": categories or "",
            "venue_name": venue_name or "",
            "venue_address": venue_address or "",
            "municipality": municipality or "",
            "lat": lat,
            "lon": lon,
            "precision": precision or ("exact" if lat is not None else "unplaced"),
            "located_by": located_by or "The twin's memory",
            "url": url,
            "description": description or "",
            "source": source or "visit_cyprus",
        })
    return out


def read_event_by_id(memory: Memory | None, eid: str) -> dict | None:
    if memory is None or not memory.exists():
        return None
    try:
        with memory.reading() as con:
            row = con.execute(
                """SELECT event_id, title, start_ts, end_ts, all_day, categories,
                          venue_name, venue_address, municipality, lat, lon, precision,
                          located_by, url, description, source
                   FROM city_events WHERE lower(event_id) = lower(?)""",
                [eid],
            ).fetchone()
    except (Unavailable, duckdb.CatalogException):
        return None
    if not row:
        return None
    (
        event_id, title, start_ts, end_ts, all_day, categories,
        venue_name, venue_address, municipality, lat, lon, precision,
        located_by, url, description, source,
    ) = row
    st = start_ts.astimezone(TZ).isoformat(timespec="minutes") if isinstance(start_ts, datetime) else str(start_ts)
    en = end_ts.astimezone(TZ).isoformat(timespec="minutes") if isinstance(end_ts, datetime) else (str(end_ts) if end_ts else st)
    return {
        "id": event_id,
        "title": title,
        "start_ts": st,
        "end_ts": en,
        "all_day": bool(all_day),
        "categories": categories or "",
        "venue_name": venue_name or "",
        "venue_address": venue_address or "",
        "municipality": municipality or "",
        "lat": lat,
        "lon": lon,
        "precision": precision or ("exact" if lat is not None else "unplaced"),
        "located_by": located_by or "The twin's memory",
        "url": url,
        "description": description or "",
        "source": source or "visit_cyprus",
    }

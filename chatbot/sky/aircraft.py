"""Live aircraft over and around Nicosia from adsb.lol (ODbL, keyless), with 24-hour
DuckDB history for the city clock.

Community ADS-B receiver coverage around Cyprus is partial, especially at low altitude and on
quiet evenings, so an empty or sparse sky here is a receiver limit, never a claim that the
airspace is empty.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

from collector import db

from chatbot.settings import NICOSIA_CENTER, TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

ADSB_URL = "https://api.adsb.lol/v2/lat/35.15/lon/33.36/dist/40"
ADSB_TTL_S = 30.0
ADSB_KEEP_S = 180.0
HISTORY_HOURS = 24
REPLAY_WINDOW_S = 180
PRUNE_EVERY_S = 600.0
MIN_WRITE_STEP_S = 10

COVERAGE_NOTE = (
    "Community ADS-B receiver coverage around Nicosia is partial; low-level flights and "
    "quiet evenings may show few or no aircraft."
)
NO_RECORD_NOTE = "No recorded aircraft positions for this moment"

TABLE = """CREATE TABLE IF NOT EXISTS aircraft_positions (
    hex VARCHAR NOT NULL,
    reported_ts TIMESTAMPTZ NOT NULL,
    callsign VARCHAR,
    registration VARCHAR,
    aircraft_type VARCHAR,
    lat DOUBLE NOT NULL,
    lon DOUBLE NOT NULL,
    altitude_ft INTEGER,
    speed_kmh DOUBLE,
    track_deg DOUBLE,
    vertical_rate_fpm INTEGER,
    on_ground BOOLEAN,
    squawk VARCHAR,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (hex, reported_ts)
)"""

_COLS = {
    "hex": "VARCHAR",
    "reported_ts": "BIGINT",
    "callsign": "VARCHAR",
    "registration": "VARCHAR",
    "aircraft_type": "VARCHAR",
    "lat": "DOUBLE",
    "lon": "DOUBLE",
    "altitude_ft": "INTEGER",
    "speed_kmh": "DOUBLE",
    "track_deg": "DOUBLE",
    "vertical_rate_fpm": "INTEGER",
    "on_ground": "BOOLEAN",
    "squawk": "VARCHAR",
    "recorded_at": "BIGINT",
}


def _clean_str(val: object) -> str | None:
    if not isinstance(val, str):
        return None
    s = val.strip()
    return s or None


def _num(val: object) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def parse_aircraft(payload: dict, now: datetime) -> list[dict]:
    """Normalise an adsb.lol v2 /lat/.../lon/.../dist/... JSON response."""
    now_ts = now.timestamp()
    out: list[dict] = []
    for raw in payload.get("ac") or []:
        if not isinstance(raw, dict):
            continue
        hex_code = _clean_str(raw.get("hex"))
        lat = _num(raw.get("lat"))
        lon = _num(raw.get("lon"))
        if not hex_code or lat is None or lon is None:
            continue
        hex_l = hex_code.lower()
        reg = _clean_str(raw.get("r"))
        flight = _clean_str(raw.get("flight"))
        callsign = flight or reg or hex_l.upper()
        ac_type = _clean_str(raw.get("t"))

        raw_alt = raw.get("alt_baro")
        on_ground = raw_alt == "ground"
        if on_ground:
            alt_ft = 0
        else:
            v = _num(raw_alt)
            if v is None:
                v = _num(raw.get("alt_geom"))
            alt_ft = round(v) if v is not None else None
        alt_m = round(alt_ft * 0.3048) if alt_ft is not None else None

        gs_kt = _num(raw.get("gs"))
        speed_kmh = round(gs_kt * 1.852, 1) if gs_kt is not None else None

        track = _num(raw.get("track"))
        if track is None:
            track = _num(raw.get("true_heading"))
        if track is None:
            track = _num(raw.get("mag_heading"))
        track_deg = round(track % 360, 1) if track is not None else None

        vrate = _num(raw.get("baro_rate"))
        if vrate is None:
            vrate = _num(raw.get("geom_rate"))
        vrate_fpm = round(vrate) if vrate is not None else None

        seen_s = _num(raw.get("seen_pos"))
        if seen_s is None:
            seen_s = _num(raw.get("seen"))
        seen_val = round(max(0.0, seen_s), 1) if seen_s is not None else 0.0
        reported = int(round(now_ts - seen_val))

        dst_nm = _num(raw.get("dst"))
        distance_km = round(dst_nm * 1.852, 1) if dst_nm is not None else None

        out.append({
            "id": f"ac-{hex_l}",
            "hex": hex_l,
            "callsign": callsign,
            "flight": flight,
            "registration": reg,
            "type": ac_type,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "altitude_ft": alt_ft,
            "altitude_m": alt_m,
            "on_ground": on_ground,
            "ground_speed_kt": round(gs_kt, 1) if gs_kt is not None else None,
            "speed_kmh": speed_kmh,
            "track_deg": track_deg,
            "heading": track_deg,
            "vertical_rate_fpm": vrate_fpm,
            "squawk": _clean_str(raw.get("squawk")),
            "category": _clean_str(raw.get("category")),
            "distance_nm": round(dst_nm, 1) if dst_nm is not None else None,
            "distance_km": distance_km,
            "seen_s": seen_val,
            "reported": reported,
        })
    out.sort(key=lambda a: (a["on_ground"], a["distance_km"] if a["distance_km"] is not None else 999.0, a["callsign"]))
    return out


class AircraftTracker:
    """Fetches adsb.lol at most every 30 s when polled and archives 24 h into DuckDB."""

    def __init__(self, fetch_json, memory=None, *, now=None, ttl_s: float = ADSB_TTL_S):
        self.fetch_json = fetch_json
        self.memory = memory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._last: dict | None = None
        self._read_at: float | None = None
        self._read_ok_at: float | None = None
        self._error: str | None = None
        self._written: dict[str, int] = {}
        self._last_prune = 0.0
        self.fetches = 0

    def health(self) -> dict | None:
        """Last fetch metadata for /api/sources (never fetches)."""
        if self._read_at is None:
            return None
        last = self._last or {}
        return {
            "read_at": self._read_at,
            "read_ok_at": self._read_ok_at,
            "error": self._error,
            "status": last.get("status"),
            "count": last.get("count", 0),
        }

    def snapshot(self) -> dict:
        """Live aircraft around Nicosia, cached for ttl_s and kept briefly on errors."""
        now_dt = self.now()
        now_s = now_dt.timestamp()
        with self._lock:
            if self._last and now_s < self._last.get("retry_at", 0.0):
                return self._last
        with self._lock:
            if self._last and now_s < self._last.get("retry_at", 0.0):
                return self._last
            res = self._refresh(now_dt, self._last)
            self._last = res
            return res

    def _refresh(self, now_dt: datetime, previous: dict | None) -> dict:
        now_s = now_dt.timestamp()
        self._read_at = now_s
        try:
            self.fetches += 1
            payload = self.fetch_json(ADSB_URL)
            aircraft = parse_aircraft(payload, now_dt)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
            self._error = err
            log.warning("adsb.lol fetch failed: %s", err)
            if previous and previous.get("fetched_at") and now_s - (previous["fetched_at"] / 1000) < ADSB_KEEP_S:
                return {
                    **previous,
                    "status": "stale",
                    "error": err,
                    "retry_at": now_s + 15.0,
                }
            return {
                "status": "unavailable",
                "error": err,
                "known": False,
                "recorded": False,
                "count": 0,
                "fetched_at": int(now_s * 1000),
                "as_of": now_dt.astimezone(TZ).isoformat(timespec="seconds"),
                "coverage_note": COVERAGE_NOTE,
                "aircraft": [],
                "retry_at": now_s + 15.0,
            }

        self._read_ok_at = now_s
        self._error = None
        if self.memory is not None and aircraft:
            self.record(aircraft, now_dt)
        return {
            "status": "live",
            "error": None,
            "known": True,
            "recorded": False,
            "count": len(aircraft),
            "fetched_at": int(now_s * 1000),
            "as_of": now_dt.astimezone(TZ).isoformat(timespec="seconds"),
            "coverage_note": COVERAGE_NOTE,
            "aircraft": aircraft,
            "retry_at": now_s + self.ttl_s,
        }

    def record(self, aircraft: list[dict], now_dt: datetime | None = None) -> int:
        """Archive current aircraft positions into `aircraft_positions` with 24 h retention."""
        if self.memory is None or not aircraft:
            return 0
        now_dt = now_dt or self.now()
        now_us = int(now_dt.timestamp() * 1_000_000)
        rows = []
        new_written: dict[str, int] = {}
        for a in aircraft:
            hx = a["hex"]
            rep_s = int(a["reported"])
            prev_s = self._written.get(hx)
            if prev_s is not None and abs(rep_s - prev_s) < MIN_WRITE_STEP_S:
                continue
            rows.append((
                hx,
                rep_s * 1_000_000,
                a.get("callsign"),
                a.get("registration"),
                a.get("type"),
                a["lat"],
                a["lon"],
                a.get("altitude_ft"),
                a.get("speed_kmh"),
                a.get("track_deg"),
                a.get("vertical_rate_fpm"),
                bool(a.get("on_ground")),
                a.get("squawk"),
                now_us,
            ))
            new_written[hx] = rep_s
        if not rows:
            return 0
        try:
            with self.memory.writing(timeout=3.0) as con:
                con.execute(TABLE)
                written = db.bulk(
                    con, _COLS, rows,
                    """INSERT OR IGNORE INTO aircraft_positions
                       SELECT hex, make_timestamptz(reported_ts), callsign, registration,
                              aircraft_type, lat, lon, altitude_ft, speed_kmh, track_deg,
                              vertical_rate_fpm, on_ground, squawk, make_timestamptz(recorded_at)
                       FROM __SRC__""",
                )
                if time.monotonic() - self._last_prune >= PRUNE_EVERY_S:
                    cutoff_us = int((now_dt - timedelta(hours=HISTORY_HOURS)).timestamp() * 1_000_000)
                    con.execute("DELETE FROM aircraft_positions WHERE reported_ts < make_timestamptz(?)", [cutoff_us])
                    self._last_prune = time.monotonic()
            self._written.update(new_written)
            return written
        except Exception as e:  # noqa: BLE001 - never break live response if store is busy
            log.warning("aircraft archive write skipped: %s", e)
            return 0

    def at(self, moment: datetime) -> dict:
        """Aircraft recorded around a past moment in `aircraft_positions`."""
        as_of = moment.astimezone(TZ).isoformat(timespec="seconds")
        empty = {
            "status": "unavailable",
            "error": NO_RECORD_NOTE,
            "note": NO_RECORD_NOTE,
            "known": False,
            "recorded": False,
            "at": as_of,
            "count": 0,
            "fetched_at": int(moment.timestamp() * 1000),
            "as_of": as_of,
            "coverage_note": COVERAGE_NOTE,
            "aircraft": [],
        }
        if self.memory is None:
            return empty
        lo = moment - timedelta(seconds=REPLAY_WINDOW_S)
        hi = moment + timedelta(seconds=30)
        try:
            with self.memory.reading() as con:
                rows = con.execute(
                    """SELECT hex, epoch_ms(reported_ts), callsign, registration, aircraft_type,
                              lat, lon, altitude_ft, speed_kmh, track_deg, vertical_rate_fpm,
                              on_ground, squawk
                       FROM (
                           SELECT *, ROW_NUMBER() OVER (PARTITION BY hex ORDER BY reported_ts DESC) AS rn
                           FROM aircraft_positions
                           WHERE reported_ts BETWEEN ? AND ?
                       )
                       WHERE rn = 1
                       ORDER BY coalesce(on_ground, false), callsign""",
                    [lo, hi],
                ).fetchall()
        except duckdb.CatalogException:
            return empty
        except Exception as e:  # noqa: BLE001
            log.warning("aircraft replay read failed: %s", e)
            return {**empty, "error": f"the twin's memory couldn't be read ({e})"}
        if not rows:
            return empty
        clat, clon = NICOSIA_CENTER
        items = []
        for hx, rep_ms, callsign, reg, ac_type, lat, lon, alt_ft, spd_kmh, trk, vrate, on_g, sq in rows:
            rep_s = int(rep_ms // 1000)
            alt_m = round(alt_ft * 0.3048) if alt_ft is not None else None
            seen_s = round(max(0.0, moment.timestamp() - rep_s), 1)
            # Rough ground distance from Nicosia centre for sorting/reading.
            dlat = (lat - clat) * 110.574
            dlon = (lon - clon) * 111.320 * 0.817
            dist_km = round((dlat * dlat + dlon * dlon) ** 0.5, 1)
            items.append({
                "id": f"ac-{hx}",
                "hex": hx,
                "callsign": callsign or reg or hx.upper(),
                "flight": callsign,
                "registration": reg,
                "type": ac_type,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "altitude_ft": alt_ft,
                "altitude_m": alt_m,
                "on_ground": bool(on_g),
                "ground_speed_kt": round(spd_kmh / 1.852, 1) if spd_kmh is not None else None,
                "speed_kmh": round(spd_kmh, 1) if spd_kmh is not None else None,
                "track_deg": trk,
                "heading": trk,
                "vertical_rate_fpm": vrate,
                "squawk": sq,
                "distance_km": dist_km,
                "seen_s": seen_s,
                "reported": rep_s,
            })
        return {
            "status": "recorded",
            "error": None,
            "known": True,
            "recorded": True,
            "at": as_of,
            "count": len(items),
            "fetched_at": int(moment.timestamp() * 1000),
            "as_of": as_of,
            "coverage_note": COVERAGE_NOTE,
            "aircraft": items,
        }

    def aircraft(self, eid: str, at: datetime | None = None) -> dict | None:
        key = eid.strip().lower()
        snap = self.at(at) if at is not None else self.snapshot()
        return next((a for a in snap["aircraft"] if a["id"].lower() == key or a["hex"] == key), None)

    def latest_recorded(self, eid: str) -> dict | None:
        """Latest sighting of `eid` stored in `aircraft_positions` (for inspecting past tracks)."""
        if self.memory is None:
            return None
        hx = eid.strip().lower().removeprefix("ac-")
        try:
            with self.memory.reading() as con:
                row = con.execute(
                    """SELECT hex, epoch_ms(reported_ts), callsign, registration, aircraft_type,
                              lat, lon, altitude_ft, speed_kmh, track_deg, vertical_rate_fpm,
                              on_ground, squawk
                       FROM aircraft_positions
                       WHERE hex = ?
                       ORDER BY reported_ts DESC LIMIT 1""",
                    [hx],
                ).fetchone()
        except (duckdb.CatalogException, Exception):  # noqa: BLE001
            return None
        if row is None:
            return None
        hx, rep_ms, callsign, reg, ac_type, lat, lon, alt_ft, spd_kmh, trk, vrate, on_g, sq = row
        rep_s = int(rep_ms // 1000)
        return {
            "id": f"ac-{hx}",
            "hex": hx,
            "callsign": callsign or reg or hx.upper(),
            "flight": callsign,
            "registration": reg,
            "type": ac_type,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "altitude_ft": alt_ft,
            "altitude_m": round(alt_ft * 0.3048) if alt_ft is not None else None,
            "on_ground": bool(on_g),
            "ground_speed_kt": round(spd_kmh / 1.852, 1) if spd_kmh is not None else None,
            "speed_kmh": round(spd_kmh, 1) if spd_kmh is not None else None,
            "track_deg": trk,
            "heading": trk,
            "vertical_rate_fpm": vrate,
            "squawk": sq,
            "distance_km": None,
            "seen_s": None,
            "reported": rep_s,
        }

"""Measured weather and reference air: what real instruments say, beside the twin's models.

- The Department of Meteorology's automatic weather stations: one XML file for the island,
  new readings every 10 minutes (about 10 minutes behind). The five within 15 km of the centre
  are kept: Lefkosia in the centre, Athalassa and its park's visitor centre, KEPNIC (a rain
  gauge) and Mammari (the nearest sun meter, 13 km west). Air temperature at 1.2 m and at 5 cm,
  humidity, rain, wind, sunshine (global radiation) and pressure, by station.
- The national network's two Nicosia air stations through the European Environment Agency's
  up-to-date files: hourly means of NO2, O3 and PM10, one Parquet file per station and
  pollutant, two to three hours behind and not yet verified. CY0004A is the traffic station
  (Strovolos police station), CY0006A the residential one (Apostolos Loukas school).

`Measured` reads both with conditional requests (a file comes down again only once it has
changed), keeps a copy on disk, holds the newest readings, and records every reading in the
twin's memory (its own table, `station_readings`) so the city clock replays them. Reads never
ask a public service: only the poller does (`start()`), the Met file every 10 minutes and the
EEA files every 20 minutes. Each EEA file holds the year so far, so a gap while the twin was off
is filled at its next read; the Met file holds only now, so its history grows while the twin
runs. See docs/measured-weather-air.md.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from chatbot.geo import haversine_m
from chatbot.settings import CACHE_DIR, TIMEZONE, Settings

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)
UTC1 = timezone(timedelta(hours=1))  # the EEA gives hourly data in UTC+1, all year

MET_URL = "https://www.dom.org.cy/AWS/OpenData/CyDoM.xml"
MET_PAGE = "https://www.dom.org.cy/"
EEA_LIST_URL = "https://eeadmz1-downloads-api-appservice.azurewebsites.net/ParquetFile/urls"
EEA_BLOB = "https://eeadmz1batchservice02.blob.core.windows.net/airquality-p/CY"
EEA_PAGE = "https://eeadmz1-downloads-webapp.azurewebsites.net/"
MET_EVERY_S = 600
EEA_EVERY_S = 1200
EEA_LIST_EVERY_S = 24 * 3600
POLL_TICK_S = 30
CENTRE = (35.1700, 33.3600)  # (lat, lon): Eleftheria Square
MET_REACH_M = 15_000
EEA_KEEP_DAYS = 35  # how far back an EEA file is read into the memory the first time
EEA_REREAD_DAYS = 3  # afterwards: unverified hours can still change
KNOT_MS = 0.514444
# How long a reading speaks for after its time: a 10-minute reading for half an hour, an hourly
# mean for three hours (the EEA files run two to three hours behind now).
LOOKBACK = {"weather_station": timedelta(minutes=30), "reference_air_station": timedelta(hours=3)}
# The newest an hourly mean may be to anchor a surface at a moment: its own hour or the one
# before. Older, it says what the air was, not what it is.
SURFACE_LAG = timedelta(hours=2)
MET_SURFACE_LAG = timedelta(minutes=30)
COMPARE_DAYS = 7
COMPARE_MIN_HOURS = 12
COMPARE_TTL_S = 1800

CREDIT_MET = ("Weather Data Copyright The Cyprus Department of Meteorology. This information is "
              "intended as a guide only.")

# The Met stations around Nicosia: code -> (name, what it is). Coordinates come from the file.
MET_NAMES = {
    "LEFKOSIA": ("Lefkosia", "The Department's station in the centre of Nicosia"),
    "ATHALASSA": ("Athalassa", "On the edge of Athalassa park, south-east Nicosia"),
    "VISITOR": ("Athalassa visitor centre", "By the visitor centre of Athalassa park"),
    "KEPNIC": ("KEPNIC", "A rain gauge in west Nicosia"),
    "MAMMARI": ("Mammari", "13 km west of the centre: the nearest station that measures the sun"),
}
MET_FALLBACK = {  # where the file put them on 27 November 2026, for when it can't be read
    "LEFKOSIA": (35.1643667, 33.3560722), "ATHALASSA": (35.140957, 33.396579),
    "VISITOR": (35.144477, 33.403387), "KEPNIC": (35.1523889, 33.3122806),
    "MAMMARI": (35.172358, 33.2148785),
}
# The file's reading names -> the twin's keys.
MET_KEYS = {
    "Air Temperature (1.2m)": "temperature_c",
    "Air Temperature (5cm)": "ground_c",
    "Relative Humidity (1.2m)": "humidity_pct",
    "Accumulated Rainfall (10 min.)": "rain_10min_mm",
    "Accumulated Rainfall (24 hours)": "rain_24h_mm",
    "Wind Speed (10m)": "wind_ms",
    "Wind Speed (2m)": "wind_2m_ms",
    "Wind Direction (10m)": "wind_from_deg",
    "Global Radiation": "radiation_wm2",
    "Direct Solar Radiation": "direct_radiation_wm2",
    "Atmospheric Pressure (Station Level)": "pressure_hpa",
    "Atmospheric Pressure (Mean Sea Level)": "pressure_msl_hpa",
    "Extreme Day Max. Temp.": "day_max_c",
    "Extreme Day Min. Temp.": "day_min_c",
}
# The EEA's pollutant codes the twin reads.
POLLUTANTS = {8: "no2", 7: "o3", 5: "pm10", 6001: "pm2_5"}
EEA_DEFAULT_FILES = ("SPO-CY0004A_00008_100", "SPO-CY0004A_00007_100", "SPO-CY0004A_00005_100",
                     "SPO-CY0006A_00008_100", "SPO-CY0006A_00007_100")

# What each key is, for readouts: label, unit, decimals.
READING_INFO: dict[str, tuple[str, str, int]] = {
    "temperature_c": ("Air temperature", "°C", 1),
    "ground_c": ("Ground at 5 cm", "°C", 1),
    "humidity_pct": ("Humidity", "%", 0),
    "rain_10min_mm": ("Rain in the last 10 minutes", "mm", 1),
    "rain_24h_mm": ("Rain in the last 24 hours", "mm", 1),
    "wind_ms": ("Wind at 10 m", "m/s", 1),
    "wind_2m_ms": ("Wind at 2 m", "m/s", 1),
    "wind_from_deg": ("Wind from", "°", 0),
    "radiation_wm2": ("Sunshine (global radiation)", "W/m²", 0),
    "direct_radiation_wm2": ("Direct sunshine", "W/m²", 0),
    "pressure_hpa": ("Pressure at the station", "hPa", 1),
    "pressure_msl_hpa": ("Pressure at sea level", "hPa", 1),
    "day_max_c": ("Highest today", "°C", 1),
    "day_min_c": ("Lowest today", "°C", 1),
    "no2": ("NO₂", "µg/m³", 1),
    "o3": ("O₃", "µg/m³", 1),
    "pm10": ("PM10", "µg/m³", 1),
    "pm2_5": ("PM2.5", "µg/m³", 1),
}
# The order readings are told in, the headline first.
WEATHER_ORDER = ("temperature_c", "humidity_pct", "rain_10min_mm", "rain_24h_mm", "wind_ms",
                 "wind_from_deg", "wind_2m_ms", "radiation_wm2", "direct_radiation_wm2", "ground_c",
                 "day_max_c", "day_min_c", "pressure_hpa", "pressure_msl_hpa")
AIR_ORDER = ("no2", "pm10", "pm2_5", "o3")
# What a city sensor's raw keys are in the twin's memory (collector/interpret.py reads the same).
CITY_KEYS = {"no2": "NO2", "o3": "O3", "pm10": "PM10", "pm2_5": "PM2.5", "temperature_c": "T"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_readings (
    station VARCHAR NOT NULL,        -- met-LEFKOSIA, aq-CY0004A
    key VARCHAR NOT NULL,            -- temperature_c, rain_10min_mm, no2, ...
    ts TIMESTAMPTZ NOT NULL,         -- the reading's time; an hourly mean's start
    value DOUBLE,
    source VARCHAR,                  -- met | eea
    fetched_at TIMESTAMPTZ,
    PRIMARY KEY (station, key, ts)
);
"""


@dataclass(frozen=True)
class StationInfo:
    id: str
    kind: str  # weather_station | reference_air_station
    code: str
    name: str
    lat: float
    lon: float
    detail: str
    source: str  # met_stations | eea_air
    type: str = ""  # traffic | residential (air stations)


AIR_STATIONS: dict[str, StationInfo] = {
    "CY0004A": StationInfo("aq-CY0004A", "reference_air_station", "CY0004A", "Nicosia traffic station",
                           35.15194, 33.34778, "At the Strovolos police station, beside the road",
                           "eea_air", "traffic"),
    "CY0006A": StationInfo("aq-CY0006A", "reference_air_station", "CY0006A", "Nicosia residential station",
                           35.12694, 33.33167, "In the yard of the Apostolos Loukas primary school, Strovolos",
                           "eea_air", "residential"),
}

Reading = tuple[str, str, int, float]  # (station id, key, ts ms, value)


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def met_id(code: str) -> str:
    return f"met-{code}"


# -- parsing -------------------------------------------------------------------------------------


def _float(text: str | None) -> float | None:
    try:
        v = float((text or "").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def met_stamp(text: str | None) -> datetime | None:
    """'2026-11-27 22:00 (Local Time)' -> 22:00 in Nicosia."""
    raw = (text or "").split("(")[0].strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return None


def parse_met(xml_text: str | bytes, reach_m: float = MET_REACH_M
              ) -> tuple[dict[str, StationInfo], list[Reading]]:
    """The Met stations within `reach_m` of the centre and their readings, from CyDoM.xml.
    Wind at 10 m comes in knots and is turned into m/s; a reading the file can't give a number
    for is left out."""
    root = ET.fromstring(xml_text)
    stations: dict[str, StationInfo] = {}
    for s in root.iterfind("stations/station"):
        code = (s.findtext("station_code") or "").strip()
        lat, lon = _float(s.findtext("station_latitude")), _float(s.findtext("station_longitude"))
        if not code or lat is None or lon is None:
            continue
        if haversine_m(CENTRE[0], CENTRE[1], lat, lon) > reach_m:
            continue
        name, detail = MET_NAMES.get(code, (code.replace("_", " ").title(), "A Department of Meteorology station"))
        stations[code] = StationInfo(met_id(code), "weather_station", code, name, lat, lon, detail, "met_stations")
    readings: list[Reading] = []
    for obs in root.iterfind("observations"):
        code = (obs.findtext("station_code") or "").strip()
        when = met_stamp(obs.findtext("date_time"))
        if code not in stations or when is None:
            continue
        for o in obs.iterfind("observation"):
            key = MET_KEYS.get((o.findtext("observation_name") or "").strip())
            value = _float(o.findtext("observation_value"))
            if key is None or value is None:
                continue
            unit = (o.findtext("observation_unit") or "").strip().lower()
            if unit in ("knots", "knot", "kt"):
                value = round(value * KNOT_MS, 2)
            readings.append((met_id(code), key, _ms(when), value))
    return stations, readings


def parse_eea_listing(text: str) -> list[str]:
    """The file names (SPO-CY0004A_00008_100) of the Nicosia stations' pollutants the twin
    reads, from the download service's CSV of URLs."""
    out = []
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        url = (row[0] if row else "").strip()
        if not url.lower().startswith("http"):
            continue
        name = url.rsplit("/", 1)[-1].removesuffix(".parquet")
        parts = name.removeprefix("SPO-").split("_")
        if len(parts) < 2 or parts[0] not in AIR_STATIONS:
            continue
        try:
            pollutant = int(parts[1])
        except ValueError:
            continue
        if pollutant in POLLUTANTS:
            out.append(name)
    return sorted(set(out))


def file_station(name: str) -> tuple[StationInfo, str] | None:
    """SPO-CY0004A_00008_100 -> (the traffic station, "no2")."""
    parts = name.removeprefix("SPO-").split("_")
    try:
        st, pol = AIR_STATIONS.get(parts[0]), POLLUTANTS.get(int(parts[1]))
    except (IndexError, ValueError):
        return None
    return (st, pol) if st and pol else None


def parse_eea_parquet(path: Path | str, since: datetime | None = None) -> list[Reading]:
    """Valid hourly means from one EEA file (Validity above 0), each at its hour's start."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            """SELECT Samplingpoint, Pollutant, Start, Value::DOUBLE FROM read_parquet(?)
               WHERE Validity > 0 AND Value IS NOT NULL AND AggType = 'hour'
               ORDER BY Start""", [str(path)]).fetchall()
    finally:
        con.close()
    out: list[Reading] = []
    for point, pollutant, start, value in rows:
        st = AIR_STATIONS.get(str(point).rsplit("/", 1)[-1].removeprefix("SPO-").split("_")[0])
        key = POLLUTANTS.get(int(pollutant))
        if st is None or key is None or value is None or not math.isfinite(value) or value < 0:
            continue
        t = start.replace(tzinfo=UTC1) if start.tzinfo is None else start
        if since is not None and t < since:
            continue
        out.append((st.id, key, _ms(t), round(float(value), 2)))
    return out


# -- HTTP ----------------------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]


def http(url: str, *, user_agent: str, headers: dict[str, str] | None = None,
         data: bytes | None = None, timeout: float = 30.0) -> Response:
    """One request: a 304 comes back as a Response, other HTTP errors raise."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": user_agent, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Response(304, b"", {k.lower(): v for k, v in (e.headers or {}).items()})
        host = urllib.parse.urlsplit(url).netloc
        raise RuntimeError(f"{host} answered HTTP {e.code}") from None


# -- the service ---------------------------------------------------------------------------------


@dataclass
class FeedStatus:
    read_at: float | None = None  # when it was last asked (epoch s)
    ok_at: float | None = None
    error: str | None = None
    newest_ms: int | None = None  # the newest reading it gave
    files: int = 0


@dataclass
class _Latest:
    by_station: dict[str, dict[str, tuple[int, float]]] = field(default_factory=dict)

    def add(self, readings: list[Reading]) -> None:
        for sid, key, ts, value in readings:
            cur = self.by_station.setdefault(sid, {}).get(key)
            if cur is None or ts >= cur[0]:
                self.by_station[sid][key] = (ts, value)


class Measured:
    """The measuring stations around Nicosia: who they are, what they read now or at a moment,
    their history in the twin's memory, and how the city's own sensors compare with them."""

    def __init__(self, settings: Settings, memory=None, *, http_get: Callable[..., Response] = http,
                 cache_dir: Path | None = None, now: Callable[[], datetime] | None = None):
        self.s = settings
        self.memory = memory
        self._http = http_get
        self.cache = cache_dir or CACHE_DIR / "stations"
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._latest = _Latest()
        self._met: dict[str, StationInfo] = {}
        self._pending: list[tuple[Reading, str]] = []
        self._etags: dict[str, str] = {}
        self._eea_files: list[str] = list(EEA_DEFAULT_FILES)
        self._eea_seen: set[str] = set()  # files read into the memory once already
        self.met_status = FeedStatus()
        self.eea_status = FeedStatus()
        self._eea_list_at = 0.0
        self._compare: dict[tuple, tuple[float, dict]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def now(self) -> datetime:
        return self._now()

    # -- who they are --------------------------------------------------------------------------

    def stations(self, kind: str | None = None) -> list[StationInfo]:
        with self._lock:
            met = dict(self._met)
        if not met:  # the file not read yet: where they stood
            met = {c: StationInfo(met_id(c), "weather_station", c, MET_NAMES[c][0], la, lo, MET_NAMES[c][1],
                                  "met_stations") for c, (la, lo) in MET_FALLBACK.items()}
        out = list(met.values()) + list(AIR_STATIONS.values())
        return [s for s in out if kind is None or s.kind == kind]

    def station(self, sid: str) -> StationInfo | None:
        sid = sid.strip()
        return next((s for s in self.stations() if s.id.lower() == sid.lower()), None)

    # -- reading the feeds (the poller only) ---------------------------------------------------

    def refresh_met(self) -> int:
        """Read the Met file (only if it changed) and record its readings. Returns how many."""
        st = self.met_status
        st.read_at = time.time()
        headers = {"Accept": "application/xml"}
        if self._etags.get("met"):
            headers["If-None-Match"] = self._etags["met"]
        try:
            resp = self._http(MET_URL, user_agent=self.s.user_agent, headers=headers, timeout=30)
            if resp.status == 304:
                st.ok_at, st.error = time.time(), None
                return 0
            stations, readings = parse_met(resp.body)
        except Exception as e:  # noqa: BLE001 - say so; the last readings stand
            st.error = f"{type(e).__name__}: {e}"[:300]
            log.info("met stations: %s", st.error)
            return 0
        if resp.headers.get("etag"):
            self._etags["met"] = resp.headers["etag"]
        self._save("met.xml", resp.body)
        with self._lock:
            if stations:
                self._met = stations
            self._latest.add(readings)
        st.ok_at, st.error = time.time(), None
        st.newest_ms = max((r[2] for r in readings), default=st.newest_ms)
        self._record(readings, "met")
        return len(readings)

    def refresh_eea(self) -> int:
        """Read the EEA files that changed and record their hours. Returns how many hours."""
        st = self.eea_status
        st.read_at = time.time()
        if time.time() - self._eea_list_at > EEA_LIST_EVERY_S:
            self._eea_list_at = time.time()
            try:
                body = json_body({"countries": ["CY"], "cities": ["Lefkosia"], "pollutants": [],
                                  "dataset": 1, "source": "Api"})
                resp = self._http(EEA_LIST_URL, user_agent=self.s.user_agent, data=body, timeout=30,
                                  headers={"Content-Type": "application/json", "Accept": "text/csv"})
                files = parse_eea_listing(resp.body.decode("utf-8", "replace"))
                if files:
                    self._eea_files = files
            except Exception as e:  # noqa: BLE001 - the known files stand
                log.info("EEA listing: %s", e)
        total, errors, ok = 0, [], 0
        for name in self._eea_files:
            headers = {"If-None-Match": self._etags[name]} if self._etags.get(name) else {}
            try:
                resp = self._http(f"{EEA_BLOB}/{name}.parquet", user_agent=self.s.user_agent,
                                  headers=headers, timeout=60)
                ok += 1
                if resp.status == 304:
                    continue
                path = self._save(f"eea/{name}.parquet", resp.body)
                days = EEA_REREAD_DAYS if name in self._eea_seen else EEA_KEEP_DAYS
                readings = parse_eea_parquet(path, self.now() - timedelta(days=days))
            except Exception as e:  # noqa: BLE001 - one file never hides the others
                errors.append(f"{name}: {e}"[:200])
                continue
            if resp.headers.get("etag"):
                self._etags[name] = resp.headers["etag"]
            self._eea_seen.add(name)
            with self._lock:
                self._latest.add(readings)
            st.newest_ms = max([st.newest_ms or 0, *(r[2] for r in readings)]) or None
            total += len(readings)
            self._record(readings, "eea")
        st.files = ok
        st.error = "; ".join(errors) if errors else None
        if ok:
            st.ok_at = time.time()
        return total

    def load_cached(self) -> None:
        """The copies on disk, so the stations show at once after a restart."""
        try:
            met = self.cache / "met.xml"
            if met.exists():
                stations, readings = parse_met(met.read_bytes())
                with self._lock:
                    self._met = stations or self._met
                    self._latest.add(readings)
            for name in self._eea_files:
                p = self.cache / "eea" / f"{name}.parquet"
                if p.exists():
                    readings = parse_eea_parquet(p, self.now() - timedelta(days=1))
                    with self._lock:
                        self._latest.add(readings)
        except Exception as e:  # noqa: BLE001 - the poller reads them soon anyway
            log.info("stations: the copies on disk couldn't be read: %s", e)

    def _save(self, rel: str, body: bytes) -> Path:
        path = self.cache / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        except OSError as e:
            log.info("stations: couldn't keep %s: %s", rel, e)
        return path

    # -- the poller ----------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self.load_cached()
        self._thread = threading.Thread(target=self._run, name="stations", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        met_due = eea_due = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now >= met_due:
                    met_due = now + MET_EVERY_S
                    self.refresh_met()
                if now >= eea_due:
                    eea_due = now + EEA_EVERY_S
                    self.refresh_eea()
                if self._pending:
                    self._flush()
            except Exception:  # noqa: BLE001 - the thread outlives any one read
                log.exception("stations: a read failed")
            self._stop.wait(POLL_TICK_S)

    # -- the twin's memory ---------------------------------------------------------------------

    def _record(self, readings: list[Reading], source: str) -> None:
        if self.memory is None or not readings or not getattr(self.memory, "recorder", None):
            return
        with self._lock:
            self._pending.extend((r, source) for r in readings)
            if len(self._pending) > 200_000:
                del self._pending[: len(self._pending) - 200_000]
        self._flush()

    def _flush(self) -> bool:
        from collector import db

        with self._lock:
            batch, self._pending = self._pending, []
        if not batch:
            return True
        now_us = int(time.time() * 1e6)
        try:
            with self.memory.writing() as con:
                con.execute(SCHEMA)
                db.bulk(con, {"station": "VARCHAR", "key": "VARCHAR", "ts": "BIGINT", "value": "DOUBLE",
                              "source": "VARCHAR"},
                        [(sid, key, ts * 1000, value, src) for (sid, key, ts, value), src in batch],
                        f"""INSERT OR REPLACE INTO station_readings
                            SELECT station, key, make_timestamptz(ts), value, source,
                                   make_timestamptz({now_us}) FROM __SRC__""")
            return True
        except Exception as e:  # noqa: BLE001 - kept for the next try
            log.info("stations: the memory is busy (%s); trying again later", e)
            with self._lock:
                self._pending = batch + self._pending
            return False

    def stored(self, stations: list[str], keys: list[str] | tuple[str, ...] | None,
               start: datetime, end: datetime) -> list[tuple[str, str, int, float]]:
        """Readings in the memory between two moments (and the newest ones held here)."""
        rows: list[tuple[str, str, int, float]] = []
        if self.memory is not None and getattr(self.memory, "exists", lambda: False)():
            sql = """SELECT station, key, epoch_ms(ts), value FROM station_readings
                     WHERE list_contains(?, station)
                       AND ts BETWEEN make_timestamptz(? * 1000) AND make_timestamptz(? * 1000)"""
            params: list = [list(stations), _ms(start), _ms(end)]
            if keys is not None:
                sql += " AND list_contains(?, key)"
                params.append(list(keys) or [""])
            try:
                with self.memory.reading() as con:
                    rows = [tuple(r) for r in con.execute(sql + " ORDER BY ts", params).fetchall()]
            except duckdb.CatalogException:
                rows = []  # nothing recorded yet
            except Exception as e:  # noqa: BLE001 - the memory is busy or missing: say less, not wrong
                log.info("stations: the memory couldn't be read: %s", e)
                rows = []
        seen = {(r[0], r[1], r[2]) for r in rows}
        lo, hi = _ms(start), _ms(end)
        with self._lock:
            for sid in stations:
                for key, (ts, v) in self._latest.by_station.get(sid, {}).items():
                    if (keys is None or key in keys) and lo <= ts <= hi and (sid, key, ts) not in seen:
                        rows.append((sid, key, ts, v))
        return sorted(rows, key=lambda r: r[2])

    # -- what they read --------------------------------------------------------------------------

    def readings_at(self, at: datetime | None, kind: str | None = None) -> list[dict]:
        """Each station with its readings at a moment (None: now): the newest of each within
        its lookback, with its time and age. A station with nothing then is listed, unknown."""
        when = at or self.now()
        infos = self.stations(kind)
        longest = max(LOOKBACK.values())
        if at is None:
            with self._lock:
                latest = {sid: dict(v) for sid, v in self._latest.by_station.items()}
            rows = [(sid, key, ts, v) for sid, kv in latest.items() for key, (ts, v) in kv.items()]
            if not rows:  # after a restart, before the first read: the memory's newest
                rows = self.stored([s.id for s in infos], None, when - longest, when)
        else:
            rows = self.stored([s.id for s in infos], None, when - longest, when)
        newest: dict[tuple[str, str], tuple[int, float]] = {}
        for sid, key, ts, v in rows:
            if ts <= _ms(when) + 60_000 and ((sid, key) not in newest or ts > newest[(sid, key)][0]):
                newest[(sid, key)] = (ts, v)
        out = []
        for s in infos:
            back = _ms(when - LOOKBACK[s.kind])
            vals = {k: (ts, v) for (sid, k), (ts, v) in newest.items() if sid == s.id and ts >= back}
            out.append(self._row(s, vals, when))
        return out

    def _row(self, s: StationInfo, vals: dict[str, tuple[int, float]], when: datetime) -> dict:
        order = WEATHER_ORDER if s.kind == "weather_station" else AIR_ORDER
        readings = {}
        for k in [*order, *sorted(set(vals) - set(order))]:
            if k not in vals:
                continue
            ts, v = vals[k]
            label, unit, dec = READING_INFO.get(k, (k, "", 1))
            item = {"value": round(v, dec) if dec else round(v), "unit": unit, "label": label,
                    "as_of": _local(ts), "age_min": max(0, round((_ms(when) - ts) / 60000))}
            if s.kind == "reference_air_station":
                item["until"] = _local(ts + 3_600_000)  # an hourly mean: the hour it covers
            readings[k] = item
        if "wind_ms" in readings:
            readings["wind_ms"]["kmh"] = round(readings["wind_ms"]["value"] * 3.6)
        newest = max((ts for ts, _ in vals.values()), default=None)
        return {"id": s.id, "kind": s.kind, "code": s.code, "name": s.name, "detail": s.detail,
                "lat": s.lat, "lon": s.lon, "type": s.type or None, "source": s.source,
                "known": bool(readings), "readings": readings,
                "as_of": _local(newest) if newest else None,
                "age_min": max(0, round((_ms(when) - newest) / 60000)) if newest else None}

    def series(self, sid: str, key: str, end: datetime | None, hours: int = 24) -> list[tuple[int, float]]:
        until = end or self.now()
        return [(ts, v) for _, _, ts, v in self.stored([sid], [key], until - timedelta(hours=hours), until)]

    def rain_last_hour(self, at: datetime | None) -> dict[str, float]:
        """The rain each Met station measured in the hour up to a moment (its six readings)."""
        when = at or self.now()
        ids = [s.id for s in self.stations("weather_station")]
        sums: dict[str, list[float]] = {}
        for sid, _, _, v in self.stored(ids, ["rain_10min_mm"], when - timedelta(minutes=59), when):
            sums.setdefault(sid, []).append(v)
        return {sid: round(sum(vs), 1) for sid, vs in sums.items()}

    def for_surface(self, metric: str, at: datetime | None) -> tuple[list[dict], list[dict]]:
        """Stations that can anchor a surface of `metric` at a moment: (used, left out with
        why). An hourly mean anchors only its own hour or the next; a Met reading half an hour."""
        when = at or self.now()
        used, left = [], []
        for row in self.readings_at(at):
            r = row["readings"].get(metric)
            if r is None:
                continue
            lag = MET_SURFACE_LAG if row["kind"] == "weather_station" else SURFACE_LAG
            ts = datetime.fromisoformat(r["as_of"]).replace(tzinfo=TZ)
            item = {"id": row["id"], "name": row["name"], "lat": row["lat"], "lon": row["lon"],
                    metric: r["value"], "as_of": r["as_of"], "reference": True, "kind": row["kind"]}
            if when - ts <= lag:
                used.append(item)
            else:
                left.append({**item, "why": f"its newest reading ({_hour_text(r, row['kind'])}) is too old "
                                            f"for {'now' if at is None else 'that moment'}"})
        return used, left

    # -- how the city's sensors compare ----------------------------------------------------------

    def nearest(self, lat: float, lon: float, metric: str, kinds: tuple[str, ...] | None = None
                ) -> tuple[StationInfo, float] | None:
        """The nearest station that measures `metric` (Met stations measure temperature)."""
        best = None
        with self._lock:
            latest = {sid: dict(kv) for sid, kv in self._latest.by_station.items()}
        for s in self.stations():
            if kinds and s.kind not in kinds:
                continue
            if not _measures(s, metric, latest.get(s.id, {})):
                continue
            d = haversine_m(lat, lon, s.lat, s.lon)
            if best is None or d < best[1]:
                best = (s, d)
        return best

    def compare(self, sensors: list[tuple[str, str, float, float]], metrics: tuple[str, ...] = ("no2", "o3", "pm10", "temperature_c"),
                days: int = COMPARE_DAYS, end: datetime | None = None) -> list[dict]:
        """Each city sensor (id, name, lat, lon) against its nearest reference station for each
        metric, hour by hour over `days` up to `end`: bias, bias in %, RMSE, correlation, the
        hours paired and how far apart they are. From the twin's memory only."""
        until = (end or self.now()).replace(minute=0, second=0, microsecond=0)
        key = (tuple(s[0] for s in sensors), metrics, days, _ms(until) // 3_600_000)
        with self._lock:
            hit = self._compare.get(key)
            if hit and time.time() - hit[0] < COMPARE_TTL_S:
                return hit[1]
        start = until - timedelta(days=days)
        city = self._city_hourly([s[0] for s in sensors], metrics, start, until)
        refs: dict[str, StationInfo] = {}
        pairs_for: list[tuple[tuple, str, StationInfo, float]] = []
        for sensor in sensors:
            for m in metrics:
                near = self.nearest(sensor[2], sensor[3], m,
                                    ("weather_station",) if m == "temperature_c" else ("reference_air_station",))
                if near:
                    refs[near[0].id] = near[0]
                    pairs_for.append((sensor, m, near[0], near[1]))
        ref_hourly = self._ref_hourly(list(refs), metrics, start, until)
        out = []
        for sensor, m, ref, dist in pairs_for:
            a = city.get((sensor[0], m), {})
            b = ref_hourly.get((ref.id, m), {})
            hours = sorted(set(a) & set(b))
            out.append(_stats(sensor, m, ref, dist, [(h, a[h], b[h]) for h in hours], days))
        with self._lock:
            self._compare[key] = (time.time(), out)
            if len(self._compare) > 64:
                self._compare.pop(next(iter(self._compare)))
        return out

    def pairs(self, sensor: tuple[str, str, float, float], metric: str, days: int = COMPARE_DAYS,
              end: datetime | None = None) -> tuple[StationInfo | None, list[tuple[int, float, float]]]:
        """One sensor's hours beside its nearest reference: (the station, [(hour ms, sensor,
        reference)])."""
        until = (end or self.now()).replace(minute=0, second=0, microsecond=0)
        start = until - timedelta(days=days)
        near = self.nearest(sensor[2], sensor[3], metric,
                            ("weather_station",) if metric == "temperature_c" else ("reference_air_station",))
        if near is None:
            return None, []
        a = self._city_hourly([sensor[0]], (metric,), start, until).get((sensor[0], metric), {})
        b = self._ref_hourly([near[0].id], (metric,), start, until).get((near[0].id, metric), {})
        return near[0], [(h, a[h], b[h]) for h in sorted(set(a) & set(b))]

    def _city_hourly(self, names: list[str], metrics: tuple[str, ...], start: datetime, end: datetime
                     ) -> dict[tuple[str, str], dict[int, float]]:
        keys = [CITY_KEYS[m] for m in metrics if m in CITY_KEYS]
        back = {v: k for k, v in CITY_KEYS.items()}
        if self.memory is None or not keys:
            return {}
        try:
            recs = self.memory.readings(names, keys, _ms(start), _ms(end))
        except Exception as e:  # noqa: BLE001 - no memory, no comparison
            log.info("stations: city sensors' history unavailable: %s", e)
            return {}
        acc: dict[tuple[str, str], dict[int, list[float]]] = {}
        for name, rs in recs.items():
            for r in rs:
                v = _float(str(r.get("value")))
                if v is None or v < -50:
                    continue
                h = int(r["timestamp"]) // 3_600_000 * 3_600_000
                acc.setdefault((name, back[r["key"]]), {}).setdefault(h, []).append(v)
        return {k: {h: statistics.fmean(vs) for h, vs in hv.items()} for k, hv in acc.items()}

    def _ref_hourly(self, ids: list[str], metrics: tuple[str, ...], start: datetime, end: datetime
                    ) -> dict[tuple[str, str], dict[int, float]]:
        acc: dict[tuple[str, str], dict[int, list[float]]] = {}
        for sid, key, ts, v in self.stored(ids, list(metrics), start, end):
            acc.setdefault((sid, key), {}).setdefault(ts // 3_600_000 * 3_600_000, []).append(v)
        return {k: {h: statistics.fmean(vs) for h, vs in hv.items()} for k, hv in acc.items()}

    # -- health, for the sources panel -----------------------------------------------------------

    def health(self, which: str) -> FeedStatus:
        return self.met_status if which == "met" else self.eea_status


def _measures(s: StationInfo, metric: str, latest: dict) -> bool:
    if s.kind == "weather_station":
        if metric != "temperature_c":
            return False
        return "temperature_c" in latest or (not latest and s.code != "KEPNIC")
    files = [n for n in EEA_DEFAULT_FILES if n.startswith(f"SPO-{s.code}_")]
    return metric in latest or any(file_station(n) and file_station(n)[1] == metric for n in files)


def _hour_text(r: dict, kind: str) -> str:
    if kind == "reference_air_station":
        return f"{r['as_of'][11:16]} to {r.get('until', '')[11:16]}"
    return r["as_of"][11:16]


def _stats(sensor: tuple, metric: str, ref: StationInfo, dist: float,
           pairs: list[tuple[int, float, float]], days: int) -> dict:
    label, unit, dec = READING_INFO.get(metric, (metric, "", 1))
    out = {"sensor_id": sensor[0], "sensor": sensor[1], "metric": metric, "label": label, "unit": unit,
           "reference_id": ref.id, "reference": ref.name, "distance_m": round(dist), "hours": len(pairs),
           "days": days}
    if len(pairs) < COMPARE_MIN_HOURS:
        out["note"] = (f"only {len(pairs)} hour{'s' if len(pairs) != 1 else ''} in the twin's memory with "
                       f"both readings, too few to say")
        return out
    a = [p[1] for p in pairs]
    b = [p[2] for p in pairs]
    diffs = [x - y for x, y in zip(a, b)]
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    out |= {"sensor_mean": round(mean_a, dec), "reference_mean": round(mean_b, dec),
            "bias": round(statistics.fmean(diffs), dec),
            "rmse": round(math.sqrt(statistics.fmean([d * d for d in diffs])), dec)}
    if metric != "temperature_c" and mean_b > 0.5:
        out["bias_pct"] = round(100 * (mean_a / mean_b - 1))
    if len(pairs) >= 3 and statistics.pstdev(a) > 0 and statistics.pstdev(b) > 0:
        out["r"] = round(statistics.correlation(a, b), 2)
    out["text"] = agreement_text(out)
    return out


def agreement_text(c: dict) -> str:
    """'reads 18% above the traffic station over 7 days (120 hours, 640 m away)'."""
    if "bias" not in c:
        return c.get("note") or "not compared"
    span = f"over {c['days']} days ({c['hours']} hours, {_dist(c['distance_m'])} away)"
    if "bias_pct" in c:
        p = c["bias_pct"]
        how = "agrees with" if abs(p) < 10 else f"reads {abs(p)}% {'above' if p > 0 else 'below'}"
    else:
        b = c["bias"]
        how = "agrees with" if abs(b) < 0.5 else f"reads {abs(b):g} {c['unit']} {'above' if b > 0 else 'below'}"
    return f"{how} {_the(c['reference'])} {span}"


def _the(name: str) -> str:
    return name if not name.lower().startswith("nicosia ") else "the " + name[8:]


def _dist(m: float) -> str:
    return f"{m:,.0f} m" if m < 1000 else f"{m / 1000:.1f} km"


def json_body(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")

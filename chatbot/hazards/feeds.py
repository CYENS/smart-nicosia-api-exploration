"""The three hazard feeds, read into one shape (`Hazard`), and the places they are told against.

- Earthquakes: EMSC's FDSN event service, which carries the Cyprus Geological Survey
  Department's own solutions (`auth` "GSD"), and EMSC's felt reports (testimonies): how many people
  said they felt a quake, and where (positions rounded for privacy, each with an intensity).
- Weather warnings: Meteoalarm's feed for Cyprus, issued by the Department of Meteorology, in CAP
  (event, awareness level and type, area, onset, expiry and the warning's own words). The JSON API
  carries the words; the legacy Atom feed is the fallback and has the essentials.
- Fire danger: EFFIS's fire weather index forecast (`mf010.fwi`), read from its map image over the
  island, since its feature info answers with empty values: each pixel's colour is a danger class.

Where: the island and its six districts (data/cyprus_areas.json, geoBoundaries from
OpenStreetMap), so a quake is "off Paphos, 91 km south-west of Nicosia" and the district's fire
danger is read over the district itself.

How loud each hazard is (the watch's thresholds) lives here too, in one place: `quake_rules`,
`WARNING_SEVERITY` and `FIRE_SEVERITY`. Parsers are pure functions of the payloads, tested on
fixtures of real past events (tests/fixtures/hazards).
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from chatbot.dls import centroid, contains
from chatbot.geo import haversine_m
from chatbot.settings import NICOSIA_CENTER, ROOT, TIMEZONE

TZ = ZoneInfo(TIMEZONE)
NICOSIA = NICOSIA_CENTER  # (lat, lon): the walled city's centre, the map's home
AREAS_PATH = ROOT / "data" / "cyprus_areas.json"

EMSC_URL = "https://www.seismicportal.eu/fdsnws/event/1/query"
EMSC_BOX = {"minlat": 34.4, "maxlat": 35.8, "minlon": 32.0, "maxlon": 34.7}  # Cyprus and its seas
FELT_URL = "https://www.seismicportal.eu/testimonies-ws/api/search"
METEOALARM_API = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-cyprus"
METEOALARM_ATOM = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-cyprus"
EFFIS_WMS = "https://maps.effis.emergency.copernicus.eu/effis"
FIRE_BBOX = (32.2, 34.5, 34.7, 35.75)  # lon_min, lat_min, lon_max, lat_max: the island
FIRE_SIZE = (250, 125)  # pixels: about 1 km each


@dataclass
class Hazard:
    id: str  # eq-20251112_0000180 | wx-cy-240813170521-maxtemp-000 | fire-2025-07-23
    kind: str  # earthquake | warning | fire
    source: str  # emsc | meteoalarm | effis
    start: datetime  # the quake's origin time | the warning's onset | the day's start
    end: datetime | None  # None for a quake | the warning's expiry | the day's end
    title: str
    lat: float | None = None
    lon: float | None = None
    level: str | None = None  # magnitude type | yellow, orange, red | a fire danger class
    value: float | None = None  # magnitude | awareness level (2 to 4) | fire class (0 to 5)
    issued: datetime | None = None  # EMSC's last update | the warning sent | the map read
    details: dict = field(default_factory=dict)
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    def public(self, now: datetime | None = None) -> dict:
        """For the page, the tools and the inspector: local wall times, every detail."""
        out = {"id": self.id, "kind": self.kind, "source": self.source, "title": self.title,
               "start": wall(self.start), "end": wall(self.end), "lat": self.lat, "lon": self.lon,
               "level": self.level, "value": self.value, "issued": wall(self.issued),
               "first_seen": wall(self.first_seen), **self.details}
        if now is not None and self.kind == "warning":
            out["in_force"] = warning_in_force(self, now)
        return out


def wall(t: datetime | None) -> str | None:
    return t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if t else None


def parse_time(s: str | None) -> datetime | None:
    """ISO 8601 with Z, an offset or EMSC's " UTC"; naive times are UTC."""
    if not s:
        return None
    s = s.strip().replace(" UTC", "Z")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# -- where ---------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def areas() -> dict[str, dict]:
    """The island ("cyprus") and its districts by lower-case name: {name, level, geometry}."""
    try:
        data = json.loads(AREAS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {f["properties"]["id"]: {**f["properties"], "geometry": f["geometry"]}
            for f in data.get("features", [])}


def area_feature(area_id: str, props: dict | None = None) -> dict | None:
    a = areas().get(area_id)
    if a is None:
        return None
    return {"type": "Feature", "geometry": a["geometry"],
            "properties": {"area": area_id, "name": a["name"], **(props or {})}}


COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def _nearest_vertex_m(geom: dict, lat: float, lon: float) -> float:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return min(haversine_m(lat, lon, y, x) for poly in polys for ring in poly for x, y in ring[::3])


def where(lat: float, lon: float) -> dict:
    """A point said against Nicosia and the island: its distance and direction from the city's
    centre, its district (or the nearest coast's, at sea) and a phrase for it."""
    d_km = haversine_m(NICOSIA[0], NICOSIA[1], lat, lon) / 1000
    direction = compass(bearing_deg(NICOSIA[0], NICOSIA[1], lat, lon))
    districts = {k: a for k, a in areas().items() if a.get("level") == "district"}
    island = areas().get("cyprus")
    at_sea = bool(island) and not contains(island["geometry"], lon, lat)
    district = next((a["name"] for a in districts.values() if contains(a["geometry"], lon, lat)), None)
    if district is None and districts:
        district = min(districts.values(), key=lambda a: _nearest_vertex_m(a["geometry"], lat, lon))["name"]
    if d_km < 3:
        words = "under Nicosia's centre"
    elif d_km < 15:
        words = f"{d_km:.0f} km {direction} of Nicosia's centre"
    else:
        place = (f"off {district}" if at_sea else f"in the {district} district") if district else ""
        words = f"{place}, {d_km:.0f} km {direction} of Nicosia".lstrip(", ")
    return {"distance_km": round(d_km, 1), "direction": direction, "district": district,
            "at_sea": at_sea, "words": words}


# -- earthquakes ---------------------------------------------------------------------------------

EARTHQUAKE_TYPES = (None, "", "ke", "se")  # known or suspected earthquakes, not blasts

# When a quake raises an incident: (least magnitude, within km of Nicosia's centre).
QUAKE_RAISES = ((2.5, 20.0), (3.0, 100.0), (4.5, 250.0))
FELT_NEAR_KM = 25.0  # felt reports this close to the centre are "felt in Nicosia"
FELT_PAGES = 10  # this many of them: the city felt it, so the watch pages (high severity)
QUAKE_HIGH = ((5.0, 150.0), (4.0, 50.0))
QUAKE_MEDIUM = ((4.0, 100.0), (3.5, 50.0))
QUAKE_LASTS = timedelta(hours=3)  # a quake's incident stays "happening" this long
QUAKE_SEQUENCE_KM = 60.0  # quakes this close and within a day are one sequence (one incident)
QUAKE_SEQUENCE_WITHIN = timedelta(hours=24)

INTENSITY_WORDS = {1: "not felt", 2: "scarcely felt", 3: "weak", 4: "largely observed", 5: "strong",
                   6: "slightly damaging", 7: "damaging", 8: "heavily damaging", 9: "destructive",
                   10: "very destructive", 11: "devastating", 12: "completely devastating"}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X",
         11: "XI", 12: "XII"}


def _met(mag: float, km: float, rules) -> bool:
    return any(mag >= m and km <= d for m, d in rules)


def quake_rules(h: Hazard) -> tuple[bool, str, str]:
    """(raises an incident, severity, why) for a quake, from its magnitude, its distance from
    Nicosia and the felt reports near the city (when they have been read)."""
    mag, km = h.value or 0.0, h.details.get("distance_km") or 9999.0
    near = (h.details.get("felt") or {}).get("near_nicosia") or 0
    raises = _met(mag, km, QUAKE_RAISES) or near > 0
    if near >= FELT_PAGES:
        return raises, "high", f"{near} felt reports within {FELT_NEAR_KM:.0f} km of Nicosia"
    if _met(mag, km, QUAKE_HIGH):
        return raises, "high", f"M{mag:.1f} at {km:.0f} km"
    if _met(mag, km, QUAKE_MEDIUM) or near > 0:
        return raises, "medium", (f"{near} felt report{'s' if near != 1 else ''} near Nicosia"
                                  if near else f"M{mag:.1f} at {km:.0f} km")
    return raises, "low", f"M{mag:.1f} at {km:.0f} km"


def parse_emsc(payload: dict) -> list[Hazard]:
    """EMSC's FDSN events (format=json) as earthquakes, described against Nicosia."""
    out = []
    for f in (payload or {}).get("features") or []:
        p = f.get("properties") or {}
        if p.get("evtype") not in EARTHQUAKE_TYPES:
            continue
        unid = p.get("unid") or f.get("id")
        t = parse_time(p.get("time"))
        try:
            lat, lon, mag = float(p["lat"]), float(p["lon"]), float(p["mag"])
        except (KeyError, TypeError, ValueError):
            continue
        if not unid or t is None:
            continue
        depth = abs(float(p.get("depth") or 0.0))
        w = where(lat, lon)
        auth = p.get("auth") or "EMSC"
        details = {"magnitude": round(mag, 1), "magtype": p.get("magtype"), "depth_km": round(depth, 1),
                   "auth": auth, "located_by": _located_by(auth), "region": p.get("flynn_region"),
                   **{k: w[k] for k in ("distance_km", "direction", "district", "at_sea")},
                   "where": w["words"], "unid": unid,
                   "url": f"https://www.seismicportal.eu/eventdetails.html?unid={unid}"}
        out.append(Hazard(f"eq-{unid}", "earthquake", "emsc", t, None,
                          f"M{mag:.1f} earthquake {w['words']}", lat, lon,
                          level=p.get("magtype"), value=round(mag, 1),
                          issued=parse_time(p.get("lastupdate")), details=details))
    return out


def _located_by(auth: str) -> str:
    return {"GSD": "the Cyprus Geological Survey Department", "EMSC": "EMSC",
            "NEIC": "the USGS (NEIC)", "NOA": "the National Observatory of Athens",
            "KOERI": "Kandilli Observatory (KOERI)", "AFAD": "AFAD (Türkiye)"}.get(auth, auth)


def parse_felt_counts(payload) -> dict[str, int]:
    """EMSC testimonies search (includeTestimonies=false): felt reports per quake."""
    out = {}
    for r in payload or []:
        if isinstance(r, dict) and r.get("ev_unid"):
            out[r["ev_unid"]] = int(r.get("ev_nbtestimonies") or 0)
    return out


def parse_felt_zip(blob: bytes) -> list[tuple[float, float, int, float]]:
    """EMSC testimonies with their positions (a zip holding one CSV per quake): (lon, lat, epoch
    ms, corrected intensity) per felt report."""
    out = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if not name.endswith(".csv"):
                continue
            text = z.read(name).decode("utf-8", "replace")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 6 or row[0].startswith("#"):
                    continue
                try:
                    lon, lat, icorr = float(row[0]), float(row[1]), float(row[5])
                except ValueError:
                    continue
                t = parse_time(row[2])
                out.append((lon, lat, int(t.timestamp() * 1000) if t else 0, icorr))
    return out


CELL_DEG = 0.02  # felt reports are drawn in cells about 2 km across


def felt_summary(reports: list[tuple[float, float, int, float]], total: int | None,
                 read_at: datetime) -> dict:
    """What the felt reports say: how many, how many near Nicosia and how strongly (the median:
    a few reports always say far more than the rest), and cells for the map ({lon, lat, n, i}:
    reports and their median intensity)."""
    near = [r for r in reports if haversine_m(NICOSIA[0], NICOSIA[1], r[1], r[0]) <= FELT_NEAR_KM * 1000]
    cells: dict[tuple[int, int], list[float]] = {}
    for lon, lat, _t, i in reports:
        cells.setdefault((round(lon / CELL_DEG), round(lat / CELL_DEG)), []).append(i)
    near_i = [r[3] for r in near]
    typical = round(statistics.median(near_i)) if near_i else None
    return {
        "count": max(total or 0, len(reports)), "located": len(reports), "near_nicosia": len(near),
        "near_intensity": typical,
        "near_words": INTENSITY_WORDS.get(typical) if typical else None,
        "cells": [{"lon": round(x * CELL_DEG, 3), "lat": round(y * CELL_DEG, 3), "n": len(v),
                   "i": round(statistics.median(v), 1)} for (x, y), v in sorted(cells.items())],
        "read_at": wall(read_at),
    }


# -- weather warnings ----------------------------------------------------------------------------

NS = {"a": "http://www.w3.org/2005/Atom", "cap": "urn:oasis:names:tc:emergency:cap:1.2"}
LEVELS = {"yellow": 2, "orange": 3, "red": 4}
# The Department of Meteorology warns for the island as one area (EMMA CY001), so every Cyprus
# warning covers Nicosia; a later split into regions would name these.
NICOSIA_AREAS = ("CY001",)
NICOSIA_WORDS = re.compile(r"cyprus|nicosia|lefkosia|λευκωσία|κύπρος|inland|mesaoria", re.I)
WARNING_SEVERITY = {"yellow": "low", "orange": "high", "red": "high"}
SEVERITY_LEVEL = {"Moderate": "yellow", "Severe": "orange", "Extreme": "red"}


def _slug(identifier: str) -> str:
    """A CAP identifier as an id: 2.49.0.0.196.0.CY.240813170521.maxtemp.000 -> cy-240813170521-maxtemp-000."""
    parts = identifier.split(".")
    tail = next((parts[i:] for i, p in enumerate(parts) if re.fullmatch(r"[A-Z]{2}", p)), parts[-3:])
    return re.sub(r"[^a-z0-9]+", "-", "-".join(tail).lower()).strip("-")[:60]


def _awareness_type(event: str) -> str:
    """'High Temperature Yellow' or 'Moderate Wind warning' -> 'high temperature' or 'wind'."""
    words = re.sub(r"\b(yellow|orange|red|warning|moderate|severe|extreme|minor)\b", " ", event or "", flags=re.I)
    return re.sub(r"[-_\s]+", " ", words).strip().lower() or "weather"


def _covers_nicosia(area: str | None, emma: str | None) -> bool:
    return (emma or "") in NICOSIA_AREAS or bool(NICOSIA_WORDS.search(area or ""))


def _warning(identifier: str, k: int, *, level: str, kind: str, event: str, area: str | None,
             emma: str | None, onset, expires, sent, severity: str | None, msg_type: str | None,
             **more) -> Hazard | None:
    start = onset or sent
    if start is None:
        return None
    hid = f"wx-{_slug(identifier)}" + (f"-{k}" if k else "")
    words = f"{level.capitalize()} {kind} warning"
    covers = _covers_nicosia(area, emma)
    center = NICOSIA if covers else None
    details = {"event": event, "awareness": kind, "level_word": level.capitalize(),
               "severity_cap": severity, "area": area, "emma_id": emma, "covers_nicosia": covers,
               "msg_type": msg_type, "identifier": identifier,
               "area_id": "cyprus" if emma == "CY001" else None, **{k2: v for k2, v in more.items() if v}}
    return Hazard(hid, "warning", "meteoalarm", start, expires, f"{words} for {area or 'Cyprus'}",
                  center[0] if center else None, center[1] if center else None,
                  level=level, value=float(LEVELS.get(level, 0)), issued=sent, details=details)


def parse_meteoalarm_atom(text: str) -> list[Hazard]:
    """Meteoalarm's legacy Atom feed with CAP 1.2 entries."""
    root = ET.fromstring(text)
    out = []
    for k, e in enumerate(root.findall("a:entry", NS)):
        def t(tag: str) -> str | None:
            el = e.find(tag, NS)
            return el.text.strip() if el is not None and el.text else None
        if (t("cap:status") or "Actual") != "Actual":
            continue
        event = t("cap:event") or ""
        title = t("a:title") or ""
        level = next((w.lower() for w in (title.split()[:1] + event.split()[-1:]) if w.lower() in LEVELS),
                     SEVERITY_LEVEL.get(t("cap:severity") or "", "yellow"))
        identifier = t("cap:identifier") or t("a:id") or f"atom-{k}"
        cap = e.find("a:link[@type='application/cap+xml']", NS)
        h = _warning(identifier, 0, level=level, kind=_awareness_type(event), event=event,
                     area=t("cap:areaDesc"), emma=t("cap:geocode/a:value"),
                     onset=parse_time(t("cap:onset")), expires=parse_time(t("cap:expires")),
                     sent=parse_time(t("cap:sent")), severity=t("cap:severity"),
                     msg_type=t("cap:message_type"), headline=title,
                     cap_url=cap.get("href") if cap is not None else None)
        if h is not None:
            out.append(h)
    return out


def _param(info: dict, name: str) -> str | None:
    return next((p.get("value") for p in info.get("parameter") or [] if p.get("valueName") == name), None)


def parse_meteoalarm_api(payload: dict) -> list[Hazard]:
    """Meteoalarm's JSON API (feeds-<country>): full CAP alerts, in English and the country's
    own language."""
    out = []
    for w in (payload or {}).get("warnings") or []:
        alert = w.get("alert") or {}
        if alert.get("status", "Actual") != "Actual":
            continue
        infos = alert.get("info") or []
        en = next((i for i in infos if str(i.get("language", "")).lower().startswith("en")), infos[0] if infos else None)
        if en is None:
            continue
        other = next((i for i in infos if i is not en), None)
        lvl = (_param(en, "awareness_level") or "").split(";")
        level = lvl[1].strip().lower() if len(lvl) > 1 and lvl[1].strip().lower() in LEVELS \
            else SEVERITY_LEVEL.get(en.get("severity") or "", "yellow")
        typ = (_param(en, "awareness_type") or "").split(";")
        kind = (typ[1].strip().lower().replace("-", " ") if len(typ) > 1 else _awareness_type(en.get("event", "")))
        identifier = alert.get("identifier") or w.get("uuid") or "?"
        for k, area in enumerate(en.get("area") or [{}]):
            emma = next((g.get("value") for g in area.get("geocode") or [] if g.get("valueName") == "EMMA_ID"), None)
            h = _warning(identifier, k, level=level, kind=kind, event=en.get("event") or "",
                         area=area.get("areaDesc"), emma=emma, onset=parse_time(en.get("onset")),
                         expires=parse_time(en.get("expires")), sent=parse_time(alert.get("sent")),
                         severity=en.get("severity"), msg_type=alert.get("msgType"),
                         headline=en.get("headline"), description=en.get("description"),
                         instruction=en.get("instruction"), web=en.get("web"),
                         sender=en.get("senderName"),
                         description_local=(other or {}).get("description"),
                         local_language=(other or {}).get("language"),
                         cap_url=f"{METEOALARM_API}/{w['uuid']}" if w.get("uuid") else None)
            if h is not None:
                out.append(h)
    return out


def warning_in_force(h: Hazard, at: datetime) -> bool:
    return h.start <= at and (h.end is None or at < h.end)


# -- fire danger ---------------------------------------------------------------------------------

FIRE_CLASSES = ("low", "moderate", "high", "very_high", "extreme", "very_extreme")
FIRE_WORDS = {"low": "Low", "moderate": "Moderate", "high": "High", "very_high": "Very high",
              "extreme": "Extreme", "very_extreme": "Very extreme"}
FIRE_FWI = {"low": "under 11.2", "moderate": "11.2 to 21.3", "high": "21.3 to 38",
            "very_high": "38 to 50", "extreme": "50 to 70", "very_extreme": "over 70"}
# EFFIS's legend for mf010.fwi, colour to class.
FIRE_PALETTE = {(156, 255, 192): 0, (205, 226, 78): 1, (230, 172, 0): 2, (217, 112, 16): 3,
                (173, 6, 14): 4, (58, 0, 21): 5}
FIRE_SHARE = 0.25  # the district's level: the highest class at least this share of it reaches
FIRE_SEVERITY = {"very_high": "medium", "extreme": "high", "very_extreme": "high"}


def _fire_class(rgba: tuple[int, ...]) -> int | None:
    if len(rgba) > 3 and rgba[3] < 128:
        return None
    best, dist = None, 1e9
    for colour, cls in FIRE_PALETTE.items():
        d = sum((a - b) ** 2 for a, b in zip(rgba[:3], colour))
        if d < dist:
            best, dist = cls, d
    return best if dist <= 40 ** 2 else None


def pixel_centre(i: int, j: int, size=FIRE_SIZE, bbox=FIRE_BBOX) -> tuple[float, float]:
    """(lon, lat) of pixel column i, row j (row 0 the north edge)."""
    w, h = size
    return (bbox[0] + (i + 0.5) * (bbox[2] - bbox[0]) / w, bbox[3] - (j + 0.5) * (bbox[3] - bbox[1]) / h)


@lru_cache(maxsize=4)
def _district_pixels(area_id: str, size: tuple[int, int]) -> tuple[int, ...]:
    a = areas().get(area_id)
    if a is None:
        return ()
    w, h = size
    out = []
    for j in range(h):
        for i in range(w):
            lon, lat = pixel_centre(i, j, size)
            if contains(a["geometry"], lon, lat):
                out.append(j * w + i)
    return tuple(out)


def rle(cells: list[int | None]) -> str:
    """Classes as runs ("<class><count>", '.' for no data), space separated: small enough to
    store per day and send to the page."""
    out, prev, n = [], None, 0
    for c in cells:
        ch = "." if c is None else str(c)
        if ch == prev:
            n += 1
            continue
        if prev is not None:
            out.append(f"{prev}{n}")
        prev, n = ch, 1
    if prev is not None:
        out.append(f"{prev}{n}")
    return " ".join(out)


def unrle(s: str) -> list[int | None]:
    out: list[int | None] = []
    for run in s.split():
        ch, n = run[0], int(run[1:])
        out += [None if ch == "." else int(ch)] * n
    return out


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def parse_fire_png(blob: bytes, day: date, read_at: datetime, *, district: str = "nicosia") -> Hazard | None:
    """EFFIS's fire weather index image over the island for one day, as that day's fire danger
    in the district (None when the image holds no data there)."""
    from PIL import Image  # noqa: PLC0415 - only the fire danger reads images

    im = Image.open(io.BytesIO(blob)).convert("RGBA")
    size = im.size
    flat = im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()
    cells = [_fire_class(tuple(px)) for px in flat]
    inside = [cells[k] for k in _district_pixels(district, size) if cells[k] is not None]
    if not inside:
        return None
    counts = [sum(1 for c in inside if c == k) for k in range(len(FIRE_CLASSES))]
    shares = {FIRE_CLASSES[k]: round(n / len(inside), 3) for k, n in enumerate(counts) if n}
    level_i = 0
    for k in range(len(FIRE_CLASSES) - 1, -1, -1):
        if sum(counts[k:]) / len(inside) >= FIRE_SHARE:
            level_i = k
            break
    w, _h = size
    ci = int((NICOSIA[1] - FIRE_BBOX[0]) / (FIRE_BBOX[2] - FIRE_BBOX[0]) * w)
    cj = int((FIRE_BBOX[3] - NICOSIA[0]) / (FIRE_BBOX[3] - FIRE_BBOX[1]) * size[1])
    near = [cells[j * w + i] for j in range(cj - 1, cj + 2) for i in range(ci - 1, ci + 2)
            if 0 <= i < w and 0 <= j < size[1] and cells[j * w + i] is not None]
    city = FIRE_CLASSES[max(near)] if near else None
    level = FIRE_CLASSES[level_i]
    start, end = day_bounds(day)
    name = areas().get(district, {}).get("name", district.title())
    today = read_at.astimezone(TZ).date()
    details = {"day": day.isoformat(), "district": name, "district_level": level, "city_level": city,
               "shares": shares, "fwi": FIRE_FWI[level], "forecast": day > today,
               "grid": rle(cells), "size": list(size), "bbox": list(FIRE_BBOX),
               "area_id": district, "read_on": today.isoformat()}
    return Hazard(f"fire-{day.isoformat()}", "fire", "effis", start, end,
                  f"{FIRE_WORDS[level]} fire danger in the {name} district", NICOSIA[0], NICOSIA[1],
                  level=level, value=float(level_i), issued=read_at, details=details)


def fire_index(h: Hazard) -> int:
    return FIRE_CLASSES.index(h.level) if h.level in FIRE_CLASSES else 0


def district_center(area_id: str) -> tuple[float, float] | None:
    a = areas().get(area_id)
    c = centroid(a["geometry"]) if a else None
    return (c[1], c[0]) if c else None


def with_felt(h: Hazard, felt: dict) -> Hazard:
    return replace(h, details={**h.details, "felt": felt})

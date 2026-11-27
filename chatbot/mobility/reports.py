"""Road reports: what is happening on Nicosia's roads besides speed.

Three feeds of the Cyprus National Access Point (traffic4cyprus.org.cy, DATEX II, CC BY 4.0):

- Waze alerts (fixcyprus.cy, dataset waze_alerts): what drivers report in the Waze app, across
  the island: closures, hazards (a pothole, a car stopped on the shoulder, something on the
  road), jams and accidents, each a point with its street and when it was reported (local
  time). A closure comes as several points along the closed stretch, reported within seconds
  of each other: they are grouped into one report with the stretch as a line. Many closures
  stay in the feed for weeks or months (long works), so a report's age is part of it.
- Waze traffic (dataset waze_traffic): jam lines with Waze's jam level (0 free flow to 5
  closed), speed and delay, a second source of congestion beside the Bluetooth detectors. It
  answered HTTP 503 when this was built, so its shape is assumed from the alerts and its catalogue
  entry, it is parsed defensively, and a failing fetch is tried again only every 15 minutes.
- Traffic events (SituationPublication, dataset trafficevents): roadworks, diversions and
  accidents the Public Works Department's operators enter, each with its planned start and end,
  a point and a description. Some are written in Greek and come again as a separate English
  record: the two are joined into one report that keeps the Greek.

Every report, whatever its source, is a `RoadReport`: a kind (closure, accident, hazard, jam,
roadworks, diversion), an English label from its type, the publisher's own words, where, since
when, until when if said, and a stable id (report-waze-<8 hex>, report-works-<record number>,
report-jam-<8 hex>). Only Greater Nicosia is kept, as for the road speeds.

One `RoadReports` on `Mobility` reads each feed at most every few minutes (alerts and jam lines
every 3, the operators' events every 15) for every page, tool and the inspector, keeps the last
good copy for a while when a fetch fails (and says so), and hands each good fetch to the twin's
memory (table road_reports, and the feed's coverage), so the city clock replays them. A past
moment the memory didn't watch a feed for is unknown, never empty.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from chatbot.geo import haversine_m
from chatbot.mobility.gtfs import TZ
from chatbot.mobility.roads import AREA, NAP

log = logging.getLogger(__name__)

WAZE_ALERTS_URL = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/"
WAZE_TRAFFIC_URL = "https://fixcyprus.cy/gnosis/open/api/nap/datasets/waze_traffic/"
EVENTS_URL = NAP + "SituationPublication"
LICENCE = "CC BY 4.0, Cyprus National Access Point (traffic4cyprus.org.cy)"
PAGES = {
    "waze_alerts": "https://traffic4cyprus.org.cy/dataset/waze_alerts",
    "waze_traffic": "https://traffic4cyprus.org.cy/dataset/waze_traffic",
    "traffic_events": "https://traffic4cyprus.org.cy/dataset/trafficevents",
}
SOURCE_NAMES = {"waze_alerts": "Waze drivers", "waze_traffic": "Waze traffic",
                "traffic_events": "Public Works Department"}

# Each kind's words, loudest first: (label, one, many).
KINDS: dict[str, tuple[str, str, str]] = {
    "accident": ("Accident", "accident", "accidents"),
    "closure": ("Road closed", "closure", "closures"),
    "jam": ("Jam", "jam", "jams"),
    "hazard": ("Hazard", "hazard", "hazards"),
    "roadworks": ("Roadworks", "place with roadworks", "places with roadworks"),
    "diversion": ("Diversion", "diversion", "diversions"),
}
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
# Night map colours: closures in the ink white of a no-entry sign, jams in the road speeds' "very
# slow", hazards amber, roadworks teal (planned, long-running), diversions blue, and signal red
# for accidents only (real danger). The page tells kinds apart by shape as well.
KIND_COLORS = {"accident": "#ff5a4e", "closure": "#e4ebf4", "jam": "#ff9a71", "hazard": "#f5b94a",
               "roadworks": "#4fc1b0", "diversion": "#7aa7ff"}
LEGEND = {KINDS[k][0]: KIND_COLORS[k] for k in KINDS}

ALERTS_TTL_S = 180.0  # the publisher updates "up to 5 min"
TRAFFIC_TTL_S = 180.0
EVENTS_TTL_S = 900.0  # entered by operators, a few times a day at most
RETRY_S = {"waze_alerts": 60.0, "waze_traffic": 900.0, "traffic_events": 300.0}
KEEP_STALE_S = {"waze_alerts": 30 * 60, "waze_traffic": 30 * 60, "traffic_events": 6 * 3600}
# A report counts at a past moment if the twin saw it within this of it (about two fetches).
SEEN_SLACK = {"waze_alerts": timedelta(minutes=8), "waze_traffic": timedelta(minutes=8),
              "traffic_events": timedelta(minutes=35)}
# Content older than this says the feed has stopped, whatever its HTTP status.
FRESH_WITHIN = {"waze_alerts": timedelta(hours=6), "waze_traffic": timedelta(hours=1),
                "traffic_events": timedelta(days=21)}
COVERAGE_TYPE = "RoadReports"  # the memory's coverage table: device = the feed
CLOSURE_JOIN_S = 600  # closure points reported this close together, on one street, are one closure
CLOSURE_JOIN_M = 1500.0
STRETCH_HOP_M = 600.0  # a closed stretch is drawn as a line when its points chain this closely
TRANSLATION_M = 150.0  # a record and its translation sit this close
NEAR_ROAD_M = 60.0  # a report this close to a measured road segment is on it
RECENT = timedelta(minutes=30)
OLD = timedelta(days=7)  # older than this is a long closure or long works, not news

WAZE_TYPES = {"ROAD_CLOSED": ("closure", "Road closed"), "JAM": ("jam", "Traffic jam"),
              "ACCIDENT": ("accident", "Accident"), "HAZARD": ("hazard", "Hazard"),
              "WEATHERHAZARD": ("hazard", "Weather hazard")}
WAZE_SUBTYPES = {
    "JAM_LIGHT_TRAFFIC": "Light traffic", "JAM_MODERATE_TRAFFIC": "Moderate traffic",
    "JAM_HEAVY_TRAFFIC": "Heavy traffic", "JAM_STAND_STILL_TRAFFIC": "Standstill traffic",
    "ACCIDENT_MINOR": "Minor accident", "ACCIDENT_MAJOR": "Major accident",
    "HAZARD_ON_ROAD": "Hazard on the road", "HAZARD_ON_SHOULDER": "Hazard on the shoulder",
    "HAZARD_WEATHER": "Weather hazard", "HAZARD_ON_ROAD_OBJECT": "Object on the road",
    "HAZARD_ON_ROAD_POT_HOLE": "Pothole", "HAZARD_ON_ROAD_ROAD_KILL": "Dead animal on the road",
    "HAZARD_ON_ROAD_CAR_STOPPED": "Car stopped on the road",
    "HAZARD_ON_ROAD_TRAFFIC_LIGHT_FAULT": "Traffic lights out", "HAZARD_ON_ROAD_LANE_CLOSED": "Lane closed",
    "HAZARD_ON_ROAD_OIL": "Oil on the road", "HAZARD_ON_ROAD_ICE": "Ice on the road",
    "HAZARD_ON_ROAD_CONSTRUCTION": "Construction on the road",
    "HAZARD_ON_SHOULDER_CAR_STOPPED": "Car stopped on the shoulder",
    "HAZARD_ON_SHOULDER_ANIMALS": "Animals on the shoulder",
    "HAZARD_ON_SHOULDER_MISSING_SIGN": "Missing sign", "HAZARD_WEATHER_FOG": "Fog",
    "HAZARD_WEATHER_HAIL": "Hail", "HAZARD_WEATHER_HEAVY_RAIN": "Heavy rain",
    "HAZARD_WEATHER_FLOOD": "Flooding", "HAZARD_WEATHER_FREEZING_RAIN": "Freezing rain",
    "HAZARD_WEATHER_HEAT_WAVE": "Heat wave",
    "ROAD_CLOSED_HAZARD": "Closed for a hazard", "ROAD_CLOSED_CONSTRUCTION": "Closed for works",
    "ROAD_CLOSED_EVENT": "Closed for an event",
}
SUBTYPE_KIND = {"HAZARD_ON_ROAD_CONSTRUCTION": "roadworks"}
JAM_LEVELS = {1: "Light traffic", 2: "Moderate traffic", 3: "Heavy traffic", 4: "Standstill traffic",
              5: "Closed to traffic"}
MIN_JAM_LEVEL = 2  # light traffic is no news
GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")


# -- the model ----------------------------------------------------------------------------------


@dataclass
class RoadReport:
    id: str
    source: str  # waze_alerts | waze_traffic | traffic_events
    kind: str  # closure | accident | hazard | jam | roadworks | diversion
    label: str  # its type in English words ("Pothole", "Diversion in operation")
    street: str | None
    text: str | None  # the publisher's own words (Greek where they wrote Greek)
    lat: float
    lon: float
    line: list[tuple[float, float]] | None = None  # (lon, lat): a closed stretch, a jam
    points: list[tuple[float, float]] | None = None  # (lon, lat): what a closure groups
    since: datetime | None = None  # reported at, or when the works start
    until: datetime | None = None  # when the works are planned to end
    severity: str | None = None
    details: dict = field(default_factory=dict)
    first_seen: datetime | None = None  # by the twin (from its memory)
    last_seen: datetime | None = None

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind][0]

    @property
    def color(self) -> str:
        return KIND_COLORS[self.kind]

    def status(self, at: datetime) -> str:
        """active, planned (works that start later) or ended (their end has passed)."""
        if self.since is not None and self.since > at + timedelta(minutes=1) and self.source == "traffic_events":
            return "planned"
        if self.until is not None and self.until < at:
            return "ended"
        return "active"

    def geometry(self) -> dict:
        if self.line and len(self.line) >= 2:
            return {"type": "LineString", "coordinates": [list(c) for c in self.line]}
        if self.points and len(self.points) >= 2:
            return {"type": "MultiPoint", "coordinates": [list(c) for c in self.points]}
        return {"type": "Point", "coordinates": [self.lon, self.lat]}

    def row(self) -> tuple:
        """For the twin's memory: collector.jobs.ROAD_REPORT_COLUMNS without the sightings."""
        geom = self.geometry()
        return (self.id, self.source, self.kind, self.label, self.street, self.text, self.lat,
                self.lon, json.dumps(geom) if geom["type"] != "Point" else None,
                _us(self.since), _us(self.until), self.severity,
                json.dumps(self.details, ensure_ascii=False, default=str) if self.details else None)

    @classmethod
    def from_row(cls, r: tuple) -> RoadReport:
        """A row of the memory's road_reports, in column order."""
        (rid, source, kind, label, street, text, lat, lon, geom, since, until, severity, details,
         first, last) = r
        line = points = None
        try:
            g = json.loads(geom) if geom else None
        except ValueError:
            g = None
        if g and g.get("type") == "LineString":
            line = [tuple(c) for c in g["coordinates"]]
        elif g and g.get("type") == "MultiPoint":
            points = [tuple(c) for c in g["coordinates"]]
        try:
            more = json.loads(details) if details else {}
        except ValueError:
            more = {}
        return cls(rid, source, kind if kind in KINDS else "hazard", label or "", street, text,
                   float(lat), float(lon), line, points, since, until, severity, more, first, last)


def _us(t: datetime | None) -> int | None:
    return int(t.timestamp() * 1_000_000) if t else None


def in_area(lat: float, lon: float) -> bool:
    return AREA[0] <= lon <= AREA[2] and AREA[1] <= lat <= AREA[3]


def _local(text: str | None) -> datetime | None:
    """A time as the feeds write it: ISO or "2026-11-27 21:15:37", Nicosia time unless it says."""
    if not text:
        return None
    t = text.strip().replace("Z", "+00:00")
    # .NET writes up to 7 fractional digits; Python reads 6.
    t = re.sub(r"(\.\d{6})\d+", r"\1", t)
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.year < 1900:  # 0001-01-01: "never" in the swarco feeds
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _short_id(text: str) -> str:
    """Eight hex characters for an id: a UUID's own first eight, else a hash."""
    hexed = re.sub(r"[^0-9a-f]", "", text.lower())
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-.*", text.lower()) and len(hexed) >= 8:
        return hexed[:8]
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _first(el: ET.Element, name: str) -> str | None:
    for x in el.iter():
        if _tag(x) == name and x.text and x.text.strip():
            return x.text.strip()
    return None


def _float(v: str | None) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _coords(el: ET.Element) -> list[tuple[float, float]]:
    """Every (lon, lat) in an element, in document order: coordinate pairs, or GML posList."""
    out: list[tuple[float, float]] = []
    lat = None
    for x in el.iter():
        n = _tag(x)
        if n == "latitude":
            lat = _float(x.text)
        elif n == "longitude" and lat is not None:
            lon = _float(x.text)
            if lon is not None:
                out.append((round(lon, 6), round(lat, 6)))
            lat = None
        elif n in ("posList", "coordinates") and x.text and not out:
            nums = [_float(v) for v in re.split(r"[\s,]+", x.text.strip())]
            nums = [v for v in nums if v is not None]
            for la, lo in zip(nums[0::2], nums[1::2]):  # DATEX writes lat, lon
                out.append((round(lo, 6), round(la, 6)))
    return out


def _comments(el: ET.Element) -> dict[str, str]:
    """A Waze element's generalPublicComment: {commentType: value}."""
    out: dict[str, str] = {}
    for c in el.iter():
        if _tag(c) != "comment":
            continue
        kind = value = None
        for x in c:
            if _tag(x) == "commentType":
                kind = (x.text or "").strip()
            elif _tag(x) == "value":
                value = (x.text or "").strip()
        if kind and value and kind not in out:
            out[kind] = value
    return out


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    return haversine_m(a[1], a[0], b[1], b[0])


# -- Waze alerts ----------------------------------------------------------------------------------


@dataclass
class Parsed:
    """One fetch, parsed: the reports in Greater Nicosia, how many the feed held in all, and the
    times inside it (for freshness)."""
    reports: list[RoadReport]
    total: int
    published: datetime | None = None
    newest: datetime | None = None  # the newest report or record anywhere in the feed


def parse_waze_alerts(xml_text: str) -> Parsed:
    root = ET.fromstring(xml_text)
    published = _local(_first(root, "publicationTime"))
    singles: list[RoadReport] = []
    closures: list[tuple[datetime | None, str, str | None, str | None, tuple[float, float]]] = []
    total = 0
    newest: datetime | None = None
    for el in root.iter():
        if _tag(el) != "trafficElement":
            continue
        total += 1
        uid = _first(el, "id")
        c = _comments(el)
        pts = _coords(el)
        reported = _local(c.get("report_time") or c.get("pub_time"))
        if reported and (newest is None or reported > newest):
            newest = reported
        if not uid or not pts:
            continue
        lon, lat = pts[0]
        if not in_area(lat, lon):
            continue
        wtype, sub = c.get("type", "").upper(), (c.get("subtype") or "").upper() or None
        if wtype not in WAZE_TYPES:
            continue  # police, chit-chat: not road reports
        street = c.get("street") or None
        if wtype == "ROAD_CLOSED":
            closures.append((reported, uid, street, sub, (lon, lat)))
            continue
        kind, label = WAZE_TYPES[wtype]
        kind = SUBTYPE_KIND.get(sub or "", kind)
        label = WAZE_SUBTYPES.get(sub or "", label)
        details = {"type": wtype, **({"subtype": sub} if sub else {})}
        for key in ("reliability", "confidence", "thumbs_up", "nThumbsUp"):
            if c.get(key):
                details[key] = c[key]
        singles.append(RoadReport(f"report-waze-{_short_id(uid)}", "waze_alerts", kind, label, street,
                                  " / ".join(x for x in (wtype, sub) if x), lat, lon, since=reported,
                                  severity="major" if sub == "ACCIDENT_MAJOR" else None, details=details))
    return Parsed(singles + group_closures(closures), total, published, newest)


def group_closures(points: list[tuple[datetime | None, str, str | None, str | None, tuple[float, float]]]
                   ) -> list[RoadReport]:
    """Waze sends a closure as one point per closed piece of road, reported together: points on
    one street reported within CLOSURE_JOIN_S of each other, near each other, are one closure."""
    groups: list[list[tuple]] = []
    for p in sorted(points, key=lambda p: (p[0] or datetime.min.replace(tzinfo=timezone.utc), p[1])):
        t, _, street, _, xy = p
        home = None
        for g in groups:
            g_street, g_last = g[0][2], g[-1][0]
            if (g_street or "") != (street or ""):
                continue
            if t is None or g_last is None or (t - g_last).total_seconds() > CLOSURE_JOIN_S:
                continue
            if min(_metres(xy, q[4]) for q in g) > CLOSURE_JOIN_M:
                continue
            home = g
            break
        if home is None:
            groups.append([p])
        else:
            home.append(p)
    out = []
    for g in groups:
        first = g[0]
        pts = list(dict.fromkeys(q[4] for q in g))  # one point once, though sent twice
        chain = _chain(pts)
        line = chain if len(chain) >= 2 and all(_metres(a, b) <= STRETCH_HOP_M for a, b in zip(chain, chain[1:])) else None
        anchor = _medoid(pts)
        subs = [q[3] for q in g if q[3]]
        sub = Counter(subs).most_common(1)[0][0] if subs else None
        label = WAZE_SUBTYPES.get(sub or "", "Road closed")
        details = {"type": "ROAD_CLOSED", "points": len(pts), **({"subtype": sub} if sub else {})}
        if line:
            details["length_m"] = round(sum(_metres(a, b) for a, b in zip(line, line[1:])))
        out.append(RoadReport(f"report-waze-{_short_id(first[1])}", "waze_alerts", "closure", label,
                              first[2], " / ".join(x for x in ("ROAD_CLOSED", sub) if x),
                              anchor[1], anchor[0], line=line, points=pts if len(pts) > 1 else None,
                              since=first[0], details=details))
    return out


def _chain(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The points in order along the road: from one end, always to the nearest one left."""
    if len(pts) <= 2:
        return list(pts)
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    start = max(pts, key=lambda p: _metres(p, (cx, cy)))
    out, left = [start], [p for p in pts if p != start]
    while left:
        nxt = min(left, key=lambda p: _metres(out[-1], p))
        out.append(nxt)
        left.remove(nxt)
    return out


def _medoid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    if len(pts) <= 2:
        return pts[0]
    return min(pts, key=lambda p: sum(_metres(p, q) for q in pts))


# -- Waze jam lines ---------------------------------------------------------------------------------


def _number(c: dict[str, str], *keys: str) -> float | None:
    for k in keys:
        if k in c:
            v = _float(c[k])
            if v is not None:
                return v
    return None


def parse_waze_traffic(xml_text: str) -> Parsed:
    """The jam lines. Their exact shape wasn't seen (the feed answered 503 when this was built):
    each trafficElement is read like an alert, its level, speed, delay and length from its
    comments under any of the usual names, its line from every coordinate pair in it."""
    root = ET.fromstring(xml_text)
    published = _local(_first(root, "publicationTime"))
    out, total, newest = [], 0, None
    for el in root.iter():
        if _tag(el) != "trafficElement":
            continue
        total += 1
        uid = _first(el, "id")
        c = {k.lower(): v for k, v in _comments(el).items()}
        line = _coords(el)
        reported = _local(c.get("report_time") or c.get("pub_time") or c.get("update_time"))
        if reported and (newest is None or reported > newest):
            newest = reported
        level = _number(c, "level", "jam_level")
        if not uid or not line or level is None or level < MIN_JAM_LEVEL:
            continue
        mid = line[len(line) // 2]
        if not any(in_area(y, x) for x, y in line):
            continue
        lvl = int(min(5, max(0, round(level))))
        speed = _number(c, "speed_kmh", "speedkmh", "speed")
        delay = _number(c, "delay_s", "delay")
        length = _number(c, "length_m", "length")
        details = {"level": lvl, "speed_kmh": round(speed, 1) if speed is not None else None,
                   "delay_s": round(delay) if delay is not None and delay >= 0 else None,
                   "length_m": round(length) if length is not None else None}
        out.append(RoadReport(f"report-jam-{_short_id(uid)}", "waze_traffic",
                              "closure" if lvl == 5 else "jam", JAM_LEVELS[lvl], c.get("street") or None,
                              f"level {lvl}", mid[1], mid[0], line=line if len(line) >= 2 else None,
                              since=reported, details={k: v for k, v in details.items() if v is not None}))
    return Parsed(out, total, published, newest)


# -- the operators' events -----------------------------------------------------------------------------


def _event_kind(xtype: str, subtype: str) -> str:
    s = subtype.lower()
    if "accident" in s or xtype == "Accident":
        return "accident"
    if "clos" in s or xtype in ("RoadOrCarriagewayOrLaneManagement",) and "clos" in s:
        return "closure"
    if "diversion" in s or "detour" in s:
        return "diversion"
    if "work" in s or xtype in ("MaintenanceWorks", "ConstructionWorks", "Roadworks"):
        return "roadworks"
    return "hazard"


def _event_label(subtype: str, kind: str) -> str:
    s = re.sub(r"\(s\)$", "", subtype.strip()).strip()
    return s[:1].upper() + s[1:] if s else KINDS[kind][0]


def parse_events(xml_text: str) -> Parsed:
    root = ET.fromstring(xml_text)
    published = _local(_first(root, "publicationTime"))
    records = []
    total = 0
    newest: datetime | None = None
    for rec in root.iter():
        if _tag(rec) != "situationRecord":
            continue
        total += 1
        rid = (rec.get("id") or "").split(":")[-1].strip()
        xtype = (rec.get("{http://www.w3.org/2001/XMLSchema-instance}type") or "").split(":")[-1]
        version_time = _local(_first(rec, "situationRecordVersionTime") or _first(rec, "situationRecordCreationTime"))
        if version_time and (newest is None or version_time > newest):
            newest = version_time
        pts = _coords(rec)
        if not rid or not pts:
            continue
        lon, lat = pts[0]
        if not in_area(lat, lon):
            continue
        subtype = _first(rec, "subtype") or ""
        kind = _event_kind(xtype, subtype)
        text = _first(rec, "description")
        details = {"type": xtype, "record": f"EVT:{rid}"}
        for key in ("eventTypeId", "eventSubtypeId", "roadMaintenanceType", "constructionWorkType",
                    "impact", "affectedLanes", "originalNumberOfLanes", "bearing"):
            v = _first(rec, key)
            if v:
                details[key] = v
        if rec.get("version"):
            details["version"] = rec.get("version")
        records.append(RoadReport(f"report-works-{rid}", "traffic_events", kind, _event_label(subtype, kind),
                                  None, text, lat, lon,
                                  since=_local(_first(rec, "overallStartTime") or _first(rec, "startOfPeriod")),
                                  until=_local(_first(rec, "overallEndTime") or _first(rec, "endOfPeriod")),
                                  severity=_first(rec, "severity"), details=details))
    return Parsed(join_translations(records), total, published, newest)


def join_translations(records: list[RoadReport]) -> list[RoadReport]:
    """An operator who writes an event in Greek enters its English translation as a second
    record: same kind and label, a few metres away, the same weeks. Keep one, with the Greek as
    its text and the English beside it."""
    out: list[RoadReport] = []
    used: set[int] = set()
    for i, a in enumerate(records):
        if i in used:
            continue
        for j in range(i + 1, len(records)):
            b = records[j]
            if j in used or a.kind != b.kind or a.label != b.label:
                continue
            if _metres((a.lon, a.lat), (b.lon, b.lat)) > TRANSLATION_M:
                continue
            ga, gb = bool(GREEK.search(a.text or "")), bool(GREEK.search(b.text or ""))
            if ga == gb or not _overlap(a, b):
                continue
            greek, english = (a, b) if ga else (b, a)
            first = a  # the earlier record names the pair
            a = replace(first, text=greek.text,
                        details={**first.details, "text_en": english.text,
                                 "records": [first.details.get("record"), b.details.get("record")]},
                        since=min((x for x in (a.since, b.since) if x), default=None),
                        until=max((x for x in (a.until, b.until) if x), default=None))
            used.add(j)
            break
        out.append(a)
    return out


def _overlap(a: RoadReport, b: RoadReport) -> bool:
    lo = datetime.min.replace(tzinfo=timezone.utc)
    hi = datetime.max.replace(tzinfo=timezone.utc)
    return (a.since or lo) <= (b.until or hi) and (b.since or lo) <= (a.until or hi)


# -- words ----------------------------------------------------------------------------------------------


def age_words(since: datetime | None, now: datetime) -> str | None:
    """How old a report is, as people say it: "12 min ago", "3 h ago", "since 3 Jul"."""
    if since is None:
        return None
    s = (now - since).total_seconds()
    if s < -60:
        local = since.astimezone(TZ)
        return f"from {local.day} {local:%b}" + (f" {local.year}" if local.year != now.astimezone(TZ).year else "")
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 24 * 3600:
        return f"{int(s // 3600)} h ago"
    if s < 7 * 86400:
        days = int(s // 86400)
        return "yesterday" if days == 1 else f"{days} days ago"
    local = since.astimezone(TZ)
    year = f" {local.year}" if local.year != now.astimezone(TZ).year else ""
    return f"since {local.day} {local:%b}{year}"


def clock_text(t: datetime, now: datetime) -> str:
    """"21:23" today, "27 Nov 21:23" before."""
    local = t.astimezone(TZ)
    if local.date() == now.astimezone(TZ).date():
        return local.strftime("%H:%M")
    return f"{local.day} {local:%b %H:%M}"


def local_text(t: datetime | None) -> str | None:
    return t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if t else None


def counts(reports: list[RoadReport]) -> dict[str, int]:
    c = Counter(r.kind for r in reports)
    return {k: c[k] for k in KINDS if c.get(k)}


def count_words(by: dict[str, int]) -> str:
    """"2 closures, 3 jams and 1 hazard"."""
    parts = [f"{n} {KINDS[k][1] if n == 1 else KINDS[k][2]}"
             for k, n in sorted(by.items(), key=lambda kv: KIND_ORDER[kv[0]]) if n]
    if not parts:
        return "nothing"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def sort_key(r: RoadReport, now: datetime):
    """Loud first: kind, then newest."""
    t = r.since or r.first_seen or now
    return (KIND_ORDER.get(r.kind, 9), -(t.timestamp()))


def report_feature(r: RoadReport, now: datetime) -> dict:
    """The report's mark (a point, with everything the page's style and popup need)."""
    age = (now - r.since) if r.since else None
    who = SOURCE_NAMES.get(r.source, r.source)
    status = r.status(now)
    where = f": {r.street}" if r.street else ""
    when = when_text(r, status, now) if r.source == "traffic_events" else age_words(r.since, now)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r.lon, r.lat]},
            "properties": {
                "id": r.id, "kind": r.kind, "kind_label": r.kind_label, "label": r.label,
                "street": r.street, "color": r.color, "source": r.source, "entity_kind": "road_report",
                "since": local_text(r.since), "until": local_text(r.until), "status": status,
                "age": when, "recent": bool(age is not None and timedelta(0) <= age <= RECENT and status == "active"),
                "old": bool(age is not None and age > OLD),
                "popup": f"{r.label}{where}\n{who}" + (f", {when}" if when else ""),
            }}


def when_text(r: RoadReport, status: str, now: datetime) -> str | None:
    """Planned works: "from 30 Nov" before they start, else "until 23 Dec"."""
    year = now.astimezone(TZ).year
    if status == "planned" and r.since:
        d = r.since.astimezone(TZ)
        return f"from {d.day} {d:%b}" + (f" {d.year}" if d.year != year else "")
    if r.until:
        d = r.until.astimezone(TZ)
        return f"until {d.day} {d:%b}" + (f" {d.year}" if d.year != year else "")
    return age_words(r.since, now)


def line_feature(r: RoadReport) -> dict | None:
    if not r.line or len(r.line) < 2:
        return None
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in r.line]},
            "properties": {"id": r.id, "kind": r.kind, "color": r.color, "part": "line",
                           "entity_kind": "road_report", "level": r.details.get("level")}}


def features(reports: list[RoadReport], now: datetime) -> list[dict]:
    """Lines first (a closed stretch, a jam), then the marks on top, loudest last."""
    lines = [f for f in (line_feature(r) for r in reports) if f]
    marks = [report_feature(r, now) for r in sorted(reports, key=lambda r: sort_key(r, now), reverse=True)]
    return lines + marks


# -- the feeds ----------------------------------------------------------------------------------------


@dataclass
class Feed:
    source: str
    url: str
    ttl_s: float
    parse: Callable[[str], Parsed]


FEEDS = (Feed("waze_alerts", WAZE_ALERTS_URL, ALERTS_TTL_S, parse_waze_alerts),
         Feed("traffic_events", EVENTS_URL, EVENTS_TTL_S, parse_events),
         Feed("waze_traffic", WAZE_TRAFFIC_URL, TRAFFIC_TTL_S, parse_waze_traffic))


@dataclass
class FeedState:
    tried_at: float = 0.0  # the last attempt (epoch)
    ok_at: float | None = None  # the last good fetch
    parsed: Parsed | None = None
    error: str | None = None

    def fresh_problem(self, source: str, now: datetime) -> str | None:
        """Why the content says the feed has stopped, whatever HTTP said; None when it is fresh."""
        p = self.parsed
        if p is None:
            return None
        if p.published and now - p.published > timedelta(hours=1):
            return f"the feed was last published at {clock_text(p.published, now)}"
        if p.newest is None:
            return "the feed holds no reports" if source == "waze_alerts" else None
        if now - p.newest > FRESH_WITHIN[source]:
            return f"nothing new in it since {clock_text(p.newest, now)}"
        return None


class RoadReports:
    """Nicosia's road reports now or at a moment, one fetch of each feed for every reader."""

    def __init__(self, fetch_text: Callable[[str], str], memory=None, now=None, roads=None,
                 feeds: tuple[Feed, ...] = FEEDS, waze_traffic: bool = True):
        self.fetch_text = fetch_text
        self.memory = memory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.roads = roads  # the Bluetooth segments, for "on which measured road"
        self.feeds = {f.source: f for f in feeds if waze_traffic or f.source != "waze_traffic"}
        self.state: dict[str, FeedState] = {s: FeedState() for s in self.feeds}
        self._locks = {s: threading.Lock() for s in self.feeds}
        self._recorded: dict[str, float] = {}

    # -- now ------------------------------------------------------------------------------------

    def feed(self, source: str, force: bool = False) -> FeedState:
        """A feed's state, read again when due (its TTL after a good fetch, sooner after a failure)."""
        f, st = self.feeds[source], self.state[source]
        due = (time.time() - st.tried_at) >= (f.ttl_s if st.error is None else RETRY_S[source])
        # Not due, and read before: answer at once. A first read under way is waited for, so no
        # reader takes "not read yet" for an answer.
        if not (force or due) and (st.parsed is not None or st.error is not None):
            return st
        with self._locks[source]:
            st = self.state[source]
            due = (time.time() - st.tried_at) >= (f.ttl_s if st.error is None else RETRY_S[source])
            if not (force or due):
                return st
            st.tried_at = time.time()
            try:
                parsed = f.parse(self.fetch_text(f.url))
                if source == "waze_alerts" and parsed.total == 0:
                    raise ValueError("the feed came back empty")  # never record that as "nothing"
            except ET.ParseError:
                st.error = "it didn't send readable DATEX II (an error page?)"
                log.info("road reports: %s unreadable", source)
                return st
            except Exception as e:  # noqa: BLE001 - keep the last copy, say why
                st.error = str(e)[:200]
                log.info("road reports: %s failed: %s", source, st.error)
                return st
            st.parsed, st.ok_at, st.error = parsed, st.tried_at, None
            self._record(source, parsed, st.tried_at)
            return st

    def _record(self, source: str, parsed: Parsed, at: float) -> None:
        if self.memory is None or self.memory.recorder is None:
            return
        self.memory.recorder.road_reports(source, [r.row() for r in parsed.reports], int(at * 1000))
        self._recorded[source] = at

    def refresh(self) -> None:
        """Every feed that is due (the watch calls this while a page listens)."""
        for s in self.feeds:
            try:
                self.feed(s)
            except Exception:  # noqa: BLE001 - one feed never stops the others
                log.exception("road reports: refresh of %s failed", s)

    def current(self) -> dict:
        """The reports now: {status, reports, feeds: {source: {...}}, at}. A feed that failed
        keeps its last good copy for KEEP_STALE_S and says so; after that it has nothing."""
        now = self.now()
        out: list[RoadReport] = []
        feeds = {}
        for s in self.feeds:
            st = self.feed(s)
            age = time.time() - st.ok_at if st.ok_at else None
            usable = st.parsed is not None and age is not None and age <= KEEP_STALE_S[s]
            problem = st.fresh_problem(s, now) if usable else None
            state = ("live" if st.error is None and not problem else "stale") if usable else (
                "unavailable" if st.error else "waiting")
            if usable:
                out += st.parsed.reports
            feeds[s] = {"state": state, "error": st.error, "note": problem, "count": len(st.parsed.reports) if usable else 0,
                        "fetched_at": st.ok_at}
        known = [v for v in feeds.values() if v["state"] in ("live", "stale")]
        status = "live" if any(v["state"] == "live" for v in feeds.values()) else "stale" if known else "unavailable"
        live = [r for r in out if r.status(now) != "ended"]
        return {"status": status, "reports": live, "feeds": feeds, "at": now}

    def cached(self) -> list[RoadReport]:
        """The last good copy of every feed, without fetching (for search, on every keystroke)."""
        return [r for st in self.state.values() if st.parsed for r in st.parsed.reports]

    def fetched_at(self, source: str) -> datetime | None:
        st = self.state.get(source)
        return datetime.fromtimestamp(st.ok_at, timezone.utc) if st and st.ok_at else None

    # -- a moment of the past ---------------------------------------------------------------------

    def at(self, moment: datetime) -> dict:
        """The reports as the twin's memory saw them at a past moment. A feed the twin wasn't
        watching then is unknown; status "unknown" when none was watched."""
        if self.memory is None:
            return {"status": "unavailable", "error": "the twin's memory is off", "reports": [], "feeds": {},
                    "at": moment}
        from chatbot.memory import Unavailable  # noqa: PLC0415 - the memory is optional here

        t_ms = int(moment.timestamp() * 1000)
        try:
            if self.memory.recorder is not None:
                self.memory.recorder.flush(2.0)
            covered = self.memory.coverage(COVERAGE_TYPE, list(self.feeds), t_ms, t_ms)
            watched = [s for s, spans in covered.items() if any(a <= t_ms <= b for a, b in spans)]
            rows = self._rows_at(moment, watched) if watched else []
        except Unavailable as e:
            if e.reason == "missing":
                return {"status": "unknown", "reports": [], "feeds": {s: {"state": "unknown"} for s in self.feeds},
                        "at": moment, "error": None}
            return {"status": "unavailable", "error": str(e), "reports": [], "feeds": {}, "at": moment}
        reports = [r for r in (RoadReport.from_row(x) for x in rows) if r.status(moment) == "active"]
        feeds = {s: {"state": "recorded" if s in watched else "unknown",
                     "count": sum(1 for r in reports if r.source == s)} for s in self.feeds}
        return {"status": "recorded" if watched else "unknown", "reports": reports, "feeds": feeds,
                "at": moment, "error": None}

    def _rows_at(self, moment: datetime, sources: list[str]) -> list[tuple]:
        import duckdb  # noqa: PLC0415

        out = []
        with self.memory.reading() as con:
            for s in sources:
                slack = SEEN_SLACK[s]
                try:
                    out += con.execute(
                        f"""SELECT {ROW_COLUMNS} FROM road_reports
                            WHERE source = ? AND first_seen <= ? AND last_seen >= ?""",
                        [s, moment + slack, moment - slack]).fetchall()
                except duckdb.CatalogException:  # a memory from before road reports
                    return []
        return out

    def from_memory(self, rid: str) -> RoadReport | None:
        """A report the memory holds (gone from the feed, or a moment of the past)."""
        if self.memory is None:
            return None
        import duckdb  # noqa: PLC0415

        from chatbot.memory import Unavailable  # noqa: PLC0415

        try:
            with self.memory.reading() as con:
                row = con.execute(f"SELECT {ROW_COLUMNS} FROM road_reports WHERE id = ?", [rid]).fetchone()
        except (Unavailable, duckdb.CatalogException):
            return None
        return RoadReport.from_row(row) if row else None

    def seen(self, rid: str) -> tuple[datetime, datetime] | None:
        r = self.from_memory(rid)
        return (r.first_seen, r.last_seen) if r and r.first_seen else None

    def watched_since(self) -> dict[str, datetime | None]:
        """The oldest moment the memory watched each feed (for "the twin has watched it since")."""
        if self.memory is None:
            return {}
        from chatbot.memory import Unavailable  # noqa: PLC0415

        try:
            covered = self.memory.coverage(COVERAGE_TYPE, list(self.feeds), 0, int(self.now().timestamp() * 1000))
        except Unavailable:
            return {}
        return {s: datetime.fromtimestamp(spans[0][0] / 1000, timezone.utc) if spans else None
                for s, spans in covered.items()}

    # -- one report ---------------------------------------------------------------------------------

    def find(self, rid: str, at: datetime | None = None) -> tuple[RoadReport | None, bool]:
        """(the report, whether a feed holds it now). From the live copy, else the memory."""
        rid = rid.strip().lower()
        if at is None:
            for r in self.current()["reports"]:
                if r.id == rid:
                    return r, True
        else:
            for r in self.at(at)["reports"]:
                if r.id == rid:
                    return r, True
        return self.from_memory(rid), False

    def on_road(self, r: RoadReport) -> list[tuple[object, float]]:
        """The measured road segments (roads.py) the report sits on: within NEAR_ROAD_M of
        their line, nearest first (an avenue's two directions both)."""
        if self.roads is None:
            return []
        try:
            segs = self.roads.segments()
        except Exception:  # noqa: BLE001 - the inventory isn't there yet
            return []
        pts = r.line or r.points or [(r.lon, r.lat)]
        out = []
        for s in segs:
            d = min(distance_to_line(p, s.coords) for p in pts)
            if d <= NEAR_ROAD_M:
                out.append((s, d))
        return sorted(out, key=lambda x: x[1])[:2]


ROW_COLUMNS = ("id, source, kind, label, street, text, lat, lon, geometry, since, until, severity, "
               "details, first_seen, last_seen")


def distance_to_line(p: tuple[float, float], line: list[tuple[float, float]]) -> float:
    """Metres from a point to a polyline, both (lon, lat), on a local flat projection."""
    kx = 111_320 * math.cos(math.radians(p[1]))
    ky = 110_574

    def xy(q):
        return ((q[0] - p[0]) * kx, (q[1] - p[1]) * ky)

    best = math.inf
    pts = [xy(q) for q in line]
    if len(pts) == 1:
        return math.hypot(*pts[0])
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


__all__ = ["RoadReport", "RoadReports", "parse_waze_alerts", "parse_waze_traffic", "parse_events",
           "features", "counts", "count_words", "age_words", "KINDS", "KIND_COLORS", "LEGEND"]

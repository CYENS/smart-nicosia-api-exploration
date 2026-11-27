"""Incidents: detections and city alarms grouped by place and time, with what people did.

Three lamps on one street going silent together are one probable power cut, not three faults;
a bin that loses its connection eighteen times in two days is one flapping bin. `group()` links
detections (and the members of incidents already open) when they share a family of cause, a
place (the same street, or within a radius) and a time (they began close together, or overlap),
and every connected group is one incident. An incident keeps its members, the evidence with
times, its severity and place, and a history of actions (acknowledge, snooze until, resolve,
reopen), each with a time and an optional name. The twin resolves an incident itself once
every member has stopped being true, and reopens it if it comes back.

`Book` holds the incidents in this process and writes them to the twin's memory (DuckDB, tables
`incidents` and `incident_actions`), so they outlive a restart. Without a memory they live in
this process only.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

from chatbot.geo import haversine_m
from chatbot.memory import Memory, Unavailable
from chatbot.settings import TIMEZONE
from chatbot.watch.detectors import RANK, Detection, Observed, span, when, worst

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id VARCHAR PRIMARY KEY,
    number INTEGER,
    title VARCHAR,
    summary VARCHAR,
    family VARCHAR,
    severity VARCHAR,                -- high | medium | low
    status VARCHAR,                  -- open | acknowledged | resolved
    snoozed_until TIMESTAMPTZ,
    active BOOLEAN,                  -- still true by the latest readings
    start_ts TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    created_ts TIMESTAMPTZ,
    updated_ts TIMESTAMPTZ,
    place VARCHAR,
    lat DOUBLE,
    lon DOUBLE,
    members VARCHAR                  -- JSON: the devices and detections, with their evidence
);
CREATE TABLE IF NOT EXISTS incident_actions (
    incident VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    action VARCHAR NOT NULL,         -- acknowledged | snoozed | unsnoozed | resolved | reopened | merged
    by_name VARCHAR,                 -- who, if they said; 'Twin' for the twin itself
    until_ts TIMESTAMPTZ,
    note VARCHAR,
    PRIMARY KEY (incident, ts, action)
);
"""

TWIN = "Twin"
RESOLVE_AFTER = timedelta(minutes=20)  # every member ended this long ago: the twin resolves it
KEEP_ENDED = timedelta(hours=24)  # a group that ended longer ago than this isn't news
MAX_SNOOZE = timedelta(days=7)
MAX_EVIDENCE = 80

# How things link into one incident, per family: the radius (metres; None = the same street
# only, inf = anywhere in the city) and how close in time they must begin (None = overlapping).
LINKS: dict[str, tuple[float | None, timedelta | None]] = {
    "outage": (250.0, timedelta(minutes=45)),  # went quiet together: power or network
    "lighting": (300.0, timedelta(minutes=45)),  # switched on together by day: a controller
    "power": (None, None),  # out of line on one street at the same time
    "parking": (60.0, None),  # neighbouring bays stuck together
    "bins": (40.0, timedelta(hours=6)),  # containers at one spot
    "air": (math.inf, timedelta(hours=2)),  # an episode over the city
    "roads": (500.0, timedelta(hours=1)),  # a closure, an accident and the jams behind them
    "other": (0.0, timedelta(0)),  # never across devices
    "feed": (0.0, timedelta(0)),  # a data feed stalling is its own incident
}
SAME_DEVICE_GAP = {"outage": timedelta(hours=18), "lighting": timedelta(hours=18)}
DEFAULT_SAME_GAP = timedelta(hours=8)

CATEGORIES = {"outage": "Outage", "lighting": "Lit by day", "power": "Lamp power",
              "parking": "Parking sensor", "bins": "Bins", "air": "Air", "other": "City alarm",
              "feed": "Data feed", "roads": "Roads"}
NOUNS = {"Lighting": ("lamp", "lamps"), "Parking": ("bay", "bays"), "Wastebin": ("bin", "bins"),
         "smartsense": ("air station", "air stations"), "InfoPoints": ("info screen", "info screens"),
         "Camera": ("camera", "cameras"), "feed": ("data feed", "data feeds"),
         "road_report": ("road report", "road reports")}
ACTIONS = ("acknowledge", "snooze", "unsnooze", "resolve", "reopen")
DONE = {"acknowledge": "acknowledged", "snooze": "snoozed", "unsnooze": "unsnoozed",
        "resolve": "resolved", "reopen": "reopened"}


class ActionError(ValueError):
    """An action that can't be taken (unknown incident, bad time); the message says why."""


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def local(ms: int | None) -> str | None:
    return _dt(ms).astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if ms else None


def noun(device_type: str, n: int = 1) -> str:
    one, many = NOUNS.get(device_type, ("device", "devices"))
    return one if n == 1 else many


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# -- grouping -------------------------------------------------------------------------------------


def _end(m: dict) -> int | None:
    return m.get("end")


def _overlap(a: dict, b: dict, gap: timedelta) -> bool:
    g = int(gap.total_seconds() * 1000)
    a0, a1 = a["start"], _end(a) or math.inf
    b0, b1 = b["start"], _end(b) or math.inf
    return a0 <= b1 + g and b0 <= a1 + g


def linked(a: dict, b: dict) -> bool:
    """Whether two members belong to one incident."""
    if a["family"] != b["family"]:
        return False
    family = a["family"]
    if a["device"] == b["device"]:
        return _overlap(a, b, SAME_DEVICE_GAP.get(family, DEFAULT_SAME_GAP))
    radius, within = LINKS.get(family, LINKS["other"])
    same_street = bool(a.get("street")) and a.get("street") == b.get("street")
    if radius is None:
        near = same_street
    elif radius == math.inf:
        near = True
    elif same_street and family in ("outage", "lighting"):
        near = True
    elif None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        near = False
    else:
        near = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= radius
    if not near:
        return False
    if within is None:
        return _overlap(a, b, timedelta(0))
    return abs(a.get("link", a["start"]) - b.get("link", b["start"])) <= within.total_seconds() * 1000


def components(members: list[dict]) -> list[list[int]]:
    """Connected groups of members (indexes) under `linked`, by union-find."""
    parent = list(range(len(members)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            if find(i) != find(j) and linked(members[i], members[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(members)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


# -- what an incident says ----------------------------------------------------------------------


def place_of(members: list[dict], streets: Callable[[float, float], str | None] | None = None) -> dict:
    pts = [(m["lat"], m["lon"]) for m in members if m.get("lat") is not None and m.get("lon") is not None]
    lat = sum(p[0] for p in pts) / len(pts) if pts else None
    lon = sum(p[1] for p in pts) / len(pts) if pts else None
    named = Counter(m["street"] for m in members if m.get("street"))
    devices = {m["device"] for m in members}
    if named and len(named) == 1 and sum(named.values()) == len(members):
        street = next(iter(named))
        label, phrase = street, f"on {street}"
    elif named:
        street = named.most_common(1)[0][0]
        label = phrase = f"near {street}"
    else:
        street = streets(lat, lon) if (streets and lat is not None) else None
        if street:
            label = phrase = f"near {street}"
        elif len(devices) == 1:
            label = members[0].get("label") or members[0]["device"]
            phrase = f"at {label}"
        else:
            label, phrase = "Nicosia", "across the city"
    return {"label": label, "phrase": phrase, "street": street, "lat": lat, "lon": lon}


def compose(members: list[dict], place: dict) -> tuple[str, str, str]:
    """(title, severity, summary) of an incident from its members."""
    family = members[0]["family"]
    devices: dict[str, dict] = {}
    for m in sorted(members, key=lambda m: m["start"]):
        devices.setdefault(m["device"], m)
    n = len(devices)
    types = Counter(m["device_type"] for m in devices.values())
    severity = worst(*(m["severity"] for m in members))
    on = place.get("phrase") or place["label"]
    links = sorted(m.get("link", m["start"]) for m in devices.values())
    spread = span(timedelta(milliseconds=links[-1] - links[0])) if n > 1 else ""
    first = devices[next(iter(devices))]
    one = f"{cap(noun(first['device_type']))} {first['device']}"
    if family == "feed":  # a feed has no place: its name and what is true of it now say it
        lead = max(members, key=lambda m: (m.get("end") is None, m["start"]))
        story = (lead.get("extra") or {}).get("story") or f"{first['label']}: {lead['what']}."
        return f"{first.get('label') or one}: {lead['what']}", severity, story
    if family == "roads":
        return roads_story(list(devices.values()), severity, on)
    if n == 1:
        # One device: what is true of it now leads (the active finding, else the latest), the
        # rest of its story goes in the summary.
        lead = max(members, key=lambda m: (m.get("end") is None, m["start"]))
        same = sum(1 for m in members if m["rule"] == lead["rule"])
        what = lead["what"] + (f" ({same} episodes)" if same > 1 else "")
        others = [m["what"] for m in sorted(members, key=lambda m: m["start"])
                  if m["rule"] != lead["rule"]]
        where_one = f" on {first['street']}" if first.get("street") else ""
        story = f"{one}{where_one}: {what}." + (
            " Also: " + "; ".join(dict.fromkeys(others)) + "." if others else "")
        return f"{one}{where_one}: {what}", severity, story
    kinds = ", ".join(f"{c} {noun(t, c)}" for t, c in types.most_common())
    if family == "outage":
        if set(types) == {"Lighting"}:
            if n >= 3:
                return (f"Probable power cut {on}", "high",
                        f"{n} lamps {on} went dark at night within {spread} of each other.")
            return f"{n} lamps dark {on}", worst(severity, "medium"), \
                f"{n} lamps {on} went dark within {spread} of each other."
        sev = "high" if n >= 3 else worst(severity, "medium")
        return (f"{n} devices went quiet together {on}", sev,
                f"{cap(kinds)} stopped reporting within {spread} of each other: a power or "
                "network fault is likely.")
    if family == "lighting":
        return (f"{n} lamps lit by day {on}", worst(severity, "medium"),
                f"{n} lamps {on} reported lit while the sun was up, within {spread}: a controller "
                "or its schedule is the likely cause.")
    if family == "power":
        return (f"{n} lamps {on} draw power out of line", severity,
                f"{n} lamps {on} draw power unlike the other lamps of the street.")
    if family == "parking":
        return (f"{n} bays stuck occupied {on}", severity,
                f"{n} neighbouring bays have reported occupied for days while they keep "
                "reporting: the sensors, or something parked for good.")
    if family == "bins":
        return (f"{n} bins at one spot {on}", severity,
                f"{cap(kinds)} at one spot {on}: " + "; ".join(
                    f"{m['device']} {m['what']}" for m in devices.values()) + ".")
    if family == "air":
        return (f"Poor air at {n} stations", "high" if n >= 3 else worst(severity, "medium"),
                f"{n} of the city's air stations raised high CAQI within {spread}: a city-wide "
                "episode (dust, smoke or traffic) is more likely than one street.")
    return (f"{n} alarms {on}", severity, f"{cap(kinds)} raised alarms {on}.")


ROAD_NOUNS = {"closure": ("road closed", "roads closed"), "jam": ("jam", "jams"),
              "accident": ("accident", "accidents")}


def roads_story(reports: list[dict], severity: str, on: str) -> tuple[str, str, str]:
    """A closure, an accident and the jams near them, as one incident: (title, severity, story)."""
    kinds = Counter((m.get("extra") or {}).get("kind", "jam") for m in reports)
    parts = []
    for k in ("accident", "closure", "jam"):
        if kinds.get(k):
            one, many = ROAD_NOUNS[k]
            parts.append(one if kinds[k] == 1 else f"{kinds[k]} {many}")
    words = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    if len(reports) == 1:
        m = reports[0]
        where = f"on {m['street']}" if m.get("street") else on
        return f"{cap(m['what'])} {where}", severity, \
            f"{cap(m['what'])} {where} at {when(_dt(m['start']))}, by {_feed_name(m)}."
    story = "; ".join(f"{m.get('label') or m['what']} at {when(_dt(m['start']))}"
                      for m in sorted(reports, key=lambda m: m["start"]))
    title = f"{cap(words)} {on}" if kinds.get("closure") or len(parts) > 1 else f"{cap(words)} reported {on}"
    return title, severity, f"{cap(words)} {on} within an hour of each other: {story}."


def _feed_name(m: dict) -> str:
    feed = (m.get("extra") or {}).get("feed")
    return {"waze_alerts": "Waze drivers", "waze_traffic": "Waze's jam lines",
            "traffic_events": "the Public Works Department"}.get(feed or "", "a road report")


def merge_evidence(members: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for m in members:
        for e in m.get("evidence", []):
            k = (e["t"], e.get("device"), e["text"])
            if k not in seen:
                seen.add(k)
                out.append(e)
    out.sort(key=lambda e: e["t"])
    if len(out) > MAX_EVIDENCE:
        out = out[:20] + out[-(MAX_EVIDENCE - 20):]
    return out


def _member(d: Detection, now: datetime) -> dict:
    m = d.to_member()
    m["seen"] = _ms(now)
    m["evidence"] = [{"t": _ms(e.t), "text": e.text, "device": e.device, "kind": e.kind,
                      "value": e.value, "unit": e.unit} for e in d.evidence]
    return m


# -- the book -------------------------------------------------------------------------------------


class Book:
    """The incidents, in this process and in the twin's memory."""

    def __init__(self, memory: Memory | None, now: Callable[[], datetime]):
        self.memory = memory
        self.now = now
        self.lock = threading.RLock()
        self.incidents: dict[str, dict] = {}
        self.loaded = False
        self.dirty: set[str] = set()  # not saved yet
        self._touched: set[str] = set()  # changed by the update under way
        self.last_error: str | None = None

    # -- storage ---------------------------------------------------------------------------------

    def load(self) -> None:
        """Read the stored incidents once (a missing table or store is an empty book)."""
        if self.loaded:
            return
        rows, acts = [], []
        if self.memory is not None and self.memory.exists():
            try:
                with self.memory.reading(timeout=5.0) as con:
                    rows = con.execute(
                        """SELECT id, number, title, summary, family, severity, status,
                                  epoch_ms(snoozed_until), active, epoch_ms(start_ts),
                                  epoch_ms(last_seen), epoch_ms(created_ts), epoch_ms(updated_ts),
                                  place, lat, lon, members FROM incidents""").fetchall()
                    acts = con.execute(
                        """SELECT incident, epoch_ms(ts), action, by_name, epoch_ms(until_ts), note
                           FROM incident_actions ORDER BY ts""").fetchall()
            except duckdb.CatalogException:
                rows, acts = [], []
            except Unavailable as e:
                self.last_error = str(e)
                return  # try again next time
        with self.lock:
            for (iid, number, title, summary, family, severity, status, snoozed, active, start,
                 last_seen, created, updated, place, lat, lon, members) in rows:
                street = None
                try:
                    ms_ = json.loads(members or "[]")
                except ValueError:
                    ms_ = []
                streets = {m.get("street") for m in ms_ if m.get("street")}
                if len(streets) == 1:
                    street = next(iter(streets))
                self.incidents[iid] = {
                    "id": iid, "number": number, "title": title, "summary": summary,
                    "family": family, "severity": severity, "status": status,
                    "snoozed_until": snoozed, "active": bool(active), "start": start,
                    "last_seen": last_seen, "created": created, "updated": updated,
                    "place": {"label": place, "street": street, "lat": lat, "lon": lon},
                    "members": ms_, "actions": []}
            for iid, t, action, by, until, note in acts:
                if iid in self.incidents:
                    self.incidents[iid]["actions"].append(
                        {"t": t, "action": action, "by": by, "until": until, "note": note})
            self.loaded = True

    def save(self) -> bool:
        """Write the changed incidents (and their new actions). False if the store was busy:
        they stay marked and go with the next save."""
        with self.lock:
            ids = sorted(self.dirty)
            rows = [self.incidents[i] for i in ids if i in self.incidents]
        if not rows or self.memory is None:
            with self.lock:
                self.dirty.difference_update(ids)
            return True
        try:
            with self.memory.writing(timeout=5.0) as con:
                con.execute(SCHEMA)
                for x in rows:
                    con.execute(
                        "INSERT OR REPLACE INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [x["id"], x["number"], x["title"], x["summary"], x["family"],
                         x["severity"], x["status"],
                         _dt(x["snoozed_until"]) if x.get("snoozed_until") else None,
                         x["active"], _dt(x["start"]), _dt(x["last_seen"]), _dt(x["created"]),
                         _dt(x["updated"]), x["place"]["label"], x["place"]["lat"],
                         x["place"]["lon"],
                         json.dumps(x["members"], ensure_ascii=False, separators=(",", ":"))])
                    for a in x["actions"]:
                        con.execute(
                            "INSERT OR IGNORE INTO incident_actions VALUES (?, ?, ?, ?, ?, ?)",
                            [x["id"], _dt(a["t"]), a["action"], a.get("by"),
                             _dt(a["until"]) if a.get("until") else None, a.get("note")])
        except Exception as e:  # noqa: BLE001 - busy or broken: keep them for the next save
            self.last_error = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
            log.warning("incidents: could not save: %s", self.last_error)
            return False
        with self.lock:
            self.dirty.difference_update(ids)
        self.last_error = None
        return True

    # -- updating from detections ----------------------------------------------------------------

    def _touch(self, iid: str) -> None:
        self.dirty.add(iid)
        self._touched.add(iid)

    def _next_number(self) -> int:
        return max((x["number"] for x in self.incidents.values()), default=0) + 1

    def _act(self, x: dict, action: str, by: str | None, now: datetime, *,
             until: int | None = None, note: str | None = None) -> dict:
        t = _ms(now)
        while any(a["t"] == t and a["action"] == action for a in x["actions"]):
            t += 1  # two of the same action in one millisecond stay two
        a = {"t": t, "action": action, "by": by, "until": until, "note": note}
        x["actions"].append(a)
        x["updated"] = t
        self._touch(x["id"])
        return a

    def _refresh(self, x: dict, now: datetime, streets) -> None:
        members = x["members"]
        x["place"] = place_of(members, streets)
        x["title"], x["severity"], x["summary"] = compose(members, x["place"])
        x["active"] = any(m.get("end") is None for m in members)
        x["start"] = min(m["start"] for m in members)
        ends = [m.get("end") or m.get("seen") or _ms(now) for m in members]
        x["last_seen"] = _ms(now) if x["active"] else max(ends)
        x["family"] = members[0]["family"]

    def update(self, detections: list[Detection], now: datetime,
               streets: Callable[[float, float], str | None] | None = None,
               ran: set[str] | None = None) -> list[dict]:
        """Group fresh detections with the open incidents. Returns the changes, each
        {"change": new | escalated | grew | reopened | resolved | merged, "incident": summary}."""
        self.load()
        changes: list[dict] = []
        with self.lock:
            self._touched = set()
            live = [x for x in self.incidents.values()
                    if x["status"] != "resolved" or (
                        _last_action_by(x) == TWIN and now - _dt(x["last_seen"]) < KEEP_ENDED)]
            fresh = [_member(d, now) for d in detections]
            # Match each fresh member to a stored one (same key, or the same run continuing).
            stored = [(x, m) for x in live for m in x["members"]]
            matched: set[int] = set()
            unmatched_fresh = []
            for f in fresh:
                hit = next(((x, m) for x, m in stored if m["key"] == f["key"]), None)
                if hit is None:
                    gap = SAME_DEVICE_GAP.get(f["family"], DEFAULT_SAME_GAP).total_seconds() * 1000
                    hit = next(((x, m) for x, m in stored
                                if m["rule"] == f["rule"] and m["device"] == f["device"]
                                and m.get("end") is None and f["start"] <= m.get("seen", m["start"]) + gap),
                               None)
                if hit is None:
                    unmatched_fresh.append(f)
                    continue
                x, m = hit
                matched.add(id(m))
                before = (m.get("end"), m["severity"], m["what"])
                start = min(m["start"], f["start"])
                link = min(m.get("link", m["start"]), f.get("link", f["start"]))
                m.update({k: v for k, v in f.items() if k not in ("key", "start", "link")})
                m["start"], m["link"] = start, link
                if before != (m.get("end"), m["severity"], m["what"]):
                    self._touch(x["id"])
            # Stored members still marked true that their detector (which ran) didn't find
            # again have ended. A detector that failed this time says nothing either way.
            for x, m in stored:
                if id(m) in matched or m.get("end") is not None:
                    continue
                if ran is None or m["rule"] in ran:
                    m["end"] = m.get("seen") or _ms(now)
                    self._touch(x["id"])
            # Link what is new with each other and with the open incidents.
            nodes = [(None, f) for f in unmatched_fresh] + [(x, m) for x in live for m in x["members"]]
            for group in components([m for _, m in nodes]):
                owners = sorted({nodes[i][0]["id"] for i in group if nodes[i][0] is not None},
                                key=lambda iid: self.incidents[iid]["number"])
                new = [nodes[i][1] for i in group if nodes[i][0] is None]
                if not owners:
                    if not new:
                        continue
                    if all(m.get("end") is not None and now - _dt(m["end"]) > KEEP_ENDED for m in new):
                        continue
                    changes += self._open(new, now, streets)
                    continue
                keep = self.incidents[owners[0]]
                for other in owners[1:]:  # two incidents that turned out to be one
                    o = self.incidents[other]
                    keep["members"] += o["members"]
                    o["members"] = []
                    o["status"] = "resolved"
                    self._act(o, "merged", TWIN, now, note=f"merged into {keep['id']}")
                    changes.append({"change": "merged", "incident": self.summary(o, now)})
                    del self.incidents[other]
                if new:
                    keep["members"] += new
                    self._touch(keep["id"])
            # Every incident that changed: what it says now, and its status.
            for x in list(self.incidents.values()):
                if x["id"] not in self._touched or not x["members"]:
                    continue
                changes += self._settle(x, now, streets)
            for x in self.incidents.values():  # the twin resolves what cleared a while ago
                if (x["status"] != "resolved" and not x["active"] and x["members"]
                        and now - _dt(x["last_seen"]) >= RESOLVE_AFTER):
                    x["status"] = "resolved"
                    self._act(x, "resolved", TWIN, now, note=_cleared(x))
                    changes.append({"change": "resolved", "incident": self.summary(x, now)})
        return changes

    def _open(self, members: list[dict], now: datetime, streets) -> list[dict]:
        number = self._next_number()
        x = {"id": f"INC-{number}", "number": number, "status": "open", "snoozed_until": None,
             "created": _ms(now), "updated": _ms(now), "members": members, "actions": []}
        self._refresh(x, now, streets)
        self.incidents[x["id"]] = x
        self._touch(x["id"])
        if not x["active"]:
            # Seen only after it was over (the first look at the memory): history, not news.
            x["status"] = "resolved"
            self._act(x, "resolved", TWIN, now, note=_cleared(x))
            return [{"change": "history", "incident": self.summary(x, now)}]
        return [{"change": "new", "incident": self.summary(x, now)}]

    def _settle(self, x: dict, now: datetime, streets) -> list[dict]:
        before = (x.get("severity"), len({m["device"] for m in x["members"]}), x.get("active"))
        self._refresh(x, now, streets)
        x["updated"] = _ms(now)
        out = []
        if x["status"] == "resolved" and x["active"]:
            last = x["actions"][-1] if x["actions"] else None
            by_person = last is not None and last["action"] == "resolved" and last.get("by") != TWIN
            fresh_since = last is None or any(m["start"] > last["t"] for m in x["members"])
            if not by_person or fresh_since:
                x["status"] = "open"
                self._act(x, "reopened", TWIN, now, note="it is happening again")
                out.append({"change": "reopened", "incident": self.summary(x, now)})
                return out
        if RANK.get(x["severity"], 0) > RANK.get(before[0] or "low", 0):
            out.append({"change": "escalated", "incident": self.summary(x, now)})
        elif len({m["device"] for m in x["members"]}) > before[1]:
            out.append({"change": "grew", "incident": self.summary(x, now)})
        else:
            out.append({"change": "updated", "incident": self.summary(x, now)})
        return out

    # -- what people do --------------------------------------------------------------------------

    def act(self, iid: str, action: str, *, by: str | None = None, until: datetime | None = None,
            note: str | None = None) -> dict:
        self.load()
        now = self.now()
        by = (by or "").strip()[:60] or None
        note = (note or "").strip()[:300] or None
        with self.lock:
            x = self.incidents.get(iid.strip().upper())
            if x is None:
                raise ActionError(f"no incident {iid!r}")
            if action not in ACTIONS:
                raise ActionError(f"action must be one of {', '.join(ACTIONS)}")
            if action == "snooze":
                if until is None:
                    raise ActionError("say until when to snooze it")
                if until <= now:
                    raise ActionError("snooze until a time in the future")
                if until - now > MAX_SNOOZE:
                    raise ActionError("snooze for 7 days at most")
                if x["status"] == "resolved":
                    raise ActionError("it is resolved; reopen it first")
                x["snoozed_until"] = _ms(until)
            elif action == "unsnooze":
                x["snoozed_until"] = None
            elif action == "acknowledge":
                if x["status"] == "resolved":
                    raise ActionError("it is resolved; reopen it first")
                x["status"] = "acknowledged"
            elif action == "resolve":
                x["status"] = "resolved"
                x["snoozed_until"] = None
            elif action == "reopen":
                x["status"] = "open"
            self._act(x, DONE[action], by, now, until=x["snoozed_until"] if action == "snooze" else None,
                      note=note)
            return self.detail(x, now)

    # -- reading ---------------------------------------------------------------------------------

    def get(self, iid: str) -> dict | None:
        self.load()
        with self.lock:
            return self.incidents.get((iid or "").strip().upper())

    def status_of(self, x: dict, now: datetime) -> str:
        if x["status"] == "resolved":
            return "resolved"
        if x.get("snoozed_until") and x["snoozed_until"] > _ms(now):
            return "snoozed"
        return x["status"]

    def summary(self, x: dict, now: datetime) -> dict:
        devices: dict[str, dict] = {}
        for m in x["members"]:
            devices.setdefault(m["device"], m)
        last = x["actions"][-1] if x["actions"] else None
        return {
            "id": x["id"], "title": x["title"], "severity": x["severity"],
            "status": self.status_of(x, now), "active": x["active"],
            "category": CATEGORIES.get(x["family"], "Alarm"), "family": x["family"],
            "place": x["place"]["label"], "lat": x["place"]["lat"], "lon": x["place"]["lon"],
            "start": local(x["start"]), "start_ms": x["start"],
            "last_seen": local(x["last_seen"]), "last_seen_ms": x["last_seen"],
            "snoozed_until": local(x.get("snoozed_until")),
            "devices": len(devices), "ids": list(devices)[:8],
            "kinds": dict(Counter(m["kind"] for m in devices.values())),
            "source": "+".join(sorted({m["source"] for m in x["members"]})) or "twin",
            "last_action": ({"action": last["action"], "by": last.get("by"), "at": local(last["t"])}
                            if last else None),
            "updated_ms": x["updated"],
        }

    def detail(self, x: dict, now: datetime) -> dict:
        out = self.summary(x, now)
        devices: dict[str, dict] = {}
        for m in sorted(x["members"], key=lambda m: m["start"]):
            d = devices.setdefault(m["device"], {
                "id": m["device"], "kind": m["kind"], "label": m["label"], "street": m.get("street"),
                "lat": m.get("lat"), "lon": m.get("lon"), "what": m["what"],
                "severity": m["severity"], "start": local(m["start"]), "end": local(m.get("end")),
                "active": m.get("end") is None, "source": m["source"]})
            d.update({"what": m["what"], "end": local(m.get("end")),
                      "active": d["active"] or m.get("end") is None})
        out["summary"] = x["summary"]
        out["members"] = list(devices.values())
        out["evidence"] = [{"time": _dt(e["t"]).astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S"),
                            **{k: v for k, v in e.items() if k != "t" and v is not None}}
                           for e in merge_evidence(x["members"])]
        out["actions"] = [{"at": local(a["t"]), "t_ms": a["t"], "action": a["action"],
                           "by": a.get("by"), "until": local(a.get("until")), "note": a.get("note")}
                          for a in x["actions"]]
        out["created"] = local(x["created"])
        return out

    def query(self, *, status: str = "unresolved", severity: str | None = None,
              family: str | None = None, entity: str | None = None, since: datetime | None = None,
              near: tuple[float, float, float] | None = None, street: str | None = None,
              limit: int = 50) -> list[dict]:
        """Incidents newest first, filtered. `status`: open, acknowledged, snoozed, resolved,
        unresolved (anything not resolved) or all; `severity` is the least severe kept."""
        self.load()
        now = self.now()
        with self.lock:
            xs = [x for x in self.incidents.values() if x["members"]]
        out = []
        for x in xs:
            st = self.status_of(x, now)
            if status == "unresolved" and st == "resolved":
                continue
            if status not in ("all", "unresolved") and st != status:
                continue
            if severity and RANK.get(x["severity"], 0) < RANK.get(severity, 0):
                continue
            if family and x["family"] != family:
                continue
            if entity and not any(m["device"].lower() == entity.lower() for m in x["members"]):
                continue
            if since and x["last_seen"] < _ms(since):
                continue
            if street and not any((m.get("street") or "").lower() == street.lower() for m in x["members"]):
                continue
            if near:
                lat, lon, radius = near
                if not any(m.get("lat") is not None and haversine_m(lat, lon, m["lat"], m["lon"]) <= radius
                           for m in x["members"]):
                    continue
            out.append(x)
        order = {"open": 0, "acknowledged": 1, "snoozed": 2, "resolved": 3}
        out.sort(key=lambda x: (order.get(self.status_of(x, now), 4), -RANK.get(x["severity"], 0),
                                -x["last_seen"]))
        return [self.summary(x, now) for x in out[:limit]]

    def counts(self) -> dict:
        self.load()
        now = self.now()
        with self.lock:
            xs = [x for x in self.incidents.values() if x["members"]]
        c = Counter()
        for x in xs:
            st = self.status_of(x, now)
            if st == "resolved":
                continue
            c["unresolved"] += 1
            c[st] += 1
            if st != "snoozed":
                c[x["severity"]] += 1
        return {"unresolved": c["unresolved"], "open": c["open"], "acknowledged": c["acknowledged"],
                "snoozed": c["snoozed"], "high": c["high"], "medium": c["medium"], "low": c["low"]}


def _last_action_by(x: dict) -> str | None:
    return x["actions"][-1].get("by") if x["actions"] else None


def _cleared(x: dict) -> str:
    whys = Counter()
    for m in x["members"]:
        for e in reversed(m.get("evidence", [])):
            if e["text"] in ("reported again", "fill back under 80%", "the index back under 75"):
                whys[e["text"]] += 1
                break
    if whys:
        return "cleared: " + ", ".join(f"{w}" for w, _ in whys.most_common(2))
    return "cleared: no longer seen in the readings"


def streets_near(obs: Observed, radius_m: float = 200.0) -> Callable[[float, float], str | None]:
    """The street of the nearest lamp within radius_m (lamps carry their street's name)."""
    lamps = [(p.lat, p.lon, p.street) for p in obs.of_type("Lighting")
             if p.street and p.lat is not None and p.lon is not None]

    def nearest(lat: float, lon: float) -> str | None:
        best = min(((haversine_m(lat, lon, a, b), s) for a, b, s in lamps), default=None,
                   key=lambda x: x[0])
        return best[1] if best and best[0] <= radius_m else None
    return nearest


__all__ = ["Book", "ActionError", "components", "linked", "compose", "place_of", "streets_near",
           "SCHEMA", "TWIN", "when"]

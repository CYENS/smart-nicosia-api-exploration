"""The counting-lines register: what each camera's counting line measures, and which lines of a
camera may be added together, so the twin never counts one car twice because it crossed two of
a camera's lines.

A camera's video analytics counts crossings of lines drawn on its picture, per kind of road user
and per way across. One camera can have several lines: a pair counting cars in and out, a line
across the pavement for people, one line per movement through a junction (where a turning car
can cross two), or lines on two streets that a walker can cross one after the other. Adding
every line of a camera counts some road users twice, and mixes in the camera's misreadings (a
"person" on a line drawn for cars).

The register, data/traffic_lines.csv, one row per line, says:
- measures: people, vehicles or both: which kinds of road user its counts stand for;
- direction: in, out, both, or "one way" when its name doesn't say which way;
- street: the road or approach it watches, in words;
- adds_up: yes (it goes into the camera's totals), no (kept apart: it overlaps another line),
  or review (not decided yet, so kept apart until someone decides);
- reviewed: when a person confirmed it on the twin's camera walkthrough; empty for a
  suggestion drawn from the line's name and settings.

A line the file doesn't list (a new camera, a line the city renamed) is suggested here from its
name and settings, so the register always covers every line the city has. Totals never add
across cameras either: a car driving down a street passes several of them.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import tempfile
import threading
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from collector import parse
from collector.http import ApiError
from chatbot.settings import ROOT

log = logging.getLogger(__name__)

REGISTER = ROOT / "data" / "traffic_lines.csv"
FIELDS = ("analytics_id", "camera", "line_id", "line_name", "measures", "direction", "street",
          "adds_up", "reviewed", "note")
MEASURES = ("people", "vehicles", "both")
DIRECTIONS = ("in", "out", "both", "one way")
ADDS_UP = ("yes", "no", "review")
PEOPLE = frozenset({0})
VEHICLES = frozenset({1, 2, 3, 4, 5})  # bike, car, motorcycle, bus, truck
KINDS = {"people": PEOPLE, "vehicles": VEHICLES, "both": PEOPLE | VEHICLES}
PREFIXES = {"ST": "Stasikratous", "KM": "Kyriakou Matsi", "MK": "Makariou"}
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class LineRole:
    analytics_id: int
    camera: str
    line_id: int | None
    line_name: str
    measures: str
    direction: str
    street: str
    adds_up: str
    reviewed: str = ""  # an ISO date once a person confirmed it
    note: str = ""

    @property
    def key(self) -> tuple[int, str]:
        return self.analytics_id, self.line_name.strip()

    def kinds(self) -> frozenset[int]:
        return KINDS.get(self.measures, KINDS["both"])

    def public(self) -> dict:
        return asdict(self)


# -- suggestions --------------------------------------------------------------------------------


def _measures(name: str, feed: str, types: set[int]) -> str:
    if re.search(r"\bP ?\d*$", name) or "people" in feed.lower() or name.endswith("People"):
        return "people"
    if types and types <= PEOPLE:
        return "people"
    if (re.search(r"\b(IN|OUT)\b|\bV(\s|$)", name) or "vehicles" in feed.lower()
            or re.search(r"\bL[PR][PR]\b", feed)):
        return "vehicles"
    if types and not types & PEOPLE:
        return "vehicles"
    return "both"


def _direction(name: str, setting: int | None) -> str:
    if "IN/OUT" in name:
        return "both"
    if re.search(r"\bIN\b|\bInbound\b|\(Entry\)", name):
        return "in"
    if re.search(r"\bOUT\b|\bOutbound\b", name):
        return "out"
    return "one way" if setting in (1, 2) else "both"


def _street(name: str) -> str:
    s = re.sub(r"\s+(V IN/OUT|V IN|V OUT|IN V|OUT V|LPR|V|P ?\d*|IN|OUT|People)$", "",
               name.strip())
    s = re.sub(r"\s+(LPR|IN|OUT)$", "", s).strip()
    m = re.match(r"(ST|KM|MK)\s+(.*)", s)
    if m:
        s = f"{PREFIXES[m.group(1)]}, {m.group(2).strip()}"
    return s


def suggest_feed(feed_name: str, analytics_id: int, lines: list[dict]) -> list[LineRole]:
    """A suggestion for every line of one analytics feed, from names and settings alone.

    A line adds up when nothing else on the same feed could count the same road users: a
    single line; a people line beside vehicle lines; an in and an out line. Lines that could
    overlap (two lines counting everything, junction movements, two lines with the same role
    on the same street) are left for review."""
    roles = []
    for line in lines:
        name = (line.get("name") or "").strip()
        roles.append(LineRole(
            analytics_id=analytics_id, camera=feed_name, line_id=line.get("id"),
            line_name=name,
            measures=_measures(name, feed_name, set(line.get("types") or [])),
            direction=_direction(name, line.get("direction")),
            street=_street(name), adds_up="yes"))
    if len(roles) <= 1:
        return roles
    groups = Counter((r.measures, r.direction, r.street) for r in roles)
    out = []
    for r in roles:
        overlaps = r.measures == "both" or groups[(r.measures, r.direction, r.street)] > 1
        out.append(replace(r, adds_up="review") if overlaps else r)
    return out


def camera_lines(cam: dict) -> list[dict]:
    """A camera row's lines (CityData.traffic_cameras): their id, name, settings and ends."""
    raw = cam.get("lines")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    return [line for line in raw or [] if isinstance(line, dict)]


# -- the file -------------------------------------------------------------------------------------

_lock = threading.RLock()  # save_camera reads the file while holding it
_cache: dict[Path, tuple[float, dict[tuple[int, str], LineRole]]] = {}


def read_register(path: Path | None = None) -> dict[tuple[int, str], LineRole]:
    """The reviewed and suggested roles in the file, keyed by (analytics id, line name)."""
    path = Path(path or REGISTER)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {}
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    roles: dict[tuple[int, str], LineRole] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                role = LineRole(
                    analytics_id=int(row["analytics_id"]), camera=row.get("camera") or "",
                    line_id=int(row["line_id"]) if (row.get("line_id") or "").strip() else None,
                    line_name=(row.get("line_name") or "").strip(),
                    measures=row.get("measures") or "both",
                    direction=row.get("direction") or "both",
                    street=row.get("street") or "", adds_up=row.get("adds_up") or "review",
                    reviewed=row.get("reviewed") or "", note=row.get("note") or "")
            except (KeyError, ValueError) as e:
                log.warning("skipping a bad row in %s: %s", path, e)
                continue
            if role.measures not in MEASURES or role.direction not in DIRECTIONS \
                    or role.adds_up not in ADDS_UP:
                log.warning("skipping %s in %s: unknown value", role.key, path)
                continue
            roles[role.key] = role
    with _lock:
        _cache[path] = (mtime, roles)
    return roles


def write_register(roles: Iterable[LineRole], path: Path | None = None) -> None:
    """Write the whole file at once, sorted by camera and line, so diffs stay readable."""
    path = Path(path or REGISTER)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for r in sorted(roles, key=lambda r: (r.analytics_id, r.line_id if r.line_id is not None
                                          else 999, r.line_name)):
        w.writerow({k: ("" if v is None else v) for k, v in r.public().items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def roles(cams: list[dict], path: Path | None = None) -> dict[tuple[int, str], LineRole]:
    """Every current line's role: the file's where it has one, a suggestion otherwise."""
    filed = read_register(path)
    out: dict[tuple[int, str], LineRole] = {}
    for cam in cams:
        suggestions = suggest_feed(cam.get("name") or str(cam["analytics_id"]),
                                   cam["analytics_id"], camera_lines(cam))
        for s in suggestions:
            out[s.key] = filed.get(s.key, s)
    for key, role in filed.items():  # kept even when a feed is missing from this fetch
        out.setdefault(key, role)
    return out


def walkthrough(cams: list[dict], path: Path | None = None) -> list[dict]:
    """Every camera with its lines, their roles and whether a person confirmed them, for the
    review walkthrough: unreviewed cameras first, then by name."""
    filed = read_register(path)
    out = []
    for cam in cams:
        lines = camera_lines(cam)
        suggestions = {s.key: s for s in suggest_feed(cam.get("name") or "", cam["analytics_id"],
                                                     lines)}
        rows = []
        for line in lines:
            key = (cam["analytics_id"], (line.get("name") or "").strip())
            role = filed.get(key) or suggestions[key]
            rows.append({**role.public(), "suggested": suggestions[key].public(),
                         "in_file": key in filed, "setting": line.get("direction"),
                         "count_type": line.get("count_type"), "types": line.get("types") or [],
                         "ends": [line.get(k) for k in ("x1", "y1", "x2", "y2")]})
        out.append({
            "analytics_id": cam["analytics_id"], "name": cam.get("name"),
            "stream_name": cam.get("stream_name"), "status": cam.get("status"),
            "lat": cam.get("lat"), "lon": cam.get("lon"),
            "location_source": cam.get("location_source"), "lines": rows,
            "reviewed": bool(rows) and all(r["reviewed"] for r in rows),
        })
    out.sort(key=lambda c: (c["reviewed"], (c["name"] or "").lower()))
    return out


def save_camera(cams: list[dict], analytics_id: int, changes: list[dict], reviewed_on: str,
                path: Path | None = None) -> list[LineRole]:
    """Store one camera's confirmed lines (every line of it, as given), keeping the rest of the
    file as it is. Each change names its line and gives measures, direction, street, adds_up
    and an optional note; a value outside the allowed ones is a ValueError."""
    cam = next((c for c in cams if c["analytics_id"] == analytics_id), None)
    if cam is None:
        raise KeyError(analytics_id)
    by_name = {(line.get("name") or "").strip(): line for line in camera_lines(cam)}
    current = roles(cams, path)
    saved = []
    for ch in changes:
        name = (ch.get("line_name") or "").strip()
        if name not in by_name:
            raise ValueError(f"camera {analytics_id} has no line called {name!r}")
        for field, allowed in (("measures", MEASURES), ("direction", DIRECTIONS),
                               ("adds_up", ADDS_UP)):
            if ch.get(field) not in allowed:
                raise ValueError(f"{field} must be one of {', '.join(allowed)}")
        street = str(ch.get("street") or "").strip()
        if not street or len(street) > 120:
            raise ValueError("street must be 1 to 120 characters")
        note = str(ch.get("note") or "").strip()[:300]
        base = current.get((analytics_id, name))
        saved.append(LineRole(
            analytics_id=analytics_id, camera=cam.get("name") or "",
            line_id=by_name[name].get("id"), line_name=name, measures=ch["measures"],
            direction=ch["direction"], street=street, adds_up=ch["adds_up"],
            reviewed=reviewed_on, note=note if note else (base.note if base else "")))
    with _lock:
        filed = dict(read_register(path))
    filed.update({r.key: r for r in saved})
    write_register(filed.values(), path)
    return saved


# -- counting -------------------------------------------------------------------------------------


@dataclass
class Tally:
    """Counts by (bucket start, analytics id) and kind, added only as the register allows; the
    per-line counts behind them; and the lines left out, with the reason."""
    added: dict[tuple[datetime, int], Counter]
    by_line: dict[tuple[int, str], Counter]
    left_out: dict[tuple[int, str], str]
    used: set[tuple[int, str]]


def tally(rows: Iterable[parse.TrafficRow], line_roles: dict[tuple[int, str], LineRole]) -> Tally:
    added: dict[tuple[datetime, int], Counter] = defaultdict(Counter)
    by_line: dict[tuple[int, str], Counter] = defaultdict(Counter)
    left_out: dict[tuple[int, str], str] = {}
    used: set[tuple[int, str]] = set()
    for r in rows:
        key = (r.analytics_id, (r.line_name or "").strip())
        by_line[key][r.object_type] += r.total or 0
        role = line_roles.get(key)
        if role is None:
            left_out[key] = "not in the register"
            continue
        if role.adds_up != "yes":
            left_out[key] = ("kept apart: it overlaps another line" if role.adds_up == "no"
                             else "kept apart until someone reviews it")
            continue
        used.add(key)
        if r.object_type in role.kinds():
            added[(r.bucket_start, r.analytics_id)][r.object_type] += r.total or 0
    return Tally(dict(added), dict(by_line), left_out, used)


def how_counted(t: Tally, line_roles: dict[tuple[int, str], LineRole], names: dict[int, str]
                ) -> dict:
    """What an answer says about its counting: how many lines were added, which were left out
    and why, and the rule that holds across cameras."""
    return {
        "lines_added": len(t.used),
        "lines_left_out": [
            {"camera": names.get(a) or str(a), "line": n, "why": why,
             "crossings": sum(t.by_line.get((a, n), Counter()).values())}
            for (a, n), why in sorted(t.left_out.items())][:20],
        "rule": "Each camera's lines are added only where the register says they don't overlap, "
                "and only for the road users each line is for. Cameras are never added together "
                "as if they counted different people: a car driving down a street passes "
                "several.",
    }


# -- per-line hours ---------------------------------------------------------------------------------


def hour_rows(payload: dict, hour_start: datetime, tz) -> list[parse.TrafficRow]:
    """A group_by=stream answer for one hour as hourly per-line rows (the API keys it by day)."""
    return parse.parse_traffic_stream_hour(payload, hour_start, tz)


class LineHours:
    """Every camera's per-line counts, hour by hour: one request per hour covers every line of
    every camera (group_by=stream over that hour). A finished hour never changes, so it is kept
    (the last `keep` of them) and handed to the twin's memory; the hour under way is asked
    again each time."""

    def __init__(self, city, tz, keep: int = 24 * 9):
        self.city, self.tz, self.keep = city, tz, keep
        self._hours: OrderedDict[datetime, list[parse.TrafficRow]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, starts: list[datetime]) -> dict[datetime, list[parse.TrafficRow] | None]:
        """The rows of each hour asked; None for an hour whose request failed."""
        now = self.city.now()
        with self._lock:
            out: dict[datetime, list[parse.TrafficRow] | None] = {
                h: self._hours[h] for h in starts if h in self._hours}
        missing = [h for h in starts if h not in out and h < now]

        def one(h: datetime):
            end = min(later(h), now)
            try:
                payload = self.city.client.get_json(
                    "getTrafficReports", va_ids="[]", group_by="stream",
                    start_date=int(h.timestamp() * 1000),
                    end_date=int(end.timestamp() * 1000) - 1)
            except ApiError as e:
                log.warning("per-line traffic for %s failed: %s", h, e)
                return h, None
            return h, hour_rows(payload, h, self.tz)

        for h, rows in self.city.each(one, missing):
            out[h] = rows
            if rows is not None and later(h) <= now:
                self.city.record("traffic", rows)
                with self._lock:
                    self._hours[h] = rows
                    while len(self._hours) > self.keep:
                        self._hours.popitem(last=False)
        return out


def line_hours(city, tz) -> LineHours:
    """The CityData's shared per-line hours (made on first use)."""
    lh = getattr(city, "_line_hours", None)
    if lh is None:
        lh = LineHours(city, tz)
        city._line_hours = lh
    return lh


def later(t: datetime, by: timedelta = HOUR) -> datetime:
    """`t` plus real time (Python adds to an aware local time by the wall clock, which is wrong
    across a clock change), in `t`'s own zone."""
    return (t.astimezone(timezone.utc) + by).astimezone(t.tzinfo)


def day_hours(day_start: datetime, now: datetime) -> list[datetime]:
    """The hours of one local day that have begun by `now`: 23, 24 or 25 of them."""
    tz = day_start.tzinfo
    next_day = datetime.combine(day_start.date() + timedelta(days=1), time(0), tzinfo=tz)
    end = min(next_day, now)
    hours, h = [], day_start
    while h < end:
        hours.append(h)
        h = later(h)
    return hours

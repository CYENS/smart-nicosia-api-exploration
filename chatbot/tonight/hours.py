"""Pure-Python evaluator for OpenStreetMap `opening_hours` strings in Nicosia."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from chatbot.settings import TIMEZONE

TZ = ZoneInfo(TIMEZONE)

WEEKDAYS = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Day selector tokens like "Mo-Fr", "Mo,Tu,Th,Fr", "Su", "PH"
DAY_EXPR_RE = re.compile(
    r"^(?:(?:mo|tu|we|th|fr|sa|su|ph)(?:\s*[-,]\s*(?:mo|tu|we|th|fr|sa|su|ph))*)\b",
    re.IGNORECASE,
)
TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})(\+)?$")


@dataclass(frozen=True)
class HoursResult:
    is_open: bool | None  # True = open, False = closed, None = cannot parse
    status: str  # "open" | "closed" | "unknown"
    detail: str
    until: str | None = None
    always_open: bool = False


def _parse_days(token: str) -> set[int] | None:
    days: set[int] = set()
    for part in token.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p == "ph":
            # Public holidays are not evaluated; do not treat PH as matching ordinary weekdays.
            continue
        if "-" in p:
            bounds = [x.strip() for x in p.split("-", 1)]
            if bounds[0] not in WEEKDAYS or bounds[1] not in WEEKDAYS:
                return None
            a, b = WEEKDAYS[bounds[0]], WEEKDAYS[bounds[1]]
            cur = a
            while True:
                days.add(cur)
                if cur == b:
                    break
                cur = (cur + 1) % 7
        elif p in WEEKDAYS:
            days.add(WEEKDAYS[p])
        else:
            return None
    return days


def _to_min(hh: int, mm: int) -> int | None:
    if hh == 24 and mm == 0:
        return 24 * 60
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh * 60 + mm
    return None


def _parse_times(expr: str) -> tuple[bool, list[tuple[int, int]]] | None:
    """Return (is_off, [(start_min, end_min), ...]) or None if unparseable."""
    clean = expr.strip()
    low = clean.lower()
    if low in ("off", "closed"):
        return True, []
    if low in ("open", "24/7"):
        return False, [(0, 24 * 60)]
    ranges: list[tuple[int, int]] = []
    for chunk in clean.split(","):
        c = chunk.strip()
        if not c:
            continue
        m = TIME_RANGE_RE.match(c)
        if not m:
            return None
        s = _to_min(int(m.group(1)), int(m.group(2)))
        e = _to_min(int(m.group(3)), int(m.group(4)))
        if s is None or e is None:
            return None
        if m.group(5) == "+" and e <= s:
            e = 24 * 60
        ranges.append((s, e))
    return (False, ranges) if ranges else None


def _fmt_min(m: int) -> str:
    m = m % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def evaluate_opening_hours(raw: str | None, at: datetime) -> HoursResult:
    """Evaluate an OSM `opening_hours` string at local moment `at`."""
    if not raw or not raw.strip():
        return HoursResult(None, "unknown", "Hours not mapped")
    text = raw.strip()
    if text.lower() in ("24/7", "24 hours", "mo-su 00:00-24:00"):
        return HoursResult(True, "open", "Open 24/7", until=None, always_open=True)

    # Reject conditional rules with brackets or sun events rather than guessing.
    if "[" in text or any(w in text.lower() for w in ("sunrise", "sunset", "dawn", "dusk")):
        return HoursResult(None, "unknown", f"Check hours ({text})")

    schedule: dict[int, list[tuple[int, int]]] = {d: [] for d in range(7)}
    parsed_any = False

    for raw_rule in text.split(";"):
        rule = raw_rule.strip()
        if not rule:
            continue
        # Strip inline quoted comments like `"by appointment"`
        rule_no_comment = re.sub(r'"[^"]*"', "", rule).strip()
        if not rule_no_comment:
            continue
        m = DAY_EXPR_RE.match(rule_no_comment)
        if m:
            days = _parse_days(m.group(0))
            if days is None:
                return HoursResult(None, "unknown", f"Hours: {text}")
            rest = rule_no_comment[m.end():].strip()
            if not rest:
                # e.g. "PH" with no time
                continue
        else:
            days = set(range(7))
            rest = rule_no_comment

        parsed = _parse_times(rest)
        if parsed is None:
            return HoursResult(None, "unknown", f"Hours: {text}")
        is_off, ranges = parsed
        parsed_any = True
        for d in days:
            schedule[d] = [] if is_off else ranges

    if not parsed_any:
        return HoursResult(None, "unknown", f"Hours: {text}")

    # Normalize 24/7 equivalent schedules (all 7 days 00:00-24:00)
    if all(schedule[d] == [(0, 24 * 60)] for d in range(7)):
        return HoursResult(True, "open", "Open 24/7", until=None, always_open=True)

    local = at.astimezone(TZ)
    wd = local.weekday()
    prev_wd = (wd - 1) % 7
    now_m = local.hour * 60 + local.minute

    # 1. Check yesterday's overnight spill-over (start >= end, e.g. 19:30-02:00).
    for s, e in schedule[prev_wd]:
        if e <= s and now_m < e:
            return HoursResult(True, "open", f"Open until {_fmt_min(e)}", until=_fmt_min(e))

    # 2. Check today's intervals.
    for s, e in schedule[wd]:
        if e <= s:
            # Overnight span starting today
            if now_m >= s:
                return HoursResult(True, "open", f"Open until {_fmt_min(e)}", until=_fmt_min(e))
        elif s <= now_m < e:
            if e == 24 * 60:
                return HoursResult(True, "open", "Open until midnight", until="24:00")
            return HoursResult(True, "open", f"Open until {_fmt_min(e)}", until=_fmt_min(e))

    # 3. Closed now: find next opening time today or in the next few days.
    upcoming_today = sorted(s for s, _ in schedule[wd] if s > now_m)
    if upcoming_today:
        return HoursResult(False, "closed", f"Closed now, opens {_fmt_min(upcoming_today[0])}")
    for offset in range(1, 7):
        d2 = (wd + offset) % 7
        if schedule[d2]:
            first = min(s for s, _ in schedule[d2])
            when = "tomorrow" if offset == 1 else DAY_NAMES[d2]
            return HoursResult(False, "closed", f"Closed now, opens {when} {_fmt_min(first)}")
    return HoursResult(False, "closed", "Closed now")


def is_open_at(raw: str | None, at: datetime) -> bool | None:
    return evaluate_opening_hours(raw, at).is_open

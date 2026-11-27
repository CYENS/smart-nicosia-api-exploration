"""The traffic archive: every per-line hourly answer exactly as the city's API gave it, kept so
the history survives the API dropping it and can be read again if our reading of it changes.

    <root>/traffic/hourly/YYYY/MM/YYYY-MM-DD.jsonl.gz   one line per hour (local date), gzip
    <root>/traffic/lines/YYYY-MM-DD.json.gz             the camera feeds' line definitions and
                                                        the road-user types, as on that day

Each hourly line holds the window asked for (start and end, in UTC milliseconds), the exact
request, when it was fetched, and the answer untouched. Day files are appended to (gzip allows
several members in one file), so an interrupted backfill resumes by skipping the hours already
there. Nothing here is ever rewritten.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOUR_MS = 3_600_000


def local(ms: int, tz: ZoneInfo) -> datetime:
    """A UTC millisecond time as a local time."""
    return datetime.fromtimestamp(ms / 1000, tz=tz)


def hourly_file(root: Path, window_start_ms: int, tz: ZoneInfo) -> Path:
    d = local(window_start_ms, tz)
    return Path(root) / "traffic" / "hourly" / f"{d:%Y}" / f"{d:%m}" / f"{d:%Y-%m-%d}.jsonl.gz"


def append_hour(root: Path, tz: ZoneInfo, window_start_ms: int, params: dict, payload: object,
                fetched_at: datetime) -> Path:
    """Add one hour's answer to its day's file."""
    path = hourly_file(root, window_start_ms, tz)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "window_start_ms": window_start_ms,
        "window_end_ms": window_start_ms + HOUR_MS - 1,
        "window_start": datetime.fromtimestamp(window_start_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds"),
        "endpoint": "getTrafficReports",
        "params": params,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "response": payload,
    }
    with gzip.open(path, "at", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def read_hours(root: Path) -> Iterator[tuple[Path, dict]]:
    """Every archived hour, oldest day first (a damaged last line is skipped, not fatal)."""
    base = Path(root) / "traffic" / "hourly"
    for path in sorted(base.glob("*/*/*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        yield path, json.loads(line)
                    except ValueError:
                        continue
        except (OSError, EOFError):  # a file cut short by a crash: what was read stands
            continue


def archived_hours(root: Path) -> set[int]:
    """The window starts (UTC ms) already in the archive."""
    return {rec["window_start_ms"] for _, rec in read_hours(root) if "window_start_ms" in rec}


def snapshot_lines(root: Path, tz: ZoneInfo, analytics: object, object_types: object,
                   fetched_at: datetime) -> Path:
    """Keep the line definitions as they are today (one file a day: the day's last fetch)."""
    path = Path(root) / "traffic" / "lines" / f"{fetched_at.astimezone(tz):%Y-%m-%d}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"fetched_at": fetched_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "analytics": analytics, "object_types": object_types}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, separators=(",", ":"))
    return path

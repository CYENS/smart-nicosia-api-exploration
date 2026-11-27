"""What the admin page changed: collection on or paused, recording on or paused, and each job's
switch, cadence and night window.

Kept in a small JSON file beside the database (`collector_controls.json`), so every collector
that writes that database obeys it: the one inside the chatbot, and a `collector run` started by
Task Scheduler or Docker. It is read at the start of every pass, so a change applies on the next
pass, with no restart.

Order of precedence: the built-in defaults, then collector.toml, then this file. Removing a job's
entry (the page's "Reset") hands it back to collector.toml and the defaults.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collector.config import Config

log = logging.getLogger(__name__)

FILE_NAME = "collector_controls.json"

# The cadences the page offers, in minutes. The API takes any value in the bounds below.
CADENCES = (1, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)
MIN_EVERY_MINUTES = 1.0
MAX_EVERY_MINUTES = 7 * 1440.0

JOB_FIELDS = ("enabled", "every_minutes", "night_only")

_lock = threading.Lock()


@dataclass
class Controls:
    # None: not decided on the page. Inside the chatbot CHATBOT_COLLECT then decides; a
    # `collector run` collects. False pauses every collector that writes this database.
    collecting: bool | None = None
    recording: bool | None = None  # the chatbot's recorder; None: CHATBOT_RECORD decides
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    changed_at: str | None = None

    def public(self) -> dict:
        return {"collecting": self.collecting, "recording": self.recording,
                "jobs": {k: dict(v) for k, v in self.jobs.items()}, "changed_at": self.changed_at}


def controls_path(cfg: Config) -> Path:
    return cfg.db_path.with_name(FILE_NAME)


def load(path: Path) -> Controls:
    """The saved controls, or none at all when the file is missing or unreadable (a broken file
    must never stop collection; it is logged and ignored)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Controls()
    except (OSError, ValueError) as e:
        log.warning("ignoring %s: %s", path, e)
        return Controls()
    if not isinstance(data, dict):
        log.warning("ignoring %s: not a JSON object", path)
        return Controls()
    jobs: dict[str, dict[str, Any]] = {}
    for name, values in (data.get("jobs") or {}).items():
        if isinstance(values, dict):
            try:
                jobs[name] = clean_job_patch(values)
            except ValueError as e:
                log.warning("ignoring %s's controls in %s: %s", name, path, e)
    return Controls(
        collecting=_bool_or_none(data.get("collecting")),
        recording=_bool_or_none(data.get("recording")),
        jobs=jobs,
        changed_at=data.get("changed_at") if isinstance(data.get("changed_at"), str) else None,
    )


def save(path: Path, controls: Controls) -> None:
    """Write the whole file at once (a reader never sees half of it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(controls.public(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(path: Path, change) -> Controls:
    """Load, let `change(controls)` edit it, stamp and save, under one lock."""
    with _lock:
        controls = load(path)
        change(controls)
        controls.changed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save(path, controls)
        return controls


def clean_job_patch(values: dict[str, Any]) -> dict[str, Any]:
    """Check a job's changes; raises ValueError naming the field and what it accepts."""
    unknown = [k for k in values if k not in JOB_FIELDS]
    if unknown:
        raise ValueError(f"unknown setting {unknown[0]!r}; a job takes {', '.join(JOB_FIELDS)}")
    out: dict[str, Any] = {}
    for key in ("enabled", "night_only"):
        if key in values:
            if not isinstance(values[key], bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = values[key]
    if "every_minutes" in values:
        v = values["every_minutes"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("every_minutes must be a number of minutes")
        if not MIN_EVERY_MINUTES <= v <= MAX_EVERY_MINUTES:
            raise ValueError(f"every_minutes must be between {MIN_EVERY_MINUTES:g} and "
                             f"{MAX_EVERY_MINUTES:g} (a week)")
        out["every_minutes"] = float(v)
    return out


def apply(cfg: Config, controls: Controls) -> Config:
    """`cfg` with the page's job changes on top. Jobs the config doesn't know are skipped."""
    if not controls.jobs:
        return cfg
    jobs = dict(cfg.jobs)
    for name, values in controls.jobs.items():
        if name in jobs and values:
            jobs[name] = replace(jobs[name], **values)
    return replace(cfg, jobs=jobs)


def effective(cfg: Config) -> Config:
    """The config every collector runs with: `cfg` plus the controls file beside its database."""
    return apply(cfg, load(controls_path(cfg)))


def _bool_or_none(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None

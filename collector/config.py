"""Collector settings: defaults, overridable from a TOML file and environment variables.

Polling volume needs the API owners' OK, so the defaults stay under ~25k Nokia requests a day
(see `collector budget`). Every cadence can be changed in collector.toml, for example:

    [jobs.parking]
    every_minutes = 15
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JobConfig:
    every_minutes: float
    enabled: bool = True
    # Run only between sunset and sunrise in Nicosia (lamps report only while powered).
    night_only: bool = False


# Order matters: a pass runs jobs in this order, so the inventory exists before per-device jobs.
DEFAULT_JOBS: dict[str, JobConfig] = {
    "devices": JobConfig(24 * 60),
    "attributes": JobConfig(24 * 60),
    "cameras_map": JobConfig(24 * 60),
    # TENANT time series that mirrors every device message (status/level/state per message).
    "stream": JobConfig(5),
    "events": JobConfig(5),
    "alarms": JobConfig(5),
    "travel_times": JobConfig(5),
    # Per-device latest telemetry. Parking is hourly, not every 5 min: 303 sensors x 288 polls is
    # 87k requests/day on its own, and the stream job already records every status change.
    "parking": JobConfig(60),
    # Lamps report hourly at most, so polling them every 30 min at night loses nothing.
    "lamps": JobConfig(30, night_only=True),
    "bins": JobConfig(30),
    "infopoints": JobConfig(30),
    "air": JobConfig(60),
    "cameras": JobConfig(60),
    "traffic_daily": JobConfig(24 * 60),
    "traffic_hourly": JobConfig(60),
    "speed": JobConfig(60),
}


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("data/smart_nicosia.duckdb")
    log_file: Path | None = Path("data/collector.log")
    camera_csv: Path = Path("data/camera_locations.csv")

    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi"
    nap_url: str = (
        "https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationDataPublication"
    )
    tenant: str = "CYTA"
    timezone: str = "Asia/Nicosia"
    latitude: float = 35.1725
    longitude: float = 33.3650

    # HTTP politeness.
    timeout_s: float = 60.0
    request_delay_s: float = 0.1  # minimum gap between request starts
    max_retries: int = 2
    concurrency: int = 1  # parallel requests for per-device jobs
    insecure: bool = False
    cafile: str | None = None

    # API behaviour.
    range_cap: int = 1000  # records per getTelemetryRange response (5,000 with credentials)
    stream_keys: tuple[str, ...] = ("deviceName", "status", "level", "state", "connectionState")
    stream_max_pages: int = 20
    stream_max_lookback_hours: float = 6.0
    events_max_pages: int = 5
    alarm_states: tuple[str, ...] = ("ACTIVE_UNACK",)
    alarm_limit: int = 20
    traffic_max_days_per_request: int = 30
    traffic_initial_days: int = 7  # first traffic_daily run fetches this many past days
    speed_group_by: str = "FIVE_MIN"
    speed_line_type: str = "AVERAGE_SPEED"
    speed_max_catchup_days: float = 7.0

    daily_request_budget: int = 25_000
    jobs: dict[str, JobConfig] = field(default_factory=lambda: dict(DEFAULT_JOBS))


_PATH_FIELDS = {"db_path", "log_file", "camera_csv"}
_TUPLE_FIELDS = {"stream_keys", "alarm_states"}


def load_config(path: str | os.PathLike | None = None, **overrides: Any) -> Config:
    """Load defaults, then collector.toml (or `path` / $COLLECTOR_CONFIG), then $COLLECTOR_DB."""
    cfg = Config()
    candidate = path or os.environ.get("COLLECTOR_CONFIG") or None
    if candidate is None and Path("collector.toml").exists():
        candidate = "collector.toml"
    if candidate is not None:
        with open(candidate, "rb") as f:
            cfg = apply_settings(cfg, tomllib.load(f))
    if os.environ.get("COLLECTOR_DB"):
        cfg = replace(cfg, db_path=Path(os.environ["COLLECTOR_DB"]))
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if overrides:
        cfg = apply_settings(cfg, overrides)
    return cfg


def apply_settings(cfg: Config, data: dict[str, Any]) -> Config:
    known = {f.name for f in fields(Config)}
    changes: dict[str, Any] = {}
    for key, value in data.items():
        if key == "jobs":
            jobs = dict(cfg.jobs)
            for name, job_data in value.items():
                if name not in jobs:
                    raise ValueError(f"Unknown job in config: {name!r} (known: {', '.join(jobs)})")
                jobs[name] = replace(jobs[name], **job_data)
            changes["jobs"] = jobs
        elif key not in known:
            raise ValueError(f"Unknown config setting: {key!r}")
        elif key in _PATH_FIELDS:
            changes[key] = Path(value) if value not in (None, "") else None
        elif key in _TUPLE_FIELDS:
            changes[key] = tuple(value)
        else:
            changes[key] = value
    return replace(cfg, **changes)

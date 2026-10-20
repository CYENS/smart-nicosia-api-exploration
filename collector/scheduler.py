"""Decides which jobs are due, runs them, and records every run in job_runs.

`collector run --once` runs one pass over the due jobs and exits (for Task Scheduler or cron
every 5 minutes); `collector run` loops forever (for Docker).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from collector import db, sun
from collector.config import Config
from collector.http import ApiError, Client
from collector.jobs import JOBS, KNOWN_COUNTS, Context, Fetched, Job

log = logging.getLogger(__name__)

# A failed job is retried on a later pass, but not more often than this.
ERROR_RETRY = timedelta(minutes=15)


class FileLock:
    """An OS-level exclusive lock on a file; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"another collector process holds {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()


def lock_path(cfg: Config, name: str) -> Path:
    return cfg.db_path.with_name(cfg.db_path.name + f".{name}.lock")


@dataclass
class RunSummary:
    job: str
    status: str  # ok | partial | error | skipped
    requests: int = 0
    rows: int = 0
    message: str = ""
    seconds: float = 0.0


def last_runs(con) -> dict[str, tuple[datetime | None, datetime | None]]:
    """job -> (start of last ok/partial run, start of last attempt)."""
    rows = con.execute(
        """SELECT job,
                  max(started_at) FILTER (WHERE status IN ('ok', 'partial')),
                  max(started_at)
           FROM job_runs GROUP BY job"""
    ).fetchall()
    return {job: (ok, attempt) for job, ok, attempt in rows}


def is_due(
    every: timedelta, last_ok: datetime | None, last_attempt: datetime | None, now: datetime
) -> bool:
    # Allow some slack so a job every 5 min still runs when the scheduler fires a bit early.
    slack = min(timedelta(seconds=60), every * 0.1)
    if last_ok is not None and now - last_ok < every - slack:
        return False
    if last_attempt is not None and last_attempt != last_ok:
        if now - last_attempt < min(every, ERROR_RETRY) - slack:
            return False
    return True


def due_jobs(cfg: Config, runs: dict, now: datetime) -> list[str]:
    night = sun.is_night(now, cfg.latitude, cfg.longitude)
    due = []
    for name, jc in cfg.jobs.items():
        if not jc.enabled or (jc.night_only and not night):
            continue
        last_ok, last_attempt = runs.get(name, (None, None))
        if is_due(timedelta(minutes=jc.every_minutes), last_ok, last_attempt, now):
            due.append(name)
    return due


def run_job(cfg: Config, job: Job, client: Client, now: datetime) -> RunSummary:
    ctx = Context(cfg, now)
    started = time.monotonic()
    before = client.stats.requests
    fetched = Fetched()
    status = "error"
    rows = 0
    message = ""
    try:
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            plan = job.prepare(con, ctx)
        fetched = job.fetch(client, plan, ctx)
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            stored = job.store(con, fetched, ctx)
        rows, message = stored.rows, stored.message
        status = "partial" if fetched.errors else "ok"
        if fetched.errors:
            message += f"; {len(fetched.errors)} request(s) failed: {fetched.errors[0]}"
    except ApiError as e:
        fetched.errors.append(e)
        message = str(e)
    except Exception as e:  # keep the pass going; the error is recorded and shown in status
        log.exception("job %s failed", job.name)
        message = f"{type(e).__name__}: {e}"
    requests = client.stats.requests - before
    seconds = time.monotonic() - started
    try:
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            con.execute(
                """INSERT INTO job_runs (job, started_at, finished_at, status, requests,
                                         rows_written, errors, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [job.name, now, db.utcnow(), status, requests, rows, len(fetched.errors),
                 message[:2000]],
            )
            for e in fetched.errors[:50]:
                db.record_error(con, job.name, e.endpoint, e.params, e.kind, e.message)
            if status == "error" and not fetched.errors:
                db.record_error(con, job.name, "", None, "internal", message)
    except Exception:
        log.exception("could not record the run of %s", job.name)
    level = logging.INFO if status == "ok" else logging.WARNING
    log.log(level, "%-14s %-7s %4d req %6d rows %5.1fs  %s", job.name, status, requests, rows,
            seconds, message)
    return RunSummary(job.name, status, requests, rows, message, seconds)


def run_pass(
    cfg: Config,
    *,
    only: list[str] | None = None,
    force: bool = False,
    client: Client | None = None,
    now_fn: Callable[[], datetime] = db.utcnow,
) -> list[RunSummary]:
    """Run every due job once (or only `only`; `force` ignores cadence and night windows)."""
    unknown = [j for j in only or [] if j not in JOBS]
    if unknown:
        raise ValueError(f"unknown job(s): {', '.join(unknown)}; known: {', '.join(JOBS)}")
    lock = FileLock(lock_path(cfg, "run"))
    if not lock.acquire():
        log.warning("another `collector run` is still working; skipping this pass")
        return []
    try:
        client = client or Client(cfg)
        with db.session(cfg.db_path, timezone_name=cfg.timezone) as con:
            runs = last_runs(con)
        now = now_fn()
        if force:  # ignores cadence and night windows; disabled jobs run only if named
            names = [n for n, jc in cfg.jobs.items()
                     if (n in only if only is not None else jc.enabled)]
        else:
            names = [n for n in due_jobs(cfg, runs, now) if only is None or n in only]
        summaries = []
        for name in names:
            summaries.append(run_job(cfg, JOBS[name], client, now_fn()))
        return summaries
    finally:
        lock.release()


def run_forever(cfg: Config, *, tick_s: float = 30.0) -> None:
    log.info("collector loop started; database %s", cfg.db_path)
    while True:
        started = time.monotonic()
        try:
            run_pass(cfg)
        except Exception:
            log.exception("pass failed")
        time.sleep(max(1.0, tick_s - (time.monotonic() - started)))


# -- request budget ----------------------------------------------------------------------------


@dataclass
class BudgetLine:
    job: str
    every_minutes: float
    runs_per_day: float
    requests_per_run: float
    requests_per_day: float
    nokia: bool
    enabled: bool
    night_only: bool


def estimate_budget(
    cfg: Config, counts: dict[str, int] | None = None, now: datetime | None = None
) -> list[BudgetLine]:
    counts = counts or KNOWN_COUNTS
    now = now or db.utcnow()
    night = sun.night_fraction(now, cfg.latitude, cfg.longitude)
    lines = []
    for name, jc in cfg.jobs.items():
        job = JOBS[name]
        runs = 1440 / jc.every_minutes * (night if jc.night_only else 1.0)
        per_run = job.requests_per_run(counts, cfg)
        lines.append(BudgetLine(
            name, jc.every_minutes, runs, per_run,
            runs * per_run if jc.enabled else 0.0, job.nokia, jc.enabled, jc.night_only,
        ))
    return lines


def render_budget(cfg: Config, lines: list[BudgetLine]) -> str:
    out = [f"{'job':<15}{'every':>8}{'runs/day':>10}{'req/run':>9}{'req/day':>10}  notes"]
    for b in lines:
        notes = []
        if not b.enabled:
            notes.append("disabled")
        if b.night_only:
            notes.append("night only")
        if not b.nokia:
            notes.append("not Smart Nicosia")
        out.append(f"{b.job:<15}{fmt_every(b.every_minutes):>8}{b.runs_per_day:>10.1f}"
                   f"{b.requests_per_run:>9.1f}{b.requests_per_day:>10.0f}  {', '.join(notes)}")
    nokia = sum(b.requests_per_day for b in lines if b.nokia)
    other = sum(b.requests_per_day for b in lines if not b.nokia)
    out.append(f"\nSmart Nicosia requests/day: ~{nokia:,.0f} (budget {cfg.daily_request_budget:,})"
               f"   other feeds: ~{other:,.0f}")
    if nokia > cfg.daily_request_budget:
        out.append("WARNING: over budget; raise some every_minutes in collector.toml")
    return "\n".join(out)


def fmt_every(minutes: float) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes / 1440:g}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes / 60:g}h"
    return f"{minutes:g}m"

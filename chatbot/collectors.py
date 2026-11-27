"""The collectors, run and steered from the admin page (`/admin`, chatbot/routes/collectors.py).

One `Collectors` per server holds:

- the full collector (collector/scheduler.py) on a thread of this process. It makes a pass every
  `tick_s` while collection is on, and runs a job straight away when the page asks ("Run now").
- the recorder's pause: the chatbot keeps what its pages fetch (chatbot/memory.py).
- the report the page draws: every job in plain words (collector/catalog.py), the source it
  reads (chatbot/sources.py), its switch and cadence, its last run, and the request budget.

What the page changes is saved in collector/controls.py's file beside the database, so it
outlasts a restart, and a `collector run` from Task Scheduler or Docker obeys it too. Two
collectors never poll at once: a pass holds the database's run lock, and the other one waits.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import duckdb

from collector import controls, db, scheduler, sun
from collector.catalog import GROUPS, JOB_INFO, UNRECORDED
from collector.config import Config, load_config
from collector.http import Client
from collector.jobs import JOBS
from chatbot.memory import Unavailable
from chatbot.sources import SOURCES

if TYPE_CHECKING:
    from chatbot.memory import Memory

log = logging.getLogger(__name__)

SOURCE_BY_ID = {s.id: s for s in SOURCES}
RECENT = timedelta(minutes=15)  # a run this recent means some collector is at work
RETRY_S = 5.0  # how long a Run now waits when another collector holds the database


class UnknownJob(KeyError):
    pass


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _source(source_id: str) -> dict:
    s = SOURCE_BY_ID.get(source_id)
    if s is None:
        return {"id": source_id, "name": source_id}
    return {"id": s.id, "name": s.name, "short": s.short, "publisher": s.publisher,
            "gives": s.gives, "url": s.url, "licence": s.licence, "licence_url": s.licence_url}


class Collectors:
    def __init__(self, memory: Memory, *, collect_default: bool, record_default: bool = True,
                 tick_s: float = 30.0, base: Config | None = None, client: Client | None = None):
        self.memory = memory
        # Defaults plus collector.toml, on the chatbot's database; the page's changes go on top.
        self.base = base or replace(load_config(), db_path=memory.path, log_file=None)
        self.path = controls.controls_path(self.base)
        self.collect_default = collect_default
        self.record_default = record_default
        self.tick_s = tick_s
        self._client = client
        self._cv = threading.Condition()
        self._queue: list[str] = []  # jobs asked for with Run now, in order
        self._hold_until = 0.0  # monotonic time before which the queue waits
        self._pass_now = True
        self._passing = False
        self._last_pass_end: datetime | None = None
        self.current: str | None = None  # the job running in this process right now
        self.current_since: datetime | None = None
        self.last_pass: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- state -------------------------------------------------------------------------------

    def controls(self) -> controls.Controls:
        return controls.load(self.path)

    def collecting(self, page: controls.Controls | None = None) -> bool:
        page = page or self.controls()
        return self.collect_default if page.collecting is None else page.collecting

    def recording(self, page: controls.Controls | None = None) -> bool:
        if self.memory.recorder is None:
            return False
        page = page or self.controls()
        return self.record_default if page.recording is None else page.recording

    def config(self, page: controls.Controls | None = None) -> Config:
        return controls.apply(self.base, page or self.controls())

    # -- the thread --------------------------------------------------------------------------

    def start(self) -> None:
        page = self.controls()
        if self.memory.recorder is not None:
            self.memory.recorder.paused = not self.recording(page)
        self.memory.collecting = self.collecting(page)
        if self.memory.collecting:
            per_day = sum(b.requests_per_day for b in scheduler.estimate_budget(self.config(page))
                          if b.nokia)
            log.info("full collection on: about %d city API requests a day (budget %d)",
                     per_day, self.base.daily_request_budget)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="twin-collector", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        next_pass = time.monotonic()
        while not self._stop.is_set():
            with self._cv:
                while not self._stop.is_set() and not self._pass_now:
                    t = time.monotonic()
                    if t >= next_pass or (self._queue and t >= self._hold_until):
                        break
                    wake = min(next_pass, self._hold_until) if self._queue else next_pass
                    self._cv.wait(timeout=max(0.05, wake - t))
                due = self._pass_now or time.monotonic() >= next_pass
                self._pass_now = False
            if self._stop.is_set():
                break
            try:
                self.turn(due)
            except Exception:  # noqa: BLE001 - the thread must outlive any one pass
                log.exception("collector pass failed")
            if due:
                next_pass = time.monotonic() + self.tick_s

    def turn(self, due: bool) -> None:
        """One turn of the thread: the jobs asked for by name, then a scheduled pass if one is
        due and collection is on. Public so tests can drive it without the thread."""
        with self._cv:
            asked = self._queue if time.monotonic() >= self._hold_until else []
            self._queue = [j for j in self._queue if j not in asked]
        page = self.controls()
        self.memory.collecting = self.collecting(page)
        if self.memory.recorder is not None:
            self.memory.recorder.paused = not self.recording(page)
        if asked:
            self._pass(only=asked, force=True)
        if due and self.memory.collecting:
            self._pass()

    def _pass(self, *, only: list[str] | None = None, force: bool = False) -> None:
        started = db.utcnow()
        if scheduler.lock_held_elsewhere(self.base):
            self.last_pass = {"at": _iso(started), "outcome": "elsewhere"}
            if only:  # the other collector is mid-pass: try these again in a few seconds
                with self._cv:
                    self._queue = [j for j in only if j not in self._queue] + self._queue
                    self._hold_until = time.monotonic() + RETRY_S
            return
        self._passing = True
        try:
            summaries = scheduler.run_pass(self.base, only=only, force=force, client=self.client(),
                                           on_job=self._on_job)
            failed = [s.job for s in summaries if s.status == "error"]
            self.last_pass = {
                "at": _iso(started), "outcome": "ran" if summaries else "nothing due",
                "jobs": [s.job for s in summaries], "failed": failed}
        except Exception as e:  # noqa: BLE001
            log.exception("collector pass failed")
            self.last_pass = {"at": _iso(started), "outcome": "error",
                              "message": f"{type(e).__name__}: {e}"}
        finally:
            self._passing = False
            # Runs that started before this are this server's own (the run lock keeps any
            # other collector out while a pass is on); a newer one is another collector's.
            self._last_pass_end = db.utcnow()

    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self.base)
        return self._client

    def _on_job(self, name: str | None) -> None:
        with self._cv:
            self.current = name
            self.current_since = db.utcnow() if name else None

    # -- what the page changes ---------------------------------------------------------------

    def set_collecting(self, on: bool) -> None:
        controls.update(self.path, lambda c: setattr(c, "collecting", on))
        self.memory.collecting = on
        with self._cv:
            self._pass_now = on
            self._cv.notify_all()

    def set_recording(self, on: bool) -> None:
        if self.memory.recorder is None:
            raise ValueError("this server doesn't record what pages fetch (CHATBOT_RECORD=0)")
        controls.update(self.path, lambda c: setattr(c, "recording", on))
        self.memory.recorder.paused = not on

    def update_job(self, name: str, patch: dict) -> None:
        """Change a job's switch, cadence or night window. A value equal to the default is
        dropped, so the page shows only real changes."""
        self._known(name)
        patch = controls.clean_job_patch(patch)
        base = self.base.jobs[name]

        def change(c: controls.Controls) -> None:
            merged = {**c.jobs.get(name, {}), **patch}
            merged = {k: v for k, v in merged.items() if getattr(base, k) != v}
            if merged:
                c.jobs[name] = merged
            else:
                c.jobs.pop(name, None)

        controls.update(self.path, change)
        with self._cv:
            self._cv.notify_all()

    def reset_job(self, name: str) -> None:
        self._known(name)
        controls.update(self.path, lambda c: c.jobs.pop(name, None))

    def run_now(self, name: str) -> str:
        """Queue a job to run at once, whether or not it is due or switched on."""
        self._known(name)
        with self._cv:
            if name == self.current:
                return "running"
            if name not in self._queue:
                self._queue.append(name)
            self._cv.notify_all()
        return "queued"

    def _known(self, name: str) -> None:
        if name not in JOBS or name not in self.base.jobs:
            raise UnknownJob(name)

    # -- the report --------------------------------------------------------------------------

    def report(self, now: datetime | None = None) -> dict:
        now = now or db.utcnow()
        page = self.controls()
        cfg = self.config(page)
        collecting = self.collecting(page)
        with self._cv:
            current, current_since, queued = self.current, self.current_since, list(self._queue)

        store: dict = {"path": str(self.memory.path), "status": "ok"}
        last: dict = {}
        per_job_24h: dict[str, int] = {}
        counts: dict[str, int] = {}
        errors: list[dict] = []
        newest_run = None
        try:
            store["size_mb"] = round(self.memory.path.stat().st_size / 1e6, 1)
            with self.memory.reading(timeout=2.0) as con:
                day_ago = now - timedelta(hours=24)
                for row in con.execute(
                    """SELECT job, arg_max(status, started_at), max(started_at),
                              arg_max(finished_at, started_at), arg_max(requests, started_at),
                              arg_max(rows_written, started_at), arg_max(message, started_at),
                              max(started_at) FILTER (WHERE status IN ('ok', 'partial')),
                              count(*) FILTER (WHERE status = 'error' AND started_at > ?),
                              sum(requests) FILTER (WHERE started_at > ?)
                       FROM job_runs GROUP BY job""", [day_ago, day_ago]).fetchall():
                    name, status, started, finished, requests, rows, message, last_ok, err, req = row
                    last[name] = {
                        "status": status, "started": _iso(started),
                        "seconds": round((finished - started).total_seconds(), 1)
                        if finished and started else None,
                        "requests": requests, "rows": rows, "message": message or None,
                        "last_ok": _iso(last_ok), "errors_24h": err or 0,
                        "_last_ok": last_ok, "_started": started,
                    }
                    per_job_24h[name] = int(req or 0)
                    newest_run = max(filter(None, (newest_run, started)), default=None)
                counts = dict(con.execute(
                    "SELECT type, count(*) FROM devices WHERE in_inventory GROUP BY type"
                ).fetchall())
                errors = [
                    {"at": _iso(ts), "job": job, "kind": kind, "message": msg,
                     "title": JOB_INFO[job].title if job in JOB_INFO else job}
                    for ts, job, kind, msg in con.execute(
                        """SELECT ts, job, kind, message FROM api_errors
                           ORDER BY ts DESC LIMIT 8""").fetchall()
                ]
        except FileNotFoundError:
            store["status"] = "missing"
            store["message"] = "the twin's memory is empty: nothing has been recorded yet"
        except Unavailable as e:
            store["status"] = e.reason
            store["message"] = str(e)
        except duckdb.Error as e:
            store["status"] = "error"
            store["message"] = f"the twin's memory can't be read: {_first_line(e)}"

        budget_lines = {b.job: b for b in scheduler.estimate_budget(cfg, counts or None, now)}
        night = sun.is_night(now, cfg.latitude, cfg.longitude)
        jobs = []
        for name, jc in cfg.jobs.items():
            info = JOB_INFO.get(name)
            if info is None or name not in JOBS:
                continue
            base = self.base.jobs[name]
            b = budget_lines[name]
            mine = last.get(name)
            next_due = None
            if jc.enabled and mine and mine["_last_ok"]:
                next_due = _iso(max(now, mine["_last_ok"] + timedelta(minutes=jc.every_minutes)))
            elif jc.enabled:
                next_due = _iso(now)
            state = ("running" if name == current else "queued" if name in queued else None)
            jobs.append({
                "name": name, **info.public(), "source": _source(info.source),
                "enabled": jc.enabled, "every_minutes": jc.every_minutes,
                "night_only": jc.night_only,
                "defaults": {"enabled": base.enabled, "every_minutes": base.every_minutes,
                             "night_only": base.night_only},
                "changed": sorted(page.jobs.get(name, {})),
                "counts_against_budget": JOBS[name].nokia,
                "requests_per_run": round(b.requests_per_run, 2),
                "requests_per_day": round(b.requests_per_day),
                "requests_24h": per_job_24h.get(name, 0),
                "waiting_for_dark": jc.enabled and jc.night_only and not night,
                "state": state,
                "running_since": _iso(current_since) if state == "running" else None,
                "last": {k: v for k, v in mine.items() if not k.startswith("_")} if mine else None,
                "next_due": next_due,
            })
        order = {g: i for i, g in enumerate(GROUPS)}
        jobs.sort(key=lambda j: order.get(j["group"], len(order)))

        nokia_per_day = sum(b.requests_per_day for b in budget_lines.values() if b.nokia)
        other_per_day = sum(b.requests_per_day for b in budget_lines.values() if not b.nokia)
        nokia_24h = sum(v for k, v in per_job_24h.items() if JOBS.get(k) is None or JOBS[k].nokia)
        elsewhere_now = not self._passing and scheduler.lock_held_elsewhere(self.base)
        mine_until = self._last_pass_end
        other = newest_run if newest_run and (mine_until is None or newest_run > mine_until) else None
        recorder = {"on": False}
        if self.memory.recorder is not None:
            recorder = self.memory.recorder.status()
            recorder["last_flush"] = _iso(self.memory.recorder.last_flush)  # with its offset
        recorder["recording"] = self.recording(page)
        return {
            "now": _iso(now),
            "collecting": collecting,
            "decided_on_page": page.collecting is not None,
            "collect_default": self.collect_default,
            "elsewhere_now": elsewhere_now,
            # The newest run this server didn't make: Task Scheduler, Docker, another server.
            "other_collector_last_run": _iso(other),
            "other_collector_recent": bool(other and now - other < RECENT),
            "last_pass_here": self.last_pass or None,
            "running": current,
            "queued": queued,
            "night": night,
            "night_fraction": round(sun.night_fraction(now, cfg.latitude, cfg.longitude), 3),
            "store": store,
            "budget": {
                "per_day": round(nokia_per_day), "limit": cfg.daily_request_budget,
                "last_24h": nokia_24h, "other_per_day": round(other_per_day),
            },
            "recorder": recorder,
            "jobs": jobs,
            "groups": list(GROUPS),
            "unrecorded": [{"source": _source(sid), "why": why} for sid, why in UNRECORDED.items()],
            "recent_errors": errors,
            "cadences": list(controls.CADENCES),
            "changed_at": page.changed_at,
        }


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__

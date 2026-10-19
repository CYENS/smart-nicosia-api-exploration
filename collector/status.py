"""`collector status`: last runs, row counts, freshness, device health and recent errors."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from collector import db, interpret, sun
from collector.config import Config
from collector.jobs import JOBS
from collector.scheduler import estimate_budget, fmt_every

TABLES = [
    ("devices", None),
    ("telemetry", "ts"),
    ("state_changes", "ts"),
    ("events", "ts"),
    ("alarms", "start_ts"),
    ("traffic_counts", "bucket_start"),
    ("speeds", "bucket_start"),
    ("travel_times", "measured_at"),
    ("traffic_cameras", None),
    ("attributes", None),
]


def collect_status(cfg: Config, now: datetime | None = None) -> dict:
    now = now or db.utcnow()
    out: dict = {"db_path": str(cfg.db_path), "now": now, "exists": cfg.db_path.exists()}
    if not out["exists"]:
        return out
    out["size_mb"] = cfg.db_path.stat().st_size / 1e6
    with db.session(cfg.db_path, read_only=True, wait_s=30, timezone_name=cfg.timezone) as con:
        day_ago = now - timedelta(hours=24)
        last = {
            r[0]: r[1:]
            for r in con.execute(
                """SELECT job, arg_max(status, started_at), max(started_at),
                          arg_max(finished_at, started_at), arg_max(requests, started_at),
                          arg_max(rows_written, started_at), arg_max(message, started_at),
                          max(started_at) FILTER (WHERE status IN ('ok', 'partial')),
                          count(*) FILTER (WHERE status = 'error' AND started_at > ?)
                   FROM job_runs GROUP BY job""",
                [day_ago],
            ).fetchall()
        }
        jobs = []
        for name, jc in cfg.jobs.items():
            status, started, finished, requests, rows, message, last_ok, errors_24h = last.get(
                name, (None,) * 8)
            every = timedelta(minutes=jc.every_minutes)
            jobs.append({
                "job": name, "enabled": jc.enabled, "every_minutes": jc.every_minutes,
                "night_only": jc.night_only, "status": status, "started": started,
                "seconds": (finished - started).total_seconds() if finished and started else None,
                "requests": requests, "rows": rows, "message": message, "last_ok": last_ok,
                "next_due": (last_ok + every) if last_ok else None, "errors_24h": errors_24h or 0,
            })
        out["jobs"] = jobs

        per_job = dict(con.execute(
            "SELECT job, sum(requests) FROM job_runs WHERE started_at > ? GROUP BY job", [day_ago]
        ).fetchall())
        out["requests_24h"] = {
            "nokia": int(sum(v for k, v in per_job.items() if JOBS.get(k) is None or JOBS[k].nokia)),
            "other": int(sum(v for k, v in per_job.items() if k in JOBS and not JOBS[k].nokia)),
        }
        counts = dict(con.execute(
            "SELECT type, count(*) FROM devices WHERE in_inventory GROUP BY type").fetchall())
        out["estimated_per_day"] = sum(
            b.requests_per_day for b in estimate_budget(cfg, counts or None, now) if b.nokia)

        tables = []
        for table, col in TABLES:
            if col:
                n, lo, hi = con.execute(f"SELECT count(*), min({col}), max({col}) FROM {table}").fetchone()
            else:
                n, lo, hi = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0], None, None
            tables.append({"table": table, "rows": n, "oldest": lo, "newest": hi})
        out["tables"] = tables

        readings = dict(con.execute(
            """SELECT d.type, count(*) FROM telemetry t JOIN devices d ON d.name = t.device
               GROUP BY d.type"""
        ).fetchall())
        out["telemetry_by_type"] = [
            {"type": t, "devices": n, "readings": readings.get(t, 0), "newest": newest,
             "fresh_2h": fresh}
            for t, n, newest, fresh in con.execute(
                """SELECT type, count(*), max(last_telemetry),
                          count(*) FILTER (WHERE last_telemetry > ?)
                   FROM devices WHERE in_inventory GROUP BY type ORDER BY 2 DESC""",
                [now - timedelta(hours=2)],
            ).fetchall()
        ]

        night = sun.is_night(now, cfg.latitude, cfg.longitude)
        health: dict[str, dict[str, int]] = {}
        for name, dtype, last_activity, last_telemetry, timeout_ms in con.execute(
            """SELECT name, type, last_activity, last_telemetry, inactivity_timeout_ms
               FROM devices WHERE in_inventory"""
        ).fetchall():
            seen = max((t for t in (last_activity, last_telemetry) if t), default=None)
            verdict = interpret.online_status(
                dtype, seen, now, night=night,
                timeout=timedelta(milliseconds=timeout_ms) if timeout_ms else None,
            )
            counts_by_state = health.setdefault(dtype, {})
            counts_by_state[verdict.state] = counts_by_state.get(verdict.state, 0) + 1
        out["health"] = health
        out["night"] = night

        out["errors_24h"] = con.execute(
            "SELECT count(*) FROM api_errors WHERE ts > ?", [day_ago]).fetchone()[0]
        out["recent_errors"] = [
            {"ts": ts, "job": job, "endpoint": ep, "kind": kind, "message": msg}
            for ts, job, ep, kind, msg in con.execute(
                "SELECT ts, job, endpoint, kind, message FROM api_errors ORDER BY ts DESC LIMIT 8"
            ).fetchall()
        ]
    return out


def render_status(s: dict, cfg: Config) -> str:
    tz = ZoneInfo(cfg.timezone)
    now = s["now"]

    def t(dt):
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else "-"

    def ago(dt):
        if not dt:
            return "never"
        secs = (now - dt).total_seconds()
        if secs < 0:
            return f"in {_dur(-secs)}"
        return f"{_dur(secs)} ago"

    if not s["exists"]:
        return f"No database at {s['db_path']} yet. Run `collector run --once` first."
    lines = [
        f"Database {s['db_path']} ({s['size_mb']:.1f} MB), {t(now)} {cfg.timezone}"
        f"{' (night)' if s['night'] else ''}",
        "",
        f"{'job':<15}{'every':>7}  {'last run':<16}{'status':<9}{'req':>6}{'rows':>8}"
        f"{'secs':>7}  {'next due':<12} note",
    ]
    for j in s["jobs"]:
        every = fmt_every(j["every_minutes"]) + ("*" if j["night_only"] else "")
        if not j["enabled"]:
            next_due = "disabled"
        elif j["next_due"] is None:
            next_due = "now"
        else:
            next_due = "now" if j["next_due"] <= now else ago(j["next_due"])
        note = (j["message"] or "")[:70]
        if j["errors_24h"]:
            note = f"[{j['errors_24h']} errors/24h] " + note
        secs = f"{j['seconds']:.1f}" if j["seconds"] is not None else "-"
        lines.append(
            f"{j['job']:<15}{every:>7}  {ago(j['started']):<16}{j['status'] or '-':<9}"
            f"{_n(j['requests']):>6}{_n(j['rows']):>8}{secs:>7}  {next_due:<12} {note}"
        )
    lines.append("* night only (sunset to sunrise)")
    r = s["requests_24h"]
    lines += [
        "",
        f"Requests in the last 24 h: {r['nokia']:,} Smart Nicosia (estimate at current settings "
        f"~{s['estimated_per_day']:,.0f}/day, budget {cfg.daily_request_budget:,}), "
        f"{r['other']:,} national traffic feed",
        "",
        f"{'table':<17}{'rows':>12}  {'oldest':<17}{'newest':<17}",
    ]
    for row in s["tables"]:
        lines.append(f"{row['table']:<17}{row['rows']:>12,}  {t(row['oldest']):<17}"
                     f"{t(row['newest']):<17}")
    lines += ["", f"{'device type':<13}{'devices':>8}{'readings':>11}  {'newest reading':<17}"
                  f"{'reported<2h':>12}  health"]
    for row in s["telemetry_by_type"]:
        health = ", ".join(f"{n} {state}" for state, n in sorted(
            s["health"].get(row["type"], {}).items(), key=lambda x: -x[1]))
        lines.append(f"{row['type']:<13}{row['devices']:>8}{row['readings']:>11,}  "
                     f"{t(row['newest']):<17}{row['fresh_2h']:>12}  {health}")
    lines += ["", f"Errors in the last 24 h: {s['errors_24h']}"]
    for e in s["recent_errors"]:
        lines.append(f"  {t(e['ts'])} {e['job']:<14} {e['kind']:<10} {e['endpoint']}: "
                     f"{(e['message'] or '')[:90]}")
    return "\n".join(lines)


def _n(x) -> str:
    return "-" if x is None else f"{x:,}"


def _dur(secs: float) -> str:
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs / 60:.0f}m"
    if secs < 2 * 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


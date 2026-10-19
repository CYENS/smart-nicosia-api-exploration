"""Command line: collector run | status | backfill | budget."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta, timezone

from collector import db
from collector.config import Config, load_config


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    if cfg.log_file:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def parse_when(text: str) -> datetime:
    """'2026-09-01', '2026-09-01T12:00', or a relative '30d' / '12h'."""
    text = text.strip()
    if text[-1:] in ("d", "h") and text[:-1].isdigit():
        n = int(text[:-1])
        return db.utcnow() - (timedelta(days=n) if text[-1] == "d" else timedelta(hours=n))
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _csv(text: str | None) -> list[str] | None:
    return [x.strip() for x in text.split(",") if x.strip()] if text else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="collector", description=__doc__)
    p.add_argument("--config", help="TOML settings file (default: ./collector.toml if present)")
    p.add_argument("--db", help="DuckDB file (default: data/smart_nicosia.duckdb)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="poll the API (forever, or one pass with --once)")
    run.add_argument("--once", action="store_true",
                     help="run the jobs that are due, then exit (for Task Scheduler / cron)")
    run.add_argument("--jobs", help="comma-separated job names to consider (default: all)")
    run.add_argument("--force", action="store_true",
                     help="run the selected jobs now, ignoring cadence and night windows")

    st = sub.add_parser("status", help="show last runs, row counts, freshness and errors")
    st.add_argument("--json", action="store_true")

    sub.add_parser("budget", help="estimate requests per day at the configured cadences")

    bf = sub.add_parser("backfill", help="gently fetch the history the API still holds")
    bf.add_argument("--since", default="30d",
                    help="how far back: a date (2026-09-01) or relative (30d); default 30d")
    bf.add_argument("--until", help="newest point to page back from (default: now)")
    bf.add_argument("--what", default=",".join(("events", "speed", "traffic", "telemetry")),
                    help="any of events,speed,traffic,telemetry")
    bf.add_argument("--types", help="device types for telemetry (default: all but ParkingLot)")
    bf.add_argument("--devices", help="only these device names (case-sensitive)")
    bf.add_argument("--max-requests", type=int, default=5000,
                    help="stop after this many requests (default 5000; rerun to continue)")
    bf.add_argument("--concurrency", type=int, default=1, choices=(1, 2),
                    help="requests in flight (max 2)")
    bf.add_argument("--delay", type=float, default=0.5,
                    help="minimum seconds between request starts (default 0.5)")
    bf.add_argument("--reset", action="store_true",
                    help="ignore saved progress and re-page from --until")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config, db_path=args.db)

    if args.command == "budget":
        from collector.scheduler import estimate_budget, render_budget

        counts = None
        if cfg.db_path.exists():
            with db.session(cfg.db_path, read_only=True, wait_s=30,
                            timezone_name=cfg.timezone) as con:
                counts = dict(con.execute(
                    "SELECT type, count(*) FROM devices WHERE in_inventory GROUP BY type"
                ).fetchall()) or None
        print(render_budget(cfg, estimate_budget(cfg, counts)))
        return 0

    if args.command == "status":
        from collector.status import collect_status, render_status

        s = collect_status(cfg)
        if args.json:
            print(json.dumps(s, default=str, indent=2))
        else:
            print(render_status(s, cfg))
        return 0

    setup_logging(cfg, args.verbose)

    if args.command == "run":
        from collector.scheduler import run_forever, run_pass

        if args.once:
            summaries = run_pass(cfg, only=_csv(args.jobs), force=args.force)
            return 1 if any(s.status == "error" for s in summaries) else 0
        if args.jobs or args.force:
            print("--jobs/--force only apply with --once", file=sys.stderr)
            return 2
        try:
            run_forever(cfg)
        except KeyboardInterrupt:
            return 0
        return 0

    if args.command == "backfill":
        from collector.backfill import DEFAULT_TYPES, BackfillOptions, run_backfill
        from collector.scheduler import FileLock, lock_path

        opts = BackfillOptions(
            since=parse_when(args.since),
            until=parse_when(args.until) if args.until else db.utcnow(),
            what=tuple(_csv(args.what) or ()),
            types=tuple(_csv(args.types) or DEFAULT_TYPES),
            devices=tuple(_csv(args.devices)) if args.devices else None,
            max_requests=args.max_requests,
            concurrency=args.concurrency,
            delay_s=args.delay,
            reset=args.reset,
        )
        bad = set(opts.what) - {"events", "speed", "traffic", "telemetry"}
        if bad:
            print(f"unknown --what: {', '.join(sorted(bad))}", file=sys.stderr)
            return 2
        with FileLock(lock_path(cfg, "backfill")):
            report = run_backfill(cfg, opts, log_fn=logging.getLogger("backfill").info)
        print(f"backfill: {report.requests} requests, rows {report.rows}, devices done "
              f"{report.devices_done}, pending {report.devices_pending}"
              + ("; stopped at --max-requests (rerun to continue)" if report.stopped_by_budget
                 else ""))
        for e in report.errors[:20]:
            print(f"  error: {e}")
        return 1 if report.errors else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

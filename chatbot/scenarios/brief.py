"""A run's exports: the CSV of its series, and the decision brief (a clean page to print or save
as PDF: the question, the options compared, the results with their ranges, the method, the
assumptions, the sources and the backtest)."""

from __future__ import annotations

import csv
import html
import io


def series_csv(run_: dict) -> str:
    """Every series row of a run, one per line: metric, time, variant, value."""
    rows = run_["result"].get("series") or []
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["run", "scenario", "metric", "time", "variant", "value"])
    for r in rows:
        w.writerow([run_["id"], run_["scenario"], r.get("metric"), r.get("time"), r.get("variant"),
                    r.get("value")])
    return buf.getvalue()


def brief_html(run_: dict, other: dict | None = None) -> str:
    e = html.escape
    return f"<!doctype html><title>{e(run_.get('label') or run_['scenario'])}</title>"

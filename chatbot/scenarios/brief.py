"""A run's exports: the CSV of its series, and the decision brief, a clean page to print or save
as PDF: the question, the options compared, the results with their ranges, the sensitivity to
each assumption, the backtest, the safety check, the method, the assumptions and the sources.
It is plain HTML with inline SVG: no script, nothing fetched."""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from chatbot import sources as data_sources

TZ = ZoneInfo("Asia/Nicosia")
e = html.escape


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


def num(v, digits: int = 0, sign: bool = False) -> str:
    if v is None:
        return "n/a"
    s = f"{abs(v):,.{max(0, digits)}f}"
    if v < 0:
        return f"-{s}" if sign else s
    return f"+{s}" if sign and v > 0 else s


def change_words(k: dict) -> str:
    """'saves 2,280 EUR (2,145 to 3,790)' or 'adds 120 lamp-hours (no range)'."""
    d, lo, hi, dg = k.get("delta"), k.get("delta_low"), k.get("delta_high"), k.get("digits", 0)
    if d is None:
        return f"{num(k.get('scenario'), dg)} {k['unit']}" + (
            f" ({num(k['low'], dg)} to {num(k['high'], dg)})" if k.get("low") != k.get("high") else "")
    if d == 0 and lo == hi == 0:
        return "no change"
    lower = d < 0
    verb = "saves" if (lower and k["better"] == "lower") else "cuts" if lower else "adds"
    text = f"{verb} {num(d, dg)} {k['unit']}"
    if lo is not None and hi is not None and lo != hi:
        a, b = sorted((abs(lo), abs(hi)))
        text += f" ({num(a, dg)} to {num(b, dg)})"
    return text


def _kpi_table(runs: list[dict]) -> str:
    head = "<th>Measure</th><th>Today</th>" + "".join(
        f"<th>{e(r['label'])}</th><th>Change, with its range</th>" for r in runs)
    rows = []
    first = runs[0]["result"]["kpis"]
    for k in first:
        cells = [f"<th scope='row'>{e(k['label'])}<span class='unit'>{e(k['unit'])}</span></th>",
                 f"<td class='n'>{num(k['baseline'], k['digits'])}</td>"]
        for r in runs:
            kk = next((x for x in r["result"]["kpis"] if x["id"] == k["id"]), None)
            if kk is None:
                cells += ["<td>n/a</td>", "<td>n/a</td>"]
                continue
            rng = f"<span class='rng'>{num(kk['low'], kk['digits'])} to {num(kk['high'], kk['digits'])}</span>" \
                if kk["low"] != kk["high"] else ""
            cells += [f"<td class='n'>{num(kk['scenario'], kk['digits'])}{rng}</td>",
                      f"<td>{e(change_words(kk))}</td>"]
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table class='kpis'><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _tornado(run_: dict) -> str:
    """How far each assumption, alone across its plausible range, moves the headline change."""
    sens = [s for s in run_["result"].get("sensitivity") or [] if s["at_low"] is not None]
    if not sens:
        return ""
    base = sens[0]["base"] or 0
    lo = min(min(s["at_low"], s["at_high"], base) for s in sens)
    hi = max(max(s["at_low"], s["at_high"], base) for s in sens)
    span = (hi - lo) or 1
    w, label_w, row_h, value_w = 760, 200, 30, 230
    plot = w - label_w - value_w - 10

    def x(v):
        return label_w + (v - lo) / span * plot

    bars = []
    for i, s in enumerate(sens):
        y = 10 + i * row_h
        a, b = sorted((s["at_low"], s["at_high"]))
        bars.append(
            f"<text x='{label_w - 8}' y='{y + 15}' text-anchor='end' class='lbl'>{e(s['label'])}</text>"
            f"<rect x='{x(a):.1f}' y='{y + 4}' width='{max(1.5, x(b) - x(a)):.1f}' height='16' rx='2' class='bar'/>"
            f"<text x='{w - value_w + 6}' y='{y + 16}' class='val'>{e(_effect(s))}</text>")
    height = 20 + len(sens) * row_h
    return (f"<svg viewBox='0 0 {w} {height + 16}' role='img' aria-label='How each assumption moves the "
            f"change in {e(sens[0]['kpi'])}' class='chart'>"
            f"<line x1='{x(base):.1f}' x2='{x(base):.1f}' y1='4' y2='{height}' class='base'/>"
            f"<text x='{x(base):.1f}' y='{height + 13}' text-anchor='middle' class='val'>as set: {e(_amount(base, sens[0]['unit']))}"
            f"</text>{''.join(bars)}</svg>")


def _amount(v: float | None, unit: str) -> str:
    """A change in words: 'saves 1,719 EUR' for a fall, 'adds 30 EUR' for a rise."""
    if v is None:
        return "n/a"
    if v == 0:
        return "no change"
    return f"{'saves' if v < 0 else 'adds'} {num(v)} {unit}"


def _effect(s: dict) -> str:
    """An assumption's sweep: 'saves 1,719 to 2,751 EUR (0.2 to 0.32)'."""
    a, b = s["at_low"], s["at_high"]
    if a == b:
        return "no effect"
    if a < 0 and b < 0:
        text = f"saves {num(a)} to {num(b)} {s['unit']}"
    elif a >= 0 and b >= 0:
        text = f"adds {num(a)} to {num(b)} {s['unit']}"
    else:
        text = f"{_amount(a, s['unit'])} to {_amount(b, s['unit'])}"
    return f"{text} ({_plain(s['value_low'])} to {_plain(s['value_high'])})"


def _plain(v) -> str:
    """A setting as written: 10, not 10.00; 0.55, not 0.6."""
    if v is None:
        return "n/a"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}".rstrip("0").rstrip(".")


def _differences(runs: list[dict]) -> str:
    """When two runs are compared, the parameters they don't share."""
    if len(runs) < 2:
        return ""
    a, b = runs[0], runs[1]
    labels = {x["id"]: (x["label"], x.get("unit", "")) for x in a["result"].get("assumptions") or []}
    rows = []
    for k in sorted(set(a["params"]) | set(b["params"])):
        if a["params"].get(k) != b["params"].get(k) and k in labels:
            name, unit = labels[k]
            rows.append(f"{name}: {_plain(a['params'].get(k))} against {_plain(b['params'].get(k))} {unit}".strip())
    if not rows:
        return ""
    return ("<p class='small'>The two runs also differ in their assumptions, so part of the difference "
            f"comes from them: {e('; '.join(rows))}.</p>")


def _night_chart(run_: dict) -> str:
    rows = [r for r in run_["result"].get("series") or [] if r["metric"] == "night_kw"]
    if not rows:
        return ""
    times = sorted({r["time"] for r in rows})
    peak = max((r["value"] or 0) for r in rows) or 1
    w, h, pad = 640, 170, 34
    def pt(i, v):
        return pad + i / max(1, len(times) - 1) * (w - 2 * pad), h - 22 - (v or 0) / peak * (h - 40)
    paths = []
    for variant, cls in (("baseline", "today"), ("scenario", "scen")):
        by = {r["time"]: r["value"] for r in rows if r["variant"] == variant}
        d = " ".join(f"{'M' if i == 0 else 'L'}{pt(i, by.get(t))[0]:.1f},{pt(i, by.get(t))[1]:.1f}"
                     for i, t in enumerate(times))
        paths.append(f"<path d='{d}' class='{cls}'/>")
    ticks = "".join(f"<text x='{pt(i, 0)[0]:.1f}' y='{h - 6}' text-anchor='middle' class='val'>{t[11:16]}</text>"
                    for i, t in enumerate(times) if t[14:16] == "00" and int(t[11:13]) % 3 == 0)
    return (f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='All lamps power through one night, today "
            f"and in the scenario' class='chart'>{''.join(paths)}{ticks}"
            f"<text x='{pad}' y='12' class='val'>{num(peak, 1)} kW</text>"
            f"<text x='{w - pad}' y='12' text-anchor='end' class='lbl'><tspan class='k-today'>Today</tspan>"
            f"  <tspan class='k-scen'>Scenario</tspan></text></svg>")


def _assumptions(run_: dict) -> str:
    rows = "".join(
        f"<tr><th scope='row'>{e(a['label'])}</th><td class='n'>{_plain(a['value'])} {e(a['unit'])}</td>"
        f"<td class='n'>{_plain(a['low'])} to {_plain(a['high'])}</td><td>{e(a['source'])}</td></tr>"
        for a in run_["result"]["assumptions"])
    return ("<table><thead><tr><th>Assumption</th><th>As set</th><th>Plausible range</th><th>Where it comes "
            f"from</th></tr></thead><tbody>{rows}</tbody></table>")


def _backtest(run_: dict) -> str:
    bt = run_["result"].get("backtest")
    if not bt:
        return "<p>This scenario has no backtest.</p>"
    out = (f"<p><b>{e(bt['metric'])}, {e(_lower_first(bt['period']))}:</b> the sensors measured "
           f"{num(bt['measured'], 1)} {e(bt['unit'])}; the model of today's policy gives {num(bt['modelled'], 1)} "
           f"{e(bt['unit'])}, an error of {num(bt['error_pct'], 1, sign=True)}%.")
    hold = bt.get("holdout")
    if hold and hold.get("error_pct") is not None:
        out += (f" Learnt only until {e(hold['learnt_until'])} and replayed on the rest, the error is "
                f"{num(hold['error_pct'], 1, sign=True)}%.")
    if bt.get("median_lamp_error_pct") is not None:
        out += f" Lamp by lamp, half are within {num(bt['median_lamp_error_pct'], 1)}%."
    out += f"</p><p class='small'>{e(bt['note'])}</p>"
    for o in bt.get("others") or []:  # further comparisons a scenario makes
        err = "" if o.get("error_pct") is None else f", an error of {num(o['error_pct'], 1, sign=True)}%"
        out += (f"<p><b>{e(o['metric'])}, {e(_lower_first(o['period']))}:</b> measured "
                f"{num(o['measured'], 1)}, modelled {num(o['modelled'], 1)} {e(o.get('unit', ''))}{err}."
                f"</p><p class='small'>{e(o.get('note', ''))}</p>")
    return out


def _lower_first(text: str) -> str:
    """'The last 30 days' -> 'the last 30 days'; dates keep their capitals."""
    return text[:1].lower() + text[1:] if text.startswith("The ") else text


def _safety(run_: dict) -> str:
    det = run_["result"].get("details") or {}
    rows = det.get("safety") or []
    summary = det.get("summary") or {}
    if not rows:
        text = "<p>No dimmed or dark lamp is on a street with night pedestrians, near a bus stop or near a dangerous building.</p>"
    else:
        by_street: dict[str, list[dict]] = {}
        for r in rows:
            by_street.setdefault(r["street"], []).append(r)
        items = "".join(
            f"<li><b>{e(s)}</b>: {len(v)} {'lamp' if len(v) == 1 else 'lamps'}, "
            f"{e(', '.join(sorted({x for r in v for x in r['reasons']})).lower())}"
            + (f", {num(max(r['people_an_hour'] or 0 for r in v), 1)} people an hour" if any(r["people_an_hour"] for r in v) else "")
            + "</li>" for s, v in sorted(by_street.items(), key=lambda kv: -len(kv[1])))
        text = f"<p>{len(rows)} dimmed or dark lamps are flagged:</p><ul>{items}</ul>"
    if summary.get("dimmed_or_dark_without_camera"):
        text += (f"<p class='small'>{summary['dimmed_or_dark_without_camera']} dimmed or dark lamps are on streets "
                 "no camera counts, so nobody knows who walks there at night.</p>")
    return text


def _sources(run_: dict) -> str:
    items = []
    for sid in run_["result"].get("sources") or []:
        s = data_sources.BY_ID.get(sid)
        if s is None:
            continue
        link = f" <a href='{e(s.url)}'>{e(s.url)}</a>" if s.url else ""
        items.append(f"<li><b>{e(s.name)}</b>, {e(s.publisher)}{f', {e(s.licence)}' if s.licence else ''}: "
                     f"{e(s.gives)}.{link}</li>")
    return f"<ul class='small'>{''.join(items)}</ul>"


STYLE = """
:root { color-scheme: light; }
body { font: 14px/1.5 "Sofia Sans", system-ui, -apple-system, "Segoe UI", sans-serif; color: #15202e;
       background: #fff; max-width: 860px; margin: 32px auto; padding: 0 20px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; border-bottom: 1px solid #d6dde6; padding-bottom: 4px; }
.meta, .small { color: #536173; font-size: 12.5px; }
.lead { font-size: 15.5px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e3e8ee; vertical-align: top; }
thead th { font-size: 12px; color: #536173; font-weight: 600; }
td.n { text-align: right; white-space: nowrap; }
.unit { display: block; font-weight: 400; color: #536173; font-size: 12px; }
.rng { display: block; color: #536173; font-size: 12px; }
.chart { width: 100%; height: auto; margin: 6px 0 2px; }
.chart .lbl { font-size: 12px; fill: #15202e; } .chart .val { font-size: 11px; fill: #536173; }
.chart .bar { fill: #c77b16; } .chart .base { stroke: #15202e; stroke-dasharray: 3 3; }
.chart .today { fill: none; stroke: #8a96a6; stroke-width: 2; } .chart .scen { fill: none; stroke: #c77b16; stroke-width: 2.5; }
.k-today { fill: #8a96a6; font-weight: 600; } .k-scen { fill: #c77b16; font-weight: 600; }
.tools { margin: 16px 0; } ul.small a { word-break: break-all; } .tools a, .tools button { font: inherit; margin-right: 12px; }
@media print { .tools { display: none; } body { margin: 0; max-width: none; } h2 { break-after: avoid; } table, svg { break-inside: avoid; } }
"""


def brief_html(run_: dict, other: dict | None = None) -> str:
    runs = [run_] + ([other] if other else [])
    res = run_["result"]
    question = (res.get("details") or {}).get("question") or ""
    method, main = "", ""
    today = "as measured"
    try:
        from chatbot.scenarios import registry
        scen = registry.get(run_["scenario"])
        question = question or scen.question
        method, main = scen.method, scen.main_kpi
        today = getattr(scen, "today", "") or today
    except Exception:  # noqa: BLE001 - a run of a scenario that no longer exists still prints
        pass
    title = run_.get("scenario_title") or run_["scenario"]
    created = datetime.fromisoformat(run_["created"]).astimezone(TZ).strftime("%d %b %Y, %H:%M")
    headline = next((k for k in res["kpis"] if k["id"] == main), None) or next(
        (k for k in res["kpis"] if k.get("delta") not in (None, 0)), res["kpis"][0] if res["kpis"] else None)
    lead = ""
    if headline:
        lead = (f"<p class='lead'><b>{e(run_['label'])}</b>: {e(headline['label'].lower())}, "
                f"{e(change_words(headline))}, against today's measured policy. Ranges span every combination "
                "of the assumptions' plausible values.</p>")
    options = "".join(f"<li><b>{e(r['label'])}</b> (run {e(r['id'])}, learnt from {e(r['period']['label'].lower())})</li>"
                      for r in runs)
    notes = "".join(f"<li>{e(n)}</li>" for n in res.get("notes") or [])
    parts = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{e(title)}: decision brief</title><style>{STYLE}</style></head><body>",
        f"<h1>{e(title)}</h1><p class='meta'>Decision brief from the Nicosia digital twin, {e(created)}</p>",
        "<div class='tools'><button type='button' onclick='window.print()'>Print or save as PDF</button>"
        f"<a href='series.csv' download>Download the data (CSV)</a></div>",
        f"<h2>The question</h2><p>{e(question)}</p>",
        f"<h2>Options compared</h2><ul><li><b>Today</b>, {e(today)}</li>{options}</ul>",
        f"<h2>Results</h2>{lead}{_kpi_table(runs)}{_differences(runs)}",
    ]
    for r in runs:
        t = _tornado(r)
        if t:
            parts.append(f"<h2>What moves the result: {e(r['label'])}</h2><p class='small'>Each assumption alone, "
                         "from the low to the high end of its plausible range, with the others as set.</p>" + t)
    night = _night_chart(run_)
    if night:
        parts.append(f"<h2>A night, today and in the scenario</h2>{night}")
    if "safety" in (res.get("details") or {}):
        parts.append(f"<h2>Safety check</h2>{_safety(run_)}")
    parts += [f"<h2>Backtest</h2>{_backtest(run_)}",
              f"<h2>Method</h2><p>{e(method)}</p><ul class='small'>{notes}</ul>",
              f"<h2>Assumptions</h2>{_assumptions(run_)}",
              f"<h2>Sources</h2>{_sources(run_)}",
              "</body></html>"]
    return "".join(parts)

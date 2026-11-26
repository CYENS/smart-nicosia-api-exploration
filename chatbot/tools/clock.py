"""The city clock: one time control for the whole twin. `set_city_time` moves it (a moment, a
range to play, a comparison, or back to live) and reads the twin's memory at that moment, so the
answer's numbers come from a tool like every other."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta

from collector.interpret import caqi_band

from chatbot.city import KINDS
from chatbot.moments import (CLOCK_KINDS, MAX_DAYS, NOTHING, OutOfRange, Moments, frame_of,
                             label, local_iso, parse_at)
from chatbot.store import STATE_COLORS, legend_for, point
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj

TOOLS = ToolSet()
tool = TOOLS.tool

SPEEDS = [1, 2, 4, 8]
STEPS = [15, 30, 60, 120, 180, 360]
COMPARE = {"last_week": timedelta(days=7), "yesterday": timedelta(days=1)}
# The strip's readouts, as rows a chart can compare: (key, label, unit, read from the strip).
READOUTS = [
    ("lamps_lit", "Lamps lit", "lamps", lambda s: s.get("lights", {}).get("on")),
    ("free_parking", "Free parking bays", "bays", lambda s: s.get("parking", {}).get("free")),
    ("full_bins", "Full bins", "bins", lambda s: s.get("bins", {}).get("full")),
    ("air_caqi", "Air quality (median CAQI)", "CAQI", lambda s: s.get("air", {}).get("caqi")),
]


def moments(ctx: ToolContext) -> Moments:
    """The twin's past moments, shared with the page's endpoints (made here for a context
    built without them, as in tests)."""
    if ctx.moments is None:
        ctx.moments = Moments(ctx.city, ctx.memory)
    return ctx.moments


def _when(ctx: ToolContext, text: str, what: str) -> datetime | None:
    try:
        return parse_at(text, ctx.city.now())
    except OutOfRange as e:
        raise ToolError(f"{what}: {e}") from None


def live_strip(ctx: ToolContext) -> dict:
    """The strip's readouts now, by the rules the moments use (a bay or bin that stopped
    reporting is left out), so now and another moment compare."""
    out: dict = {}
    states = {t: ctx.city.states(t) for t in ("Lighting", "Parking", "Wastebin", "smartsense")}
    lamps = Counter(s.interp.state for s in states["Lighting"])
    out["lights"] = {"on": lamps.get("on", 0), "total": sum(lamps.values()) - lamps.get("unknown", 0),
                     "coverage_pct": 100.0}
    bays = Counter(s.interp.state for s in states["Parking"] if not s.interp.stale)
    out["parking"] = {"free": bays.get("free", 0), "total": bays.get("free", 0) + bays.get("occupied", 0),
                      "coverage_pct": 100.0}
    bins = Counter(s.interp.state for s in states["Wastebin"] if not s.interp.stale)
    out["bins"] = {"full": bins.get("full", 0), "total": sum(bins.values()), "coverage_pct": 100.0}
    caqis = [s.interp.detail.get("caqi") for s in states["smartsense"] if not s.interp.stale]
    caqis = [c for c in caqis if isinstance(c, (int, float))]
    if caqis:
        median = statistics.median(caqis)
        out["air"] = {"caqi": round(median), "band": caqi_band(median), "coverage_pct": 100.0}
    else:
        out["air"] = {"unknown": True}
    return out


def readouts(m: Moments, at: datetime | None, ctx: ToolContext | None = None) -> dict:
    """The strip at a moment (now when `at` is None), compactly: each readout's value, or None
    where the memory holds nothing, with the coverage behind it."""
    strip = m.strip_at(at) if at is not None else live_strip(ctx)
    out = {}
    for key, lab, unit, read in READOUTS:
        part = strip.get({"lamps_lit": "lights", "free_parking": "parking", "full_bins": "bins",
                          "air_caqi": "air"}[key], {})
        out[key] = {"value": read(strip), "coverage_pct": part.get("coverage_pct")}
        if key == "lamps_lit" and not part.get("unknown"):
            out[key]["of"] = part.get("total")
        if key == "free_parking" and not part.get("unknown"):
            out[key]["of_known"] = part.get("total")
        if key == "full_bins" and not part.get("unknown"):
            out[key]["of_known"] = part.get("total")
        if key == "air_caqi" and not part.get("unknown"):
            out[key]["band"] = part.get("band")
    return out


@tool("set_city_time",
      "Set the city clock: the one time control the whole twin follows (the night lamps, the "
      "layers on the map, the strip of readouts, the sky and base map, the inspector and the "
      "time line of every chart). `at` shows the city at a past moment ('2026-11-21T21:00', "
      "local time); `from` and `to` put a range on the clock to play (with play and speed); "
      "`compare` swipes the map between the clock's moment and another ('last_week', "
      "'yesterday', or a local time), with the strip showing both; `live` true goes back to "
      "now. It reads the twin's memory at the moment (and at the compared one) and returns the "
      "strip's readouts there: lamps lit, free parking bays, full bins and the median CAQI, "
      "each with how much of it the memory covers (null: no record then, never a guess), and "
      "the events logged in the hour before. Returns a data_ref with one row per readout "
      "(value at the moment, and then at the compared moment). With `kind`, also a layer_ref of "
      "those devices as they were (with then_state and changed when comparing) and a data_ref "
      "of them, linked by id. The clock reaches back 62 days; moments the memory holds "
      "nothing for show as unknown.",
      obj({
          "at": {"type": "string",
                 "description": "The moment to show, local time ('2026-11-21T21:00'). Inside a "
                                "range, where the playhead starts."},
          "from": {"type": "string", "description": "Start of a range to play, local time."},
          "to": {"type": "string", "description": "End of the range, local time (default now)."},
          "step_minutes": {"type": "integer", "enum": STEPS,
                           "description": "Minutes between the range's moments (default: about "
                                          "a hundred moments over the range)."},
          "play": {"type": "boolean", "description": "Play the range (default true with a range)."},
          "speed": {"type": "integer", "enum": SPEEDS, "description": "Playback speed (default 1)."},
          "compare": {"type": "string",
                      "description": "Compare with 'last_week' or 'yesterday' (relative to the "
                                     "clock's moment), a local time, or 'off'."},
          "live": {"type": "boolean", "description": "Go back to live (now)."},
          "kind": {"type": "string", "enum": list(CLOCK_KINDS),
                   "description": "Also return these devices at the moment as a layer_ref."},
      }))
def h_set_city_time(ctx: ToolContext, a: dict) -> dict:
    now = ctx.city.now()
    m = moments(ctx)
    if a.get("live"):
        if a.get("at") or a.get("from"):
            raise ToolError("give either live true or a moment (at, from), not both")
        ctx.emit({"type": "ui", "action": "clock", "live": True, "compare": "off", "range": None})
        return {"ok": True, "clock": "live", "_summary": "clock: live"}

    start = _when(ctx, a["from"], "from") if a.get("from") else None
    end = _when(ctx, a["to"], "to") if a.get("to") else None
    if a.get("to") and not a.get("from"):
        raise ToolError("a range needs from (and optionally to)")
    rng = None
    if a.get("from"):
        if start is None:
            raise ToolError("from is now: a range starts in the past")
        stop = end or now
        if stop <= start:
            raise ToolError("the range is empty: from must be before to")
        hours = (stop - start).total_seconds() / 3600
        step = a.get("step_minutes") or next((s for s in STEPS if hours * 60 / s <= 120), STEPS[-1])
        if hours * 60 / step > 400:
            raise ToolError(f"{hours:.0f} h every {step} min is too many moments; use a larger "
                            "step_minutes")
        rng = {"from": local_iso(start), "to": local_iso(stop) if end else None,
               "step_min": step, "label": f"{label(start)} to {label(stop) if end else 'now'}"}
    at = _when(ctx, a["at"], "at") if a.get("at") else start
    if rng and at is not None and not (start <= at <= (end or now)):
        raise ToolError("at must fall inside the range")
    if at is None and not a.get("compare"):
        raise ToolError("give a past moment (at), a range (from, to), compare, or live true")
    moment = at or now  # comparing now with another moment

    then = None
    compare = a.get("compare")
    if compare and compare != "off":
        if compare in COMPARE:
            then = moment - COMPARE[compare]
        else:
            then = _when(ctx, compare, "compare")
            if then is None:
                raise ToolError("compare needs another moment than now")
        if then < now - timedelta(days=MAX_DAYS):
            raise ToolError(f"the compared moment is more than {MAX_DAYS} days back")

    ctx.emit({"type": "ui", "action": "clock", "live": at is None,
              "at": local_iso(at) if at else None, "range": rng,
              "play": bool(a.get("play", rng is not None)) if rng else False,
              "speed": a.get("speed") or 1,
              "compare": local_iso(then) if then else ("off" if compare == "off" else None),
              "compare_offset_min": -int(COMPARE[compare].total_seconds() // 60)
              if compare in COMPARE else None})

    here = readouts(m, at, ctx)
    there = readouts(m, then) if then else None
    res: dict = {"ok": True, "clock": label(frame_of(at)) if at else "live (now)"}
    if rng:
        res["range"] = rng["label"]
        res["playing"] = bool(a.get("play", True))
    res["readouts"] = here
    res["events_in_the_hour_before"] = m.events_near(at or now)
    if then is not None:
        res["compare"] = {"at": label(frame_of(then)), "readouts": there,
                          "events_in_the_hour_before": m.events_near(then)}
    rows = []
    for key, lab, unit, _ in READOUTS:
        row = {"readout": lab, "key": key, "unit": unit,
               "at": label(frame_of(at)) if at else "now",
               "value": here[key]["value"]}
        if there is not None:
            row["then"] = there[key]["value"]
            row["then_at"] = label(frame_of(then))
        rows.append(row)
    res["data_ref"] = ctx.store.add_dataset("city_time", rows, title=f"The city at {res['clock']}")
    if a.get("kind"):
        res.update(_kind_layer(ctx, m, a["kind"], at, then))
    gaps = [k for k, v in here.items() if v["value"] is None]
    if gaps:
        res["notes"] = [f"{NOTHING} for: {', '.join(gaps)}; say so, don't guess"]
    res["page"] = ("the map, the strip, the lamps and the charts now follow this moment; the user "
                   "can drag the clock or press L to go back to live")
    res["_summary"] = f"clock: {res['clock']}" + (f" vs {res['compare']['at']}" if then else "")
    return res


def _kind_layer(ctx: ToolContext, m: Moments, kind: str, at: datetime | None,
                then: datetime | None) -> dict:
    """One kind's devices at the moment (live states when the clock is live), with what they
    were at the compared moment."""
    if at is None:
        states = {s.device.name: s for s in ctx.city.states(KINDS[kind])}
        devices = [s for s in m.devices(KINDS[kind])]
        now_states = {n: (s.interp.state, s.values()) for n, s in states.items()}
    else:
        snap = m.snapshot(kind, at)
        devices = snap.devices
        now_states = {d.device.name: (st, v) for d, st, v in zip(snap.devices, snap.states, snap.values)}
    before = {}
    if then is not None:
        snap = m.snapshot(kind, then)
        before = {d.device.name: st for d, st in zip(snap.devices, snap.states)}
    feats, rows = [], []
    for d in devices:
        st, vals = now_states.get(d.device.name, ("unknown", {}))
        props = {"id": d.device.name, "label": d.street or d.label, "name": d.label, "state": st,
                 "color": STATE_COLORS.get(st, STATE_COLORS["unknown"]),
                 **{k: v for k, v in (vals or {}).items() if v is not None}}
        row = {"id": d.device.name, "label": d.label, "street": d.street, "state": st,
               **{k: v for k, v in (vals or {}).items() if v is not None}}
        if then is not None:
            was = before.get(d.device.name, "unknown")
            changed = "unknown" not in (st, was) and st != was
            props |= {"then_state": was, "changed": changed}
            row |= {"then_state": was, "changed": changed}
        feats.append(point(d.device.lon, d.device.lat, props))
        rows.append(row)
    when = label(frame_of(at)) if at else "now"
    title = f"{kind.replace('_', ' ')}s at {when}"
    states_seen = sorted({f["properties"]["state"] for f in feats})
    layer = ctx.store.add_layer(f"{kind}_at", feats, geometry="point", title=title,
                                legend=legend_for(states_seen),
                                value_fields=sorted({k for r in rows for k, v in r.items()
                                                     if isinstance(v, (int, float))
                                                     and not isinstance(v, bool)}))
    data = ctx.store.add_dataset(f"{kind}_at", rows, title=title, link={"key": "id"})
    out: dict = {"layer_ref": layer, "devices_data_ref": data,
                 "by_state": {s: sum(1 for r in rows if r["state"] == s) for s in states_seen}}
    if then is not None:
        out["changed"] = sum(1 for r in rows if r.get("changed"))
    return out

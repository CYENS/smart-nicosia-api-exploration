"""The island's electricity grid: get_grid (chatbot/energy)."""

from __future__ import annotations

from datetime import timedelta

from chatbot.energy import LABELS, SOLAR_NOTE, WHERE, carbon, local_midnight, plants, point
from chatbot.energy.tso import TZ
from chatbot.mobility.roads import parse_moment
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj

TOOLS = ToolSet()
tool = TOOLS.tool

# The grid's colours, shared with its map layer and the Outside card (lib/power on the page).
COLORS = {LABELS["conventional_mw"]: "#b8835c", LABELS["wind_mw"]: "#3fc1b0",
          LABELS["solar_mw"]: "#f2cf4a"}
STACK = ("conventional_mw", "wind_mw", "solar_mw")  # bottom to top


def _wall(iso: str) -> str:
    return iso[:16]  # local wall time, as the charts read it


@tool("get_grid",
      "The island's electricity grid (Cyprus, the Republic's side) every 15 minutes, from the "
      "Transmission System Operator: total demand in MW, and what met it: the oil-fired "
      "stations, wind, and the operator's estimate of solar (rooftop and parks) with a few small "
      "plants; the share from wind and sun; and the grid's carbon in kg CO2 per kWh (an "
      "estimate: the oil-fired share times 0.70). Call it for 'how much power is Cyprus using "
      "now', 'how much is wind and solar', 'how clean is the electricity now', 'when was the "
      "peak', or the grid at a past moment the twin recorded (at) or over past days (days). "
      "These are island-wide figures, never Nicosia's. Returns the reading in force with its "
      "age, today's peak and cleanest quarter hour, the usual demand at that time; a data_ref "
      "of long rows (time, source, mw) for a stacked area chart of what met demand, a carbon_ref "
      "(time, demand_mw, usual_demand_mw, carbon_kg_kwh, renewable_pct) for lines, and a "
      "layer_ref of where the power is made (the oil-fired stations and wind farms, by "
      "installed capacity).",
      obj({"at": {"type": "string", "description": "A past local moment, ISO (2026-11-26T19:00): "
                                                   "the grid as the twin's memory recorded it."},
           "days": {"type": "integer", "minimum": 1, "maximum": 31,
                    "description": "Days to chart, ending at the moment (default 1: that day from "
                                   "midnight). 7 for a week."}}))
def h_get_grid(ctx: ToolContext, a: dict) -> dict:
    grid = getattr(ctx.entities, "grid", None)
    if grid is None:
        raise ToolError("the grid isn't connected in this copy of the twin")
    now = ctx.city.now()
    try:
        at = parse_moment(a.get("at"), now)
    except ValueError as e:
        raise ToolError(str(e).replace("road speeds have", "the grid has")) from None
    snap = grid.snapshot(at)
    end = now if snap["live"] else at
    days = a.get("days") or 1
    start = local_midnight(end) - timedelta(days=days - 1)
    rows = grid.series(start, end)
    if not rows:
        if snap["live"]:
            raise ToolError("the operator's page isn't answering and the twin has no copy of the "
                            f"grid yet ({snap.get('error') or 'nothing read'}). Say so plainly")
        raise ToolError(f"the twin's memory has no record of the grid between "
                        f"{start.astimezone(TZ):%a %d %b %H:%M} and {end.astimezone(TZ):%a %d %b %H:%M}; "
                        "it records the grid while the server runs and keeps 15 days back")
    pts = [point(r) for r in rows]
    usual = {u["t"]: u["demand_mw"] for u in snap["usual"]}
    stack_rows = [{"time": _wall(p["t"]), "source": LABELS[k], "mw": p[k], "order": i}
                  for p in pts for i, k in enumerate(STACK) if p[k] is not None]
    line_rows = [{"time": _wall(p["t"]), "demand_mw": p["demand_mw"],
                  "usual_demand_mw": usual.get(p["t"][11:16]) if days == 1 else None,
                  "carbon_kg_kwh": p["carbon_kg_kwh"], "renewable_pct": p["renewable_pct"]} for p in pts]
    when = f"{start.astimezone(TZ):%a %d %b}" + (f" to {end.astimezone(TZ):%a %d %b}" if days > 1 else "")
    data_ref = ctx.store.add_dataset("grid_mix", stack_rows, title=f"What met the island's demand, {when} (MW)")
    carbon_ref = ctx.store.add_dataset("grid_carbon", line_rows,
                                       title=f"The island's demand and carbon, {when}")
    n = snap["now"] or {}
    feats = plants.features({"oil": n.get("conventional_mw"), "wind": n.get("wind_mw")})
    layer_ref = ctx.store.add_layer("grid_plants", feats, geometry="point",
                                    title="Where the island's power is made", legend=plants.legend(),
                                    value_fields=["capacity_mw"])
    ctx.store.layers[layer_ref]["meta"]["note"] = plants.SOLAR_NOTE
    dem = [p["demand_mw"] for p in pts if p["demand_mw"] is not None]
    ren = [p["renewable_pct"] for p in pts if p["renewable_pct"] is not None]
    car = [p["carbon_kg_kwh"] for p in pts if p["carbon_kg_kwh"] is not None]
    hm = (n.get("t") or "")[11:16]
    res = {
        "where": WHERE, "status": snap["status"],
        "source": "live, the operator's page" if snap["live"] else "the twin's memory",
        "now" if snap["live"] else "then": {k: n.get(k) for k in (
            "t", "age_min", "demand_mw", "conventional_mw", "wind_mw", "solar_mw", "carbon_kg_kwh",
            "renewable_pct", "wind_pct", "solar_pct")} if n else None,
        "usual_demand_mw_at_this_time": usual.get(hm), "usual_days": snap["usual_days"],
        "day_peak": snap["peak"], "day_cleanest": snap["cleanest"],
        "range": {"from": _wall(pts[0]["t"]), "to": _wall(pts[-1]["t"]), "quarter_hours": len(pts),
                  "demand_mw": {"min": min(dem), "mean": round(sum(dem) / len(dem)), "max": max(dem)} if dem else None,
                  "renewable_pct_mean": round(sum(ren) / len(ren), 1) if ren else None,
                  "carbon_kg_kwh_mean": round(sum(car) / len(car), 3) if car else None},
        "installed_mw": snap["installed"], "solar_note": SOLAR_NOTE, "carbon_method": carbon.METHOD,
        "colors": COLORS,
        "data_ref": data_ref, "carbon_ref": carbon_ref, "layer_ref": layer_ref,
        "chart_hint": ("data_ref: a stacked area, x time (temporal), y mw stacked, colour source with "
                       "`colors`, sorted by order; carbon_ref: demand_mw against usual_demand_mw, or "
                       "carbon_kg_kwh as a line"),
        "map_hint": ("layer_ref is where the power is made, by installed capacity: style columns_3d "
                     "with value_field capacity_mw. Never draw the grid's figures on Nicosia's streets"),
    }
    if snap.get("error"):
        res["feed_note"] = f"the latest read of the operator's page failed ({snap['error']}); this is the last good copy"
    if n:
        res["_summary"] = (f"{n['demand_mw']:,} MW, {round(n['renewable_pct'] or 0)}% wind and sun, "
                           f"{n['carbon_kg_kwh']} kg CO2/kWh")
    return res

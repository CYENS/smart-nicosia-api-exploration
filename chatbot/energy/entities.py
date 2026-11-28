"""The island's grid as one of the twin's entities: `grid-cyprus`, kind `power_grid`.

It has no place on Nicosia's map (its figures are the whole island's), so it has no geometry:
the inspector opens it from Ctrl K, a chip in an answer or the Outside card. Its 24-hour series
is demand every 15 minutes, and at a moment of the city clock it reads the twin's memory only.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from chatbot import entities as E
from chatbot.energy import carbon
from chatbot.energy.tso import PAGE_URL, TZ

GRID_ID = "grid-cyprus"
E.KIND_LABELS["power_grid"] = "Electricity grid"
STATES = {  # status -> (label, colour, tone)
    "live": ("Live", "#ffae45", "live"), "known": ("Recorded", "#ffae45", "live"),
    "stale": ("Not updated lately", "#8798b0", "warn"),
    "unavailable": ("Not answering", "#ff5a4e", "alarm"), "unknown": ("No record", "#8798b0", "muted"),
}


def _grid(ents):
    return getattr(ents, "grid", None)


def _num(v, unit: str, key: str, label: str, as_of: str | None) -> dict | None:
    if v is None:
        return None
    return {"key": key, "label": label, "value": v, "unit": unit, "as_of": as_of}


def build_grid(ents, eid: str, near, at: datetime | None = None) -> dict | None:
    if eid.strip().lower() != GRID_ID:
        return None
    grid = _grid(ents)
    if grid is None:
        return None
    snap = grid.snapshot(at)
    n = snap["now"] or {}
    label, color, tone = STATES[snap["status"]]
    if n.get("demand_mw") is not None:
        label = f"{n['demand_mw']:,} MW"
        if n.get("renewable_pct") is not None:
            label += f", {round(n['renewable_pct'])}% from wind and sun"
    state = {"value": snap["status"], "label": label, "color": color, "tone": tone,
             "stale": snap["status"] == "stale", "reason": snap.get("error") or snap.get("reason"),
             "local": None, "as_of": n.get("t"), "age_s": n["age_min"] * 60 if n.get("age_min") is not None else None}
    as_of = n.get("t")
    readings = [x for x in (
        _num(n.get("demand_mw"), "MW", "demand_mw", "Demand", as_of),
        _num(n.get("conventional_mw"), "MW", "conventional_mw", "Oil-fired stations", as_of),
        _num(n.get("wind_mw"), "MW", "wind_mw", "Wind", as_of),
        _num(n.get("solar_mw"), "MW", "solar_mw", "Solar and small plants, estimated", as_of),
        _num(round(n["carbon_kg_kwh"] * 1000) if n.get("carbon_kg_kwh") is not None else None,
             "g CO2/kWh", "carbon_g_kwh", "Carbon, estimated", as_of),
        _num(n.get("renewable_pct"), "%", "renewable_pct", "From wind and sun", as_of),
    ) if x]
    inst = snap.get("installed") or {}
    installed = ", ".join(f"{name} {inst[k]:,.0f} MW" for k, name in (
        ("conventional_mw", "oil-fired"), ("wind_mw", "wind"), ("solar_mw", "solar"),
        ("biomass_mw", "biomass")) if inst.get(k)) or None
    peak = snap.get("peak")
    usual_now = None
    if snap.get("usual") and as_of:
        hm = as_of[11:16]
        u = next((u for u in snap["usual"] if u["t"] == hm), None)
        if u:
            usual_now = f"{u['demand_mw']:,} MW at this time on a usual {'weekend day' if datetime.fromisoformat(as_of).weekday() >= 5 else 'weekday'}, over {snap['usual_days']} days"
    return E.new_entity(
        GRID_ID, "power_grid", "Cyprus electricity grid",
        subtitle="The whole island (the Republic's side), every 15 minutes",
        state=state, readings=readings,
        attributes=[E.attr("Covers", snap["where"]),
                    E.attr("Peak today", f"{peak['demand_mw']:,} MW at {peak['t'][11:16]}" if peak else None),
                    E.attr("Usual", usual_now),
                    E.attr("Installed", installed),
                    E.attr("Solar figure", snap["solar_note"]),
                    E.attr("Carbon", carbon.METHOD)],
        sources=[{"source": "tso_grid", "name": "Transmission System Operator of Cyprus",
                  "detail": "generation on the transmission system, every 15 minutes", "url": PAGE_URL,
                  "as_of": as_of},
                 {"source": "oil_co2", "name": "CO2 per kWh of the oil-fired stations",
                  "detail": f"{carbon.CONVENTIONAL_KG_KWH:.2f} kg CO2/kWh, an estimate"}],
        as_of=as_of, has_series=True, grid=snap,
        _for_model={"note": "island-wide figures, not Nicosia's", "carbon_method": carbon.METHOD})


def grid_at(ents, entity: dict, at: datetime) -> dict:
    from chatbot.moments import label

    e = build_grid(ents, entity["id"], None, at=at) or dict(entity)
    known = (e.get("grid") or {}).get("now") is not None
    e["moment"] = {"at": at.astimezone(TZ).isoformat(timespec="minutes"), "label": label(at),
                   "known": known, "applies": True}
    return e


def grid_series(ents, entity: dict, end: datetime | None = None) -> dict | None:
    """Demand in the 24 hours up to `end` (the clock's moment; default now), every 15 minutes."""
    grid = _grid(ents)
    if grid is None:
        return None
    now = end or grid.clock()
    start = now - timedelta(hours=24)
    rows = grid.series(start, now)
    if len(rows) < 2:
        return None
    points = [{"t": r.t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M"), "v": r.demand_mw} for r in rows]
    vals = [p["v"] for p in points if p["v"] is not None]
    return {"metric": "demand_mw", "label": "Demand", "unit": "MW", "draw": "line", "step_min": 15,
            "points": points, "from": points[0]["t"], "to": points[-1]["t"], "summary": E.summary(vals),
            "source": "memory"}


def search_items(ents) -> list[dict]:
    if _grid(ents) is None:
        return []
    item = E.index_item(GRID_ID, "power_grid", "Cyprus electricity grid", "Energy",
                        "Demand, wind, solar and carbon now, island-wide", None, None,
                        ["electricity grid power demand wind solar carbon energy", "ρεύμα ηλεκτρισμός",
                         "transmission system operator tso"])
    item["_aliases"] = {"grid", "power", "electricity", "energy", "carbon"}
    return [item]


E.register_kind(E.Kind("power_grid", lambda i: i.strip().lower() == GRID_ID, build_grid, ttl_s=60))
E.register_series("power_grid", grid_series)
E.register_at("power_grid", grid_at)
E.register_search(search_items, {"Energy": 1})

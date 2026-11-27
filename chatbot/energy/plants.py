"""Where the island's power is made: the generating stations, for the grid's map layer.

The grid's figures belong to the whole island, so the honest place to draw them is where the
power comes from, not Nicosia's streets. The file `data/cyprus_power_plants.json` holds the
three oil-fired stations and the wind farms as OpenStreetMap maps them (ODbL), each with its
installed capacity. It was cut once from one Overpass answer by `build` and is committed: the
twin never asks Overpass for it.

Solar is left out on purpose: about 1,000 MW of it sit on roofs and in parks across the island,
and OpenStreetMap gives a capacity for only a handful of parks, so points would draw a small
and misleading part of it. The layer says so.

Run `python -m chatbot.energy.plants <overpass.json> [<turbines.json>]` to cut the file again:
the first answer is `nwr["power"="plant"]` over the Republic's side with `out center tags`,
the optional second `node["power"="generator"]["generator:source"="wind"]` with `out`.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

from chatbot.settings import ROOT

PLANTS_FILE = ROOT / "data" / "cyprus_power_plants.json"
OSM_COPYRIGHT = "https://www.openstreetmap.org/copyright"

# How each kind is drawn and named. Colours are the grid's own, shared with its charts.
KINDS = {
    "oil": {"label": "Oil-fired station", "color": "#b8835c"},
    "wind": {"label": "Wind farm", "color": "#3fc1b0"},
}
SOLAR_NOTE = ("Solar is not drawn: about 1,000 MW of it sit on roofs and in parks across the "
              "island, too many and too small to map")
CLUSTER_KM = 4.0  # wind turbines this close together make one farm
# Plants that are mapped but not counted in the operator's installed conventional capacity
# (1,478 MW is exactly Vasilikos, Dhekelia and Moni), so not generating for this grid yet.
NOT_IN_OPERATOR_CAPACITY = ("gas",)

_MW = re.compile(r"([\d.]+)\s*(k|m)?w", re.IGNORECASE)


def _mw(text: str | None) -> float | None:
    m = _MW.search(text or "")
    if not m:
        return None
    v = float(m.group(1))
    return round(v / 1000 if (m.group(2) or "").lower() == "k" else v, 2)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    kx = 111.32 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * 110.57, (a[1] - b[1]) * kx)


def build(plants: dict, turbines: dict | None = None) -> dict:
    """The plants file from Overpass answers (see the module docstring)."""
    out: list[dict] = []
    for e in plants.get("elements", []):
        tags = e.get("tags") or {}
        src = (tags.get("plant:source") or tags.get("generator:source") or "").lower()
        if src not in KINDS:
            continue
        where = e.get("center") or e
        if "lat" not in where:
            continue
        out.append({
            "id": f"osm-{e['type'][0]}{e['id']}", "kind": src,
            "name": tags.get("name:en") or tags.get("name") or KINDS[src]["label"],
            "name_el": tags.get("name") if tags.get("name:en") else None,
            "operator": tags.get("operator"),
            "capacity_mw": _mw(tags.get("plant:output:electricity")),
            "lat": round(where["lat"], 5), "lon": round(where["lon"], 5),
        })
    # Wind turbines the plants don't cover, gathered into farms.
    farms: list[dict] = []
    for e in (turbines or {}).get("elements", []):
        if "lat" not in e:
            continue
        p = (e["lat"], e["lon"])
        if any(_km(p, (x["lat"], x["lon"])) < CLUSTER_KM for x in out if x["kind"] == "wind"):
            continue
        f = next((f for f in farms if _km(p, (f["lat"], f["lon"])) < CLUSTER_KM), None)
        if f is None:
            f = {"lat": p[0], "lon": p[1], "n": 0, "mw": 0.0, "known": 0, "ops": set(), "id": e["id"]}
            farms.append(f)
        f["n"] += 1
        f["lat"] += (p[0] - f["lat"]) / f["n"]
        f["lon"] += (p[1] - f["lon"]) / f["n"]
        mw = _mw((e.get("tags") or {}).get("generator:output:electricity"))
        if mw:
            f["mw"] += mw
            f["known"] += 1
        if op := (e.get("tags") or {}).get("operator"):
            f["ops"].add(op)
    for f in farms:
        if f["n"] < 2:
            continue
        out.append({"id": f"osm-n{f['id']}", "kind": "wind", "name": f"Wind farm of {f['n']} turbines",
                    "name_el": None, "operator": ", ".join(sorted(f["ops"])) or None,
                    "capacity_mw": round(f["mw"], 1) if f["known"] == f["n"] else None,
                    "lat": round(f["lat"], 5), "lon": round(f["lon"], 5)})
    out.sort(key=lambda p: (p["kind"] != "oil", -(p["capacity_mw"] or 0), p["name"]))
    return {"cut": date.today().isoformat(), "source": "OpenStreetMap contributors",
            "licence": "ODbL", "licence_url": OSM_COPYRIGHT, "plants": out}


@lru_cache(maxsize=1)
def load(path: Path = PLANTS_FILE) -> tuple[dict, ...]:
    """The stations, or none if the file is missing."""
    try:
        return tuple(json.loads(path.read_text(encoding="utf-8"))["plants"])
    except (OSError, ValueError, KeyError):
        return ()


def features(now: dict | None = None) -> list[dict]:
    """The stations as map features. `now` (MW by kind: oil, wind) is the live output of each
    kind together, which the popups give for the group: no station reports its own."""
    feats = []
    for p in load():
        k = KINDS[p["kind"]]
        group = (now or {}).get(p["kind"])
        cap = p.get("capacity_mw")
        lines = [p["name"], k["label"] + (f", {cap:,.0f} MW installed" if cap else "")]
        if group is not None:
            together = "all oil-fired stations" if p["kind"] == "oil" else "every wind farm"
            lines.append(f"{group:,.0f} MW now from {together} together")
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                      "properties": {"id": p["id"], "label": p["name"], "state": k["label"], "color": k["color"],
                                     "capacity_mw": cap or 0, "operator": p.get("operator"),
                                     "popup": "\n".join(lines)}})
    return feats


def legend() -> dict[str, str]:
    return {k["label"]: k["color"] for k in KINDS.values()}


if __name__ == "__main__":
    first = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    second = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else None
    PLANTS_FILE.write_text(json.dumps(build(first, second), ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
    print(f"wrote {PLANTS_FILE} ({len(load.__wrapped__(PLANTS_FILE))} plants)")

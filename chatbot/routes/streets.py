"""The street rules for the page's Layers sheet: each inventory as a map layer, and how old
each one is. These read the copies the twin keeps (chatbot/streets); they never wait on a feed
for more than the first read of the day."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from chatbot.routes import agent_of
from chatbot.streets import layers as Ly
from chatbot.streets.analysis import cycle_network

router = APIRouter()

LAYERS = {
    "limits": ("speed_limits", "Speed limits"),
    "zones": ("zones", "Slow zones"),
    "signals": ("signals", "Traffic signals"),
    "crossings": ("crossings", "Pedestrian crossings"),
    "cycleways": ("cycleways", "Cycleways"),
    "bus_lanes": ("bus_lanes", "Bus lanes"),
    "cameras": ("cameras", "Enforcement cameras"),
}
SOURCES = {"limits": ["speed_limits", "openstreetmap"], "zones": ["slow_zones"], "signals": ["traffic_signals"],
           "crossings": ["pedestrian_crossings"], "cycleways": ["cycleways"], "bus_lanes": ["bus_lanes"],
           "cameras": ["enforcement_cameras"]}
_cache: dict[str, tuple[tuple, bytes]] = {}


def _streets(request: Request):
    st = getattr(agent_of(request), "streets", None)
    if st is None:
        raise HTTPException(503, "the street rules aren't available in this deployment")
    return st


@router.get("/api/streets/status")
def status(request: Request) -> dict:
    return _streets(request).status()


@router.get("/api/streets/layers/{name}")
def layer(name: str, request: Request) -> Response:
    if name not in LAYERS:
        raise HTTPException(404, f"no streets layer {name!r}; layers: {', '.join(LAYERS)}")
    st = _streets(request)
    key, title = LAYERS[name]
    inv = st.feeds.get(key)
    if inv.copy == "none" and not inv.records:
        raise HTTPException(503, inv.error or f"{title} couldn't be read yet; try again in a minute")
    status = "ready"
    lim = st.limits() if name == "limits" else None
    stamp = (name, id(inv), id(lim), st.feeds.version)
    hit = _cache.get(name)
    if hit is not None and hit[0] == stamp:
        return Response(hit[1], media_type="application/json", headers={"Cache-Control": "no-store"})
    by = {}
    if name == "limits":
        signs = inv.records
        if lim is None:
            status = "loading"  # the street network isn't in yet: the signs alone
            feats = Ly.sign_features(signs)
        else:
            feats = Ly.limit_features(lim.pieces) + Ly.sign_features(signs, lim.snaps)
            for p in lim.pieces:
                by[p["tier"]] = by.get(p["tier"], 0) + p["length_m"]
        legend = Ly.limit_legend()
    elif name == "zones":
        feats = Ly.zone_features(inv.records, st.records("zone_entrances"))
        legend = {"Zone 30, planned": Ly.ZONE_COLOR}
    elif name == "signals":
        feats, legend = Ly.signal_features(inv.records), {"traffic signals": Ly.SIGNAL_COLOR}
    elif name == "crossings":
        feats, legend = Ly.crossing_features(inv.records), {"pedestrian crossing": Ly.CROSSING_COLOR}
    elif name == "cycleways":
        net = cycle_network(st)
        feats = Ly.cycle_features(inv.records, net["gaps"], net["loose_ends"])
        legend = {"cycleway": Ly.CYCLE_COLOR, "gap or loose end": Ly.GAP_COLOR}
        by = {"pieces": len(net["pieces"]), "gaps": len(net["gaps"]), "km": round(net["total_m"] / 1000, 1)}
    elif name == "bus_lanes":
        feats, legend = Ly.bus_lane_features(inv.records), {"bus lane": Ly.BUS_LANE_COLOR}
        by = {s: sum(1 for b in inv.records if b["status"] == s) for s in ("active", "future")}
    else:
        cams = st.cameras()
        feats, legend = Ly.camera_features(cams), {"enforcement camera": Ly.CAMERA_COLOR}
        by = {"rough": sum(1 for c in cams if c["placed"] == "rough"), "moved": sum(1 for c in cams if c["placed"] == "junction")}
    count = len(inv.records) if name != "cycleways" else len(inv.records)
    body = {"type": "FeatureCollection", "features": feats,
            "meta": {"title": title, "count": count, "legend": legend, "status": status, "by": by,
                     "inventory": inv.public(), "sources": SOURCES[name],
                     "network_error": st.network.error if name == "limits" else None}}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    _cache[name] = (stamp, raw)
    return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})

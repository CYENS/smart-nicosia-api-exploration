"""Devices for the page's own map layers, without asking the assistant: every parking bay, bin,
air station or traffic camera as it is now (the points find_devices draws), and the alarms
raised now. The topic chips and the Layers sheet switch them on; the city clock restates the
first three at a past moment from the twin's memory, as it does any layer of devices.

Both read what the strip and the tools already fetch: the city's snapshots are shared and kept
for `snapshot_ttl_s`, and the alarms for a minute here, so a page costs the city API nothing
more."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot import context
from chatbot.city import KINDS
from chatbot.overview import CITY_TTL_S, cached
from chatbot.routes import agent_of
from chatbot.store import STATE_COLORS, legend_for
from chatbot.tools.city import alarm_features, device_feature

router = APIRouter()

TITLES = {
    "parking_sensor": "Parking bays", "waste_bin": "Waste bins",
    "air_station": "Air and noise stations", "camera": "Traffic cameras",
}


def device_layer(agent, kind: str) -> dict:
    """Every device of `kind` with a trusted reading, as points coloured by state. Bays, bins
    and stations that stopped reporting are left out, as the strip and find_devices leave
    them out."""
    states = agent.city.states(KINDS[kind])
    fresh = [s for s in states if not s.interp.stale]
    by_state = Counter(s.interp.state for s in fresh)
    features = [f for s in fresh if (f := device_feature(s)) is not None]
    return {"type": "FeatureCollection", "features": features, "meta": {
        "kind": kind, "title": TITLES[kind], "count": len(features), "total": len(states),
        "left_out_stale": len(states) - len(fresh), "by_state": dict(by_state.most_common()),
        "legend": legend_for(list(by_state)),
    }}


@router.get("/api/devices/{kind}")
def devices(kind: str, request: Request) -> JSONResponse:
    if kind not in TITLES:
        raise HTTPException(400, f"kind must be one of {', '.join(TITLES)}")
    agent = agent_of(request)
    body = cached(f"devices:{kind}", CITY_TTL_S, lambda: device_layer(agent, kind))
    if body is None:
        raise HTTPException(503, "the city's sensors aren't answering just now")
    return JSONResponse(body, headers={"Cache-Control": "max-age=30"})


@router.get("/api/alarms")
def alarms(request: Request) -> JSONResponse:
    """The newest active, unacknowledged alarms (at most 20, as the city API gives them)."""
    agent = agent_of(request)
    res = cached("alarms", CITY_TTL_S, lambda: context.alarms(agent.city, 20))
    if res is None:
        raise HTTPException(503, "the city's alarms aren't answering just now")
    features = alarm_features(res["alarms"])
    return JSONResponse({"type": "FeatureCollection", "features": features, "meta": {
        "title": "Alarms now", "count": len(features), "total": len(res["alarms"]),
        "legend": {"alarm": STATE_COLORS["alarm"]},
    }}, headers={"Cache-Control": "max-age=30"})

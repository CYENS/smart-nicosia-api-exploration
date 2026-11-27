"""Road reports for the page: closures, jams, accidents, hazards, roadworks and diversions in
Greater Nicosia, now or at a moment of the city clock (`?at=`), as one GeoJSON layer with a
summary the Traffic card and the Layers sheet read. Every page shares one fetch of each feed
(chatbot/mobility/reports.py)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from chatbot.mobility.reports import (KIND_COLORS, KINDS, LEGEND, OLD, RECENT, SOURCE_NAMES, RoadReport,
                                      age_words, count_words, counts, features, local_text, sort_key,
                                      when_text)
from chatbot.moments import label
from chatbot.routes import agent_of
from chatbot.routes.live import AT, moment

router = APIRouter()
LIST = 8  # reports the card lists


def _then(words: str | None) -> str | None:
    """An age at a past moment of the clock: "12 min before", "the day before"."""
    if not words:
        return words
    return "the day before" if words == "yesterday" else words.replace(" ago", " before")


def _item(r: RoadReport, now, past: bool = False) -> dict:
    status = r.status(now)
    age = when_text(r, status, now) if r.source == "traffic_events" else age_words(r.since, now)
    return {"id": r.id, "kind": r.kind, "kind_label": r.kind_label, "label": r.label, "street": r.street,
            "color": KIND_COLORS[r.kind], "source": r.source, "status": status,
            "since": local_text(r.since), "until": local_text(r.until),
            "age": _then(age) if past else age,
            "old": bool(r.since and now - r.since > OLD), "lon": r.lon, "lat": r.lat}


def summary(reports: list[RoadReport], now, past: bool = False) -> dict:
    """What the Traffic card says: counts by kind, what Waze drivers reported in the last half
    hour, the newest jam, and the reports worth listing (loud and new first, long closures
    last)."""
    active = [r for r in reports if r.status(now) == "active"]
    recent = [r for r in active if r.since and timedelta(0) <= now - r.since <= RECENT and r.source != "traffic_events"]
    jams = sorted((r for r in active if r.kind == "jam"), key=lambda r: r.since or now, reverse=True)
    fresh = [r for r in active if not (r.since and now - r.since > OLD)]
    fresh.sort(key=lambda r: sort_key(r, now))
    long = sorted((r for r in active if r.since and now - r.since > OLD), key=lambda r: sort_key(r, now))
    by = counts(active)
    return {"counts": by, "in_words": count_words(by), "total": len(active),
            "recent": counts(recent), "recent_words": count_words(counts(recent)) if recent else None,
            "newest_jam": _item(jams[0], now, past) if jams else None,
            "long_closures": sum(1 for r in long if r.kind == "closure"),
            "planned": sum(1 for r in reports if r.status(now) == "planned"),
            "list": [_item(r, now, past) for r in (fresh + long)[:LIST]]}


@router.get("/api/road-reports")
def road_reports(request: Request, at: str | None = AT) -> JSONResponse:
    """Every road report in Greater Nicosia now (or at `at`, from the twin's memory), drawn
    by kind, with a summary. A moment the memory didn't watch the feeds for is "unknown"."""
    agent = agent_of(request)
    m = agent.mobility
    if m is None or getattr(m, "reports", None) is None:
        raise HTTPException(404, "road reports aren't available in this deployment")
    when = moment(request, at)
    got = m.reports.at(when) if when is not None else m.reports.current()
    now = when or got["at"]
    reports = got["reports"]
    feeds = {s: {k: v for k, v in f.items() if k in ("state", "error", "note", "count")}
             | {"name": SOURCE_NAMES.get(s, s)} for s, f in got["feeds"].items()}
    body = {"type": "FeatureCollection", "features": features(reports, now),
            "meta": {"status": got["status"], "error": got.get("error"), "at": local_text(when),
                     "label": label(when) if when else None, "feeds": feeds, "legend": LEGEND,
                     "kinds": {k: {"label": v[0], "one": v[1], "many": v[2], "color": KIND_COLORS[k]} for k, v in KINDS.items()},
                     **summary(reports, now, past=when is not None)}}
    # A feed not read yet (the server just started) is asked again soon: never keep that answer.
    waiting = any(f.get("state") == "waiting" for f in feeds.values())
    cache = "no-store" if waiting or got["status"] == "unavailable" else "max-age=60" if when is None else "max-age=300"
    return JSONResponse(body, headers={"Cache-Control": cache})

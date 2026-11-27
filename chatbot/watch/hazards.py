"""Natural hazards in the watch: an earthquake near Nicosia, an official weather warning for it or
high fire danger in its district, raised as incidents beside the devices' (the feeds are read and
recorded by chatbot/hazards; how loud each one is lives in chatbot/hazards/feeds.py).

Each hazard is a member of an incident as a device's detection is: its id (eq-..., wx-...,
fire-nicosia) stands in for the device, and the epicentre or the city for the place.

- quake: an earthquake that raises an incident by `quake_rules` (M3 or more within 100 km of the
  centre, M4.5 within 250, M2.5 within 20, or anyone near Nicosia reporting it felt). High, so the
  open pages toast it, when ten or more people within 25 km reported feeling it, M5 within 150 km
  or M4 within 50; a small distant quake never pages. It stays happening for three hours. Quakes
  within 60 km and a day of each other are one incident: a main shock and its aftershocks.
- weather_warning: a Meteoalarm warning that covers Nicosia, from when it was issued to its expiry
  (or its withdrawal). Yellow is low (the inbox, no toast); orange and red are high. Warnings of
  one kind whose hours fall within 12 hours of each other are one incident (a heatwave's run of
  daily warnings).
- fire_danger: days in a row with the Nicosia district's fire danger very high or worse (EFFIS),
  up to today; very high is medium, extreme and very extreme high. Forecasts don't raise one.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from chatbot.geo import haversine_m
from chatbot.hazards import feeds as F
from chatbot.hazards.feeds import TZ, Hazard
from chatbot.hazards.service import from_memory
from chatbot.memory import Memory, Unavailable
from chatbot.watch import detectors as D
from chatbot.watch.detectors import Detection, Evidence, Observed, worst
from chatbot.watch.incidents import Family, register_family

WINDOW = timedelta(hours=72)
AHEAD = timedelta(days=3)  # warnings issued for the coming days
WARNINGS_LINK = timedelta(hours=12)
FIRE_FIRST = 3  # very high
FIRE_GRACE = timedelta(hours=6)  # a run goes on past midnight until the new day is read
FIRE_DEVICE = "fire-nicosia"
SOURCES = {"earthquake": "emsc", "warning": "meteoalarm", "fire": "effis"}


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def day_time(t: datetime) -> str:
    return f"{t.astimezone(TZ):%a %d %b %H:%M}"


def hhmm(t: datetime) -> str:
    return f"{t.astimezone(TZ):%H:%M}"


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def observe(memory: Memory | None, obs: Observed) -> None:
    """The hazards of the window into obs.more["hazards"] (None when the memory can't say)."""
    if memory is None:
        obs.more["hazards"] = None
        return
    try:
        obs.more["hazards"] = from_memory(memory, obs.now - WINDOW, obs.now + AHEAD)
    except Unavailable:
        obs.more["hazards"] = None


def _hazards(obs: Observed, kind: str) -> list[Hazard] | None:
    hs = obs.more.get("hazards")
    if hs is None:
        return None
    return [h for h in hs if h.kind == kind]  # type: ignore[union-attr]


# -- earthquakes ---------------------------------------------------------------------------------


def felt_words(felt: dict, subject: str = "it") -> str | None:
    """'3,491 people reported feeling it to EMSC, 817 within 25 km of Nicosia, most calling it
    weak (intensity III)'."""
    n = felt.get("count") or 0
    if not n:
        return None
    near = felt.get("near_nicosia")
    out = f"{n:,} {'people' if n != 1 else 'person'} reported feeling {subject} to EMSC"
    if near:
        i = felt.get("near_intensity")
        out += (f", {near:,} within {F.FELT_NEAR_KM:.0f} km of Nicosia"
                + (f", most calling it {F.INTENSITY_WORDS.get(i, '?')} (intensity {F.ROMAN.get(i, i)})" if i else ""))
    elif near == 0:
        out += f", none within {F.FELT_NEAR_KM:.0f} km of Nicosia"
    return out


def words_and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def quakes(obs: Observed) -> list[Detection] | None:
    hs = _hazards(obs, "earthquake")
    if hs is None:
        return None
    out = []
    for h in hs:
        raises, severity, why = F.quake_rules(h)
        if not raises or h.start > obs.now:
            continue
        d, felt = h.details, h.details.get("felt") or {}
        over = h.start + F.QUAKE_LASTS
        end = over if obs.now >= over else None
        mag = h.value or 0.0
        ev = [Evidence(h.start, f"M{mag:.1f} earthquake {d.get('where')}, {d.get('depth_km', 0):.0f} km deep, "
                                f"located by {d.get('located_by')}", h.id, "event", value=mag, unit="M")]
        if words := felt_words(felt):
            read = felt.get("read_at")
            t = datetime.fromisoformat(read).replace(tzinfo=TZ) if read else h.start
            ev.append(Evidence(max(t, h.start), words, h.id, "event", value=float(felt["count"]),
                               unit="reports"))
        if end is not None:
            ev.append(Evidence(end, f"{F.QUAKE_LASTS.seconds // 3600} hours on: counted as over", h.id,
                               "detection"))
        out.append(Detection(
            "quake", "quake", h.id, "earthquake", h.start, end, severity,
            f"M{mag:.1f} at {hhmm(h.start)}, {d.get('depth_km', 0):.0f} km deep, {d.get('where')}", ev,
            source="emsc", label=f"M{mag:.1f}, {d.get('where')}", lat=h.lat, lon=h.lon,
            extra={"magnitude": mag, "depth_km": d.get("depth_km"), "where": d.get("where"),
                   "distance_km": d.get("distance_km"), "located_by": d.get("located_by"),
                   "felt": {k: v for k, v in felt.items() if k != "cells"}, "why": why}))
    return out


def _quake_link(a: dict, b: dict) -> bool:
    if a["device"] == b["device"]:
        return True
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return False
    near = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= F.QUAKE_SEQUENCE_KM * 1000
    return near and abs(a["start"] - b["start"]) <= F.QUAKE_SEQUENCE_WITHIN.total_seconds() * 1000


def _strongest(members: list[dict]) -> dict:
    return max(members, key=lambda m: ((m.get("extra") or {}).get("magnitude") or 0, -m["start"]))


def _quake_place(members: list[dict]) -> dict:
    m = _strongest(members)
    w = (m.get("extra") or {}).get("where") or "near Cyprus"
    return {"label": cap(w), "phrase": w, "street": None, "lat": m.get("lat"), "lon": m.get("lon")}


def _mag(m: dict) -> float:
    return (m.get("extra") or {}).get("magnitude") or 0.0


def _quake_compose(members: list[dict], place: dict) -> tuple[str, str, str]:
    qs = sorted({m["device"]: m for m in members}.values(), key=lambda m: m["start"])
    top = _strongest(qs)
    x = top.get("extra") or {}
    severity = worst(*(m["severity"] for m in qs))
    where = x.get("where") or place["phrase"]
    mag = _mag(top)
    by = sorted({(m.get("extra") or {}).get("located_by") for m in qs} - {None},
                key=lambda b: (b != "the Cyprus Geological Survey Department", b))
    located = words_and(by) if by else "EMSC"
    felt_m = max(qs, key=lambda m: ((m.get("extra") or {}).get("felt") or {}).get("count") or 0)
    felt = (felt_m.get("extra") or {}).get("felt") or {}
    if len(qs) == 1:
        title = f"M{mag:.1f} earthquake {where}"
        story = (f"An M{mag:.1f} earthquake at {day_time(_dt(top['start']))}, {x.get('depth_km', 0):.0f} km "
                 f"deep, {where}, located by {located}.")
        felt_line = felt_words(felt)
    else:
        a, b = _dt(qs[0]["start"]).astimezone(TZ), _dt(qs[-1]["start"]).astimezone(TZ)
        span_ = (f"on {a:%a %d %b} from {a:%H:%M} to {b:%H:%M}" if a.date() == b.date()
                 else f"from {day_time(a)} to {day_time(b)}")
        big = sorted((m for m in qs if _mag(m) >= max(4.0, mag - 0.5)), key=lambda m: -_mag(m))[:3] or [top]
        strongest = words_and([f"M{_mag(m):.1f} at {hhmm(_dt(m['start']))}" for m in big])
        title = f"M{mag:.1f} earthquake {where}, and {len(qs) - 1} more"
        story = (f"{len(qs)} earthquakes {where} {span_}, the strongest {strongest}; located by "
                 f"{located}.")
        felt_line = felt_words(felt, f"the M{_mag(felt_m):.1f} at {hhmm(_dt(felt_m['start']))}")
    if felt_line:
        story += f" {cap(felt_line)}."
    return title, severity, story


def _quake_cleared(x: dict) -> str:
    return f"cleared: {F.QUAKE_LASTS.seconds // 3600} hours since the last quake"


# -- weather warnings ----------------------------------------------------------------------------


def _hours(h: Hazard) -> str:
    """'Thu 24 Jul 11:00 to 16:59', or across days 'Thu 24 Jul 21:00 to Fri 25 Jul 16:59'."""
    a = h.start.astimezone(TZ)
    if h.end is None:
        return f"from {day_time(a)}"
    b = h.end.astimezone(TZ)
    return f"{day_time(a)} to {b:%H:%M}" if a.date() == b.date() else f"{day_time(a)} to {day_time(b)}"


def warnings(obs: Observed) -> list[Detection] | None:
    hs = _hazards(obs, "warning")
    if hs is None:
        return None
    out = []
    for h in hs:
        d = h.details
        if not d.get("covers_nicosia"):
            continue
        issued = min(h.issued or h.start, h.start)
        if issued > obs.now:
            continue
        end = h.end if h.end is not None and h.end <= obs.now else None
        level = d.get("level_word") or (h.level or "").capitalize()
        name = f"{level} {d.get('awareness')} warning"
        ev = [Evidence(issued, f"{name} issued by the Department of Meteorology for {d.get('area') or 'Cyprus'}"
                       + (f": {d['headline']}" if d.get("headline") else ""), h.id, "alarm")]
        if h.start > issued:
            ev.append(Evidence(h.start, "in force from here", h.id, "event"))
        if d.get("withdrawn_at") and h.end is not None:
            ev.append(Evidence(h.end, "left Meteoalarm's feed before its expiry: withdrawn or replaced", h.id))
        elif h.end is not None:
            ev.append(Evidence(h.end, "expired", h.id))
        out.append(Detection(
            "weather_warning", "warning", h.id, "warning", issued, end,
            F.WARNING_SEVERITY.get(h.level or "", "low"), f"{name}, {_hours(h)}", ev,
            source="meteoalarm", label=f"{name} for {d.get('area') or 'Cyprus'}", lat=h.lat, lon=h.lon,
            extra={"level": h.level, "awareness": d.get("awareness"), "onset_ms": _ms(h.start),
                   "expires_ms": _ms(h.end) if h.end else None, "area": d.get("area"),
                   "hours": _hours(h), "headline": d.get("headline"),
                   "description": d.get("description"), "withdrawn": bool(d.get("withdrawn_at"))}))
    return out


def _warning_link(a: dict, b: dict) -> bool:
    if a["device"] == b["device"]:
        return True
    xa, xb = a.get("extra") or {}, b.get("extra") or {}
    if xa.get("awareness") != xb.get("awareness"):
        return False
    gap = WARNINGS_LINK.total_seconds() * 1000
    a0, a1 = xa.get("onset_ms") or a["start"], xa.get("expires_ms") or float("inf")
    b0, b1 = xb.get("onset_ms") or b["start"], xb.get("expires_ms") or float("inf")
    return a0 <= b1 + gap and b0 <= a1 + gap


def _city_place(label: str, phrase: str) -> dict:
    return {"label": label, "phrase": phrase, "street": None, "lat": F.NICOSIA[0], "lon": F.NICOSIA[1]}


def _warning_place(members: list[dict]) -> dict:
    areas = Counter((m.get("extra") or {}).get("area") or "Cyprus" for m in members)
    area = areas.most_common(1)[0][0]
    return _city_place(f"{area}, Nicosia included" if area == "Cyprus" else area, f"for {area}")


LEVEL_RANK = {"yellow": 1, "orange": 2, "red": 3}


def _warning_compose(members: list[dict], place: dict) -> tuple[str, str, str]:
    ws = sorted({m["device"]: m for m in members}.values(), key=lambda m: (m.get("extra") or {}).get("onset_ms") or m["start"])
    top = max(ws, key=lambda m: LEVEL_RANK.get((m.get("extra") or {}).get("level") or "", 0))
    x = top.get("extra") or {}
    level = (x.get("level") or "yellow").capitalize()
    kind = x.get("awareness") or "weather"
    first = _dt((ws[0].get("extra") or {}).get("onset_ms") or ws[0]["start"]).astimezone(TZ)
    last_ms = (ws[-1].get("extra") or {}).get("expires_ms")
    last = _dt(last_ms).astimezone(TZ) if last_ms else None
    if len(ws) == 1:
        when_ = x.get("hours") or day_time(first)
    elif last and first.month == last.month:
        when_ = f"{first.day} to {last.day} {last:%b}"
    else:
        when_ = f"{first:%d %b} to {last:%d %b}" if last else f"from {first:%d %b}"
    title = f"{level} {kind} warning, {when_}"
    area = x.get("area") or "Cyprus"
    story = (f"The Department of Meteorology's {level.lower()} {kind} warning for {area}"
             + (" (the island as one area, Nicosia included)" if area == "Cyprus" else "")
             + ", through Meteoalarm: in force " + "; ".join((m.get("extra") or {}).get("hours") or "" for m in ws) + ".")
    words = next(((m.get("extra") or {}).get("description") for m in reversed(ws) if (m.get("extra") or {}).get("description")), None)
    if words:
        story += f" It says: “{words.strip()}”"
    return title, worst(*(m["severity"] for m in ws)), story


def _warning_cleared(x: dict) -> str:
    if any((m.get("extra") or {}).get("withdrawn") for m in x["members"]):
        return "cleared: withdrawn from Meteoalarm's feed before its expiry"
    return "cleared: the warning expired"


# -- fire danger ---------------------------------------------------------------------------------


def fire(obs: Observed) -> list[Detection] | None:
    hs = _hazards(obs, "fire")
    if hs is None:
        return None
    days = sorted((h for h in hs if h.start <= obs.now), key=lambda h: h.start)
    by_day = {h.details.get("day"): h for h in days}
    out = []
    run: list[Hazard] = []

    def close(run: list[Hazard]) -> None:
        if not run:
            return
        last = run[-1]
        nxt = (last.start.astimezone(TZ).date() + timedelta(days=1)).isoformat()
        end = last.end
        if end is not None and (end > obs.now or (nxt not in by_day and obs.now - end < FIRE_GRACE)):
            end = None
        worst_day = max(run, key=F.fire_index)
        severity = worst(*(F.FIRE_SEVERITY.get(h.level or "", "low") for h in run))
        what = "; ".join(f"{F.FIRE_WORDS[h.level].lower()} on {h.start.astimezone(TZ):%a %d %b}" for h in run[-4:])
        ev = [Evidence(h.start, f"{F.FIRE_WORDS[h.level]} fire danger in the Nicosia district (fire weather index "
                                f"{F.FIRE_FWI[h.level]}; {round(100 * sum(v for k, v in (h.details.get('shares') or {}).items() if F.FIRE_CLASSES.index(k) >= F.fire_index(h)))}% "
                                f"of the district at that level or worse)", h.id, "event", value=float(F.fire_index(h)))
              for h in run[-6:]]
        if end is not None:
            ev.append(Evidence(end, "fire danger back under very high", FIRE_DEVICE))
        out.append(Detection(
            "fire_danger", "fire", FIRE_DEVICE, "fire_danger", run[0].start, end, severity,
            f"fire danger {what}", ev, source="effis", label="Nicosia district",
            lat=F.NICOSIA[0], lon=F.NICOSIA[1],
            extra={"days": [{"day": h.details.get("day"), "level": h.level,
                             "shares": h.details.get("shares")} for h in run],
                   "worst": worst_day.level, "worst_day": worst_day.details.get("day")}))

    for h in days:  # runs of days in a row at very high or worse
        if F.fire_index(h) < FIRE_FIRST:
            close(run)
            run = []
            continue
        if run and h.start.astimezone(TZ).date() - run[-1].start.astimezone(TZ).date() != timedelta(days=1):
            close(run)
            run = []
        run.append(h)
    close(run)
    return out


def _fire_link(a: dict, b: dict) -> bool:
    if a["device"] == b["device"]:
        return True
    day = 86_400_000
    a1, b1 = a.get("end") or float("inf"), b.get("end") or float("inf")
    return a["start"] <= b1 + day and b["start"] <= a1 + day


def _fire_place(members: list[dict]) -> dict:
    return _city_place("Nicosia district", "in the Nicosia district")


def _fire_compose(members: list[dict], place: dict) -> tuple[str, str, str]:
    days = [d for m in sorted(members, key=lambda m: m["start"]) for d in (m.get("extra") or {}).get("days") or []]
    days = list({d["day"]: d for d in days}.values())
    top = max(days, key=lambda d: F.FIRE_CLASSES.index(d["level"])) if days else {"level": "very_high", "day": None}
    word = F.FIRE_WORDS[top["level"]]
    when_ = ""
    if len(days) > 1:
        a, b = datetime.fromisoformat(days[0]["day"]), datetime.fromisoformat(days[-1]["day"])
        when_ = f", {a.day} to {b.day} {b:%b}" if a.month == b.month else f", {a:%d %b} to {b:%d %b}"
    elif days:
        when_ = f", {datetime.fromisoformat(days[0]['day']):%a %d %b}"
    title = f"{word} fire danger in the Nicosia district{when_}"
    parts = []
    for d in days[-5:]:
        sh = d.get("shares") or {}
        worse = round(100 * sum(v for k, v in sh.items() if F.FIRE_CLASSES.index(k) >= F.FIRE_CLASSES.index(d["level"])))
        parts.append(f"{F.FIRE_WORDS[d['level']].lower()} on {datetime.fromisoformat(d['day']):%a %d %b} "
                     f"({worse}% of the district)")
    story = ("EFFIS's fire weather index for the Nicosia district: " + "; ".join(parts)
             + ". It is a forecast of how readily fires would start and spread in that weather, not a fire.")
    return title, worst(*(m["severity"] for m in members)), story


def _fire_cleared(x: dict) -> str:
    return "cleared: fire danger back under very high"


# -- registration --------------------------------------------------------------------------------

D.register(quakes, "quake", observer=observe)
D.register(warnings, "weather_warning")
D.register(fire, "fire_danger")
register_family("quake", Family("Earthquake", _quake_link, _quake_place, _quake_compose, _quake_cleared))
register_family("warning", Family("Weather warning", _warning_link, _warning_place, _warning_compose,
                                  _warning_cleared))
register_family("fire", Family("Fire danger", _fire_link, _fire_place, _fire_compose, _fire_cleared))

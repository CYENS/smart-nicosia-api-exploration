"""Natural hazards: the three feeds' parsers on real past events, the service that reads and
records them (gently), the watch's incidents for them, the chat tool, the endpoints, the entities
and their sources. Offline: every payload is a fixture in tests/fixtures/hazards (see its
README), every request a fake.

The events: the 12 November 2025 M5.2 and M5.3 about 90 km south-west of Nicosia (3,491 and
4,478 felt reports), the quiet 30 days before 27 November 2026, Meteoalarm's Orange heat warnings
of 24 to 26 July 2025 and Yellow thunderstorm warning of 23 January 2026, and EFFIS's fire danger
of 23 July 2025 (extreme) and 27 November 2026 (high)."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chatbot import entities as E
from chatbot import sources
from chatbot.hazards import Hazards
from chatbot.hazards import feeds as F
from chatbot.hazards.service import from_memory
from chatbot.memory import Memory
from chatbot.routes import hazards as hazard_routes
from chatbot.routes import sources as source_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from chatbot.watch import Watcher
from chatbot.watch import detectors as D
from chatbot.watch import hazards as WH
from chatbot.watch.incidents import CATEGORIES, linked
from chatbot.web import WebError

FX = Path(__file__).resolve().parent / "fixtures" / "hazards"
TZ = ZoneInfo("Asia/Nicosia")
M52 = "eq-20251112_0000180"
M53 = "eq-20251112_0000274"


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def text(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


# -- the parsers ---------------------------------------------------------------------------------


def test_emsc_quakes_are_told_against_nicosia():
    qs = {q.id: q for q in F.parse_emsc(json.loads(text("emsc_2025-11-12.json")))}
    assert len(qs) == 156
    q = qs[M52]
    assert q.value == 5.2 and q.level == "mw" and q.kind == "earthquake" and q.end is None
    assert q.start == utc(2025, 11, 12, 9, 31, 25, 660000)
    assert q.details["distance_km"] == pytest.approx(91.2, abs=0.5)
    assert q.details["direction"] == "south-west" and q.details["district"] == "Paphos"
    assert q.title == "M5.2 earthquake in the Paphos district, 91 km south-west of Nicosia"
    assert F.quake_rules(q) == (True, "high", "M5.2 at 91 km")
    gsd = [x for x in qs.values() if x.details["auth"] == "GSD"]
    assert gsd and gsd[0].details["located_by"] == "the Cyprus Geological Survey Department"


def test_a_quiet_month_raises_nothing():
    qs = F.parse_emsc(json.loads(text("emsc_30d_2026-11-27.json")))
    assert len(qs) == 29
    assert not [q for q in qs if F.quake_rules(q)[0]]  # M3.2 at 106 km and smaller: no one paged
    near = min(qs, key=lambda q: q.details["distance_km"])
    assert near.details["distance_km"] < 20 and near.value < 2.5 and not F.quake_rules(near)[0]


def test_felt_reports_say_how_many_felt_it_near_nicosia():
    counts = F.parse_felt_counts(json.loads(text("emsc_felt_counts_2025-11-12.json")))
    assert counts == {"20251112_0000180": 3491, "20251112_0000274": 4478}
    reports = F.parse_felt_zip((FX / "emsc_felt_20251112_0000180.zip").read_bytes())
    felt = F.felt_summary(reports, counts["20251112_0000180"], utc(2025, 11, 12, 10))
    assert felt["count"] == 3491 and felt["located"] == 3491
    assert felt["near_nicosia"] == 817 and felt["near_intensity"] == 3 and felt["near_words"] == "weak"
    assert 0 < len(felt["cells"]) < 600 and sum(c["n"] for c in felt["cells"]) == 3491
    # Felt near the city raises even a quake the magnitude alone would not.
    small = F.Hazard("eq-x", "earthquake", "emsc", utc(2025, 1, 1), None, "M2.2", value=2.2,
                     details={"distance_km": 40.0, "felt": {"near_nicosia": 12}})
    assert F.quake_rules(small) == (True, "high", "12 felt reports within 25 km of Nicosia")


def test_meteoalarm_atom_warnings_from_real_past_feeds():
    heat = F.parse_meteoalarm_atom(text("meteoalarm_2025-07-25.xml"))
    assert [h.level for h in heat] == ["orange"] * 3
    first = min(heat, key=lambda h: h.start)
    assert first.id == "wx-cy-250723170828-maxtemp-000"
    assert first.title == "Orange high temperature warning for Cyprus"
    assert F.wall(first.start) == "2025-07-24T11:00" and F.wall(first.end) == "2025-07-24T16:59"
    assert first.details["covers_nicosia"] and first.details["emma_id"] == "CY001"
    assert first.details["awareness"] == "high temperature" and first.value == 3.0
    storm = F.parse_meteoalarm_atom(text("meteoalarm_2026-01-24.xml"))
    assert [(h.level, h.details["awareness"]) for h in storm] == [("yellow", "thunderstorm")]
    assert len(F.parse_meteoalarm_atom(text("meteoalarm_2024-08-13.xml"))) == 2
    assert F.parse_meteoalarm_atom(text("meteoalarm_empty_2026-11-27.xml")) == []


def test_meteoalarm_api_carries_the_warnings_own_words():
    ws = F.parse_meteoalarm_api(json.loads(text("meteoalarm_api_greece_2026-11-22.json")))
    assert [w.id for w in ws] == ["wx-gr-261122041000-100000003", "wx-gr-261122041000-100000015"]
    w = ws[0]
    assert w.level == "yellow" and w.details["awareness"] == "wind"
    assert w.details["description"].startswith("Locally gale force winds 8 beaufort")
    assert w.details["description_local"] and w.details["local_language"] == "el-GR"
    assert not w.details["covers_nicosia"] and w.lat is None  # Greece: not ours
    assert F.parse_meteoalarm_api(json.loads(text("meteoalarm_api_cyprus_empty.json"))) == []


def test_effis_fire_danger_over_the_nicosia_district():
    hot = F.parse_fire_png((FX / "effis_fwi_2025-07-23.png").read_bytes(), date(2025, 7, 23),
                           utc(2025, 7, 23, 6))
    assert hot.id == "fire-2025-07-23" and hot.level == "extreme" and hot.value == 4.0
    assert hot.title == "Extreme fire danger in the Nicosia district"
    assert hot.details["shares"]["extreme"] > 0.35 and hot.details["city_level"] == "very_extreme"
    assert F.unrle(hot.details["grid"]).__len__() == 250 * 125
    assert F.wall(hot.start) == "2025-07-23T00:00" and F.wall(hot.end) == "2025-07-24T00:00"
    mild = F.parse_fire_png((FX / "effis_fwi_2026-11-27.png").read_bytes(), date(2026, 11, 27),
                            utc(2026, 11, 26, 20))
    assert mild.level == "high" and mild.details["forecast"] is True


def test_where_reads_the_island():
    assert F.where(35.1725, 33.3615)["words"] == "under Nicosia's centre"
    w = F.where(35.10, 33.30)
    assert w["district"] == "Nicosia" and w["words"].endswith("of Nicosia's centre")
    sea = F.where(34.40, 33.40)  # south of the island
    assert sea["at_sea"] and sea["words"].startswith("off ")


# -- the service ---------------------------------------------------------------------------------


class Feeds:
    """The three feeds as fakes, answering from fixtures as they stood at the clock's moment."""

    def __init__(self, clock: list[datetime]):
        self.clock = clock
        self.calls: list[str] = []
        self.warnings = "meteoalarm_2025-07-25.xml"
        self.api_fails = True
        self.fire = {}
        self.emsc = "emsc_2025-11-12.json"

    def text(self, url, *, user_agent, params=None, timeout=30, accept=None):
        assert user_agent == Settings().user_agent
        self.calls.append(url.split("?")[0].rsplit("/", 1)[-1])
        if url.startswith(F.EMSC_URL):
            d = json.loads(text(self.emsc))
            d["features"] = [f for f in d["features"] if F.parse_time(f["properties"]["time"]) <= self.clock[0]]
            return json.dumps(d) if d["features"] else ""  # FDSN answers 204, no body, for none
        if url.startswith(F.FELT_URL):
            return text("emsc_felt_counts_2025-11-12.json")
        if url == F.METEOALARM_API:
            if self.api_fails:
                raise WebError("feeds.meteoalarm.org answered HTTP 503")
            return text("meteoalarm_api_cyprus_empty.json")
        if url == F.METEOALARM_ATOM:
            return text(self.warnings)
        raise AssertionError(url)

    def bytes(self, url, *, user_agent, timeout=60):
        self.calls.append("felt_zip" if url.startswith(F.FELT_URL) else "effis")
        if url.startswith(F.FELT_URL):
            return (FX / "emsc_felt_20251112_0000180.zip").read_bytes()
        day = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["TIME"]
        return (FX / self.fire.get(day, "effis_fwi_2026-11-27.png")).read_bytes()


@pytest.fixture
def clock():
    return [utc(2025, 11, 12, 10, 0)]


@pytest.fixture
def feeds(clock):
    return Feeds(clock)


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "twin.duckdb")


def hazards(memory, clock, feeds, tmp_path, **kw) -> Hazards:
    return Hazards(Settings(), memory, now=lambda: clock[0], fetch_text=feeds.text,
                   fetch_bytes=feeds.bytes, cache_dir=tmp_path / "cache", **kw)


def test_the_service_reads_records_and_replays(memory, clock, feeds, tmp_path):
    hz = hazards(memory, clock, feeds, tmp_path)
    changed = hz.refresh(force=True)
    assert M52 in changed
    # One request per feed, the felt counts once, the positions of the one felt quake that raises
    # an incident once, three fire danger images (today and the next two days).
    assert feeds.calls == ["query", "search", "felt_zip", "feeds-cyprus",
                           "meteoalarm-legacy-atom-cyprus", "effis", "effis", "effis"]
    q = hz.get(M52)
    assert q.details["felt"]["near_nicosia"] == 817 and q.first_seen == clock[0]
    # Recorded: a fresh service (a restart) has them from the memory alone.
    again = Hazards(Settings(), memory, now=lambda: clock[0], fetch_text=None, fetch_bytes=None)
    assert again.get(M52).details["felt"]["count"] == 3491
    assert {h.kind for h in from_memory(memory, utc(2025, 7, 1))} == {"earthquake", "warning", "fire"}
    # Not due again yet: no request.
    feeds.calls.clear()
    assert hz.refresh() == [] and feeds.calls == []
    # Five minutes on, while a page listens: EMSC again, the felt positions not (too soon).
    hz.listening = lambda: 1
    hz._next = {}
    clock[0] += timedelta(minutes=5)
    hz.refresh(only=("earthquakes",))
    assert feeds.calls == ["query", "search"]


def test_a_past_moment_reads_what_was_known_then(memory, clock, feeds, tmp_path):
    hz = hazards(memory, clock, feeds, tmp_path)
    clock[0] = utc(2025, 11, 12, 18, 0)
    hz.refresh(force=True)
    m = hz.moment(utc(2025, 11, 12, 10, 0), days=1)
    assert m["live"] is False and m["at"] == "2025-11-12T12:00"
    ids = [q["id"] for q in m["earthquakes"]]
    assert M52 in ids and M53 not in ids  # the M5.3 came at 14:23 UTC
    live = hz.moment(None, days=1)
    assert M53 in [q["id"] for q in live["earthquakes"]] and live["fire"]["level"] == "high"
    assert len(live["fire_ahead"]) == 2


def test_a_warning_that_leaves_the_feed_early_is_withdrawn(memory, clock, feeds, tmp_path):
    feeds.warnings = "meteoalarm_2024-08-13.xml"
    clock[0] = utc(2024, 8, 14, 9, 0)  # 12:00 in Nicosia: the 14 Aug warning is in force
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("warnings",))
    w = hz.get("wx-cy-240813170521-maxtemp-000")
    assert F.warning_in_force(w, clock[0]) and not w.details.get("withdrawn_at")
    feeds.warnings = "meteoalarm_empty_2026-11-27.xml"
    clock[0] += timedelta(hours=1)
    hz.refresh(force=True, only=("warnings",))
    w = hz.get("wx-cy-240813170521-maxtemp-000")
    assert w.end == clock[0] and w.details["withdrawn_at"] == "2024-08-14T13:00"
    assert hz.health["warnings"]["note"] == "no warning for Cyprus, from the Atom feed"


def test_a_failing_feed_says_so_and_keeps_what_it_had(memory, clock, feeds, tmp_path):
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("earthquakes",))
    assert hz.feed_state("earthquakes")[0] == "live"
    assert hz.feed_state("fire") == ("waiting", None, "read in the background a few seconds after the server starts")

    def down(*a, **k):
        raise WebError("www.seismicportal.eu unreachable: timed out")
    hz._text = down
    hz.refresh(force=True, only=("earthquakes",))
    state, _, note = hz.feed_state("earthquakes")
    assert state == "stale" and "timed out" in note and hz.get(M52) is not None


def test_a_past_days_fire_danger_is_read_once_and_kept(memory, clock, feeds, tmp_path):
    clock[0] = utc(2025, 7, 25, 9)
    feeds.fire = {"2025-07-23": "effis_fwi_2025-07-23.png"}
    hz = hazards(memory, clock, feeds, tmp_path)
    assert hz.fire_day(date(2025, 7, 23)).level == "extreme"
    assert hz.fire_day(date(2025, 7, 23)).level == "extreme"
    assert feeds.calls == ["effis"]
    assert (tmp_path / "cache" / "effis_fwi_2025-07-23.png").exists()
    assert hz.fire_day(date(2017, 6, 1)) is None  # before EFFIS's archive: never asked
    assert feeds.calls == ["effis"]


# -- the watch -----------------------------------------------------------------------------------


def watcher(memory, clock) -> Watcher:
    return Watcher(None, memory, now=lambda: clock[0])


def test_a_felt_quake_pages_and_its_aftershocks_join_it(memory, clock, feeds, tmp_path):
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True)
    w = watcher(memory, clock)
    loop = asyncio.new_event_loop()
    q = w.hub.subscribe(loop)
    changes = w.evaluate()
    new = next(c for c in changes if c["change"] == "new")["incident"]
    assert new["category"] == "Earthquake" and new["severity"] == "high" and new["active"]
    assert new["title"] == "M5.2 earthquake in the Paphos district, 91 km south-west of Nicosia, and 4 more"
    assert new["place"] == "In the Paphos district, 91 km south-west of Nicosia"
    assert new["kinds"] == {"earthquake": 5} and new["source"] == "emsc"
    loop.run_until_complete(asyncio.sleep(0))
    event = q.get_nowait()  # the page toasts a new high-severity incident
    assert event["change"] == "new" and event["incident"]["severity"] == "high"
    loop.close()
    d = w.book.detail(w.book.get(new["id"]), clock[0])
    assert "817 within 25 km of Nicosia, most calling it weak (intensity III)" in d["summary"]
    assert d["summary"].startswith("5 earthquakes in the Paphos district")
    # Three hours after the last quake it is over; the M5.3 later that day reopens it.
    clock[0] = utc(2025, 11, 12, 13, 30)
    assert any(c["change"] == "resolved" for c in w.evaluate())
    clock[0] = utc(2025, 11, 12, 14, 30)
    hz.refresh(force=True, only=("earthquakes",))
    changes = w.evaluate()
    assert [c["change"] for c in changes if c["change"] != "updated"] == ["reopened"]
    x = w.book.get(new["id"])
    assert x["title"].startswith("M5.3 earthquake in the Paphos district, 85 km west of Nicosia")
    assert len(w.book.query(status="all")) == 1


def test_a_quiet_month_opens_no_incident(memory, clock, feeds, tmp_path):
    feeds.emsc = "emsc_30d_2026-11-27.json"
    clock[0] = utc(2026, 11, 27, 19, 30)
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("earthquakes",))
    assert len(hz.all("earthquake")) == 29
    assert "search" not in feeds.calls or "felt_zip" not in feeds.calls
    assert watcher(memory, clock).evaluate() == []


def test_a_heatwaves_orange_warnings_are_one_high_incident(memory, clock, feeds, tmp_path):
    clock[0] = utc(2025, 7, 24, 9, 0)  # 12:00 in Nicosia, the first warning in force
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("warnings",))
    w = watcher(memory, clock)
    new = next(c for c in w.evaluate() if c["change"] == "new")["incident"]
    assert new["category"] == "Weather warning" and new["severity"] == "high"
    assert new["title"] == "Orange high temperature warning, Thu 24 Jul 11:00 to 16:59"
    assert new["place"] == "Cyprus, Nicosia included"
    assert (new["lat"], new["lon"]) == F.NICOSIA
    clock[0] = utc(2025, 7, 26, 9, 0)  # all three issued
    w.evaluate()
    x = w.book.detail(w.book.get(new["id"]), clock[0])
    assert x["title"] == "Orange high temperature warning, 24 to 26 Jul" and len(x["members"]) == 3
    assert "the island as one area, Nicosia included" in x["summary"]
    clock[0] = utc(2025, 7, 26, 15, 0)  # the last expired at 16:59 Nicosia time
    assert any(c["change"] == "resolved" for c in w.evaluate())
    assert w.book.get(new["id"])["actions"][-1]["note"] == "cleared: the warning expired"


def test_a_yellow_warning_is_in_the_inbox_but_pages_no_one(memory, clock, feeds, tmp_path):
    feeds.warnings = "meteoalarm_2026-01-24.xml"
    clock[0] = utc(2026, 1, 23, 21, 0)
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("warnings",))
    new = next(c for c in watcher(memory, clock).evaluate() if c["change"] == "new")["incident"]
    assert new["severity"] == "low" and new["title"].startswith("Yellow thunderstorm warning")


def test_extreme_fire_danger_days_are_one_incident_until_it_drops(memory, clock, feeds, tmp_path):
    clock[0] = utc(2025, 7, 23, 9)
    feeds.fire = {"2025-07-22": "effis_fwi_2025-07-23.png", "2025-07-23": "effis_fwi_2025-07-23.png"}
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("fire",))
    hz.fire_day(date(2025, 7, 22))
    w = watcher(memory, clock)
    new = next(c for c in w.evaluate() if c["change"] == "new")["incident"]
    assert new["category"] == "Fire danger" and new["severity"] == "high"
    assert new["title"] == "Extreme fire danger in the Nicosia district, 22 to 23 Jul"
    assert new["place"] == "Nicosia district"
    d = w.book.detail(w.book.get(new["id"]), clock[0])
    assert "not a fire" in d["summary"] and d["members"][0]["id"] == "fire-nicosia"
    # 24 July was read as high: the run ended at midnight, and the twin resolves it.
    clock[0] = utc(2025, 7, 24, 9)
    hz.refresh(force=True, only=("fire",))
    assert any(c["change"] == "resolved" for c in w.evaluate())
    assert w.book.get(new["id"])["actions"][-1]["note"] == "cleared: fire danger back under very high"


def test_high_fire_danger_alone_raises_nothing(memory, clock, feeds, tmp_path):
    clock[0] = utc(2026, 11, 27, 9)
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True, only=("fire",))
    assert watcher(memory, clock).evaluate() == []


def test_the_families_are_registered_beside_the_devices():
    assert {"quake", "weather_warning", "fire_danger"} <= set(D.RULES)
    assert WH.observe in D.OBSERVERS
    assert CATEGORIES["quake"] == "Earthquake" and CATEGORIES["fire"] == "Fire danger"
    a = {"family": "quake", "device": "eq-a", "start": 0, "lat": 34.80, "lon": 32.47}
    b = {**a, "device": "eq-b", "start": 3_600_000, "lat": 34.89, "lon": 32.49}
    far = {**b, "device": "eq-c", "lat": 35.6, "lon": 34.5}
    assert linked(a, b) and not linked(a, far)


def test_an_unreadable_memory_ends_nothing():
    obs = D.Observed(utc(2025, 1, 1), {}, {}, {})
    WH.observe(None, obs)
    assert WH.quakes(obs) is None and WH.warnings(obs) is None and WH.fire(obs) is None


# -- the tool, the endpoints, the entities, the sources ----------------------------------------------


@pytest.fixture
def ready(memory, clock, feeds, tmp_path):
    clock[0] = utc(2025, 11, 12, 18, 0)
    hz = hazards(memory, clock, feeds, tmp_path)
    hz.refresh(force=True)
    w = watcher(memory, clock)
    w.hazards = hz
    w.evaluate()
    return hz, w


def ctx_for(w, clock) -> ToolContext:
    city = SimpleNamespace(now=lambda: clock[0])
    return ToolContext(Settings(), city, None, None, SessionStore(), lambda e: None, "c1", watcher=w)


def test_get_hazards_answers_with_a_layer_and_rows(ready, clock):
    hz, w = ready
    ctx = ctx_for(w, clock)
    res, err = run_tool(ctx, "get_hazards", {"kind": "earthquakes", "days": 1, "notable_only": True})
    assert not err
    top = res["earthquakes"]["strongest_first"][0]
    assert top["id"] == M53 and top["magnitude"] == 5.3 and top["severity_here"] == "high"
    assert top["incident"] == "INC-1" and top["felt_reports"] == 4478
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["hazards"]["at"] and layer["meta"]["sources"][:3] == ["emsc", "meteoalarm", "effis"]
    kinds = {f["properties"]["kind"] for f in layer["features"]}
    assert kinds == {"earthquake", "felt"}  # earthquakes only: no warning area, no fire
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert all(r["raises_incident"] for r in rows) and ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    res, err = run_tool(ctx, "get_hazards", {"kind": "warnings"})
    assert not err and res["warnings"] == [] and "no weather warning for Cyprus" in res["warnings_note"]
    res, err = run_tool(ctx, "get_hazards", {"kind": "fire"})
    assert not err and res["fire_danger"][0]["level"] == "High"


def test_the_endpoints_follow_the_clock(ready, clock):
    hz, w = ready
    agent = SimpleNamespace(hazards=hz, watcher=w, city=SimpleNamespace(now=lambda: clock[0]))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=agent)))
    now = hazard_routes.hazards(req, at=None, days=30)
    assert now["live"] and any(f["properties"].get("id") == M53 for f in now["features"])
    assert now["incidents"][M52] == "INC-1" and "Earthquake, felt strongly here" in now["legend"]
    then = hazard_routes.hazards(req, at="2025-11-12T12:00", days=1)
    assert not then["live"] and M53 not in [q["id"] for q in then["earthquakes"]]
    marks = hazard_routes.marks(req, days=31)["marks"]
    assert {m["kind"] for m in marks} == {"earthquake"} and marks[0]["t"].startswith("2025-11-12")
    items = hazard_routes.items(req, ids=f"{M52},fire-nicosia")
    assert [q["id"] for q in items["earthquakes"]] == [M52] and items["fire"]["kind"] == "fire"


def test_hazards_open_as_entities(ready):
    hz, w = ready
    ents = E.Entities(None)
    ents.watcher = w
    q = ents.get(M52)
    assert q["kind"] == "earthquake" and q["state"]["label"] == "Felt in Nicosia"
    assert {r["key"]: r["value"] for r in q["readings"]}["felt_near"] == 817
    assert q["relations"][0]["id"] == "INC-1" and q["sources"][0]["source"] == "emsc"
    fire = ents.get("fire-nicosia")
    assert fire["kind"] == "fire_danger" and fire["state"]["label"] == "High"


def test_every_hazard_source_is_listed_and_probed(ready):
    hz, w = ready
    assert {"emsc", "meteoalarm", "effis", "geoboundaries"} <= set(sources.BY_ID)
    assert sources.TOOL_SOURCES["get_hazards"][:3] == ("emsc", "meteoalarm", "effis")
    agent = SimpleNamespace(hazards=hz)
    for sid in ("emsc", "meteoalarm", "effis"):
        assert source_routes.PROBES[sid](agent)["state"] == "live"

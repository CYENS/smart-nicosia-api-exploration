"""Road reports: the three feeds parsed from fixtures (Waze alerts and the Public Works
Department's events as captured on Fri 27 Nov 2026 at 21:30; Waze's jam lines answered 503 then,
so their shape here is assumed), one shared and throttled fetch, the twin's memory replaying
them for the city clock, the inspector's kind, the chat tool, the page's endpoint, the
freshness probes and the watch's detectors. Offline."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chatbot import server
from chatbot.entities import Entities, EntityNotFound
from chatbot.memory import Memory
from chatbot.mobility import reports as R
from chatbot.mobility.reports import (EVENTS_URL, WAZE_ALERTS_URL, WAZE_TRAFFIC_URL, RoadReport, RoadReports,
                                      age_words, count_words, parse_events, parse_waze_alerts,
                                      parse_waze_traffic)
from chatbot.routes import reports as report_routes
from chatbot.routes import sources as source_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, run_tool
from chatbot.watch import detectors as D
from chatbot.watch.incidents import Book

from conftest import FIXTURES

TZ = ZoneInfo("Asia/Nicosia")
NOW = datetime(2026, 11, 27, 21, 36, tzinfo=TZ).astimezone(timezone.utc)  # just after the capture
DIR = FIXTURES / "road_reports"
ALERTS = (DIR / "waze_alerts.xml").read_text(encoding="utf-8")
EVENTS = (DIR / "situations.xml").read_text(encoding="utf-8")
DOWN = (DIR / "waze_traffic_503.html").read_text(encoding="utf-8")

# Waze's jam lines as the alerts are written (the real feed answered 503 when this was built).
JAMS = """<?xml version='1.0' encoding='UTF-8'?>
<d2LogicalModel xmlns="https://datex2.eu/schema/3/exchangeInformation" xmlns:common="https://datex2.eu/schema/3/common"
 xmlns:location="https://datex2.eu/schema/3/locationReferencing" xmlns:traffic="https://datex2.eu/schema/3/traffic">
  <traffic:trafficElementPublication>
    <ns1:publicationTime xmlns:ns1="https://datex2.eu/schema/3/publication">2026-11-27T21:35:00+02:00</ns1:publicationTime>
    {elements}
  </traffic:trafficElementPublication>
</d2LogicalModel>"""
JAM = """<traffic:trafficElement><common:id>{uid}</common:id><common:generalPublicComment>
  <common:comment><common:commentType>level</common:commentType><common:value>{level}</common:value></common:comment>
  <common:comment><common:commentType>speedKMH</common:commentType><common:value>4.5</common:value></common:comment>
  <common:comment><common:commentType>delay</common:commentType><common:value>240</common:value></common:comment>
  <common:comment><common:commentType>length</common:commentType><common:value>610</common:value></common:comment>
  <common:comment><common:commentType>street</common:commentType><common:value>Archiepiskopou Makariou III</common:value></common:comment>
  <common:comment><common:commentType>report_time</common:commentType><common:value>2026-11-27 21:20:00</common:value></common:comment>
</common:generalPublicComment><location:location>
  <location:pointCoordinates><common:latitude>35.1600</common:latitude><common:longitude>33.3600</common:longitude></location:pointCoordinates>
  <location:pointCoordinates><common:latitude>35.1630</common:latitude><common:longitude>33.3620</common:longitude></location:pointCoordinates>
  <location:pointCoordinates><common:latitude>35.1660</common:latitude><common:longitude>33.3630</common:longitude></location:pointCoordinates>
</location:location></traffic:trafficElement>"""


def jams_xml(*levels: int) -> str:
    return JAMS.format(elements="".join(JAM.format(uid=f"{i:08x}-0000-4000-8000-000000000000", level=lv)
                                        for i, lv in enumerate(levels, start=1)))


def with_accident(xml: str, lat=35.1605, lon=33.3604) -> str:
    """The captured alerts plus an accident on Makariou (none was reported at capture time)."""
    el = f"""<traffic:trafficElement><common:id>acc1d3e7-1111-4222-8333-944444444444</common:id>
      <common:generalPublicComment>
        <common:comment><common:commentType>type</common:commentType><common:value>ACCIDENT</common:value></common:comment>
        <common:comment><common:commentType>subtype</common:commentType><common:value>ACCIDENT_MAJOR</common:value></common:comment>
        <common:comment><common:commentType>street</common:commentType><common:value>Archiepiskopou Makariou III</common:value></common:comment>
        <common:comment><common:commentType>report_time</common:commentType><common:value>2026-11-27 21:30:00</common:value></common:comment>
      </common:generalPublicComment>
      <location:location><location:pointByCoordinates><location:pointCoordinates>
        <common:latitude>{lat}</common:latitude><common:longitude>{lon}</common:longitude>
      </location:pointCoordinates></location:pointByCoordinates></location:location></traffic:trafficElement>"""
    return xml.replace("</traffic:trafficElementPublication>", el + "</traffic:trafficElementPublication>")


class Feeds:
    """The three feeds from fixtures; `fail` makes a URL raise. Counts what was asked."""

    def __init__(self, alerts=ALERTS, events=EVENTS, jams=DOWN):
        self.files = {WAZE_ALERTS_URL: alerts, EVENTS_URL: events, WAZE_TRAFFIC_URL: jams}
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        return self.files[url]


def reports(now=NOW, memory=None, feeds=None, roads=None):
    feeds = feeds or Feeds()
    clock = {"now": now}
    r = RoadReports(feeds, memory=memory, now=lambda: clock["now"], roads=roads)
    return r, feeds, clock


# -- the feeds -------------------------------------------------------------------------------------


def test_waze_alerts_become_reports_and_a_closure_is_one_report_however_many_points():
    p = parse_waze_alerts(ALERTS)
    assert p.total == 51 and p.newest == datetime(2026, 11, 27, 21, 23, 55, tzinfo=TZ)
    assert all(R.in_area(r.lat, r.lon) for r in p.reports)  # Paphos and Limassol are left out
    by = R.counts(p.reports)
    assert by == {"closure": 12, "jam": 3, "hazard": 7}  # 35 closure points, 12 closures
    karolou = next(r for r in p.reports if r.street == "Prigkipos Karolou")
    assert karolou.kind == "closure" and karolou.details["points"] == 8
    assert karolou.line and len(karolou.line) == 8 and 400 < karolou.details["length_m"] < 500
    assert karolou.since == datetime(2026, 7, 3, 22, 5, 5, tzinfo=TZ)  # local time, as written
    jam = next(r for r in p.reports if r.street == "Laerti")
    assert (jam.kind, jam.label, jam.id) == ("jam", "Standstill traffic", "report-waze-9f75bbe4")
    assert next(r for r in p.reports if r.street == "Kavalas").label == "Pothole"
    # The same feed gives the same ids: the first point of a closure names it.
    assert {r.id for r in parse_waze_alerts(ALERTS).reports} == {r.id for r in p.reports}


def test_the_operators_events_keep_their_dates_and_their_greek(monkeypatch):
    p = parse_events(EVENTS)
    assert p.total == 19 and len(p.reports) == 2  # two of the island's events are in Nicosia
    ammochostou = next(r for r in p.reports if r.kind == "diversion")
    assert ammochostou.label == "Diversion in operation" and "Ammochostou" in ammochostou.text
    assert ammochostou.since == datetime(2026, 10, 15, 8, 0, tzinfo=TZ)
    assert ammochostou.until == datetime(2027, 2, 15, 16, 0, tzinfo=TZ)
    works = next(r for r in p.reports if r.kind == "roadworks")
    assert works.id == "report-works-639313660076011442" and works.label == "Construction work"
    # Over the island: a Greek record and its English twin are one report, the Greek kept.
    monkeypatch.setattr(R, "AREA", (32.0, 34.5, 34.6, 35.7))
    island = parse_events(EVENTS).reports
    wall = next(r for r in island if r.id == "report-works-639196150036830249")
    assert R.GREEK.search(wall.text) and "retaining wall" in wall.details["text_en"]
    assert not any(r.id == "report-works-639219487363505114" for r in island)
    # Works and a diversion at one roundabout are two reports, not a translation.
    assert sum(1 for r in island if "Faneromenis" in (r.text or "") and "pavements along" in (r.text or "")) >= 2
    assert works.status(NOW) == "active"
    planned = next(r for r in island if r.label == "Road marking work")
    assert planned.status(NOW) == "planned"


def test_jam_lines_read_defensively_and_an_error_page_is_no_report():
    p = parse_waze_traffic(jams_xml(1, 4, 5))
    assert p.total == 3 and len(p.reports) == 2  # light traffic is no news
    standstill, closed = p.reports
    assert standstill.kind == "jam" and standstill.label == "Standstill traffic"
    assert standstill.line and len(standstill.line) == 3 and standstill.details["delay_s"] == 240
    assert standstill.id.startswith("report-jam-") and closed.kind == "closure"
    r, feeds, _ = reports()
    st = r.feed("waze_traffic")
    assert st.parsed is None and "readable DATEX II" in st.error


def test_words_for_ages_and_counts():
    assert age_words(NOW - timedelta(minutes=12), NOW) == "12 min ago"
    assert age_words(NOW - timedelta(hours=3), NOW) == "3 h ago"
    assert age_words(NOW - timedelta(days=1, hours=2), NOW) == "yesterday"
    assert age_words(datetime(2026, 7, 3, 22, 5, tzinfo=TZ), NOW) == "since 3 Jul"
    assert age_words(datetime(2025, 7, 3, tzinfo=TZ), NOW) == "since 3 Jul 2025"
    assert count_words({"jam": 3, "closure": 1}) == "1 closure and 3 jams"
    assert count_words({"roadworks": 1}) == "1 place with roadworks"


# -- one fetch for everyone, and what a failure leaves --------------------------------------------


def test_every_reader_shares_one_fetch_and_a_failure_keeps_the_last_copy(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(R.time, "time", lambda: t["now"])
    r, feeds, _ = reports()
    now = r.current()
    assert now["status"] == "live" and len(now["reports"]) == 24
    assert now["feeds"]["waze_traffic"]["state"] == "unavailable"
    r.current()
    assert feeds.calls.count(WAZE_ALERTS_URL) == 1 and feeds.calls.count(EVENTS_URL) == 1
    t["now"] += R.ALERTS_TTL_S + 1  # alerts are due again, the operators' events aren't
    feeds.fail[WAZE_ALERTS_URL] = OSError("timed out")
    later = r.current()
    assert feeds.calls.count(WAZE_ALERTS_URL) == 2 and feeds.calls.count(EVENTS_URL) == 1
    assert later["feeds"]["waze_alerts"]["state"] == "stale" and len(later["reports"]) == 24
    t["now"] += R.KEEP_STALE_S["waze_alerts"] + 1
    gone = r.current()
    assert gone["feeds"]["waze_alerts"]["state"] == "unavailable"
    assert {x.source for x in gone["reports"]} == {"traffic_events"}
    # The jam lines are asked again only every 15 minutes while they fail.
    assert feeds.calls.count(WAZE_TRAFFIC_URL) == 2


def test_a_reader_during_the_first_read_waits_for_it():
    """The watch and a page asking at once: the second waits for the first's fetch, rather than
    taking "not read yet" for the answer (which the page would then show and keep)."""
    import threading

    started, release = threading.Event(), threading.Event()
    feeds = Feeds()

    def slow(url):
        if url == WAZE_ALERTS_URL:
            started.set()
            release.wait(5)
        return feeds(url)

    r = RoadReports(slow, now=lambda: NOW)
    first = threading.Thread(target=r.feed, args=("waze_alerts",))
    first.start()
    assert started.wait(5)
    got = {}
    second = threading.Thread(target=lambda: got.setdefault("st", r.feed("waze_alerts")))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert got["st"].parsed is not None and len(got["st"].parsed.reports) == 22
    assert feeds.calls.count(WAZE_ALERTS_URL) == 1


def test_a_feed_that_answers_with_old_news_is_stale(monkeypatch):
    # Published just now (it answers 200), but its newest report is 9 hours old.
    later = ALERTS.replace("2026-11-27T21:35:53.505339+02:00", "2026-11-28T06:35:00+02:00")
    r, _, clock = reports(now=NOW + timedelta(hours=9), feeds=Feeds(alerts=later))
    st = r.feed("waze_alerts")
    assert st.fresh_problem("waze_alerts", clock["now"]) == "nothing new in it since 27 Nov 21:23"
    agent = SimpleNamespace(mobility=SimpleNamespace(reports=r))
    probe = source_routes.PROBES["waze_alerts"](agent)
    assert probe["state"] == "stale" and probe["note"].startswith("nothing new in it since 27 Nov 21:23")
    cached, _, clock = reports(now=NOW + timedelta(hours=9))  # a copy published 9 hours ago
    assert cached.feed("waze_alerts").fresh_problem("waze_alerts", clock["now"]) ==         "the feed was last published at 27 Nov 21:35"
    fresh, _, _ = reports()
    fresh.feed("waze_alerts")
    ok = source_routes.PROBES["waze_alerts"](SimpleNamespace(mobility=SimpleNamespace(reports=fresh)))
    assert ok["state"] == "live" and "22 in Greater Nicosia" in ok["note"]
    waiting = source_routes.PROBES["traffic_events"](SimpleNamespace(mobility=SimpleNamespace(reports=fresh)))
    assert waiting["state"] == "waiting"


# -- the twin's memory and the city clock -----------------------------------------------------------


def test_the_memory_replays_reports_and_a_moment_nobody_watched_is_unknown(tmp_path, monkeypatch):
    mem = Memory(tmp_path / "twin.duckdb")
    t = {"now": NOW.timestamp()}
    monkeypatch.setattr(R.time, "time", lambda: t["now"])
    r, feeds, clock = reports(memory=mem)
    r.current()
    assert mem.recorder.flush()
    # Three minutes later the Laerti jam has cleared.
    t["now"] += R.ALERTS_TTL_S + 1
    clock["now"] = NOW + timedelta(seconds=R.ALERTS_TTL_S + 1)
    feeds.files[WAZE_ALERTS_URL] = ALERTS.replace("<common:id>9f75bbe4", "<common:id>gone-9f75bbe4").replace(
        "Laerti", "Laerti (gone)")
    r.current()
    assert mem.recorder.flush()
    with mem.reading() as con:
        assert con.execute("SELECT count(*) FROM road_reports").fetchone()[0] == 25
    then = r.at(NOW)
    assert then["status"] == "recorded" and then["feeds"]["waze_alerts"]["state"] == "recorded"
    assert any(x.id == "report-waze-9f75bbe4" for x in then["reports"])
    karolou = next(x for x in then["reports"] if x.street == "Prigkipos Karolou")
    assert karolou.line and len(karolou.line) == 8 and karolou.first_seen is not None
    after = r.at(NOW + timedelta(minutes=20))
    assert not any(x.id == "report-waze-9f75bbe4" for x in after["reports"])
    before = r.at(NOW - timedelta(hours=2))
    assert before["status"] == "unknown" and before["reports"] == []
    assert r.at(NOW + timedelta(hours=3))["status"] == "unknown"  # coverage ends 30 min after the last read


def test_an_old_memory_without_the_table_is_unknown_not_broken(tmp_path):
    r, _, _ = reports(memory=Memory(tmp_path / "missing.duckdb"))
    assert r.at(NOW - timedelta(hours=1))["status"] == "unknown"
    assert r.from_memory("report-waze-9f75bbe4") is None


# -- the inspector -----------------------------------------------------------------------------------


def test_a_report_is_an_entity_on_its_road_and_beside_its_neighbours(tmp_path, monkeypatch):
    from test_mobility import make_mobility

    m, fetch = make_mobility(tmp_path, now=NOW)
    fetch.files.update({k: v.encode() for k, v in Feeds().files.items()})
    seg = m.roads.segments()[0]
    on_road = seg.coords[len(seg.coords) // 2]
    fetch.files[WAZE_ALERTS_URL] = with_accident(ALERTS, lat=on_road[1], lon=on_road[0]).encode()
    ents = Entities(None, mobility=m)
    acc = ents.get("report-waze-acc1d3e7")
    assert acc["kind"] == "road_report" and acc["name"] == "Major accident on Archiepiskopou Makariou III"
    assert acc["state"]["tone"] == "alarm" and acc["state"]["label"] == "Major accident, 6 min ago"
    assert any(x["kind"] == "road_segment" and x["id"] == seg.id for x in acc["relations"])
    closure = ents.get("report-waze-889cf9c5")
    assert closure["name"] == "Road closed: Prigkipos Karolou" and closure["geometry"]["type"] == "LineString"
    assert closure["state"]["label"] == "Road closed, since 3 Jul" and closure["state"]["tone"] == "warn"
    assert {"label": "Closed points", "value": 8} in closure["attributes"]
    georgiou = ents.get("report-waze-b21696fe")  # beside the closure on Archiepiskopou Kyprianou
    assert any(x["id"] == "report-waze-1d76c05a" for x in georgiou["relations"])
    works = ents.get("report-works-639274141179895317")
    assert works["state"]["label"] == "Diversion in operation, until 15 Feb 2027"
    assert works["sources"][0]["source"] == "traffic_events"
    with pytest.raises(EntityNotFound, match="left its feed"):
        ents.get("report-waze-00000000")
    hits = ents.search("Karolou closed")  # a bus stop on the street is found too
    assert any(h["id"] == "report-waze-889cf9c5" and h["group"] == "Road reports" for h in hits)


# -- the chat tool -------------------------------------------------------------------------------------


class Router:
    def route(self, stops, mode, prefer, avoid=None):
        (a_lat, a_lon), (b_lat, b_lon) = stops
        return {"distance_km": 4.2, "duration_min": 9, "legs": [],
                "coordinates": [[a_lon, a_lat], [33.3604, 35.1605], [b_lon, b_lat]]}


def ctx_for(mobility, now=NOW):
    city = SimpleNamespace(now=lambda: now)
    places = SimpleNamespace(ready=False, geocode=lambda text, limit=3: [
        {"name": "Nicosia General Hospital", "lat": 35.1237, "lon": 33.3787}] if "hospital" in text.lower()
        else [{"name": "Eleftheria Square", "lat": 35.16855, "lon": 33.36148}])
    return ToolContext(Settings(), city, places, Router(), SessionStore(), lambda e: None, "c1",
                       mobility=mobility)


def test_get_road_reports_answers_with_a_layer_and_linked_rows():
    r, _, _ = reports(feeds=Feeds(alerts=with_accident(ALERTS)))
    ctx = ctx_for(SimpleNamespace(reports=r))
    res, err = run_tool(ctx, "get_road_reports", {})
    assert not err and res["counts"] == {"accident": 1, "closure": 12, "jam": 3, "hazard": 7, "roadworks": 1,
                                         "diversion": 1}
    assert res["reports"][0]["kind"] == "accident"  # the loudest first
    assert res["older_than_a_week"] >= 12 and "1 accident" in res["reported_last_30_min"]
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["live"] == {"kind": "road_reports", "at": None}
    assert set(layer["meta"]["sources"]) == {"waze_alerts", "traffic_events", "waze_traffic"}
    assert any(f["geometry"]["type"] == "LineString" for f in layer["features"])  # closed stretches
    data = ctx.store.datasets[res["data_ref"]]
    assert data["link"] == {"key": "id"} and {row["id"] for row in data["rows"]} <= {
        f["properties"]["id"] for f in layer["features"]}
    centre, err = run_tool(ctx, "get_road_reports", {"area": "centre", "kinds": ["closure"]})
    assert not err and set(centre["counts"]) == {"closure"} and centre["where"].startswith("in the centre")
    jams, err = run_tool(ctx, "get_road_reports", {"kinds": ["jam"], "recent_minutes": 60})
    assert not err and jams["counts"] == {"jam": 3}
    drive, err = run_tool(ctx, "get_road_reports", {"from_place": "Eleftheria Square",
                                                     "to_place": "the general hospital"})
    assert not err and drive["counts"] == {"accident": 1} and "drive from Eleftheria Square" in drive["where"]
    route = ctx.store.layers[drive["layer_ref"]]["features"][0]
    assert route["properties"]["part"] == "route"
    nobody, err = run_tool(ctx, "get_road_reports", {"to_place": "the general hospital"})
    assert err and "location" in nobody["error"]


def test_get_road_reports_at_a_moment_nobody_recorded_says_so(tmp_path):
    r, _, _ = reports(memory=Memory(tmp_path / "twin.duckdb"))
    ctx = ctx_for(SimpleNamespace(reports=r))
    res, err = run_tool(ctx, "get_road_reports", {"at": "2026-11-27T08:00"})
    assert err and "holds no road reports" in res["error"]


# -- the page's endpoint ---------------------------------------------------------------------------------


def test_the_endpoint_draws_every_kind_with_a_summary_for_the_card(monkeypatch):
    r, _, _ = reports()
    agent = SimpleNamespace(settings=Settings(), city=SimpleNamespace(now=lambda: NOW),
                            mobility=SimpleNamespace(reports=r))
    monkeypatch.setattr(server.app.state, "agent", agent, raising=False)
    body = json.loads(report_routes.road_reports(SimpleNamespace(app=server.app), at=None).body)
    meta = body["meta"]
    assert meta["status"] == "live" and meta["counts"]["closure"] == 12
    assert meta["recent"] == {"jam": 2, "hazard": 1}  # reported in the last 30 minutes
    assert meta["recent_words"] == "2 jams and 1 hazard"
    assert meta["newest_jam"]["street"] == "E1003 - Dimokratias Ave" and meta["long_closures"] == 12
    assert meta["list"][0]["kind"] == "jam" and not meta["list"][0]["old"]  # news before long closures
    marks = [f for f in body["features"] if f["geometry"]["type"] == "Point"]
    assert len(marks) == 24 and {f["properties"]["entity_kind"] for f in marks} == {"road_report"}
    assert meta["feeds"]["waze_traffic"]["state"] == "unavailable"
    past = json.loads(report_routes.road_reports(SimpleNamespace(app=server.app), at="2026-11-27T08:00").body)
    assert past["meta"]["status"] == "unavailable" and past["features"] == []  # this agent's memory is off


# -- the watch ---------------------------------------------------------------------------------------------


def observed_with(rows: list[RoadReport], read_at: datetime, now: datetime) -> D.Observed:
    obs = D.Observed(now, {}, {}, {})
    obs.road_reports = rows
    obs.road_watched = {"waze_alerts": read_at, "traffic_events": read_at, "waze_traffic": read_at}
    return obs


def seen(r: RoadReport, first: datetime, last: datetime) -> RoadReport:
    r.first_seen, r.last_seen = first, last
    return r


def test_old_closures_never_flood_the_inbox_but_new_ones_and_jams_come_in():
    p = parse_waze_alerts(with_accident(ALERTS))
    rows = [seen(r, NOW, NOW) for r in p.reports]
    obs = observed_with(rows, NOW, NOW)
    assert D.road_closures(obs) == []  # twelve closures from July to October: met already old
    jams = D.road_jams(obs)
    assert {d.extra["kind"] for d in jams} == {"jam", "accident"}
    acc = next(d for d in jams if d.extra["kind"] == "accident")
    assert acc.severity == "high" and acc.what == "major accident reported" and acc.end is None
    fresh = RoadReport("report-waze-0c0c0c0c", "waze_alerts", "closure", "Road closed", "Lidras", "ROAD_CLOSED",
                       35.1732, 33.3610, since=NOW - timedelta(minutes=40), details={"points": 3})
    found = D.road_closures(observed_with([*rows, seen(fresh, NOW - timedelta(minutes=35), NOW)], NOW, NOW))
    assert [d.device for d in found] == ["report-waze-0c0c0c0c"] and found[0].severity == "medium"
    # It leaves the feed while the twin reads it: over. Unread, a closure stands; a jam doesn't.
    gone = D.road_closures(observed_with([seen(fresh, NOW - timedelta(minutes=35), NOW)], NOW + timedelta(minutes=30),
                                         NOW + timedelta(minutes=30)))
    assert gone[0].end == NOW and gone[0].evidence[-1].text == "no longer in the feed"
    unread = D.road_closures(observed_with([fresh], NOW, NOW + timedelta(hours=2)))
    assert unread[0].end is None
    jam_unread = D.road_jams(observed_with(rows, NOW, NOW + timedelta(hours=2)))
    assert all(d.end == NOW for d in jam_unread)


def test_an_accident_and_the_jams_behind_it_are_one_incident():
    p = parse_waze_alerts(with_accident(ALERTS, lat=35.1781, lon=33.3236))  # beside the Laerti jams
    obs = observed_with([seen(r, NOW, NOW) for r in p.reports], NOW, NOW)
    book = Book(None, lambda: NOW)
    changes = book.update(D.road_jams(obs), NOW)
    titles = sorted(c["incident"]["title"] for c in changes if c["change"] == "new")
    assert any(t.startswith("Accident and 3 jams") for t in titles), titles
    one = next(x for x in book.incidents.values() if x["title"].startswith("Accident"))
    assert one["severity"] == "high" and one["family"] == "roads"


def test_the_watch_reads_the_road_reports_from_the_memory(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb")
    r, _, _ = reports(memory=mem)
    r.current()
    assert mem.recorder.flush()
    obs = D.observe(mem, None, NOW + timedelta(minutes=1))
    assert len(obs.road_reports) == 15 and set(obs.road_watched) == {"waze_alerts", "traffic_events"}
    found, ran = D.detect(obs)
    assert {"road_closure", "road_jam"} <= ran
    assert {d.rule for d in found} == {"road_jam"}  # the jams are news; the closures are months old


def test_the_page_knows_the_tool_and_report_ids():
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "chatbot" / "web" / "src"
    chat = (web / "lib" / "chat.ts").read_text(encoding="utf-8")
    assert "get_road_reports:" in chat
    entities = (web / "lib" / "entities.ts").read_text(encoding="utf-8")
    assert "report-(?:waze-[0-9a-f]{8}|jam-[0-9a-f]{8}|works-\\d{1,20})" in entities

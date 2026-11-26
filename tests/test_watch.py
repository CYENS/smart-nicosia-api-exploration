"""The twin's watch: every detector on fixtures, the grouping into incidents, their history of
actions and its storage, watches (proposed, saved by a click, fired once per crossing), the
live stream, the endpoints and the three chat tools. Offline: readings are synthetic nights and
days around Wed 25 Nov 2026, and the city API is a fake."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from collector.interpret import Interpretation

from chatbot.city import CityData, Device, DeviceState
from chatbot.memory import Memory
from chatbot.routes import watch as watch_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import TOOL_DEFS, ToolContext, run_tool
from chatbot.watch import Watcher
from chatbot.watch import detectors as D
from chatbot.watch.detectors import Observed, Place
from chatbot.watch.incidents import TWIN, ActionError, Book, components, linked
from chatbot.watch.watches import Rule, WatchError, Watches

from conftest import ms

TZ = ZoneInfo("Asia/Nicosia")
NOW = datetime(2026, 11, 25, 23, 45, tzinfo=TZ).astimezone(timezone.utc)  # Wed, night
NOON = datetime(2026, 11, 25, 12, 0, tzinfo=TZ)


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ).astimezone(timezone.utc)


def every(start, end, step):
    t = start
    while t <= end:
        yield t
        t += step


# Three lamps on Mnasiadou a few metres apart, one on Mnasiadou further up, one on
# Stasikratous, and the other devices the detectors read.
PLACES = {
    "YL1": ("Lighting", 35.1700, 33.3600, "Mnasiadou"),
    "YL2": ("Lighting", 35.1701, 33.3602, "Mnasiadou"),
    "YL3": ("Lighting", 35.1702, 33.3604, "Mnasiadou"),
    "YL4": ("Lighting", 35.1720, 33.3640, "Mnasiadou"),
    "YL5": ("Lighting", 35.1650, 33.3700, "Stasikratous"),
    "YL6": ("Lighting", 35.1703, 33.3606, "Mnasiadou"),
    "YP1": ("Parking", 35.1660, 33.3640, None),
    "YP2": ("Parking", 35.1661, 33.3641, None),
    "YP3": ("Parking", 35.1900, 33.3800, None),
    "YW1": ("Wastebin", 35.1662, 33.3642, None),
    "YW2": ("Wastebin", 35.1800, 33.3700, None),
    "a1": ("smartsense", 35.1600, 33.3500, None),
}


def lamp_nights(stop: datetime | None = None, watts: float = 54.0, start=NOW - timedelta(hours=30)):
    """Hourly reports at :40 while dark (17:40 to 05:40), lit at full level, until `stop`."""
    out = []
    for t in every(start.replace(minute=40, second=0, microsecond=0), NOW, timedelta(hours=1)):
        h = t.astimezone(TZ).hour
        if not (h >= 17 or h <= 5) or (stop and t > stop):
            continue
        out += [(t, "level", "100"),
                (t, "powerstats", json.dumps({"last_pwr_act_avg": watts, "last_volt_avg": 229}))]
    return out


def observed(readings: dict, *, covered="all", events=(), alarms=(), streets=None,
             now=NOW) -> Observed:
    devices = {n: Place(n, t, la, lo, f"Device {n}", (streets or {}).get(n, s),
                        max((r[0] for r in readings.get(n, [])), default=None))
               for n, (t, la, lo, s) in PLACES.items()}
    recs = {n: sorted((ms(t), k, v) for t, k, v in rows) for n, rows in readings.items()}
    if covered == "all":
        cov = {n: [(ms(now - timedelta(days=9)), ms(now))] for n in devices}
    else:
        cov = {n: [(ms(a), ms(b)) for a, b in spans] for n, spans in covered.items()}
    return Observed(now, devices, recs, cov, [(ms(t), d, m) for t, d, m in events], list(alarms))


def night_of_lamps(**stops) -> dict:
    """Every lamp lit all night; those named stop reporting at the given time."""
    return {n: lamp_nights(stops.get(n)) for n in ("YL1", "YL2", "YL3", "YL4", "YL5")}


# -- detectors -----------------------------------------------------------------------------------


def test_lamps_dark_at_night_until_they_report_again():
    at = local(2026, 11, 25, 21, 40)
    obs = observed(night_of_lamps(YL1=at, YL2=at, YL3=at))
    found = {d.device: d for d in D.lamps_dark(obs)}
    assert set(found) == {"YL1", "YL2", "YL3"}
    d = found["YL1"]
    assert d.active and d.severity == "medium" and d.family == "outage"
    assert d.street == "Mnasiadou" and "silent since" in d.what
    assert d.link_time == local(2026, 11, 25, 21, 40)
    assert [e.text for e in d.evidence][0] == "last report"


def test_a_lamp_silent_by_day_or_where_nobody_recorded_is_not_dark():
    # Reports stop at dawn: by day a silent lamp is off, as it should be.
    obs = observed(night_of_lamps(), now=local(2026, 11, 25, 14, 0))
    assert D.lamps_dark(obs) == []
    # Silent at night, but the memory holds nothing for the stretch: unknown, never dark.
    obs = observed(night_of_lamps(YL1=local(2026, 11, 25, 21, 40)),
                   covered={"YL1": [(NOW - timedelta(hours=30), local(2026, 11, 25, 22, 0))]})
    assert D.lamps_dark(obs) == []


def test_a_short_gap_that_ended_is_a_blip_not_an_incident():
    rows = lamp_nights()
    gap = [r for r in rows if not (local(2026, 11, 25, 19, 0) < r[0] < local(2026, 11, 25, 20, 0))]
    obs = observed({"YL1": gap})
    assert D.lamps_dark(obs) == []  # one report missed: 45 minutes dark, then it reported


def test_lamps_lit_by_day_but_not_at_the_evening_switch_on():
    noon = [(NOON, "level", "100")]
    early = [(local(2026, 11, 20, 15, 48), "level", "100")]  # the city switches on 50 min early
    obs = observed({"YL1": noon, "YL2": early}, now=local(2026, 11, 25, 13, 0))
    found = D.lamps_by_day(obs)
    assert [d.device for d in found] == ["YL1"]
    assert "lit by day at 12:00" in found[0].what and found[0].active


def test_power_out_of_line_with_the_class_of_similar_lamps_on_its_street():
    readings = {n: lamp_nights(watts=54) for n in ("YL1", "YL2", "YL3", "YL4")}
    readings["YL5"] = lamp_nights(watts=12)
    streets = {"YL5": "Mnasiadou"}
    found = D.lamps_power(observed(readings, streets=streets))
    assert [d.device for d in found] == ["YL5"]
    assert found[0].extra == {"power_w": 12.0, "similar_w": 54.0}
    # Three or more lamps of another class on the street are a class of their own, not faults.
    readings |= {"YL1": lamp_nights(watts=12), "YL2": lamp_nights(watts=11), "YL6": lamp_nights(watts=55)}
    assert D.lamps_power(observed(readings, streets=streets)) == []


def test_parking_stuck_occupied_for_days_while_it_keeps_reporting():
    since = NOW - timedelta(days=4)
    stuck = [(t, "status", "1") for t in every(since, NOW, timedelta(hours=4))]
    busy = stuck[:-3] + [(NOW - timedelta(hours=10), "status", "0"), (NOW - timedelta(hours=2), "status", "1")]
    quiet = [(t, "status", "1") for t in every(since, NOW - timedelta(hours=8), timedelta(hours=4))]
    found = D.parking_stuck(observed({"YP1": stuck, "YP2": busy, "YP3": quiet}))
    assert [d.device for d in found] == ["YP1"]
    assert "for at least 4 days" in found[0].what
    # The memory missing most of those days: no verdict.
    assert D.parking_stuck(observed({"YP1": stuck}, covered={"YP1": [(NOW - timedelta(hours=6), NOW)]})) == []


def test_bin_jumps_and_misreadings():
    jump = [(NOW - timedelta(hours=8), "level", "10"), (NOW - timedelta(hours=6), "level", "75"),
            (NOW - timedelta(hours=4), "level", "77")]
    spike = [(NOW - timedelta(hours=8), "level", "20"), (NOW - timedelta(hours=7), "level", "95"),
             (NOW - timedelta(hours=5), "level", "22")]
    found = {d.device: d for d in D.bins_jump(observed({"YW1": jump, "YW2": spike}))}
    assert found["YW1"].what == "jumped from 10% to 75% in 2 h"
    assert "probably a misreading" in found["YW2"].evidence[-1].text and found["YW2"].end


def test_bins_and_air_stations_that_go_quiet():
    obs = observed({"YW1": [(NOW - timedelta(hours=13), "level", "40")],
                    "YW2": [(NOW - timedelta(hours=1), "level", "40")],
                    "a1": [(NOW - timedelta(hours=4), "caqi", "30")]})
    assert [(d.device, d.what) for d in D.bins_silent(obs)] == [("YW1", "no report for 13 h")]
    air = D.air_silent(obs)
    assert [d.device for d in air] == ["a1"] and air[0].severity == "medium"
    # Not covered by the memory: it may simply not have been recorded.
    assert D.bins_silent(observed({"YW1": [(NOW - timedelta(hours=13), "level", "40")]},
                                  covered={"YW1": []})) == []


def test_city_events_run_until_the_device_reports_again_or_the_bin_empties():
    lost = [(NOW - timedelta(hours=h), "YW1", "YW1: Lost connection") for h in (20, 16, 12)]
    full = [(NOW - timedelta(hours=3), "YW2", "YW2: Fill level above threshold of 80 %")]
    readings = {"YW1": [(NOW - timedelta(hours=11), "level", "50")],
                "YW2": [(NOW - timedelta(hours=2), "level", "90")]}
    found = {d.device: d for d in D.city_events(observed(readings, events=lost + full))}
    yw1 = found["YW1"]
    assert yw1.count == 3 and yw1.what == "lost connection, 3 times" and yw1.source == "city"
    assert yw1.end == NOW - timedelta(hours=11) and yw1.evidence[-1].text == "reported again"
    assert found["YW2"].active and found["YW2"].family == "bins"
    readings["YW2"].append((NOW - timedelta(hours=1), "level", "5"))
    assert not D.city_events(observed(readings, events=full))[0].active


def test_active_city_alarms_without_an_event_are_detections_too():
    alarms = [{"id": "x1", "device": "YP1", "device_type": "Parking", "type": "Lost connection",
               "severity": "MAJOR", "status": "ACTIVE_UNACK", "start_ms": ms(NOW - timedelta(hours=1)),
               "end_ms": None, "clear_ms": None, "last_seen_ms": ms(NOW)}]
    found = D.city_alarms(observed({}, alarms=alarms))
    assert [(d.rule, d.device, d.what) for d in found] == [("city_alarm", "YP1", "lost connection")]


def test_a_detector_that_fails_is_skipped_and_said(monkeypatch):
    def broken(obs):
        raise RuntimeError("boom")
    monkeypatch.setattr(D, "DETECTORS", ((broken, "lamp_dark"), (D.bins_silent, "bin_silent")))
    found, ran = D.detect(observed({}))
    assert found == [] and ran == {"bin_silent"}


# -- grouping ------------------------------------------------------------------------------------


def detections(**stops):
    return D.lamps_dark(observed(night_of_lamps(**stops)))


def test_three_lamps_on_one_street_going_dark_together_are_one_probable_power_cut():
    at = local(2026, 11, 25, 21, 40)
    found = detections(YL1=at, YL2=at, YL3=at + timedelta(minutes=0), YL5=at,
                       YL4=local(2026, 11, 25, 18, 40))
    book = Book(None, lambda: NOW)
    changes = book.update(found, NOW)
    titles = sorted(c["incident"]["title"] for c in changes if c["change"] == "new")
    assert titles == ["Lamp YL4 on Mnasiadou: dark at night, silent since 18:40",
                      "Lamp YL5 on Stasikratous: dark at night, silent since 21:40",
                      "Probable power cut on Mnasiadou"]
    cut = next(x for x in book.incidents.values() if x["title"].startswith("Probable"))
    assert cut["severity"] == "high" and {m["device"] for m in cut["members"]} == {"YL1", "YL2", "YL3"}
    assert "3 lamps on Mnasiadou went dark at night within" in cut["summary"]


def test_links_need_the_same_cause_a_place_and_a_time():
    base = {"family": "outage", "device": "A", "device_type": "Lighting", "street": "Mnasiadou",
            "lat": 35.17, "lon": 33.36, "start": 0, "link": 0, "end": None}
    near_later = base | {"device": "B", "link": 3 * 3_600_000, "start": 3 * 3_600_000}
    other_cause = base | {"device": "C", "family": "bins"}
    far = base | {"device": "D", "street": None, "lat": 35.20, "lon": 33.40}
    same = base | {"device": "E"}
    assert linked(base, same) and not linked(base, near_later)
    assert not linked(base, other_cause) and not linked(base, far)
    assert components([base, same, near_later, far]) == [[0, 1], [2], [3]]


def test_stuck_neighbouring_bays_are_one_incident():
    since = NOW - timedelta(days=4)
    stuck = [(t, "status", "1") for t in every(since, NOW, timedelta(hours=4))]
    book = Book(None, lambda: NOW)
    book.update(D.parking_stuck(observed({"YP1": stuck, "YP2": stuck, "YP3": stuck})), NOW)
    titles = sorted(x["title"] for x in book.incidents.values())
    assert titles[0].startswith("2 bays stuck occupied") and titles[1].startswith("Bay YP3")


# -- the book: lifecycle and actions -------------------------------------------------------------


def test_an_incident_is_kept_resolved_by_the_twin_and_reopened():
    at = local(2026, 11, 25, 21, 40)
    clock = {"now": NOW}
    book = Book(None, lambda: clock["now"])
    book.update(detections(YL1=at, YL2=at, YL3=at), NOW)
    again = book.update(detections(YL1=at, YL2=at, YL3=at), NOW + timedelta(minutes=5))
    assert again == [] and len(book.incidents) == 1  # nothing changed, nothing to say
    x = next(iter(book.incidents.values()))
    # The lamps report again: every member ends, and 20 minutes on the twin resolves it.
    later = NOW + timedelta(minutes=10)
    assert book.update([], later, ran={"lamp_dark"}) == [{"change": "updated", "incident": book.summary(x, later)}]
    assert x["active"] is False and x["status"] == "open"
    done = book.update([], later + timedelta(minutes=25), ran={"lamp_dark"})
    assert [c["change"] for c in done] == ["resolved"]
    assert x["actions"][-1]["by"] == TWIN and x["actions"][-1]["note"].startswith("cleared")
    # A detector that didn't run says nothing about its members.
    book2 = Book(None, lambda: NOW)
    book2.update(detections(YL1=at), NOW)
    book2.update([], NOW + timedelta(hours=1), ran=set())
    assert next(iter(book2.incidents.values()))["active"]
    # The same lamps dark again the next night: the incident comes back.
    next_night = detections(YL1=at, YL2=at, YL3=at)
    for d in next_night:
        d.start += timedelta(hours=1)
        d.extra["silent_since"] += 60_000
    back = book.update(next_night, later + timedelta(hours=1))
    assert "reopened" in [c["change"] for c in back] and x["status"] == "open"


def test_people_acknowledge_snooze_and_resolve_with_a_time_and_a_name():
    at = local(2026, 11, 25, 21, 40)
    book = Book(None, lambda: NOW)
    book.update(detections(YL1=at, YL2=at, YL3=at), NOW)
    iid = next(iter(book.incidents))
    book.act(iid, "acknowledge", by="Maria")
    snoozed = book.act(iid.lower(), "snooze", until=NOW + timedelta(hours=4), note="crew on the way")
    assert snoozed["status"] == "snoozed" and snoozed["snoozed_until"] == "2026-11-26T03:45"
    assert [(a["action"], a["by"]) for a in snoozed["actions"]] == [("acknowledged", "Maria"), ("snoozed", None)]
    assert snoozed["actions"][1]["note"] == "crew on the way" and snoozed["actions"][0]["at"] == "2026-11-25T23:45"
    assert book.counts()["snoozed"] == 1 and book.counts()["high"] == 0
    with pytest.raises(ActionError, match="future"):
        book.act(iid, "snooze", until=NOW - timedelta(minutes=1))
    with pytest.raises(ActionError, match="7 days"):
        book.act(iid, "snooze", until=NOW + timedelta(days=8))
    with pytest.raises(ActionError, match="no incident"):
        book.act("INC-999", "resolve")
    resolved = book.act(iid, "resolve", by="Maria")
    assert resolved["status"] == "resolved"
    with pytest.raises(ActionError, match="reopen it first"):
        book.act(iid, "acknowledge")
    # Resolved by a person and still dark: not reopened for the same lamps.
    book.update(detections(YL1=at, YL2=at, YL3=at), NOW + timedelta(minutes=5))
    assert book.incidents[iid]["status"] == "resolved"


def test_incidents_and_their_actions_are_stored_in_the_memory(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb", record=False)
    at = local(2026, 11, 25, 21, 40)
    book = Book(mem, lambda: NOW)
    book.update(detections(YL1=at, YL2=at, YL3=at, YL5=at), NOW)
    iid = next(x["id"] for x in book.incidents.values() if x["severity"] == "high")
    book.act(iid, "acknowledge", by="Andreas")
    assert book.save() and not book.dirty
    again = Book(mem, lambda: NOW)
    assert again.query(status="all") == book.query(status="all")
    assert again.get(iid)["actions"][0]["by"] == "Andreas"
    assert again.detail(again.get(iid), NOW)["evidence"][0]["time"].startswith("2026-11-25T21:40")
    with mem.reading() as con:
        assert con.execute("SELECT count(*) FROM incident_actions").fetchone()[0] == 1


def test_filters_on_the_inbox():
    at = local(2026, 11, 25, 21, 40)
    book = Book(None, lambda: NOW)
    book.update(detections(YL1=at, YL2=at, YL3=at, YL5=at), NOW)
    assert [r["title"] for r in book.query(severity="high")] == ["Probable power cut on Mnasiadou"]
    assert [r["devices"] for r in book.query(street="stasikratous")] == [1]
    assert [r["ids"] for r in book.query(entity="YL2")] == [["YL1", "YL2", "YL3"]]
    assert book.query(near=(35.1650, 33.3700, 50)) == book.query(street="Stasikratous")
    assert book.query(status="resolved") == []


# -- watches -------------------------------------------------------------------------------------


def state(name, kind_type, state_, *, level=None, caqi=None, stale=False, street=None, lat=35.17, lon=33.36):
    detail = {"level_pct": level, "caqi": caqi}
    return DeviceState(Device(name, kind_type, lat, lon), f"Device {name}", street,
                       Interpretation(kind_type, state_, stale, NOW, 60.0, "", detail))


def test_a_watch_is_only_saved_by_a_click_and_fires_once_per_crossing(tmp_path):
    mem = Memory(tmp_path / "twin.duckdb", record=False)
    clock = {"now": NOW}
    w = Watches(mem, lambda: clock["now"])
    rule = Rule("waste_bin", ids=["YW1394"], metric="fill_pct", op="above", value=90)
    assert rule.label() == "Bin YW1394 passes 90%"
    p = w.propose(rule, [state("YW1394", "Wastebin", "full", level=64)])
    assert p["true_now"] == [] and p["values_now"][0]["value"] == 64 and w.list() == []
    saved = w.confirm(p["proposal_id"], by="Eleni")
    assert saved["by"] == "Eleni" and saved["label"] == "Bin YW1394 passes 90%"
    with pytest.raises(WatchError, match="expired or was already saved"):
        w.confirm(p["proposal_id"])
    assert w.check_states("waste_bin", [state("YW1394", "Wastebin", "full", level=88)]) == []
    fired = w.check_states("waste_bin", [state("YW1394", "Wastebin", "full", level=93)])
    assert [f["message"] for f in fired] == ["Bin YW1394 passed 90%: 93% at 23:45"]
    assert fired[0]["entity"]["id"] == "YW1394"
    assert w.check_states("waste_bin", [state("YW1394", "Wastebin", "full", level=95)]) == []
    w.check_states("waste_bin", [state("YW1394", "Wastebin", "low", level=5)])  # emptied: armed
    clock["now"] = NOW + timedelta(hours=3)
    assert len(w.check_states("waste_bin", [state("YW1394", "Wastebin", "full", level=91)])) == 1
    # A stale reading says nothing.
    assert w.check_states("waste_bin", [state("YW1394", "Wastebin", "low", level=1, stale=True)]) == []
    # Stored: the watch and what it fired come back.
    again = Watches(mem, lambda: NOW)
    assert [x["label"] for x in again.list()] == ["Bin YW1394 passes 90%"]
    assert len(again.list()[0]["fires"]) == 2


def test_watch_rules_for_states_places_streets_and_incidents():
    w = Watches(None, lambda: NOW)
    air = Rule("air_station", near={"lat": 35.17, "lon": 33.36, "radius_m": 800, "label": "your location"},
               states=["high", "very_high"])
    assert air.label() == "Any air station within 800 m of your location turns poor or worse"
    w.confirm(w.propose(air, [state("a1", "smartsense", "low", caqi=30)])["proposal_id"])
    fired = w.check_states("air_station", [state("a1", "smartsense", "high", caqi=80),
                                           state("far", "smartsense", "high", caqi=80, lat=35.3)])
    assert [f["message"] for f in fired] == ["Air station a1 is poor now"]
    lamps = Rule("street_light", street="Mnasiadou", states=["offline"])
    w.confirm(w.propose(lamps, [state("YL1", "Lighting", "on", street="Mnasiadou")])["proposal_id"])
    out = w.check_states("street_light", [state("YL1", "Lighting", "offline", stale=True, street="Mnasiadou")])
    assert [f["message"] for f in out] == ["Lamp YL1 on Mnasiadou is offline now"]
    inc = Rule("incident", severity="high")
    assert inc.label() == "Any new high-severity incident"
    w.confirm(w.propose(inc, [])["proposal_id"])
    news = [{"change": "new", "incident": {"id": "INC-3", "severity": "high", "title": "Probable power cut",
                                           "status": "open", "place": "Mnasiadou", "lat": None}},
            {"change": "new", "incident": {"id": "INC-4", "severity": "low", "title": "Bay", "status": "open",
                                           "place": "x", "lat": None}}]
    assert [f["message"] for f in w.check_incidents(news)] == ["New high: Probable power cut"]
    for bad, says in ((Rule("waste_bin", ids=["YW1"], metric="caqi", op="above", value=1), "no reading"),
                      (Rule("waste_bin", ids=["YW1"]), "give a condition"),
                      (Rule("street_light", states=["offline"]), "say which ones"),
                      (Rule("parking_sensor", ids=["YP1"], states=["broken"]), "no state")):
        with pytest.raises(WatchError, match=says):
            w.propose(bad, [])
    with pytest.raises(WatchError, match="no bin matches"):
        w.propose(Rule("waste_bin", ids=["YW404"], metric="fill_pct", op="above", value=90),
                  [state("YW1", "Wastebin", "low", level=4)])


# -- the watcher, the stream and the endpoints ---------------------------------------------------


@pytest.fixture
def memory(tmp_path):
    """Lamps on Mnasiadou that went dark at 21:40, with their street names, in a real store."""
    mem = Memory(tmp_path / "twin.duckdb")
    rec = mem.recorder
    rec.inventory([{"device_name": n, "device_type": t, "device_label": None,
                    "device_key_type": [{"key": "latitude", "value": str(la)},
                                        {"key": "longitude", "value": str(lo)}]}
                   for n, (t, la, lo, _) in PLACES.items()])
    at = local(2026, 11, 25, 21, 40)
    lamps = night_of_lamps(YL1=at, YL2=at, YL3=at)
    rec.readings("Lighting", {n: [{"key": k, "timestamp": str(ms(t)), "value": v} for t, k, v in rows]
                              for n, rows in lamps.items()},
                 [(n, ms(NOW - timedelta(days=2)), ms(NOW)) for n in lamps])
    rec.events([(ms(at + timedelta(minutes=50)), "YL1", "YL1: Lost connection")])
    assert rec.flush()
    return mem


@pytest.fixture
def city(api, client, tmp_path, monkeypatch, memory):
    monkeypatch.setattr("chatbot.city.CACHE_DIR", tmp_path)
    api.on("getTenantDevices", [])
    c = CityData(Settings(), client=client, now=lambda: NOW, recorder=memory.recorder)
    c._attrs = {n: {"name": f"{s} {i}"} for i, (n, (_, _, _, s)) in enumerate(PLACES.items()) if s}
    return c


def test_the_watcher_reads_the_memory_and_tells_the_pages(city, memory):
    w = Watcher(city, memory)
    loop = asyncio.new_event_loop()
    q = w.hub.subscribe(loop)
    changes = w.evaluate()
    cut = next(c for c in changes if c["change"] == "new")["incident"]
    assert cut["title"] == "Probable power cut on Mnasiadou" and cut["severity"] == "high"
    assert cut["source"] == "city+twin" and cut["devices"] == 3
    loop.run_until_complete(asyncio.sleep(0))
    event = q.get_nowait()
    assert event["type"] == "incident" and event["change"] == "new" and event["counts"]["high"] == 1
    loop.close()
    # Stored: a fresh watcher (a restart) reads the same inbox.
    assert Watcher(city, memory).book.query() == w.book.query()
    assert w.fresh_readings in city.listeners


def test_fresh_snapshots_check_the_watches(city, memory, api, monkeypatch):
    w = Watcher(city, memory)
    rule = Rule("waste_bin", ids=["YW1"], metric="fill_pct", op="above", value=90)
    w.watches.confirm(w.watches.propose(rule, [state("YW1", "Wastebin", "high", level=60)])["proposal_id"])
    monkeypatch.setattr(city, "cached_states", lambda t, max_age_s=900: {
        "YW1": state("YW1", "Wastebin", "full", level=95)} if t == "Wastebin" else {})
    w.fresh_readings("Wastebin")
    published = []
    monkeypatch.setattr(w.hub, "publish", published.append)
    report = w.tick()
    assert report["watch_fires"] == 1
    assert published[-1]["message"] == "Bin YW1 passed 90%: 95% at 23:45"


def test_endpoints_list_act_and_mark(city, memory):
    w = Watcher(city, memory)
    w.evaluate()
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(watcher=w))))
    body = watch_routes.incidents(req, status="unresolved", severity=None, family=None, entity=None,
                                  since_hours=None, limit=100)
    iid = body["incidents"][0]["id"]
    assert body["counts"]["high"] == 1
    detail = watch_routes.incident(iid, req)
    assert detail["members"][0]["id"] == "YL1" and detail["evidence"]
    out = watch_routes.act(iid, watch_routes.Action(action="snooze", until="2026-11-26T08:00", by="Nikos"), req)
    assert out["status"] == "snoozed" and out["snoozed_until"] == "2026-11-26T08:00"
    with pytest.raises(HTTPException) as e:
        watch_routes.act(iid, watch_routes.Action(action="nap"), req)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        watch_routes.incident("INC-77", req)
    marks = watch_routes.marks(req, days=31)["marks"]
    assert marks and marks[0]["id"] == iid and marks[0]["severity"] == "high"
    with pytest.raises(HTTPException):
        watch_routes.incidents(req, status="later", severity=None, family=None, entity=None,
                               since_hours=None, limit=10)


def test_the_stream_says_hello_then_passes_events_on(city, memory):
    w = Watcher(city, memory)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(watcher=w))))

    async def run():
        gen = watch_routes.stream(req)
        hello = await gen.__anext__()
        assert hello.data["type"] == "hello" and w.hub.listening == 1
        w.hub.publish({"type": "watch", "message": "Bin YW1 passed 90%"})
        ev = await asyncio.wait_for(gen.__anext__(), 2)
        await gen.aclose()
        return ev
    ev = asyncio.run(run())
    assert ev.data["message"] == "Bin YW1 passed 90%" and w.hub.listening == 0


# -- tools ---------------------------------------------------------------------------------------


def ctx_of(city, memory, w, emitted=None):
    return ToolContext(Settings(), city, None, None, SessionStore(),
                       (emitted if emitted is not None else []).append, "c1", memory=memory,
                       watcher=w)


def test_the_watch_tools_come_last_in_the_tool_order():
    assert [t["name"] for t in TOOL_DEFS][-3:] == ["list_incidents", "explain_incident", "create_watch"]


def test_list_and_explain_incidents(city, memory):
    w = Watcher(city, memory)
    w.evaluate()
    ctx = ctx_of(city, memory, w)
    res, err = run_tool(ctx, "list_incidents", {"severity": "high"})
    assert not err and res["incidents"][0]["title"] == "Probable power cut on Mnasiadou"
    layer = ctx.store.layers[res["layer_ref"]]
    assert {f["properties"]["id"] for f in layer["features"]} >= {"YL1", "YL2", "YL3"}
    rows = ctx.store.datasets[res["data_ref"]]["rows"]
    assert rows[0]["still_happening"] is True and ctx.store.datasets[res["data_ref"]]["link"] == {"key": "id"}
    res, err = run_tool(ctx, "list_incidents", {"street": "Stasikratous"})
    assert not err and res["incidents"] == [] and "no incidents match" in res["note"]
    iid = w.book.query()[0]["id"]
    res, err = run_tool(ctx, "explain_incident", {"id": iid.lower(), "hours": 3})
    assert not err, res
    assert res["incident"]["id"] == iid and res["devices"][0]["id"] == "YL1"
    assert any(r["source"] == "incident" for r in ctx.store.datasets[res["data_ref"]]["rows"])
    res, err = run_tool(ctx, "explain_incident", {"id": "INC-404"})
    assert err and "no incident" in res["error"]


def test_create_watch_proposes_and_never_saves(city, memory, monkeypatch):
    w = Watcher(city, memory)
    monkeypatch.setattr("chatbot.tools.watch.states_of",
                        lambda c, kind: [state("YW1394", "Wastebin", "high", level=64)])
    emitted = []
    ctx = ctx_of(city, memory, w, emitted)
    res, err = run_tool(ctx, "create_watch", {"kind": "waste_bin", "ids": ["YW1394"], "metric": "fill_pct",
                                              "op": "above", "value": 90})
    assert not err and res["status"] == "waiting_for_the_users_click"
    assert res["label"] == "Bin YW1394 passes 90%" and w.watches.list() == []
    assert emitted[0]["action"] == "watch_confirm" and emitted[0]["proposal"]["proposal_id"] == res["proposal_id"]
    res, err = run_tool(ctx, "create_watch", {"kind": "waste_bin", "ids": ["YW1394"]})
    assert err and "give a condition" in res["error"]
    res, err = run_tool(ctx, "create_watch", {"kind": "air_station", "near_place": "my location",
                                              "states": ["high"]})
    assert err and "hasn't shared a location" in res["error"]

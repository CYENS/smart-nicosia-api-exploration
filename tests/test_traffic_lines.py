"""The counting-lines register: its suggestions, the counting rules that keep a road user from
being counted twice, saving a reviewed camera, and the guards around raw line counts."""

import json
from datetime import datetime

import pytest

from collector import parse
from chatbot import context, traffic_lines as tl
from chatbot.memory import Memory
from chatbot.tools import ToolContext, run_tool
from chatbot.settings import Settings
from chatbot.store import SessionStore
from test_chatbot import city, per_line_hours  # noqa: F401 - city is a fixture

TZ = context.TZ


def line(i, name, direction=0, types=(0, 1, 2, 3, 4, 5)):
    return {"id": i, "name": name, "direction": direction, "count_type": 1, "types": list(types),
            "x1": 0.1, "y1": 0.2, "x2": 0.8, "y2": 0.6}


def cam(va, name, *lines, lat=35.17, lon=33.36):
    return {"analytics_id": va, "name": name, "lines": json.dumps(list(lines)), "lat": lat,
            "lon": lon, "status": "started", "location_source": "device"}


STASIKRATOUS = cam(67, "Stasikratous (Mnasiadou) cam01",
                   line(0, "ST Mnasiadou from Makariou IN V", 1, (1, 2, 3, 4, 5)),
                   line(1, "ST Mnasiadou from Makariou P", 0, (0,)),
                   line(2, "ST Mnasiadou from Makariou OUT V", 2, (1, 2, 3, 4, 5)))
JUNCTION = cam(88, "Kallipoleos (Agora Agiou Antoniou)",
               line(0, "Agora Agiou Ant. (Digeni Akrita pros Aglantzia)"),
               line(1, "Agora Agiou Ant. (Pros Kallipoleos)"),
               line(2, "Agora Agiou Ant. (From Evgenias/Theodotou)"))
ERMOU = cam(106, "Ermou", line(0, "Ermou"))


def row(va, name, obj, n, at=datetime(2026, 10, 18, 12, tzinfo=TZ)):
    return parse.TrafficRow("hour", at, va, name, obj, n, 0, n)


# -- suggestions ----------------------------------------------------------------------------------


def test_suggestions_add_up_only_lines_that_cant_overlap():
    roles = tl.roles([STASIKRATOUS, JUNCTION, ERMOU])
    s = {r.line_name: r for r in roles.values()}
    inn, p, out = (s[f"ST Mnasiadou from Makariou {x}"] for x in ("IN V", "P", "OUT V"))
    assert (inn.measures, inn.direction, inn.adds_up) == ("vehicles", "in", "yes")
    assert (out.measures, out.direction, out.adds_up) == ("vehicles", "out", "yes")
    assert (p.measures, p.adds_up) == ("people", "yes")
    assert inn.street == "Stasikratous, Mnasiadou from Makariou"
    # A junction's movements could count a turning car twice: kept apart until reviewed.
    assert {r.adds_up for r in roles.values() if r.analytics_id == 88} == {"review"}
    # A camera with one line has nothing to overlap.
    assert s["Ermou"].adds_up == "yes" and s["Ermou"].measures == "both"


def test_two_feeds_on_one_camera_split_people_from_vehicles():
    vehicles = cam(98, "Ledras End (Hellenic Bank) Vehicles",
                   line(0, "Ledras End (Hellenic Bank)", 0, (1, 2, 3, 5)))
    people = cam(99, "Ledras End (Hellenic Bank) People",
                 line(0, "Ledras End (Hellenic Bank) People", 0, (0, 1, 2, 3, 5)))
    roles = tl.roles([vehicles, people])
    assert roles[(98, "Ledras End (Hellenic Bank)")].measures == "vehicles"
    # It is set to count cars too, but its cars are the Vehicles feed's: people only.
    assert roles[(99, "Ledras End (Hellenic Bank) People")].measures == "people"


# -- counting -------------------------------------------------------------------------------------


def test_a_road_user_counts_once_and_only_on_its_own_kind_of_line():
    roles = tl.roles([STASIKRATOUS, JUNCTION])
    rows = [row(67, "ST Mnasiadou from Makariou IN V", 2, 10),
            row(67, "ST Mnasiadou from Makariou IN V", 0, 3),    # "people" on a car line
            row(67, "ST Mnasiadou from Makariou OUT V", 2, 8),
            row(67, "ST Mnasiadou from Makariou P", 0, 20),
            row(67, "ST Mnasiadou from Makariou P", 1, 1),       # a bike on the people line
            row(88, "Agora Agiou Ant. (Pros Kallipoleos)", 2, 50),
            row(88, "Agora Agiou Ant. (From Evgenias/Theodotou)", 2, 40),
            row(5, "A line nobody knows", 2, 99)]
    t = tl.tally(rows, roles)
    at = datetime(2026, 10, 18, 12, tzinfo=TZ)
    assert t.added[(at, 67)] == {2: 18, 0: 20}  # cars in and out; people from the people line
    assert (at, 88) not in t.added  # the junction waits for a review rather than add 50 + 40
    assert t.left_out[(88, "Agora Agiou Ant. (Pros Kallipoleos)")].startswith("kept apart")
    assert t.left_out[(5, "A line nobody knows")] == "not in the register"
    how = tl.how_counted(t, roles, {67: "Stasikratous", 88: "Agora"})
    assert how["lines_added"] == 3 and len(how["lines_left_out"]) == 3
    assert {"camera": "Agora", "line": "Agora Agiou Ant. (Pros Kallipoleos)",
            "why": "kept apart until someone reviews it", "crossings": 50} in how["lines_left_out"]


def test_get_traffic_adds_a_camera_once_and_says_how(city, api, monkeypatch):
    monkeypatch.setattr(city, "traffic_cameras", lambda: [STASIKRATOUS, JUNCTION])
    day = context._period_bounds("yesterday", city.now())[2][0]
    api.on("getTrafficReports", {"data": {"group_by": {day.strftime("%d-%m-%Y"): {
        "67": {"ST Mnasiadou from Makariou IN V": {"2": {"1": 10, "2": 0, "total": 10}},
               "ST Mnasiadou from Makariou P": {"0": {"1": 9, "2": 11, "total": 20}}},
        "88": {"Agora Agiou Ant. (Pros Kallipoleos)": {"2": {"1": 50, "2": 0, "total": 50}},
               "Agora Agiou Ant. (From Evgenias/Theodotou)": {"2": {"1": 40, "2": 0,
                                                                    "total": 40}}}}}}})
    res = context.traffic(city, "yesterday", "camera", None)
    assert res["rows"] == [{"camera": "Stasikratous (Mnasiadou) cam01", "lat": 35.17,
                            "lon": 33.36, "car": 10, "people": 20, "total": 30}]
    left = {x["line"] for x in res["how_counted"]["lines_left_out"]}
    assert left == {"Agora Agiou Ant. (Pros Kallipoleos)",
                    "Agora Agiou Ant. (From Evgenias/Theodotou)"}
    by_day = context.traffic(city, "yesterday", "day", None)
    assert by_day["rows"] == [{"day": day.isoformat(), "car": 10, "people": 20, "total": 30}]


def test_hours_are_one_request_each_for_every_camera_and_kept(city, api, monkeypatch):
    monkeypatch.setattr(city, "traffic_cameras", lambda: [STASIKRATOUS, ERMOU])
    api.on("getTrafficReports", per_line_hours({
        "08:00": {67: {"ST Mnasiadou from Makariou P": {0: 30}}, 106: {"Ermou": {0: 5, 2: 1}}}}))
    res = context.traffic(city, "yesterday", "hour_of_day", "Ermou")
    hours = [p for e, p in api.calls if e == "getTrafficReports"]
    assert len(hours) == 24 and {p["va_ids"] for p in hours} == {"[]"}
    assert res["rows"] == [{"hour": res["rows"][0]["hour"], "people": 5, "car": 1, "total": 6}]
    assert res["rows"][0]["hour"].endswith("T08:00")
    context.traffic(city, "yesterday", "hour_of_day", "Stasikratous")
    assert api.count("getTrafficReports") == 24  # yesterday's hours are kept: no new requests


def test_the_day_the_clocks_go_back_has_25_hours():
    start = datetime(2026, 10, 25, tzinfo=TZ)
    hours = tl.day_hours(start, datetime(2026, 10, 27, tzinfo=TZ))
    assert len(hours) == 25 and hours[3].hour == hours[4].hour == 3


# -- the file and its review -----------------------------------------------------------------------


def test_saving_a_reviewed_camera_keeps_the_rest_of_the_file(tmp_path):
    path = tmp_path / "lines.csv"
    cams = [STASIKRATOUS, JUNCTION]
    tl.write_register(tl.roles(cams, path).values(), path)
    saved = tl.save_camera(cams, 88, [
        {"line_name": "Agora Agiou Ant. (Digeni Akrita pros Aglantzia)", "measures": "vehicles",
         "direction": "out", "street": "Digeni Akrita, towards Aglantzia", "adds_up": "no"},
        {"line_name": "Agora Agiou Ant. (Pros Kallipoleos)", "measures": "vehicles",
         "direction": "out", "street": "Kallipoleos", "adds_up": "no"},
        {"line_name": "Agora Agiou Ant. (From Evgenias/Theodotou)", "measures": "both",
         "direction": "in", "street": "Evgenias Theodotou", "adds_up": "yes",
         "note": "the only entry the camera sees"},
    ], "2026-11-27", path)
    assert len(saved) == 3 and all(r.reviewed == "2026-11-27" for r in saved)
    back = tl.read_register(path)
    assert back[(88, "Agora Agiou Ant. (From Evgenias/Theodotou)")].adds_up == "yes"
    assert back[(67, "ST Mnasiadou from Makariou P")].reviewed == ""  # untouched
    walk = tl.walkthrough(cams, path)
    assert [c["analytics_id"] for c in walk] == [67, 88]  # still to review comes first
    assert walk[1]["reviewed"] is True and walk[1]["lines"][0]["ends"] == [0.1, 0.2, 0.8, 0.6]


@pytest.mark.parametrize("change, words", [
    ({"measures": "cats"}, "measures must be one of"),
    ({"adds_up": "maybe"}, "adds_up must be one of"),
    ({"street": ""}, "street must be"),
    ({"line_name": "No such line"}, "has no line called"),
])
def test_a_bad_review_says_what_is_allowed(tmp_path, change, words):
    good = {"line_name": "Ermou", "measures": "both", "direction": "both", "street": "Ermou",
            "adds_up": "yes"}
    with pytest.raises(ValueError, match=words):
        tl.save_camera([ERMOU], 106, [{**good, **change}], "2026-11-27", tmp_path / "x.csv")


# -- guards ---------------------------------------------------------------------------------------


def test_sql_cant_total_raw_line_crossings(city, tmp_path):
    memory = Memory(tmp_path / "twin.duckdb")
    memory.recorder.traffic([row(67, "ST Mnasiadou from Makariou P", 0, 20)])
    assert memory.recorder.flush()
    ctx = ToolContext(Settings(), city, None, None, SessionStore(), [].append, "c1",
                      memory=memory)
    res, err = run_tool(ctx, "run_sql", {"sql": "SELECT sum(total) FROM traffic_counts"})
    assert err and "get_traffic" in res["error"]
    res, err = run_tool(ctx, "run_sql", {
        "sql": "SELECT line_name, total FROM traffic_counts WHERE analytics_id = 67"})
    assert not err and res["rows"][0]["total"] == 20

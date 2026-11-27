"""The admin page's collectors: the controls file every collector obeys, the service that runs
the collector inside the server, its report, and its routes."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from collector import controls, scheduler
from collector.catalog import JOB_INFO, UNRECORDED
from collector.config import DEFAULT_JOBS
from collector.jobs import JOBS
from chatbot.collectors import Collectors, UnknownJob
from chatbot.memory import Memory
from chatbot.routes import collectors as routes
from chatbot.sources import SOURCES
from test_jobs import NOW, city  # noqa: F401 - the fake city API fixture


def make(cfg, client, **kw) -> Collectors:
    return Collectors(Memory(cfg.db_path), collect_default=kw.pop("collect", False),
                      base=cfg, client=client, **kw)


def request(c: Collectors):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(collectors=c)))


# -- the catalogue -------------------------------------------------------------------------------


def test_every_job_is_described_and_names_a_known_source():
    ids = {s.id for s in SOURCES}
    assert set(JOB_INFO) == set(JOBS) == set(DEFAULT_JOBS)
    for name, info in JOB_INFO.items():
        assert info.source in ids, name
        assert info.title and info.summary and info.records and info.uses and info.tables, name
        assert len(info.summary) <= 60 and not info.summary.endswith("."), name  # one short line
        for text in (info.title, info.summary, info.records, info.uses):
            assert "—" not in text and "–" not in text and "--" not in text, name
    assert set(UNRECORDED) <= ids


# -- the controls file ---------------------------------------------------------------------------


def test_controls_round_trip_and_ignore_a_broken_file(tmp_path):
    path = tmp_path / controls.FILE_NAME
    assert controls.load(path) == controls.Controls()
    controls.update(path, lambda c: (setattr(c, "collecting", False),
                                     c.jobs.update(speed={"every_minutes": 15.0})))
    back = controls.load(path)
    assert back.collecting is False and back.jobs == {"speed": {"every_minutes": 15.0}}
    assert back.changed_at
    path.write_text("{not json", encoding="utf-8")
    assert controls.load(path) == controls.Controls()


@pytest.mark.parametrize("patch, words", [
    ({"every": 5}, "unknown setting"),
    ({"enabled": "yes"}, "true or false"),
    ({"every_minutes": 0}, "between 1 and 10080"),
    ({"every_minutes": "5"}, "number of minutes"),
])
def test_a_bad_job_change_says_what_is_accepted(patch, words):
    with pytest.raises(ValueError, match=words):
        controls.clean_job_patch(patch)


def test_a_pass_obeys_the_page(cfg, client, city):  # noqa: F811
    path = controls.controls_path(cfg)
    controls.update(path, lambda c: setattr(c, "collecting", False))
    assert scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW) == []  # paused
    # Asked for by name, a job still runs while collection is paused.
    ran = scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW, only=["speed"], force=True)
    assert [s.job for s in ran] == ["speed"]

    controls.update(path, lambda c: (setattr(c, "collecting", None),
                                     c.jobs.update(events={"enabled": False})))
    ran = {s.job for s in scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW)}
    assert "events" not in ran and "alarms" in ran


def test_a_pause_stops_a_pass_already_under_way(cfg, client, city):  # noqa: F811
    path = controls.controls_path(cfg)
    started = []

    def pause_after_the_first(name):
        if name and not started:
            controls.update(path, lambda c: setattr(c, "collecting", False))
        if name:
            started.append(name)

    ran = scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW, on_job=pause_after_the_first)
    assert [s.job for s in ran] == started == ["devices"]  # the job under way finishes; no more


# -- the service ---------------------------------------------------------------------------------


def test_report_before_anything_was_recorded(cfg, client):
    c = make(cfg, client)
    r = c.report(NOW)
    assert r["store"]["status"] == "missing"
    assert r["collecting"] is False and r["decided_on_page"] is False
    assert [j["name"] for j in r["jobs"]][:3] == ["stream", "events", "alarms"]
    travel = next(j for j in r["jobs"] if j["name"] == "travel_times")
    assert travel["source"]["id"] == "road_speeds" and travel["counts_against_budget"] is False
    assert travel["last"] is None and travel["requests_per_day"] == 288
    assert 0 < r["budget"]["per_day"] <= r["budget"]["limit"]
    assert r["unrecorded"][0]["source"]["id"] == "bus_live"


def test_changes_keep_only_what_differs_from_the_default(cfg, client):
    c = make(cfg, client)
    c.update_job("speed", {"every_minutes": 15, "enabled": True})
    saved = json.loads(controls.controls_path(cfg).read_text(encoding="utf-8"))
    assert saved["jobs"] == {"speed": {"every_minutes": 15.0}}
    speed = next(j for j in c.report(NOW)["jobs"] if j["name"] == "speed")
    assert speed["every_minutes"] == 15 and speed["changed"] == ["every_minutes"]
    assert speed["defaults"]["every_minutes"] == 60

    c.update_job("speed", {"every_minutes": 60})  # back to the default: nothing left to keep
    assert "speed" not in controls.load(controls.controls_path(cfg)).jobs
    c.update_job("speed", {"enabled": False})
    c.reset_job("speed")
    assert controls.load(controls.controls_path(cfg)).jobs == {}
    with pytest.raises(UnknownJob):
        c.update_job("nope", {"enabled": False})
    with pytest.raises(UnknownJob):
        c.run_now("nope")


def test_run_now_runs_a_switched_off_job_and_the_report_shows_it(cfg, client, city):  # noqa: F811
    c = make(cfg, client)
    c.update_job("speed", {"enabled": False})
    assert c.run_now("speed") == "queued"
    assert next(j for j in c.report(NOW)["jobs"] if j["name"] == "speed")["state"] == "queued"
    c.turn(due=True)  # collection is off: only the asked job runs
    r = c.report()
    speed = next(j for j in r["jobs"] if j["name"] == "speed")
    assert speed["state"] is None and speed["last"]["status"] == "ok"
    assert speed["last"]["requests"] >= 1
    assert all(j["last"] is None for j in r["jobs"] if j["name"] != "speed")
    assert r["last_pass_here"]["jobs"] == ["speed"]
    assert r["store"]["status"] == "ok"


def test_starting_collection_runs_a_scheduled_pass(cfg, client, city):  # noqa: F811
    c = make(cfg, client)
    c.turn(due=True)
    assert c.report()["last_pass_here"] is None  # off: nothing ran
    c.set_collecting(True)
    assert controls.load(controls.controls_path(cfg)).collecting is True
    c.turn(due=True)
    assert c.memory.collecting is True
    assert "stream" in c.report()["last_pass_here"]["jobs"]

    c.set_collecting(False)
    c.turn(due=True)
    assert c.memory.collecting is False
    # The pause holds for a `collector run` from Task Scheduler as well.
    assert scheduler.run_pass(cfg, client=client) == []


def test_pausing_the_recorder_drops_what_pages_fetch(cfg, client):
    c = make(cfg, client)
    rec = c.memory.recorder
    c.set_recording(False)
    rec.events([(1_700_000_000_000, "YW1", "Fill level above 80 %")])
    assert not rec._batch.events and rec.status()["paused"] is True
    c.set_recording(True)
    rec.events([(1_700_000_000_000, "YW1", "Fill level above 80 %")])
    assert len(rec._batch.events) == 1
    assert c.report(NOW)["recorder"]["recording"] is True


# -- the routes ----------------------------------------------------------------------------------


def test_routes_check_their_input(cfg, client):
    c = make(cfg, client)
    req = request(c)
    with pytest.raises(HTTPException) as e:
        routes.set_collecting(req, {"on": "yes"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        routes.update_job("speed", req, {"every_minutes": -1})
    assert e.value.status_code == 400 and "between" in e.value.detail
    with pytest.raises(HTTPException) as e:
        routes.update_job("nope", req, {"enabled": True})
    assert e.value.status_code == 404
    res = routes.update_job("bins", req, {"every_minutes": 60})
    body = json.loads(res.body)
    assert next(j for j in body["jobs"] if j["name"] == "bins")["every_minutes"] == 60
    assert routes.run_job_now("bins", req).status_code == 202
    assert json.loads(routes.reset_job("bins", req).body)["changed_at"]

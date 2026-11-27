"""The admin page's collectors: the controls file every collector obeys, the service that runs
the collector inside the server, its report, "How it's collected", the routes and their locks,
and the declarations on the job classes, checked against what each job really does."""

import inspect
import json
from dataclasses import fields
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from collector import controls, db, scheduler
from collector.catalog import COMMON_SETTINGS, JOB_INFO, SETTING_LABELS, UNRECORDED
from collector.config import DEFAULT_JOBS, Config
from collector.jobs import JOBS, Job
from chatbot.collectors import REPO_URL, Collectors, TooSoon, UnknownJob
from chatbot.memory import Memory
from chatbot.routes import collectors as routes
from chatbot.sources import SOURCES
from test_jobs import NOW, city  # noqa: F401 - the fake city API fixture

PASSWORD = "a long admin password"
ADMIN = {"X-Admin-Password": PASSWORD}
BOOKKEEPING = {"job_runs", "api_errors", "coverage", "watermarks"}
# Written only when a reading changes a device's state; one fixture snapshot often doesn't.
MAY_STAY_UNCHANGED = {"state_changes"}


def make(cfg, client, **kw) -> Collectors:
    return Collectors(Memory(cfg.db_path), collect_default=kw.pop("collect", False),
                      base=cfg, client=client, **kw)


def request(c: Collectors, headers: dict | None = None):
    """A request as the routes see it: JSON, from the page itself, plus `headers`."""
    h = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin", **(headers or {})}
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(collectors=c)),
                           headers=Headers(h))


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("CHATBOT_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(routes, "WRONG_PASSWORD_PAUSE_S", 0)


def fingerprints(cfg) -> dict[str, tuple]:
    """Every table's row count and content hash, so an update shows as well as an insert."""
    with db.session(cfg.db_path) as con:  # opening it creates the schema
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()]
        return {t: con.execute(f'SELECT count(*), coalesce(sum(hash(x)), 0) FROM "{t}" x')
                .fetchone() for t in tables if t not in BOOKKEEPING}


# -- the catalogue and the declarations ---------------------------------------------------------


def test_every_job_is_described_and_names_a_known_source():
    ids = {s.id for s in SOURCES}
    assert set(JOB_INFO) == set(JOBS) == set(DEFAULT_JOBS)
    for name, info in JOB_INFO.items():
        assert info.source in ids, name
        assert info.title and info.summary and info.records and info.uses and info.how, name
        assert len(info.summary) <= 60 and not info.summary.endswith("."), name  # one short line
        for text in (info.title, info.summary, info.records, info.uses, info.how):
            assert "—" not in text and "–" not in text and "--" not in text, name
    assert set(UNRECORDED) <= ids


def test_every_job_class_explains_itself():
    config_fields = {f.name for f in fields(Config)}
    assert set(COMMON_SETTINGS) <= set(SETTING_LABELS) and set(COMMON_SETTINGS) <= config_fields
    for name, job in JOBS.items():
        cls = type(job)
        assert cls.__dict__.get("__doc__"), f"{cls.__name__} needs its own docstring"
        assert job.calls and job.writes, name
        for key in job.settings:
            assert key in config_fields, f"{name} declares {key!r}, which Config doesn't have"
            assert key in SETTING_LABELS, f"{key!r} needs a label in collector/catalog.py"
        # A job that works out its own cost says why, beside it.
        owner = next(k for k in cls.__mro__ if "requests_per_run" in k.__dict__)
        if owner is not Job:
            assert "cost_note" in owner.__dict__, f"{owner.__name__} needs a cost_note"


@pytest.mark.parametrize("name", list(JOBS))
def test_each_job_declares_what_it_really_calls_and_writes(name, cfg, client, city):  # noqa: F811
    if name != "devices":  # the per-device jobs need the inventory first
        scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW, only=["devices"], force=True)
    before = fingerprints(cfg)
    city.calls.clear()
    ran = scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW, only=[name], force=True)
    assert [s.status for s in ran] == ["ok"], ran
    after = fingerprints(cfg)
    assert {e for e, _ in city.calls} == set(JOBS[name].calls)
    written = {t for t in after if after[t] != before.get(t)}
    declared = set(JOBS[name].writes)
    assert written <= declared, f"{name} writes {written - declared} without declaring them"
    assert declared - written <= MAY_STAY_UNCHANGED, f"{name} never wrote {declared - written}"


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
    assert r["store"]["path"] == cfg.db_path.name  # outside the project: the file name only
    assert r["collecting"] is False and r["decided_on_page"] is False
    assert [j["name"] for j in r["jobs"]][:3] == ["stream", "events", "alarms"]
    travel = next(j for j in r["jobs"] if j["name"] == "travel_times")
    assert travel["source"]["id"] == "road_speeds" and travel["counts_against_budget"] is False
    assert travel["last"] is None and travel["requests_per_day"] == 288
    assert travel["tables"] == ["travel_times"]
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
    with pytest.raises(TooSoon, match="less than a minute ago"):  # once a minute per job
        c.run_now("speed")
    assert c.run_now("bins") == "queued"  # other jobs aren't held up


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


def test_how_reads_the_code_the_settings_and_the_database(cfg, client, city, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHATBOT_COMMIT", "abc1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-to-be-shown")
    c = make(cfg, client)
    scheduler.run_pass(cfg, client=client, now_fn=lambda: NOW, only=["devices", "bins"], force=True)
    h = c.how("bins", NOW)
    assert h["how"] == JOB_INFO["bins"].how and h["doc"] == inspect.getdoc(type(JOBS["bins"]))
    assert [x["endpoint"] for x in h["calls"]] == ["getLatestTelemetry"]
    assert h["calls"][0]["url"].endswith("/getLatestTelemetry")
    code = h["code"]
    assert code["path"] == "collector/jobs.py" and code["commit"] == "abc1234"
    assert code["url"] == f"{REPO_URL}/blob/abc1234/collector/jobs.py#L{code['line']}"
    tables = {t["name"]: t for t in h["tables"]}
    assert set(tables) == set(JOBS["bins"].writes) and all(t["columns"] for t in h["tables"])
    assert tables["telemetry"]["rows"] > 0 and tables["telemetry"]["newest"]
    assert "Air and noise stations" in tables["telemetry"]["shared_with"]
    assert h["runs"][0]["status"] == "ok"
    assert h["cost"]["why"].startswith("One request per bin:")
    assert {s["key"] for s in h["settings"]} <= set(SETTING_LABELS)
    text = json.dumps(h)
    assert "sk-not-to-be-shown" not in text and str(cfg.db_path.parent) not in text
    with pytest.raises(UnknownJob):
        c.how("nope")


# -- the routes and their locks ------------------------------------------------------------------


def test_changes_are_locked_without_an_admin_password(cfg, client, monkeypatch):
    monkeypatch.delenv("CHATBOT_ADMIN_PASSWORD", raising=False)
    c = make(cfg, client)
    body = json.loads(routes.collectors_report(request(c)).body)
    assert body["admin"] == {"configured": False, "unlocked": False}
    with pytest.raises(HTTPException) as e:
        routes.set_collecting(request(c, ADMIN), {"on": True})
    assert e.value.status_code == 403 and "CHATBOT_ADMIN_PASSWORD" in e.value.detail
    assert controls.load(controls.controls_path(cfg)).collecting is None  # nothing changed


@pytest.mark.parametrize("headers, status, words", [
    ({}, 403, "need the admin password"),
    ({"X-Admin-Password": "guess"}, 403, "isn't right"),
    ({**ADMIN, "Sec-Fetch-Site": "cross-site"}, 403, "own admin page"),
    ({**ADMIN, "Sec-Fetch-Site": "same-site"}, 403, "own admin page"),
    ({**ADMIN, "Content-Type": "text/plain"}, 415, "JSON"),
])
def test_a_change_needs_the_password_json_and_this_page(cfg, client, admin, headers, status,
                                                        words):
    c = make(cfg, client)
    with pytest.raises(HTTPException) as e:
        routes.run_job_now("speed", request(c, headers))
    assert e.value.status_code == status and words in e.value.detail
    assert c.report(NOW)["queued"] == []


def test_routes_check_their_input(cfg, client, city, admin):  # noqa: F811
    c = make(cfg, client)
    req = request(c, ADMIN)
    assert json.loads(routes.collectors_report(req).body)["admin"]["unlocked"] is True
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
    assert routes.run_job_now("bins", req).status_code == 202  # still waiting: nothing new
    c.turn(due=False)  # it runs; asking again within the minute is refused
    with pytest.raises(HTTPException) as e:
        routes.run_job_now("bins", req)
    assert e.value.status_code == 429 and e.value.headers["Retry-After"]
    assert json.loads(routes.reset_job("bins", req).body)["changed_at"]
    how = json.loads(routes.how_it_is_collected("travel_times", request(c)).body)  # no password
    assert how["calls"][0]["endpoint"] == "PredefinedLocationDataPublication"


def test_the_report_is_as_slow_as_a_change_for_a_wrong_password(cfg, client, admin, monkeypatch):
    # The report says whether the password was right, so it mustn't be a fast way to guess.
    slept = []
    monkeypatch.setattr(routes.time, "sleep", slept.append)
    monkeypatch.setattr(routes, "WRONG_PASSWORD_PAUSE_S", 1.0)
    c = make(cfg, client)
    routes.collectors_report(request(c, {"X-Admin-Password": "guess"}))
    routes.collectors_report(request(c, ADMIN))
    routes.collectors_report(request(c))
    assert slept == [1.0]

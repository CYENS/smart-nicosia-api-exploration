from pathlib import Path

import pytest

from collector.config import DEFAULT_JOBS, load_config
from collector.cli import parse_when


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COLLECTOR_CONFIG", raising=False)
    monkeypatch.delenv("COLLECTOR_DB", raising=False)
    cfg = load_config()
    assert cfg.db_path == Path("data/smart_nicosia.duckdb")
    assert cfg.jobs == DEFAULT_JOBS
    assert cfg.jobs["lamps"].night_only


def test_toml_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("COLLECTOR_DB", raising=False)
    path = tmp_path / "collector.toml"
    path.write_text(
        'db_path = "x/y.duckdb"\nrequest_delay_s = 1.5\nalarm_states = ["ACTIVE_UNACK", "ACTIVE_ACK"]\n'
        "[jobs.parking]\nevery_minutes = 15\n[jobs.travel_times]\nenabled = false\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.db_path == Path("x/y.duckdb")
    assert cfg.request_delay_s == 1.5
    assert cfg.alarm_states == ("ACTIVE_UNACK", "ACTIVE_ACK")
    assert cfg.jobs["parking"].every_minutes == 15
    assert not cfg.jobs["travel_times"].enabled
    assert cfg.jobs["bins"] == DEFAULT_JOBS["bins"]


def test_env_and_cli_override_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLECTOR_DB", "env.duckdb")
    assert load_config().db_path == Path("env.duckdb")
    assert load_config(db_path="cli.duckdb").db_path == Path("cli.duckdb")


@pytest.mark.parametrize("text", ["nonsense = 1\n", "[jobs.toaster]\nevery_minutes = 1\n"])
def test_unknown_settings_are_rejected(tmp_path, text):
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_parse_when():
    assert parse_when("2026-09-01").isoformat() == "2026-09-01T00:00:00+00:00"
    assert parse_when("30d") < parse_when("12h")


def test_example_config_parses_to_the_defaults():
    from conftest import ROOT

    cfg = load_config(ROOT / "collector.example.toml")
    assert cfg.jobs == DEFAULT_JOBS

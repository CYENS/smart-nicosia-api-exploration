import json
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector.config import Config
from collector.http import Client

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = ROOT / "sample_payloads"  # captured Jan 2026


def load(name: str):
    """A captured payload from tests/fixtures (captured 19 Oct 2026)."""
    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def sample(name: str):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class FakeApi:
    """Stands in for the network: routes request URLs to handlers that return payloads."""

    def __init__(self):
        self.handlers: dict[str, Callable[[dict], object]] = {}
        self.calls: list[tuple[str, dict]] = []

    def on(self, endpoint: str, handler):
        self.handlers[endpoint] = handler if callable(handler) else (lambda p, v=handler: v)
        return self

    def fetch(self, url: str, accept: str, timeout: float) -> str:
        parsed = urllib.parse.urlsplit(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((endpoint, params))
        if endpoint not in self.handlers:
            raise AssertionError(f"unexpected request {endpoint} {params}")
        result = self.handlers[endpoint](params)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, str) else json.dumps(result)

    def count(self, endpoint: str) -> int:
        return sum(1 for e, _ in self.calls if e == endpoint)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "test.duckdb",
        log_file=None,
        camera_csv=tmp_path / "camera_locations.csv",
        request_delay_s=0.0,
        max_retries=1,
    )


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def client(cfg, api) -> Client:
    return Client(cfg, fetch=api.fetch, sleep=lambda s: None)

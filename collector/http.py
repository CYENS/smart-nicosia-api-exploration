"""HTTP access for the collector, built on smart_nicosia_api (standard library only).

Adds what unattended polling needs: a minimum gap between requests, retries with backoff on
network errors and 5xx, a request counter for the daily budget, and detection of the Incoresoft
errors that come back as HTTP 200.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import smart_nicosia_api as sn
from smart_nicosia_api import IncoresoftError

from collector.config import Config

log = logging.getLogger(__name__)

# fetch(url, accept, timeout) -> response body
Fetch = Callable[[str, str, float], str]


class ApiError(Exception):
    def __init__(self, endpoint: str, params: dict, message: str, kind: str = "http"):
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.params = params
        self.message = message
        self.kind = kind


@dataclass
class RequestStats:
    requests: int = 0  # every HTTP attempt, including retries
    nokia_requests: int = 0
    errors: list[ApiError] = field(default_factory=list)


class Client:
    def __init__(
        self,
        cfg: Config,
        *,
        fetch: Fetch | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float | None = None,
        request_delay_s: float | None = None,
    ):
        self.cfg = cfg
        self.timeout_s = timeout_s if timeout_s is not None else cfg.timeout_s
        self.request_delay_s = (
            request_delay_s if request_delay_s is not None else cfg.request_delay_s
        )
        self._fetch = fetch or self._default_fetch
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self.stats = RequestStats()

    def _default_fetch(self, url: str, accept: str, timeout: float) -> str:
        return sn.fetch(url, accept, timeout, self.cfg.insecure, self.cfg.cafile)

    def _wait_turn(self) -> None:
        """Space request starts at least request_delay_s apart, across threads."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_slot)
            self._next_slot = start + self.request_delay_s
        if start > now:
            self._sleep(start - now)

    def _get(self, url: str, endpoint: str, params: dict, accept: str, nokia: bool) -> str:
        attempt = 0
        while True:
            self._wait_turn()
            with self._lock:
                self.stats.requests += 1
                if nokia:
                    self.stats.nokia_requests += 1
            try:
                return self._fetch(url, accept, self.timeout_s)
            except urllib.error.HTTPError as e:
                retryable = e.code >= 500 or e.code == 429
                message = f"HTTP {e.code} {e.reason}"
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
                retryable = True
                message = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            if not retryable or attempt >= self.cfg.max_retries:
                raise ApiError(endpoint, params, message)
            attempt += 1
            backoff = 2.0 * 2 ** (attempt - 1)
            log.warning("%s failed (%s); retry %d in %.0fs", endpoint, message, attempt, backoff)
            self._sleep(backoff)

    def get_json(self, endpoint: str, **params: Any) -> Any:
        """GET a Smart Nicosia endpoint and return parsed JSON. Raises ApiError."""
        url = sn.build_url(f"{self.cfg.base_url}/{endpoint}", sn.normalize_params(params))
        body = self._get(url, endpoint, params, "application/json", nokia=True)
        try:
            data = json.loads(body)
        except ValueError as e:
            raise ApiError(endpoint, params, f"invalid JSON: {e}; body starts {body[:120]!r}")
        try:
            return sn.raise_for_incoresoft_error(data)
        except IncoresoftError as e:
            raise ApiError(endpoint, params, str(e), kind="incoresoft") from e

    def get_text(self, url: str, name: str, accept: str = "application/xml") -> str:
        """GET a non-Nicosia URL (the national traffic feed). Raises ApiError."""
        return self._get(url, name, {}, accept, nokia=False)

    # -- endpoint helpers ----------------------------------------------------------------------

    def latest_telemetry(self, device: str, keys: tuple[str, ...] | None = None) -> list[dict]:
        return self.get_json(
            "getLatestTelemetry",
            entityType="DEVICE",
            entityName=device,
            keys=",".join(keys) if keys else None,
        )

    def latest_attributes(self, device: str) -> list[dict]:
        return self.get_json("getLatestAttribute", entityType="DEVICE", entityName=device)

    def telemetry_range(
        self, entity_type: str, entity_name: str, keys: tuple[str, ...], start_ms: int, end_ms: int
    ) -> list[dict]:
        return self.get_json(
            "getTelemetryRange",
            entityType=entity_type,
            entityName=entity_name,
            keys=",".join(keys),
            startTs=start_ms,
            endTs=end_ms,
        )

    def range_pages(
        self,
        entity_type: str,
        entity_name: str,
        keys: tuple[str, ...],
        start_ms: int,
        end_ms: int,
        *,
        max_pages: int | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[list[dict]]:
        """Page getTelemetryRange backwards from end_ms to start_ms (newest records first)."""
        yield from page_backwards(
            lambda s, e: self.telemetry_range(entity_type, entity_name, keys, s, e),
            start_ms,
            end_ms,
            cap=self.cfg.range_cap,
            max_pages=max_pages,
            stop=stop,
        )


def page_backwards(
    fetch_page: Callable[[int, int], list[dict]],
    start_ms: int,
    end_ms: int,
    *,
    cap: int = 1000,
    max_pages: int | None = None,
    stop: Callable[[], bool] | None = None,
) -> Iterator[list[dict]]:
    """Yield pages of a newest-first, capped range query until the range is exhausted.

    The API returns at most `cap` records, shared across keys, newest first, and both startTs and
    endTs are inclusive. The next page ends at the oldest timestamp seen (not oldest - 1): a full
    page can cut a timestamp's records in half, and asking for that timestamp again (duplicates are
    dropped on insert) keeps the rest. If a whole page has one timestamp we step past it.
    """
    end = end_ms
    pages = 0
    while end >= start_ms:
        if stop is not None and stop():
            return
        records = fetch_page(start_ms, end)
        pages += 1
        yield records
        if len(records) < cap:
            return
        stamps = [int(r["timestamp"]) for r in records]
        oldest = min(stamps)
        end = oldest if max(stamps) > oldest and oldest < end else oldest - 1
        if max_pages is not None and pages >= max_pages:
            return

"""Small HTTP helpers for the public services the chatbot uses (standard library only)."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class WebError(Exception):
    """A public service failed or timed out. The message is safe to show the model."""


class Throttle:
    """At most one request per `interval_s` (Nominatim's usage policy is 1 req/s)."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval_s
        if start > now:
            time.sleep(start - now)


def request(
    url: str,
    *,
    user_agent: str,
    params: dict | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
    accept: str = "application/json",
) -> str:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": user_agent, "Accept": accept}
    )
    host = urllib.parse.urlsplit(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
        raise WebError(f"{host} answered HTTP {e.code}: {body}".strip()) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
        raise WebError(f"{host} unreachable: {getattr(e, 'reason', e)}") from None


def get_json(url: str, *, user_agent: str, params: dict | None = None,
             data: bytes | None = None, timeout: float = 30.0) -> Any:
    body = request(url, user_agent=user_agent, params=params, data=data, timeout=timeout)
    try:
        return json.loads(body)
    except ValueError:
        host = urllib.parse.urlsplit(url).netloc
        raise WebError(f"{host} returned invalid JSON: {body[:120]!r}") from None

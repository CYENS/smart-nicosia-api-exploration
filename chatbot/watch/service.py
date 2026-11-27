"""The twin's watch: it looks at the city whenever fresh data arrives and says what is wrong.

`Watcher` (one per Agent) runs in a background thread:

- Fresh device readings (CityData tells it after every snapshot, which the live strip asks for
  every few minutes while a page is open) are checked against the watches at once.
- At most once a minute, and at least every five, it reads the twin's memory through the
  detectors, groups what they find with the city's alarms into incidents (`Book`), saves them,
  and checks the incident watches.
- While a page is listening (and the full collector isn't running here, which fetches them
  anyway), it asks the city for its active alarms and its event feed every five minutes: two
  requests, recorded into the memory like everything else the chatbot fetches. It reads the
  other feeds its detectors need (`feeds`: the road reports) on the same terms, each at its own
  pace.
- Everything that changes goes to the open pages through `Hub`, a server-sent events stream.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from collector import parse
from collector.http import ApiError

from chatbot import context
from chatbot.city import KINDS, CityData
from chatbot.memory import Memory, Unavailable
from chatbot.watch import detectors
from chatbot.watch.incidents import Book, streets_near
from chatbot.watch.watches import Watches

log = logging.getLogger(__name__)

EVALUATE_EVERY_S = 300.0  # the detectors run at least this often...
EVALUATE_GAP_S = 60.0  # ...and at most this often
FETCH_EVERY_S = 300.0  # the city's alarms and event feed, while a page listens
SETTLE_S = 2.0  # snapshots of several kinds arrive together: wait for the rest


class Hub:
    """The open pages listening on /api/watch/stream. `publish` may be called from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subs.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(lp, x) for lp, x in self._subs if x is not q]

    @property
    def listening(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, event)
            except RuntimeError:  # that page's loop has closed
                self.unsubscribe(q)


def _offer(q: asyncio.Queue, event: dict) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:  # a page that stopped reading misses events, not the server
        pass


class Watcher:
    def __init__(self, city: CityData | None, memory: Memory | None,
                 now: Callable[[], datetime] | None = None):
        self.city = city
        self.memory = memory
        self.now = now or (city.now if city is not None else (lambda: datetime.now(timezone.utc)))
        self.book = Book(memory, self.now)
        self.watches = Watches(memory, self.now)
        self.hub = Hub()
        self._lock = threading.Lock()  # one evaluation at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._fresh: set[str] = set()
        # Other feeds the detectors read from the memory, refreshed while a page listens (each
        # throttles itself): the road reports (chatbot/mobility/reports.py), set by the Agent.
        self.feeds: list[Callable[[], None]] = []
        self._thread: threading.Thread | None = None
        self._last_eval = 0.0
        self._last_fetch = 0.0
        self.evaluated_at: datetime | None = None
        self.last_error: str | None = None
        self.last_took_s: float | None = None
        if city is not None and hasattr(city, "listeners"):
            city.listeners.append(self.fresh_readings)

    # -- the loop --------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="twin-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def fresh_readings(self, device_type: str) -> None:
        """CityData fetched a fresh snapshot of a type."""
        with self._lock:
            self._fresh.add(device_type)
        self._wake.set()

    def _run(self) -> None:
        time.sleep(3.0)  # let the server finish starting
        while not self._stop.is_set():
            woke = self._wake.wait(30.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            if woke:
                time.sleep(SETTLE_S)
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the watch must outlive any one look
                log.exception("watch: tick failed")

    def tick(self, force: bool = False) -> dict:
        """One look: fresh readings against the watches, then (when due) the detectors."""
        with self._lock:
            fresh, self._fresh = self._fresh, set()
        events: list[dict] = []
        for device_type in fresh:
            events += self.check_watches(device_type)
        due = time.time() - self._last_eval
        report = {"watch_fires": len(events), "evaluated": False}
        if force or due >= EVALUATE_EVERY_S or (fresh and due >= EVALUATE_GAP_S):
            if not force:
                self.fetch_city_alarms()
                self.read_feeds()
            changes = self.evaluate()
            report |= {"evaluated": True, "changes": len(changes)}
        for e in events:
            self.hub.publish(e)
        return report

    # -- the city's alarms -----------------------------------------------------------------------

    def fetch_city_alarms(self, force: bool = False) -> bool:
        """The active alarms and the event feed since the last look, into the memory. Only
        while a page listens, and never when the full collector runs here (it fetches them)."""
        if self.city is None or self.memory is None or self.memory.recorder is None:
            return False
        if self.memory.collecting:
            return False
        if not force and (self.hub.listening == 0 or time.time() - self._last_fetch < FETCH_EVERY_S):
            return False
        self._last_fetch = time.time()
        try:
            context.alarms(self.city, 20)  # recorded by the city's recorder
        except ApiError as e:
            log.info("watch: alarms unavailable: %s", e)
        now_ms = self.city.now_ms()
        start = now_ms - 2 * 3_600_000
        try:
            records: list[dict] = []
            for page in self.city.client.range_pages("TENANT", "CYTA", ("toastDesc",), start,
                                                      now_ms, max_pages=2):
                records.extend(page)
            self.memory.recorder.events(parse.parse_events(records))
        except ApiError as e:
            log.info("watch: event feed unavailable: %s", e)
        return True

    def read_feeds(self) -> None:
        """The other feeds the detectors read (the road reports), while a page listens."""
        if self.hub.listening == 0:
            return
        for read in self.feeds:
            try:
                read()
            except Exception:  # noqa: BLE001 - one feed never stops the watch
                log.exception("watch: a feed failed")

    # -- detectors and incidents ------------------------------------------------------------------

    def evaluate(self) -> list[dict]:
        """Read the memory through the detectors and update the incidents. Publishes what
        changed and returns it."""
        started = time.time()
        self._last_eval = started
        now = self.now()
        if self.memory is not None and self.memory.recorder is not None:
            self.memory.recorder.flush(timeout=2.0)  # what was just fetched counts too
        try:
            obs = detectors.observe(self.memory, self.city, now)
        except Unavailable as e:
            self.last_error = str(e)
            self.book.load()
            return []
        found, ran = detectors.detect(obs)
        changes = self.book.update(found, now, streets_near(obs), ran)
        self.book.save()
        fires = self.watches.check_incidents(changes)
        self.evaluated_at = now
        self.last_error = None
        self.last_took_s = round(time.time() - started, 2)
        counts = self.book.counts()
        for c in changes:
            if c["change"] in ("updated", "history"):
                continue
            self.hub.publish({"type": "incident", "change": c["change"], "incident": c["incident"],
                              "counts": counts})
        if any(c["change"] in ("updated", "history") for c in changes):
            self.hub.publish({"type": "incidents", "counts": counts})
        for f in fires:
            self.hub.publish(f)
        return changes

    def check_watches(self, device_type: str) -> list[dict]:
        if self.city is None:
            return []
        kind = next((k for k, t in KINDS.items() if t == device_type), None)
        if kind is None:
            return []
        states = list(self.city.cached_states(device_type).values())
        return self.watches.check_states(kind, states) if states else []

    def acted(self, incident: dict) -> None:
        """Someone acknowledged, snoozed or resolved an incident: tell the other pages."""
        self.hub.publish({"type": "incident", "change": "acted", "incident": incident,
                          "counts": self.book.counts()})

    def status(self) -> dict:
        return {"evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
                "took_s": self.last_took_s, "error": self.last_error or self.book.last_error,
                "listening": self.hub.listening,
                "next_in_s": max(0, round(EVALUATE_EVERY_S - (time.time() - self._last_eval)))}


def states_of(city: CityData | None, kind: str):
    """The live states of a kind for a watch proposal (one snapshot, as the strip would)."""
    if city is None or kind not in KINDS:
        return []
    try:
        return city.states(KINDS[kind])
    except ApiError as e:
        log.info("watch: states for %s unavailable: %s", kind, e)
        return list(city.cached_states(KINDS[kind], max_age_s=3600).values())


SNOOZES = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}

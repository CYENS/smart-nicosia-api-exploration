"""Live mobility: buses (the timetable and the live feed), road speeds, and what joins them.

One `Mobility` per process (on the Agent), shared by the chat tools, the page's endpoints and
the inspector, so every page and every answer reads the same fetch of each feed:

- `timetable()`: the static GTFS of NPT, Intercity and Pame Express (gtfs.py), loaded in the
  background on first use and refreshed daily at most;
- `fleet`: the GTFS-RT vehicles joined to it (realtime.py), fetched at most every 30 s;
- `roads`: the Bluetooth road segments with their speeds (roads.py), fetched at most every
  5 minutes and recorded into the twin's memory;
- departures and walk-ride-walk trips (planner.py).
"""

from __future__ import annotations

import logging
import socket
import urllib.error
import urllib.request
import urllib.parse
from collections import Counter
from datetime import datetime, timezone

from chatbot.mobility import planner
from chatbot.mobility.gtfs import Timetable, TimetableSource, line_order
from chatbot.mobility.realtime import Fleet, Snapshot
from chatbot.mobility.roads import Roads, slowest
from chatbot.web import WebError

log = logging.getLogger(__name__)


def fetch_bytes(url: str, user_agent: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    host = urllib.parse.urlsplit(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise WebError(f"{host} answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
        raise WebError(f"{host} unreachable: {getattr(e, 'reason', e)}") from None


class TimetableLoading(WebError):
    """The timetable isn't loaded yet (the first start downloads about 5 MB)."""


class Mobility:
    def __init__(self, settings, memory=None, router=None, *, fetch=None, now=None,
                 cache_dir=None):
        ua = settings.user_agent
        self.fetch = fetch or (lambda url: fetch_bytes(url, ua, 45.0))
        # The live feed answers in about a second; don't hold a page for long when it hangs.
        fetch_live = fetch or (lambda url: fetch_bytes(url, ua, 12.0))
        self.now = now or (lambda: datetime.now(timezone.utc))
        kw = {"cache_dir": cache_dir / "gtfs"} if cache_dir else {}
        self.source = TimetableSource(self.fetch, **kw)
        # The first fetch waits for the timetable, so no bus is drawn without its line.
        self.fleet = Fleet(fetch_live, lambda: self.source.get(20), now=self.now)
        self.roads = Roads(lambda url: self.fetch(url).decode("utf-8"), memory=memory,
                           router=router, now=self.now, cache_dir=cache_dir)
        self.memory = memory

    # -- the timetable ---------------------------------------------------------------------

    def timetable(self, wait_s: float = 0.0) -> Timetable | None:
        return self.source.get(wait_s)

    def require_timetable(self, wait_s: float = 25.0) -> Timetable:
        tt = self.source.get(wait_s)
        if tt is None:
            raise TimetableLoading(self.source.error or "the bus timetable is still loading; "
                                                        "try again in a minute")
        return tt

    def warm_up(self) -> None:
        self.source.get()

    # -- buses -----------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        return self.fleet.snapshot()

    def buses(self, lines: set[str] | None = None) -> tuple[Snapshot, list[dict]]:
        snap = self.snapshot()
        out = [b for b in snap.vehicles if not lines or b.get("line_id") in lines]
        return snap, out

    def by_line(self, buses: list[dict]) -> list[dict]:
        """Buses running per line, busiest first, with how late they run."""
        tt = self.timetable()
        groups: dict[str, list[dict]] = {}
        for b in buses:
            if b.get("finished"):
                continue
            groups.setdefault(b.get("line_id") or "?", []).append(b)
        rows = []
        for lid, bs in groups.items():
            line = tt.lines.get(lid) if tt else None
            delays = [b["delay_s"] for b in bs if b.get("delay_s") is not None]
            rows.append({"id": lid, "line": line.short if line else bs[0].get("line", "?"),
                         "name": line.name if line else None,
                         "operator": line.operator if line else None,
                         "color": line.color if line else bs[0].get("color"),
                         "buses": len(bs), "late": sum(1 for d in delays if d >= 180),
                         "mean_delay_min": round(sum(delays) / len(delays) / 60, 1) if delays else None})
        rows.sort(key=lambda r: (-r["buses"], line_order(tt.lines[r["id"]]) if tt and r["id"] in tt.lines else (9,)))
        return rows

    def bus(self, bus_id: str) -> dict | None:
        bid = bus_id.strip().lower()
        return next((b for b in self.snapshot().vehicles if b["id"].lower() == bid), None)

    # -- summary for the strip and the Mobility workspace -------------------------------------

    def summary(self) -> dict:
        out: dict = {}
        try:
            snap = self.snapshot()
            running = [b for b in snap.vehicles if not b.get("finished")]
            out["buses"] = {"status": snap.status, "running": len(running),
                            "lines": len({b.get("line_id") for b in running}),
                            "late": sum(1 for b in running if (b.get("delay_s") or 0) >= 180),
                            "by_line": self.by_line(running)[:12], "error": snap.error,
                            "as_of": datetime.fromtimestamp(snap.fetched_at, timezone.utc).isoformat()}
        except Exception as e:  # noqa: BLE001 - one feed failing leaves the other readouts
            out["buses"] = {"status": "unavailable", "error": str(e)[:200], "running": 0}
        try:
            sp = self.roads.speeds()
            rows = sp["segments"]
            counts = Counter(r["state"] for r in rows)
            out["roads"] = {"status": sp["status"], "measured": sp["measured"], "total": sp["total"],
                            "by_state": dict(counts), "slowest": slowest(rows, 5), "error": sp.get("error")}
        except Exception as e:  # noqa: BLE001
            out["roads"] = {"status": "unavailable", "error": str(e)[:200]}
        return out


# The mobility kinds of the twin's entities (bus, bus_stop, bus_line, road_segment).
from chatbot.mobility import entities as _kinds  # noqa: E402,F401

__all__ = ["Mobility", "TimetableLoading", "planner", "fetch_bytes"]

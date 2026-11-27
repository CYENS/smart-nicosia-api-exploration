"""Living sky and CyNAP ITS service shared across routes, tools and the inspector."""

from __future__ import annotations

from datetime import datetime, timezone

from chatbot import web
from chatbot.settings import CACHE_DIR, Settings
from chatbot.sky.aircraft import AircraftTracker
from chatbot.sky.bus_archive import BusArchiver
from chatbot.sky.iss import ISSTracker
from chatbot.sky.its import ITSInventory


class SkyService:
    def __init__(self, settings: Settings, memory=None, mobility=None, *,
                 fetch_json=None, fetch_text=None, now=None, cache_dir=None):
        ua = settings.user_agent
        f_json = fetch_json or (lambda url: web.get_json(url, user_agent=ua, timeout=12.0))
        f_text = fetch_text or (lambda url: web.request(url, user_agent=ua, timeout=20.0, accept="*/*"))
        cdir = cache_dir or CACHE_DIR
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.memory = memory
        self.aircraft = AircraftTracker(f_json, memory=memory, now=self.now)
        self.iss = ISSTracker(f_text, cache_dir=cdir, now=self.now)
        self.its = ITSInventory(f_text, cache_dir=cdir)
        self.bus_archiver = (
            BusArchiver(mobility, memory)
            if (settings.record and memory is not None and mobility is not None)
            else None
        )

    def start(self) -> None:
        if self.bus_archiver is not None:
            self.bus_archiver.start()

    def stop(self) -> None:
        if self.bus_archiver is not None:
            self.bus_archiver.stop()

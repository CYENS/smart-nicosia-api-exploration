"""FastAPI app: serves the map and chat page, behind an optional shared password. The API
routes live in chatbot/routes/, one router per area."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from chatbot.agent import Agent
from chatbot.city import CityData
from chatbot.citytiles import CityTiles
from chatbot.dls import DLS
from chatbot.eoa import Registry
from chatbot.geo import Places
from chatbot.memory import Memory
from chatbot.osm3d import OsmBuildings
from chatbot.overture import Overture
from chatbot.routes import routers
from chatbot.routing import Router
from chatbot.settings import ROOT, STATIC_DIR, load_settings
from chatbot.tools import vega_lite_validator

log = logging.getLogger("chatbot")


def _warm_up(agent: Agent) -> None:
    """Load the slow, cacheable things in the background so the first question is quick."""
    # The traffic cameras (one request, kept 6 h) let search and the inspector find them by name.
    # The bus timetable loads in the background (about 5 MB the first time, then from disk).
    for step in (agent.mobility.warm_up, agent.city.overview, agent.city.traffic_cameras,
                 lambda: vega_lite_validator(agent.settings)):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - best effort
            log.warning("warm-up step failed: %s", e)
    # Overpass is often at capacity: retry with growing pauses (Photon covers place search meanwhile).
    for pause in (0, 60, 180, 600, 1800):
        time.sleep(pause)
        agent.places.refresh_if_needed()
        if agent.places.ready:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(ROOT / ".env")
    settings = load_settings()
    dls = DLS(settings)
    registers = Registry(settings, dls)
    memory = Memory(settings.memory_path, record=settings.record)
    overture = Overture()  # the second places index, from the last copy on disk
    agent = Agent(settings, CityData(settings, recorder=memory.recorder),
                  Places(settings, overture=overture), Router(settings), dls=dls,
                  registers=registers, memory=memory)
    app.state.agent = agent
    # The 3D city's vector tiles (DLS buildings, OpenStreetMap's where the DLS has none).
    app.state.city_tiles = CityTiles(dls, OsmBuildings(settings))
    if settings.city_fill:
        app.state.city_tiles.start()
    if memory.recorder is not None:
        memory.recorder.start()
    agent.watcher.start()
    # The measuring stations: the Met file every 10 minutes, the EEA files every 20 (each comes
    # down again only once it has changed), recorded in the memory for the clock.
    agent.environment.measured.start()
    if settings.collect:
        start_collector(memory)
    log.info("twin's memory: %s (recording %s, full collection %s)", memory.path,
             "on" if memory.recorder else "off", "on" if settings.collect else "off")
    threading.Thread(target=_warm_up, args=(agent,), daemon=True).start()
    # Overture's places: a newer release is looked for at most monthly (a minute or two to cut).
    threading.Thread(target=overture.watch, daemon=True).start()
    # The registers refresh daily; the first build (parcels, then 3D heights) takes ~2 minutes.
    threading.Thread(target=registers.refresh_if_needed, daemon=True).start()
    yield
    app.state.city_tiles.stop()
    agent.watcher.stop()
    agent.environment.measured.stop()
    if memory.recorder is not None:
        memory.recorder.stop()


def start_collector(memory: Memory) -> None:
    """Run the full collector (collector/scheduler.py) in this process, with its own cadences
    and request budget. A `collector run` elsewhere holds the run lock, and then this one
    skips its passes rather than polling twice."""
    from dataclasses import replace

    from collector.config import load_config
    from collector.scheduler import estimate_budget, run_pass

    cfg = replace(load_config(), db_path=memory.path, log_file=None)
    per_day = sum(b.requests_per_day for b in estimate_budget(cfg) if b.nokia)
    log.info("full collection on: about %d city API requests a day (budget %d)", per_day,
             cfg.daily_request_budget)
    memory.collecting = True

    def loop() -> None:
        while True:
            started = time.monotonic()
            try:
                run_pass(cfg)
            except Exception:  # noqa: BLE001 - the next pass tries again
                log.exception("collector pass failed")
            time.sleep(max(1.0, 30.0 - (time.monotonic() - started)))

    threading.Thread(target=loop, name="twin-collector", daemon=True).start()


def password_ok(authorization: str | None, password: str) -> bool:
    """Whether an `Authorization: Basic ...` header carries the password (any username)."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, given = base64.b64decode(token, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(given.encode(), password.encode())


class SharedPassword:
    """Puts the whole site behind one shared password (HTTP Basic) when CHATBOT_PASSWORD is set,
    so a hosted copy doesn't let anyone spend the API key. The browser asks once and then sends
    it with every request, the chat stream included."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        password = os.environ.get("CHATBOT_PASSWORD")
        if scope["type"] == "http" and password:
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if not password_ok(auth, password):
                deny = PlainTextResponse("Password required.", status_code=401, headers={
                    "WWW-Authenticate": 'Basic realm="Nicosia digital twin", charset="UTF-8"'})
                await deny(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(title="Nicosia digital twin", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=4096)  # the register layer and 3D tiles are big
app.add_middleware(SharedPassword)  # added last, so it runs first
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    # Always revalidated: it names the hashed bundles of the current build.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


for router in routers():
    app.include_router(router)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    settings = load_settings()
    import uvicorn

    print(f"Nicosia digital twin: http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()

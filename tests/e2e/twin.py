"""The twin without its backend, for the browser tests.

`PageServer` serves the built page (chatbot/static) on a random local port, and writes the canned
chat streams (streams.py) as real server-sent events, a few milliseconds apart. Everything else
the page asks for is answered by `Twin`, a Playwright route handler: `/api/*` from the recorded
fixtures, the base map from small offline stand-ins, and every other host is blocked. Nothing
leaves the machine, and no question reaches Claude.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import BrowserContext, Route

from . import streams

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "chatbot" / "static"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
META = json.loads((FIXTURES / "meta.json").read_text(encoding="utf-8"))
EVENT_GAP_S = 0.03  # between two events of a stream
HOLD_MAX_S = 30  # a held stream gives up after this long


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def empty_fc() -> dict:
    return {"type": "FeatureCollection", "features": []}


# -- the page and the streams ----------------------------------------------------------------------

class _Handler(SimpleHTTPRequestHandler):
    server: "PageServer"

    def log_message(self, *args) -> None:  # quiet
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        # As chatbot/server.py serves it: the page at /, its files under /static.
        path = urllib.parse.urlsplit(path).path
        if path == "/":
            return str(STATIC / "index.html")
        if path.startswith("/static/"):
            return super().translate_path(path.removeprefix("/static"))
        return str(STATIC / "__missing__")

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        url = urllib.parse.urlsplit(self.path)
        if not url.path.startswith("/__stream/"):
            self.send_error(404)
            return
        name = url.path.removeprefix("/__stream/")
        q = dict(urllib.parse.parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        opening = [{"type": "conversation", "id": streams.CID, "run": q.get("run", "r1")},
                   {"type": "turn", "index": int(q.get("index", 0))}]
        closed = False
        try:
            for ev in [*opening, *streams.events(name, q.get("q", "")), {"type": "done"}]:
                if ev is streams.HOLD:
                    self._hold()
                    return
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                time.sleep(EVENT_GAP_S)
        except (BrokenPipeError, ConnectionResetError):
            closed = True
        finally:
            self.server.finished.append({"run": q.get("run"), "stream": name, "closed_by_page": closed})

    def _hold(self) -> None:
        """Keep the stream open, with a comment line now and then, until the page closes it."""
        end = time.monotonic() + HOLD_MAX_S
        while time.monotonic() < end:
            self.wfile.write(b": still working\n\n")
            self.wfile.flush()
            time.sleep(0.1)
        raise TimeoutError("a held stream was never closed by the page")


class PageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        if not (STATIC / "index.html").exists():
            raise RuntimeError(f"{STATIC} has no index.html: build the page (npm run build in chatbot/web)")
        super().__init__(("127.0.0.1", 0), partial(_Handler, directory=str(STATIC)))
        self.finished: list[dict] = []  # each stream once it ends: whether the page closed it
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self) -> None:
        self.shutdown()
        self.server_close()


# -- the base map, offline -------------------------------------------------------------------------

# A style with only a background, so the map loads without OpenFreeMap. The page's own layers
# (devices, answers' layers, the 3D city) draw over it as usual.
BASE_STYLE = {
    "version": 8,
    "name": "e2e",
    "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
    "sources": {},
    "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#0e1828"}}],
}
TILEJSON = {
    "tilejson": "3.0.0", "minzoom": 0, "maxzoom": 14, "vector_layers": [],
    "tiles": ["https://tiles.openfreemap.org/e2e/{z}/{x}/{y}.pbf"],
}


# -- the API ---------------------------------------------------------------------------------------

class Twin:
    """Answers the page's requests for one test, and keeps what the page asked for."""

    def __init__(self, server: PageServer) -> None:
        self.server = server
        self.chats: list[dict] = []  # each /api/chat request: message, replace_turn, context, stream
        self.stops: list[str] = []  # each /api/stop/{cid}/{run}
        self.unknown: list[str] = []  # /api calls the fixtures don't cover (answered 404)
        self.blocked: list[str] = []  # outside hosts the page tried
        self.turns = 0
        self.fail: set[str] = set()  # endpoints (by path prefix) to answer with an error

    def install(self, context: BrowserContext) -> None:
        context.route("**/*", self._route)

    # -- dispatch

    def _route(self, route: Route) -> None:
        url = urllib.parse.urlsplit(route.request.url)
        if url.netloc == urllib.parse.urlsplit(self.server.url).netloc:
            if url.path.startswith("/api/"):
                self._api(route, url)
            else:
                route.continue_()
        elif url.netloc == "tiles.openfreemap.org":
            self._basemap(route, url.path)
        else:
            self.blocked.append(route.request.url)
            route.abort("blockedbyclient")

    def _basemap(self, route: Route, path: str) -> None:
        if path.startswith("/styles/"):
            route.fulfill(json=BASE_STYLE)
        elif path.endswith(".pbf"):  # tiles and glyphs: empty is valid for both
            route.fulfill(status=200, body=b"", content_type="application/x-protobuf")
        else:  # the tile set's TileJSON
            route.fulfill(json=TILEJSON)

    def _api(self, route: Route, url: urllib.parse.SplitResult) -> None:
        path = url.path
        q = dict(urllib.parse.parse_qsl(url.query))
        method = route.request.method
        if any(path.startswith(p) for p in self.fail):
            route.fulfill(status=503, json={"detail": "made to fail by the test"})
            return
        if path == "/api/chat" and method == "POST":
            self._chat(route)
            return
        if path.startswith("/api/stop/") and method == "POST":
            self.stops.append(path.removeprefix("/api/stop/"))
            route.fulfill(json={"stopped": True})
            return
        if path.startswith("/api/reset/"):
            self.turns = 0
            route.fulfill(json={"ok": True})
            return
        if path == "/api/watch/stream":
            # One hello, and a long retry so the page doesn't reconnect during a test.
            hello = {"type": "hello", "counts": fixture("watch/incidents")["counts"]}
            route.fulfill(status=200, content_type="text/event-stream",
                          body=f"retry: 3600000\ndata: {json.dumps(hello)}\n\n")
            return
        try:
            body = self._get(path, q)
        except FileNotFoundError:
            body = None
        if body is None:
            self.unknown.append(f"{method} {path}{'?' + url.query if url.query else ''}")
            route.fulfill(status=404, json={"detail": "not in the e2e fixtures"})
        else:
            route.fulfill(json=body)

    def _get(self, path: str, q: dict):  # noqa: C901, PLR0911, PLR0912 - one table of endpoints
        past = "at" in q
        parts = path.removeprefix("/api/").split("/")
        head = parts[0]
        if path == "/api/health":
            return fixture("health")
        if path == "/api/overview":
            return fixture("overview_past" if past else "overview")
        if path == "/api/sources":
            return fixture("sources")
        if path == "/api/clock":
            return fixture("clock")
        if path == "/api/clock/states":
            return fixture(f"clock_states/{q.get('kind', '')}")
        if head == "devices" and len(parts) == 2:
            return fixture(f"devices/{parts[1]}") if (FIXTURES / "devices" / f"{parts[1]}.json").exists() else empty_fc()
        if path == "/api/alarms":
            return fixture("alarms")
        if path == "/api/lamps":
            return fixture("lamps_past" if past else "lamps")
        if path == "/api/dangerous":
            return fixture("dangerous")
        if head == "buildings3d":
            return empty_fc()
        if path == "/api/mobility":
            return fixture("mobility")
        if path == "/api/buses":
            return fixture("buses")
        if path == "/api/roads":
            return fixture("roads")
        if path == "/api/transit/stops":
            return fixture("transit/stops")
        if path.startswith("/api/transit/shapes/"):
            return {**fixture("transit/shape"), "id": urllib.parse.unquote(parts[-1])}
        if path == "/api/entities/search":
            return self._search(q.get("q", ""))
        if head == "entities" and len(parts) == 3 and parts[2] == "series":
            return fixture(f"entities/{parts[1]}_series")
        if head == "entities" and len(parts) == 2:
            return fixture(f"entities/{parts[1]}_past" if past else f"entities/{parts[1]}")
        if head == "history" and len(parts) == 2:
            return fixture(f"entities/{parts[1]}_history")
        if head == "layers" and len(parts) == 3:
            return streams.LAYERS.get(parts[2])
        if path == "/api/incidents":
            body = fixture("watch/incidents")
            status = q.get("status", "unresolved")
            if status not in ("unresolved", "all"):
                body["incidents"] = [r for r in body["incidents"] if r["status"] == status]
            return body
        if path == "/api/incidents/marks":
            return fixture("watch/marks")
        if head == "incidents" and len(parts) == 2:
            return fixture(f"watch/{parts[1]}")
        if path == "/api/watches":
            return fixture("watch/watches")
        return None

    def _search(self, text: str) -> dict:
        body = fixture("search")
        t = text.strip().lower()
        body["results"] = [r for r in body["results"] if t in r["id"].lower() or t in r["name"].lower()]
        body["query"] = text
        return body

    # -- the conversation

    def _chat(self, route: Route) -> None:
        req = route.request.post_data_json or {}
        replace = req.get("replace_turn")
        if replace is None:
            index = self.turns
            self.turns += 1
        else:
            index = replace
            self.turns = replace + 1
        name = streams.pick(req.get("message", ""), req.get("context"))
        run = f"run{len(self.chats) + 1}"
        self.chats.append({**req, "stream": name, "run": run, "index": index})
        q = urllib.parse.urlencode({"index": index, "run": run, "q": req.get("message", "")})
        route.continue_(url=f"{self.server.url}/__stream/{name}?{q}")

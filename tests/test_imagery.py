"""DLS aerial photos as map tiles (chatbot/imagery.py and its route), offline: real tiles captured
from DLS on 26 Nov 2026 stand in for the service."""

import asyncio
import urllib.parse
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from chatbot.imagery import EMPTY_PNG, PHOTOS, Imagery, in_cover, sniff, tile_mercator_bounds
from chatbot.routes import imagery as imagery_routes
from chatbot.settings import Settings
from chatbot.terrain import decode_png
from chatbot.web import WebError
from conftest import FIXTURES

JPG_2019 = (FIXTURES / "dls_ortho2019_18_155365_103681.jpg").read_bytes()  # Eleftheria Square
EDGE_2019 = (FIXTURES / "dls_ortho2019_edge.png").read_bytes()  # where the photo ends at the Green Line
JPG_1963 = (FIXTURES / "dls_ortho1963_18_155365_103681.jpg").read_bytes()
X, Y, Z = 155365, 103681, 18  # a z18 tile on Eleftheria Square


class FakeDls:
    def __init__(self, answers):
        self.answers = answers  # url fragment -> (status, bytes) or an exception
        self.asked: list[str] = []

    def __call__(self, url: str):
        self.asked.append(url)
        for frag, out in self.answers.items():
            if frag in url:
                if isinstance(out, Exception):
                    raise out
                return out
        return 404, b""


def make(tmp_path, answers):
    fake = FakeDls(answers)
    return Imagery(Settings(), cache_dir=tmp_path, fetch=fake), fake


def test_the_photos_and_their_tiles():
    assert set(PHOTOS) == {"2019", "1993", "1963"}
    assert sniff(JPG_2019) == "image/jpeg" and sniff(EDGE_2019) == "image/png"
    assert sniff(b"<html>error</html>") is None and sniff(EMPTY_PNG) == "image/png"
    w, h, ch, pixels = decode_png(EMPTY_PNG)  # a real image: a broken one fails in the browser
    assert (w, h, ch) == (1, 1, 4) and pixels == b"\x00\x00\x00\x00"
    xmin, ymin, xmax, ymax = tile_mercator_bounds(Z, X, Y)
    assert round(xmax - xmin, 3) == round(ymax - ymin, 3) == 152.874  # a z18 tile is ~153 m
    assert in_cover(Z, X, Y) and not in_cover(Z, X + 3000, Y)  # Nicosia, then far out to sea


def test_2019_comes_from_the_tile_cache_once_then_from_disk(tmp_path):
    im, fake = make(tmp_path, {"/tile/18/103681/155365": (200, JPG_2019)})
    data, kind = im.tile("2019", Z, X, Y)
    assert data == JPG_2019 and kind == "image/jpeg"
    assert fake.asked == [f"{PHOTOS['2019'].url}/tile/18/103681/155365"]  # row, then column
    assert im.tile("2019", Z, X, Y) == (JPG_2019, "image/jpeg") and len(fake.asked) == 1
    again = Imagery(Settings(), cache_dir=tmp_path, fetch=FakeDls({}))  # a restart reads the disk
    assert again.cached("2019", Z, X, Y) == (JPG_2019, "image/jpeg")


def test_the_edge_of_the_photo_keeps_its_transparency(tmp_path):
    im, _ = make(tmp_path, {"/tile/": (200, EDGE_2019)})
    assert im.tile("2019", 17, 77683, 51832) == (EDGE_2019, "image/png")


def test_1963_and_1993_are_exported_in_web_mercator(tmp_path):
    im, fake = make(tmp_path, {"Imagery_Orthophoto_1963": (200, JPG_1963)})
    assert im.tile("1963", Z, X, Y) == (JPG_1963, "image/jpeg")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.asked[0]).query)
    assert q["bboxSR"] == ["3857"] and q["imageSR"] == ["3857"] and q["size"] == ["256,256"]
    box = [float(v) for v in q["bbox"][0].split(",")]
    assert box == pytest.approx(list(tile_mercator_bounds(Z, X, Y)), abs=0.01)


def test_no_photo_is_an_empty_tile_and_never_asked_again(tmp_path):
    im, fake = make(tmp_path, {})  # DLS answers 404: past the photo
    assert im.tile("2019", Z, X, Y) == (EMPTY_PNG, "image/png")
    assert im.tile("2019", Z, X, Y) == (EMPTY_PNG, "image/png") and len(fake.asked) == 1
    # Outside greater Nicosia and outside a photo's zoom levels, DLS isn't asked at all.
    assert im.tile("2019", Z, X + 3000, Y) == (EMPTY_PNG, "image/png")
    assert im.tile("1963", 20, X * 4, Y * 4) == (EMPTY_PNG, "image/png")
    assert len(fake.asked) == 1


def test_a_failure_is_said_and_nothing_is_kept(tmp_path):
    im, fake = make(tmp_path, {"/tile/": WebError("DLS imagery unreachable: timed out")})
    with pytest.raises(WebError):
        im.tile("2019", Z, X, Y)
    assert im.cached("2019", Z, X, Y) is None and im.failed == 1
    im2, _ = make(tmp_path, {"/export": (200, b"<html>Server error</html>")})
    with pytest.raises(WebError):
        im2.tile("1993", Z, X, Y)
    assert im2.cached("1993", Z, X, Y) is None
    with pytest.raises(KeyError):
        im.tile("2030", Z, X, Y)


def asgi_get(app, path):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": [], "http_version": "1.1", "scheme": "http",
             "server": ("test", 80), "client": ("test", 1), "root_path": ""}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_the_route_serves_tiles_with_their_type_and_a_long_cache(tmp_path):
    im, _ = make(tmp_path, {"/tile/18/103681/155365": (200, JPG_2019),
                            "/tile/18/103681/155366": WebError("DLS imagery unreachable")})
    app = FastAPI()
    app.include_router(imagery_routes.router)
    app.state.agent = SimpleNamespace(imagery=im)
    status, headers, body = asgi_get(app, f"/api/imagery/2019/{Z}/{X}/{Y}")
    assert status == 200 and body == JPG_2019 and headers["content-type"] == "image/jpeg"
    assert "max-age=2592000" in headers["cache-control"]
    assert asgi_get(app, f"/api/imagery/2019/{Z}/{X + 1}/{Y}")[0] == 502
    assert asgi_get(app, f"/api/imagery/1850/{Z}/{X}/{Y}")[0] == 404
    assert asgi_get(app, f"/api/imagery/2019/3/9/0")[0] == 404
    status, _, body = asgi_get(app, "/api/imagery")
    assert status == 200 and b'"1963"' in body

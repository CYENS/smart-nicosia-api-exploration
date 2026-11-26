"""Views from high places: the terrain tiles, the line of sight, find_high_places and view_from,
all offline (a real Terrarium tile from tests/fixtures, and a made-up landscape)."""

import math
import struct
import zlib
from datetime import datetime, timezone

import pytest

from chatbot import views as V
from chatbot.dls import centroid, lonlat_to_tile
from chatbot.geo import norm
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.terrain import (SIZE, ZOOM, Terrain, curvature_drop, decode_png, destination,
                             line_of_sight, terrarium)
from chatbot.tools import run_tool
from chatbot.tools.base import ToolContext
from chatbot.web import WebError
from conftest import FIXTURES

TILE = (FIXTURES / "terrarium_12_2427_1620.png").read_bytes()  # the centre of Nicosia, z12


# -- the tiles -------------------------------------------------------------------------------------


def encode_png(pixels: list[list[tuple[int, int, int]]], filters: list[int]) -> bytes:
    """A tiny 8-bit RGB PNG, each row written with the given filter (0 to 4)."""
    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else (b if pb <= pc else c)

    raw, prev = bytearray(), bytes(len(pixels[0]) * 3)
    for row, f in zip(pixels, filters):
        line = bytes(v for px in row for v in px)
        out = bytearray()
        for i, x in enumerate(line):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            pred = (0, a, b, (a + b) >> 1, paeth(a, b, c))[f]
            out.append((x - pred) & 255)
        raw += bytes([f]) + out
        prev = line

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", len(pixels[0]), len(pixels), 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def test_png_rows_decode_with_every_filter():
    pixels = [[((x * 37 + y * 11) % 256, (x * 5 + y * 90) % 256, (x * y * 7) % 256) for x in range(6)]
              for y in range(5)]
    w, h, ch, data = decode_png(encode_png(pixels, [0, 1, 2, 3, 4]))
    assert (w, h, ch) == (6, 5, 3)
    assert data == bytes(v for row in pixels for px in row for v in px)


def test_terrarium_pixels_are_metres_above_sea_level():
    # 128 * 256 + 150 + 128 / 256 - 32768 = 150.5 m
    png = encode_png([[(128, 150, 128), (127, 255, 0)]], [1])
    assert terrarium(png) == [150.5, -1.0]


def test_the_centres_tile_decodes_to_nicosias_ground(tmp_path):
    fetched = []

    def fetch(url):
        fetched.append(url)
        assert url.endswith(f"/terrarium/{ZOOM}/2427/1620.png")
        return TILE

    heights = terrarium(TILE)
    assert len(heights) == SIZE * SIZE and -10 < min(heights) and max(heights) < 400
    t = Terrain(Settings(), cache_dir=tmp_path, fetch=fetch)
    centre = t.elevation(*V.CENTRE)
    assert 140 < centre < 165  # Eleftheria Square stands about 150 m up
    # Kept on disk as it came: another Terrain reads it without asking again.
    again = Terrain(Settings(), cache_dir=tmp_path, fetch=lambda url: pytest.fail("fetched twice"))
    assert again.elevation(*V.CENTRE) == centre and len(fetched) == 1


def test_a_failing_terrain_server_is_said_plainly(tmp_path):
    def down(url):
        raise WebError("terrain tiles unreachable: timed out")

    t = Terrain(Settings(), cache_dir=tmp_path, fetch=down)
    with pytest.raises(WebError):
        t.elevation(*V.CENTRE)


# -- the line of sight -----------------------------------------------------------------------------


def test_a_ridge_hides_the_target():
    flat = [(d, 150.0) for d in range(0, 3001, 30)]
    ridge = [(d, 195.0 if 1450 <= d <= 1550 else 150.0) for d in range(0, 3001, 30)]
    eye, top = 151.7, 162.0
    assert line_of_sight(flat, eye, top) == {"visible": True}
    hidden = line_of_sight(ridge, eye, top)
    assert not hidden["visible"]
    assert hidden["blocked_by"]["what"] == "ground" and 1450 <= hidden["blocked_by"]["at_m"] <= 1550
    # From higher up the same ridge no longer hides it.
    assert line_of_sight(ridge, 260.0, top)["visible"]
    # A building near the eye hides it too, and is named.
    tall = line_of_sight(flat, eye, top, [(40, 150 + 14, "a 14 m building")])
    assert tall["blocked_by"]["what"] == "a 14 m building" and tall["blocked_by"]["at_m"] == 40


def test_the_earth_curves_away_on_a_far_line():
    # 20 km over flat ground: a 2 m target is below a 2 m eye's horizon, a 200 m one isn't.
    flat = [(d, 0.0) for d in range(0, 20001, 100)]
    assert curvature_drop(20000) > 20
    assert line_of_sight(flat, 2.0, 2.0)["blocked_by"]["what"] == "ground"
    assert line_of_sight(flat, 2.0, 200.0)["visible"]
    # The near slope of a rounded summit hides its very top, but it is the mountain in view.
    dome = [(d, 900 - 750 * (1 - math.exp(-((20000 - d) / 3300) ** 2 / 2))) for d in range(0, 20001, 100)]
    assert not line_of_sight(dome, 200.0, 900.0)["visible"]
    assert line_of_sight(dome, 200.0, 900.0, part_of_target_m=3000)["visible"]


# -- a made-up Nicosia -----------------------------------------------------------------------------

HILL = (35.1500, 33.4000)  # 60 m above the city, 4.4 km south-east of the centre
WEST = (35.1725, 33.3000)  # flat, behind a ridge running north-south at 33.33
STREET = (35.1600, 33.3615)  # 1.4 km south of the centre, a 20 m building just north of it
MOUNTAIN = (35.3000, 33.4000)  # the Kyrenia range


def ground(lat: float, lon: float) -> float:
    h = 150.0
    h += 60 * math.exp(-((lat - HILL[0]) ** 2 + ((lon - HILL[1]) * 0.82) ** 2) / (2 * 0.003 ** 2))
    if 35.15 < lat < 35.20 and abs(lon - 33.33) < 0.0012:
        h += 45
    h += 750 * math.exp(-((lat - MOUNTAIN[0]) ** 2 + ((lon - MOUNTAIN[1]) * 0.82) ** 2) / (2 * 0.03 ** 2))
    return h


class FakeTerrain(Terrain):
    def __init__(self, tmp_path):
        super().__init__(Settings(), cache_dir=tmp_path, fetch=lambda url: pytest.fail(url))
        self._tiles = {}

    def tile(self, x, y, z=ZOOM):
        if (z, x, y) not in self._tiles:
            n = 2 ** z * SIZE
            out = []
            for py in range(SIZE):
                lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y * SIZE + py + 0.5) / n))))
                for px in range(SIZE):
                    out.append(ground(lat, (x * SIZE + px + 0.5) / n * 360 - 180))
            self._tiles[(z, x, y)] = out
        return self._tiles[(z, x, y)]


def square(lat, lon, half_m, oid, h):
    dlat, dlon = half_m / 110574, half_m / (111320 * math.cos(math.radians(lat)))
    ring = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]
    return {"type": "Feature", "id": oid, "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"h": h, "e": h, "f": int(h // 3)}}


class FakeDLS:
    def __init__(self, buildings):
        self.buildings = buildings

    def tile(self, x, y):
        return [b for b in self.buildings if lonlat_to_tile(*centroid(b["geometry"])) == (x, y)]

    cached_tile = tile


SHACOLAS = (35.171852, 33.361525)
NORTH_OF_STREET = destination(*STREET, 0, 30)
PLACES = [
    {"id": "n7021261387", "category": "viewpoint", "name": "End of new route", "lat": HILL[0], "lon": HILL[1]},
    {"id": "n100", "category": "viewpoint", "name": "Sunset point", "lat": WEST[0], "lon": WEST[1]},
    {"id": "n12062676614", "category": "museum", "name": "Shacolas Observatory", "lat": SHACOLAS[0], "lon": SHACOLAS[1]},
    {"id": "n5942209785", "category": "viewpoint", "name": "Eleventh stage", "lat": 35.171861, "lon": 33.361511},
    {"id": "w1", "category": "park", "name": "Athalassa Park", "lat": HILL[0] + 0.002, "lon": HILL[1]},
    {"id": "w2", "category": "theatre", "name": "Pallas cinema-theatre", "name_local": "Κινηματοθέατρο Παλλάς",
     "lat": 35.173175, "lon": 33.357572},
]


class FakePlaces:
    ready = True
    fetched_at = 0.0

    def all(self):
        return PLACES

    def by_id(self, pid):
        return next((dict(p) for p in PLACES if p["id"] == pid), None)

    def named(self, text, near=None):
        return [dict(p) for p in PLACES if norm(p["name"]) == norm(text)]

    def geocode(self, text, limit=3):
        return []

    def find(self, *a, **k):
        return [], "test"

    def reverse(self, lat, lon):
        area = "Aglantzia" if lon > 33.38 else "Strovolos" if lat < 35.165 else "Old Nicosia"
        return {"neighbourhood": area, "municipality": "Nicosia"}


class FakeCity:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def cached_devices(self):
        return []


DAY = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)  # 12:00 in Nicosia
NIGHT = datetime(2026, 11, 26, 19, 0, tzinfo=timezone.utc)  # 21:00 in Nicosia


@pytest.fixture
def world(tmp_path):
    events = []
    dls = FakeDLS([square(*SHACOLAS, 20, 160440, 47.0), square(*NORTH_OF_STREET, 8, 900, 20.0)])
    ctx = ToolContext(Settings(), FakeCity(DAY), FakePlaces(), None, SessionStore(), events.append,
                      "c1", dls, terrain=FakeTerrain(tmp_path))
    return ctx, events


def test_high_places_are_ranked_by_view_and_named_plainly(world):
    ctx, events = world
    res, err = run_tool(ctx, "find_high_places", {"radius_m": 7000})
    assert not err, res
    rows = {r["id"]: r for r in res["places"]}
    hill, west, tower = rows["n7021261387"], rows["n100"], rows["n12062676614"]
    # Never a raw map label alone: where it is, and how high.
    assert hill["name"] == "Viewpoint in Aglantzia" and hill["mapped_as"] == "End of new route"
    assert "near Athalassa Park" in hill["where"] and "south-east of the centre" in hill["where"]
    assert 55 <= hill["above_centre_m"] <= 65 and hill["centre_in_sight"]
    assert hill["how_high"].startswith(f"{hill['elevation_m']} m above sea level")
    # The observatory is measured on its DLS building, and the viewpoint mapped in it is the same.
    assert tower["kind"] == "lookout" and tower["building_height_m"] == 47.0
    assert "at the top of a 47 m building" in tower["how_high"] and tower["mapped_as"] == "Eleventh stage"
    assert "n5942209785" not in rows and "w2" not in rows  # a theatre is not a θέα
    # The ridge hides the centre from the west.
    assert not west["centre_in_sight"] and west["view"].startswith("rising ground")
    assert west["name"] == "Viewpoint in Old Nicosia"
    order = [r["id"] for r in res["places"]]
    assert order.index("n7021261387") < order.index("n100")
    assert [r["rank"] for r in res["places"]] == list(range(1, len(order) + 1))
    # A layer and a dataset, linked by id.
    layer = ctx.store.layers[res["layer_ref"]]
    data = ctx.store.datasets[res["data_ref"]]
    assert data["link"] == {"key": "id"} and {f["properties"]["id"] for f in layer["features"]} == set(order)
    assert "terrain" in layer["meta"]["sources"] and "above_centre_m" in layer["meta"]["value_fields"]
    popup = next(f for f in layer["features"] if f["properties"]["id"] == "n7021261387")["properties"]["popup"]
    assert 'Mapped as "End of new route"' in popup


def test_view_from_a_high_place_says_what_is_in_sight(world):
    ctx, events = world
    found, _ = run_tool(ctx, "find_high_places", {"radius_m": 7000})
    res, err = run_tool(ctx, "view_from", {"from": "n7021261387"})
    assert not err, res
    cmd = events[-1]
    assert cmd["type"] == "ui" and cmd["action"] == "view"
    v = cmd["view"]
    assert v["name"] == "Viewpoint in Aglantzia" and v["height_m"] == 1.7
    assert v["eye_m"] == pytest.approx(v["ground_m"] + 1.7, abs=0.11) and v["ground_m"] > 200
    assert 300 < v["bearing"] < 330 and res["looking"] == "north-west"  # towards the centre
    assert v["toward"]["in_sight"] and res["toward"]["distance_m"] > 4000
    marks = {m["title"]: m for m in v["landmarks"]}
    assert marks["The city centre"]["visible"] and marks["The Kyrenia mountains"]["visible"]
    assert "Shacolas Tower" in marks
    assert any("in sight" in f for f in res["facts"]) and "now" in res["when"]
    # Next view goes through the other places of the same answer, in their order.
    assert [t["id"] for t in v["tour"]] == [r["id"] for r in found["places"]]
    assert res["next_views"] == len(found["places"]) - 1


def test_buildings_by_the_eye_hide_the_centre_until_you_climb(world):
    ctx, events = world
    spot = f"{STREET[0]},{STREET[1]}"
    res, err = run_tool(ctx, "view_from", {"from": spot})
    assert not err and not res["toward"]["in_sight"], res["facts"]
    assert any("a 20 m building 30 m away hides the centre" in f.lower() for f in res["facts"])
    assert any("From a roof or higher up it may open up" in f for f in res["facts"])
    res, err = run_tool(ctx, "view_from", {"from": spot, "height": 40})
    assert not err and res["toward"]["in_sight"] and res["from"]["above_ground_m"] == 40
    res, err = run_tool(ctx, "view_from", {"from": spot, "height": "roof"})
    assert err and "no building at that spot" in res["error"]
    # On the roof of the building itself, its own walls don't hide anything.
    res, err = run_tool(ctx, "view_from", {"from": f"{NORTH_OF_STREET[0]},{NORTH_OF_STREET[1]}",
                                           "height": "roof"})
    assert not err and res["from"]["above_ground_m"] == 21.7 and res["toward"]["in_sight"]


def test_a_direction_and_the_centre_itself(world):
    ctx, events = world
    res, err = run_tool(ctx, "view_from", {"from": "the city centre"})
    assert not err and res["looking"] == "north" and "toward" not in res  # to the mountains
    res, err = run_tool(ctx, "view_from", {"from": "Shacolas Observatory", "toward": "south-west"})
    assert not err and res["looking"] == "south-west" and events[-1]["view"]["bearing"] == 225


def test_at_night_in_daylight_is_last_night_and_says_so(world):
    ctx, events = world
    res, err = run_tool(ctx, "view_from", {"from": "n7021261387", "when": "at night"})
    assert not err, res
    v = events[-1]["view"]
    assert v["at"] == "2026-11-25T21:00" and v["dark"] is True
    assert "last night at 21:00" in v["when"] and "daylight" in v["when"]
    assert "2026-11-25T21:00" in res["clock"]
    res, err = run_tool(ctx, "view_from", {"from": "n7021261387", "when": "last night 02:30"})
    assert not err and events[-1]["view"]["at"] == "2026-11-26T02:30"


def test_at_night_after_dark_is_live(world):
    ctx, events = world
    ctx.city = FakeCity(NIGHT)
    res, err = run_tool(ctx, "view_from", {"from": "n7021261387", "when": "at night"})
    assert not err and events[-1]["view"]["at"] is None and events[-1]["view"]["dark"] is True
    assert "clock" not in res
    res, err = run_tool(ctx, "view_from", {"from": "n7021261387", "when": "2099-01-01T21:00"})
    assert err and "future" in res["error"]

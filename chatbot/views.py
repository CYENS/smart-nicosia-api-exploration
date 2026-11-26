"""Views from high places: where in Nicosia you can look out over the city, and what you see
from any spot at eye level, on a roof or higher up.

Heights come from the terrain tiles (chatbot/terrain.py) and the DLS 3D buildings; places from
the OpenStreetMap index, the places found on the web and the twin's other entities. `find_high`
ranks viewpoints, public rooftops and the high ground of the terrain itself by how good their
view of the city is; `view` works out one view: how high the eye is, where it looks, whether the
city centre can be seen or what hides it, the landmarks in sight, and the camera the page flies
to. The chat tools (chatbot/tools/views.py) and the page's own endpoint (chatbot/routes/views.py)
both use these, so a shared link shows the view the answer described.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from collector.sun import solar_elevation

from chatbot.dls import centroid, contains, lonlat_to_tile
from chatbot.geo import norm
from chatbot.settings import CACHE_DIR, NICOSIA_CENTER
from chatbot.terrain import (SIZE, ZOOM, bearing_deg, compass, destination, distance_m,
                             line_of_sight, tile_xy)
from chatbot.web import WebError

if TYPE_CHECKING:
    from chatbot.terrain import Terrain
    from chatbot.tools.base import ToolContext

log = logging.getLogger(__name__)

CENTRE = NICOSIA_CENTER  # Eleftheria Square, where Ledras Street meets the walls
CENTRE_NAME = "the city centre"
EYE_M = 1.7  # a standing person's eyes above their feet
CENTRE_TOP_M = 12.0  # seeing the centre: its rooftops and lit streets, about four floors up
CENTRE_AREA_M = 200  # the centre is an area: its own blocks and towers are part of what is seen
PLACE_TOP_M = 10.0
IN_CENTRE_M = 450  # this close, a place is in the centre rather than looking at it
MAX_HEIGHT_M = 300
NIGHT_HOUR = 21
STEP_M = 30.0  # line-of-sight samples, about one terrain pixel apart
ROOF_CHECK_M = 1500  # buildings near the eye are checked against the line of sight
# Places whose name says the public can look out from up high (an observatory floor, a rooftop
# bar, a terrace).
LOOKOUT_NAME = re.compile(
    r"observ|roof ?top|roof garden|sky ?bar|skyline|panoram|lookout|terrace|ταράτσα|παρατηρητ"
    r"|\bθέας?\b", re.IGNORECASE)  # θέα, a view, but not θέατρο, a theatre
PLAIN_VIEWPOINT = re.compile(r"observ|lookout|view|panoram|hill|\bθέας?\b|λόφος", re.IGNORECASE)
NEAR_KINDS = ("park", "attraction", "monument", "historic_site", "museum", "place_of_worship")
# What a place in a tall building can be named by (not a bench or a cash machine in its hall).
NAMING_KINDS = ("museum", "attraction", "hotel", "theatre", "gallery", "restaurant", "bar", "cafe",
                "hospital")
KIND_STATES = {"viewpoint": "viewpoint", "lookout": "public lookout", "high_ground": "high ground"}
DIRECTIONS = {
    "n": 0, "north": 0, "nne": 22.5, "ne": 45, "north east": 45, "northeast": 45, "ene": 67.5,
    "e": 90, "east": 90, "ese": 112.5, "se": 135, "south east": 135, "southeast": 135,
    "sse": 157.5, "s": 180, "south": 180, "ssw": 202.5, "sw": 225, "south west": 225,
    "southwest": 225, "wsw": 247.5, "w": 270, "west": 270, "wnw": 292.5, "nw": 315,
    "north west": 315, "northwest": 315, "nnw": 337.5,
}
CENTRE_WORDS = {"centre", "center", "the centre", "the center", "the city centre",
                "the city center", "city centre", "city center", "old town", "the old town",
                "downtown", "eleftheria square", "town centre", "town center"}
# The Kyrenia mountains, on the northern horizon from most of Nicosia: the highest ground of
# this box (lon_min, lat_min, lon_max, lat_max) is found in the terrain, not written down.
KYRENIA_RANGE = (33.05, 35.26, 33.75, 35.36)


class ViewError(Exception):
    """Something the caller (the model, or a link) can fix: a place not found, a bad height."""


# -- places --------------------------------------------------------------------------------------


@dataclass
class Spot:
    name: str
    lat: float
    lon: float
    id: str | None = None
    kind: str = "place"  # place, viewpoint, lookout, high_ground, building, you, centre
    roof_m: float | None = None  # the building it is on (DLS roof above ground), when known
    building_id: str | None = None
    extra: dict = field(default_factory=dict)


def centre_spot() -> Spot:
    return Spot("the city centre (Eleftheria Square)", CENTRE[0], CENTRE[1], kind="centre")


def direction_of(text: str | None) -> float | None:
    """A compass direction ('north-east', 'NE', 'south west') as a bearing, else None."""
    key = norm(text).replace("-", " ").replace("towards ", "").replace("to the ", "").strip()
    key = re.sub(r"\s+", " ", key)
    return float(DIRECTIONS[key]) if key in DIRECTIONS else None


def known_high_place(ctx: ToolContext, pid: str) -> tuple[Spot, list[dict]] | None:
    """A place from a find_high_places answer in this conversation, by its id, with the list it
    came in (newest answer first)."""
    for data in reversed(list(ctx.store.datasets.values())):
        if not data.get("high_places"):
            continue
        rows = data["rows"]
        for row in rows:
            if row["id"] == pid or norm(row["name"]) == norm(pid):
                return spot_of_row(row), rows
    return None


def spot_of_row(row: dict) -> Spot:
    return Spot(row["name"], row["lat"], row["lon"], id=row["id"], kind=row["kind"],
                roof_m=row.get("building_height_m"), building_id=row.get("building_id"),
                extra={"row": row})


def resolve_spot(ctx: ToolContext, text: str) -> Spot:
    """Any place the twin knows: a high place it listed, the user's location, an id (an OSM
    place, a web place, a DLS building, any entity with a centre), the centre, or a name."""
    from chatbot.entities import EntityNotFound
    from chatbot.tools.base import ME, ToolError, my_location, resolve_place
    from chatbot.tools.entities import entities_of

    text = (text or "").strip()
    if not text:
        raise ViewError("give a place")
    hit = known_high_place(ctx, text)
    if hit:
        return hit[0]
    if norm(text) in ME:
        me = my_location(ctx)
        return Spot("your location", me["lat"], me["lon"], kind="you")
    if norm(text) in CENTRE_WORDS:
        return centre_spot()
    m = re.fullmatch(r"\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*", text)
    if m:  # a spot by its coordinates (a shared link, a click on the map)
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ViewError("coordinates out of range")
        area = area_of(ctx, lat, lon, lookup=False)
        return Spot(f"a spot in {area}" if area else "this spot", lat, lon)
    if re.fullmatch(r"[A-Za-z]{1,6}-?[\w.-]{1,40}", text) and re.search(r"\d", text):
        try:
            ent = entities_of(ctx).get(text)
        except (EntityNotFound, WebError):
            ent = None
        if ent and ent.get("center"):
            lon, lat = ent["center"]
            spot = Spot(ent.get("name") or text, lat, lon, id=ent["id"], kind=ent.get("kind", "place"))
            if ent.get("kind") == "building_3d" and ent.get("footprints"):
                spot.roof_m = ent["footprints"][0]["height_m"]
                spot.building_id = ent["id"]
            return spot
    try:
        p = resolve_place(ctx, text)
    except ToolError as e:
        raise ViewError(str(e)) from None
    return Spot(p.get("name") or text, p["lat"], p["lon"], id=p.get("id"))


def building_under(ctx: ToolContext, lat: float, lon: float, fetch: bool = True) -> dict | None:
    """The DLS 3D building standing at a point: its own tile (fetched if need be, once) and the
    cached tiles around it, since a footprint's centre can lie in the next tile."""
    if ctx.dls is None:
        return None
    x, y = lonlat_to_tile(lon, lat)
    for k, (tx, ty) in enumerate([(x, y)] + [(x + dx, y + dy) for dx in (-1, 0, 1)
                                             for dy in (-1, 0, 1) if dx or dy]):
        try:
            feats = ctx.dls.tile(tx, ty) if k == 0 and fetch else ctx.dls.cached_tile(tx, ty)
        except WebError as e:
            log.info("DLS tile for a view failed: %s", e)
            feats = None
        for b in feats or []:
            if contains(b["geometry"], lon, lat):
                return b
    return None


# -- moments ---------------------------------------------------------------------------------------


@dataclass
class When:
    at: datetime | None  # None: now (live)
    dark: bool
    note: str  # what the card and the answer say about it


def _local(ctx: ToolContext, t: datetime) -> datetime:
    from chatbot.tools.base import TZ
    return t.astimezone(TZ)


def resolve_when(ctx: ToolContext, text: str | None) -> When:
    """'now', a past moment ('2026-11-21T21:00') or a night ('at night', 'last night 22:30').
    The clock shows only the past and now: a night asked for in daylight is last night's, and
    says so; asked for after dark, it is now."""
    from chatbot.moments import OutOfRange, label, parse_at

    now = ctx.city.now() if ctx.city is not None else datetime.now().astimezone()
    raw = (text or "").strip()
    key = norm(raw)
    if not key or key in ("now", "live", "today", "right now"):
        dark = solar_elevation(now) < -6
        return When(None, dark, "now, live")
    nightly = re.search(r"night|tonight|evening|dark|lights", key)
    hm = re.search(r"\b(\d{1,2})[:.h](\d{2})\b", key) or re.search(r"\b(\d{1,2}) ?(?:pm|am)\b", key)
    if nightly or (hm and not re.match(r"\d{4}-", key)):
        if hm:
            hour = int(hm.group(1)) % 24
            minute = int(hm.group(2)) if hm.re.groups > 1 and hm.group(2) else 0
            if "pm" in key and hour < 12:
                hour += 12
        else:
            hour, minute = NIGHT_HOUR, 0
        if solar_elevation(now) < -6 and not hm and "last" not in key and "yesterday" not in key:
            return When(None, True, "now: it is dark, so the lamps and the sky are live")
        local = _local(ctx, now)
        t = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "yesterday" in key or t > local - timedelta(minutes=1):
            t -= timedelta(days=1)
        if nightly and solar_elevation(t) >= -6:  # a summer evening still light at nine
            t = t.replace(hour=22, minute=0)
        try:
            at = parse_at(t.isoformat(), now)
        except OutOfRange as e:
            raise ViewError(f"when: {e}") from None
        dark = solar_elevation(t) < -6
        said = "last night" if dark else "yesterday"
        if local.date() == t.date():
            said = "tonight" if dark else "today"
        why = "" if solar_elevation(now) < -6 else " (it is daylight now, and the clock shows only the past and now)"
        return When(at, dark, f"{said} at {t.strftime('%H:%M')}, from the twin's memory{why}")
    try:
        at = parse_at(raw, now)
    except OutOfRange as e:
        raise ViewError(f"when: {e}") from None
    if at is None:
        return When(None, solar_elevation(now) < -6, "now, live")
    return When(at, solar_elevation(at) < -6, f"{label(at)}, from the twin's memory")


# -- heights and sight -----------------------------------------------------------------------------


def _roofs_near_line(ctx: ToolContext, terrain: Terrain, lat1: float, lon1: float,
                     lat2: float, lon2: float, skip: set[int]) -> list[tuple[float, float, str]]:
    """DLS buildings the line of sight crosses near the eye (cached tiles only: no request),
    as (distance from the eye, top above sea level, what)."""
    if ctx.dls is None:
        return []
    total = distance_m(lat1, lon1, lat2, lon2)
    upto = min(total, ROOF_CHECK_M)
    n = max(1, int(upto // 10))
    samples = []
    for i in range(1, n + 1):
        d = upto * i / n
        f = d / total if total else 0
        samples.append((d, lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f))
    tiles: dict[tuple[int, int], list[dict]] = {}
    for _, lat, lon in samples:
        key = lonlat_to_tile(lon, lat)
        if key not in tiles:
            tiles[key] = ctx.dls.cached_tile(*key) or []
    boxes = []
    for feats in tiles.values():
        for b in feats:
            if b["id"] in skip:
                continue
            ring = b["geometry"]["coordinates"][0] if b["geometry"]["type"] == "Polygon" \
                else b["geometry"]["coordinates"][0][0]
            xs, ys = [c[0] for c in ring], [c[1] for c in ring]
            boxes.append((min(xs), min(ys), max(xs), max(ys), b))
    out = []
    for d, lat, lon in samples:
        for w, s, e, n_, b in boxes:
            if w <= lon <= e and s <= lat <= n_ and contains(b["geometry"], lon, lat):
                h = b["properties"]["h"]
                out.append((d, terrain.elevation(lat, lon) + h, f"a {round(h)} m building"))
                break
    return out


def sight(ctx: ToolContext, terrain: Terrain, eye: tuple[float, float, float],
          target: tuple[float, float, float], skip: set[int] | None = None,
          roofs: bool = True, part_of_target_m: float = 0) -> dict:
    """Line of sight from an eye (lat, lon, metres above sea level) to the top of a target
    (lat, lon, metres above sea level), over the terrain and (with `roofs`) the DLS buildings
    near the eye whose tiles are cached. `skip` names buildings that are neither: the one the
    eye stands on, the one looked at."""
    lat1, lon1, eye_m = eye
    lat2, lon2, top_m = target
    prof = terrain.profile(lat1, lon1, lat2, lon2, STEP_M)
    near = _roofs_near_line(ctx, terrain, lat1, lon1, lat2, lon2, skip or set()) if roofs else []
    return line_of_sight(prof, eye_m, top_m, near, part_of_target_m)


def hidden_by(los: dict) -> str | None:
    """'a ridge 1.2 km away', 'a 14 m building 40 m away', or None when nothing hides it."""
    if los.get("visible", True):
        return None
    b = los["blocked_by"]
    at = b["at_m"]
    far = f"{at / 1000:.1f} km away" if at >= 1000 else f"{at} m away"
    if b["what"] == "ground":
        return f"rising ground {far}"
    return f"{b['what']} {far}"


def distance_text(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{round(m / 10) * 10:.0f} m"


def where_text(lat: float, lon: float) -> str:
    """'4.7 km north-east of the centre', or 'in the centre'."""
    d = distance_m(CENTRE[0], CENTRE[1], lat, lon)
    if d < IN_CENTRE_M:
        return "in the centre"
    return f"{distance_text(d)} {compass(bearing_deg(CENTRE[0], CENTRE[1], lat, lon))} of the centre"


# -- area names -------------------------------------------------------------------------------------

_areas_lock = threading.Lock()
_areas: dict[Path, dict[str, str | None]] = {}


def area_of(ctx: ToolContext, lat: float, lon: float, lookup: bool = True) -> str | None:
    """The neighbourhood or municipality a point is in ('Aglantzia'), from the address search,
    kept on disk beside the terrain tiles (about 100 m apart), or None when it can't be told."""
    path = (ctx.terrain.dir if ctx.terrain is not None else CACHE_DIR / "terrain") / "areas.json"
    key = f"{lat:.3f},{lon:.3f}"
    with _areas_lock:
        if path not in _areas:
            try:
                _areas[path] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _areas[path] = {}
        known = _areas[path]
        if key in known:
            return known[key]
    if not lookup or ctx.places is None or not hasattr(ctx.places, "reverse"):
        return None
    try:
        r = ctx.places.reverse(lat, lon)
    except WebError:
        return None
    area = None
    if r:
        area = r.get("neighbourhood") or r.get("municipality")
        if area and r.get("municipality") and r["municipality"] != area and len(area) > 28:
            area = r["municipality"]
    with _areas_lock:
        known[key] = area
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return area


def landmark_near(ctx: ToolContext, lat: float, lon: float, max_m: float = 500,
                  skip: str | None = None) -> dict | None:
    """The nearest named park, monument or landmark within `max_m`, to say where a point is."""
    if ctx.places is None or not getattr(ctx.places, "ready", False):
        return None
    best = None
    for p in ctx.places.all():
        if p["category"] not in NEAR_KINDS or not p.get("name") or p["id"] == skip:
            continue
        if abs(p["lat"] - lat) > 0.006 or abs(p["lon"] - lon) > 0.007:
            continue
        d = distance_m(lat, lon, p["lat"], p["lon"])
        if 40 <= d <= max_m and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


# -- high places ------------------------------------------------------------------------------------


def _pixel_lat_lon(gx: int, gy: int) -> tuple[float, float]:
    n = 2 ** ZOOM * SIZE
    lon = (gx + 0.5) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (gy + 0.5) / n))))
    return lat, lon


def hilltops(terrain: Terrain, lat: float, lon: float, radius_m: float,
             block_px: int = 8) -> list[tuple[float, float, float]]:
    """The high ground of the terrain within `radius_m`: (lat, lon, metres above sea level) of
    each point that is the highest of its block (about 250 m across) and of the blocks around
    it, highest first."""
    dlat = radius_m / 110574
    dlon = radius_m / (111320 * math.cos(math.radians(lat)))
    x0, y0 = tile_xy(lat + dlat, lon - dlon)
    x1, y1 = tile_xy(lat - dlat, lon + dlon)
    px0, py0, px1, py1 = int(x0 * SIZE), int(y0 * SIZE), int(x1 * SIZE), int(y1 * SIZE)
    blocks: dict[tuple[int, int], tuple[float, int, int]] = {}
    for gy in range(py0, py1 + 1):
        by = (gy - py0) // block_px
        ty, ry = divmod(gy, SIZE)
        gx = px0
        while gx <= px1:  # one run of a row at a time, within a tile and a block
            tx, rx = divmod(gx, SIZE)
            bx = (gx - px0) // block_px
            end = min(px1 + 1, (tx + 1) * SIZE, px0 + (bx + 1) * block_px)
            heights = terrain.tile(tx, ty)
            run = heights[ry * SIZE + rx:ry * SIZE + rx + (end - gx)]
            top = max(run)
            k = (bx, by)
            if k not in blocks or top > blocks[k][0]:
                blocks[k] = (top, gx + run.index(top), gy)
            gx = end
    out = []
    for (bx, by), (h, gx, gy) in blocks.items():
        if all(blocks.get((bx + dx, by + dy), (-1e9,))[0] <= h
               for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy):
            plat, plon = _pixel_lat_lon(gx, gy)
            if distance_m(lat, lon, plat, plon) <= radius_m:
                out.append((plat, plon, h))
    out.sort(key=lambda t: -t[2])
    return out


def prominence(terrain: Terrain, lat: float, lon: float, eye_m: float, ring_m: float = 1000) -> float:
    """How far the eye stands above the ground around it (the mean of eight points a km off):
    a hilltop or a tower looks out over its surroundings."""
    ring = [terrain.elevation(*destination(lat, lon, b, ring_m)) for b in range(0, 360, 45)]
    return eye_m - sum(ring) / len(ring)


def _candidates(ctx: ToolContext, terrain: Terrain, near: tuple[float, float],
                radius_m: float) -> list[dict]:
    """Viewpoints, public lookouts in tall buildings and hilltops within the radius."""
    out: list[dict] = []
    if ctx.places is not None and getattr(ctx.places, "ready", False):
        for p in ctx.places.all():
            d = distance_m(near[0], near[1], p["lat"], p["lon"])
            if d > radius_m:
                continue
            names = " ".join(x for x in (p.get("name"), p.get("name_local"), p.get("description")) if x)
            if p["category"] == "viewpoint":
                out.append({"kind": "viewpoint", "lat": p["lat"], "lon": p["lon"], "place": p})
            elif p.get("name") and LOOKOUT_NAME.search(names) and p["category"] != "parking_lot":
                out.append({"kind": "lookout", "lat": p["lat"], "lon": p["lon"], "place": p})
    # A viewpoint mapped inside a lookout's building is the same place: keep the lookout, and
    # what the viewpoint was mapped as.
    looks = [c for c in out if c["kind"] == "lookout"]
    kept = []
    for c in out:
        same = [q for q in looks if c["kind"] == "viewpoint"
                and distance_m(c["lat"], c["lon"], q["lat"], q["lon"]) < 60]
        if same:
            same[0].setdefault("also", []).append(c["place"])
        else:
            kept.append(c)
    out = kept
    for lat, lon, h in hilltops(terrain, near[0], near[1], radius_m)[:40]:
        if any(distance_m(lat, lon, c["lat"], c["lon"]) < 350 for c in out):
            continue
        out.append({"kind": "high_ground", "lat": lat, "lon": lon, "ground_m": h})
    return out


def _score(above_centre: float, prom: float, visible: bool, dist_m: float) -> float:
    """How good the view of the city is: high above the centre and above its surroundings,
    with the centre in sight, and not too far to make it out."""
    return round(min(above_centre, 120) + 0.5 * min(max(prom, -30), 80)
                 + (25 if visible else -15) - 4 * max(0.0, dist_m / 1000 - 6), 1)


def find_high(ctx: ToolContext, terrain: Terrain, near: tuple[float, float], radius_m: float,
              limit: int) -> list[dict]:
    """High places within the radius, best view first, each with plain words for where it is
    and how high."""
    centre_ground = terrain.elevation(*CENTRE)
    rows = []
    for c in _candidates(ctx, terrain, near, radius_m):
        lat, lon = c["lat"], c["lon"]
        ground = c.get("ground_m") or terrain.elevation(lat, lon)
        building = None
        if c["kind"] == "lookout":
            building = building_under(ctx, lat, lon)
            if building is None:  # a terrace we can't measure: no height to rank it by
                continue
        up = (building["properties"]["h"] if building else 0) + EYE_M
        eye = ground + up
        c.update(ground_m=ground, eye_m=eye, building=building,
                 above_centre=eye - (centre_ground + EYE_M),
                 dist=distance_m(CENTRE[0], CENTRE[1], lat, lon))
        c["prom"] = prominence(terrain, lat, lon, eye)
        rows.append(c)
    # The line of sight is the costly part: only for the likeliest.
    rows.sort(key=lambda c: -(min(c["above_centre"], 120) + 0.5 * c["prom"]))
    rows = rows[:max(limit * 3, 12)]
    for c in rows:
        if c["dist"] < IN_CENTRE_M:
            c["los"] = {"visible": True}
        else:
            skip = {c["building"]["id"]} if c["building"] else set()
            # Over the terrain only: the same answer whatever buildings happen to be cached.
            c["los"] = sight(ctx, terrain, (c["lat"], c["lon"], c["eye_m"]),
                             (CENTRE[0], CENTRE[1], centre_ground + CENTRE_TOP_M), skip,
                             roofs=False, part_of_target_m=CENTRE_AREA_M)
        c["score"] = _score(c["above_centre"], c["prom"], c["los"]["visible"], c["dist"])
    rows.sort(key=lambda c: -c["score"])
    return [_describe(ctx, c, rank) for rank, c in enumerate(rows[:limit], 1)]


def _describe(ctx: ToolContext, c: dict, rank: int) -> dict:
    """A ranked high place in plain words."""
    lat, lon = c["lat"], c["lon"]
    area = area_of(ctx, lat, lon)
    place = c.get("place")
    near = landmark_near(ctx, lat, lon, skip=place["id"] if place else None)
    where = where_text(lat, lon)
    in_area = f" in {area}" if area else ""
    if c["kind"] == "lookout":
        name = place["name"]
        h = c["building"]["properties"]["h"]
        how_high = f"at the top of a {round(h)} m building, {round(c['eye_m'])} m above sea level"
    elif c["kind"] == "viewpoint":
        raw = place.get("name")
        if raw and PLAIN_VIEWPOINT.search(raw):
            name = f"{raw}{in_area}"
        else:
            name = f"Viewpoint{in_area}" if area else (f"Viewpoint near {near['name']}" if near else "Viewpoint")
        how_high = f"{round(c['eye_m'])} m above sea level"
    else:
        name = f"High ground{in_area}" if area else (f"High ground near {near['name']}" if near else "High ground")
        how_high = f"{round(c['ground_m'])} m above sea level"
    above = round(c["above_centre"])
    how_high += f", {abs(above)} m {'above' if above >= 0 else 'below'} the centre"
    if c["dist"] < IN_CENTRE_M:
        sees = "in the centre itself, with the old town all around"
    elif c["los"]["visible"]:
        sees = "the centre is in sight over the terrain"
    else:
        sees = f"{hidden_by(c['los'])} hides the centre"
    prom = round(c["prom"])
    row = {
        "rank": rank, "id": _id_of(c), "name": name, "kind": c["kind"],
        "where": where + (f", near {near['name']}" if near and near["name"] not in name else ""),
        "how_high": how_high,
        "elevation_m": round(c["eye_m"]), "ground_m": round(c["ground_m"]),
        "above_centre_m": above, "above_surroundings_m": prom,
        "distance_to_centre_m": round(c["dist"]),
        "direction_to_centre": compass(bearing_deg(lat, lon, CENTRE[0], CENTRE[1])),
        "centre_in_sight": bool(c["los"]["visible"]), "view": sees, "score": c["score"],
        "lat": round(lat, 6), "lon": round(lon, 6),
    }
    if area:
        row["area"] = area
    if place:
        row["osm_id"] = place["id"]
        if place.get("name") and place["name"] != name:
            row["mapped_as"] = place["name"]
        if place.get("opening_hours"):
            row["opening_hours"] = place["opening_hours"]
    for other in c.get("also", []):
        if other.get("name"):
            row["mapped_as"] = other["name"]
    if c.get("building"):
        b = c["building"]
        row["building_id"] = f"B{b['id']}"
        row["building_height_m"] = b["properties"]["h"]
    if c["kind"] == "high_ground":
        row["access"] = "open ground from the terrain: the data says nothing about access"
    return row


def _id_of(c: dict) -> str:
    if c.get("place"):
        return c["place"]["id"]
    return f"hill-{c['lat']:.4f}-{c['lon']:.4f}".replace(".", "_")


# -- one view ----------------------------------------------------------------------------------------


def _eye_height(ctx: ToolContext, spot: Spot, height: str | float | None) -> tuple[float, str, dict | None]:
    """(metres above the ground, how it is said, the building under it)."""
    building = None
    if height in (None, "", "auto"):
        height = "roof" if spot.kind == "lookout" else "eye"
    if isinstance(height, (int, float)) or re.fullmatch(r"\d+(\.\d+)?( ?m)?", str(height).strip()):
        m = float(str(height).strip().rstrip("m ").strip()) if not isinstance(height, (int, float)) else float(height)
        if not 0 <= m <= MAX_HEIGHT_M:
            raise ViewError(f"height must be between 0 and {MAX_HEIGHT_M} m above the ground")
        return max(m, EYE_M), f"{round(m)} m above the ground", None
    key = norm(str(height))
    if key in ("eye", "eye level", "street", "ground"):
        return EYE_M, "at eye level", None
    if key in ("roof", "rooftop", "top", "the roof"):
        if spot.roof_m is not None:
            return spot.roof_m + EYE_M, f"on the roof, {round(spot.roof_m)} m up", None
        building = building_under(ctx, spot.lat, spot.lon)
        if building is None:
            raise ViewError("the DLS model has no building at that spot; use height 'eye' or "
                            "metres above the ground")
        h = building["properties"]["h"]
        return h + EYE_M, f"on the roof, {round(h)} m up", building
    raise ViewError("height is 'eye', 'roof' or metres above the ground (up to 300)")


def landmarks(ctx: ToolContext, terrain: Terrain, eye: tuple[float, float, float],
              target: Spot | None, skip_building: int | None) -> list[dict]:
    """Things to label in the view, each with whether it can be seen from the eye."""
    lat, lon, eye_m = eye
    marks: list[dict] = []
    if distance_m(lat, lon, *CENTRE) >= IN_CENTRE_M:
        marks.append({"title": "The city centre", "said": "the city centre", "lat": CENTRE[0],
                      "lon": CENTRE[1], "top": CENTRE_TOP_M, "kind": "centre"})
    if ctx.places is not None and getattr(ctx.places, "ready", False):
        for p in ctx.places.all():
            if p.get("name") and "shacolas" in norm(p["name"]):
                b = building_under(ctx, p["lat"], p["lon"], fetch=False)
                marks.append({"title": "Shacolas Tower", "lat": p["lat"], "lon": p["lon"],
                              "top": b["properties"]["h"] if b else PLACE_TOP_M, "kind": "tower",
                              **({"bid": b["id"]} if b else {})})
                break
    # The tallest DLS buildings around the centre, from what is cached (no request).
    if ctx.dls is not None:
        x, y = lonlat_to_tile(CENTRE[1], CENTRE[0])
        tall = []
        for tx in range(x - 2, x + 3):
            for ty in range(y - 2, y + 3):
                for b in ctx.dls.cached_tile(tx, ty) or []:
                    if b["properties"]["h"] >= 40 and b["id"] != skip_building:
                        tall.append(b)
        tall.sort(key=lambda b: -b["properties"]["h"])
        for b in tall[:4]:
            c = centroid(b["geometry"])
            if c and not any(distance_m(c[1], c[0], m["lat"], m["lon"]) < 80 for m in marks):
                h = round(b["properties"]["h"])
                named = _named_inside(ctx, b)
                marks.append({"title": f"{named}, {h} m" if named else f"{h} m tower",
                              "said": named or f"a {h} m tower", "lat": c[1], "lon": c[0],
                              "top": b["properties"]["h"], "kind": "tower", "id": f"B{b['id']}",
                              "bid": b["id"]})
    try:
        peak = terrain_peak(terrain, KYRENIA_RANGE)
    except WebError:
        peak = None
    if peak:
        marks.append({"title": "The Kyrenia mountains", "said": "the Kyrenia mountains",
                      "lat": peak[0], "lon": peak[1], "top": 0.0, "kind": "mountains",
                      "ground_m": peak[2]})
    if target is not None and target.kind != "centre":
        marks.append({"title": target.name, "lat": target.lat, "lon": target.lon,
                      "top": PLACE_TOP_M, "kind": "target"})
    out = []
    skip = {skip_building} if skip_building else set()
    for m in marks:
        d = distance_m(lat, lon, m["lat"], m["lon"])
        if d < 60:
            continue
        ground = m.get("ground_m") or terrain.elevation(m["lat"], m["lon"])
        los = sight(ctx, terrain, eye, (m["lat"], m["lon"], ground + m["top"]),
                    skip | ({m["bid"]} if m.get("bid") else set()),
                    part_of_target_m={"mountains": 3000, "centre": CENTRE_AREA_M}.get(m["kind"], 0))
        item = {"title": m["title"], "said": m.get("said") or m["title"], "kind": m["kind"],
                "lat": round(m["lat"], 6),
                "lon": round(m["lon"], 6), "ground_m": round(ground, 1),
                "top_m": round(ground + m["top"], 1), "distance_m": round(d),
                "bearing": round(bearing_deg(lat, lon, m["lat"], m["lon"]), 1),
                "visible": los["visible"]}
        if m.get("id"):
            item["id"] = m["id"]
        if not los["visible"]:
            item["hidden_by"] = hidden_by(los)
        out.append(item)
    return out


def _named_inside(ctx: ToolContext, building: dict) -> str | None:
    """The name of a museum, hotel, theatre or the like inside a building's footprint."""
    if ctx.places is None or not getattr(ctx.places, "ready", False):
        return None
    c = centroid(building["geometry"])
    if c is None:
        return None
    for p in ctx.places.all():
        if (p["category"] in NAMING_KINDS and p.get("name") and abs(p["lat"] - c[1]) < 0.002
                and abs(p["lon"] - c[0]) < 0.002 and contains(building["geometry"], p["lon"], p["lat"])):
            return p["name"]
    return None


def terrain_peak(terrain: Terrain, bbox: tuple[float, float, float, float],
                 z: int = 10) -> tuple[float, float, float] | None:
    """The highest point of the terrain in a box (lat, lon, metres), at a coarse zoom."""
    if bbox in terrain.peaks:
        return terrain.peaks[bbox]
    lon0, lat0, lon1, lat1 = bbox
    x0, y0 = tile_xy(lat1, lon0, z)
    x1, y1 = tile_xy(lat0, lon1, z)
    best = None
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            heights = terrain.tile(tx, ty, z)
            for i, h in enumerate(heights):
                gx, gy = tx * SIZE + i % SIZE, ty * SIZE + i // SIZE
                if not (x0 * SIZE <= gx <= x1 * SIZE and y0 * SIZE <= gy <= y1 * SIZE):
                    continue
                if best is None or h > best[0]:
                    best = (h, gx, gy)
    if best is None:
        terrain.peaks[bbox] = None
        return None
    n = 2 ** z * SIZE
    lon = (best[1] + 0.5) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (best[2] + 0.5) / n))))
    terrain.peaks[bbox] = (lat, lon, best[0])
    return terrain.peaks[bbox]


def view(ctx: ToolContext, terrain: Terrain, frm: Spot, *, toward: Spot | None = None,
         bearing: float | None = None, height: str | float | None = None,
         when: When | None = None, with_landmarks: bool = True) -> dict:
    """One view: the facts in words, and the camera for the page.

    Looks at `toward` (a place), along `bearing` (a compass direction), or at the city centre;
    from the centre itself, north across the old town to the mountains."""
    up, said, building = _eye_height(ctx, frm, height)
    ground = terrain.elevation(frm.lat, frm.lon)
    eye_m = ground + up
    centre_ground = terrain.elevation(*CENTRE)
    target = toward
    if target is None and bearing is None:
        if distance_m(frm.lat, frm.lon, *CENTRE) < IN_CENTRE_M:
            bearing = 0.0  # from the centre: across the old town to the Kyrenia mountains
        else:
            target = centre_spot()
    if target is not None:
        dist = distance_m(frm.lat, frm.lon, target.lat, target.lon)
        if dist < 30:
            raise ViewError("from and toward are the same place; give another place to look at "
                            "or a direction")
        bearing = bearing_deg(frm.lat, frm.lon, target.lat, target.lon)
        target_ground = terrain.elevation(target.lat, target.lon)
    skip_id = building["id"] if building else (int(frm.building_id[1:]) if frm.building_id else None)
    skip = {skip_id} if skip_id else set()
    rise = round(eye_m - centre_ground - EYE_M)
    level = ("level with the centre's streets" if rise == 0 else
             f"{abs(rise)} m {'above' if rise > 0 else 'below'} the centre's streets")
    facts = [f"From {frm.name}, {said}: {round(eye_m)} m above sea level, {level}."]
    out: dict = {
        "from": {"name": frm.name, "id": frm.id, "lat": round(frm.lat, 6), "lon": round(frm.lon, 6),
                 "ground_m": round(ground, 1), "eye_m": round(eye_m, 1),
                 "above_ground_m": round(up, 1),
                 "above_centre_m": round(eye_m - centre_ground - EYE_M, 1),
                 "where": where_text(frm.lat, frm.lon)},
        "looking": compass(bearing), "bearing": round(bearing, 1),
    }
    if building or frm.building_id:
        out["from"]["building_id"] = f"B{building['id']}" if building else frm.building_id
    los = None
    if target is not None:
        top = CENTRE_TOP_M if target.kind == "centre" else PLACE_TOP_M
        los = sight(ctx, terrain, (frm.lat, frm.lon, eye_m),
                    (target.lat, target.lon, target_ground + top), skip,
                    part_of_target_m=CENTRE_AREA_M if target.kind == "centre" else 0)
        out["toward"] = {"name": target.name, "id": target.id, "lat": round(target.lat, 6),
                         "lon": round(target.lon, 6), "ground_m": round(target_ground, 1),
                         "distance_m": round(dist), "in_sight": los["visible"]}
        name = "the centre" if target.kind == "centre" else target.name
        facts.append(f"Looking {compass(bearing)} at {name}, {distance_text(dist)} away.")
        if los["visible"]:
            facts.append(f"{name[0].upper()}{name[1:]} is in sight: nothing on the ground "
                         "between hides it" + (" (buildings near you were checked too)."
                                               if ctx.dls is not None else "."))
        else:
            b = los["blocked_by"]
            facts.append(f"{hidden_by(los)[0].upper()}{hidden_by(los)[1:]} hides {name}: it rises "
                         f"{b['by_m']} m above the line of sight."
                         + (" From a roof or higher up it may open up." if b["what"] != "ground"
                            and up < 10 else ""))
        out["line_of_sight"] = los
    else:
        facts.append(f"Looking {compass(bearing)}.")
    if with_landmarks:
        marks = landmarks(ctx, terrain, (frm.lat, frm.lon, eye_m), target, skip_id)
        out["landmarks"] = marks
        said = [m for m in marks if m["kind"] != "target"
                and not (m["kind"] == "centre" and target is not None and target.kind == "centre")]
        seen = [m["said"] for m in said if m["visible"]]
        unseen = [f"{m['said']} ({m['hidden_by']})" for m in said if not m["visible"]]
        if seen:
            facts.append("In sight: " + ", ".join(seen) + ".")
        if unseen:
            facts.append("Hidden: " + "; ".join(unseen) + ".")
    if when is not None:
        out["when"] = when.note
        out["dark"] = when.dark
    out["facts"] = facts
    return out


def camera(v: dict, when: When | None, *, look_around: bool = False) -> dict:
    """The page's part of a view: where the eye is, what it looks at, and the labels."""
    f = v["from"]
    cmd = {
        "id": f.get("id") or f"{f['lat']:.5f},{f['lon']:.5f}",
        "name": f["name"], "lat": f["lat"], "lon": f["lon"], "ground_m": f["ground_m"],
        "eye_m": f["eye_m"], "height_m": f["above_ground_m"],
        "above_centre_m": f["above_centre_m"], "bearing": v["bearing"],
        "landmarks": [{k: m[k] for k in ("title", "lat", "lon", "top_m", "visible", "distance_m")
                       if k in m} | ({"hidden_by": m["hidden_by"]} if m.get("hidden_by") else {})
                      for m in v.get("landmarks", [])],
        "look_around": look_around,
    }
    if f.get("building_id"):
        cmd["building_id"] = f["building_id"]
    if v.get("toward"):
        t = v["toward"]
        cmd["toward"] = {"name": t["name"], "lat": t["lat"], "lon": t["lon"],
                         "ground_m": t["ground_m"], "in_sight": t["in_sight"]}
    if when is not None:
        cmd["at"] = when.at.astimezone(_tz()).strftime("%Y-%m-%dT%H:%M") if when.at else None
        cmd["dark"] = when.dark
        cmd["when"] = when.note
    return cmd


def _tz():
    from chatbot.tools.base import TZ
    return TZ

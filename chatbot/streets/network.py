"""Nicosia's streets as a network, from OpenStreetMap: the drivable roads with their names,
classes and OSM's own speed tags, and the schools, in one Overpass query a week.

The national speed limit inventory gives points (a sign here says 50), not lines. To say which
stretch a sign governs, each sign is snapped to its street and the limit is carried along that
street (through junctions, as long as the street keeps its name) towards the next sign, at most
REACH_M. How sure it is, piece by piece:

- signed: within SURE_M of the sign, or between two signs that agree;
- likely: further along from one sign, with nothing saying otherwise;
- uncertain: between two signs that disagree, away from both: the limit changes somewhere
  there, and the inventory doesn't say where (nor which way each sign faces);
- mapped: no sign reaches it, but OpenStreetMap's contributors tagged its limit.

A street no sign reaches and OSM doesn't tag is left out: in towns the default is 50 km/h, but
that is the law's default, not something anyone measured or posted there.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import re
import threading
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field

from chatbot.geo import OVERPASS_MIRRORS, loose, norm
from chatbot.settings import CACHE_DIR
from chatbot.streets.feeds import AREA, latin
from chatbot.web import WebError, get_json

log = logging.getLogger(__name__)

NETWORK_TTL_S = 7 * 86400
ROADS = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
         "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
         "residential", "living_street")
MAIN = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
        "primary_link", "secondary_link", "tertiary_link"}
SNAP_M = 35.0  # a sign stands beside its road; further than this it isn't placed on one
REACH_M = 800.0  # how far along its street a sign's limit is carried, at most
SURE_M = 250.0
STEP_M = 10.0
KX = 111_320 * math.cos(math.radians(35.15))  # metres per degree of longitude in Nicosia
KY = 110_574.0
CELL = 0.002  # grid cell in degrees (about 180 m by 220 m)
# Which OSM classes a national road reference's letter fits (A motorways, B main roads, E
# secondary roads, F and G local ones): a sign between two roads goes to the one it is on.
REF_CLASSES = {
    "A": {"motorway", "motorway_link", "trunk", "trunk_link"},
    "B": {"trunk", "trunk_link", "primary", "primary_link", "secondary"},
    "E": {"primary", "secondary", "secondary_link", "tertiary", "unclassified"},
    "F": {"tertiary", "tertiary_link", "unclassified", "residential", "living_street"},
    "G": {"unclassified", "residential", "living_street", "tertiary"},
}
CLASS_LABELS = {"motorway": "Motorway", "trunk": "Main road", "primary": "Main road",
                "secondary": "Secondary road", "tertiary": "Local distributor",
                "unclassified": "Minor road", "residential": "Residential street",
                "living_street": "Living street"}


def query(area=AREA) -> str:
    lon0, lat0, lon1, lat1 = area
    bbox = f"{lat0},{lon0},{lat1},{lon1}"
    roads = "|".join(ROADS)
    return (f'[out:json][timeout:180];(way["highway"~"^({roads})$"]({bbox}););out body geom qt;'
            f'(nwr["amenity"~"^(school|kindergarten)$"]({bbox}););out center tags qt;')


def maxspeed(tag: str | None) -> int | None:
    """OSM's maxspeed as km/h ('50', '30 mph'); None for anything else ('CY:urban', 'signals')."""
    if not tag:
        return None
    m = re.fullmatch(r"\s*(\d{1,3})\s*(mph)?\s*", tag)
    if not m:
        return None
    v = int(m.group(1))
    return round(v * 1.609) if m.group(2) else v


def compact(data: dict) -> dict:
    """Overpass JSON -> what the twin keeps: ways as [id, name, name_en, class, maxspeed,
    oneway, ref, nodes, coords] and schools as dicts."""
    ways, schools = [], []
    for e in data.get("elements", []):
        tags = e.get("tags") or {}
        if e.get("type") == "way" and tags.get("highway") in ROADS:
            geom = e.get("geometry") or []
            nodes = e.get("nodes") or []
            if len(geom) < 2 or len(geom) != len(nodes) or any(g is None for g in geom):
                continue
            coords = [[round(g["lon"], 6), round(g["lat"], 6)] for g in geom]
            ways.append([e["id"], tags.get("name"), tags.get("name:en"), tags["highway"],
                         maxspeed(tags.get("maxspeed")), tags.get("oneway") in ("yes", "1", "-1"),
                         tags.get("ref"), nodes, coords])
        elif tags.get("amenity") in ("school", "kindergarten"):
            lat = e.get("lat", (e.get("center") or {}).get("lat"))
            lon = e.get("lon", (e.get("center") or {}).get("lon"))
            if lat is None or lon is None:
                continue
            schools.append({"id": f"{e['type'][0]}{e['id']}", "name": tags.get("name:en") or tags.get("name"),
                            "name_local": tags.get("name"), "kind": tags["amenity"],
                            "lat": round(lat, 6), "lon": round(lon, 6)})
    return {"ways": ways, "schools": schools,
            "osm_base": (data.get("osm3s") or {}).get("timestamp_osm_base")}


def xy(lon: float, lat: float) -> tuple[float, float]:
    return lon * KX, lat * KY


def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * KX, (a[1] - b[1]) * KY)


def cumulative(coords) -> list[float]:
    out = [0.0]
    for i in range(1, len(coords)):
        out.append(out[-1] + dist_m(coords[i - 1], coords[i]))
    return out


def slice_line(coords, cum, a: float, b: float) -> list[list[float]]:
    """The part of a line from a to b metres along it."""
    if b < a:
        a, b = b, a
    out = [point_at(coords, cum, a)]
    for i in range(1, len(coords) - 1):
        if a < cum[i] < b:
            out.append(list(coords[i]))
    out.append(point_at(coords, cum, b))
    return [[round(x, 6), round(y, 6)] for x, y in out]


def point_at(coords, cum, at: float) -> list[float]:
    at = max(0.0, min(at, cum[-1]))
    for i in range(1, len(cum)):
        if cum[i] >= at:
            span = cum[i] - cum[i - 1]
            t = (at - cum[i - 1]) / span if span else 0.0
            (x1, y1), (x2, y2) = coords[i - 1], coords[i]
            return [x1 + t * (x2 - x1), y1 + t * (y2 - y1)]
    return list(coords[-1])


def project(p, a, b) -> tuple[float, float]:
    """(t along a-b in 0..1, distance in metres) of point p from segment a-b."""
    ax, ay = xy(*a)
    bx, by = xy(*b)
    px, py = xy(*p)
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return t, math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def bearing(a, b) -> float:
    return math.degrees(math.atan2((b[0] - a[0]) * KX, (b[1] - a[1]) * KY)) % 180.0


def street_key(name: str | None) -> str | None:
    return norm(name) or None


@dataclass
class Link:
    """A stretch of one OSM way between two junctions."""
    idx: int
    way: int
    name: str | None
    name_en: str | None
    hw: str
    osm_kmh: int | None
    oneway: bool
    ref: str | None
    a: int  # node ids at each end
    b: int
    coords: list[list[float]]
    cum: list[float] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.cum[-1]

    @property
    def key(self) -> str | None:
        return street_key(self.name)

    @property
    def label(self) -> str | None:
        """The street's name as a reader would write it in English."""
        return self.name_en or latin(self.name) or self.ref


@dataclass
class Snap:
    link: int
    along: float
    dist: float
    point: list[float]


class Grid:
    """A grid of line segments for nearest-line lookups: key -> [(item, segment index)]."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)

    @staticmethod
    def cell(lon: float, lat: float) -> tuple[int, int]:
        return int(lon // CELL), int(lat // CELL)

    def add(self, item: int, coords) -> None:
        for i in range(len(coords) - 1):
            (x1, y1), (x2, y2) = coords[i], coords[i + 1]
            cx0, cy0 = self.cell(min(x1, x2), min(y1, y2))
            cx1, cy1 = self.cell(max(x1, x2), max(y1, y2))
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self.cells[(cx, cy)].append((item, i))

    def near(self, lon: float, lat: float) -> set[tuple[int, int]]:
        cx, cy = self.cell(lon, lat)
        out: set[tuple[int, int]] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.update(self.cells.get((cx + dx, cy + dy), ()))
        return out


class Network:
    """The drivable streets, split at junctions, with a grid to find the street at a point."""

    def __init__(self, ways: list, schools: list[dict] | None = None, osm_base: str | None = None):
        self.schools = schools or []
        self.osm_base = osm_base
        self.links: list[Link] = []
        self.adj: dict[int, list[int]] = defaultdict(list)
        uses: dict[int, int] = defaultdict(int)
        for w in ways:
            for k, n in enumerate(w[7]):
                uses[n] += 2 if 0 < k < len(w[7]) - 1 else 1
        for w in ways:
            wid, name, name_en, hw, osm_kmh, oneway, ref, nodes, coords = w
            start = 0
            for k in range(1, len(nodes)):
                if k == len(nodes) - 1 or uses[nodes[k]] > 2:
                    if k > start:
                        part = coords[start:k + 1]
                        link = Link(len(self.links), wid, name, name_en, hw, osm_kmh, oneway, ref,
                                    nodes[start], nodes[k], part)
                        link.cum = cumulative(part)
                        if link.length > 0.5:
                            self.links.append(link)
                            self.adj[link.a].append(link.idx)
                            self.adj[link.b].append(link.idx)
                    start = k
        self.grid = Grid()
        for link in self.links:
            self.grid.add(link.idx, link.coords)
        self._names: dict[str, list[int]] | None = None

    def candidates(self, lon: float, lat: float, max_m: float = SNAP_M) -> list[Snap]:
        """Every street within max_m of a point, nearest first, one per link."""
        best: dict[int, Snap] = {}
        for li, si in self.grid.near(lon, lat):
            link = self.links[li]
            a, b = link.coords[si], link.coords[si + 1]
            t, d = project((lon, lat), a, b)
            if d > max_m or (li in best and best[li].dist <= d):
                continue
            along = link.cum[si] + t * (link.cum[si + 1] - link.cum[si])
            best[li] = Snap(li, along, d, [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])])
        return sorted(best.values(), key=lambda s: s.dist)

    def snap(self, lon: float, lat: float, road_ref: str | None = None,
             max_m: float = SNAP_M) -> Snap | None:
        cands = self.candidates(lon, lat, max_m)
        if not cands:
            return None
        fits = REF_CLASSES.get((road_ref or "?")[0].upper())
        if fits:
            good = [c for c in cands if self.links[c.link].hw in fits and c.dist <= cands[0].dist + 12]
            if good:
                return good[0]
        return cands[0]

    def named(self, text: str) -> list[int]:
        """Links whose street is called `text` (any script, loose spelling): 'Makariou' finds
        'Λεωφόρος Αρχιεπισκόπου Μακαρίου Γ''."""
        q = loose(re.sub(r"\b(avenue|ave|street|st|road|rd|leoforos|odos|λεωφόρος|οδός)\b\.?", "",
                         text, flags=re.IGNORECASE)).strip()
        if len(q) < 3:
            return []
        if self._names is None:
            names: dict[str, list[int]] = defaultdict(list)
            for link in self.links:
                for n in {link.name, link.name_en}:
                    if n:
                        names[n].append(link.idx)
            self._names = dict(names)
        out: list[int] = []
        for n, idxs in self._names.items():
            if q in loose(n) or q in loose(n, eta="e"):
                out += idxs
        return sorted(set(out))


# -- what each sign governs ----------------------------------------------------------------------


def _continues(net: Network, from_link: Link, to_link: Link) -> bool:
    """Whether a street carries on from one link to the next at a junction: the same name, or
    for a street with no name, the same OSM way."""
    if from_link.key:
        return to_link.key == from_link.key
    return to_link.way == from_link.way


def _claims(net: Network, signs: list[dict], snaps: list[Snap | None], reach: float):
    """link -> [(sign index, how its distance runs: ('at', p) | ('from_a', d) | ('from_b', d))]."""
    claims: dict[int, list[tuple[int, str, float]]] = defaultdict(list)
    for si, snap in enumerate(snaps):
        if snap is None:
            continue
        start = net.links[snap.link]
        claims[start.idx].append((si, "at", snap.along))
        seen: dict[tuple[int, str], float] = {}
        # (distance at the node we leave from, node, link we came along)
        frontier = [(snap.along, start.a, start), (start.length - snap.along, start.b, start)]
        while frontier:
            d, node, came = frontier.pop()
            if d >= reach:
                continue
            for li in net.adj.get(node, ()):
                link = net.links[li]
                if link.idx == came.idx or not _continues(net, came, link):
                    continue
                end = "from_a" if link.a == node else "from_b"
                if seen.get((li, end), math.inf) <= d:
                    continue
                seen[(li, end)] = d
                claims[li].append((si, end, d))
                other = link.b if link.a == node else link.a
                frontier.append((d + link.length, other, link))
    return claims


def _distance(kind: str, base: float, x: float, length: float) -> float:
    if kind == "at":
        return abs(x - base)
    return base + (x if kind == "from_a" else length - x)


def infer(net: Network, signs: list[dict], reach: float = REACH_M) -> tuple[list[dict], list[Snap | None]]:
    """The stretches the signs govern, piece by piece, and where each sign was placed."""
    snaps = [net.snap(s["lon"], s["lat"], s.get("road_ref")) for s in signs]
    # Claims run twice the reach, so a piece can tell it lies between two signs that agree.
    claims = _claims(net, signs, snaps, 2 * reach)
    pieces: list[dict] = []
    for li, cl in claims.items():
        link = net.links[li]
        L = link.length
        n = max(1, math.ceil(L / STEP_M))
        xs = [L * k / n for k in range(n + 1)]
        states = []
        for x in xs:
            ds: dict[int, float] = {}
            side: dict[int, int] = {}  # -1: the sign lies towards the link's start, +1 its end
            for si, kind, base in cl:
                d = _distance(kind, base, x, L)
                if d < ds.get(si, math.inf):
                    ds[si] = d
                    side[si] = -1 if kind == "from_a" or (kind == "at" and base <= x) else 1
            s1 = min(ds, key=ds.get) if ds else None
            if s1 is not None and ds[s1] <= reach:
                v1, d1 = signs[s1]["kmh"], ds[s1]
                other = {si: d for si, d in ds.items() if signs[si]["kmh"] != v1 and d <= reach}
                sides = {side[si] for si in ds if signs[si]["kmh"] == v1}
                agree = 2 if len(sides) == 2 and not any(signs[si]["kmh"] != v1 for si in ds) else 1
                if other:
                    s2 = min(other, key=other.get)
                    d2 = other[s2]
                    if d1 <= 120 and d1 < 0.3 * (d1 + d2):
                        tier, alt = "signed", None
                    else:
                        tier, alt = "uncertain", signs[s2]["kmh"]
                elif d1 <= SURE_M or agree >= 2:
                    tier, alt = "signed", None
                else:
                    tier, alt = "likely", None
                states.append((s1, v1, tier, alt))
            else:
                states.append(None)
        # Runs of the same state, split halfway between the samples where it changes.
        start = 0.0
        for k in range(1, len(xs) + 1):
            if k < len(xs) and states[k] == states[k - 1]:
                continue
            end = (xs[k - 1] + xs[k]) / 2 if k < len(xs) else L
            if states[k - 1] is not None and end - start > 1.0:
                pieces.append(_piece(link, states[k - 1], signs, start, end))
            start = end
    # Where no sign reaches, OSM's own tag, a whole way at a time.
    claimed = set(claims)
    by_way: dict[int, list[Link]] = defaultdict(list)
    for link in net.links:
        if link.osm_kmh and link.idx not in claimed:
            by_way[link.way].append(link)
    for links in by_way.values():
        run: list[Link] = []
        for link in links + [None]:  # type: ignore[list-item]
            if link is not None and run and run[-1].b == link.a:
                run.append(link)
                continue
            if run:
                coords = [c for k, x in enumerate(run) for c in (x.coords if k == 0 else x.coords[1:])]
                pieces.append({"coords": coords, "kmh": run[0].osm_kmh, "tier": "mapped", "alt_kmh": None,
                               "sign": None, "street": run[0].label, "name_local": run[0].name,
                               "way": run[0].way, "hw": run[0].hw, "osm_kmh": run[0].osm_kmh,
                               "length_m": round(sum(x.length for x in run))})
            run = [link] if link is not None else []
    for k, p in enumerate(pieces):
        p["id"] = f"stretch-{k}"
    return pieces, snaps


def _piece(link: Link, state, signs, a: float, b: float) -> dict:
    s1, v1, tier, alt = state
    return {"coords": slice_line(link.coords, link.cum, a, b), "kmh": v1, "tier": tier, "alt_kmh": alt,
            "sign": signs[s1]["id"], "street": link.label, "name_local": link.name, "way": link.way,
            "hw": link.hw, "osm_kmh": link.osm_kmh, "length_m": round(b - a)}


class PieceIndex:
    """The limit pieces with a grid, to find the limit along a line or at a point."""

    def __init__(self, pieces: list[dict]):
        self.pieces = pieces
        self.grid = Grid()
        for k, p in enumerate(pieces):
            self.grid.add(k, p["coords"])

    def at(self, lon: float, lat: float, max_m: float = 25.0, heading: float | None = None) -> dict | None:
        """The piece nearest a point (parallel to `heading`, degrees, when given)."""
        best, best_d = None, max_m
        for k, si in self.grid.near(lon, lat):
            c = self.pieces[k]["coords"]
            _, d = project((lon, lat), c[si], c[si + 1])
            if d >= best_d:
                continue
            if heading is not None:
                diff = abs(bearing(c[si], c[si + 1]) - heading) % 180
                if min(diff, 180 - diff) > 30:
                    continue
            best, best_d = self.pieces[k], d
        return best

    def along(self, coords, step: float = 25.0, max_m: float = 20.0) -> dict | None:
        """The limit along a line (a road segment): its commonest posted value by length, how
        much of the line the pieces cover, and what else is posted on it."""
        cum = cumulative(coords)
        if not cum or cum[-1] < 1:
            return None
        n = max(2, int(cum[-1] // step))
        found: dict[int, float] = defaultdict(float)
        tiers: dict[int, set[str]] = defaultdict(set)
        signs: dict[int, set[str]] = defaultdict(set)
        hit = 0
        for k in range(n):
            at = cum[-1] * (k + 0.5) / n
            p = point_at(coords, cum, at)
            q = point_at(coords, cum, min(cum[-1], at + 5))
            r = point_at(coords, cum, max(0.0, at - 5))
            piece = self.at(p[0], p[1], max_m, bearing(r, q))
            if piece is None or piece["tier"] == "uncertain":
                continue
            hit += 1
            found[piece["kmh"]] += 1
            tiers[piece["kmh"]].add(piece["tier"])
            if piece.get("sign"):
                signs[piece["kmh"]].add(piece["sign"])
        if not found:
            return None
        kmh = max(found, key=found.get)
        order = ("signed", "likely", "mapped")
        tier = next(t for t in order if t in tiers[kmh])
        return {"kmh": kmh, "coverage": round(hit / n, 2), "share": round(found[kmh] / hit, 2),
                "tier": tier, "values": {str(v): round(c / hit, 2) for v, c in sorted(found.items())},
                "signs": sorted(signs[kmh]), "source": "openstreetmap" if tier == "mapped" else "speed_limits"}


# -- the network, fetched and kept --------------------------------------------------------------


class NetworkSource:
    """The street network, fetched from Overpass at most once a week (in the background) and
    kept on disk."""

    def __init__(self, settings, cache_dir=None, fetch=None):
        self.settings = settings
        self.path = (cache_dir or CACHE_DIR) / "streets" / "osm_network.json.gz"
        self._fetch = fetch  # fetch(query) -> Overpass JSON (tests pass their own)
        self._net: Network | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._running = False
        self.auto = False  # load and refresh on demand: set once the server starts the street rules
        self.error: str | None = None

    @property
    def fetched_at(self) -> float:
        return self._fetched_at

    def current(self) -> Network | None:
        """The network if it is loaded, never waiting; starts loading it when it isn't (once the
        server has started the street rules)."""
        if self.auto and (self._net is None or time.time() - self._fetched_at > NETWORK_TTL_S):
            self.load_async()
        return self._net

    def get(self, wait_s: float = 0.0) -> Network | None:
        net = self.current()
        if net is not None or wait_s <= 0:
            return net
        deadline = time.time() + wait_s
        while time.time() < deadline and self._net is None and self._running:
            time.sleep(0.2)
        return self._net

    def load_async(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._load, daemon=True, name="street-network").start()

    def load_now(self) -> Network | None:
        with self._lock:
            self._running = True
        self._load()
        return self._net

    def _load(self) -> None:
        try:
            data = None
            if self.path.exists():
                try:
                    data = json.loads(gzip.decompress(self.path.read_bytes()))
                except (OSError, ValueError) as e:
                    log.warning("street network copy unreadable: %s", e)
            stale = data is None or time.time() - data.get("fetched_at", 0) > NETWORK_TTL_S
            if data is not None and self._net is None:
                self._install(data)
            if stale:
                try:
                    fresh = compact(self._overpass())
                    fresh["fetched_at"] = time.time()
                    if len(fresh["ways"]) > 50:  # an empty answer from a busy server isn't news
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        self.path.write_bytes(gzip.compress(json.dumps(fresh, ensure_ascii=False).encode()))
                        self._install(fresh)
                        self.error = None
                except Exception as e:  # noqa: BLE001 - last week's streets are fine
                    self.error = f"the street network couldn't be read from OpenStreetMap: {str(e)[:160]}"
                    log.warning("%s", self.error)
                    if data is not None:
                        self._fetched_at = time.time() - NETWORK_TTL_S + 6 * 3600  # try again in 6 h
        finally:
            self._running = False

    def _install(self, data: dict) -> None:
        net = Network(data["ways"], data.get("schools"), data.get("osm_base"))
        self._net, self._fetched_at = net, data.get("fetched_at", time.time())
        log.info("street network: %d links, %d schools", len(net.links), len(net.schools))

    def _overpass(self) -> dict:
        if self._fetch is not None:
            return self._fetch(query())
        body = urllib.parse.urlencode({"data": query()}).encode()
        errors = []
        for url in dict.fromkeys([self.settings.overpass_url, *OVERPASS_MIRRORS]):
            try:
                return get_json(url, user_agent=self.settings.user_agent, data=body, timeout=200)
            except WebError as e:  # busy servers answer 429/502/504; try the next mirror
                errors.append(str(e).split("\n")[0])
        raise WebError("; ".join(errors))


__all__ = ["Network", "NetworkSource", "PieceIndex", "infer", "compact", "maxspeed", "CLASS_LABELS",
           "MAIN", "dist_m", "cumulative", "point_at", "slice_line", "project", "bearing"]

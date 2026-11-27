"""The rules and safety features of Nicosia's streets: speed limits, 20 and 30 km/h zones,
traffic signals, pedestrian crossings, cycleways, bus lanes and enforcement cameras.

One `Streets` per process (on the Agent), shared by the chat tools, the page's layers and the
inspector, so every page and answer reads the same copy of each inventory:

- `feeds`: the seven National Access Point inventories (feeds.py), each read once a day;
- `network`: Nicosia's drivable streets and its schools from OpenStreetMap (network.py), read
  once a week, which places each speed sign on its street and carries its limit along it;
- `limits()`: the stretches the signs govern, with how sure each piece is;
- the analyses people use (analysis.py): crossings and signals near schools and bus stops, the
  gaps in the cycle network, the streets inside a slow zone.

The road speeds read the posted limit from here (`segment_limit`): a road segment shows
"limit 50, traffic at 34", and a road with no better free flow uses its posted limit in place
of the assumed 50 km/h.

These are inventories, not readings: they hold at every moment of the city clock, and they say
how old they are.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from chatbot.geo import haversine_m, loose
from chatbot.streets.feeds import AREA, FEEDS, Feeds, in_area
from chatbot.streets.network import MAIN, NetworkSource, PieceIndex, infer
from chatbot.web import request

log = logging.getLogger(__name__)

MIN_COVERAGE = 0.4  # a road segment's posted limit needs pieces along this much of it
SCHOOL_KINDS = {"school", "preschool", "elementary_school", "high_school", "private_school",
                "public_school", "middle_school", "day_care_preschool"}


@dataclass
class Limits:
    pieces: list[dict]
    snaps: list  # per sign: its Snap on a street, or None
    index: PieceIndex
    key: tuple
    built_at: float


def _words(name: str | None) -> list[str]:
    """The telling words of a street's name, in a spelling-tolerant form without h's (the
    inventories write 'Dhigenis', 'Digeni' and 'Hadjiosif' for the same streets)."""
    skip = {"ave", "avenue", "street", "st", "road", "archbishop", "leoforos", "odos", "and"}
    out = []
    for w in re.findall(r"[^\W\d_]+", name or ""):
        if len(w) >= 4 and w.lower() not in skip:
            out.append(loose(w).replace("h", ""))
    return out


def names_match(a: str | None, b: str | None) -> bool:
    """Whether two ways of writing a street name name the same street."""
    wa, wb = _words(a), "".join(_words(b))
    return any(w[:5] in wb for w in wa if len(w) >= 4)


class Streets:
    def __init__(self, settings, mobility=None, places=None, *, fetch=None, network_fetch=None,
                 cache_dir=None):
        ua = settings.user_agent
        self.settings = settings
        self.fetch_text = fetch or (lambda url: request(url, user_agent=ua, timeout=60,
                                                         accept="application/xml, application/json, */*"))
        self.feeds = Feeds(self.fetch_text, cache_dir)
        self.network = NetworkSource(settings, cache_dir, fetch=network_fetch)
        self.mobility = mobility
        self.places = places
        self._limits: Limits | None = None
        self._limits_lock = threading.Lock()
        self._limits_running = False
        self._seg: dict[str, tuple[tuple, dict | None]] = {}
        self._cams: tuple[tuple, list[dict]] | None = None
        self._schools: tuple[tuple, list[dict]] | None = None
        # Nothing is fetched until the server starts the street rules (warm_up) or a tool asks.
        self.started = False
        if mobility is not None:
            mobility.roads.posted_limit = self.segment_limit_kmh

    # -- start ---------------------------------------------------------------------------------

    def warm_up(self) -> None:
        """Read every inventory and the street network, then work out the limits (in the
        background at start, so the first question finds them ready)."""
        self.started = self.network.auto = True
        for key in FEEDS:
            try:
                self.feeds.get(key)
            except Exception as e:  # noqa: BLE001 - one feed failing leaves the others
                log.warning("street inventory %s: %s", key, e)
        self.network.get(wait_s=240)  # starts the load (the copy on disk, or Overpass) and waits
        self.limits(wait=True)

    # -- the inventories ---------------------------------------------------------------------------

    def records(self, key: str) -> list[dict]:
        return self.feeds.get(key).records

    def by_id(self, eid: str) -> tuple[str, dict] | None:
        """(feed key, record) for an id such as signal-12 or ecam-30."""
        eid = eid.strip().lower()
        prefix = eid.split("-", 1)[0]
        key = {"limit": "speed_limits", "zone": "zones", "zentry": "zone_entrances", "signal": "signals",
               "xing": "crossings", "cycle": "cycleways", "buslane": "bus_lanes", "ecam": "cameras"}.get(prefix)
        if key is None:
            return None
        recs = self.cameras() if key == "cameras" else self.records(key)
        rec = next((r for r in recs if r["id"] == eid), None)
        return (key, rec) if rec else None

    def age(self, key: str) -> dict:
        inv = self.feeds.get(key)
        return {"updated": inv.updated, "published": inv.published, "copy": inv.copy, "error": inv.error}

    # -- speed limits -----------------------------------------------------------------------------

    def limits(self, wait: bool = False) -> Limits | None:
        """The stretches the signs govern. Worked out once the street network is in (about a
        second), again when a newer inventory or network comes; None until then, unless
        `wait`."""
        net = self.network.current()
        if net is None and not wait:
            return self._limits  # never fetches: the network comes with the server's start
        signs = self.records("speed_limits")
        key = (id(net), self.feeds.get("speed_limits").published, len(signs))
        hit = self._limits
        if hit is not None and hit.key == key:
            return hit
        if net is None and wait:
            if self.network._fetch is not None and not self.network._running:
                net = self.network.load_now()  # a test's own network
            elif self.started:
                net = self.network.get(wait_s=20)
            if net is not None:
                key = (id(net), self.feeds.get("speed_limits").published, len(signs))
        if net is None:
            return hit
        if not wait:
            if not self.started:
                return hit
            if not self._limits_running:
                self._limits_running = True
                threading.Thread(target=self._build_limits, args=(net, signs, key), daemon=True,
                                 name="street-limits").start()
            return hit
        return self._build_limits(net, signs, key)

    def _build_limits(self, net, signs, key) -> Limits:
        try:
            with self._limits_lock:
                if self._limits is not None and self._limits.key == key:
                    return self._limits
                t = time.time()
                pieces, snaps = infer(net, signs)
                self._limits = Limits(pieces, snaps, PieceIndex(pieces), key, time.time())
                self._seg.clear()
                log.info("speed limits: %d pieces from %d signs in %.1f s", len(pieces), len(signs), time.time() - t)
                return self._limits
        finally:
            self._limits_running = False

    def segment_limit(self, seg) -> dict | None:
        """A road segment's posted limit: {kmh, coverage, share, tier, values, signs, source},
        or None where the signs and OSM don't say (never waits)."""
        lim = self.limits()
        if lim is None:
            return None
        hit = self._seg.get(seg.id)
        if hit is not None and hit[0] == lim.key:
            return hit[1]
        got = lim.index.along([list(c) for c in seg.coords])
        if got is not None and got["coverage"] < MIN_COVERAGE:
            got = None
        self._seg[seg.id] = (lim.key, got)
        return got

    def segment_limit_kmh(self, seg) -> float | None:
        got = self.segment_limit(seg)
        return float(got["kmh"]) if got else None

    def limit_at(self, lon: float, lat: float, max_m: float = 30.0) -> dict | None:
        lim = self.limits()
        return lim.index.at(lon, lat, max_m) if lim else None

    def sign_snap(self, sign_id: str):
        lim = self.limits()
        if lim is None:
            return None
        signs = self.records("speed_limits")
        k = next((i for i, s in enumerate(signs) if s["id"] == sign_id), None)
        return lim.snaps[k] if k is not None and k < len(lim.snaps) else None

    def street_label_at(self, lon: float, lat: float, max_m: float = 40.0) -> str | None:
        net = self.network.current()
        if net is None:
            return None
        c = net.candidates(lon, lat, max_m)
        return net.links[c[0].link].label if c else None

    # -- cameras, placed as well as their names allow ----------------------------------------------------

    def cameras(self) -> list[dict]:
        """The enforcement cameras. One placed only to two decimals (about a kilometre) moves to
        the signalled junction its name gives, when the signals inventory has it."""
        cams = self.records("cameras")
        signals = self.records("signals")
        key = (id(cams), id(signals))
        if self._cams is not None and self._cams[0] == key:
            return self._cams[1]
        out = []
        for c in cams:
            c = dict(c)
            c["placed"] = "feed"
            if not c["precise"] and c.get("street") and c.get("at"):
                near = [s for s in signals if haversine_m(c["lat"], c["lon"], s["lat"], s["lon"]) <= 1500
                        and any(names_match(c["street"], x) for x in s["streets"])
                        and any(names_match(c["at"], x) for x in s["streets"])]
                if near:
                    s = min(near, key=lambda s: haversine_m(c["lat"], c["lon"], s["lat"], s["lon"]))
                    c["feed_lon"], c["feed_lat"] = c["lon"], c["lat"]
                    c["lon"], c["lat"], c["placed"], c["signal"] = s["lon"], s["lat"], "junction", s["id"]
                else:
                    c["placed"] = "rough"
            out.append(c)
        self._cams = (key, out)
        return out

    # -- schools ------------------------------------------------------------------------------------

    def schools(self) -> list[dict]:
        """Schools and kindergartens in Greater Nicosia: OpenStreetMap's (with the street network)
        and Overture's, once each (two within 60 m are one school)."""
        net = self.network.current()
        ov = getattr(self.places, "overture", None) if self.places is not None else None
        ov_places = ov.all() if ov is not None and getattr(ov, "ready", False) else []
        key = (id(net), len(ov_places))
        if self._schools is not None and self._schools[0] == key:
            return self._schools[1]
        out: list[dict] = []
        for s in (net.schools if net else []):
            if in_area(s["lon"], s["lat"]):
                out.append({"id": f"school-{s['id']}", "name": s["name"] or ("Kindergarten" if s["kind"] == "kindergarten" else "School"),
                            "named": bool(s["name"]), "kind": s["kind"], "lat": s["lat"], "lon": s["lon"],
                            "source": "openstreetmap", "ref": s["id"]})
        for p in ov_places:
            if p.get("kind") not in SCHOOL_KINDS or not in_area(p["lon"], p["lat"]):
                continue
            if any(haversine_m(p["lat"], p["lon"], s["lat"], s["lon"]) <= 60 for s in out):
                continue
            pid = p.get("id") or f"ov-{(p.get('gers') or '')[:12]}"
            out.append({"id": f"school-{pid}", "name": p.get("name") or "School", "named": bool(p.get("name")),
                        "kind": "kindergarten" if "preschool" in p["kind"] else "school",
                        "lat": p["lat"], "lon": p["lon"], "source": "overture", "ref": pid})
        self._schools = (key, out)
        return out

    def school(self, sid: str) -> dict | None:
        sid = sid.strip()
        return next((s for s in self.schools() if s["id"].lower() == sid.lower()), None)

    # -- where things are ----------------------------------------------------------------------------

    def zone_at(self, lon: float, lat: float) -> dict | None:
        from chatbot.streets.analysis import inside
        return next((z for z in self.records("zones") if inside(z["ring"], lon, lat)), None)

    def main_road_near(self, lon: float, lat: float, max_m: float = 60.0) -> tuple[object, float] | None:
        """The nearest main road (tertiary and up) to a point: (link, metres)."""
        net = self.network.current()
        if net is None:
            return None
        for c in net.candidates(lon, lat, max_m):
            link = net.links[c.link]
            if link.hw in MAIN:
                return link, c.dist
        return None

    def status(self) -> dict:
        """What the page shows about the inventories: counts and how old, without reading any."""
        net = self.network
        lim = self._limits
        return {"feeds": self.feeds.status(),
                "network": {"ready": net._net is not None, "error": net.error,
                            "fetched_at": net.fetched_at or None},
                "limits": {"ready": lim is not None, "pieces": len(lim.pieces) if lim else 0}}


__all__ = ["Streets", "AREA", "names_match"]

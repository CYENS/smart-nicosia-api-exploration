"""Places the chat found on the web: a singing teacher, a tailor, a yoga studio, anything the
OpenStreetMap places index doesn't list. The model passes what the search results say (a name,
an address, a phone, a website, the page it read); this puts each one on the map without
guessing and keeps it under a stable id (web-1a2b3c4d), so the answer's chips, the inspector,
search and routes can find it again.

A place is placed, most trusted first:
- "exact": a place in the OSM index with exactly that name; a place in Overture Maps' index
  (chatbot/overture.py) whose name matches loosely and whose address agrees, by its postcode or
  a street of it (Overture's points stand at the door, so this beats any geocoding); or its
  address (or its name) found by Nominatim or Photon as a building, a shop, an office or a house;
- "street": only the street of its address was found, so its pin stands on the street, not at
  the door (the map draws it paler and the answer says so);
- otherwise it is not placed. A neighbourhood or a town is not a position.

Greater Nicosia has several streets of one name (three Nikiforou Foka: by Famagusta Gate, in
Pallouriotissa and in Latsia), and a geocoder may know the right one only by its Greek name. So
the street is looked up in Nominatim and Photon both, a hit in another postcode than the
address's is another place, and when streets of that name stand apart and nothing in the
address tells them apart, the place is not placed rather than put on the first one.

Lookups go through Places (Nominatim throttled to 1 request a second, then Photon, cached),
about five per new place, and every place placed is kept on disk, so asking about it again costs
no request at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from chatbot.geo import Places, haversine_m, in_bbox, loose, loose_match, norm
from chatbot.settings import CACHE_DIR

log = logging.getLogger(__name__)

WEB_ID = re.compile(r"^web-[0-9a-f]{8}$")
KEEP = 1000  # places kept on disk, the most recently found
NAME_NEAR_STREET_M = 1500  # a name match counts only this close to its address's street
SAME_STREET_M = 1000  # pieces of a street name closer than this are one street; farther, namesakes
# Bumped when placing changes, so a place kept from before is placed again when it's next found
# (version 1 put Ithaki on the Nikiforou Foka in Pallouriotissa, not the one by Famagusta Gate;
# version 3 takes Overture's point before geocoding).
PLACER = 3
POSTCODE = re.compile(r"(?<![\d.])(\d{4})(?![\d.])")  # Cyprus: four digits, 1000 to 9999

AREA_TYPES = {"suburb", "neighbourhood", "quarter", "city", "town", "village", "hamlet",
              "city_block", "locality", "municipality", "county", "state", "region", "country",
              "island", "postcode", "administrative", "district", "borough", "city_district"}
ROAD_TYPES = {"residential", "primary", "secondary", "tertiary", "unclassified", "service",
              "pedestrian", "living_street", "footway", "trunk", "road", "motorway", "path",
              "primary_link", "secondary_link", "tertiary_link", "trunk_link", "track", "street"}
# Bits of an address a geocoder can't match: floors, flats, offices, the country.
UNIT_PART = re.compile(r"\b(floor|fl\.?|flat|apt\.?|apartment|office|suite|shop|unit|"
                       r"όροφος|διαμέρισμα|γραφείο|κατάστημα)\b", re.IGNORECASE)
COUNTRY = re.compile(r"\b(cyprus|κύπρος|kibris)\b", re.IGNORECASE)
NUMBER = re.compile(r"\b\d+[a-zα-ω]?\b", re.IGNORECASE)
# Words that name the kind of street rather than the street ("Makariou Ave").
STREET_TYPE = re.compile(r"\b(street|str|st|road|rd|avenue|ave|av|boulevard|blvd|square|sq|"
                         r"οδός|οδος|λεωφόρος|λεωφορος|λεωφ|πλατεία|πλατεια|odos|leoforos)\b\.?",
                         re.IGNORECASE)
TOWNS = {"nicosia", "lefkosia", "λευκωσια", "lefkoşa"}


def web_id(name: str, address: str | None) -> str:
    key = f"{norm(name)}|{norm(address)}".encode()
    return "web-" + hashlib.sha1(key).hexdigest()[:8]


def clean(text: object, n: int) -> str | None:
    """Text from a web page as one plain line, or None."""
    s = re.sub(r"[\x00-\x1f\x7f<>]+", " ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()[:n]
    return s or None


def clean_url(url: object) -> str | None:
    s = clean(url, 500)
    if not s:
        return None
    if not re.match(r"^https?://", s, re.IGNORECASE):
        s = f"https://{s}" if re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", s) else ""
    parts = urlsplit(s)
    return s if parts.scheme in ("http", "https") and parts.netloc and " " not in s else None


def precision_of(hit: dict) -> str:
    """How close a geocoder hit is to a door: exact, street or area."""
    if hit.get("source") == "places index":
        return "exact"
    cls, kind = hit.get("class"), hit.get("kind")
    if cls == "highway" or (cls is None and kind in ROAD_TYPES):
        return "street"
    if cls in ("place", "boundary") and kind != "house" or kind in AREA_TYPES:
        return "area"
    return "exact"


def street_only(address: str) -> str | None:
    """The address without house numbers, postcodes, floors or the country: 'Yiannaki Talioti
    7, 2014 Strovolos, Cyprus' -> 'Yiannaki Talioti, Strovolos'. None if nothing changes."""
    parts = []
    for part in address.split(","):
        if UNIT_PART.search(part) or COUNTRY.search(part):
            continue
        part = re.sub(r"\s+", " ", NUMBER.sub(" ", part)).strip(" .-")
        if part:
            parts.append(part)
    out = ", ".join(parts)
    return out if out and norm(out) != norm(address) else None


def respell(text: str) -> str:
    """The other common Latin spelling of a Greek name: Y before i or e is G at the start of a
    word ('Yiannaki' -> 'Giannaki', 'Yeoryiou' -> 'Georgiou'), and 'ayi' is 'agi' ('Ayios')."""
    t = re.sub(r"\b([Yy])(?=[iIeE])", lambda m: "G" if m.group(1) == "Y" else "g", text)
    t = re.sub(r"(?<=[aA])yi", "gi", t)
    return re.sub(r"(?<=[eE])ory", "org", t)


def address_streets(address: str) -> list[str]:
    """The parts of an address that name its street (or its building), to check a geocoder's
    answer against: no numbers, floors, street-type words, the country or the town it ends in."""
    parts = []
    for part in address.split(","):
        if UNIT_PART.search(part) or COUNTRY.search(part):
            continue
        part = re.sub(r"\s+", " ", STREET_TYPE.sub(" ", NUMBER.sub(" ", part))).strip(" .-'")
        if len(re.sub(r"\W", "", part)) >= 3 and norm(part) not in TOWNS:
            parts.append(part)
    return parts[:-1] if len(parts) > 1 else parts  # the last part is the town or the area


def address_postcode(address: str | None) -> str | None:
    """The postcode an address gives: 'Nikiforou Foka 33, 1016 Nicosia' -> '1016'."""
    found = POSTCODE.findall(address or "")
    return found[-1] if found else None


def address_areas(address: str) -> list[str]:
    """The places an address names after its street ('Emanuel Roidi 2, Strovolos, Nicosia 1095'
    -> ['Strovolos']), to choose between streets of the same name."""
    out = []
    for part in address.split(",")[1:]:
        if UNIT_PART.search(part) or COUNTRY.search(part):
            continue
        part = re.sub(r"\s+", " ", NUMBER.sub(" ", part)).strip(" .-")
        if len(part) >= 3 and norm(part) not in TOWNS:
            out.append(part)
    return out


def postcode_of(hit: dict) -> str | None:
    """A geocoder hit's postcode: its own field, else the last one in its address."""
    if hit.get("postcode"):
        m = POSTCODE.search(str(hit["postcode"]))
        return m.group(1) if m else None
    found = POSTCODE.findall(hit.get("address") or "")
    return found[-1] if found else None


def by_postcode(hits: list[dict], code: str | None) -> list[dict]:
    """The hits that can be at the address's postcode, best first: the same postcode, then a
    neighbouring one (the same first three digits: the next block of one avenue). Hits that don't
    say their postcode count only when none does say it right. A hit in another postcode is
    another place."""
    if not code:
        return hits
    same = [h for h in hits if postcode_of(h) == code]
    near = [h for h in hits if (p := postcode_of(h)) and p != code and p[:3] == code[:3]]
    return same + near or [h for h in hits if postcode_of(h) is None]


def streets_apart(hits: list[dict]) -> list[list[dict]]:
    """Street hits grouped into streets: pieces within SAME_STREET_M of each other are one."""
    groups: list[list[dict]] = []
    for h in hits:
        for g in groups:
            if any(haversine_m(h["lat"], h["lon"], x["lat"], x["lon"]) <= SAME_STREET_M for x in g):
                g.append(h)
                break
        else:
            groups.append([h])
    return groups


def where_is(street: list[dict]) -> str:
    """A street among its namesakes, for a person: its postcode, else its neighbourhood."""
    for h in street:
        if code := postcode_of(h):
            return f"postcode {code}"
    parts = [p.strip() for p in (street[0].get("address") or "").split(",")[1:] if p.strip()]
    return parts[0] if parts else f"{street[0]['lat']:.4f}, {street[0]['lon']:.4f}"


def on_street(hit: dict, streets: list[str]) -> bool:
    """Whether a geocoder's answer stands on one of the address's streets (a fuzzy geocoder
    happily answers 'Talioti 7' with number 7 of another street)."""
    if not streets:
        return True
    text = " ".join(x for x in (hit.get("name"), hit.get("street"),
                                *(hit.get("address") or "").split(",")[:2]) if x)
    return any(loose_match(s, text) for s in streets)


# Words that say what kind of place it is rather than which: 'Ithaki Bar' and 'Ithaki Venue'
# are one name.
VENUE_WORDS = {loose(w) for w in ("the", "bar", "venue", "club", "cafe", "café", "restaurant",
                                  "tavern", "taverna", "lounge", "pub", "studio", "school",
                                  "nicosia", "lefkosia", "cyprus", "ltd", "and")}


def name_words(name: str | None) -> set[str]:
    return {w for w in re.split(r"[^0-9a-z]+", loose(name)) if len(w) >= 2} - VENUE_WORDS


def names_agree(a: str | None, b: str | None) -> int:
    """2 when two names are one ('DownTown Live' and 'Downtown Live', 'Ithaki Bar' and 'Ithaki
    Venue'), 1 when one is a near spelling of the other ('S//P (SLIP)' and 'S//IP'), else 0."""
    la, lb = loose(a), loose(b)
    if not la or not lb:
        return 0
    if la == lb:
        return 2
    wa, wb = name_words(a), name_words(b)
    if wa and wb and (wa <= wb or wb <= wa):
        return 2
    forms_a = {re.sub(r"[^0-9a-z]", "", la), *wa}
    forms_b = {re.sub(r"[^0-9a-z]", "", lb), *wb}
    near = max((SequenceMatcher(None, x, y).ratio() for x in forms_a for y in forms_b
                if len(x) >= 3 and len(y) >= 3), default=0.0)
    return 1 if near >= 0.8 else 0


class WebPlaces:
    def __init__(self, places: Places, path: Path | None = None):
        self.places = places
        self.path = path or CACHE_DIR / "web_places.json"
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = {p["id"]: p for p in data["places"]}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    # -- reading -------------------------------------------------------------------------------

    def get(self, wid: str) -> dict | None:
        return self._items.get((wid or "").strip().lower())

    def all(self) -> list[dict]:
        return list(self._items.values())

    def named(self, text: str) -> dict | None:
        """The most recently found place called exactly `text`, or the one with that id."""
        if WEB_ID.match((text or "").strip().lower()):
            return self.get(text)
        key = norm(text)
        same = [p for p in self._items.values() if key and norm(p["name"]) == key]
        return max(same, key=lambda p: p.get("found_at", 0)) if same else None

    # -- placing -------------------------------------------------------------------------------

    def locate(self, item: dict, topic: str | None = None) -> tuple[dict | None, str | None]:
        """One place from a web result ({name, address, phone, website, source_url, note}) on
        the map. Returns (the stored place, None) or (None, why it couldn't be placed)."""
        name = clean(item.get("name"), 120)
        if not name:
            return None, "no name"
        address = clean(item.get("address"), 200)
        wid = web_id(name, address)
        fields = {"phone": clean(item.get("phone"), 40), "website": clean_url(item.get("website")),
                  "note": clean(item.get("note"), 200)}
        source = clean_url(item.get("source_url"))
        with self._lock:
            known = self._items.get(wid)
        if known is None or known.get("placer") != PLACER:
            spot = self._find(name, address)
            if isinstance(spot, str):
                if known is not None:  # placed by an older, wrong rule: forget that pin
                    with self._lock:
                        self._items.pop(wid, None)
                        self._save()
                return None, spot
            kept = {k: v for k, v in (known or {}).items()
                    if k in ("sources", "topics", "phone", "website", "note")}
            known = {"sources": [], "topics": [], **kept, "id": wid, "name": name,
                     "address": address, **spot, "placer": PLACER}
        place = {**known, **{k: v for k, v in fields.items() if v}, "found_at": time.time()}
        if source and source not in place["sources"]:
            place["sources"] = [*place["sources"], source][-3:]
        if (t := clean(topic, 60)) and t not in place["topics"]:
            place["topics"] = [*place["topics"], t][-3:]
        with self._lock:
            self._items[wid] = place
            self._save()
        return place, None

    def _find(self, name: str, address: str | None) -> dict | str:
        """Where a place is, as {lat, lon, precision, located_by, matched, osm_id?}, or a
        reason it can't be placed."""
        if self.places.ready:
            hits = self.places.named(name)
            if hits:
                p = hits[0]
                return {"lat": p["lat"], "lon": p["lon"], "precision": "exact",
                        "located_by": "OpenStreetMap", "matched": p.get("name"), "osm_id": p["id"]}
        if (found := self._overture(name, address)) is not None:
            return found
        code = address_postcode(address)
        street, namesakes, streets = None, [], []
        if address:
            streets = address_streets(address)
            other = respell(address)
            tries = [address, other, street_only(other), street_only(address)]
            for text in list(dict.fromkeys(x for x in tries if x))[:3]:
                hits = by_postcode([h for h in self.places.geocode(text) if on_street(h, streets)],
                                   code)
                hit, prec = self._best(hits)
                if prec == "exact":
                    return self._spot(hit, "exact", "its address")
                if hit:
                    break  # the street is known; a shorter address won't find the door
            if streets:
                street, namesakes = self._street(street_only(other) or street_only(address)
                                                 or address, streets, code, address)
        hits = by_postcode([h for h in self.places.geocode(name)
                            if loose_match(name, h.get("name")) or loose_match(h.get("name"), name)],
                           code)
        hit, prec = self._best(hits)
        # A name match counts near the address's street, or near one of its namesakes (which it
        # then tells apart); with no street found at all, its name is the best we have.
        anchors = [street] if street else [g[0] for g in namesakes]
        if prec == "exact" and (not anchors or any(haversine_m(
                a["lat"], a["lon"], hit["lat"], hit["lon"]) <= NAME_NEAR_STREET_M for a in anchors)):
            return self._spot(hit, "exact", "its name")
        if street is not None:
            return self._spot(street, "street", "its street")
        if namesakes:
            return (f"Nicosia has {len(namesakes)} streets called {streets[0]} "
                    f"({', '.join(where_is(g) for g in namesakes)}) and the result doesn't say "
                    "which: pass its postcode if the page gives one")
        if not address:
            return "no address in the result, and its name isn't on the map"
        if code:
            return f"no street called {streets[0] if streets else address} in postcode {code}"
        return "its address isn't on the map of Nicosia"

    def _overture(self, name: str, address: str | None) -> dict | None:
        """The Overture place the result names, at its door. Its address must agree first, by
        the postcode or a street of it (a place in another postcode is another place), then its
        name: the same telling words ('Ithaki Bar' is 'Ithaki Venue'), or, with both the
        postcode and the street agreeing, a near spelling ('S//P (SLIP)' is 'S//IP'). A result
        with no address takes only a name no other Overture place has."""
        ov = getattr(self.places, "overture", None)
        if ov is None or not ov.ready:
            return None
        if not address:
            same = ov.named(name)
            return self._from_overture(same[0]) if len(same) == 1 else None
        code = address_postcode(address)
        streets = address_streets(address)
        best = None
        for p in ov.all():
            pc = postcode_of({"postcode": p.get("postcode")})
            if code and pc and pc[:3] != code[:3]:
                continue
            agree = 2 * bool(code and pc == code) + (bool(streets) and ov.on_street(p, streets))
            if not agree:
                continue
            alike = names_agree(name, p["name"])
            if alike == 2 or (alike == 1 and agree == 3):
                rank = (alike, agree, p["confidence"])
                if best is None or rank > best[0]:
                    best = (rank, p)
        return self._from_overture(best[1]) if best else None

    @staticmethod
    def _from_overture(p: dict) -> dict:
        where = ", ".join(x for x in (p.get("address"), p.get("postcode")) if x)
        return {"lat": p["lat"], "lon": p["lon"], "precision": "exact",
                "located_by": "Overture Maps",
                "matched": clean(f"{p['name']}, {where}" if where else p["name"], 160),
                "overture_id": p["id"], **{k: p[k] for k in ("phone", "website") if p.get(k)}}

    def _street(self, query: str, streets: list[str], code: str | None,
                address: str) -> tuple[dict | None, list[list[dict]]]:
        """The address's street, from every street of that name both geocoders know inside
        Nicosia, as (the street, []), or (None, the streets of that name) when they stand apart
        and neither the postcode nor an area in the address tells which."""
        hits = [h for h in self.places.every_match(query)
                if in_bbox(h["lat"], h["lon"]) and precision_of(h) == "street"
                and on_street(h, streets)]
        groups = streets_apart(by_postcode(hits, code))
        if len(groups) <= 1:
            return (groups[0][0] if groups else None), []
        areas = address_areas(address)
        fit = [g for g in groups if any(loose_match(a, h.get("address")) for a in areas for h in g)]
        if len(fit) == 1:
            return fit[0][0], []
        return None, groups

    @staticmethod
    def _best(hits: list[dict]) -> tuple[dict | None, str | None]:
        """The most precise hit inside Nicosia, and its precision (areas don't count)."""
        rank = {"exact": 0, "street": 1}
        inside = [(rank[p], i, h) for i, h in enumerate(hits or [])
                  if in_bbox(h["lat"], h["lon"]) and (p := precision_of(h)) in rank]
        if not inside:
            return None, None
        r, _, h = min(inside, key=lambda t: t[:2])
        return h, ("exact", "street")[r]

    @staticmethod
    def _spot(hit: dict, precision: str, how: str) -> dict:
        src = hit.get("source") or "geocoder"
        parts = [p.strip() for p in (hit.get("address") or "").split(",") if p.strip()]
        # What the geocoder matched: the street's name, or the start of the address it found.
        matched = hit.get("name") if precision == "street" else ", ".join(parts[:2]) or hit.get("name")
        return {"lat": round(hit["lat"], 6), "lon": round(hit["lon"], 6), "precision": precision,
                "located_by": f"{how} ({'OpenStreetMap' if src == 'places index' else src})",
                "matched": clean(matched, 160)}

    def _save(self) -> None:
        keep = sorted(self._items.values(), key=lambda p: -p.get("found_at", 0))[:KEEP]
        self._items = {p["id"]: p for p in keep}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"places": keep}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save web places: %s", e)

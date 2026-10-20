"""Dangerous buildings from the public registers of Cyprus's District Local Government
Organisations (Επαρχιακοί Οργανισμοί Αυτοδιοίκησης, EOA).

Since the 2024 local-government reform the EOAs handle dangerous buildings under the Streets
and Buildings Regulation Law: article 15B "dangerous" (επικίνδυνη) and article 15A "needs
repair" (χρήζει επισκευής). Four of the five publish a register, each in its own way:

- Nicosia (ndlgo.org.cy): the live register's map page; the records are embedded as JSON with
  hand-placed coordinates, the file number, urban or rural, and, in the notes, the DLS parcel
  OBJECTID.
- Limassol (eoalemesos.org.cy): an ArcGIS Online WebGIS; layer 13 holds the plots as polygons.
- Larnaca (eoal.org.cy): a WordPress table filled through admin-ajax, with the declaration
  date and public instructions but no block number and no coordinates.
- Famagusta (eoaa.org.cy): two HTML tables of plot references, articles 15A and 15B.

Paphos publishes no list. Every record is tied to its DLS parcel and to the DLS 3D buildings
standing on it, so the map can extrude it at its real height. The registers are cached for a
day; the parcels come first (seconds), the 3D link follows in the background (a few minutes
the first time, then from the tile cache).
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import math
import re
import statistics
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable

from chatbot.dls import (DISTRICT_CODES, DLS, ParcelRef, area_m2, bbox_of, centroid,
                         parcel_refs, tiles_for_bbox)
from chatbot.geo import haversine_m
from chatbot.settings import CACHE_DIR, ROOT, Settings
from chatbot.web import WebError, get_json, request

log = logging.getLogger(__name__)

TTL_S = 86400
# The registers as published on the day this was built: the fallback when a site is down or
# asks for a human check (Nicosia's does after a few quick requests from one address).
SEED_PATH = ROOT / "data" / "eoa_registers_seed.json"
# The live register's map page. The standalone file it replaced
# (wp-content/uploads/2026/07/EOA_Xartis_Epikindynes_Oikodomes.html) stopped at 711 records.
NICOSIA_URL = "https://ndlgo.org.cy/en/hazardousbldsmap/"
LIMASSOL_URL = ("https://services-eu1.arcgis.com/mRIyMyDt3UXNMvgd/arcgis/rest/services/"
                "EOA_WebGIS_EXT_v1/FeatureServer/13")
LARNACA_URL = "https://eoal.org.cy/epikindyna-ktiria/"
LARNACA_AJAX = "https://eoal.org.cy/wp-admin/admin-ajax.php"
FAMAGUSTA_URL = ("https://eoaa.org.cy/%CE%B5%CF%80%CE%B9%CE%BA%CE%AF%CE%BD%CE%B4%CF%85%CE%BD"
                 "%CE%B5%CF%82-%CE%BF%CE%B9%CE%BA%CE%BF%CE%B4%CE%BF%CE%BC%CE%AD%CF%82-%CE%B5"
                 "%CF%80%CE%B1%CF%81%CF%87%CE%AF%CE%B1%CF%82-%CE%B1%CE%BC%CE%BC%CE%BF/")

EOAS = {
    "nicosia": {"name": "ΕΟΑ Λευκωσίας", "name_en": "Nicosia DLGO",
                "page": "https://ndlgo.org.cy/en/hazardousbldslist/"},
    "limassol": {"name": "ΕΟΑ Λεμεσού", "name_en": "Limassol DLGO",
                 "page": "https://eoalemesos.org.cy/"},
    "larnaca": {"name": "ΕΟΑ Λάρνακας", "name_en": "Larnaca DLGO", "page": LARNACA_URL},
    "famagusta": {"name": "ΕΟΑ Αμμοχώστου", "name_en": "Famagusta DLGO", "page": FAMAGUSTA_URL},
}
STATUS_GR = {"dangerous": "Επικίνδυνη", "needs_repair": "Χρήζει επισκευής"}
MATCH_NOTES = {
    "on_parcel": "DLS 3D buildings standing on the plot",
    "spans_parcel": "one DLS 3D building spanning this and neighbouring plots",
    "block": "the plot lies inside a DLS 3D block (old town); height is the block's eave, approx.",
    "near_parcel": "DLS 3D building at the plot's edge",
    "none": "no DLS 3D building on the plot (empty, demolished or not modelled)",
}


def _text(fragment: str) -> str:
    """HTML fragment -> plain single-spaced text."""
    t = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\s*\n\s*", "\n", t)).strip()


def _js_literals(page: str, name: str) -> list:
    """Every JSON value assigned with `const NAME = ...` in a page (parsed, never executed)."""
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(rf"const\s+{name}\s*=\s*", page):
        try:
            out.append(dec.raw_decode(page, m.end())[0])
        except ValueError:
            continue
    return out


def _split_place(text: str, sep: str) -> tuple[str | None, str | None]:
    parts = [p.strip() for p in (text or "").split(sep, 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None, (parts[0] or None) if parts else None


# -- parsers (pure: page text in, raw records out) ------------------------------------------------


def parse_nicosia(page: str) -> list[dict]:
    buildings = next((v for v in _js_literals(page, "BUILDINGS") if isinstance(v, list)), None)
    if not buildings:
        raise ValueError("no BUILDINGS literal in the Nicosia map page")
    coords: dict = {}
    for c in _js_literals(page, "COORDS"):  # later literals override earlier ones, as on the page
        if isinstance(c, dict):
            coords.update(c)
    out = []
    for r in buildings:
        i = r.get("record_id", r.get("i"))
        lat, lon = coords.get(str(i)) or (r.get("lat"), r.get("lng"))
        muni, comm = _split_place(r.get("area") or "", " - ")
        status = "needs_repair" if "ΕΠΙΣΚΕΥ" in (r.get("status") or "") else "dangerous"
        out.append({
            "id": f"NIC-{i}", "eoa": "nicosia", "status": status,
            "status_local": r.get("status"), "municipality": muni, "community": comm,
            "parish": r.get("parish") or None, "address": (r.get("address") or "").strip() or None,
            "sheet": r.get("sheet"), "plan": r.get("plan"), "block": r.get("section"),
            "parcel": r.get("parcel"),
            "file_number": re.sub(r"\s*\n\s*", ", ", r.get("file_number") or "") or None,
            "setting": r.get("category"),
            "lat": lat, "lon": lon, "position": "published" if lat is not None else None,
            "_oids": [int(x) for x in re.findall(r"OBJECTID\s+(\d+)", r.get("notes") or "")],
        })
    return out


def parse_limassol(geojson: dict) -> list[dict]:
    out = []
    for f in geojson.get("features") or []:
        p = f.get("properties") or {}
        place = next((v for k, v in p.items() if k.startswith("ΔΗΜΟΣ")), "") or ""
        muni, comm = _split_place(place, ",")

        def clean(key):
            v = p.get(key)
            if v is None:
                return None
            return re.sub(r"\s+", " ", str(v)).strip() or None

        out.append({
            "id": f"LIM-{p.get('OBJECTID')}", "eoa": "limassol", "status": "dangerous",
            "status_local": "ΕΠΙΚΙΝΔΥΝΗ", "municipality": muni, "community": comm,
            "parish": clean("QRTR_NM_G"), "address": clean("ADDRESS"),
            "name": clean("BUILDING_NAME"), "registration": clean("ARITHMOS_EGGRAFIS"),
            "sheet": clean("SHEET"), "plan": clean("PLAN_NBR"), "block": clean("BLCK_CODE"),
            "parcel": clean("PARCEL_NBR"), "lat": None, "lon": None, "position": None,
            "_parcel_geometry": f.get("geometry"),
        })
    return out


LARNACA_COLUMNS = ["deo", "district", "location", "address", "sheet_plan", "parcel", "declared",
                   "description", "danger_to", "instructions", "listed"]


def parse_larnaca_rows(table_html: str) -> list[dict]:
    rows = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table_html):
        cells = [_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", tr)]
        if len(cells) < len(LARNACA_COLUMNS) or not re.search(r"\d", cells[0]):
            continue
        rows.append(dict(zip(LARNACA_COLUMNS, cells)))
    return rows


def larnaca_records(rows: list[dict]) -> list[dict]:
    out, seen = [], Counter()
    for r in rows:
        num = re.sub(r"[^0-9A-Za-z]+", "", r["deo"].split("-")[-1]) or str(len(out) + 1)
        seen[num] += 1
        rid = f"LAR-{num}" + (f"-{seen[num]}" if seen[num] > 1 else "")
        sheet, _, plan = r["sheet_plan"].partition("/")
        m = re.match(r"(.*?)\s*\((.*)\)\s*$", r["location"])
        muni, comm = (m.group(1), m.group(2)) if m else (r["location"] or None, None)
        out.append({
            "id": rid, "eoa": "larnaca", "status": "dangerous", "status_local": "ΕΠΙΚΙΝΔΥΝΗ",
            "municipality": muni, "community": comm, "parish": None,
            "address": r["address"] or None, "deo": r["deo"], "sheet": sheet or None,
            "plan": plan or None, "block": None, "parcel": r["parcel"],
            "declared": r["declared"] or None, "description": r["description"] or None,
            "danger_to": r["danger_to"] or None,
            "instructions": re.sub(r"\s*\n\s*", " ", r["instructions"]) or None,
            "listed": r["listed"].strip().upper().startswith("ΝΑΙ"),
            "lat": None, "lon": None, "position": None,
        })
    return out


FAMAGUSTA_HEADERS = {"Α/Α": "aa", "Εγγραφ": "registration", "Τεμάχ": "parcel", "Φύλλο": "sheet_plan",
                     "Τμήμα": "block", "Δήμος": "place"}


def parse_famagusta(page: str) -> list[dict]:
    """Two tables, each after its heading: article 15A (needs repair) and 15B (dangerous). The
    headings also appear earlier on the page, so a table takes the last heading before it."""
    out = []
    for m in re.finditer(r"(?is)<table.*?</table>", page):
        before = page[max(0, m.start() - 800):m.start()]
        article = max(("15Α", "15Β"), key=before.rfind)
        if before.rfind(article) < 0:
            continue
        status = "needs_repair" if article == "15Α" else "dangerous"
        cols: list[str | None] = []
        for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", m.group(0)):
            cells = [_text(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)]
            if not cols and any("Τεμάχ" in c for c in cells):
                cols = [next((v for k, v in FAMAGUSTA_HEADERS.items() if k in c), None)
                        for c in cells]
                continue
            if not cols or not cells or not re.fullmatch(r"\d+", cells[0] or ""):
                continue
            r = {c: v for c, v in zip(cols, cells) if c}
            sheet_plan = r.get("sheet_plan", "")
            sheet, _, plan = sheet_plan.partition("/") if "/" in sheet_plan else ("0", "", sheet_plan)
            muni, comm = _split_place(r.get("place", ""), ",")
            label = "ΧΡΗΖΕΙ ΕΠΙΣΚΕΥΗΣ" if status == "needs_repair" else "ΕΠΙΚΙΝΔΥΝΗ"
            out.append({
                "id": f"FAM-{'15A' if status == 'needs_repair' else '15B'}-{r.get('aa')}",
                "eoa": "famagusta", "status": status, "status_local": f"{label} (Άρθρο {article})",
                "municipality": muni, "community": comm, "parish": None, "address": None,
                "registration": r.get("registration") or None, "sheet": sheet, "plan": plan,
                "block": r.get("block") or None, "parcel": r.get("parcel"),
                "lat": None, "lon": None, "position": None,
            })
    if not out:
        raise ValueError("no tables found on the Famagusta page")
    return out


# -- fetchers ------------------------------------------------------------------------------------


def human_check(page: str) -> bool:
    """Whether a site answered with a CAPTCHA or bot check instead of the page. We never try to
    get past one: the register falls back to its last good copy."""
    return bool(re.search(r"(?i)captcha|validation request|user validation required",
                          page[:4000]))


def fetch_nicosia(s: Settings) -> list[dict]:
    page = request(NICOSIA_URL, user_agent=s.user_agent, accept="text/html", timeout=60)
    if human_check(page):
        raise ValueError("ndlgo.org.cy asked for a human check (CAPTCHA); not retried")
    return parse_nicosia(page)


def fetch_limassol(s: Settings) -> list[dict]:
    return parse_limassol(get_json(f"{LIMASSOL_URL}/query", user_agent=s.user_agent, params={
        "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson"}, timeout=60))


def fetch_larnaca(s: Settings) -> list[dict]:
    page = request(LARNACA_URL, user_agent=s.user_agent, accept="text/html", timeout=60)
    m = re.search(r'dbm_ajax\s*=\s*\{[^}]*"nonce"\s*:\s*"([0-9a-f]+)"', page)
    if not m:
        raise ValueError("no table nonce on the Larnaca page")
    rows, page_no, last = [], 1, 1
    while page_no <= last and page_no <= 50:
        body = urllib.parse.urlencode({"action": "dbm_load_buildings", "nonce": m.group(1),
                                       "page": page_no, "location": "", "address": "",
                                       "sheet_plan": "", "parcel": ""}).encode()
        data = get_json(LARNACA_AJAX, user_agent=s.user_agent, data=body, timeout=60)
        if not data.get("success"):
            raise ValueError(f"Larnaca table answered {str(data)[:120]}")
        got = parse_larnaca_rows(data["data"].get("table", ""))
        if not got:
            break
        rows.extend(got)
        pages = [int(p) for p in re.findall(r'data-page="(\d+)"', data["data"].get("pagination", ""))]
        last = max(pages or [page_no])
        page_no += 1
    return larnaca_records(rows)


def fetch_famagusta(s: Settings) -> list[dict]:
    return parse_famagusta(request(FAMAGUSTA_URL, user_agent=s.user_agent, accept="text/html",
                                   timeout=60))


FETCHERS: dict[str, Callable[[Settings], list[dict]]] = {
    "nicosia": fetch_nicosia, "limassol": fetch_limassol, "larnaca": fetch_larnaca,
    "famagusta": fetch_famagusta,
}


# -- linking to the DLS cadastre and 3D model ----------------------------------------------------


def _merge(geoms: list[dict]) -> dict | None:
    polys = []
    for g in geoms:
        if g and g["type"] == "Polygon":
            polys.append(g["coordinates"])
        elif g and g["type"] == "MultiPolygon":
            polys.extend(g["coordinates"])
    if not polys:
        return None
    return {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else \
        {"type": "MultiPolygon", "coordinates": polys}


def resolve_parcels(records: list[dict], dls: DLS) -> None:
    """Attach each record's DLS parcel(s) and a position: the published point if there is one,
    else the parcel's centre. Nicosia gives the parcel OBJECTID; the others give references."""
    oids = {o for r in records for o in r.get("_oids", [])}
    by_oid = dls.parcels_by_objectid(oids) if oids else {}
    for r in records:
        r["_refs"] = parcel_refs(r.get("sheet"), r.get("plan"), r.get("parcel"), r.get("block"),
                                 DISTRICT_CODES[r["eoa"]])
    for r in records:  # a register that publishes its plots, or a copy already resolved
        r["_given"] = r.get("_parcel_geometry") or r.get("parcel_geometry")
    refs: list[ParcelRef] = [ref for r in records
                             if not r["_given"]
                             and not any(o in by_oid for o in r.get("_oids", []))
                             for ref in r["_refs"]]
    by_ref = dls.parcels_by_ref(refs) if refs else {}
    for r in records:
        found: list[dict] = []
        if r["_given"]:
            oid = next(iter(r.get("parcel_ids") or []), None)
            found = [{"geometry": r["_given"], "properties": {"OBJECTID": oid} if oid else {}}]
        elif any(o in by_oid for o in r.get("_oids", [])):
            found = [by_oid[o] for o in r["_oids"] if o in by_oid]
        else:
            for ref in r["_refs"]:
                matches = by_ref.get(ref, [])
                if len(matches) > 1 and r.get("lat") is not None:
                    matches = sorted(matches, key=lambda f: haversine_m(
                        r["lat"], r["lon"], *reversed(centroid(f["geometry"]) or (0, 0))))
                if len(matches) > 1:
                    r["parcel_note"] = f"{len(matches)} plots share this reference; showing one"
                found += matches[:1]
        geom = _merge([f["geometry"] for f in found])
        r["parcel_geometry"] = geom
        r["parcel_ids"] = [f["properties"]["OBJECTID"] for f in found
                           if f["properties"].get("OBJECTID")]
        if geom and r.get("lat") is None:
            c = centroid(geom)
            if c:
                r["lon"], r["lat"], r["position"] = round(c[0], 6), round(c[1], 6), "parcel"


LINKED_FIELDS = ("match_3d", "footprints", "height_m", "floors", "building_type", "ground_m",
                 "buildings_3d", "height_estimated", "footprint_m2", "volume_m3")


def link_3d(records: list[dict], dls: DLS) -> None:
    """Attach the DLS 3D buildings standing on each record's parcel, with heights."""
    keys = set()
    for r in records:
        if r.get("parcel_geometry"):
            w, s, e, n = bbox_of(r["parcel_geometry"])
            keys.update(tiles_for_bbox((w - 0.0008, s - 0.0008, e + 0.0008, n + 0.0008)))
    dls.tiles(sorted(keys))  # warm the tile cache in parallel
    for r in records:
        for k in LINKED_FIELDS:
            r.pop(k, None)
        if not r.get("parcel_geometry"):
            continue
        found, mode = dls.buildings_on(r["parcel_geometry"])
        block = False
        if len(found) == 1 and mode in ("on_parcel", "covers_parcel"):
            # A footprint over 3x the plot's size is an old-town block (whose roof height
            # belongs to its tallest part, a church tower or an office block, not to this plot),
            # even when its centre happens to fall inside the plot. Smaller, one that covers the
            # plot is the building on it, spanning plots.
            ratio = (found[0]["properties"].get("a") or 0) / max(area_m2(r["parcel_geometry"]), 1)
            block = ratio > 3
            if block:
                mode = "block"
            elif mode == "covers_parcel":
                mode = "spans_parcel"
        r["match_3d"] = mode
        footprints = []
        for b in found:
            p = b["properties"]
            footprints.append({
                "bid": b["id"],
                "geometry": r["parcel_geometry"] if block else b["geometry"],
                # in a block, its eave height is the best guess for the plot; floors unknown
                "height_m": p["e"] if block else p["h"], "eave_m": p["e"],
                "floors": None if block else p.get("f"),
                "type": p.get("t"), "roof": p.get("r"),
                "volume_m3": None if block else p.get("v"),
                "footprint_m2": None if block else p.get("a"), "ground_m": p.get("g")})
        r["footprints"] = footprints
        if footprints:
            tallest = max(footprints, key=lambda f: f["height_m"])
            r["height_m"] = tallest["height_m"]
            r["floors"] = max((f["floors"] or 0 for f in footprints), default=None) or None
            r["building_type"] = tallest["type"]
            r["ground_m"] = tallest["ground_m"]
            r["buildings_3d"] = len(footprints)
            if block:
                r["height_estimated"] = True
            else:
                r["footprint_m2"] = round(sum(f["footprint_m2"] or 0 for f in footprints))
                r["volume_m3"] = round(sum(f["volume_m3"] or 0 for f in footprints))


def public(r: dict) -> dict:
    """A record without its private build fields."""
    return {k: v for k, v in r.items() if not k.startswith("_")}


def load_seed() -> dict:
    try:
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Registry:
    """The combined EOA registers, cached on disk and refreshed daily in the background."""

    def __init__(self, settings: Settings, dls: DLS, cache_dir=None,
                 fetchers: dict[str, Callable[[Settings], list[dict]]] | None = None):
        self.s = settings
        self.dls = dls
        self.path = (cache_dir or CACHE_DIR) / "eoa_dangerous.json"
        self.fetchers = fetchers or FETCHERS
        self._records: list[dict] = []
        self._sources: dict[str, dict] = {}
        self._fetched_at = 0.0
        self._linked = False
        self._lock = threading.Lock()
        self._building = threading.Lock()
        self._ready = threading.Event()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._records, self._sources = data["records"], data["sources"]
            self._fetched_at, self._linked = data["fetched_at"], data.get("linked_3d", False)
            self._ready.set()
        except (OSError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "fetched_at": self._fetched_at, "linked_3d": self._linked, "sources": self._sources,
            "records": self._records}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")

    @property
    def ready(self) -> bool:
        return bool(self._records)

    @property
    def linked_3d(self) -> bool:
        return self._linked

    @property
    def sources(self) -> dict[str, dict]:
        return self._sources

    def records(self, wait_s: float = 0) -> list[dict]:
        """The records; with wait_s, build them first if there are none (parcels only, the 3D
        link carries on in the background)."""
        if not self._records and wait_s:
            threading.Thread(target=self.refresh_if_needed, daemon=True).start()
            self._ready.wait(wait_s)
        return self._records

    def refresh_if_needed(self) -> None:
        if self._records and time.time() - self._fetched_at < TTL_S and self._linked:
            return
        if not self._building.acquire(blocking=False):
            return
        try:
            if self._records and time.time() - self._fetched_at < TTL_S:
                records = [dict(r) for r in self._records]  # only the 3D link is missing
            else:
                records = self._fetch_all()
                resolve_parcels(records, self.dls)
                self._fetched_at = time.time()
                self._publish(records, linked=False)
            link_3d(records, self.dls)
            self._publish(records, linked=True)
            log.info("EOA registers: %d records, %d with 3D buildings", len(records),
                     sum(1 for r in records if r.get("footprints")))
        except (WebError, ValueError, OSError) as e:
            log.warning("EOA registers refresh failed: %s", e)
        finally:
            self._building.release()
            self._ready.set()  # never leave a caller waiting on a failed build

    def _fetch_all(self) -> list[dict]:
        """Each register live, else its last good copy, else the seed shipped with the app."""
        records, sources = [], {}
        for eoa, fetch in self.fetchers.items():
            try:
                got = fetch(self.s)
                sources[eoa] = {"copy": "live", "count": len(got),
                                "as_of": time.strftime("%Y-%m-%d")}
            except (WebError, ValueError, KeyError) as e:
                reason = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(e)))[:160]
                log.warning("EOA %s register unavailable: %s", eoa, reason)
                got = [r for r in self._records if r["eoa"] == eoa]
                old = self._sources.get(eoa, {})
                copy, as_of = ("cached", old.get("as_of")) if got else ("seed", None)
                if not got:
                    seed = load_seed()
                    got = [r for r in seed.get("records", []) if r["eoa"] == eoa]
                    as_of = seed.get("captured")
                sources[eoa] = {"copy": copy, "count": len(got), "as_of": as_of,
                                "live_error": reason}
            records.extend(got)
        self._sources = sources
        return records

    def _publish(self, records: list[dict], linked: bool) -> None:
        with self._lock:
            self._records = [public(r) for r in records]
            self._linked = linked
            self._save()
        self._ready.set()


# -- presentation helpers (shared by the tool and the /api/dangerous endpoint) ---------------------


def anchor(r: dict) -> tuple[float, float] | None:
    """Where a record really is, as (lon, lat): its plot's centre if known (hand-placed markers
    can be 10-20 m off), else its marker."""
    if r.get("parcel_geometry"):
        c = centroid(r["parcel_geometry"])
        if c:
            return c
    return (r["lon"], r["lat"]) if r.get("lat") is not None else None


def avoid_zone(r: dict, margin_m: float = 10) -> list[list[float]] | None:
    """A rectangle around a record's plot, `margin_m` wider on each side, as a closed ring.
    Streets crossing it pass right in front of the building, so a route avoiding the ring
    keeps clear of it."""
    if r.get("parcel_geometry"):
        w, s, e, n = bbox_of(r["parcel_geometry"])
    elif r.get("lat") is not None:
        w = e = r["lon"]
        s = n = r["lat"]
        margin_m += 8  # a marker only: allow for a plot around it
    else:
        return None
    dlat = margin_m / 110574
    dlon = margin_m / (111320 * math.cos(math.radians((s + n) / 2)))
    w, s, e, n = w - dlon, s - dlat, e + dlon, n + dlat
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]


def place_of(r: dict) -> str:
    return ", ".join(x for x in (r.get("community"), r.get("municipality")) if x) or "?"


def popup(r: dict) -> str:
    lines = [r.get("name") or r.get("address") or place_of(r),
             f"{STATUS_GR[r['status']]} ({r['status'].replace('_', ' ')}) · {EOAS[r['eoa']]['name']}",
             place_of(r) + (f" · {r['parish']}" if r.get("parish") else "")]
    plot = f"Plot {r.get('sheet')}/{r.get('plan')}"
    if r.get("block"):
        plot += f" block {r['block']}"
    lines.append(f"{plot} parcel {r.get('parcel')}")
    if r.get("height_estimated"):
        lines.append(f"DLS 3D: about {r['height_m']} m (old-town block model)")
    elif r.get("height_m"):
        floors = f", {r['floors']} floors" if r.get("floors") else ""
        lines.append(f"DLS 3D: {r['height_m']} m{floors}")
    if r.get("declared"):
        lines.append(f"Declared dangerous {r['declared']}")
    if r.get("instructions"):
        lines.append(r["instructions"][:160])
    return "\n".join(lines)


def row(r: dict, distance_m: float | None = None) -> dict:
    """One record as a flat row for answers and charts."""
    out = {"id": r["id"], "eoa": r["eoa"], "status": r["status"],
           "municipality": r.get("municipality"), "community": r.get("community"),
           "parish": r.get("parish"), "address": r.get("address"), "name": r.get("name"),
           "setting": r.get("setting"), "height_m": r.get("height_m"), "floors": r.get("floors"),
           "height_estimated": r.get("height_estimated"), "match_3d": r.get("match_3d"),
           "buildings_3d": r.get("buildings_3d"), "building_type": r.get("building_type"),
           "footprint_m2": r.get("footprint_m2"), "volume_m3": r.get("volume_m3"),
           "declared": r.get("declared"), "danger_to": r.get("danger_to"),
           "plot": f"{r.get('sheet')}/{r.get('plan')} parcel {r.get('parcel')}",
           "lat": r.get("lat"), "lon": r.get("lon")}
    if distance_m is not None:
        out["distance_m"] = round(distance_m)
    return {k: v for k, v in out.items() if v is not None}


def layer_features(records: list[dict], colors: dict[str, str]) -> list[dict]:
    """A marker per record, its plot outline (_kind parcel) and its DLS 3D buildings
    (_kind footprint, with height_m), all sharing the record id so hovers link up."""
    feats, drawn = [], set()  # some registers list one building several times: draw it once
    for r in records:
        if r.get("lat") is None:
            continue
        base = {"id": r["id"], "label": r.get("address") or r.get("community") or r["id"],
                "state": r["status"], "color": colors[r["status"]], "eoa": r["eoa"]}
        for k in ("height_m", "floors", "volume_m3"):
            if r.get(k) is not None:
                base[k] = r[k]
        text = popup(r)
        feats.append({"type": "Feature", "geometry": {"type": "Point",
                                                      "coordinates": [r["lon"], r["lat"]]},
                      "properties": {**base, "popup": text}})
        plot = json.dumps(r.get("parcel_geometry"))[:200]
        if r.get("parcel_geometry") and plot not in drawn:
            drawn.add(plot)
            feats.append({"type": "Feature", "geometry": r["parcel_geometry"],
                          "properties": {**base, "_kind": "parcel"}})
        for fp in r.get("footprints") or []:
            key = (fp["bid"], plot) if r.get("match_3d") == "block" else fp["bid"]
            if key in drawn:
                continue
            drawn.add(key)
            # The 3D city hides a building this layer redraws (bid), and shows a block that
            # only holds the plot as a translucent ghost (ghost_bid) so the plot shows inside.
            ref = "ghost_bid" if r.get("match_3d") == "block" else "bid"
            feats.append({"type": "Feature", "geometry": fp["geometry"], "properties": {
                **base, "_kind": "footprint", ref: fp["bid"], "height_m": fp["height_m"],
                "eave_m": fp["eave_m"], "popup": text}})
    return feats


def summary(values: list[float]) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    return {"count": len(v), "min": v[0], "median": round(statistics.median(v), 1),
            "p90": v[min(len(v) - 1, math.ceil(0.9 * len(v)) - 1)], "max": v[-1]}


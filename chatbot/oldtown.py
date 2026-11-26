"""The old town's houses. Inside Nicosia's walls the DLS 3D model is block by block: one footprint
for a whole block, at the height of its tallest part. The cadastre has every building's outline
(CadastralMap_EN layer 28, "Buildings") but no heights. Here each such block is split into the
outlines standing in it, for the map's 3D city, with heights estimated from what the block says.

How a block is split:

- A block is a 3D footprint of more than 600 m² inside the walled city that holds at least three
  cadastral outlines (by their centres) covering at least 40% of it. Other footprints stay as
  DLS models them.
- Each house gets the block's mean height: its surveyed volume (`VOL_CubicM`) over its area,
  never more than the block's own top, never less than one storey. DLS doesn't say which house
  is the tall one, so none is made up. In a block DLS gives a flat roof the houses are flat at
  that height; otherwise each carries a hipped or gabled roof 1.8 m high, its eave set so the
  house holds the mean height on average.
- Houses carry `est` (their heights are estimates), `blk` (their block's DLS id, which is what
  the inspector opens) and an id of their own (HOUSE_IDS plus the cadastre's OBJECTID). The
  block itself stays in the tile, marked `split`, so what reads the DLS model (shadows, the
  registers, the inspector) still finds it.

Outlines are fetched once per z16 tile of the walled city, throttled like every DLS request, and
kept on disk; the cadastre's layer is a view/query service, not the paid INSPIRE download.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from chatbot.dls import (DLS, PAGE, TILE_ZOOM, _round_geom, area_m2, bbox_of, centroid, contains,
                         tile_bounds)
from chatbot.settings import CACHE_DIR

log = logging.getLogger(__name__)

CADASTRAL_BUILDINGS_URL = ("https://eservices.dls.moi.gov.cy/arcgis/rest/services/National/"
                           "CadastralMap_EN/MapServer/28")
OID_FIELD = "gisportalmain.GIS.Building.OBJECTID"
# Around the Venetian walls (lon_min, lat_min, lon_max, lat_max): the blocks DLS models whole.
WALLED_CITY = (33.354, 35.168, 33.373, 35.183)
BLOCK_M2 = 600
MIN_HOUSES = 3
MIN_SHARE = 0.4
SMALLEST_M2 = 6  # sheds and slivers of the survey aren't houses
HOUSE_IDS = 50_000_000
ROOF_RISE_M = 1.8
STOREY_M = 3.0
OUTLINES_TTL_S = 30 * 86400
PAD = 0.0008  # about 80 m: a block's houses can reach past the tile its centre is in


def in_walled_city(lon: float, lat: float) -> bool:
    return WALLED_CITY[0] <= lon <= WALLED_CITY[2] and WALLED_CITY[1] <= lat <= WALLED_CITY[3]


def tile_touches_walled_city(x: int, y: int) -> bool:
    w, s, e, n = tile_bounds(x, y)
    return not (e < WALLED_CITY[0] or w > WALLED_CITY[2] or n < WALLED_CITY[1] or s > WALLED_CITY[3])


def split_blocks(buildings: list[dict], outlines: list[dict]) -> list[dict]:
    """The tile's buildings with its old-town blocks split into houses: each split block marked
    `split` (a copy: the DLS tile itself is left as it is) and its houses after it."""
    blocks = []
    for b in buildings:
        c = centroid(b["geometry"])
        if c and in_walled_city(*c) and area_m2(b["geometry"]) > BLOCK_M2:
            blocks.append((b, bbox_of(b["geometry"])))
    if not blocks:
        return buildings
    inside: dict[int, list[tuple[dict, float]]] = {}
    for o in outlines:
        geom = o.get("geometry")
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        c = centroid(geom)
        a = area_m2(geom)
        if c is None or a < SMALLEST_M2:
            continue
        for b, (w, s, e, n) in blocks:
            if w <= c[0] <= e and s <= c[1] <= n and contains(b["geometry"], *c):
                inside.setdefault(b["id"], []).append((o, a))
                break
    out = []
    for b in buildings:
        houses = inside.get(b["id"], [])
        block_area = area_m2(b["geometry"]) if houses else 0.0
        covered = sum(a for _, a in houses)
        if len(houses) < MIN_HOUSES or covered < MIN_SHARE * block_area:
            out.append(b)
            continue
        p = b["properties"]
        top = float(p["h"])
        volume = p.get("v")
        mean = (float(volume) / block_area if volume and block_area
                else (top + float(p.get("e", top))) / 2)
        mean = min(max(mean, STOREY_M), top)
        flat = "flat" in str(p.get("r", "")).lower()
        if flat:  # the block's roofs are flat: so are its houses'
            eave = ridge = round(mean, 1)
        else:
            eave = max(STOREY_M - 0.4, min(mean - ROOF_RISE_M / 3, top - ROOF_RISE_M))
            ridge = round(min(eave + ROOF_RISE_M, top), 1)
        out.append({**b, "properties": {**p, "split": 1, "houses": len(houses)}})
        for o, _a in houses:
            oid = (o.get("properties") or {}).get("OBJECTID", o.get("id"))
            if oid is None:
                continue
            props = {"h": ridge, "e": round(eave, 1),
                     "r": "Flat roof (estimated)" if flat else "Inclined roof (estimated)",
                     "k": "house", "blk": b["id"], "est": 1}
            if "g" in p:
                props["g"] = p["g"]
            out.append({"type": "Feature", "id": HOUSE_IDS + int(oid),
                        "geometry": _round_geom(o["geometry"]), "properties": props})
    return out


class OldTown:
    """Cadastral outlines of the walled city's tiles, and the tiles split into houses."""

    def __init__(self, dls: DLS, cache_dir: Path | None = None):
        self.dls = dls
        self.dir = (cache_dir or CACHE_DIR) / "dls3d" / "cadastre16"

    def _path(self, x: int, y: int) -> Path:
        return self.dir / f"{x}_{y}.json"

    def outlines(self, x: int, y: int) -> list[dict]:
        """The cadastre's building outlines around a z16 tile (padded), from disk or DLS."""
        path = self._path(x, y)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched_at", 0) < OUTLINES_TTL_S:
                return data["features"]
        except (OSError, ValueError):
            pass
        w, s, e, n = tile_bounds(x, y)
        raw, offset = [], 0
        while True:  # the layer joins another table, so its id field carries the table's name
            page = self.dls.query(CADASTRAL_BUILDINGS_URL, {
                "geometry": f"{w - PAD},{s - PAD},{e + PAD},{n + PAD}",
                "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects", "outFields": OID_FIELD, "outSR": 4326,
                "orderByFields": OID_FIELD, "resultOffset": offset, "resultRecordCount": PAGE})
            got = page.get("features") or []
            raw.extend(got)
            more = (page.get("properties") or {}).get("exceededTransferLimit") or page.get("exceededTransferLimit")
            if not got or (len(got) < PAGE and not more):
                break
            offset += len(got)
        feats = []
        for f in raw:
            geom = f.get("geometry")
            props = f.get("properties") or {}
            oid = props.get(OID_FIELD, props.get("OBJECTID", f.get("id")))
            if geom and oid is not None:
                feats.append({"type": "Feature", "id": int(oid), "geometry": _round_geom(geom),
                              "properties": {"OBJECTID": int(oid)}})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "features": feats},
                                   separators=(",", ":")), encoding="utf-8")
        return feats

    def tile(self, x: int, y: int, buildings: list[dict]) -> list[dict]:
        """A z16 tile of the 3D city with its old-town blocks split into houses (outside the
        walls, or if the cadastre can't be read, the tile as DLS models it)."""
        if not tile_touches_walled_city(x, y) or not buildings:
            return buildings
        try:
            return split_blocks(buildings, self.outlines(x, y))
        except Exception as e:  # noqa: BLE001 - the blocks stand as they are
            log.info("old town houses for %s,%s at z%s: %s", x, y, TILE_ZOOM, e)
            return buildings

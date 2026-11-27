"""Mapbox Vector Tiles (the MVT 2.1 format), written and read without dependencies.

The 3D city's tiles are written with it (chatbot/citytiles.py), and the base map's own tiles
(OpenFreeMap) are read with it for their buildings (chatbot/osm3d.py). Coordinates are tile
integers, 0 to `extent` across, y pointing down. A polygon's outer ring has a positive area by
the surveyor's formula in those coordinates (clockwise on screen) and its holes a negative one,
as the format asks; MapLibre splits polygons by it.

Also here: the geometry the tiles need (orientation, area, simplification, clipping to a box).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass, field

EXTENT = 4096
POINT, LINESTRING, POLYGON = 1, 2, 3
MOVE_TO, LINE_TO, CLOSE_PATH = 1, 2, 7

Ring = list[tuple[int, int]]  # open: the last point does not repeat the first


@dataclass
class Feature:
    """One feature of a layer. For polygons, `rings` are outer rings each followed by its holes."""
    rings: list[Ring]
    properties: dict = field(default_factory=dict)
    id: int | None = None
    type: int = POLYGON


# -- writing ---------------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _key(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _delimited(field_no: int, payload: bytes) -> bytes:
    return _key(field_no, 2) + _varint(len(payload)) + payload


def _packed(field_no: int, values: Iterable[int]) -> bytes:
    buf = bytearray()
    for v in values:  # inline: most values fit one byte, and this runs for every coordinate
        while v > 0x7F:
            buf.append((v & 0x7F) | 0x80)
            v >>= 7
        buf.append(v)
    return _key(field_no, 2) + _varint(len(buf)) + bytes(buf)


def _value(v: object) -> bytes:
    if isinstance(v, bool):
        return _key(7, 0) + _varint(int(v))
    if isinstance(v, int):
        return _key(6, 0) + _varint(_zigzag(v))
    if isinstance(v, float):
        return _key(3, 1) + struct.pack("<d", v)
    return _delimited(1, str(v).encode("utf-8"))


def encode_geometry(rings: list[Ring], kind: int = POLYGON) -> list[int]:
    """The command stream for rings (polygons) or lines; points are one MoveTo per point."""
    out: list[int] = []
    cx = cy = 0
    for ring in rings:
        if not ring:
            continue
        if kind == POINT:
            out.append(MOVE_TO | (len(ring) << 3))
            for x, y in ring:
                out += (_zigzag(x - cx), _zigzag(y - cy))
                cx, cy = x, y
            continue
        x, y = ring[0]
        out += (MOVE_TO | (1 << 3), _zigzag(x - cx), _zigzag(y - cy))
        cx, cy = x, y
        out.append(LINE_TO | ((len(ring) - 1) << 3))
        for x, y in ring[1:]:
            out += (_zigzag(x - cx), _zigzag(y - cy))
            cx, cy = x, y
        if kind == POLYGON:
            out.append(CLOSE_PATH | (1 << 3))
    return out


def encode_layer(name: str, features: Iterable[Feature], extent: int = EXTENT) -> bytes:
    """One layer, as the bytes of a tile holding only it (layers concatenate into a tile)."""
    keys: dict[str, int] = {}
    values: dict[tuple[str, object], int] = {}
    body = bytearray()
    for f in features:
        geometry = encode_geometry(f.rings, f.type)
        if not geometry:
            continue
        tags: list[int] = []
        for k, v in f.properties.items():
            if v is None:
                continue
            ki = keys.setdefault(k, len(keys))
            vi = values.setdefault((type(v).__name__, v), len(values))
            tags += (ki, vi)
        feat = bytearray()
        if f.id is not None:
            feat += _key(1, 0) + _varint(int(f.id))
        if tags:
            feat += _packed(2, tags)
        feat += _key(3, 0) + _varint(f.type)
        feat += _packed(4, geometry)
        body += _delimited(2, bytes(feat))
    layer = bytearray(_key(15, 0) + _varint(2) + _delimited(1, name.encode("utf-8")))
    layer += body
    for k in keys:
        layer += _delimited(3, k.encode("utf-8"))
    for (_, v) in values:
        layer += _delimited(4, _value(v))
    layer += _key(5, 0) + _varint(extent)
    return _delimited(3, bytes(layer))


# -- reading ---------------------------------------------------------------------------------


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes):
    """(field number, value) for each field of a message: ints for varints, bytes otherwise."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        wire = key & 7
        if wire == 0:
            v, i = _read_varint(buf, i)
        elif wire == 1:
            v, i = buf[i:i + 8], i + 8
        elif wire == 2:
            size, i = _read_varint(buf, i)
            v, i = buf[i:i + size], i + size
        elif wire == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield key >> 3, v


def _unpack(buf: bytes) -> list[int]:
    out: list[int] = []
    append = out.append
    i, n = 0, len(buf)
    while i < n:
        b = buf[i]
        if b < 0x80:  # one byte, the usual case
            append(b)
            i += 1
        else:
            v, i = _read_varint(buf, i)
            append(v)
    return out


def _read_value(buf: bytes) -> object:
    for no, v in _fields(buf):
        if no == 1:
            return bytes(v).decode("utf-8")
        if no == 2:
            return struct.unpack("<f", v)[0]
        if no == 3:
            return struct.unpack("<d", v)[0]
        if no == 4:
            return v - (1 << 64) if v >= 1 << 63 else v
        if no == 5:
            return v
        if no == 6:
            return (v >> 1) ^ -(v & 1)
        if no == 7:
            return bool(v)
    return None


def decode_geometry(commands: list[int]) -> list[Ring]:
    """Rings (or lines, or one ring per point) from a command stream."""
    parts: list[Ring] = []
    cur: Ring = []
    x = y = 0
    i, n = 0, len(commands)
    while i < n:
        c = commands[i]
        i += 1
        op, count = c & 7, c >> 3
        if op == MOVE_TO or op == LINE_TO:
            for _ in range(count):
                dx, dy = commands[i], commands[i + 1]
                i += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                if op == MOVE_TO:
                    cur = []
                    parts.append(cur)
                cur.append((x, y))
    return parts


@dataclass
class Layer:
    name: str
    extent: int
    features: list[Feature]


def decode(tile: bytes, only: Iterable[str] | None = None) -> dict[str, Layer]:
    """The layers of a tile by name (just the ones in `only`, if given)."""
    wanted = set(only) if only is not None else None
    out: dict[str, Layer] = {}
    for no, raw in _fields(tile):
        if no != 3:
            continue
        name, extent, keys, values, feats = "", EXTENT, [], [], []
        for lno, v in _fields(raw):
            if lno == 1:
                name = bytes(v).decode("utf-8")
            elif lno == 2:
                feats.append(v)
            elif lno == 3:
                keys.append(bytes(v).decode("utf-8"))
            elif lno == 4:
                values.append(_read_value(v))
            elif lno == 5:
                extent = v
        if wanted is not None and name not in wanted:
            continue
        layer = Layer(name, extent, [])
        for raw_f in feats:
            fid, tags, kind, geom = None, [], 0, []
            for fno, v in _fields(raw_f):
                if fno == 1:
                    fid = v
                elif fno == 2:
                    tags = _unpack(v)
                elif fno == 3:
                    kind = v
                elif fno == 4:
                    geom = _unpack(v)
            props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)}
            layer.features.append(Feature(decode_geometry(geom), props, fid, kind))
        out[name] = layer
    return out


# -- geometry in tile coordinates ------------------------------------------------------------


def area2(ring: Ring) -> float:
    """Twice the signed area (surveyor's formula): positive for an outer ring, y down."""
    n = len(ring)
    s = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s


def oriented(ring: Ring, outer: bool) -> Ring:
    a = area2(ring)
    return ring if (a > 0) == outer else ring[::-1]


def polygons(rings: list[Ring]) -> list[list[Ring]]:
    """Rings grouped into polygons: a ring turned like the first one starts a new polygon."""
    out: list[list[Ring]] = []
    sign = 0
    for r in rings:
        a = area2(r)
        if a == 0:
            continue
        if not sign:
            sign = 1 if a > 0 else -1
        if (a > 0) == (sign > 0) or not out:
            out.append([r])
        else:
            out[-1].append(r)
    return out


def centroid(ring: Ring) -> tuple[float, float]:
    """Area-weighted centre of a ring (the vertex mean if it has no area)."""
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        c = x1 * y2 - x2 * y1
        a += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    if abs(a) < 1e-9:
        return sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n
    return cx / (3 * a), cy / (3 * a)


def cleaned(ring: list[tuple[float, float]]) -> Ring:
    """Rounded to integers, with repeated points (and a closing point) dropped."""
    out: Ring = []
    for x, y in ring:
        p = (round(x), round(y))
        if not out or out[-1] != p:
            out.append(p)
    while len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _dp(pts: list[tuple[float, float]], lo: int, hi: int, tol2: float, keep: list[bool]) -> None:
    stack = [(lo, hi)]
    while stack:
        lo, hi = stack.pop()
        ax, ay = pts[lo]
        bx, by = pts[hi]
        dx, dy = bx - ax, by - ay
        d2 = dx * dx + dy * dy
        best, at = -1.0, -1
        for k in range(lo + 1, hi):
            px, py = pts[k]
            if d2 == 0:
                e = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / d2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                e = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if e > best:
                best, at = e, k
        if at >= 0 and best > tol2:
            keep[at] = True
            stack.append((lo, at))
            stack.append((at, hi))


def simplify(ring: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on a closed ring (open form), splitting it at its first point and the
    point farthest from it, so the ring's extremes stay."""
    n = len(ring)
    if tol <= 0 or n <= 4:
        return ring
    x0, y0 = ring[0]
    far = max(range(n), key=lambda k: (ring[k][0] - x0) ** 2 + (ring[k][1] - y0) ** 2)
    pts = ring + [ring[0]]
    keep = [False] * (n + 1)
    keep[0] = keep[far] = keep[n] = True
    _dp(pts, 0, far, tol * tol, keep)
    _dp(pts, far, n, tol * tol, keep)
    return [pts[k] for k in range(n) if keep[k]]


def clip(ring: list[tuple[float, float]], x0: float, y0: float, x1: float,
         y1: float) -> list[tuple[float, float]]:
    """A ring cut to a box (Sutherland-Hodgman). A concave ring can come out with a seam along
    the box's edge, which an extrusion never shows."""
    def cut(pts, inside, cross):
        out = []
        if not pts:
            return out
        prev = pts[-1]
        for p in pts:
            if inside(p):
                if not inside(prev):
                    out.append(cross(prev, p))
                out.append(p)
            elif inside(prev):
                out.append(cross(prev, p))
            prev = p
        return out

    def at_x(xc):
        return lambda a, b: (xc, a[1] + (b[1] - a[1]) * (xc - a[0]) / (b[0] - a[0]))

    def at_y(yc):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (yc - a[1]) / (b[1] - a[1]), yc)

    pts = list(ring)
    pts = cut(pts, lambda p: p[0] >= x0, at_x(x0))
    pts = cut(pts, lambda p: p[0] <= x1, at_x(x1))
    pts = cut(pts, lambda p: p[1] >= y0, at_y(y0))
    pts = cut(pts, lambda p: p[1] <= y1, at_y(y1))
    return pts

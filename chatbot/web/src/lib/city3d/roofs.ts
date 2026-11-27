/* Roofs for the DLS 3D city, built from what the model says about each building: its footprint,
   the roof type (ROOF_TYPE), the eave height (H_EAVE_B_1) and the roof's highest point
   (H_ROOF_A_1). The model is LoD1, one height per footprint; this gives each building an LoD2-like
   roof shape, laid out the usual way for roofs drawn from footprints:

   - flat: the walls go up to the eave; a stair head (the small room every Cypriot flat roof has
     over its stairs) rises to the highest point when that is 1.5 to 6 m above the eave;
   - one height point ("inclined roof with one height point"): a hipped roof, its ridge along the
     building's long side (a pyramid on a square house);
   - several height points: a gabled roof, its ridge along the long side;
   - mixed: a hipped roof over the middle of the building with a flat margin around it, the way
     a tiled roof sits on a house with terraces;
   - estimated (the old town's houses, chatbot/oldtown.py): hipped, or gabled on a long house.

   The ridge follows the footprint's smallest enclosing rectangle, and the roof is cut to the
   footprint, with gable walls where the roof stands over a wall. A roof steeper than about 42°
   for its building is lowered (the eave rises, the top stays where DLS measured it), and very
   large footprints (blocks and complexes) keep the model's flat top: one pitched roof over them
   would be made up. Heights are metres above the ground. */
import earcut from "earcut"

export type RoofKind = "flat" | "hip" | "gable" | "mixed"
export type BuildingProps = { h: number; e?: number; r?: string; k?: string; split?: number }

// What the walls (MapLibre's fill-extrusion) and the mesh layer share for one building.
export type RoofPlan = {
  kind: RoofKind
  wall: number // where the walls stop: the eave of a pitched roof, the top of a flat one
  top: number
  stairHead: boolean
}

export type Mesh = {
  // Per vertex: x, y in metres east and north of the anchor, z in metres above the ground.
  pos: number[]
  // Per vertex: the face's normal in MapLibre's tile frame (x east, y south, z up).
  normal: number[]
  // Per vertex: 0 a roof face, 1 a wall (a gable end or the stair head)
  mat: number[]
}

const MAX_SLOPE = 0.9 // rise over half-width, about 42°
const MIN_SLOPE = 0.08 // flatter than this reads as flat
const BIG_M2 = 1200 // a footprint this large is a block or a complex: the model's flat top stays
const BIG_WIDTH_M = 34

export function roofKind(p: BuildingProps): RoofKind {
  const r = (p.r ?? "").toLowerCase()
  if (r.includes("flat")) return "flat"
  if (r.includes("estimated")) return "hip" // decided by shape below
  if (r.includes("several") || r.includes("window")) return "gable"
  if (r.includes("one") || r === "rt_in_one") return "hip"
  if (r.includes("mixed")) return "mixed"
  return (p.h - (p.e ?? p.h)) > 1 ? "hip" : "flat"
}

// -- the footprint ------------------------------------------------------------------------------

type P = [number, number]
type Frame = { lon0: number; lat0: number; kx: number; ky: number }

export function frameAt(lon0: number, lat0: number): Frame {
  return { lon0, lat0, kx: 111320 * Math.cos((lat0 * Math.PI) / 180), ky: 110574 }
}

const toLocal = (f: Frame, c: number[]): P => [(c[0] - f.lon0) * f.kx, (c[1] - f.lat0) * f.ky]

function cross(o: P, a: P, b: P) {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
}

function hull(pts: P[]): P[] {
  const s = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1])
  if (s.length < 3) return s
  const lower: P[] = [], upper: P[] = []
  for (const p of s) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop()
    lower.push(p)
  }
  for (let i = s.length - 1; i >= 0; i--) {
    const p = s[i]
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop()
    upper.push(p)
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1))
}

// The smallest rectangle around the footprint: its centre, its long axis (unit), and its half
// length and half width.
export type Box = { cx: number; cy: number; ux: number; uy: number; L: number; W: number }

export function minBox(pts: P[]): Box | null {
  const h = hull(pts)
  if (h.length < 3) return null
  let best: Box | null = null, bestArea = Infinity
  for (let i = 0; i < h.length; i++) {
    const a = h[i], b = h[(i + 1) % h.length]
    const len = Math.hypot(b[0] - a[0], b[1] - a[1])
    if (len < 1e-6) continue
    const ux = (b[0] - a[0]) / len, uy = (b[1] - a[1]) / len
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity
    for (const p of h) {
      const u = p[0] * ux + p[1] * uy, v = -p[0] * uy + p[1] * ux
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v)
    }
    const area = (u1 - u0) * (v1 - v0)
    if (area < bestArea) {
      bestArea = area
      const cu = (u0 + u1) / 2, cv = (v0 + v1) / 2
      const box = { cx: cu * ux - cv * uy, cy: cu * uy + cv * ux, ux, uy, L: (u1 - u0) / 2, W: (v1 - v0) / 2 }
      // The long axis is u.
      best = box.L >= box.W ? box : { ...box, ux: -uy, uy: ux, L: box.W, W: box.L }
    }
  }
  return best
}

function ringArea(r: P[]) {
  let a = 0
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) a += (r[j][0] - r[i][0]) * (r[j][1] + r[i][1])
  return a / 2 // positive: counter-clockwise
}

function polygonsOf(geom: { type: string; coordinates: any }): number[][][][] { // eslint-disable-line @typescript-eslint/no-explicit-any
  if (geom.type === "Polygon") return [geom.coordinates]
  if (geom.type === "MultiPolygon") return geom.coordinates
  return []
}

// -- the plan -----------------------------------------------------------------------------------

export type Planned = { plan: RoofPlan; parts: { ring: P[]; box: Box }[]; frame: Frame }

// The roof of one building: its kind, where its walls stop, and its parts in local metres. The
// walls are drawn by MapLibre at `wall`; the mesh layer draws the rest.
export function planRoof(p: BuildingProps, geom: { type: string; coordinates: any }): Planned | null { // eslint-disable-line @typescript-eslint/no-explicit-any
  const polys = polygonsOf(geom)
  const first = polys[0]?.[0]?.[0]
  if (!first) return null
  const frame = frameAt(first[0], first[1])
  const top = p.h
  let eave = Math.min(p.e ?? top, top)
  let kind = roofKind(p)
  const parts: { ring: P[]; box: Box }[] = []
  let area = 0
  for (const poly of polys) {
    const ring = (poly[0] ?? []).map((c) => toLocal(frame, c))
    if (ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]) ring.pop()
    if (ring.length < 3) continue
    if (ringArea(ring) < 0) ring.reverse()
    const box = minBox(ring)
    if (!box) continue
    area += ringArea(ring)
    parts.push({ ring, box })
  }
  if (!parts.length) return null
  const widest = Math.max(...parts.map((q) => q.box.W))
  const narrowest = Math.min(...parts.map((q) => q.box.W))
  let rise = top - eave
  if (area > BIG_M2 || widest > BIG_WIDTH_M) kind = "flat" // a block: as the model draws it
  if (kind !== "flat") {
    const half = kind === "mixed" ? narrowest - inset(narrowest) : narrowest
    const most = MAX_SLOPE * half
    if (rise > most) { rise = most; eave = top - rise }
    if (rise < MIN_SLOPE * half || half < 1.2) kind = "flat"
  }
  if ((p.r ?? "").toLowerCase().includes("estimated") && kind !== "flat") {
    kind = parts[0].box.L > 1.6 * parts[0].box.W ? "gable" : "hip"
  }
  if (kind === "flat") {
    const above = top - eave
    const stairHead = above >= 1.5 && above <= 6 && area > 60 && area <= BIG_M2 && widest <= BIG_WIDTH_M
    return { plan: { kind, wall: stairHead ? eave : top, top, stairHead }, parts, frame }
  }
  return { plan: { kind, wall: eave, top, stairHead: false }, parts, frame }
}

function inset(W: number) {
  return Math.min(0.25 * W, 2.5)
}

// -- the faces ----------------------------------------------------------------------------------

// A roof facet: a convex polygon (local metres) and its height above the eave there, as a plane
// z = a x + b y + c.
type Facet = { poly: P[]; a: number; b: number; c: number }

// The facets of a ridge roof over a box, the height going from 0 at the eaves to `rise` at the
// ridge. Hipped: two sides and two ends of equal slope (a pyramid when L = W). Gabled: two sides.
function facets(box: Box, kind: RoofKind, rise: number): Facet[] {
  let { L, W } = box
  if (kind === "mixed") { const d = inset(W); L -= d; W -= d }
  const { cx, cy, ux, uy } = box
  const vx = -uy, vy = ux
  const g = 1.002 // a hair larger, so points on the box's edge fall inside a facet
  const at = (u: number, v: number): P => [cx + u * g * ux + v * g * vx, cy + u * g * uy + v * g * vy]
  // A plane in (u, v), z = rise * (pu u + pv v + p0), turned into local x, y.
  const plane = (pu: number, pv: number, p0: number, poly: P[]): Facet => {
    // u = (x - cx) ux + (y - cy) uy ; v = (x - cx) vx + (y - cy) vy
    const a = rise * (pu * ux + pv * vx), b = rise * (pu * uy + pv * vy)
    const c = rise * p0 - a * cx - b * cy
    return { poly, a, b, c }
  }
  if (kind === "gable") {
    return [
      plane(0, -1 / W, 1, [at(-L, 0), at(L, 0), at(L, W), at(-L, W)]),
      plane(0, 1 / W, 1, [at(-L, -W), at(L, -W), at(L, 0), at(-L, 0)]),
    ]
  }
  const r = Math.max(0, L - W) // half the ridge
  return [
    plane(0, -1 / W, 1, [at(-r, 0), at(r, 0), at(L, W), at(-L, W)]),
    plane(0, 1 / W, 1, [at(-L, -W), at(L, -W), at(r, 0), at(-r, 0)]),
    plane(-1 / W, 0, L / W, [at(r, 0), at(L, -W), at(L, W)]),
    plane(1 / W, 0, L / W, [at(-r, 0), at(-L, W), at(-L, -W)]),
  ]
}

// Sutherland-Hodgman: a (possibly concave) ring cut to a convex, counter-clockwise polygon.
function clip(ring: P[], convex: P[]): P[] {
  let out = ring
  for (let i = 0; i < convex.length && out.length; i++) {
    const a = convex[i], b = convex[(i + 1) % convex.length]
    const inside = (p: P) => cross(a, b, p) >= -1e-9
    const cut = (p: P, q: P): P => {
      const dp = cross(a, b, p), dq = cross(a, b, q)
      const t = dp / (dp - dq)
      return [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]
    }
    const input = out
    out = []
    for (let j = 0; j < input.length; j++) {
      const p = input[j], q = input[(j + 1) % input.length]
      const pin = inside(p), qin = inside(q)
      if (pin) out.push(p)
      if (pin !== qin) out.push(cut(p, q))
    }
  }
  return out
}

function ccw(poly: P[]) {
  return ringArea(poly) >= 0 ? poly : [...poly].reverse()
}

function roofHeight(fs: Facet[], x: number, y: number) {
  // The roof is the lowest of its planes, and never under the eave.
  let z = Infinity
  for (const f of fs) z = Math.min(z, f.a * x + f.b * y + f.c)
  return Math.max(0, z)
}

function pushTri(m: Mesh, pts: [number, number, number][], n: [number, number, number], mat: number) {
  for (const p of pts) { m.pos.push(p[0], p[1], p[2]); m.normal.push(n[0], n[1], n[2]); m.mat.push(mat) }
}

// The plane's normal in MapLibre's tile frame (y south).
function planeNormal(f: Facet): [number, number, number] {
  const len = Math.hypot(f.a, f.b, 1)
  return [-f.a / len, f.b / len, 1 / len]
}

// Where a segment p-q crosses the boundaries of the facets (as fractions of its length).
function breaks(p: P, q: P, fs: Facet[]) {
  const ts = [0, 1]
  for (const f of fs) {
    for (let i = 0; i < f.poly.length; i++) {
      const a = f.poly[i], b = f.poly[(i + 1) % f.poly.length]
      const d1 = cross(a, b, p), d2 = cross(a, b, q)
      if ((d1 > 0) === (d2 > 0) || d1 === d2) continue
      const t = d1 / (d1 - d2)
      if (t > 1e-4 && t < 1 - 1e-4) ts.push(t)
    }
  }
  return [...new Set(ts.map((t) => Math.round(t * 1e5) / 1e5))].sort((a, b) => a - b)
}

// The faces above the walls: the roof's facets cut to the footprint, gable walls where the roof
// stands over a wall, or the stair head of a flat roof. Base heights are the plan's wall top.
export function roofMesh(planned: Planned): Mesh {
  const { plan, parts } = planned
  const m: Mesh = { pos: [], normal: [], mat: [] }
  const base = plan.wall
  if (plan.kind === "flat") {
    if (plan.stairHead) for (const part of parts) stairHead(m, part.ring, part.box, base, plan.top)
    return m
  }
  const rise = plan.top - plan.wall
  for (const { ring, box } of parts) {
    const fs = facets(box, plan.kind, rise)
    for (const f of fs) {
      const piece = clip(ring, ccw(f.poly))
      if (piece.length < 3 || Math.abs(ringArea(piece)) < 0.05) continue
      const flat = piece.flatMap((p) => p)
      const tris = earcut(flat)
      const n = planeNormal(f)
      for (let i = 0; i < tris.length; i += 3) {
        const pts = [tris[i], tris[i + 1], tris[i + 2]].map((k): [number, number, number] => {
          const [x, y] = piece[k]
          return [x, y, base + Math.max(0, f.a * x + f.b * y + f.c)]
        })
        pushTri(m, pts, n, 0)
      }
    }
    // Gable walls: along each wall, up from the eave to the roof wherever the roof is above it.
    for (let i = 0; i < ring.length; i++) {
      const p = ring[i], q = ring[(i + 1) % ring.length]
      const len = Math.hypot(q[0] - p[0], q[1] - p[1])
      if (len < 0.05) continue
      const n: [number, number, number] = [(q[1] - p[1]) / len, (q[0] - p[0]) / len, 0] // outward, y south
      const ts = breaks(p, q, fs)
      for (let k = 0; k + 1 < ts.length; k++) {
        const a: P = [p[0] + (q[0] - p[0]) * ts[k], p[1] + (q[1] - p[1]) * ts[k]]
        const b: P = [p[0] + (q[0] - p[0]) * ts[k + 1], p[1] + (q[1] - p[1]) * ts[k + 1]]
        const za = roofHeight(fs, a[0], a[1]), zb = roofHeight(fs, b[0], b[1])
        if (za < 0.05 && zb < 0.05) continue
        const A0: [number, number, number] = [a[0], a[1], base], B0: [number, number, number] = [b[0], b[1], base]
        const A1: [number, number, number] = [a[0], a[1], base + za], B1: [number, number, number] = [b[0], b[1], base + zb]
        if (za >= 0.05) pushTri(m, [A0, B0, A1], n, 1)
        if (zb >= 0.05) pushTri(m, [B0, B1, A1], n, 1)
      }
    }
  }
  return m
}

// The room over the stairs: about 3.5 by 4.5 m in the middle of the roof, if the middle is on it.
function stairHead(m: Mesh, ring: P[], box: Box, base: number, top: number) {
  if (!insideRing(ring, box.cx, box.cy)) return
  const hu = Math.min(2.25, box.L * 0.4), hv = Math.min(1.75, box.W * 0.4)
  const { cx, cy, ux, uy } = box
  const vx = -uy, vy = ux
  const c = (u: number, v: number): P => [cx + u * ux + v * vx, cy + u * uy + v * vy]
  const corners = [c(-hu, -hv), c(hu, -hv), c(hu, hv), c(-hu, hv)]
  for (let i = 0; i < 4; i++) {
    const p = corners[i], q = corners[(i + 1) % 4]
    const len = Math.hypot(q[0] - p[0], q[1] - p[1])
    const n: [number, number, number] = [(q[1] - p[1]) / len, (q[0] - p[0]) / len, 0]
    pushTri(m, [[p[0], p[1], base], [q[0], q[1], base], [p[0], p[1], top]], n, 1)
    pushTri(m, [[q[0], q[1], base], [q[0], q[1], top], [p[0], p[1], top]], n, 1)
  }
  const up: [number, number, number] = [0, 0, 1]
  const [a, b, cc, d] = corners
  pushTri(m, [[a[0], a[1], top], [b[0], b[1], top], [cc[0], cc[1], top]], up, 1)
  pushTri(m, [[a[0], a[1], top], [cc[0], cc[1], top], [d[0], d[1], top]], up, 1)
}

function insideRing(ring: P[], x: number, y: number) {
  let inside = false
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j]
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

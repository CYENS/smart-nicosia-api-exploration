/* Trees in the 3D city (chatbot/trees.py: OpenStreetMap's mapped trees and tree rows, and trees
   estimated in its woods, orchards and parks). Simple trees: a trunk and a crown, round for a
   broadleaf, a tall cone for a pine or cypress, a fan of fronds for a palm, each at its height
   and crown size, standing on the terrain in a view.

   They load with the 3D city's tiles (the engine calls loadTrees for each tile it loads) and
   draw as one instanced mesh per kind of tree, in the city's depth buffer and light, so a few
   tens of thousands cost little. A switch in the Layers sheet and the view card turns them off
   (remembered on this device). */
import type { CustomLayerInterface, CustomRenderMethodInput, Map as MlMap } from "maplibre-gl"
import { create } from "zustand"

import { compile, lightOf, mercX, mercY, originMatrix } from "@/lib/city3d/mesh-layer"
import { registerActiveSources } from "@/lib/sources"

export const TREES = "city3d::trees"
const EARTH = 40075016.68557849
const KEY = "twin.trees"
const KINDS = ["mapped", "row", "wood", "orchard", "park"]
const LEAVES = ["broadleaved", "needleleaved", "palm"] as const

type TreeState = { on: boolean; mapped: number; estimated: number; status: "" | "loading" | "unavailable" }

function remembered() {
  try { return localStorage.getItem(KEY) !== "off" } catch { return true }
}

export const useTrees = create<TreeState>(() => ({ on: remembered(), mapped: 0, estimated: 0, status: "" }))

registerActiveSources(() => {
  const s = useTrees.getState()
  return s.on && s.mapped + s.estimated > 0 ? ["openstreetmap"] : []
}, (fn) => useTrees.subscribe(fn))

// -- the shapes ---------------------------------------------------------------------------------

// A unit tree: x, y in crown radii (the trunk in metres), z as a share of the tree's height.
// Per vertex: x, y, z, nx, ny, nz (y south, as MapLibre lights things), part (0 trunk, 1 crown).
type Shape = Float32Array

function ring(n: number, i: number) {
  const a = (i / n) * Math.PI * 2
  return [Math.cos(a), Math.sin(a)]
}

function pushV(out: number[], x: number, y: number, z: number, nx: number, ny: number, nz: number, part: number) {
  const len = Math.hypot(nx, ny, nz) || 1
  out.push(x, y, z, nx / len, -ny / len, nz / len, part)
}

function trunk(out: number[], top: number, sides = 6) {
  for (let i = 0; i < sides; i++) {
    const [x1, y1] = ring(sides, i), [x2, y2] = ring(sides, i + 1)
    const [mx, my] = ring(sides, i + 0.5)
    pushV(out, x1, y1, 0, mx, my, 0, 0); pushV(out, x2, y2, 0, mx, my, 0, 0); pushV(out, x1, y1, top, mx, my, 0, 0)
    pushV(out, x2, y2, 0, mx, my, 0, 0); pushV(out, x2, y2, top, mx, my, 0, 0); pushV(out, x1, y1, top, mx, my, 0, 0)
  }
}

// A crown of revolution: its profile as (radius share, height share) from bottom to top.
function crown(out: number[], profile: [number, number][], sides = 8) {
  for (let k = 0; k + 1 < profile.length; k++) {
    const [r1, z1] = profile[k], [r2, z2] = profile[k + 1]
    const slope = (r1 - r2) / Math.max(1e-3, z2 - z1) * 0.35
    for (let i = 0; i < sides; i++) {
      const [ax, ay] = ring(sides, i), [bx, by] = ring(sides, i + 1)
      const n = (x: number, y: number) => [x, y, slope] as const
      const tri = (pts: [number, number, number][]) => {
        for (const p of pts) { const [nx, ny, nz] = n(p[0] === 0 && p[1] === 0 ? 0 : p[0], p[1]); pushV(out, p[0], p[1], p[2], nx, ny, nz || 0.4, 1) }
      }
      if (r1 > 0) tri([[ax * r1, ay * r1, z1], [bx * r1, by * r1, z1], [ax * r2, ay * r2, z2]])
      if (r2 > 0) tri([[bx * r1, by * r1, z1], [bx * r2, by * r2, z2], [ax * r2, ay * r2, z2]])
    }
  }
}

function shape(leaf: (typeof LEAVES)[number]): Shape {
  const out: number[] = []
  if (leaf === "palm") {
    trunk(out, 0.86, 5)
    // Fronds: a low, wide crown at the top, drooping at its rim.
    crown(out, [[0.0, 0.8], [0.95, 0.84], [1.0, 0.9], [0.55, 0.99], [0.0, 1.0]], 9)
  } else if (leaf === "needleleaved") {
    trunk(out, 0.3)
    crown(out, [[0.0, 0.18], [0.95, 0.22], [0.8, 0.5], [0.45, 0.8], [0.0, 1.0]])
  } else {
    trunk(out, 0.4)
    crown(out, [[0.0, 0.3], [0.7, 0.34], [1.0, 0.52], [0.92, 0.72], [0.55, 0.92], [0.0, 1.0]])
  }
  return new Float32Array(out)
}

// -- the layer ------------------------------------------------------------------------------------

const VS = `
uniform mat4 u_matrix;
uniform float u_scale;
uniform vec3 u_lightpos;
uniform float u_lightintensity;
uniform vec3 u_lightcolor;
uniform float u_night;
uniform vec3 u_crown;
uniform vec3 u_trunk;
IN vec3 a_pos;
IN vec3 a_normal;
IN float a_part;
IN vec4 a_at;   // mercator x, y from the origin, ground (m), height (m)
IN vec2 a_size; // crown radius (m), tone (0 to 1)
OUT vec4 v_color;
void main() {
  float h = a_at.w;
  float trunkR = 0.12 + 0.018 * h;
  float r = a_part > 0.5 ? a_size.x : trunkR;
  vec3 m = vec3(a_pos.x * r, a_pos.y * r, a_pos.z * h);
  gl_Position = u_matrix * vec4(a_at.x + m.x * u_scale, a_at.y - m.y * u_scale, (a_at.z + m.z) * u_scale, 1.0);
  vec3 color = a_part > 0.5 ? u_crown * (0.82 + 0.36 * a_size.y) : u_trunk;
  color = mix(color, color * vec3(0.22, 0.26, 0.36), u_night);
  float lum = dot(color, vec3(0.2126, 0.7152, 0.0722));
  color += vec3(0.03);
  float d = clamp(dot(a_normal, u_lightpos), 0.0, 1.0);
  d = mix(1.0 - u_lightintensity, max(1.0 - lum + u_lightintensity, 1.0), d);
  vec3 c = clamp(color * d * u_lightcolor, mix(vec3(0.0), vec3(0.3), vec3(1.0) - u_lightcolor), vec3(1.0));
  v_color = vec4(c, 1.0);
}`
const FS = `
precision mediump float;
IN vec4 v_color;
FRAG_DECL
void main() { FRAG_OUT = v_color; }`

const CROWNS: Record<(typeof LEAVES)[number], [number, number, number]> = {
  broadleaved: [0.31, 0.45, 0.22], needleleaved: [0.2, 0.34, 0.22], palm: [0.42, 0.52, 0.24],
}
const TRUNK: [number, number, number] = [0.36, 0.28, 0.2]

type Inst = { lon: number; lat: number; h: number; r: number; tone: number }
type Group = { shape: Shape; n: number; geo: WebGLBuffer | null; inst: WebGLBuffer | null; data: Float32Array; list: Inst[]; dirty: boolean }

type Instancing = {
  draw: (mode: number, first: number, count: number, instances: number) => void
  divisor: (loc: number, d: number) => void
}

class TreeLayer implements CustomLayerInterface {
  id = TREES
  type = "custom" as const
  renderingMode = "3d" as const
  private map: MlMap | null = null
  private gl: WebGLRenderingContext | WebGL2RenderingContext | null = null
  private program: WebGLProgram | null = null
  private inst: Instancing | null = null
  private loc: Record<string, number> = {}
  private uni: Record<string, WebGLUniformLocation | null> = {}
  private origin: [number, number] | null = null
  private originLat = 35.17
  groups = Object.fromEntries(LEAVES.map((l) => [l, { shape: shape(l), n: 0, geo: null, inst: null, data: new Float32Array(0), list: [], dirty: false } as Group])) as Record<(typeof LEAVES)[number], Group>
  night = 1
  ground: ((lon: number, lat: number) => number) | null = null

  onAdd(map: MlMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    this.map = map
    this.gl = gl
    this.program = compile(gl, VS, FS)
    for (const a of ["a_pos", "a_normal", "a_part", "a_at", "a_size"]) this.loc[a] = gl.getAttribLocation(this.program, a)
    for (const u of ["u_matrix", "u_scale", "u_lightpos", "u_lightintensity", "u_lightcolor", "u_night", "u_crown", "u_trunk"]) {
      this.uni[u] = gl.getUniformLocation(this.program, u)
    }
    if (typeof WebGL2RenderingContext !== "undefined" && gl instanceof WebGL2RenderingContext) {
      this.inst = { draw: (m, f, c, n) => gl.drawArraysInstanced(m, f, c, n), divisor: (l, d) => gl.vertexAttribDivisor(l, d) }
    } else {
      const ext = gl.getExtension("ANGLE_instanced_arrays")
      if (ext) this.inst = { draw: (m, f, c, n) => ext.drawArraysInstancedANGLE(m, f, c, n), divisor: (l, d) => ext.vertexAttribDivisorANGLE(l, d) }
    }
    for (const g of Object.values(this.groups)) {
      g.geo = gl.createBuffer()
      gl.bindBuffer(gl.ARRAY_BUFFER, g.geo)
      gl.bufferData(gl.ARRAY_BUFFER, g.shape, gl.STATIC_DRAW)
      g.inst = gl.createBuffer()
      g.dirty = true
    }
  }

  onRemove(_map: MlMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    for (const g of Object.values(this.groups)) {
      if (g.geo) gl.deleteBuffer(g.geo)
      if (g.inst) gl.deleteBuffer(g.inst)
      g.geo = g.inst = null
    }
    if (this.program) gl.deleteProgram(this.program)
    this.program = null
    this.map = null
    this.gl = null
  }

  add(trees: { leaf: (typeof LEAVES)[number]; t: Inst }[]) {
    for (const { leaf, t } of trees) {
      this.groups[leaf].list.push(t)
      this.groups[leaf].dirty = true
    }
    if (!this.origin && trees.length) {
      this.origin = [mercX(trees[0].t.lon), mercY(trees[0].t.lat)]
      this.originLat = trees[0].t.lat
    }
    this.map?.triggerRepaint()
  }

  // Stand every tree on the ground again (the terrain came or went).
  relift() {
    for (const g of Object.values(this.groups)) g.dirty = true
    this.map?.triggerRepaint()
  }

  private build(g: Group) {
    const gl = this.gl
    if (!gl || !g.inst || !this.origin) return
    const [ox, oy] = this.origin
    const data = new Float32Array(g.list.length * 6)
    g.list.forEach((t, i) => {
      data[i * 6] = mercX(t.lon) - ox
      data[i * 6 + 1] = mercY(t.lat) - oy
      data[i * 6 + 2] = this.ground ? this.ground(t.lon, t.lat) : 0
      data[i * 6 + 3] = t.h
      data[i * 6 + 4] = t.r
      data[i * 6 + 5] = t.tone
    })
    gl.bindBuffer(gl.ARRAY_BUFFER, g.inst)
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW)
    g.data = data
    g.n = g.list.length
    g.dirty = false
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: CustomRenderMethodInput) {
    const map = this.map
    if (!map || !this.program || !this.inst || !this.origin || map.getZoom() < 14.5) return
    const light = lightOf(map)
    gl.useProgram(this.program)
    gl.uniformMatrix4fv(this.uni.u_matrix, false, originMatrix(args.defaultProjectionData.mainMatrix, this.origin[0], this.origin[1]))
    gl.uniform1f(this.uni.u_scale, 1 / (EARTH * Math.cos((this.originLat * Math.PI) / 180)))
    gl.uniform3fv(this.uni.u_lightpos, light.pos)
    gl.uniform1f(this.uni.u_lightintensity, light.intensity)
    gl.uniform3fv(this.uni.u_lightcolor, light.color)
    gl.uniform1f(this.uni.u_night, this.night)
    gl.uniform3fv(this.uni.u_trunk, TRUNK)
    const { a_pos, a_normal, a_part, a_at, a_size } = this.loc
    for (const [leaf, g] of Object.entries(this.groups) as [(typeof LEAVES)[number], Group][]) {
      if (g.dirty) this.build(g)
      if (!g.n || !g.geo || !g.inst) continue
      gl.uniform3fv(this.uni.u_crown, CROWNS[leaf])
      gl.bindBuffer(gl.ARRAY_BUFFER, g.geo)
      gl.enableVertexAttribArray(a_pos); gl.vertexAttribPointer(a_pos, 3, gl.FLOAT, false, 28, 0)
      gl.enableVertexAttribArray(a_normal); gl.vertexAttribPointer(a_normal, 3, gl.FLOAT, false, 28, 12)
      gl.enableVertexAttribArray(a_part); gl.vertexAttribPointer(a_part, 1, gl.FLOAT, false, 28, 24)
      gl.bindBuffer(gl.ARRAY_BUFFER, g.inst)
      gl.enableVertexAttribArray(a_at); gl.vertexAttribPointer(a_at, 4, gl.FLOAT, false, 24, 0)
      gl.enableVertexAttribArray(a_size); gl.vertexAttribPointer(a_size, 2, gl.FLOAT, false, 24, 16)
      this.inst.divisor(a_at, 1)
      this.inst.divisor(a_size, 1)
      this.inst.draw(gl.TRIANGLES, 0, g.shape.length / 7, g.n)
      this.inst.divisor(a_at, 0)
      this.inst.divisor(a_size, 0)
    }
    for (const a of [a_pos, a_normal, a_part, a_at, a_size]) gl.disableVertexAttribArray(a)
  }
}

// -- loading ----------------------------------------------------------------------------------------

const layer = new TreeLayer()
const loaded = new Set<string>()
const waiting = new Set<string>()
let map: MlMap | null = null
let retry = 0

type TileAnswer = { status: "ready" | "loading" | "unavailable"; trees: [number, number, number, number, number, number][] }

// The trees of one z16 tile of the 3D city (from the engine, as each of its tiles loads).
export function loadTrees(x: number, y: number) {
  const key = `${x}/${y}`
  if (loaded.has(key) || waiting.has(key)) return
  waiting.add(key)
  fetch(`/api/trees/16/${x}/${y}`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((a: TileAnswer) => {
      waiting.delete(key)
      if (a.status !== "ready") {
        useTrees.setState({ status: a.status })
        if (a.status === "loading") retryLater(x, y)
        return
      }
      loaded.add(key)
      let mapped = 0, estimated = 0
      const batch = a.trees.map(([lon, lat, h, r, kind, leaf], i) => {
        if (kind <= 1) mapped++
        else estimated++
        return { leaf: LEAVES[leaf] ?? "broadleaved", t: { lon, lat, h, r, tone: ((x * 31 + y * 17 + i * 7) % 13) / 13 } }
      })
      layer.add(batch)
      useTrees.setState((s) => ({ status: "", mapped: s.mapped + mapped, estimated: s.estimated + estimated }))
    })
    .catch(() => { waiting.delete(key) })
}

function retryLater(x: number, y: number) {
  clearTimeout(retry)
  retry = window.setTimeout(() => loadTrees(x, y), 30e3)
}

export function attachTrees(m: MlMap, before?: string) {
  map = m
  layer.ground = (lon, lat) => {
    if (!map?.getTerrain()) return 0
    const h = map.queryTerrainElevation([lon, lat])
    return h === null || h === undefined || !Number.isFinite(h) ? 0 : h
  }
  if (useTrees.getState().on && !m.getLayer(TREES)) m.addLayer(layer, before)
  watchTerrain(m)
}

export function detachTrees(m: MlMap) {
  if (m.getLayer(TREES)) m.removeLayer(TREES)
}

export function setTreesNight(dark: boolean) {
  layer.night = dark ? 1 : 0
  map?.triggerRepaint()
}

export function setTreesOn(on: boolean) {
  useTrees.setState({ on })
  try { localStorage.setItem(KEY, on ? "on" : "off") } catch { /* private mode */ }
  if (!map) return
  if (!on) { detachTrees(map); return }
  if (map.getLayer("city3d::roofs") && !map.getLayer(TREES)) {
    const order = map.getLayersOrder()
    map.addLayer(layer, order[order.indexOf("city3d::roofs") + 1])
  }
}

let watching: MlMap | null = null
let liftTimer = 0
function watchTerrain(m: MlMap) {
  if (watching === m) return
  watching = m
  const soon = () => { clearTimeout(liftTimer); liftTimer = window.setTimeout(() => layer.relift(), 250) }
  m.on("terrain", soon)
  m.on("sourcedata", (e) => { if (e.sourceId === "terrain::dem" && e.isSourceLoaded) soon() })
}

export { KINDS }

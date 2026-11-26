/* A MapLibre custom layer that draws triangles in the map's 3D scene: the roofs of the 3D city
   (lib/city3d/roofs.ts) and its trees (lib/city3d/trees.ts). It shares the depth buffer with
   MapLibre's own 3D buildings, so walls and roofs hide each other as they should, and it lights
   its faces the way MapLibre lights fill-extrusions (the same light, the same formula), so a roof
   and the walls under it read as one building at any hour.

   Geometry comes in batches (one per tile of the city as it loads) and stays on the GPU. Each
   vertex is placed relative to one origin in mercator units, computed in double precision first,
   so nothing jitters at street level. Each thing drawn can be hidden (a building the eye stands
   in, a block an answer redraws) and lifted onto the terrain (a view raises the ground), without
   building its geometry again.

   Trees are instanced: one small tree shape, drawn once per tree with its place, height, crown
   and colour. */
import type { CustomLayerInterface, CustomRenderMethodInput, Map as MlMap } from "maplibre-gl"

const EARTH = 40075016.68557849

export const mercX = (lon: number) => (180 + lon) / 360
export const mercY = (lat: number) => (180 - (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360))) / 360

// One thing drawn: its vertices (x, y in metres east and north of its anchor, z in metres above
// the ground), the normals in MapLibre's tile frame (y south), and a colour per vertex.
export type Piece = {
  id: number
  lon: number; lat: number // the anchor: where its local metres start, and where it meets the ground
  pos: ArrayLike<number>; normal: ArrayLike<number>; color: ArrayLike<number>
}

type Batch = {
  ids: number[]; starts: number[]; counts: number[]; anchors: [number, number][]
  n: number // vertices
  geo: WebGLBuffer; col: WebGLBuffer; dyn: WebGLBuffer
  dynData: Float32Array // per vertex: ground height (m), hidden (0 or 1)
}

export type Light = { pos: [number, number, number]; color: [number, number, number]; intensity: number }

const VS = `
uniform mat4 u_matrix;
uniform float u_zscale;
uniform vec3 u_lightpos;
uniform float u_lightintensity;
uniform vec3 u_lightcolor;
uniform float u_opacity;
IN vec3 a_pos;
IN vec3 a_normal;
IN vec3 a_color;
IN vec2 a_dyn;
OUT vec4 v_color;
void main() {
  if (a_dyn.y > 0.5) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); v_color = vec4(0.0); return; }
  gl_Position = u_matrix * vec4(a_pos.xy, (a_pos.z + a_dyn.x) * u_zscale, 1.0);
  // MapLibre's fill-extrusion light, so roofs match the walls under them.
  vec3 color = a_color;
  float lum = dot(color, vec3(0.2126, 0.7152, 0.0722));
  color += vec3(0.03);
  float d = clamp(dot(a_normal, u_lightpos), 0.0, 1.0);
  d = mix(1.0 - u_lightintensity, max(1.0 - lum + u_lightintensity, 1.0), d);
  vec3 c = clamp(color * d * u_lightcolor, mix(vec3(0.0), vec3(0.3), vec3(1.0) - u_lightcolor), vec3(1.0));
  v_color = vec4(c, 1.0) * u_opacity;
}`

const FS = `
precision mediump float;
IN vec4 v_color;
FRAG_DECL
void main() { FRAG_OUT = v_color; }`

function source(gl: WebGLRenderingContext | WebGL2RenderingContext, body: string, vertex: boolean) {
  const two = typeof WebGL2RenderingContext !== "undefined" && gl instanceof WebGL2RenderingContext
  const head = two ? "#version 300 es\n" : ""
  return head + body
    .replace(/\bIN\b/g, two ? "in" : vertex ? "attribute" : "varying")
    .replace(/\bOUT\b/g, two ? "out" : "varying")
    .replace("FRAG_DECL", two ? "out vec4 fragColor;" : "")
    .replace("FRAG_OUT", two ? "fragColor" : "gl_FragColor")
}

export function compile(gl: WebGLRenderingContext | WebGL2RenderingContext, vs: string, fs: string) {
  const make = (type: number, src: string) => {
    const s = gl.createShader(type)!
    gl.shaderSource(s, src)
    gl.compileShader(s)
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader")
    return s
  }
  const p = gl.createProgram()!
  gl.attachShader(p, make(gl.VERTEX_SHADER, source(gl, vs, true)))
  gl.attachShader(p, make(gl.FRAGMENT_SHADER, source(gl, fs, false)))
  gl.linkProgram(p)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) ?? "program")
  return p
}

// MapLibre's light as fill-extrusions get it (its position turned into a direction, y south).
export function lightOf(map: MlMap): Light {
  const l = map.getLight() as { position?: number[]; color?: string; intensity?: number } | undefined
  const [r, az, polar] = (l?.position as number[] | undefined) ?? [1.15, 210, 30]
  const a = ((az + 90) * Math.PI) / 180, p = (polar * Math.PI) / 180
  return {
    pos: [r * Math.cos(a) * Math.sin(p), r * Math.sin(a) * Math.sin(p), r * Math.cos(p)],
    color: rgb(l?.color ?? "#ffffff"),
    intensity: l?.intensity ?? 0.5,
  }
}

export function rgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return [1, 1, 1]
  const v = parseInt(m[1], 16)
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255]
}

// The map's matrix for mercator units, moved to an origin (in double precision, so the vertices
// near the origin keep their precision in float32).
export function originMatrix(m: ArrayLike<number>, ox: number, oy: number): Float32Array {
  const out = new Float32Array(16)
  for (let i = 0; i < 12; i++) out[i] = m[i]
  for (let r = 0; r < 4; r++) out[12 + r] = m[r] * ox + m[4 + r] * oy + m[12 + r]
  return out
}

export class MeshLayer implements CustomLayerInterface {
  id: string
  type = "custom" as const
  renderingMode = "3d" as const
  minzoom: number
  private map: MlMap | null = null
  private gl: WebGLRenderingContext | WebGL2RenderingContext | null = null
  private program: WebGLProgram | null = null
  private loc: Record<string, number> = {}
  private uni: Record<string, WebGLUniformLocation | null> = {}
  private batches: Batch[] = []
  private pending: Piece[][] = []
  private origin: [number, number] | null = null // mercator x, y
  private originLat = 35.17
  private hidden = new Set<number>()
  private where = new Map<number, [Batch, number]>() // id: its batch and index there
  opacity = 1
  ground: ((lon: number, lat: number) => number) | null = null // terrain under a point, or none

  constructor(id: string, minzoom = 0) {
    this.id = id
    this.minzoom = minzoom
  }

  onAdd(map: MlMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    this.map = map
    this.gl = gl
    this.program = compile(gl, VS, FS)
    for (const a of ["a_pos", "a_normal", "a_color", "a_dyn"]) this.loc[a] = gl.getAttribLocation(this.program, a)
    for (const u of ["u_matrix", "u_zscale", "u_lightpos", "u_lightintensity", "u_lightcolor", "u_opacity"]) {
      this.uni[u] = gl.getUniformLocation(this.program, u)
    }
    // What came before the layer was on the map.
    for (const b of this.batches) this.upload(b)
    const waiting = this.pending
    this.pending = []
    for (const p of waiting) this.add(p)
  }

  onRemove(_map: MlMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    for (const b of this.batches) for (const buf of [b.geo, b.col, b.dyn]) gl.deleteBuffer(buf)
    if (this.program) gl.deleteProgram(this.program)
    this.batches = []
    this.where.clear()
    this.program = null
    this.gl = null
    this.map = null
  }

  get count() {
    return this.where.size
  }

  // A batch of pieces (a tile's worth). Kept until the layer is on the map.
  add(pieces: Piece[]) {
    const gl = this.gl
    const fresh = pieces.filter((p) => !this.where.has(p.id) && p.pos.length)
    if (!fresh.length) return
    if (!gl) { this.pending.push(fresh); return }
    if (!this.origin) {
      this.origin = [mercX(fresh[0].lon), mercY(fresh[0].lat)]
      this.originLat = fresh[0].lat
    }
    const [ox, oy] = this.origin
    let n = 0
    for (const p of fresh) n += p.pos.length / 3
    const geo = new Float32Array(n * 6), col = new Float32Array(n * 3), dyn = new Float32Array(n * 2)
    const batch: Batch = {
      ids: [], starts: [], counts: [], anchors: [], n,
      geo: gl.createBuffer()!, col: gl.createBuffer()!, dyn: gl.createBuffer()!, dynData: dyn,
    }
    let v = 0
    for (const p of fresh) {
      const kx = 111320 * Math.cos((p.lat * Math.PI) / 180), ky = 110574
      const count = p.pos.length / 3
      batch.ids.push(p.id); batch.starts.push(v); batch.counts.push(count); batch.anchors.push([p.lon, p.lat])
      this.where.set(p.id, [batch, batch.ids.length - 1])
      const hide = this.hidden.has(p.id) ? 1 : 0
      for (let i = 0; i < count; i++, v++) {
        const lon = p.lon + p.pos[i * 3] / kx, lat = p.lat + p.pos[i * 3 + 1] / ky
        geo[v * 6] = mercX(lon) - ox
        geo[v * 6 + 1] = mercY(lat) - oy
        geo[v * 6 + 2] = p.pos[i * 3 + 2]
        geo[v * 6 + 3] = p.normal[i * 3]; geo[v * 6 + 4] = p.normal[i * 3 + 1]; geo[v * 6 + 5] = p.normal[i * 3 + 2]
        col[v * 3] = p.color[i * 3]; col[v * 3 + 1] = p.color[i * 3 + 1]; col[v * 3 + 2] = p.color[i * 3 + 2]
        dyn[v * 2 + 1] = hide
      }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, batch.geo)
    gl.bufferData(gl.ARRAY_BUFFER, geo, gl.STATIC_DRAW)
    gl.bindBuffer(gl.ARRAY_BUFFER, batch.col)
    gl.bufferData(gl.ARRAY_BUFFER, col, gl.DYNAMIC_DRAW)
    this.batches.push(batch)
    this.liftBatch(batch)
    this.map?.triggerRepaint()
  }

  private upload(b: Batch) {
    const gl = this.gl
    if (!gl) return
    gl.bindBuffer(gl.ARRAY_BUFFER, b.dyn)
    gl.bufferData(gl.ARRAY_BUFFER, b.dynData, gl.DYNAMIC_DRAW)
  }

  // New colours for everything drawn (the base map or the look changed): `colorOf` is asked per
  // piece and fills that piece's colours in place.
  recolor(colorOf: (id: number, out: Float32Array, count: number) => void) {
    const gl = this.gl
    if (!gl) return
    for (const b of this.batches) {
      const col = new Float32Array(b.n * 3)
      for (let k = 0; k < b.ids.length; k++) colorOf(b.ids[k], col.subarray(b.starts[k] * 3, (b.starts[k] + b.counts[k]) * 3), b.counts[k])
      gl.bindBuffer(gl.ARRAY_BUFFER, b.col)
      gl.bufferData(gl.ARRAY_BUFFER, col, gl.DYNAMIC_DRAW)
    }
    this.map?.triggerRepaint()
  }

  setHidden(ids: Iterable<number>) {
    const next = new Set(ids)
    const touched = new Set<Batch>()
    for (const id of new Set([...this.hidden, ...next])) {
      if (this.hidden.has(id) === next.has(id)) continue
      const at = this.where.get(id)
      if (!at) continue
      const [b, k] = at
      for (let v = b.starts[k]; v < b.starts[k] + b.counts[k]; v++) b.dynData[v * 2 + 1] = next.has(id) ? 1 : 0
      touched.add(b)
    }
    this.hidden = next
    for (const b of touched) this.upload(b)
    if (touched.size) this.map?.triggerRepaint()
  }

  // Stand everything on the ground as the map draws it (terrain in a view, flat otherwise).
  lift() {
    for (const b of this.batches) this.liftBatch(b)
    this.map?.triggerRepaint()
  }

  private liftBatch(b: Batch) {
    const ground = this.ground
    for (let k = 0; k < b.ids.length; k++) {
      const g = ground ? ground(b.anchors[k][0], b.anchors[k][1]) : 0
      for (let v = b.starts[k]; v < b.starts[k] + b.counts[k]; v++) b.dynData[v * 2] = g
    }
    this.upload(b)
  }

  clear() {
    const gl = this.gl
    if (gl) for (const b of this.batches) for (const buf of [b.geo, b.col, b.dyn]) gl.deleteBuffer(buf)
    this.batches = []
    this.pending = []
    this.where.clear()
    this.map?.triggerRepaint()
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: CustomRenderMethodInput) {
    const map = this.map
    if (!map || !this.program || !this.origin || !this.batches.length || map.getZoom() < this.minzoom) return
    const light = lightOf(map)
    gl.useProgram(this.program)
    gl.uniformMatrix4fv(this.uni.u_matrix, false, originMatrix(args.defaultProjectionData.mainMatrix, this.origin[0], this.origin[1]))
    gl.uniform1f(this.uni.u_zscale, 1 / (EARTH * Math.cos((this.originLat * Math.PI) / 180)))
    gl.uniform3fv(this.uni.u_lightpos, light.pos)
    gl.uniform1f(this.uni.u_lightintensity, light.intensity)
    gl.uniform3fv(this.uni.u_lightcolor, light.color)
    gl.uniform1f(this.uni.u_opacity, this.opacity)
    const { a_pos, a_normal, a_color, a_dyn } = this.loc
    for (const b of this.batches) {
      gl.bindBuffer(gl.ARRAY_BUFFER, b.geo)
      gl.enableVertexAttribArray(a_pos)
      gl.vertexAttribPointer(a_pos, 3, gl.FLOAT, false, 24, 0)
      gl.enableVertexAttribArray(a_normal)
      gl.vertexAttribPointer(a_normal, 3, gl.FLOAT, false, 24, 12)
      gl.bindBuffer(gl.ARRAY_BUFFER, b.col)
      gl.enableVertexAttribArray(a_color)
      gl.vertexAttribPointer(a_color, 3, gl.FLOAT, false, 12, 0)
      gl.bindBuffer(gl.ARRAY_BUFFER, b.dyn)
      gl.enableVertexAttribArray(a_dyn)
      gl.vertexAttribPointer(a_dyn, 2, gl.FLOAT, false, 8, 0)
      gl.drawArrays(gl.TRIANGLES, 0, b.n)
    }
    for (const a of [a_pos, a_normal, a_color, a_dyn]) gl.disableVertexAttribArray(a)
  }
}

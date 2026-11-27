/* The live buses as small 3D buses: one low-poly model (tools/bus_model/build_bus.py, built in
   Blender) drawn once per bus in a MapLibre custom layer, in the line's colour.

   Why not three.js: the whole job is one small flat-shaded mesh drawn a few hundred times, so a
   plain WebGL2 program does it in about the size of the model itself (a few kB, loaded only when
   buses are on), in one instanced draw call, with no second renderer fighting MapLibre over the
   GL state. The model file is glTF, so a fuller renderer can take it over if the twin ever needs
   one.

   Size. A true-size bus (12 m) is a speck at city zoom, so the model is scaled up to a steady
   size on screen and comes down to true size at street zoom (from about z17.8). Far out
   (below SHOW_ZOOM) the map keeps the plain dots.

   Depth. The model shares the depth buffer with the 3D city, so a bus at street zoom hides
   behind the buildings in front of it. While it is scaled up it is bigger than its street and
   would vanish into the blocks beside it, so first the parts that are behind something are
   drawn as a faint silhouette (only the convex shell, so nothing overlaps), fading out as the
   bus comes down to true size.

   The open bus is drawn in sodium. Picking, labels and the far-zoom dots stay MapLibre layers
   (lib/mobility/map.ts); this file only draws. Any failure (no WebGL2, a model that doesn't
   load, a shader that doesn't compile) is reported once through `onFail`, and the map goes back
   to its dots and arrows. */
import { MercatorCoordinate, type CustomLayerInterface, type CustomRenderMethodInput, type Map as MlMap } from "maplibre-gl"

import busUrl from "@/assets/models/bus.glb?url"

export const BUS_MODEL_LAYER = "mob::bus-model"
export const SHOW_ZOOM = 13.4 // the model from here; the dots fade out just above it
const ORIGIN: [number, number] = [33.3615, 35.1725] // the old town: positions are metres from here
const SODIUM: [number, number, number] = [1, 0xae / 255, 0x45 / 255]
const BUS_LENGTH = 12

// The length a bus is drawn on screen, in CSS pixels, by zoom (until true size takes over).
const SCREEN_LENGTH: [number, number][] = [[13.4, 26], [15, 34], [16.5, 42], [18, 50]]

export type BusInstance = { id: string; lon: number; lat: number; heading: number; color: string; open: boolean }

export type BusModel = {
  layer: CustomLayerInterface
  setBuses(buses: BusInstance[]): void
}

// -- sizes by zoom (shared with the labels and hit targets in map.ts) ----------------------------

function interp(stops: [number, number][], z: number) {
  if (z <= stops[0][0]) return stops[0][1]
  for (let i = 1; i < stops.length; i++) {
    const [z1, v1] = stops[i]
    if (z <= z1) {
      const [z0, v0] = stops[i - 1]
      return v0 + ((z - z0) / (z1 - z0)) * (v1 - v0)
    }
  }
  return stops[stops.length - 1][1]
}

function metresPerPixel(zoom: number, lat = ORIGIN[1]) {
  return (40_075_016.686 * Math.cos((lat * Math.PI) / 180)) / (512 * 2 ** zoom)
}

// How many times true size a bus is drawn at this zoom (1 at street zoom).
export function busScale(zoom: number) {
  return Math.max(1, (interp(SCREEN_LENGTH, zoom) * metresPerPixel(zoom)) / BUS_LENGTH)
}

// A bus's length on screen at this zoom, in CSS pixels, scaled or true size.
export function busScreenLength(zoom: number) {
  return (BUS_LENGTH * busScale(zoom)) / metresPerPixel(zoom)
}

// -- the model file -----------------------------------------------------------------------------

/* What the renderer needs from the GLB: one interleaved vertex array (position and normal in
   metres, east north up with the bus heading north; colour; kind) and one index array with the
   shell's triangles first. Kinds: 0 a constant colour, 1 tinted by the line, 2 unlit (lamps). */
type Mesh = { vertices: Float32Array; indices: Uint16Array | Uint32Array; shellCount: number }

const SHELL = ["body", "accent", "glass", "trim", "lights", "tail"]
const ORDER = [...SHELL, "tyre", "hub", "roof"]
const TINTED = new Set(["body", "accent"])
const UNLIT = new Set(["lights", "tail"])
const STRIDE = 10 // floats a vertex: position 3, normal 3, colour 3, kind 1

const toSrgb = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055)

/* eslint-disable @typescript-eslint/no-explicit-any */
export function parseGlb(buf: ArrayBuffer): Mesh {
  const dv = new DataView(buf)
  if (dv.getUint32(0, true) !== 0x46546c67) throw new Error("not a GLB file")
  let json: any = null
  let bin: ArrayBuffer | null = null
  for (let off = 12; off < buf.byteLength;) {
    const len = dv.getUint32(off, true)
    const type = dv.getUint32(off + 4, true)
    const body = buf.slice(off + 8, off + 8 + len)
    if (type === 0x4e4f534a) json = JSON.parse(new TextDecoder().decode(body))
    else if (type === 0x004e4942) bin = body
    off += 8 + len
  }
  if (!json || !bin) throw new Error("GLB without its JSON or binary chunk")
  const node = (json.nodes ?? []).find((n: any) => n.mesh !== undefined)
  if (!node) throw new Error("GLB without a mesh")
  if (node.matrix || node.rotation || node.scale) throw new Error("the bus mesh must not be transformed in the file")
  const [tx, ty, tz] = node.translation ?? [0, 0, 0]

  const read = (index: number) => {
    const acc = json.accessors[index]
    const view = json.bufferViews[acc.bufferView]
    const size = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[acc.type as string] ?? 1
    const Type = { 5126: Float32Array, 5125: Uint32Array, 5123: Uint16Array, 5121: Uint8Array }[acc.componentType as number]
    if (!Type) throw new Error(`unsupported accessor type ${acc.componentType}`)
    const start = (view.byteOffset ?? 0) + (acc.byteOffset ?? 0)
    const stride = view.byteStride ?? 0
    if (stride && stride !== size * Type.BYTES_PER_ELEMENT) throw new Error("interleaved buffers are not supported")
    return { data: new Type(bin!, start, acc.count * size) as ArrayLike<number>, count: acc.count as number }
  }

  const prims = (json.meshes[node.mesh].primitives as any[]).map((p) => {
    const mat = json.materials?.[p.material] ?? {}
    const name = String(mat.name ?? "").split(".")[0]
    return { p, name, mat }
  })
  prims.sort((a, b) => rank(a.name) - rank(b.name))

  const verts: number[] = []
  const idx: number[] = []
  let shellCount = 0
  for (const { p, name, mat } of prims) {
    if (p.mode !== undefined && p.mode !== 4) throw new Error("only triangles are supported")
    const pos = read(p.attributes.POSITION)
    const nor = read(p.attributes.NORMAL)
    const f = mat.pbrMetallicRoughness?.baseColorFactor ?? [1, 1, 1, 1]
    const colour = [toSrgb(f[0]), toSrgb(f[1]), toSrgb(f[2])]
    const kind = TINTED.has(name) ? 1 : UNLIT.has(name) ? 2 : 0
    const base = verts.length / STRIDE
    for (let i = 0; i < pos.count; i++) {
      const x = pos.data[i * 3] + tx, y = pos.data[i * 3 + 1] + ty, z = pos.data[i * 3 + 2] + tz
      const nx = nor.data[i * 3], ny = nor.data[i * 3 + 1], nz = nor.data[i * 3 + 2]
      // glTF: +Y up, +Z forward, -X right. Here: east (the bus's right) north (forward) up.
      verts.push(-x, z, y, -nx, nz, ny, colour[0], colour[1], colour[2], kind)
    }
    const ind = p.indices !== undefined ? read(p.indices) : null
    const n = ind ? ind.count : pos.count
    for (let i = 0; i < n; i++) idx.push(base + (ind ? ind.data[i] : i))
    if (SHELL.includes(name)) shellCount += n
  }
  const vertices = new Float32Array(verts)
  const indices = vertices.length / STRIDE > 65535 ? new Uint32Array(idx) : new Uint16Array(idx)
  return { vertices, indices, shellCount }
}

function rank(name: string) {
  const i = ORDER.indexOf(name)
  return i < 0 ? ORDER.length : i
}

// -- shaders ------------------------------------------------------------------------------------

const VERT = `#version 300 es
precision highp float;
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_normal;
layout(location = 2) in vec3 a_color;
layout(location = 3) in float a_kind;
layout(location = 4) in vec4 i_place;   // east, north (metres from the origin), height, heading (radians)
layout(location = 5) in vec4 i_color;   // line colour, open (1) or not
uniform mat4 u_matrix;                  // metres from the origin, east north up -> clip
uniform float u_scale;
uniform vec3 u_light;                   // toward the light, east north up
out vec3 v_color;
void main() {
  float c = cos(i_place.w), s = sin(i_place.w); // heading, clockwise from north
  vec3 p = a_pos * u_scale;
  vec3 world = vec3(i_place.x + p.x * c + p.y * s, i_place.y - p.x * s + p.y * c, i_place.z + p.z);
  vec3 n = vec3(a_normal.x * c + a_normal.y * s, -a_normal.x * s + a_normal.y * c, a_normal.z);
  vec3 tint = i_color.w > 0.5 ? vec3(${SODIUM.join(", ")}) : i_color.rgb;
  vec3 base = a_kind > 0.5 && a_kind < 1.5 ? a_color * tint : a_color;
  // Lit like the city's buildings: a soft light from the screen's upper left, roofs brightest.
  float lit = 0.6 + 0.32 * max(dot(n, u_light), 0.0) + 0.08 * max(n.z, 0.0);
  v_color = a_kind > 1.5 ? base : base * lit;
  gl_Position = u_matrix * vec4(world, 1.0);
}`

const FRAG = `#version 300 es
precision mediump float;
in vec3 v_color;
uniform float u_alpha;
out vec4 o_color;
void main() {
  o_color = vec4(v_color * u_alpha, u_alpha); // premultiplied, as MapLibre blends
}`

function compile(gl: WebGL2RenderingContext, type: number, src: string) {
  const sh = gl.createShader(type)!
  gl.shaderSource(sh, src)
  gl.compileShader(sh)
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(`bus shader: ${gl.getShaderInfoLog(sh)}`)
  return sh
}

function hexRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "")
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h
  const n = parseInt(full.slice(0, 6), 16)
  return Number.isFinite(n) ? [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255] : [0.53, 0.6, 0.69]
}

// -- the layer ----------------------------------------------------------------------------------

type Gl = {
  program: WebGLProgram; vao: WebGLVertexArrayObject; instances: WebGLBuffer
  u: { matrix: WebGLUniformLocation; scale: WebGLUniformLocation; light: WebGLUniformLocation; alpha: WebGLUniformLocation }
  indexType: number; indexCount: number; shellCount: number; buffers: WebGLBuffer[]
}

const INSTANCE_FLOATS = 8

export async function loadBusModel(map: MlMap, onFail: (why: string) => void): Promise<BusModel> {
  // The map draws with WebGL2 or WebGL1; the model needs 2 (instancing and vertex arrays).
  if (!map.getCanvas().getContext("webgl2")) throw new Error("WebGL2 is not available")
  const res = await fetch(busUrl)
  if (!res.ok) throw new Error(`the bus model didn't load (HTTP ${res.status})`)
  const mesh = parseGlb(await res.arrayBuffer())

  const origin = MercatorCoordinate.fromLngLat(ORIGIN, 0)
  const unit = origin.meterInMercatorCoordinateUnits()
  let instances = new Float32Array(0)
  let count = 0
  let dirty = true
  let state: Gl | null = null
  let failed = false
  const matrix = new Float64Array(16)
  const m32 = new Float32Array(16)

  const fail = (why: string) => {
    if (failed) return
    failed = true
    console.warn(`3D buses: ${why}; showing the flat markers instead`)
    window.setTimeout(() => onFail(why), 0) // not inside MapLibre's render
  }

  function init(gl: WebGL2RenderingContext): Gl {
    const program = gl.createProgram()!
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT))
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG))
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(`bus program: ${gl.getProgramInfoLog(program)}`)
    const vao = gl.createVertexArray()!
    gl.bindVertexArray(vao)
    const vbo = gl.createBuffer()!
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo)
    gl.bufferData(gl.ARRAY_BUFFER, mesh.vertices, gl.STATIC_DRAW)
    const F = 4
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, STRIDE * F, 0)
    gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 3, gl.FLOAT, false, STRIDE * F, 3 * F)
    gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 3, gl.FLOAT, false, STRIDE * F, 6 * F)
    gl.enableVertexAttribArray(3); gl.vertexAttribPointer(3, 1, gl.FLOAT, false, STRIDE * F, 9 * F)
    const ibo = gl.createBuffer()!
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo)
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW)
    const inst = gl.createBuffer()!
    gl.bindBuffer(gl.ARRAY_BUFFER, inst)
    gl.enableVertexAttribArray(4); gl.vertexAttribPointer(4, 4, gl.FLOAT, false, INSTANCE_FLOATS * F, 0); gl.vertexAttribDivisor(4, 1)
    gl.enableVertexAttribArray(5); gl.vertexAttribPointer(5, 4, gl.FLOAT, false, INSTANCE_FLOATS * F, 4 * F); gl.vertexAttribDivisor(5, 1)
    gl.bindVertexArray(null)
    const loc = (name: string) => gl.getUniformLocation(program, name)!
    return {
      program, vao, instances: inst, buffers: [vbo, ibo, inst],
      u: { matrix: loc("u_matrix"), scale: loc("u_scale"), light: loc("u_light"), alpha: loc("u_alpha") },
      indexType: mesh.indices instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT,
      indexCount: mesh.indices.length, shellCount: mesh.shellCount,
    }
  }

  // metres from the origin (east north up) -> clip: the map's matrix after the origin's place
  // and the metre's size, multiplied in full precision so buses don't shiver at street zoom.
  function placeMatrix(main: ArrayLike<number>) {
    const t = [origin.x, origin.y, 0]
    const s = [unit, -unit, unit] // mercator y runs south
    for (let col = 0; col < 3; col++) for (let row = 0; row < 4; row++) matrix[col * 4 + row] = main[col * 4 + row] * s[col]
    for (let row = 0; row < 4; row++) {
      matrix[12 + row] = main[row] * t[0] + main[4 + row] * t[1] + main[8 + row] * t[2] + main[12 + row]
    }
    m32.set(matrix)
    return m32
  }

  const layer: CustomLayerInterface = {
    id: BUS_MODEL_LAYER,
    type: "custom",
    renderingMode: "3d",
    onRemove(_map, gl) {
      if (!state || !(gl instanceof WebGL2RenderingContext)) return
      gl.deleteProgram(state.program)
      gl.deleteVertexArray(state.vao)
      for (const b of state.buffers) gl.deleteBuffer(b)
      state = null
      dirty = true
    },
    render(gl, args: CustomRenderMethodInput) {
      if (failed || !count) return
      const zoom = map.getZoom()
      if (zoom < SHOW_ZOOM) return
      if (!(gl instanceof WebGL2RenderingContext)) { fail("WebGL2 is not available"); return }
      try {
        state ??= init(gl)
      } catch (e) {
        fail((e as Error).message)
        return
      }
      const s = state
      gl.useProgram(s.program)
      gl.bindVertexArray(s.vao)
      if (dirty) {
        gl.bindBuffer(gl.ARRAY_BUFFER, s.instances)
        gl.bufferData(gl.ARRAY_BUFFER, instances.subarray(0, count * INSTANCE_FLOATS), gl.DYNAMIC_DRAW)
        dirty = false
      }
      const scale = busScale(zoom)
      gl.uniformMatrix4fv(s.u.matrix, false, placeMatrix(args.defaultProjectionData.mainMatrix as ArrayLike<number>))
      gl.uniform1f(s.u.scale, scale)
      // The light keeps its place on screen as the map turns, as MapLibre's does for buildings.
      const b = (map.getBearing() * Math.PI) / 180
      const lx = -0.55, ly = 0.45 // up and to the left on screen
      gl.uniform3f(s.u.light, lx * Math.cos(b) + ly * Math.sin(b), -lx * Math.sin(b) + ly * Math.cos(b), 0.7)
      gl.enable(gl.CULL_FACE)
      gl.cullFace(gl.BACK)
      // glTF's counter-clockwise front faces stay so on screen: the flip to mercator's southward
      // y is undone by the map's own projection.
      gl.frontFace(gl.CCW)
      gl.enable(gl.DEPTH_TEST)
      // 1. What is behind the city: a faint silhouette while the bus is bigger than life.
      const ghost = Math.min(0.5, Math.max(0, (scale - 1.2) / 2.6))
      if (ghost > 0.01) {
        gl.depthFunc(gl.GREATER)
        gl.depthMask(false)
        gl.enable(gl.BLEND)
        gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)
        gl.uniform1f(s.u.alpha, ghost)
        gl.drawElementsInstanced(gl.TRIANGLES, s.shellCount, s.indexType, 0, count)
      }
      // 2. The buses themselves, solid, sharing the depth buffer with the buildings.
      gl.depthFunc(gl.LEQUAL)
      gl.depthMask(true)
      gl.disable(gl.BLEND)
      gl.uniform1f(s.u.alpha, 1)
      gl.drawElementsInstanced(gl.TRIANGLES, s.indexCount, s.indexType, 0, count)

      gl.bindVertexArray(null)
      gl.frontFace(gl.CCW)
      gl.disable(gl.CULL_FACE)
      const err = gl.getError()
      if (err === gl.CONTEXT_LOST_WEBGL) fail("the graphics context was lost")
    },
  }

  return {
    layer,
    setBuses(buses) {
      if (instances.length < buses.length * INSTANCE_FLOATS) instances = new Float32Array(Math.max(64, buses.length * 2) * INSTANCE_FLOATS)
      const elev = !!map.getTerrain()
      let i = 0
      for (const bus of buses) {
        const m = MercatorCoordinate.fromLngLat([bus.lon, bus.lat], 0)
        const [r, g, bl] = hexRgb(bus.color)
        const ground = elev ? map.queryTerrainElevation([bus.lon, bus.lat]) ?? 0 : 0
        instances.set([(m.x - origin.x) / unit, -(m.y - origin.y) / unit, ground, (bus.heading * Math.PI) / 180,
                       r, g, bl, bus.open ? 1 : 0], i * INSTANCE_FLOATS)
        i++
      }
      count = i
      dirty = true
      map.triggerRepaint()
    },
  }
}

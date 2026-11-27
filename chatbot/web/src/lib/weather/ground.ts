/* The weather on the ground: one MapLibre custom layer, drawn over the base map and the 3D
   buildings and under the labels and everything the twin draws, so the data stays crisp.

   For each pixel it finds where the eye's ray meets the ground (or that it goes up into the sky)
   from the map's own camera, and from that:
   - cloud shadows drifting with the wind, in world metres, so they pass over the streets and the
     buildings and stay put when the map moves;
   - a flat wash of the weather's colour (overcast grey, rain, Saharan dust), the grading that
     lowers contrast and saturation;
   - haze into the distance: seen from above, by how far past the map's centre the ground lies
     (so the place looked at stays clear and the horizon fades); at eye level, in a view from a
     place, by the real distance and the visibility. In mist it is patchy and drifts;
   - in a tilted view's sky, clouds on a deck above the camera, fading into the horizon.
   It draws nothing at all when the sky is clear, so a clear day looks exactly as before. Its
   render also hands the camera on to the rain (lib/weather/rain.ts). */
import maplibregl, { type CustomLayerInterface, type CustomRenderMethodInput, type Map as MlMap } from "maplibre-gl"

import { BASE_SOURCE } from "@/lib/map/basemap"
import { apply, invert, mat4, multiply, type Mat4 } from "@/lib/weather/mat4"
import type { Ground } from "@/lib/weather/look"

export const WEATHER_LAYER = "weather"
export const WET_LAYER = "weather-wet"

// What the rain needs of the camera, in metres around the map's centre (x east, y north, z up).
export type Camera = {
  view: Mat4 // local metres to the camera's view space
  proj: Mat4 // view space to clip space
  dist: number // camera to the map's centre, in view units
  cssH: number
}

// What the layer draws this frame, from lib/weather/map.ts.
export type GroundFrame = {
  ground: Ground
  windTo: number // degrees from north
  windKmh: number
  octaves: number // most noise octaves (fewer on a slow machine)
  moving: boolean // drift (false with reduced motion)
}

const SHADOW_SCALE = 1800 // metres per noise cell: cumulus shadows a kilometre or two across
const MIST_SCALE = 1100
const PERIOD = 289 // the noise repeats every 289 cells, so offsets wrap without a seam
const CLOUD_SPEED = 4 // the clouds aloft outrun the wind at the ground; a touch quicker reads as alive

const VERT = `
attribute vec2 a_pos;
varying vec2 v_ndc;
void main() { v_ndc = a_pos; gl_Position = vec4(a_pos, 0.0, 1.0); }
`

const FRAG = `
precision highp float;
varying vec2 v_ndc;
uniform mat3 u_rot;        // view space to local metres (directions)
uniform vec4 u_frustum;    // proj[0], proj[5], proj[8], proj[9]
uniform vec3 u_cam;        // the camera, local metres
uniform float u_ground;    // the ground's height under the centre
uniform vec3 u_view;       // camera to centre, its horizontal part, and how low the camera is (0 high, 1 at eye level)
uniform float u_octaves;
uniform vec3 u_shadow;     // strength, cover, metres per cell
uniform vec2 u_shadowOff;
uniform vec3 u_shadowTint;
uniform float u_dim;
uniform vec4 u_veil;       // colour, alpha
uniform vec4 u_haze;       // colour, most
uniform vec4 u_hazeK;      // per view, per metre at eye level, mist, metres per mist cell
uniform vec2 u_mistOff;
uniform vec4 u_cloudLit;   // colour, opacity
uniform vec4 u_cloudBase;  // colour, cover
uniform vec2 u_skyOff;

float hash(vec2 p) {
  p = mod(p, ${PERIOD}.0);
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p, float octaves) {
  float v = 0.0;
  float a = 0.5;
  float n = 0.0;
  for (int i = 0; i < 5; i++) {
    if (float(i) >= octaves) break;
    v += a * noise(p);
    n += a;
    p = p * 2.0 + vec2(17.0, 31.0);
    a *= 0.5;
  }
  return v / n;
}

void main() {
  // The eye's ray through this pixel, from the camera.
  vec3 o = u_cam;
  vec3 dir = normalize(u_rot * vec3((v_ndc.x + u_frustum.z) / u_frustum.x, (v_ndc.y + u_frustum.w) / u_frustum.y, -1.0));
  vec3 src = vec3(0.0);
  float keep = 1.0; // the pixel becomes what was drawn times keep, plus src

  if (dir.z < -0.0001) {
    float plane = min(u_ground, o.z - 1.0); // with terrain the eye can stand below the centre's ground
    vec2 g = o.xy + dir.xy * ((plane - o.z) / dir.z);
    float m = 1.0 - u_dim;
    float va = u_veil.a;
    vec3 vc = u_veil.rgb;
    if (u_shadow.x > 0.0) {
      float n = fbm(g / u_shadow.z + u_shadowOff, u_octaves);
      float th = mix(0.78, 0.2, u_shadow.y);
      float sm = smoothstep(th - 0.06, th + 0.06, n);
      sm = mix(sm, 1.0, smoothstep(0.88, 1.0, u_shadow.y));
      m *= 1.0 - u_shadow.x * sm;
      float ta = 0.15 * u_shadow.x * sm; // shade is a little cooler than sunlight
      vc = mix(vc, u_shadowTint, ta / max(va + ta, 0.0001));
      va = va + ta * (1.0 - va);
    }
    keep = m * (1.0 - va);
    src = vc * va;
    if (u_haze.a > 0.0) {
      float dist = length(g - o.xy);
      float k = u_hazeK.x;
      if (u_hazeK.z > 0.0) k *= 1.0 + u_hazeK.z * (fbm(g / u_hazeK.w + u_mistOff, min(u_octaves, 3.0)) - 0.5) * 2.4;
      float above = max(k, 0.0) * max(0.0, dist - u_view.y) / u_view.x;
      float level = u_hazeK.y * length(g - o.xy) * (1.0 + u_hazeK.z * 0.5);
      float h = u_haze.a * (1.0 - exp(-mix(above, level, u_view.z)));
      src = src * (1.0 - h) + u_haze.rgb * h;
      keep *= 1.0 - h;
    }
  } else {
    float up = max(dir.z, 0.0);
    if (u_cloudLit.a > 0.0) {
      float t = (u_view.x * 0.6) / max(up, 0.02);
      vec2 cp = (dir.xy * t) / (u_view.x * 0.9) + u_skyOff;
      float n = fbm(cp, u_octaves);
      float th = mix(0.75, 0.2, u_cloudBase.a);
      float d = smoothstep(th - 0.1, th + 0.12, n);
      d = mix(d, 1.0, smoothstep(0.85, 1.0, u_cloudBase.a) * 0.85);
      float ca = d * smoothstep(0.0, 0.1, up) * u_cloudLit.a;
      vec3 col = mix(u_cloudBase.rgb, u_cloudLit.rgb, smoothstep(0.35, 0.8, n));
      src = src * (1.0 - ca) + col * ca;
      keep *= 1.0 - ca;
    }
    if (u_haze.a > 0.0) {
      float h = u_haze.a * (1.0 - exp(-max(u_hazeK.x, u_hazeK.y * 20000.0) * 6.0)) * (1.0 - smoothstep(0.0, 0.35, up));
      src = src * (1.0 - h) + u_haze.rgb * h;
      keep *= 1.0 - h;
    }
  }
  gl_FragColor = vec4(src, 1.0 - keep);
}
`

const UNIFORMS = [
  "u_rot", "u_frustum", "u_cam", "u_ground", "u_view", "u_octaves", "u_shadow", "u_shadowOff", "u_shadowTint", "u_dim", "u_veil",
  "u_haze", "u_hazeK", "u_mistOff", "u_cloudLit", "u_cloudBase", "u_skyOff",
] as const

function compile(gl: WebGLRenderingContext, type: number, source: string) {
  const s = gl.createShader(type)!
  gl.shaderSource(s, source)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(`weather shader: ${gl.getShaderInfoLog(s)}`)
  return s
}

export function program(gl: WebGLRenderingContext, vert: string, frag: string) {
  const p = gl.createProgram()!
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vert))
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, frag))
  gl.linkProgram(p)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`weather program: ${gl.getProgramInfoLog(p)}`)
  return p
}

const wrap = (v: number) => ((v % PERIOD) + PERIOD) % PERIOD

export class GroundLayer implements CustomLayerInterface {
  id = WEATHER_LAYER
  type = "custom" as const
  renderingMode = "2d" as const
  private map: MlMap | null = null
  private prog: WebGLProgram | null = null
  private buf: WebGLBuffer | null = null
  private loc: Partial<Record<(typeof UNIFORMS)[number], WebGLUniformLocation | null>> = {}
  private aPos = -1
  private drift = { shadow: [0, 0], mist: [0, 0], sky: [0, 0], at: 0 }
  failed = false
  // Set by lib/weather/map.ts: what to draw, and who wants the camera.
  frame: () => GroundFrame | null
  onCamera: (c: Camera) => void

  constructor(frame: () => GroundFrame | null, onCamera: (c: Camera) => void) {
    this.frame = frame
    this.onCamera = onCamera
  }

  onAdd(map: MlMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    this.map = map
    try {
      this.prog = program(gl, VERT, FRAG)
    } catch (e) {
      console.error(e)
      this.failed = true
      return
    }
    for (const u of UNIFORMS) this.loc[u] = gl.getUniformLocation(this.prog, u)
    this.aPos = gl.getAttribLocation(this.prog, "a_pos")
    this.buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
  }

  onRemove(_map: MlMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    if (this.prog) gl.deleteProgram(this.prog)
    if (this.buf) gl.deleteBuffer(this.buf)
    this.prog = null
    this.buf = null
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: CustomRenderMethodInput) {
    const map = this.map
    if (!map) return
    // The map's centre as the origin, in metres: x east, y north, z up.
    const centre = map.getCenter()
    const mc = maplibregl.MercatorCoordinate.fromLngLat(centre, 0)
    const s = mc.meterInMercatorCoordinateUnits()
    const model = mat4([s, 0, 0, 0, 0, -s, 0, 0, 0, 0, s, 0, mc.x, mc.y, 0, 1])
    const mvp = multiply(args.defaultProjectionData.mainMatrix, model)
    const proj = mat4(args.projectionMatrix)
    const projInv = invert(proj)
    if (!projInv) return
    const view = multiply(projInv, mvp)
    const viewInv = invert(view)
    if (!viewInv) return
    const camH = apply(viewInv, 0, 0, 0, 1)
    const cam: [number, number, number] = [camH[0] / camH[3], camH[1] / camH[3], camH[2] / camH[3]]
    const ground = map.getTerrain() ? map.queryTerrainElevation(centre) ?? 0 : 0
    const dist = Math.hypot(cam[0], cam[1], cam[2] - ground)
    const cssH = map.getCanvas().clientHeight || 1
    const centreView = apply(view, 0, 0, ground, 1)
    this.onCamera({ view, proj, dist: Math.hypot(centreView[0], centreView[1], centreView[2]), cssH })

    const f = this.frame()
    if (!f || !f.ground.on || this.failed || !this.prog) return
    const g = f.ground

    // Drift: the clouds and the mist go with the wind, a step each frame.
    const now = performance.now()
    const dt = this.drift.at ? Math.min(1, (now - this.drift.at) / 1000) : 0
    this.drift.at = now
    if (f.moving) {
      const ms = (f.windKmh / 3.6) * CLOUD_SPEED
      const r = (f.windTo * Math.PI) / 180
      const vx = Math.sin(r) * ms * dt, vy = Math.cos(r) * ms * dt
      this.drift.shadow = [wrap(this.drift.shadow[0] + vx / SHADOW_SCALE), wrap(this.drift.shadow[1] + vy / SHADOW_SCALE)]
      this.drift.mist = [wrap(this.drift.mist[0] + (vx * 0.35) / MIST_SCALE), wrap(this.drift.mist[1] + (vy * 0.35) / MIST_SCALE)]
      this.drift.sky = [wrap(this.drift.sky[0] + vx / Math.max(dist * 0.9, 1)), wrap(this.drift.sky[1] + vy / Math.max(dist * 0.9, 1))]
    }
    // Where the centre is in the world (metres east and north at this latitude), so the clouds
    // and the mist stay put on the ground as the map moves.
    const wx = mc.x / s, wy = -mc.y / s
    const shadowOff = [wrap(wx / SHADOW_SCALE - this.drift.shadow[0]), wrap(wy / SHADOW_SCALE - this.drift.shadow[1])]
    const mistOff = [wrap(wx / MIST_SCALE - this.drift.mist[0]), wrap(wy / MIST_SCALE - this.drift.mist[1])]

    // As many noise octaves as the pixels can show (no shimmer zoomed out), at most f.octaves.
    const pxM = (dist * 2 * Math.tan(args.fov / 2)) / cssH / Math.min(2, window.devicePixelRatio || 1)
    const octaves = Math.max(1, Math.min(f.octaves, 1 + Math.floor(Math.log2(SHADOW_SCALE / (3 * pxM)))))
    // Low camera (a view from a place): the haze by real distance and visibility.
    const height = cam[2] - ground
    const low = 1 - Math.min(1, Math.max(0, (height - 60) / 340))
    // Shadows fade out zoomed far out, where a cloud would be a speck.
    const shadow = g.shadow * (1 - Math.min(1, Math.max(0, (dist - 25000) / 35000)))

    gl.useProgram(this.prog)
    const L = this.loc
    const v = viewInv
    gl.uniformMatrix3fv(L.u_rot!, false, new Float32Array([v[0], v[1], v[2], v[4], v[5], v[6], v[8], v[9], v[10]]))
    gl.uniform4f(L.u_frustum!, proj[0], proj[5], proj[8], proj[9])
    gl.uniform3f(L.u_cam!, cam[0], cam[1], cam[2])
    gl.uniform1f(L.u_ground!, ground)
    gl.uniform3f(L.u_view!, dist, Math.hypot(cam[0], cam[1]), low)
    gl.uniform1f(L.u_octaves!, octaves)
    gl.uniform3f(L.u_shadow!, shadow, g.cover, SHADOW_SCALE)
    gl.uniform2f(L.u_shadowOff!, shadowOff[0], shadowOff[1])
    gl.uniform3f(L.u_shadowTint!, ...g.shadowTint)
    gl.uniform1f(L.u_dim!, g.dim)
    gl.uniform4f(L.u_veil!, g.veil[0], g.veil[1], g.veil[2], g.veilA)
    gl.uniform4f(L.u_haze!, g.haze[0], g.haze[1], g.haze[2], g.hazeMax)
    gl.uniform4f(L.u_hazeK!, g.hazeK, g.extinction, g.mist, MIST_SCALE)
    gl.uniform2f(L.u_mistOff!, mistOff[0], mistOff[1])
    gl.uniform4f(L.u_cloudLit!, g.cloudLit[0], g.cloudLit[1], g.cloudLit[2], g.clouds)
    gl.uniform4f(L.u_cloudBase!, g.cloudBase[0], g.cloudBase[1], g.cloudBase[2], g.cloudCover)
    gl.uniform2f(L.u_skyOff!, this.drift.sky[0], this.drift.sky[1])
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf)
    gl.enableVertexAttribArray(this.aPos)
    gl.vertexAttribPointer(this.aPos, 2, gl.FLOAT, false, 0, 0)
    gl.disable(gl.DEPTH_TEST)
    gl.disable(gl.STENCIL_TEST)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)
    gl.drawArrays(gl.TRIANGLES, 0, 3)
  }
}

// Wet roads by day: darker, as wet asphalt is. At night the lamps' reflections do it instead.
export function wetRoads(): maplibregl.LineLayerSpecification {
  return {
    id: WET_LAYER, type: "line", source: BASE_SOURCE, "source-layer": "transportation", minzoom: 12,
    filter: ["match", ["get", "class"], ["motorway", "trunk", "primary", "secondary", "tertiary", "minor", "service"], true, false],
    layout: { "line-join": "round", "line-cap": "round", visibility: "none" },
    paint: {
      "line-color": "#3a4452",
      "line-opacity": 0,
      "line-width": ["interpolate", ["exponential", 1.6], ["zoom"], 12, ["match", ["get", "class"], ["motorway", "trunk", "primary"], 1.6, 0.6],
        18, ["match", ["get", "class"], ["motorway", "trunk", "primary"], 26, ["secondary", "tertiary"], 20, 12]],
    },
  }
}

// The weather goes over the ground and the buildings (the base map's own layers but its labels,
// the 3D city, the lamps' pools, the relief) and under everything else: the labels, the walls and
// all the twin draws. Run again whenever layers come or go below it.
const SCENERY = /^city3d::|(^|::)lamps-(pool|bulb)$|(^|::)street-glow$|^env-(hillshade|shadows)$/
const isBase = (id: string) => /^(night|day)\.\d+\//.test(id)

export function placeWeather(map: MlMap, layer: GroundLayer) {
  if (!map.getLayer(WEATHER_LAYER)) map.addLayer(layer)
  if (!map.getLayer(WET_LAYER) && map.getSource(BASE_SOURCE)) map.addLayer(wetRoads())
  const order = map.getLayersOrder()
  const first = order.find((id) => id !== WEATHER_LAYER && id !== WET_LAYER && !(isBase(id) ? map.getLayer(id)?.type !== "symbol" : SCENERY.test(id)))
  if (first) map.moveLayer(WEATHER_LAYER, first)
  else map.moveLayer(WEATHER_LAYER)
  // The 3D city stands with the ground, under the weather.
  const now = map.getLayersOrder()
  const at = now.indexOf(WEATHER_LAYER)
  for (const id of ["city3d::fill", "city3d::ghost"]) if (now.indexOf(id) > at) map.moveLayer(id, WEATHER_LAYER)
  // Wet roads lie on the roads, under the buildings: just before the first 3D layer under the weather.
  if (map.getLayer(WET_LAYER)) {
    const order2 = map.getLayersOrder()
    const w = order2.indexOf(WEATHER_LAYER)
    const buildings = order2.find((id, i) => i < w && map.getLayer(id)?.type === "fill-extrusion")
    map.moveLayer(WET_LAYER, buildings ?? WEATHER_LAYER)
  }
}

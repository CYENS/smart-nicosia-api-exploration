/* Rain over the map: fine streaks falling between the eye and the city, on a canvas of its own
   laid over the map's (under its markers and popups), so animating it never makes the map draw
   again. The drops live in the camera's own space: each falls for a moment along the world's
   down, leaned by the wind, then starts again elsewhere, so the rain reads right whatever the
   tilt. Seen from above it streams towards the ground below; tilted, it falls down the screen.
   Nearer drops are longer, wider and fainter; each streak is the drop's path over a short
   exposure, a motion blur. With reduced motion it is one still frame, drawn when the map moves. */
import { apply, invert } from "@/lib/weather/mat4"
import { program, type Camera } from "@/lib/weather/ground"
import type { Rain } from "@/lib/weather/look"

const MAX_DROPS = 7000

const VERT = `
attribute vec4 a_drop;    // id, a random number, another, the phase of its life
attribute vec2 a_corner;  // along the streak (0 tail, 1 head), across it (-1, 1)
uniform mat4 u_proj;
uniform vec3 u_vel;       // view units a second
uniform vec4 u_frustum;   // proj[0], proj[5], proj[8], proj[9]
uniform vec2 u_depth;     // nearest and farthest drop, view units
uniform vec2 u_px;        // the canvas in pixels
uniform float u_time, u_blur, u_width, u_alpha;
varying float v_across;
varying float v_along;
varying float v_alpha;

vec3 hash31(float p) {
  vec3 p3 = fract(vec3(p) * vec3(0.1031, 0.1030, 0.0973));
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.xxy + p3.yzz) * p3.zyx);
}

void main() {
  float life = mix(0.55, 0.95, a_drop.y);
  float c = u_time / life + a_drop.w;
  float k = floor(c);
  float tau = fract(c) * life;
  vec3 r = hash31(a_drop.x * 1.618 + k * 7.31 + a_drop.z * 13.0);
  float s = pow(r.z, 0.7);
  float z = mix(u_depth.x, u_depth.y, s);
  vec2 ndc = (r.xy * 2.0 - 1.0) * 1.2;
  vec3 start = vec3(z * (ndc.x + u_frustum.z) / u_frustum.x, z * (ndc.y + u_frustum.w) / u_frustum.y, -z);
  vec3 head = start + u_vel * tau;
  vec3 tail = head - u_vel * u_blur;
  vec4 ch = u_proj * vec4(head, 1.0);
  vec4 ct = u_proj * vec4(tail, 1.0);
  v_across = a_corner.y;
  v_along = a_corner.x;
  v_alpha = u_alpha * sin(3.14159 * tau / life) * mix(1.0, 0.55, s);
  if (ch.w <= 0.0 || ct.w <= 0.0) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vec2 ph = ch.xy / ch.w * 0.5 * u_px;
  vec2 pt = ct.xy / ct.w * 0.5 * u_px;
  vec2 d = ph - pt;
  float len = length(d);
  vec2 along = len > 0.001 ? d / len : vec2(0.0, -1.0);
  vec2 across = vec2(-along.y, along.x);
  float w = u_width * mix(1.7, 0.6, s);
  vec2 p = mix(pt, ph, a_corner.x) + across * a_corner.y * w * 0.5 + along * (a_corner.x * 2.0 - 1.0) * w * 0.5;
  gl_Position = vec4(p / (0.5 * u_px), 0.0, 1.0);
}
`

const FRAG = `
precision mediump float;
uniform vec3 u_color;
varying float v_across;
varying float v_along;
varying float v_alpha;
void main() {
  float a = v_alpha * (1.0 - v_across * v_across) * mix(0.2, 1.0, v_along);
  gl_FragColor = vec4(u_color * a, a);
}
`

const UNIFORMS = ["u_proj", "u_vel", "u_frustum", "u_depth", "u_px", "u_time", "u_blur", "u_width", "u_alpha", "u_color"] as const

export class RainOverlay {
  canvas = document.createElement("canvas")
  private gl: WebGLRenderingContext | null = null
  private prog: WebGLProgram | null = null
  private loc: Partial<Record<(typeof UNIFORMS)[number], WebGLUniformLocation | null>> = {}
  private rain: Rain | null = null
  private camera: Camera | null = null
  private raf = 0
  private still = false
  private start = performance.now()
  quiet = 1 // fewer, fainter drops while the rain radar shows (1: none taken away)
  quality = 1 // the share of drops a slow machine draws
  frames: number[] = [] // the last frame intervals, ms, for the quality fallback
  private last = 0

  constructor() {
    this.canvas.className = "weather-rain"
    this.canvas.setAttribute("aria-hidden", "true")
    Object.assign(this.canvas.style, { position: "absolute", inset: "0", width: "100%", height: "100%", pointerEvents: "none", display: "none" })
  }

  // Laid over the map's canvas, before its markers, so those stay on top.
  attach(mapCanvas: HTMLCanvasElement) {
    if (this.canvas.isConnected) return
    mapCanvas.after(this.canvas)
  }

  private init() {
    if (this.gl) return true
    const gl = this.canvas.getContext("webgl", { premultipliedAlpha: true, alpha: true, antialias: false })
    if (!gl) return false
    try {
      this.prog = program(gl, VERT, FRAG)
    } catch (e) {
      console.error(e)
      return false
    }
    this.gl = gl
    for (const u of UNIFORMS) this.loc[u] = gl.getUniformLocation(this.prog, u)
    // Six corners for each drop, each with the drop's own numbers.
    const data = new Float32Array(MAX_DROPS * 6 * 6)
    const corners = [[0, -1], [1, -1], [1, 1], [0, -1], [1, 1], [0, 1]]
    let o = 0
    for (let i = 0; i < MAX_DROPS; i++) {
      const a = Math.random(), b = Math.random(), phase = Math.random()
      for (const [along, across] of corners) {
        data.set([i, a, b, phase, along, across], o)
        o += 6
      }
    }
    const buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW)
    const drop = gl.getAttribLocation(this.prog, "a_drop")
    const corner = gl.getAttribLocation(this.prog, "a_corner")
    gl.enableVertexAttribArray(drop)
    gl.vertexAttribPointer(drop, 4, gl.FLOAT, false, 24, 0)
    gl.enableVertexAttribArray(corner)
    gl.vertexAttribPointer(corner, 2, gl.FLOAT, false, 24, 16)
    return true
  }

  // What rain there is (null: none), and whether it may move.
  set(rain: Rain | null, moving: boolean) {
    this.rain = rain && rain.share > 0 ? rain : null
    this.still = !moving
    const show = !!this.rain && this.init()
    this.canvas.style.display = show ? "block" : "none"
    if (!show) { this.stop(); return }
    if (this.still) { this.stop(); this.draw(performance.now()) }
    else if (!this.raf) this.loop()
  }

  // The map moved: the rain's camera with it (a still frame is drawn again).
  setCamera(c: Camera) {
    this.camera = c
    if (this.rain && this.still) this.draw(performance.now())
  }

  private loop = () => {
    this.raf = requestAnimationFrame((t) => {
      if (this.last) {
        this.frames.push(t - this.last)
        if (this.frames.length > 120) this.frames.shift()
      }
      this.last = t
      this.draw(t)
      if (this.raf) this.loop()
    })
  }

  private stop() {
    cancelAnimationFrame(this.raf)
    this.raf = 0
    this.last = 0
  }

  private draw(now: number) {
    const gl = this.gl, rain = this.rain, cam = this.camera
    if (!gl || !rain || !cam || !this.prog) return
    const dpr = Math.min(window.devicePixelRatio || 1, this.quality < 1 ? 1 : 1.5)
    const w = Math.round(this.canvas.clientWidth * dpr), h = Math.round(this.canvas.clientHeight * dpr)
    if (!w || !h) return
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h }
    gl.viewport(0, 0, w, h)
    gl.clearColor(0, 0, 0, 0)
    gl.clear(gl.COLOR_BUFFER_BIT)
    // The world's down and the wind's way, as seen from the camera.
    const down = apply(cam.view, 0, 0, -1, 0)
    const r = (rain.windTo * Math.PI) / 180
    const wind = apply(cam.view, Math.sin(r), Math.cos(r), 0, 0)
    const dn = Math.hypot(down[0], down[1], down[2]) || 1
    const wn = Math.hypot(wind[0], wind[1], wind[2]) || 1
    const speed = cam.dist * 0.55 * rain.fall
    const vel = [0, 1, 2].map((i) => (down[i] / dn + (wind[i] / wn) * rain.slant) * speed)
    const P = cam.proj
    if (!invert(P)) return
    gl.useProgram(this.prog)
    const L = this.loc
    gl.uniformMatrix4fv(L.u_proj!, false, new Float32Array(P))
    gl.uniform3f(L.u_vel!, vel[0], vel[1], vel[2])
    gl.uniform4f(L.u_frustum!, P[0], P[5], P[8], P[9])
    gl.uniform2f(L.u_depth!, cam.dist * 0.1, cam.dist * 0.75)
    gl.uniform2f(L.u_px!, w, h)
    gl.uniform1f(L.u_time!, this.still ? 47.3 : ((now - this.start) / 1000) % 600)
    gl.uniform1f(L.u_blur!, 0.045 * rain.streak)
    gl.uniform1f(L.u_width!, 1.25 * dpr)
    gl.uniform1f(L.u_alpha!, rain.alpha * (this.still ? 0.8 : 1) * (0.55 + 0.45 * this.quiet))
    gl.uniform3f(L.u_color!, ...rain.color)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)
    const drops = Math.round(MAX_DROPS * rain.share * this.quality * this.quiet)
    gl.drawArrays(gl.TRIANGLES, 0, drops * 6)
  }
}

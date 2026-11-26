/* The environment twin on the map.

   Terrain. AWS's keyless Terrarium elevation tiles raise the ground (the 3D city stands on it:
   MapLibre sets each building on the terrain at its centre) and shade the relief, lit from where
   the sun is at the clock's moment.

   Shadows. Every DLS building in view casts its shadow on flat ground for the clock's moment:
   the footprint swept away from the sun by height / tan(sun altitude). All of them are filled at
   once into one canvas (same winding, so overlaps merge instead of darkening), softened a
   little and laid under the buildings. They move as the clock moves or plays.

   Surfaces. The server's grid of estimates is drawn smooth (each cell bilinear between its
   neighbours) in the metric's bands, with the opacity of how sure each place is: strong at a
   station, fading where only the model speaks, gone between stations for noise and the CAQI.
   The stations sit on top with their readings.

   Wind. Particles drift with the modelled wind, faster where it is stronger, leaving short
   trails. With reduced motion they are still arrows instead.

   Rain. RainViewer's radar frame nearest the clock's moment (it keeps the last two hours).

   Answers. A sun_and_shade or environment_surface answer's layer is drawn here too, through the
   map engine's renderer hook: its grid as an image under the labels, its stations on top. */
import type { CanvasSource, GeoJSONSource, LayerSpecification, Map as MlMap, RasterTileSource } from "maplibre-gl"

import { useClock } from "@/lib/clock"
import {
  SHADE, SHADOW_INK, WIND_INK, bandColors, hexRgb, sunHoursColor, type Metric,
} from "@/lib/environment/colors"
import {
  bandOf, fetchRadar, fetchSurface, fetchWind, patch, spotId, useEnvironment, type Band, type Grid, type Surface,
  type SunHours, type Wind,
} from "@/lib/environment/store"
import { inspect } from "@/lib/inspector"
import type { Basemap } from "@/lib/map/basemap"
import {
  cityBuildings, firstLabelLayer, getMap, onCityChange, onStyleReady, registerLayerRenderer, registerMapMenuItem,
  setCity3d, themedPaint, useMapUi, type LayerData,
} from "@/lib/map/engine"
import { skyAt } from "@/lib/map/sky"
import { registerClickHandler } from "@/lib/registries/map-clicks"

/* eslint-disable @typescript-eslint/no-explicit-any */

const DEM_SRC = "env-dem"
const HILL_SRC = "env-hill-dem"
const HILL = "env-hillshade"
const SHADOW_SRC = "env-shadows-src"
const SHADOWS = "env-shadows"
const SURF_SRC = "env-surface-src"
const SURFACE = "env-surface"
const ST_SRC = "env-stations-src"
const STATIONS = "env-stations"
const SUN_SRC = "env-sunhours-src"
const SUNHOURS = "env-sunhours"
const SUN_OUTLINE = "env-sunhours-outline"
const SUN_AREA_SRC = "env-sunhours-area"
const RADAR_SRC = "env-radar-src"
const RADAR = "env-radar"
const DEM_TILES = ["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"]
const DEM_ATTRIBUTION = '<a href="https://registry.opendata.aws/terrain-tiles/" target="_blank" rel="noreferrer">Terrain tiles</a> (AWS Open Data)'
const RADAR_ATTRIBUTION = '<a href="https://www.rainviewer.com/" target="_blank" rel="noreferrer">RainViewer</a>'
const EARTH = 40_075_016.686
const MAX_SHADOW_M = 420
const SHADOW_MIN_ZOOM = 14
const FONT_BOLD = ["Noto Sans Bold"]
const RADAR_REFRESH_MS = 5 * 60e3

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches
const base = (): Basemap => useMapUi.getState().basemap
const moment = () => useClock.getState().at ?? Date.now()
const env = useEnvironment.getState

function baseLayerOfType(map: MlMap, type: string) {
  return map.getLayersOrder().find((id) => /^(night|day)\.\d+\//.test(id) && map.getLayer(id)?.type === type)
}

// -- where the layers sit ----------------------------------------------------------------------

const answerRasters = new Set<string>() // answer layers' grids, drawn under the labels

// Hillshade above the ground and under the roads; the rasters (surfaces, sun hours, shadows,
// radar) above the roads and under the labels, in that order. Run again whenever the base map
// changes, since its layers are replaced.
function place() {
  const map = getMap()
  if (!map) return
  const roads = baseLayerOfType(map, "line")
  if (map.getLayer(HILL) && roads) map.moveLayer(HILL, roads)
  const label = firstLabelLayer()
  if (!label) return
  for (const id of [SURFACE, ...answerRasters, SUNHOURS, SUN_OUTLINE, SHADOWS, RADAR]) {
    if (map.getLayer(id)) map.moveLayer(id, label)
  }
}

function addRaster(map: MlMap, def: LayerSpecification) {
  if (!map.getLayer(def.id)) map.addLayer(def, firstLabelLayer())
  place()
}

// A canvas source shows what was drawn into its canvas after a play and a pause.
function repaint(src: CanvasSource | undefined) {
  if (!src) return
  src.play()
  window.setTimeout(() => src.pause(), 160)
}

function canvasSource(map: MlMap, id: string, canvas: HTMLCanvasElement, coordinates: number[][]) {
  const src = map.getSource(id) as CanvasSource | undefined
  if (src) {
    src.setCoordinates(coordinates as any)
    repaint(src)
  } else {
    map.addSource(id, { type: "canvas", canvas, coordinates: coordinates as any, animate: false })
    repaint(map.getSource(id) as CanvasSource)
  }
}

function drop(map: MlMap, layers: string[], sources: string[]) {
  for (const id of layers) if (map.getLayer(id)) map.removeLayer(id)
  for (const id of sources) if (map.getSource(id)) map.removeSource(id)
}

// -- terrain -------------------------------------------------------------------------------------

function demSource() {
  return { type: "raster-dem" as const, tiles: DEM_TILES, tileSize: 256, maxzoom: 15, encoding: "terrarium" as const, attribution: DEM_ATTRIBUTION }
}

// The relief is lit from the sun while it is up, and from the north-west (the cartographers'
// light) at night.
function lightFrom() {
  const sky = skyAt(new Date(moment()))
  return sky.sun.altitude > 0 ? Math.round(sky.sun.bearing) : 315
}

function syncTerrain() {
  const map = getMap()
  if (!map) return
  const { on, exaggeration } = env().terrain
  if (on) {
    if (!map.getSource(DEM_SRC)) map.addSource(DEM_SRC, demSource())
    if (!map.getSource(HILL_SRC)) map.addSource(HILL_SRC, demSource())
    if (!map.getLayer(HILL)) {
      map.addLayer({
        id: HILL, type: "hillshade", source: HILL_SRC,
        paint: {
          ...themedPaint(HILL, {
            "hillshade-shadow-color": { night: "#02060e", day: "#4d4232" },
            "hillshade-highlight-color": { night: "#3a5378", day: "#ffffff" },
            "hillshade-accent-color": { night: "#0b1424", day: "#6d604c" },
            "hillshade-exaggeration": { night: 0.5, day: 0.35 },
          }),
          "hillshade-illumination-anchor": "map",
          "hillshade-illumination-direction": lightFrom(),
        },
      } as LayerSpecification, baseLayerOfType(map, "line"))
    }
    map.setTerrain({ source: DEM_SRC, exaggeration })
  } else {
    if (map.getTerrain()) map.setTerrain(null)
    if (map.getLayer(HILL)) map.removeLayer(HILL)
  }
  place()
}

export function setTerrainOn(on: boolean) {
  patch("terrain", { on })
  onStyleReady(() => {
    syncTerrain()
    const map = getMap()
    if (on && map.getPitch() < 30) map.easeTo({ pitch: 55, duration: reducedMotion() ? 0 : 900 })
  })
}

export function setRelief(exaggeration: number) {
  patch("terrain", { exaggeration })
  onStyleReady(syncTerrain)
}

// -- shadows of the 3D city ----------------------------------------------------------------------

const shadowCanvas = document.createElement("canvas")
let shadowQueued = false

export function shadowsSoon() {
  if (shadowQueued) return
  shadowQueued = true
  requestAnimationFrame(() => { shadowQueued = false; drawShadows() })
}

const mx = (lon: number) => (lon + 180) / 360
const my = (lat: number) => (1 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / Math.PI) / 2

// Adds a closed polygon (flat x, y pairs) to the path, always wound the same way, so that the
// union of everything added fills under the nonzero rule.
function addLoop(ctx: CanvasRenderingContext2D, pts: number[]) {
  const n = pts.length / 2
  if (n < 3) return
  let area = 0
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    area += pts[2 * i] * pts[2 * j + 1] - pts[2 * j] * pts[2 * i + 1]
  }
  if (Math.abs(area) < 0.02) return
  if (area > 0) {
    ctx.moveTo(pts[0], pts[1])
    for (let i = 1; i < n; i++) ctx.lineTo(pts[2 * i], pts[2 * i + 1])
  } else {
    ctx.moveTo(pts[2 * (n - 1)], pts[2 * (n - 1) + 1])
    for (let i = n - 2; i >= 0; i--) ctx.lineTo(pts[2 * i], pts[2 * i + 1])
  }
  ctx.closePath()
}

function outerRings(geom: any): number[][][] {
  if (!geom) return []
  if (geom.type === "Polygon") return geom.coordinates.slice(0, 1)
  if (geom.type === "MultiPolygon") return geom.coordinates.map((p: number[][][]) => p[0]).filter(Boolean)
  return []
}

function compass(deg: number) {
  return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][Math.round((deg % 360) / 45) % 8]
}

function drawShadows() {
  const map = getMap()
  if (!map) return
  const city = cityBuildings()
  const on = env().shadows.on
  const sky = skyAt(new Date(moment()))
  let note = ""
  let draw = on && city.on
  if (on && !city.on) note = "Turn on the 3D city: shadows are cast by its buildings"
  else if (on && sky.sun.altitude <= 0.5) { note = "The sun is down at the clock's moment"; draw = false }
  else if (on && map.getZoom() < SHADOW_MIN_ZOOM) { note = "Zoom in to the streets to see the shadows"; draw = false }
  else if (on) note = `Sun from the ${compass(sky.sun.bearing)}, ${Math.round(sky.sun.altitude)}° up`
  if (env().shadows.note !== note) patch("shadows", { note })
  if (!draw) {
    if (map.getLayer(SHADOWS)) map.setLayoutProperty(SHADOWS, "visibility", "none")
    return
  }
  const b = map.getBounds()
  const c = map.getCenter()
  const w = Math.max(b.getWest(), c.lng - 0.03), e = Math.min(b.getEast(), c.lng + 0.03)
  const s = Math.max(b.getSouth(), c.lat - 0.024), n = Math.min(b.getNorth(), c.lat + 0.024)
  const x0 = mx(w), x1 = mx(e), y0 = my(n), y1 = my(s)
  const scale = Math.min(512 * 2 ** map.getZoom(), 2048 / (x1 - x0), 2048 / (y1 - y0))
  const W = Math.max(1, Math.ceil((x1 - x0) * scale)), H = Math.max(1, Math.ceil((y1 - y0) * scale))
  if (shadowCanvas.width !== W || shadowCanvas.height !== H) { shadowCanvas.width = W; shadowCanvas.height = H }
  const ctx = shadowCanvas.getContext("2d")!
  ctx.clearRect(0, 0, W, H)
  // Away from the sun, per metre of height.
  const away = ((sky.sun.bearing + 180) * Math.PI) / 180
  const reach = 1 / Math.tan((sky.sun.altitude * Math.PI) / 180)
  const perM = scale / (EARTH * Math.cos((c.lat * Math.PI) / 180)) // canvas px per metre
  const dxPerM = Math.sin(away) * reach * perM
  const dyPerM = -Math.cos(away) * reach * perM
  const pad = (MAX_SHADOW_M / 111_000) * 1.3
  ctx.beginPath()
  for (const f of city.features) {
    const h = Number(f.properties?.h) || 0
    if (h <= 0) continue
    const len = Math.min(h, MAX_SHADOW_M / reach)
    const ox = dxPerM * len, oy = dyPerM * len
    for (const ring of outerRings(f.geometry)) {
      const [lon, lat] = ring[0]
      if (lon < w - pad || lon > e + pad || lat < s - pad || lat > n + pad) continue
      const pts: number[] = []
      for (const [lo, la] of ring) pts.push((mx(lo) - x0) * scale, (my(la) - y0) * scale)
      if (pts.length > 2 && pts[0] === pts[pts.length - 2] && pts[1] === pts[pts.length - 1]) pts.length -= 2
      const k = pts.length / 2
      addLoop(ctx, pts)
      addLoop(ctx, pts.map((v, i) => v + (i % 2 ? oy : ox)))
      for (let i = 0; i < k; i++) {
        const j = (i + 1) % k
        const ax = pts[2 * i], ay = pts[2 * i + 1], bx = pts[2 * j], by = pts[2 * j + 1]
        addLoop(ctx, [ax, ay, bx, by, bx + ox, by + oy, ax + ox, ay + oy])
      }
    }
  }
  ctx.fillStyle = SHADOW_INK[base()]
  ctx.filter = `blur(${Math.max(0.6, Math.min(2.5, 0.8 * perM)).toFixed(2)}px)`
  ctx.fill("nonzero")
  ctx.filter = "none"
  canvasSource(map, SHADOW_SRC, shadowCanvas, [[w, n], [e, n], [e, s], [w, s]])
  if (!map.getLayer(SHADOWS)) {
    addRaster(map, {
      id: SHADOWS, type: "raster", source: SHADOW_SRC,
      paint: { ...themedPaint(SHADOWS, { "raster-opacity": { night: 0.55, day: 0.34 } }), "raster-fade-duration": 0, "raster-resampling": "linear" },
    } as LayerSpecification)
  }
  map.setLayoutProperty(SHADOWS, "visibility", "visible")
}

export function setShadowsOn(on: boolean) {
  patch("shadows", { on })
  if (on && !cityBuildings().on) setCity3d(true)
  const map = getMap()
  if (on && map && map.getZoom() < 15.5) {
    map.easeTo({ zoom: 16.2, pitch: Math.max(map.getPitch(), 50), duration: reducedMotion() ? 0 : 900 })
  }
  onStyleReady(shadowsSoon)
}

// -- grids drawn as images -------------------------------------------------------------------

// A surface's grid as a smooth image: each pixel bilinear between the cells around it, coloured
// by its band, as opaque as the estimate there is sure.
function paintSurface(canvas: HTMLCanvasElement, surf: Surface, theme: Basemap) {
  const g = surf.grid as Grid
  const K = 4
  const W = g.nx * K, H = g.ny * K
  canvas.width = W
  canvas.height = H
  const ctx = canvas.getContext("2d")!
  const img = ctx.createImageData(W, H)
  const colors = bandColors(surf.metric, theme).map(hexRgb)
  const bands = surf.bands as Band[]
  const stationsOnly = surf.background === "stations"
  const cell = (i: number, j: number) => {
    const ii = Math.min(g.nx - 1, Math.max(0, i)), jj = Math.min(g.ny - 1, Math.max(0, j))
    return jj * g.nx + ii
  }
  for (let py = 0; py < H; py++) {
    const gy = (py + 0.5) / K - 0.5
    const j0 = Math.floor(gy), fy = gy - j0
    for (let px = 0; px < W; px++) {
      const gx = (px + 0.5) / K - 0.5
      const i0 = Math.floor(gx), fx = gx - i0
      let v = 0, wsum = 0, c = 0
      for (const [di, dj, wt] of [[0, 0, (1 - fx) * (1 - fy)], [1, 0, fx * (1 - fy)], [0, 1, (1 - fx) * fy], [1, 1, fx * fy]] as const) {
        const k = cell(i0 + di, j0 + dj)
        c += wt * g.confidence[k]
        const val = g.values[k]
        if (val === null) continue
        v += wt * val
        wsum += wt
      }
      const o = 4 * (py * W + px)
      if (!wsum) continue
      const conf = c / 100
      const alpha = stationsOnly ? (conf < 0.06 ? 0 : 0.8 * conf) : 0.16 + 0.62 * conf
      const rgb = colors[Math.min(colors.length - 1, Math.max(0, bandOf(bands, v / wsum)))]
      img.data[o] = rgb[0]
      img.data[o + 1] = rgb[1]
      img.data[o + 2] = rgb[2]
      img.data[o + 3] = Math.round(255 * alpha)
    }
  }
  ctx.putImageData(img, 0, 0)
  const [w, s, e, n] = g.bbox
  return [[w, n], [e, n], [e, s], [w, s]]
}

function decode(b64: string) {
  const raw = atob(b64)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

// A sun hours grid (quarter hours per cell, 255 on a roof) or a shade grid (1 shade, 0 sun).
function paintSun(canvas: HTMLCanvasElement, grid: { nx: number; ny: number; grid: string; unit: string }, theme: Basemap) {
  const cells = decode(grid.grid)
  canvas.width = grid.nx
  canvas.height = grid.ny
  const ctx = canvas.getContext("2d")!
  const img = ctx.createImageData(grid.nx, grid.ny)
  const shade = hexRgb(SHADE.shade), sun = hexRgb(SHADE.sun)
  for (let k = 0; k < cells.length; k++) {
    const v = cells[k]
    if (v === 255) continue
    const rgb = grid.unit === "shade" ? (v ? shade : sun) : hexRgb(sunHoursColor(v / 4, theme))
    const o = 4 * k
    img.data[o] = rgb[0]
    img.data[o + 1] = rgb[1]
    img.data[o + 2] = rgb[2]
    img.data[o + 3] = grid.unit === "shade" ? (v ? 200 : 110) : 215
  }
  ctx.putImageData(img, 0, 0)
}

// -- the surface switched on in the map panel ------------------------------------------------

const surfCanvas = document.createElement("canvas")
let surfAbort: AbortController | null = null

export function setSurfaceMetric(metric: Metric | null) {
  patch("surface", { metric, error: null, ...(metric ? {} : { data: null, status: "idle" as const }) })
  loadSurface()
}

function loadSurface() {
  const { metric } = env().surface
  surfAbort?.abort()
  if (!metric) { drawSurface(); return }
  const ac = (surfAbort = new AbortController())
  patch("surface", { status: "loading" })
  fetchSurface(metric, useClock.getState().at, ac.signal)
    .then((data) => {
      if (ac.signal.aborted || env().surface.metric !== metric) return
      patch("surface", { data, status: "ready", error: data.known ? null : data.reason ?? "Nothing is known for that moment" })
      drawSurface()
    })
    .catch((e) => {
      if (ac.signal.aborted) return
      patch("surface", { status: "error", error: (e as Error).message })
      drawSurface()
    })
}

function shortValue(v: number) {
  return Math.abs(v) < 10 ? (Math.round(v * 10) / 10).toString() : Math.round(v).toString()
}

function stationFeatures(surf: Surface, theme: Basemap) {
  const colors = bandColors(surf.metric, theme)
  return {
    type: "FeatureCollection" as const,
    features: (surf.stations ?? []).map((st) => ({
      type: "Feature" as const, geometry: { type: "Point" as const, coordinates: [st.lon, st.lat] },
      properties: { id: st.id, color: colors[Math.max(0, bandOf(surf.bands, st.value))], value: shortValue(st.value) },
    })),
  }
}

// An answer's stations, as the tool drew them, in the colours of the base map on show.
function answerStations(data: LayerData, surf: Surface, theme: Basemap) {
  const colors = bandColors(surf.metric, theme)
  return {
    ...data,
    features: data.features.map((f) => {
      const v = Number(f.properties?.[surf.metric])
      return Number.isFinite(v)
        ? { ...f, properties: { ...f.properties, color: colors[Math.max(0, bandOf(surf.bands, v))], value: shortValue(v) } }
        : f
    }),
  }
}

function addStationLayers(map: MlMap, src: string, prefix: string, ids?: string[]) {
  const circle = `${prefix}${prefix.endsWith("::") ? "" : "-"}dots`
  const label = `${prefix}${prefix.endsWith("::") ? "" : "-"}values`
  if (!map.getLayer(circle)) {
    map.addLayer({
      id: circle, type: "circle", source: src,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 5, 15, 8],
        "circle-color": ["get", "color"],
        ...themedPaint(circle, { "circle-stroke-color": { night: "#e4ebf4", day: "#0e1828" } }),
        "circle-stroke-width": 1.6,
      },
    })
    ids?.push(circle)
  }
  if (!map.getLayer(label)) {
    map.addLayer({
      id: label, type: "symbol", source: src,
      layout: { "text-field": ["get", "value"], "text-font": FONT_BOLD, "text-size": 12, "text-offset": [0, -1.35], "text-allow-overlap": true },
      paint: {
        ...themedPaint(label, { "text-color": { night: "#e4ebf4", day: "#0e1828" }, "text-halo-color": { night: "#0e1828", day: "#ffffff" } }),
        "text-halo-width": 1.6,
      },
    })
    ids?.push(label)
  }
  return [circle, label]
}

function drawSurface() {
  const map = getMap()
  if (!map) return
  const { metric, data } = env().surface
  if (!metric || !data?.known || !data.grid) {
    drop(map, [SURFACE, `${STATIONS}-dots`, `${STATIONS}-values`], [SURF_SRC, ST_SRC])
    return
  }
  const coords = paintSurface(surfCanvas, data, base())
  canvasSource(map, SURF_SRC, surfCanvas, coords)
  addRaster(map, { id: SURFACE, type: "raster", source: SURF_SRC, paint: { "raster-opacity": 1, "raster-fade-duration": 0, "raster-resampling": "linear" } } as LayerSpecification)
  const fc = stationFeatures(data, base())
  const src = map.getSource(ST_SRC) as GeoJSONSource | undefined
  if (src) src.setData(fc as any)
  else map.addSource(ST_SRC, { type: "geojson", data: fc as any })
  addStationLayers(map, ST_SRC, STATIONS)
}

// -- wind ------------------------------------------------------------------------------------

class WindOverlay {
  canvas = document.createElement("canvas")
  ctx = this.canvas.getContext("2d")!
  wind: Wind | null = null
  field: { cols: number; rows: number; vx: Float32Array; vy: Float32Array; speed: Float32Array } | null = null
  px = new Float32Array(0)
  py = new Float32Array(0)
  age = new Float32Array(0)
  life = new Float32Array(0)
  running = false
  raf = 0
  last = 0
  fieldTimer = 0
  attached = false

  attach(map: MlMap) {
    if (this.attached) return
    this.canvas.className = "wind-canvas"
    this.canvas.setAttribute("aria-hidden", "true")
    map.getCanvasContainer().appendChild(this.canvas)
    map.on("move", () => this.moved())
    map.on("moveend", () => this.moved(true))
    map.on("resize", () => this.moved(true))
    document.addEventListener("visibilitychange", () => this.sync())
    this.attached = true
  }

  set(wind: Wind | null) {
    this.wind = wind?.known ? wind : null
    this.sync()
  }

  sync() {
    const map = getMap()
    const want = !!map && !!this.wind && (env().wind.on || env().wind.answers > 0)
    if (!want) { this.stop(); return }
    this.attach(map)
    this.resize()
    this.computeField()
    if (reducedMotion()) { this.stopLoop(); this.drawArrows(); return }
    if (document.hidden) { this.stopLoop(); return }
    if (!this.running) { this.running = true; this.last = performance.now(); this.raf = requestAnimationFrame((t) => this.frame(t)) }
  }

  stopLoop() {
    this.running = false
    cancelAnimationFrame(this.raf)
  }

  stop() {
    this.stopLoop()
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height)
    this.canvas.style.display = "none"
  }

  resize() {
    const map = getMap()
    const el = map.getCanvasContainer()
    const dpr = Math.min(2, window.devicePixelRatio || 1)
    const W = el.clientWidth, H = el.clientHeight
    this.canvas.style.display = "block"
    if (this.canvas.width !== Math.round(W * dpr) || this.canvas.height !== Math.round(H * dpr)) {
      this.canvas.width = Math.round(W * dpr)
      this.canvas.height = Math.round(H * dpr)
      this.canvas.style.width = `${W}px`
      this.canvas.style.height = `${H}px`
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const n = Math.round(Math.max(250, Math.min(1400, (W * H) / 1700)))
      this.px = new Float32Array(n); this.py = new Float32Array(n); this.age = new Float32Array(n); this.life = new Float32Array(n)
      for (let i = 0; i < n; i++) this.spawn(i, true)
    }
  }

  spawn(i: number, anywhere = false) {
    const el = getMap().getCanvasContainer()
    this.px[i] = Math.random() * el.clientWidth
    this.py[i] = Math.random() * el.clientHeight
    this.life[i] = 50 + Math.random() * 90
    this.age[i] = anywhere ? Math.random() * this.life[i] : 0
  }

  // The wind at screen points: u and v from the model's lattice at each point's place, turned
  // into a screen direction (so the map's bearing and pitch are taken into account). Speed is
  // drawn as 6 + 9 px/s per m/s: never still, never a blur.
  computeField() {
    const map = getMap()
    const w = this.wind
    if (!w) { this.field = null; return }
    const el = map.getCanvasContainer()
    const cols = 14, rows = 10
    const vx = new Float32Array(cols * rows), vy = new Float32Array(cols * rows), speed = new Float32Array(cols * rows)
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const sx = ((c + 0.5) / cols) * el.clientWidth, sy = ((r + 0.5) / rows) * el.clientHeight
        const ll = map.unproject([sx, sy])
        const u = lattice(w, w.u, ll.lat, ll.lng), v = lattice(w, w.v, ll.lat, ll.lng)
        const k = r * cols + c
        if (u === null || v === null) continue
        const p0 = map.project(ll)
        const pe = map.project([ll.lng + 0.001, ll.lat])
        const pn = map.project([ll.lng, ll.lat + 0.001])
        const ex = pe.x - p0.x, ey = pe.y - p0.y, nx = pn.x - p0.x, ny = pn.y - p0.y
        let dx = u * ex + v * nx, dy = u * ey + v * ny
        const len = Math.hypot(dx, dy) || 1
        dx /= len; dy /= len
        const sp = Math.hypot(u, v)
        const pxs = 6 + 9 * sp
        vx[k] = dx * pxs; vy[k] = dy * pxs; speed[k] = sp
      }
    }
    this.field = { cols, rows, vx, vy, speed }
  }

  velocity(x: number, y: number): [number, number] {
    const f = this.field!
    const el = getMap().getCanvasContainer()
    const gx = Math.min(f.cols - 1.001, Math.max(0, (x / el.clientWidth) * f.cols - 0.5))
    const gy = Math.min(f.rows - 1.001, Math.max(0, (y / el.clientHeight) * f.rows - 0.5))
    const c0 = Math.floor(gx), r0 = Math.floor(gy), fx = gx - c0, fy = gy - r0
    const at = (r: number, c: number) => r * f.cols + c
    const mix = (a: Float32Array) => (a[at(r0, c0)] * (1 - fx) + a[at(r0, c0 + 1)] * fx) * (1 - fy) + (a[at(r0 + 1, c0)] * (1 - fx) + a[at(r0 + 1, c0 + 1)] * fx) * fy
    return [mix(f.vx), mix(f.vy)]
  }

  moved(settled = false) {
    if (!this.wind || this.canvas.style.display === "none") return
    window.clearTimeout(this.fieldTimer)
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height)
    if (settled) { this.resize(); this.computeField(); if (reducedMotion()) this.drawArrows() }
    else this.fieldTimer = window.setTimeout(() => this.computeField(), 120)
  }

  frame(t: number) {
    if (!this.running) return
    this.raf = requestAnimationFrame((tt) => this.frame(tt))
    const dt = Math.min(0.1, (t - this.last) / 1000)
    if (dt < 1 / 40) return // about 30 frames a second is plenty
    this.last = t
    const ctx = this.ctx
    const el = getMap().getCanvasContainer()
    const W = el.clientWidth, H = el.clientHeight
    ctx.globalCompositeOperation = "destination-out"
    ctx.fillStyle = "rgba(0,0,0,0.1)"
    ctx.fillRect(0, 0, W, H)
    ctx.globalCompositeOperation = "source-over"
    if (!this.field) return
    ctx.strokeStyle = WIND_INK[base()]
    ctx.lineWidth = 1.3
    ctx.lineCap = "round"
    ctx.beginPath()
    for (let i = 0; i < this.px.length; i++) {
      const x = this.px[i], y = this.py[i]
      const [vx, vy] = this.velocity(x, y)
      const nx = x + vx * dt, ny = y + vy * dt
      this.age[i] += 1
      if (this.age[i] > this.life[i] || nx < 0 || ny < 0 || nx > W || ny > H) { this.spawn(i); continue }
      ctx.moveTo(x, y)
      ctx.lineTo(nx, ny)
      this.px[i] = nx
      this.py[i] = ny
    }
    ctx.stroke()
  }

  // Reduced motion: a still field of arrows pointing where the wind goes, longer where it is
  // stronger.
  drawArrows() {
    if (!this.field) return
    const ctx = this.ctx
    const el = getMap().getCanvasContainer()
    const W = el.clientWidth, H = el.clientHeight
    ctx.clearRect(0, 0, W, H)
    ctx.strokeStyle = WIND_INK[base()]
    ctx.lineWidth = 1.6
    ctx.lineCap = "round"
    const step = 72
    for (let y = step / 2; y < H; y += step) {
      for (let x = step / 2; x < W; x += step) {
        const [vx, vy] = this.velocity(x, y)
        const len = Math.hypot(vx, vy) || 1
        const l = Math.min(30, 8 + len * 0.45)
        const ux = vx / len, uy = vy / len
        const x2 = x + ux * l, y2 = y + uy * l
        ctx.beginPath()
        ctx.moveTo(x - ux * l * 0.5, y - uy * l * 0.5)
        ctx.lineTo(x2, y2)
        ctx.moveTo(x2, y2)
        ctx.lineTo(x2 - ux * 6 - uy * 4, y2 - uy * 6 + ux * 4)
        ctx.moveTo(x2, y2)
        ctx.lineTo(x2 - ux * 6 + uy * 4, y2 - uy * 6 - ux * 4)
        ctx.stroke()
      }
    }
  }
}

function lattice(w: Wind, values: (number | null)[], lat: number, lon: number): number | null {
  const idx = (axis: number[], v: number): [number, number] => {
    if (v <= axis[0]) return [0, 0]
    if (v >= axis[axis.length - 1]) return [axis.length - 2, 1]
    for (let i = 0; i < axis.length - 1; i++) if (v <= axis[i + 1]) return [i, (v - axis[i]) / (axis[i + 1] - axis[i])]
    return [axis.length - 2, 1]
  }
  const [i, fy] = idx(w.lats, lat)
  const [j, fx] = idx(w.lons, lon)
  const n = w.lons.length
  const corners: [number | null, number][] = [
    [values[i * n + j], (1 - fy) * (1 - fx)], [values[i * n + j + 1], (1 - fy) * fx],
    [values[(i + 1) * n + j], fy * (1 - fx)], [values[(i + 1) * n + j + 1], fy * fx],
  ]
  let v = 0, wsum = 0
  for (const [val, wt] of corners) if (val !== null) { v += val * wt; wsum += wt }
  return wsum ? v / wsum : null
}

const wind = new WindOverlay()
let windAbort: AbortController | null = null
let windHour = ""

function loadWind(force = false) {
  const { on, answers } = env().wind
  if (!on && !answers) { wind.set(null); return }
  const at = useClock.getState().at
  const hour = at === null ? "live" : new Date(at).toISOString().slice(0, 13)
  if (!force && hour === windHour && env().wind.data) { wind.set(env().wind.data); return }
  windHour = hour
  windAbort?.abort()
  const ac = (windAbort = new AbortController())
  patch("wind", { status: "loading" })
  fetchWind(at, ac.signal)
    .then((data) => {
      if (ac.signal.aborted) return
      patch("wind", { data, status: "ready", error: data.known ? null : data.reason ?? "No wind for that hour" })
      wind.set(data)
    })
    .catch((e) => { if (!ac.signal.aborted) { patch("wind", { status: "error", error: (e as Error).message }); wind.set(null) } })
}

export function setWindOn(on: boolean) {
  patch("wind", { on })
  onStyleReady(() => loadWind())
}

// -- rain radar --------------------------------------------------------------------------------

let radarAt = 0

function syncRadar() {
  const map = getMap()
  if (!map) return
  const r = env().rain
  if (!r.on) { drop(map, [RADAR], [RADAR_SRC]); return }
  if (!r.radar || Date.now() - radarAt > RADAR_REFRESH_MS) {
    if (r.status !== "loading") {
      patch("rain", { status: "loading" })
      radarAt = Date.now()
      fetchRadar()
        .then((radar) => { patch("rain", { radar, status: radar.known ? "ready" : "error", error: radar.reason }); syncRadar() })
        .catch((e) => { patch("rain", { status: "error", error: (e as Error).message }) })
    }
    if (!r.radar) return
  }
  const frames = r.radar.frames
  if (!frames.length) { drop(map, [RADAR], [RADAR_SRC]); return }
  const t = moment()
  if (t < frames[0].time - 10 * 60e3) {
    patch("rain", { status: "old", frame: null })
    drop(map, [RADAR], [RADAR_SRC])
    return
  }
  const frame = frames.reduce((best, f) => (Math.abs(f.time - t) < Math.abs(best.time - t) ? f : best), frames[0])
  if (r.frame?.path !== frame.path || r.status !== "ready") patch("rain", { frame, status: "ready" })
  const src = map.getSource(RADAR_SRC) as RasterTileSource | undefined
  if (src) src.setTiles([frame.tiles])
  else map.addSource(RADAR_SRC, { type: "raster", tiles: [frame.tiles], tileSize: 256, maxzoom: r.radar.max_zoom, attribution: RADAR_ATTRIBUTION })
  addRaster(map, { id: RADAR, type: "raster", source: RADAR_SRC, paint: { "raster-opacity": 0.75, "raster-fade-duration": 0, "raster-resampling": "linear" } } as LayerSpecification)
}

export function setRainOn(on: boolean) {
  patch("rain", { on, ...(on ? {} : { frame: null }) })
  onStyleReady(syncRadar)
}

// -- sun hours of a square (the workspace's analysis) ------------------------------------------

const sunCanvas = document.createElement("canvas")

export function showSunHours(data: SunHours | null) {
  const map = getMap()
  if (!map) return
  if (!data) {
    drop(map, [SUNHOURS, SUN_OUTLINE], [SUN_SRC, SUN_AREA_SRC])
    patch("sunHours", { shown: false })
    return
  }
  paintSun(sunCanvas, data, base())
  canvasSource(map, SUN_SRC, sunCanvas, data.bounds)
  addRaster(map, { id: SUNHOURS, type: "raster", source: SUN_SRC, paint: { "raster-opacity": 0.9, "raster-fade-duration": 0, "raster-resampling": "nearest" } } as LayerSpecification)
  const ring = [...data.bounds, data.bounds[0]]
  const area = { type: "Feature", geometry: { type: "Polygon", coordinates: [ring] }, properties: {} }
  const src = map.getSource(SUN_AREA_SRC) as GeoJSONSource | undefined
  if (src) src.setData(area as any)
  else map.addSource(SUN_AREA_SRC, { type: "geojson", data: area as any })
  addRaster(map, { id: SUN_OUTLINE, type: "line", source: SUN_AREA_SRC, paint: { "line-color": "#ffae45", "line-width": 1.6, "line-dasharray": [2, 2] } } as LayerSpecification)
  patch("sunHours", { shown: true })
}

// -- answers' layers ---------------------------------------------------------------------------

type EnvMeta =
  | ({ kind: "surface" } & Surface)
  | { kind: "sun_hours" | "shade"; bounds: [number, number][]; nx: number; ny: number; grid: string; unit: string; date: string; at?: string }
  | { kind: "wind"; at: string | null }

const answerCanvases = new Map<string, HTMLCanvasElement>()
const answerData = new Map<string, EnvMeta>()
const answerSources = new Map<string, { src: string; data: LayerData }>() // a surface's stations

// What an answer's environment layer shows, for its legend in the map panel.
export function answerMeta(ref: string): EnvMeta | null {
  return answerData.get(ref) ?? null
}

function drawAnswer(ref: string) {
  const map = getMap()
  const meta = answerData.get(ref)
  if (!map || !meta || meta.kind === "wind") return
  const canvas = answerCanvases.get(ref) ?? document.createElement("canvas")
  answerCanvases.set(ref, canvas)
  let coords: number[][]
  if (meta.kind === "surface") {
    coords = paintSurface(canvas, meta, base())
    const st = answerSources.get(ref)
    if (st) (map.getSource(st.src) as GeoJSONSource | undefined)?.setData(answerStations(st.data, meta, base()) as any)
  } else { paintSun(canvas, meta, base()); coords = meta.bounds }
  canvasSource(map, `${ref}::grid-src`, canvas, coords)
}

registerLayerRenderer({
  id: "environment",
  claims: (d: LayerData) => !!d.meta?.environment,
  add: (ref, src, data, ids) => {
    const map = getMap()
    const meta = data.meta.environment as EnvMeta
    answerData.set(ref, meta)
    if (meta.kind === "wind") {
      patch("wind", { answers: env().wind.answers + 1 })
      loadWind(true)
      return
    }
    drawAnswer(ref)
    const grid = `${ref}::grid`
    answerRasters.add(grid)
    addRaster(map, {
      id: grid, type: "raster", source: `${ref}::grid-src`,
      paint: { "raster-opacity": meta.kind === "surface" ? 1 : 0.9, "raster-fade-duration": 0, "raster-resampling": meta.kind === "surface" ? "linear" : "nearest" },
    } as LayerSpecification)
    ids.push(grid)
    if (meta.kind === "surface") {
      if (env().surface.metric) setSurfaceMetric(null) // one surface at a time: the answer's
      answerSources.set(ref, { src, data })
      ;(map.getSource(src) as GeoJSONSource).setData(answerStations(data, meta, base()) as any)
      addStationLayers(map, src, `${ref}::`, ids)
    } else {
      map.addLayer({ id: `${ref}::outline`, type: "line", source: src, paint: { "line-color": "#ffae45", "line-width": 1.6, "line-dasharray": [2, 2] } })
      ids.push(`${ref}::outline`)
    }
  },
  remove: (ref) => {
    const map = getMap()
    const meta = answerData.get(ref)
    answerData.delete(ref)
    answerCanvases.delete(ref)
    answerSources.delete(ref)
    if (meta?.kind === "wind") {
      patch("wind", { answers: Math.max(0, env().wind.answers - 1) })
      loadWind()
      return
    }
    answerRasters.delete(`${ref}::grid`)
    drop(map, [`${ref}::grid`], [`${ref}::grid-src`])
  },
})

// The stations of a surface open in the inspector like the air stations they are.
registerClickHandler({
  id: "environment-stations",
  priority: 9,
  layers: (l) => l === `${STATIONS}-dots`,
  resolve: (f, at) => (f.properties?.id ? { id: String(f.properties.id), at } : null),
})

// -- "What's it like here?" --------------------------------------------------------------------

export function openSpot(lat: number, lon: number, opts: { opener?: HTMLElement | null } = {}) {
  inspect({ id: spotId(lat, lon), at: [lon, lat] }, { fly: false, focus: true, opener: opts.opener })
}

export function openSpotAtCentre(opener?: HTMLElement | null) {
  const c = getMap().getCenter()
  openSpot(c.lat, c.lng, { opener })
}

registerMapMenuItem({ id: "spot", label: "What's it like here?", order: 10, run: (at) => openSpot(at.lat, at.lng) })

// -- following the clock and the base map ----------------------------------------------------

let started = false

export function startEnvironmentMap() {
  if (started) return
  started = true
  let clockTimer: number | undefined
  useClock.subscribe((st, prev) => {
    if (st.at === prev.at) return
    shadowsSoon() // the sun moves at once, also while playing
    const map = getMap()
    if (map?.getLayer(HILL)) map.setPaintProperty(HILL, "hillshade-illumination-direction", lightFrom())
    window.clearTimeout(clockTimer)
    clockTimer = window.setTimeout(() => {
      if (env().surface.metric) loadSurface()
      loadWind()
      if (env().rain.on) syncRadar()
    }, st.playing ? 600 : 180)
  })
  let lastBase = useMapUi.getState().basemap
  useMapUi.subscribe((st) => {
    if (st.basemap === lastBase) return
    lastBase = st.basemap
    onStyleReady(() => {
      place()
      drawSurface()
      for (const ref of answerData.keys()) drawAnswer(ref)
      const sh = env().sunHours
      if (sh.shown && sh.data) showSunHours(sh.data)
      shadowsSoon()
      if (reducedMotion()) wind.sync()
    })
  })
  onCityChange(shadowsSoon)
  onStyleReady(() => {
    const map = getMap()
    map.on("moveend", shadowsSoon)
    map.on("zoomend", shadowsSoon)
    map.on("style.load", () => {
      syncTerrain()
      drawSurface()
      syncRadar()
      shadowsSoon()
    })
  })
  window.setInterval(() => {
    if (document.hidden) return
    if (useClock.getState().at !== null) return
    if (env().rain.on) syncRadar() // a new frame every 10 minutes
    if (env().surface.metric) loadSurface()
    if (env().shadows.on) shadowsSoon() // the sun moves on
  }, RADAR_REFRESH_MS)
}

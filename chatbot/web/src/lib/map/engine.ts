/* The map: MapLibre plus everything the assistant draws on it. Ported from the original
   app.js; React reads what it needs through `useMapUi`, and everything else stays imperative. */
import maplibregl, {
  type GeoJSONSource, type LayerSpecification, type Map as MlMap, type VectorTileSource,
} from "maplibre-gl"
import { LocateFixedIcon, type LucideIcon } from "lucide-react"
import { create } from "zustand"

import { BASE_SOURCE, BASE_SOURCE_URLS, BASEMAP_URLS, STREET_GLOW_OPACITY, baseLayers, type Basemap } from "@/lib/map/basemap"
import { lightFor, nicosiaWallToDate, skyAt, skyFor, type Sky } from "@/lib/map/sky"
import { kindOfId } from "@/lib/entities"
import { clickableLayer, handleEmptyClick, resolveClick, type InspectTarget } from "@/lib/registries/map-clicks"
import { WALLS_RING } from "@/lib/walls"

/* eslint-disable @typescript-eslint/no-explicit-any */
type Feature = { type: "Feature"; id?: string | number; geometry: any; properties: Record<string, any> }
export type FC = { type: "FeatureCollection"; features: Feature[]; frames?: Frames; street?: Street; meta?: any }
// Every device of a kind at one moment, from /api/clock/states.
export type StatesAt = {
  kind: string; at: string; label: string; known: boolean; coverage_pct: number; reason: string | null
  colors: Record<string, string>
  devices: Record<string, { s: string } & Record<string, number | string>>
}
type Frames = {
  times: string[]; iso: string[]; state_names: string[]; states: number[][]
  values: Record<string, (number | null)[][]>
  totals?: Record<string, number[]> // the layer as a whole in each frame, by value field
  total_field?: string // the total to read out when the layer shows no value field
}
// The street a layer is about (the one its cameras stand along), as a line to highlight.
type Street = { name: string; coordinates: number[][] }
export type LayerSpec = {
  ref: string; url?: string; style: string; title: string; count: number
  legend?: Record<string, string>; value_field?: string | null; fit?: boolean; labels?: boolean
  askedAt?: number
  sources?: string[] // where its data comes from (lib/sources.ts)
}
type Entry = {
  spec: LayerSpec; data: FC; ids: string[]; stats: Stats | null; half?: number
  extruded?: boolean // has 3D footprints
  lamps?: boolean // street lights, which glow after dark
  // How the layer follows the city clock: its features' properties as the answer drew them,
  // the kinds of device it shows, its frames' moments, and what it shows on each side of a
  // comparison (null: as drawn).
  orig: Record<string, any>[]
  kinds: string[]
  times: number[] // frame moments (instants), for a time-lapse
  a: Record<string, any>[] | null
  b: Record<string, any>[] | null
  legendNow?: Record<string, string>
  frame?: number | null // the frame on show at the clock's moment (null: outside the replay)
  renderer?: LayerRenderer // drawn by a feature's own renderer (live buses, road speeds)
}

// A feature that draws some answer layers its own way (live buses gliding, road speeds offset
// by direction) registers a renderer. It claims a layer by its data and draws it from the
// layer's source; the layer is otherwise an ordinary one (the map panel lists it, charts link
// to its features, a fit frames it, clearing removes it).
export type LayerData = FC
export type LayerRenderer = {
  id: string
  claims: (data: FC) => boolean
  add: (ref: string, src: string, data: FC, ids: string[]) => void
  remove?: (ref: string) => void
}
const renderers: LayerRenderer[] = []
export function registerLayerRenderer(r: LayerRenderer) {
  const i = renderers.findIndex((x) => x.id === r.id)
  if (i >= 0) renderers.splice(i, 1)
  renderers.push(r)
}
type Stats = { min: number; max: number }

export const RAMP = ["#16a34a", "#84cc16", "#eab308", "#f97316", "#dc2626"]
export const SODIUM = "#ffae45"
const NIGHT = "#0e1828"
const FONT_REGULAR = ["Noto Sans Regular"]
const FONT_BOLD = ["Noto Sans Bold"]
const HOME = { center: [33.3615, 35.1725] as [number, number], zoom: 14.2, pitch: 45, bearing: -12 }
export const REGISTERS_REF = "eoa_registers"

// The DLS 3D city stays dark slate and lightens only as it climbs: the skyline reads at a glance,
// and the assistant's layers (red registers, blue routes) still stand out against it.
const CITY_HEIGHTS_NIGHT = [0, "#1a2840", 6, "#203250", 12, "#2a4064", 20, "#37547f", 35, "#5577a6", 60, "#93b3dc"]
const CITY_HEIGHTS_DAY = [0, "#e5e7eb", 6, "#cbd5e1", 12, "#93c5fd", 20, "#3b82f6", 35, "#1d4ed8", 60, "#312e81"]
const CITY_SRC = "city3d::src"
const CITY_MIN_ZOOM = 14.5
const CITY_RADIUS = 3 // tiles around the centre (each ~500 m), so pitched views stay bounded
// The 3D city is drawn from vector tiles (chatbot/citytiles.py): MapLibre loads what a view needs,
// near and far, and keeps what it has. Outside the DLS model they hold OpenStreetMap's buildings
// (`src: "osm"`), drawn in a plain grey so the measured city reads apart from the mapped one.
const CITY_TILES = "/api/buildings3d.json" // zooms and bounds (a TileJSON)
// MapLibre fetches tiles in its worker, where an address without its origin doesn't resolve. The
// version (citytiles.VERSION, kept in step by a test) lets browsers drop tiles when it changes.
const CITY_TILE_URL = "/api/buildings3d/{z}/{x}/{y}.mvt?v=1"
const CITY_LAYER = "buildings"
const CITY_DRAW_ZOOM = 12.5 // it fades in from here
const OSM_HEIGHTS_NIGHT = [0, "#3b4354", 12, "#475165", 40, "#5b667b"]
const OSM_HEIGHTS_DAY = [0, "#dedbd5", 12, "#d2cec6", 40, "#c2bdb4"]
// With "Heights and the 3D city" off, the same tiles are the map's everyday 3D buildings, painted
// as the base maps paint their own (lib/map/basemap.ts for Night, OpenFreeMap's Liberty for Day)
// and fading in where theirs did, from z14.
const PLAIN_HEIGHTS_NIGHT = [0, "#17253c", 15, "#1d2e4a", 40, "#263b5c"]
const PLAIN_DAY = "hsl(35, 8%, 85%)"
const PLAIN_FROM_ZOOM = 13.6
// Solid: a see-through extrusion is drawn twice (depth first, then colour), which made a view over
// the whole city a third slower on an ordinary laptop's graphics (20.6 ms a frame against 15.6).
const CITY_OPACITY = 1

// ---------------------------------------------------------------------------------------------
// What React shows: layers with their legends, the 3D city, the registers and the time-lapse.

export type LayerSummary = {
  ref: string; title: string; count: number; style: string
  renderer: string | null // the feature drawing it its own way, which may give its own legend
  legend: Record<string, string>; stats: Stats | null; valueField: string | null
  street: { name: string; follows: string | null } | null // `follows`: the total its glow tracks
  sources: string[] // where its data comes from (lib/sources.ts)
}
// What a time-lapse layer reads at the clock's moment, e.g. "2,716 people" (null outside it).
export type ReplayUi = { ref: string; title: string; frame: number | null; frames: number; reading: string | null }
type MapUi = {
  ready: boolean
  basemap: Basemap // the base map shown
  basemapMode: "auto" | Basemap // auto follows the sun
  baseProblem: boolean // the base map's tiles don't come, from either address
  skyNow: Sky // the real sky right now, for the strip and the map panel
  nightLights: boolean // street lights glow after dark
  layers: LayerSummary[]
  city: { on: boolean; status: string }
  registers: { on: boolean; status: string }
  replays: Record<string, ReplayUi> // every time-lapse layer on the map, by ref
  me: { loc: MyLocation | null; error: string }
  picking: boolean // the next map click is the user's position ("Pick my spot on the map")
  flying: string | null // the route the camera is flying along
  // The right-click menu, where it was asked for (page pixels) and the point of the map there.
  menu: { x: number; y: number; at: [number, number]; n: number } | null
  terrain: boolean // the ground is raised in 3D (lib/map/terrain.ts)
}
export type MyLocation = { lat: number; lon: number; accuracy_m: number | null; source: string }
export const useMapUi = create<MapUi>(() => ({
  ready: false,
  basemap: "night",
  basemapMode: "auto",
  baseProblem: false,
  skyNow: skyAt(new Date()),
  nightLights: true,
  layers: [],
  city: { on: false, status: "" },
  registers: { on: false, status: "" },
  replays: {},
  me: { loc: null, error: "" },
  picking: false,
  flying: null,
  menu: null,
  terrain: false,
}))
const setUi = useMapUi.setState

export function cityHeights() {
  return useMapUi.getState().basemap === "night" ? CITY_HEIGHTS_NIGHT : CITY_HEIGHTS_DAY
}

// The grey of OpenStreetMap's buildings where the DLS has none, for the legend.
export function cityOsmGrey() {
  return useMapUi.getState().basemap === "night" ? OSM_HEIGHTS_NIGHT[3] : OSM_HEIGHTS_DAY[3]
}

// ---------------------------------------------------------------------------------------------
// State shared by the map, linked charts and custom visuals.

export type ChartLink = { key: string; layer?: string }
type LinkedChart = { view: any; link: ChartLink }
type TimeChart = { view: any; min: number; max: number; last: number | null }
export type Visual = {
  iframe: HTMLIFrameElement; title: string
  onSize: (h: number) => void; onError: (text: string) => void
}

const state = {
  layers: new Map<string, Entry>(),
  callouts: [] as maplibregl.Marker[],
  pulses: new Set<string>(),
  dashLayers: new Set<string>(),
  hl3d: new Set<string>(),
  charts: [] as LinkedChart[],
  timeCharts: [] as TimeChart[],
  visuals: [] as Visual[],
  visualErrors: [] as string[],
  flownAt: 0,
}
const CITY = {
  on: false, features: [] as Feature[], ids: new Set<string | number>(), tiles: new Set<string>(),
  inflight: new Set<string>(), queue: [] as [number, number][],
  detail: new Set<string>(), // who needs the detailed buildings near the eye (wantCityDetail)
  fill: null as CityFill | null, // how far the server has read greater Nicosia
}
type CityFill = { fill: { done: number; total: number }; waiting: number; problem: string | null }

let map = undefined as unknown as MlMap // set once by initMap
let styleReady = false

export function getMap() {
  return map
}

function whenStyleReady(fn: () => void) {
  if (styleReady) fn()
  else map.once("style.load", fn)
}

// For features that keep their own layers: run once the style is there.
export function onStyleReady(fn: () => void) {
  if (map) whenStyleReady(fn)
}

export function escapeHtml(s: unknown) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!)
}

export function nicosiaTime(d: Date) {
  return d.toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })
}

export function fmt(v: number) {
  return Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : (Math.round(v * 10) / 10).toString()
}

// ---------------------------------------------------------------------------------------------
// Map setup

export type Camera = { center: [number, number]; zoom: number; pitch: number; bearing: number }

const DOUBLE_CLICK_MS = 250 // an empty-map click waits this long for a second click
const LONG_PRESS_MS = 550
const emptyClick = { timer: 0, quietUntil: 0 }
let press: { x: number; y: number; timer: number } | null = null

function cancelPress() {
  if (press) clearTimeout(press.timer)
  press = null
}

export function initMap(container: HTMLElement, camera?: Camera | null) {
  if (map) return map // React's development mode runs effects twice
  // No style yet: setBasemap loads the first one, recoloured. (Starting from a placeholder style
  // made that first setStyle try to diff against a style still loading, and stall.)
  map = new maplibregl.Map({
    container,
    ...(camera ?? HOME),
    maxPitch: 75,
    attributionControl: false, // lib/map/credits.ts adds it, with the sources of what is drawn
  })
  // A click opens what is under it in the inspector. The box around the pointer catches the
  // small dots (a lamp is a few pixels across); what lies right under the pointer wins. A click
  // on empty map goes to the empty-click handlers ("What's here?"), a moment later, so the
  // first click of a double-click (which zooms) does nothing. A drag is never a click.
  map.on("click", (e) => {
    if (pickDone) { endPick({ lat: e.lngLat.lat, lon: e.lngLat.lng }); return } // the user's spot
    if (viewFocus) return // in a view, a click is the end of a look about, not a pick
    const layers = map.getLayersOrder().filter(clickableLayer)
    const { x, y } = e.point
    const exact = layers.length ? map.queryRenderedFeatures(e.point, { layers }) : []
    const near = exact.length || !layers.length ? exact : map.queryRenderedFeatures([[x - 6, y - 6], [x + 6, y + 6]], { layers })
    const target = resolveClick(near, [e.lngLat.lng, e.lngLat.lat])
    clearTimeout(emptyClick.timer)
    if (target) {
      emptyClick.timer = 0
      for (const fn of inspectSinks) fn(target)
      return
    }
    if (performance.now() < emptyClick.quietUntil) return // the click that ends a long press
    if (emptyClick.timer) { emptyClick.timer = 0; return } // a double-click: it zooms
    const at: [number, number] = [e.lngLat.lng, e.lngLat.lat]
    emptyClick.timer = window.setTimeout(() => { emptyClick.timer = 0; handleEmptyClick(at, "click") }, DOUBLE_CLICK_MS)
  })
  map.on("dblclick", () => { clearTimeout(emptyClick.timer); emptyClick.timer = 0 })
  // A long press on touch asks about the point, as a right-click does with a mouse. Moving the
  // finger (a pan) or a second finger (a pinch) calls it off.
  map.on("touchstart", (e) => {
    cancelPress()
    if (e.originalEvent.touches.length !== 1) return
    const at: [number, number] = [e.lngLat.lng, e.lngLat.lat]
    press = {
      x: e.point.x, y: e.point.y,
      timer: window.setTimeout(() => {
        press = null
        emptyClick.quietUntil = performance.now() + 800
        if (pickDone) endPick({ lat: at[1], lon: at[0] }) // picking the user's spot, as a tap does
        else if (!viewFocus) handleEmptyClick(at, "press") // in a view, as a right-click: quiet
      }, LONG_PRESS_MS),
    }
  })
  map.on("touchmove", (e) => { if (press && Math.hypot(e.point.x - press.x, e.point.y - press.y) > 10) cancelPress() })
  map.on("touchend", cancelPress)
  map.on("touchcancel", cancelPress)
  map.on("movestart", cancelPress)
  ;(window as any).__map = map // a hook for scripted demos and browser tests (e.g. map.project)
  ;(window as any).__twin = { state, clock, setCity3d, setBasemapMode } // and what the map draws, for the same
  // Zoom, the 3D view and the compass are the page's own (components/shell/map-controls.tsx);
  // the scale sits with the credits in the bottom-right corner.
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right")
  geolocate = new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true }, trackUserLocation: false, showUserLocation: false,
    fitBoundsOptions: { maxZoom: 16.5 },
  })
  map.addControl(geolocate, "bottom-right")
  geolocate.on("geolocate", (pos: GeolocationPosition) => setMyLocation(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, "device location"))
  geolocate.on("error", () => setUi({ me: { loc: useMapUi.getState().me.loc, error: "Couldn't read your location here. Right-click the map where you are instead." } }))
  // Right-click: the map's menu ("What's here?", "I'm here"), drawn by components/map-menu.
  // A touch browser that turns a long press into a context menu gets the long press instead.
  map.on("contextmenu", (e) => {
    e.preventDefault()
    if (viewFocus) return // in a view, the map is for looking about
    const touch = (e.originalEvent as PointerEvent).pointerType === "touch" || performance.now() < emptyClick.quietUntil
    if (touch) {
      if (press) { // the browser was quicker than our timer
        cancelPress()
        emptyClick.quietUntil = performance.now() + 800
        handleEmptyClick([e.lngLat.lng, e.lngLat.lat], "press")
      }
      return
    }
    openMapMenu(e.lngLat, e.point)
  })
  // A page opened in a hidden tab measures a 0-size container; keep the canvas in step.
  new ResizeObserver(() => map.resize()).observe(container)

  map.on("style.load", () => {
    styleReady = true
    addWalls()
    addAmbientLamps()
    addCityLayer() // the 3D buildings, whether coloured by height or not
    for (const entry of state.layers.values()) addToMap(entry)
    weatherHooks.place()
    syncHighlight()
    syncSelection()
    applySky()
    if (baseWanted !== baseShown) swapBasemap() // the sky moved on while the style loaded
  })
  map.on("error", retryBase)
  map.on("sourcedata", baseTileCame)
  map.on("load", () => setUi({ ready: true }))
  map.on("movestart", (e) => { if ((e as any).originalEvent) framed = null }) // the user took over
  map.on("moveend", refreshCity)
  map.on("zoomend", cityStatus)
  showSky(clock.at === null ? new Date() : new Date(clock.at))
  requestAnimationFrame(animate)
  setInterval(() => { if (clock.at === null && hoverMs === null) setTimeAll(nicosiaNowMs()) }, 30e3)
  // The sky moves on: every minute the light, the sky and (at dusk and dawn) the base map follow.
  setInterval(() => {
    setUi({ skyNow: skyAt(new Date()) })
    if (clock.at === null) showSky(new Date())
  }, 60e3)
  // A comparison is divided where the swipe is on screen: the camera moving moves the line
  // through the city.
  map.on("move", () => { if (clock.compare !== null) splitSoon() })
  window.addEventListener("message", onVisualMessage)
  return map
}

// The base map. The first one loads as a style; after that, Night and Day change places by their
// layers alone. The tiles, the fonts and everything drawn on top stay as they are, so a
// time-lapse can cross dawn and dusk as often as it likes without the map ever going blank, and
// with no request that could fail and leave it so.

let baseShown: Basemap | null = null // the one on the map
let baseWanted: Basemap | null = null // the one the sky (or the switch in the map panel) asks for
const baseStyles: Partial<Record<Basemap, Promise<LayerSpecification[]>>> = {}

// Each showing of a base map names its layers anew ("night.3/water"). The layers of both can then
// be on the map while one takes over from the other, and a name never comes back: MapLibre keeps
// a removed layer's label buffers with the tiles, and takes them up again, half-made, for a new
// layer of the same name.
let basePrefix = "" // of the one on the map
let baseShowings = 0
const newPrefix = (b: Basemap) => `${b}.${++baseShowings}/`
const named = (layers: LayerSpecification[], prefix: string) => layers.map((l) => ({ ...l, id: prefix + l.id }))
const isBase = (id: string) => /^(night|day)\.\d+\//.test(id)

function firstBasemap(b: Basemap) {
  baseShown = b
  setUi({ basemap: b })
  map.setStyle(BASEMAP_URLS[b], {
    diff: false,
    transformStyle: (_prev, next) => {
      const layers = baseLayers(b, next)
      baseStyles[b] = Promise.resolve(layers)
      basePrefix = newPrefix(b)
      const tiles = { type: "vector" as const, url: BASE_SOURCE_URLS[baseUrl] }
      return { ...next, sources: { ...next.sources, [BASE_SOURCE]: tiles }, layers: named(layers, basePrefix) }
    },
  })
  // Have the other one at hand before it is needed.
  map.once("idle", () => { baseLayersOf(b === "night" ? "day" : "night").catch(() => {}) })
}

function baseLayersOf(b: Basemap) {
  return (baseStyles[b] ??= fetch(BASEMAP_URLS[b])
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((style) => baseLayers(b, style))
    .catch((e) => { delete baseStyles[b]; throw e }))
}

// Wait for the clock to settle, so scrubbing back and forth over dawn doesn't change the map at
// every pass.
const BASE_SETTLE_MS = 150
let baseTimer: number | undefined
function wantBasemap(b: Basemap) {
  if (b === baseWanted) return
  baseWanted = b
  if (!baseShown) { firstBasemap(b); return }
  clearTimeout(baseTimer)
  baseTimer = window.setTimeout(swapBasemap, BASE_SETTLE_MS)
}

let baseSwap = 0
async function swapBasemap() {
  const swap = ++baseSwap // a newer swap takes over from one still waiting
  const to = baseWanted
  if (!styleReady || !to) return // style.load comes back here
  for (const id of map.getLayersOrder()) if (isBase(id) && !id.startsWith(basePrefix)) map.removeLayer(id) // what an unfinished swap left
  if (to === baseShown) return
  let layers: LayerSpecification[]
  try {
    layers = await baseLayersOf(to)
  } catch { // keep the base map there is, and try again
    if (swap === baseSwap) baseTimer = window.setTimeout(swapBasemap, 5000)
    return
  }
  await baseTilesReady() // with an unfinished swap's layers, so they aren't taken for this one's
  if (swap !== baseSwap) return
  // The new layers go in under the opaque ground of the base map on show, which stays until the
  // tiles are ready to draw them.
  const ground = map.getLayersOrder()[0]
  const prefix = newPrefix(to)
  for (const l of named(layers, prefix)) {
    const hide = l.type === "fill-extrusion" // the city's tiles stand in for them
    map.addLayer((hide ? { ...l, layout: { ...l.layout, visibility: "none" } } : l) as LayerSpecification, ground)
  }
  await nextFrame() // MapLibre takes up the new layers, and starts on the tiles, with its next frame
  await baseTilesReady()
  if (swap !== baseSwap || to !== baseWanted) return // the next swap, due any moment, clears up
  for (const id of map.getLayersOrder()) if (id.startsWith(basePrefix)) map.removeLayer(id)
  baseShown = to
  basePrefix = prefix
  setUi({ basemap: to })
  placeOnBase()
  applyTheme()
  applySky()
  if (viewFocus) applyViewBuildings()
}

// Until every tile in view is prepared with the layers there are. Both waits end by themselves,
// for a page in the background (which draws no frames) or tiles that don't come.
async function baseTilesReady() {
  const until = performance.now() + 4000
  while (!map.isSourceLoaded(BASE_SOURCE) && performance.now() < until) await new Promise((r) => setTimeout(r, 30))
}

function nextFrame() {
  return new Promise<unknown>((done) => { map.once("render", done); setTimeout(done, 1000) })
}

// The walls run under the base map's labels, and light lies on the ground under its buildings.
function placeOnBase() {
  const order = map.getLayersOrder()
  const label = firstBaseLayer("symbol")
  if (label) for (const id of ["landmark-walls-glow", "landmark-walls"]) if (map.getLayer(id)) map.moveLayer(id, label)
  const buildings = glowBelow()
  if (buildings) for (const id of order) if (/(^|::)lamps-(pool|bulb)$|::street-glow$/.test(id)) map.moveLayer(id, buildings)
  weatherHooks.place()
}

function firstBaseLayer(type: string) {
  return map.getLayersOrder().find((id) => id.startsWith(basePrefix) && map.getLayer(id)?.type === type)
}

// The base map's first label layer: a feature's lines go under it, so street names stay on top.
export function firstLabelLayer() {
  return firstBaseLayer("symbol")
}

// Paint that differs between the two base maps: both values are kept while a layer is on the
// map, so a change of base map repaints it in place.
type Themed = Record<string, Record<Basemap, any>>
const themed = new Map<string, Themed>()
const STROKE: Record<Basemap, string> = { night: NIGHT, day: "#ffffff" }

export function themedPaint(id: string, paint: Themed): any {
  return forBase(id, paint)
}

function forBase(id: string, paint: Themed): any {
  themed.set(id, paint)
  const b = useMapUi.getState().basemap
  return Object.fromEntries(Object.entries(paint).map(([k, v]) => [k, v[b]]))
}

function applyTheme() {
  const b = useMapUi.getState().basemap
  for (const [id, paint] of themed) {
    if (!map.getLayer(id)) { themed.delete(id); continue }
    for (const [k, v] of Object.entries(paint)) map.setPaintProperty(id, k, v[b])
  }
}

// MapLibre doesn't try again when the style or the list of tiles fails to load, which would
// leave the map blank until the page is reloaded. Here the tiles' other address is tried at once,
// then both in turn; and when neither has brought a tile, the map says so (`baseProblem`).
let baseUrl = 0 // which of BASE_SOURCE_URLS is in use
let baseTries = 0 // failures since a tile last came
let tileFailures = 0 // of single tiles, at the address in use
let baseRetry: number | undefined

function retryBase(e: any) {
  console.error(e.error ?? e)
  const styleFailed = !styleReady
  const listFailed = e.sourceId === BASE_SOURCE && !e.tile
  // Tiles that never reach the server, one after the other, are an address that is blocked.
  const tileFailed = e.sourceId === BASE_SOURCE && e.tile && !e.error?.status
  if (tileFailed) tileFailures++
  if (!(styleFailed || listFailed || tileFailures >= 4) || baseRetry) return
  baseTries++
  if (baseTries >= BASE_SOURCE_URLS.length) setUi({ baseProblem: true })
  baseRetry = window.setTimeout(() => {
    baseRetry = undefined
    if (!styleReady && baseShown) {
      map.setStyle(null as any) // a style that failed would hold up the next one
      firstBasemap(baseShown)
    } else if (styleReady) {
      baseUrl = (baseUrl + 1) % BASE_SOURCE_URLS.length
      tileFailures = 0
      ;(map.getSource(BASE_SOURCE) as VectorTileSource | undefined)?.setUrl(BASE_SOURCE_URLS[baseUrl])
    }
  }, styleReady && baseTries === 1 ? 0 : 4000)
}

function baseTileCame(e: any) {
  if (e.sourceId !== BASE_SOURCE || e.tile?.state !== "loaded") return
  baseTries = tileFailures = 0
  if (useMapUi.getState().baseProblem) setUi({ baseProblem: false })
}

// Auto shows Night from civil dusk to civil dawn and Day otherwise; Night and Day stay put.
export function setBasemapMode(mode: "auto" | Basemap) {
  setUi({ basemapMode: mode })
  showSky(shown.at)
}

// ---------------------------------------------------------------------------------------------
// The sky: the map shows the real sun and moon over Nicosia, at the current moment or, while a
// time-lapse plays, at the moment of its frame.

let shown: Sky = skyAt(new Date())

function showSky(at: Date) {
  shown = skyAt(at)
  const mode = useMapUi.getState().basemapMode
  const want: Basemap = mode === "auto" ? (shown.dark ? "night" : "day") : mode
  applySky()
  wantBasemap(want)
}

function applySky() {
  if (!styleReady) return
  applyLight()
  syncViewLights()
  syncNightLights()
}

// The light on the 3D buildings and the sky of a tilted view, from the sun and moon, leaned
// towards the weather of the moment.
function applyLight() {
  if (!styleReady) return
  map.setLight(weatherHooks.light(lightFor(shown), shown))
  // Looking out at eye level, the haze gathers only near the horizon: the city a few km off
  // stays in sight (and its lights at night).
  const sky = viewFocus ? { ...skyFor(shown), "fog-ground-blend": 0.94, "horizon-fog-blend": shown.dark ? 0.25 : 0.4 } : skyFor(shown)
  try { map.setSky(weatherHooks.sky(sky, shown)) } catch { /* a style without sky support */ }
}

// The weather (lib/weather) leans the light and the sky towards the weather of the clock's
// moment, keeps its own layers where they belong (over the ground and the buildings, under the
// labels and everything drawn), and gives the lamps a halo in wet or misty air. It registers
// these; this module never imports it.
type LightSpec = ReturnType<typeof lightFor>
type SkySpec = ReturnType<typeof skyFor>
export const weatherHooks = {
  light: (l: LightSpec, sky: Sky): LightSpec => { void sky; return l },
  sky: (s: SkySpec, sky: Sky): SkySpec => { void sky; return s },
  place: (): void => {},
}

export function onWeather(h: Partial<typeof weatherHooks>) {
  Object.assign(weatherHooks, h)
  if (map && styleReady) { weatherHooks.place(); applyLight() }
}

// The sky the map shows (the clock's moment), and its light and sky again when the weather moves.
export function shownSky() {
  return shown
}

export function refreshSky() {
  applyLight()
}

// The lamps in the weather: their pools wider and stronger in wet or misty air, their bulbs
// softer, the wash along the night streets brighter on wet roads (lib/weather/look.ts lampsFor).
type LampAir = { halo: number; glow: number; streets: number; bloom: number }
let lampAir: LampAir = { halo: 1, glow: 1, streets: 1, bloom: 1 }
const POOL_RADIUS = [12, 9, 14, 20, 15, 28, 16, 36, 17, 44, 18, 54, 19, 64]
const POOL_INTENSITY = [12, 1.4, 15, 1.1, 16, 1, 17, 0.9, 18, 0.8]
const BULB_BLUR = [13, 0.4, 17, 0.15]
const byZoom = (stops: number[], f: number) => ["interpolate", ["linear"], ["zoom"], ...stops.map((v, i) => (i % 2 ? v * f : v))]

export function setLampWeather(l: LampAir) {
  const same = (Object.keys(l) as (keyof LampAir)[]).every((k) => Math.abs(l[k] - lampAir[k]) < 0.01)
  if (same) return
  lampAir = l
  if (!styleReady) return
  for (const id of nightLayers) {
    if (!map.getLayer(id)) continue
    if (id.endsWith("-pool")) {
      map.setPaintProperty(id, "heatmap-radius", byZoom(POOL_RADIUS, l.halo))
      map.setPaintProperty(id, "heatmap-intensity", byZoom(POOL_INTENSITY, l.glow))
    } else if (id.endsWith("-bulb")) {
      map.setPaintProperty(id, "circle-blur", byZoom(BULB_BLUR, l.bloom))
    }
  }
  const glow = `${basePrefix}street-glow`
  if (map.getLayer(glow)) map.setPaintProperty(glow, "line-opacity", byZoom(STREET_GLOW_OPACITY, l.streets))
}

export function setNightLights(on: boolean) {
  setUi({ nightLights: on })
  syncNightLights()
  if (styleReady) syncViewLights()
}

// Street lights glow from dusk to dawn: the city's own lamps (live from /api/lamps, or as they
// were at the clock's moment), any street light layer the assistant draws (following the clock),
// and a faint warm wash along the streets of the Night base map.
const nightLayers = new Set<string>()
let lamps: FC | null = null // live
let lampsAt = 0

function lampLayerOnMap() {
  for (const e of state.layers.values()) if (e.lamps) return true
  return false
}

function syncNightLights() {
  if (!styleReady) return
  const lightsOn = useMapUi.getState().nightLights
  // In a comparison the other moment may be the dark one.
  const on = lightsOn && (shown.dark || (clock.compare !== null && skyAt(new Date(clock.compare)).dark))
  if (on && clock.at === null && Date.now() - lampsAt > 5 * 60e3) loadLamps()
  const ambient = on && !lampLayerOnMap()
  for (const id of [...nightLayers, `${basePrefix}street-glow`]) {
    if (!map.getLayer(id)) continue
    const visible = id.startsWith("lamps-") ? ambient : on
    map.setLayoutProperty(id, "visibility", visible ? "visible" : "none")
  }
}

function loadLamps() {
  lampsAt = Date.now()
  fetch("/api/lamps")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((fc: FC) => {
      lamps = fc
      if (map.getSource("lamps")) pushLamps()
      else addAmbientLamps()
    })
    .catch(() => { lampsAt = 0 }) // try again on the next minute
}

// The ambient lamps at the clock's moment (and, in a comparison, the other moment's left of the
// swipe). A moment not read yet keeps what is on show until it comes.
const lampsShown: { a: FC | null; b: FC | null } = { a: null, b: null }
const NO_LAMPS: FC = { type: "FeatureCollection", features: [] }
function lampsFor(t: number | null): FC | null {
  return t === null ? lamps : clock.lamps.get(t) ?? null
}

// The lamps of a moment not read yet: none glow in daylight; after dark, what is on show stays
// until the moment's own come.
function lampsOr(t: number | null, shown: FC | null) {
  const got = lampsFor(t)
  if (got) return got
  return t !== null && !skyAt(new Date(t)).dark ? NO_LAMPS : shown
}

function pushLamps() {
  const src = map.getSource("lamps") as GeoJSONSource | undefined
  const a = lampsOr(clock.at, lampsShown.a)
  const b = clock.compare !== null ? lampsOr(clock.compare, lampsShown.b) : null
  lampsShown.a = a
  lampsShown.b = b
  if (!src || !a) return
  const features = b ? split(a.features, b.features) : a.features
  src.setData({ type: "FeatureCollection", features } as any)
}

function addAmbientLamps() {
  const first = lampsFor(clock.at) ?? lamps
  if (!first || map.getSource("lamps")) return
  map.addSource("lamps", { type: "geojson", data: first as any })
  lampsShown.a = first
  const ids: string[] = []
  addLampGlow("lamps", "lamps", ["==", ["get", "state"], "on"], ids, glowBelow())
  bindPopup("lamps-bulb") // a lamp's bulb opens it in the inspector
  syncNightLights()
}

// Light pools under the 3D buildings (the DLS city, or either base map's own), so it shows in
// the streets between them.
function glowBelow() {
  return map.getLayer("city3d::fill") ? "city3d::fill" : firstBaseLayer("fill-extrusion")
}

function addLampGlow(prefix: string, src: string, lit: any, ids: string[], before?: string) {
  const points: any = ["all", ["==", ["geometry-type"], "Point"], lit]
  const visibility = shown.dark && useMapUi.getState().nightLights ? "visible" : "none"
  map.addLayer({
    id: `${prefix}-pool`, type: "heatmap", source: src, filter: points, layout: { visibility },
    paint: {
      "heatmap-weight": 1,
      // Zoomed out, the lamps of a street merge into one warm line; zoomed in, each lamp keeps
      // its own soft pool (radius in px grows far slower than the map, intensity and opacity
      // fall), so the street, the routes and the labels stay readable underneath.
      "heatmap-intensity": byZoom(POOL_INTENSITY, lampAir.glow) as any,
      "heatmap-radius": byZoom(POOL_RADIUS, lampAir.halo) as any,
      "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"],
        0, "rgba(255,160,60,0)", 0.12, "rgba(255,150,55,0.12)", 0.3, "rgba(255,165,70,0.26)",
        0.55, "rgba(255,185,100,0.38)", 0.8, "rgba(255,205,140,0.48)", 1, "rgba(255,222,175,0.56)"],
      "heatmap-opacity": ["interpolate", ["linear"], ["zoom"], 12, 0.95, 15, 0.9, 17, 0.85, 18, 0.8],
    },
  }, before)
  map.addLayer({
    id: `${prefix}-bulb`, type: "circle", source: src, filter: points, minzoom: 13, layout: { visibility },
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 1, 16, 2.2, 18, 3.2, 19, 4],
      "circle-color": "#fff3d6",
      "circle-blur": byZoom(BULB_BLUR, lampAir.bloom) as any,
    },
  }, before)
  for (const id of [`${prefix}-pool`, `${prefix}-bulb`]) { ids.push(id); nightLayers.add(id) }
}

// The last fit to a layer stays in force until the user moves the map, the assistant flies it
// elsewhere or the layer goes. So when a panel opens over the map (the time-lapse clock as the
// layer lands, the stage with its chart a moment later) the layer is framed again in what is
// left, instead of the fit being cut short or the layer ending up behind the panel.
let framed: { ref: string; fit: () => void } | null = null

// The space the floating panels cover, so the camera centres on the open part of the map.
export function setInsets(padding: { top: number; right: number; bottom: number; left: number }) {
  if (!map) return
  const again = framed
  map.setPadding(padding) // stops any camera move in flight
  again?.fit()
}

export function resetView() {
  takeOver()
  framed = null
  map.flyTo({ ...HOME, essential: true })
}

// "Where am I": the user's position, from the device (the locate button) or set by
// right-clicking the map. It goes to the assistant with each message, only once shared.

let geolocate: maplibregl.GeolocateControl
let meMarker: maplibregl.Marker | null = null

// What a right click on the map offers: "What's here?" (lib/here.ts), "I'm here", and what
// features add. components/map-menu draws it as the page's menu at the point (arrows move,
// Enter runs one, Esc closes it and gives the keyboard back to the map). A long press on touch
// asks "What's here?" straight away (see initMap), and the card it opens has "I'm here" too.
export type MapMenuItem = {
  id: string; label: string; order: number; run: (at: { lng: number; lat: number }) => void
  icon?: LucideIcon
  opens?: boolean // opens something that takes the keyboard (the inspector): focus goes there
}
const menuItems: MapMenuItem[] = [
  { id: "me", label: "I'm here", order: 90, icon: LocateFixedIcon, run: (at) => setMyLocation(at.lat, at.lng, NaN, "set on the map") },
]
export function registerMapMenuItem(item: MapMenuItem) {
  const i = menuItems.findIndex((x) => x.id === item.id)
  if (i >= 0) menuItems.splice(i, 1)
  menuItems.push(item)
  menuItems.sort((a, b) => a.order - b.order)
}

export function mapMenuItems() {
  return [...menuItems]
}

// Open the menu at a point of the map (`point`: where on the map, in pixels, if known).
export function openMapMenu(at: { lng: number; lat: number }, point?: { x: number; y: number }) {
  const p = point ?? map.project([at.lng, at.lat])
  const box = map.getContainer().getBoundingClientRect()
  setUi({ menu: { x: box.left + p.x, y: box.top + p.y, at: [at.lng, at.lat], n: performance.now() } })
}

export function setMyLocation(lat: number, lon: number, accuracy: number, source: string) {
  const loc = { lat, lon, accuracy_m: Number.isFinite(accuracy) ? Math.round(accuracy) : null, source }
  if (!meMarker) {
    const el = document.createElement("div")
    el.className = "me-dot"
    el.title = "You are here"
    meMarker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map)
  } else {
    meMarker.setLngLat([lon, lat])
  }
  setUi({ me: { loc, error: "" } })
}

export function forgetMyLocation() {
  meMarker?.remove()
  meMarker = null
  setUi({ me: { loc: null, error: "" } })
}

export function locateMe() {
  geolocate.trigger()
}

// Ask the browser for the device's position (the click that calls this is the user gesture it
// wants). Resolves "ok" once the position is set, or why it isn't.
export function requestMyLocation(timeoutMs = 15e3): Promise<"ok" | "denied" | "unavailable" | "timeout"> {
  if (!("geolocation" in navigator) || !geolocate) return Promise.resolve("unavailable")
  return new Promise((resolve) => {
    const finish = (r: "ok" | "denied" | "unavailable" | "timeout") => {
      window.clearTimeout(timer)
      geolocate.off("geolocate", ok)
      geolocate.off("error", bad)
      resolve(r)
    }
    const ok = () => finish("ok")
    const bad = (e?: GeolocationPositionError) => finish(e?.code === 1 ? "denied" : "unavailable")
    const timer = window.setTimeout(() => finish("timeout"), timeoutMs)
    geolocate.on("geolocate", ok)
    geolocate.on("error", bad)
    if (!geolocate.trigger()) finish("unavailable")
  })
}

// "Pick my spot on the map": the next click on the map sets the user's position, as "I'm here"
// does. useMapUi's `picking` shows the hint (components/chat/pick-hint.tsx), which also offers
// the map's centre (for the keyboard) and Cancel; Esc cancels. Resolves with the point or null.
let pickDone: ((p: { lat: number; lon: number } | null) => void) | null = null

export function pickMyLocation(): Promise<{ lat: number; lon: number } | null> {
  endPick(null) // one pick at a time
  map.getCanvasContainer().style.cursor = "crosshair"
  setUi({ picking: true })
  return new Promise((resolve) => { pickDone = resolve })
}

export function endPick(p: { lat: number; lon: number } | "centre" | null) {
  const done = pickDone
  if (!done) return
  pickDone = null
  map.getCanvasContainer().style.cursor = ""
  setUi({ picking: false })
  const c = map.getCenter()
  const at = p === "centre" ? { lat: c.lat, lon: c.lng } : p
  if (at) setMyLocation(at.lat, at.lon, NaN, "set on the map")
  done(at)
}

// The Venetian walls: a faint sodium ring that keeps the old town readable at any zoom.
function addWalls() {
  if (map.getSource("landmark-walls")) return
  map.addSource("landmark-walls", {
    type: "geojson",
    data: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: WALLS_RING } },
  })
  const before = firstBaseLayer("symbol")
  map.addLayer({
    id: "landmark-walls-glow", type: "line", source: "landmark-walls",
    paint: {
      "line-color": SODIUM, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 5, 17, 18], "line-blur": 4,
      ...forBase("landmark-walls-glow", { "line-opacity": { night: 0.07, day: 0.12 } }),
    },
  }, before)
  map.addLayer({
    id: "landmark-walls", type: "line", source: "landmark-walls",
    layout: { "line-join": "round" },
    paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.8, 17, 2.4],
      ...forBase("landmark-walls", { "line-color": { night: SODIUM, day: "#b8741a" }, "line-opacity": { night: 0.42, day: 0.6 } }),
    },
  }, before)
}

// ---------------------------------------------------------------------------------------------
// Layers the assistant draws

function numericStats(features: Feature[], field: string): Stats | null {
  const vals = features.map((f) => Number(f.properties?.[field])).filter(Number.isFinite)
  if (!vals.length) return null
  return { min: Math.min(...vals), max: Math.max(...vals) }
}

function hexToRgb(h: string) {
  return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16))
}

function rampColor(t: number) {
  const x = Math.max(0, Math.min(1, t)) * (RAMP.length - 1)
  const i = Math.min(RAMP.length - 2, Math.floor(x))
  const a = hexToRgb(RAMP[i]), b = hexToRgb(RAMP[i + 1]), f = x - i
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",")})`
}

function squareAround([lon, lat]: number[], meters: number) {
  const dLat = meters / 111320
  const dLon = meters / (111320 * Math.cos((lat * Math.PI) / 180))
  return [[[lon - dLon, lat - dLat], [lon + dLon, lat - dLat], [lon + dLon, lat + dLat], [lon - dLon, lat + dLat], [lon - dLon, lat - dLat]]]
}

// A click on any of these opens the inspector (see the click handler in initMap); here they
// get a pointer and link their hover to the charts.
const boundPopups = new Set<string>() // map.on handlers outlive style changes, so bind once per id
function bindPopup(layerId: string) {
  if (boundPopups.has(layerId)) return
  boundPopups.add(layerId)
  map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; linkHover("id", null, "map") })
  map.on("mousemove", layerId, (e) => linkHover("id", e.features?.[0]?.properties?.id ?? null, "map"))
}

function addLayerSafe(def: LayerSpecification, ids: string[]) {
  map.addLayer(def)
  ids.push(def.id)
}

function addToMap(entry: Entry) {
  const { spec, data } = entry
  const ref = spec.ref
  const src = `${ref}::src`
  entry.ids = []
  if (map.getSource(src)) removeFromMap(ref)
  const renderer = renderers.find((r) => r.claims(data))
  if (renderer) {
    entry.renderer = renderer
    map.addSource(src, { type: "geojson", data: data as any })
    renderer.add(ref, src, data, entry.ids)
    return
  }
  const points: any = ["==", ["geometry-type"], "Point"]
  const vf = spec.value_field ?? undefined
  // A time-lapse keeps one scale across all frames, so heights and heat compare over time.
  entry.stats = vf ? (data.frames ? frameStats(data.frames, vf) : numericStats(data.features, vf)) : null
  addStreet(entry) // first, so the layer's own features draw over it

  if (spec.style === "columns_3d") {
    map.addSource(src, { type: "geojson", data: columnsData(entry, shownFeatures(entry)) as any })
    addLayerSafe({
      id: `${ref}::columns`, type: "fill-extrusion", source: src,
      paint: { "fill-extrusion-color": ["get", "_c"], "fill-extrusion-height": ["get", "_h"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.9 },
    }, entry.ids)
    bindPopup(`${ref}::columns`)
    if (spec.labels) addColumnReadings(entry, src, vf!)
    if (spec.fit === false && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 })
    return
  }

  map.addSource(src, { type: "geojson", data: { type: "FeatureCollection", features: shownFeatures(entry) } as any })

  if (spec.style === "buildings_3d") {
    // Real footprints at their DLS heights, plot outlines on the ground, and markers that
    // carry the layer when zoomed out (where the buildings are too small to see).
    addExtrusions(entry, src, 0)
    addLayerSafe({
      id: `${ref}::circle`, type: "circle", source: src, filter: points,
      paint: {
        "circle-color": ["get", "color"],
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.2, 12, 3.6, 15, 5, 17, 3.5],
        ...forBase(`${ref}::circle`, { "circle-stroke-color": STROKE }), "circle-stroke-width": 1.1,
        "circle-opacity": ["interpolate", ["linear"], ["zoom"], 15.5, 0.95, 17.5, 0.25],
        "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 15.5, 1, 17.5, 0.25],
      },
    }, entry.ids)
    bindPopup(`${ref}::circle`)
    return
  }

  if (spec.style === "heatmap") {
    const st = entry.stats
    const weight: any = st && st.max > st.min
      ? ["interpolate", ["linear"], ["to-number", ["get", vf], st.min], st.min, 0.05, st.max, 1]
      : 1
    // A heatmap reads the city from above; at street level (where a later answer may fly the
    // camera) it would smear over the 3D buildings, so it hands over to crisp status dots
    // between zoom 14.5 and 16.
    addLayerSafe({
      id: `${ref}::heat`, type: "heatmap", source: src, filter: points, maxzoom: 16,
      paint: {
        "heatmap-weight": weight,
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 11, 0.9, 16, 1.8],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 11, 18, 15, 38, 18, 70],
        "heatmap-opacity": ["interpolate", ["linear"], ["zoom"], 14.5, 0.85, 16, 0],
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(37,99,235,0)", 0.15, "#3b82f6", 0.35, "#22c55e", 0.55, "#eab308", 0.75, "#f97316", 1, "#dc2626"],
      },
    }, entry.ids)
    const dotsIn: any = ["interpolate", ["linear"], ["zoom"], 14.5, 0, 16, 0.95]
    addLayerSafe({
      id: `${ref}::dots`, type: "circle", source: src, filter: points, minzoom: 14.5,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 14.5, 2.5, 16, 3.5, 18, 5],
        "circle-color": ["get", "color"], "circle-opacity": dotsIn,
        ...forBase(`${ref}::dots`, { "circle-stroke-color": STROKE }), "circle-stroke-width": 1, "circle-stroke-opacity": dotsIn,
      },
    }, entry.ids)
    bindPopup(`${ref}::dots`)
    return
  }

  if (spec.style === "area") {
    // Building layers draw their plots here, not the 3D footprints.
    const polys: any = ["all", ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
      ["!=", ["coalesce", ["get", "_kind"], ""], "footprint"]]
    addLayerSafe({ id: `${ref}::fill`, type: "fill", source: src, filter: polys, paint: { "fill-color": ["get", "color"], "fill-opacity": 0.18 } }, entry.ids)
    addLayerSafe({ id: `${ref}::outline`, type: "line", source: src, filter: polys, paint: { "line-color": ["get", "color"], "line-width": 2.2 } }, entry.ids)
  }

  if (spec.style === "route") {
    const lines: any = ["==", ["geometry-type"], "LineString"]
    // A route avoiding dangerous buildings comes with the usual route, and a bus trip with its
    // walks: thinner, grey, still.
    const alt: any = ["match", ["get", "state"], ["usual route", "walk"], true, false]
    addLayerSafe({ id: `${ref}::casing`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { ...forBase(`${ref}::casing`, { "line-color": STROKE }), "line-width": ["case", alt, 6.5, 10], "line-opacity": 0.85 } }, entry.ids)
    addLayerSafe({ id: `${ref}::line`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": ["get", "color"], "line-width": ["case", alt, 3.5, 5.5] } }, entry.ids)
    addLayerSafe({ id: `${ref}::dash`, type: "line", source: src, filter: ["all", lines, ["!", alt]], layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#ffffff", "line-width": 2.5, "line-dasharray": [0, 4, 3] } }, entry.ids)
    state.dashLayers.add(`${ref}::dash`)
    // Stops are numbered; register buildings along the way are smaller red "!" markers.
    addLayerSafe({ id: `${ref}::stops`, type: "circle", source: src, filter: points, paint: { "circle-radius": ["case", ["==", ["to-string", ["get", "n"]], "!"], 8.5, 11], "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 } }, entry.ids)
    addLayerSafe({ id: `${ref}::num`, type: "symbol", source: src, filter: points, layout: { "text-field": ["to-string", ["get", "n"]], "text-font": FONT_BOLD, "text-size": 12, "text-allow-overlap": true }, paint: { "text-color": "#fff" } }, entry.ids)
    bindPopup(`${ref}::stops`)
    if (spec.labels !== false) addLabels(entry, src, points, 1.6)
    return
  }

  // A building layer shown as markers still stands up in 3D once zoomed in to street level.
  if (data.features.some((f) => f.properties?._kind === "footprint")) addExtrusions(entry, src, 15)

  // Street lights glow after dark (the lit ones), and follow a time-lapse frame by frame.
  const legend = spec.legend || {}
  entry.lamps = data.features.some((f) => f.properties?.kind === "street_light") || ("on" in legend && "offline" in legend)
  if (entry.lamps) addLampGlow(`${ref}::lamps`, src, ["==", ["get", "state"], "on"], entry.ids, glowBelow())

  if (spec.style === "pulse") {
    const id = `${ref}::halo`
    addLayerSafe({ id, type: "circle", source: src, filter: points, paint: { "circle-radius": 10, "circle-color": ["get", "color"], "circle-opacity": 0.4, "circle-blur": 0.4 } }, entry.ids)
    state.pulses.add(id)
  }
  const pulse = spec.style === "pulse"
  addLayerSafe({
    id: `${ref}::circle`, type: "circle", source: src, filter: points,
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, pulse ? 5 : 3, 14, pulse ? 8 : 5.5, 17, pulse ? 12 : 9],
      ...forBase(`${ref}::circle`, { "circle-stroke-color": STROKE }), "circle-stroke-width": 1.6, "circle-opacity": 0.95,
    },
  }, entry.ids)
  bindPopup(`${ref}::circle`)
  if (spec.labels) addLabels(entry, src, points, 1.1)
}

// The 3D part of a building layer: plot outlines, footprints extruded at their DLS heights, and
// a highlight extrusion for the hovered building. From `minzoom` (0 for 'buildings_3d').
function addExtrusions(entry: Entry, src: string, minzoom: number) {
  const ref = entry.spec.ref
  entry.extruded = true
  addLayerSafe({
    id: `${ref}::plots`, type: "line", source: src, minzoom, filter: ["==", ["get", "_kind"], "parcel"],
    paint: { "line-color": ["get", "color"], "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.5, 18, 2.4], "line-opacity": 0.9 },
  }, entry.ids)
  addLayerSafe({
    id: `${ref}::extrude`, type: "fill-extrusion", source: src, minzoom, filter: ["==", ["get", "_kind"], "footprint"],
    paint: { "fill-extrusion-color": ["get", "color"], "fill-extrusion-height": ["get", "height_m"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.93 },
  }, entry.ids)
  addLayerSafe({
    id: `${ref}::hl3d`, type: "fill-extrusion", source: src, minzoom, filter: hl3dFilter(hlValue),
    paint: { "fill-extrusion-color": SODIUM, "fill-extrusion-height": ["+", ["get", "height_m"], 0.6], "fill-extrusion-opacity": 0.96 },
  }, entry.ids)
  state.hl3d.add(`${ref}::hl3d`)
  bindPopup(`${ref}::extrude`)
  syncCityClaims()
}

function frameStats(frames: Frames, field: string): Stats | null {
  const vals = (frames.values[field] || []).flat().filter((v): v is number => Number.isFinite(v))
  return vals.length ? { min: Math.min(...vals), max: Math.max(...vals) } : null
}

function columnsData(entry: Entry, features: Feature[]) {
  const vf = entry.spec.value_field!
  const st = entry.stats || { min: 0, max: 1 }
  const span = st.max - st.min || 1
  const all = features.filter((f) => f.geometry?.type === "Point")
  // Column width follows the layer's extent: 6 m in a street, up to 45 m city-wide. So does the
  // tallest column: about a fifth of the extent, so one street's columns stay in the picture.
  const extent = extentMeters(entry.data.features.filter((f) => f.geometry?.type === "Point"))
  entry.half ??= Math.max(6, Math.min(45, extent / 220))
  const half = entry.half
  const tallest = Math.min(half * 40, Math.max(50, extent * 0.22))
  // A feature with no reading (a camera that doesn't count this, a silent sensor) has no column.
  const pts = all.filter((f) => f.properties[vf] != null && Number.isFinite(Number(f.properties[vf])))
  return {
    type: "FeatureCollection",
    features: pts.map((f) => {
      const t = (Number(f.properties[vf]) - st.min) / span
      return {
        type: "Feature",
        geometry: { type: "Polygon", coordinates: squareAround(f.geometry.coordinates, half) },
        properties: { ...f.properties, _h: half * 2 + t * tallest, _c: rampColor(t) },
      }
    }),
  }
}

// What each 3D column stands for, at its foot: the reading (it changes as a time-lapse plays),
// always shown, and under it the feature's name where there is room for it.
function addColumnReadings(entry: Entry, src: string, field: string) {
  const names = `${entry.spec.ref}::names`
  const readings = `${entry.spec.ref}::readings`
  addLayerSafe({
    id: names, type: "symbol", source: src,
    layout: {
      "text-field": ["get", "label"], "text-font": FONT_REGULAR, "text-size": 11, "text-anchor": "top",
      "text-offset": [0, 2], "text-max-width": 10, "text-line-height": 1.15,
    },
    paint: { "text-halo-width": 1.8, ...forBase(names, { "text-color": { night: "#c3cedb", day: "#1f2937" }, "text-halo-color": STROKE }) },
  }, entry.ids)
  addLayerSafe({
    id: readings, type: "symbol", source: src,
    layout: {
      "text-field": ["number-format", ["get", field], { "max-fraction-digits": 1 }], "text-font": FONT_BOLD,
      "text-size": 15, "text-anchor": "top", "text-offset": [0, 0.35], "text-allow-overlap": true, "text-ignore-placement": true,
    },
    paint: { "text-halo-width": 1.8, ...forBase(readings, { "text-color": { night: SODIUM, day: "#a85a00" }, "text-halo-color": STROKE }) },
  }, entry.ids)
}

// ---------------------------------------------------------------------------------------------
// The street a layer is about: a sodium line under the layer's features. In a time-lapse with
// totals it glows with each frame's share of the busiest one, so a quiet hour is a thin thread
// and the peak a wide band of light. The glow pools on the ground, under the 3D buildings; the
// line itself runs over them, so the street reads from any angle.

// The totals a time-lapse reads out: those of the value field on show, else its headline one.
function totalsOf(entry: Entry) {
  const fr = entry.data.frames
  const field = fr?.totals && [entry.spec.value_field, fr.total_field].find((f) => f && fr.totals![f])
  return field ? { field, values: fr!.totals![field] } : null
}

function streetLevel(entry: Entry, frame: number | null) {
  const t = totalsOf(entry)
  if (!t || frame === null) return 1
  const max = Math.max(...t.values)
  return max > 0 ? Math.max(0, Math.min(1, (t.values[frame] ?? 0) / max)) : 0
}

function streetGlow(level: number) {
  const s = 0.4 + 0.6 * level
  const c = 0.65 + 0.95 * level
  return {
    width: ["interpolate", ["exponential", 1.6], ["zoom"], 12, 6 * s, 15, 18 * s, 17, 44 * s, 19, 90 * s] as any,
    opacity: 0.3 + 0.4 * level,
    line: ["interpolate", ["linear"], ["zoom"], 12, 1.5 * c, 15, 2.8 * c, 17, 4.6 * c, 19, 7 * c] as any,
    lineOpacity: 0.72 + 0.28 * level,
  }
}

function addStreet(entry: Entry) {
  const st = entry.data.street
  if (!st || !(st.coordinates?.length >= 2)) return
  const ref = entry.spec.ref
  const src = `${ref}::street`
  const props = { id: "street", label: st.name, popup: `${st.name}\nThe street the layer's cameras stand along` }
  map.addSource(src, { type: "geojson", data: { type: "Feature", properties: props, geometry: { type: "LineString", coordinates: st.coordinates } } })
  const glow = streetGlow(streetLevel(entry, entry.frame ?? null))
  const layout = { "line-join": "round", "line-cap": "round" } as const
  map.addLayer({
    id: `${ref}::street-glow`, type: "line", source: src, layout,
    paint: {
      ...forBase(`${ref}::street-glow`, { "line-color": { night: SODIUM, day: "#f08c00" } }),
      "line-width": glow.width, "line-opacity": glow.opacity,
      "line-blur": ["interpolate", ["linear"], ["zoom"], 12, 3, 15, 7, 17, 14, 19, 26],
    },
  }, glowBelow())
  map.addLayer({
    id: `${ref}::street-line`, type: "line", source: src, layout,
    paint: {
      ...forBase(`${ref}::street-line`, { "line-color": { night: "#ffd9a3", day: "#b45f00" } }),
      "line-width": glow.line, "line-opacity": glow.lineOpacity,
    },
  })
  entry.ids.push(`${ref}::street-glow`, `${ref}::street-line`)
}

// The street follows the time-lapse (`frame` null: no time-lapse, full glow).
function syncStreet(entry: Entry, frame: number | null) {
  const ref = entry.spec.ref
  if (!styleReady || !map.getLayer(`${ref}::street-glow`)) return
  const glow = streetGlow(streetLevel(entry, frame))
  map.setPaintProperty(`${ref}::street-glow`, "line-width", glow.width)
  map.setPaintProperty(`${ref}::street-glow`, "line-opacity", glow.opacity)
  map.setPaintProperty(`${ref}::street-line`, "line-width", glow.line)
  map.setPaintProperty(`${ref}::street-line`, "line-opacity", glow.lineOpacity)
}

function addLabels(entry: Entry, src: string, filter: any, offset: number) {
  const id = `${entry.spec.ref}::labels`
  addLayerSafe({
    id, type: "symbol", source: src, filter,
    layout: { "text-field": ["get", "label"], "text-font": FONT_REGULAR, "text-size": 11.5, "text-offset": [0, offset], "text-anchor": "top", "text-max-width": 12 },
    paint: { "text-halo-width": 1.6, ...forBase(id, { "text-color": { night: "#e4ebf4", day: "#1f2937" }, "text-halo-color": STROKE }) },
  }, entry.ids)
}

function removeFromMap(ref: string) {
  if (flight?.ref === ref) stopFlight()
  if (framed?.ref === ref) framed = null
  const entry = state.layers.get(ref)
  entry?.renderer?.remove?.(ref)
  for (const id of entry?.ids || []) {
    if (map.getLayer(id)) map.removeLayer(id)
    state.pulses.delete(id)
    state.dashLayers.delete(id)
    state.hl3d.delete(id)
    nightLayers.delete(id)
  }
  for (const src of [`${ref}::src`, `${ref}::street`]) if (map.getSource(src)) map.removeSource(src)
}

function publishLayers() {
  setUi({
    layers: [...state.layers.values()].map((entry) => {
      const { spec, stats, data } = entry
      return {
        ref: spec.ref, title: spec.title, count: spec.count, style: spec.style,
        renderer: entry.renderer?.id ?? null,
        legend: entry.legendNow ?? spec.legend ?? {}, stats, valueField: spec.value_field ?? null,
        street: data.street ? { name: data.street.name, follows: totalsOf(entry)?.field ?? null } : null,
        sources: spec.sources ?? [],
      }
    }),
  })
  syncNightLights() // the city's own lamps make way for a street light layer
}

export function removeLayer(ref: string) {
  removeFromMap(ref)
  state.layers.delete(ref)
  clockSinks.gone(ref)
  publishReplays()
  if (ref === REGISTERS_REF) setUi({ registers: { on: false, status: "" } })
  syncCityClaims()
  publishLayers()
  syncHighlight()
}

export function clearMap() {
  const refs = [...state.layers.keys()]
  for (const ref of refs) removeFromMap(ref)
  state.layers.clear()
  setUi({ registers: { on: false, status: "" } })
  for (const ref of refs) clockSinks.gone(ref)
  publishReplays()
  clearCallouts()
  syncCityClaims()
  publishLayers()
  syncHighlight()
}

function coordsOf(geom: any, out: number[][]) {
  if (!geom) return out
  const walk = (c: any) => (typeof c[0] === "number" ? out.push(c) : c.forEach(walk))
  walk(geom.coordinates)
  return out
}

function extentMeters(features: Feature[]) {
  const pts: number[][] = []
  features.forEach((f) => coordsOf(f.geometry, pts))
  if (pts.length < 2) return 0
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1])
  const dLat = (Math.max(...lats) - Math.min(...lats)) * 111320
  const dLon = (Math.max(...lons) - Math.min(...lons)) * 111320 * Math.cos((lats[0] * Math.PI) / 180)
  return Math.hypot(dLat, dLon)
}

function fitTo(data: FC, pitch = map.getPitch()) {
  const pts: number[][] = []
  data.features.forEach((f) => coordsOf(f.geometry, pts))
  if (data.street) pts.push(...data.street.coordinates)
  if (!pts.length) return
  if (pts.length === 1) { map.flyTo({ center: pts[0] as [number, number], zoom: 16.5, pitch, essential: true }); return }
  const b = new maplibregl.LngLatBounds(pts[0] as [number, number], pts[0] as [number, number])
  pts.forEach((p) => b.extend(p as [number, number]))
  map.fitBounds(b, { padding: { top: 60, bottom: 60, left: 60, right: 60 }, maxZoom: 17, duration: 1400, pitch })
}

export async function showLayer(spec: LayerSpec, prefetched: FC | null = null) {
  let data = prefetched
  if (!data) {
    const res = await fetch(spec.url!)
    if (!res.ok) throw new Error(`layer ${spec.ref}: HTTP ${res.status}`)
    data = (await res.json()) as FC
  }
  if (state.layers.has(spec.ref)) removeFromMap(spec.ref)
  const entry = timedEntry(spec, data)
  state.layers.set(spec.ref, entry)
  const tilts = spec.style === "columns_3d" || spec.style === "buildings_3d"
  const pitch = tilts ? Math.max(55, map.getPitch()) : map.getPitch()
  const askedAt = spec.askedAt ?? performance.now()
  const fc = data
  whenStyleReady(() => {
    addToMap(entry)
    // A camera move asked for alongside this layer (tools run in parallel, so just before it)
    // or after it, while it loaded, wins over fitting to it.
    if (spec.fit !== false && !viewFocus && !(state.flownAt > askedAt - 3000)) {
      framed = { ref: spec.ref, fit: () => fitTo(fc, pitch) }
      framed.fit()
    } else if (spec.style === "buildings_3d" && map.getZoom() >= 15 && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 })
    publishLayers()
    syncHighlight()
    // A time-lapse becomes a range on the city clock, which plays it; a layer of devices is
    // read again at the clock's moment when that isn't now.
    if (fc.frames) clockSinks.range(spec.ref, spec.title, fc.frames.iso)
    else if (entry.kinds.length && (clock.at !== null || clock.compare !== null)) clockSinks.reload()
    publishReplays()
  })
}

// ---------------------------------------------------------------------------------------------
// The 3D buildings: every building in view, near and far, at its DLS height (from the Department
// of Lands and Surveys), and OpenStreetMap's where the DLS has none (north of the buffer zone).
// They are always there, in place of the base map's own 3D buildings (which drop out a couple of
// km away in a tilted view: the base map's tiles hold buildings from z13 only). "Heights and the
// 3D city" (`CITY.on`) colours them by height, OpenStreetMap's in grey; off, they look as the base
// map's did. Buildings that a 'buildings_3d' layer redraws are hidden here (no z-fighting); an
// old-town block that only holds a register plot turns into a translucent ghost, so the plot
// shows inside it.

function cityPaint(id: string, opacity: number): any {
  const byHeight = (heights: unknown[]) => ["interpolate", ["linear"], ["get", "h"], ...heights]
  const color = (dls: unknown[], osm: unknown[]) => ["case", ["==", ["get", "src"], "osm"], byHeight(osm), byHeight(dls)]
  const from = CITY.on ? CITY_DRAW_ZOOM : PLAIN_FROM_ZOOM
  return {
    ...forBase(id, {
      "fill-extrusion-color": CITY.on
        ? { night: color(CITY_HEIGHTS_NIGHT, OSM_HEIGHTS_NIGHT), day: color(CITY_HEIGHTS_DAY, OSM_HEIGHTS_DAY) }
        : { night: byHeight(PLAIN_HEIGHTS_NIGHT), day: PLAIN_DAY },
    }),
    "fill-extrusion-height": ["get", "h"], "fill-extrusion-base": ["coalesce", ["get", "b"], 0],
    "fill-extrusion-opacity": ["interpolate", ["linear"], ["zoom"], from, 0, from + 0.6, opacity],
  }
}

// Paint the buildings for "Heights and the 3D city" being on or off.
function paintCityLayers() {
  for (const [id, opacity] of [["city3d::fill", CITY_OPACITY], ["city3d::ghost", 0.28]] as const) {
    if (!map.getLayer(id)) continue
    for (const [k, v] of Object.entries(cityPaint(id, opacity))) map.setPaintProperty(id, k, v)
  }
}

function addCityLayer() {
  if (!map.getSource(CITY_SRC)) {
    const origin = window.location.origin
    map.addSource(CITY_SRC, { type: "vector", url: origin + CITY_TILES, tiles: [origin + CITY_TILE_URL] })
  }
  // Below any layer the assistant drew, so its buildings and markers stay on top (but over a
  // street's glow, which lies on the ground).
  const before = map.getStyle().layers.find((l) => l.id.includes("::") && !l.id.startsWith("city3d::") && !l.id.endsWith("::street-glow"))?.id
  const tiles = { source: CITY_SRC, "source-layer": CITY_LAYER, minzoom: CITY_DRAW_ZOOM } as const
  if (!map.getLayer("city3d::fill")) map.addLayer({ id: "city3d::fill", type: "fill-extrusion", ...tiles, paint: cityPaint("city3d::fill", CITY_OPACITY) }, before)
  if (!map.getLayer("city3d::ghost")) map.addLayer({ id: "city3d::ghost", type: "fill-extrusion", ...tiles, filter: idsIn([]), paint: cityPaint("city3d::ghost", 0.28) }, before)
  weatherHooks.place() // the 3D city stands under the weather, with the ground
  hideBaseBuildings()
  syncCityClaims()
  bindCityPopup()
}

// A filter for features with one of these ids: a `match`, which looks an id up at once, where an
// `in` over a list would compare every building with every id.
function idsIn(ids: Iterable<unknown>): any {
  const list = [...new Set([...ids].map(Number).filter(Number.isFinite))]
  return list.length ? ["match", ["id"], list, true, false] : ["boolean", false]
}

// The base map's own (OpenStreetMap) 3D buildings: the city's tiles stand in for them.
function hideBaseBuildings() {
  for (const l of map.getStyle()?.layers || []) {
    if (l.type === "fill-extrusion" && !l.id.includes("::")) map.setLayoutProperty(l.id, "visibility", "none")
  }
}

function syncCityClaims() {
  if (!map.getLayer("city3d::fill")) return
  const claimed = new Set<any>(), ghosts = new Set<any>()
  for (const { extruded, data } of state.layers.values()) {
    if (!extruded) continue
    for (const f of data.features) {
      if (f.properties?.bid !== undefined) claimed.add(f.properties.bid)
      if (f.properties?.ghost_bid !== undefined) ghosts.add(f.properties.ghost_bid)
    }
  }
  // While looking out from a building, the eye stands in it: it isn't drawn around the camera.
  if (viewFocus?.hide) claimed.add(viewFocus.hide)
  map.setFilter("city3d::fill", ["!", idsIn([...claimed, ...ghosts])])
  map.setFilter("city3d::ghost", idsIn([...ghosts].filter((g) => !claimed.has(g))))
}

// The detailed buildings near the eye: the DLS's z16 tiles as GeoJSON, every field and the full
// outline, for what is worked out from them on the page (shadows). They aren't drawn (the vector
// tiles draw the city), so they are read only while something asks for them (wantCityDetail).
function cityTilesInView() {
  if (!CITY.detail.size) return []
  const n = 2 ** 16
  const tx = (lon: number) => Math.floor(((lon + 180) / 360) * n)
  const ty = (lat: number) => Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n)
  if (viewFocus) return cityTilesNear(viewFocus, tx, ty)
  if (map.getZoom() < CITY_MIN_ZOOM) return []
  const b = map.getBounds(), c = map.getCenter()
  const cx = tx(c.lng), cy = ty(c.lat)
  const out: [number, number, number][] = []
  for (let x = Math.max(tx(b.getWest()), cx - CITY_RADIUS); x <= Math.min(tx(b.getEast()), cx + CITY_RADIUS); x++) {
    for (let y = Math.max(ty(b.getNorth()), cy - CITY_RADIUS); y <= Math.min(ty(b.getSouth()), cy + CITY_RADIUS); y++) {
      if (!CITY.tiles.has(`${x}/${y}`)) out.push([x, y, (x - cx) ** 2 + (y - cy) ** 2])
    }
  }
  return out.sort((p, q) => p[2] - q[2]) // nearest the centre first
}

// While looking out from a spot, only the tiles within reach of the eye: the DLS is a throttled
// public service, and a view to the horizon would otherwise ask for a wide ring of them.
function cityTilesNear(f: ViewFocus, tx: (lon: number) => number, ty: (lat: number) => number) {
  const dLat = f.radiusM / 110574, dLon = f.radiusM / (111320 * Math.cos((f.lat * Math.PI) / 180))
  const cx = tx(f.lon), cy = ty(f.lat)
  const out: [number, number, number][] = []
  for (let x = tx(f.lon - dLon); x <= tx(f.lon + dLon); x++) {
    for (let y = ty(f.lat + dLat); y <= ty(f.lat - dLat); y++) {
      if (!CITY.tiles.has(`${x}/${y}`)) out.push([x, y, (x - cx) ** 2 + (y - cy) ** 2])
    }
  }
  return out.sort((p, q) => p[2] - q[2])
}

function pumpCity() {
  while (CITY.on && CITY.queue.length && CITY.inflight.size < 6) {
    const [x, y] = CITY.queue.shift()!
    const key = `${x}/${y}`
    if (CITY.tiles.has(key) || CITY.inflight.has(key)) continue
    CITY.inflight.add(key)
    fetch(`/api/buildings3d/16/${x}/${y}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((fc: FC) => {
        CITY.tiles.add(key)
        const add = fc.features.filter((f) => !CITY.ids.has(f.id!))
        for (const f of add) { CITY.ids.add(f.id!); CITY.features.push(f) }
        if (add.length) cityChanged()
      })
      .catch(() => { /* leave it unloaded; the next move retries it */ })
      .finally(() => { CITY.inflight.delete(key); cityStatus(); pumpCity() })
  }
  cityStatus()
}

function refreshCity() {
  if (!CITY.on) return
  CITY.queue = cityTilesInView().map(([x, y]) => [x, y])
  pumpCity()
}

function cityStatus() {
  let status = ""
  const f = CITY.fill
  if (CITY.on) {
    if (map.getZoom() < CITY_DRAW_ZOOM && !viewFocus) status = "Zoom in to see the 3D buildings"
    else if (f?.problem) status = "The DLS isn't answering just now: OpenStreetMap's buildings stand in where it hasn't been read"
    else if (f && f.fill.done < f.fill.total) status = `Reading the DLS buildings of greater Nicosia, ${Math.floor((100 * f.fill.done) / f.fill.total)}% so far`
  }
  const cur = useMapUi.getState().city
  if (cur.on !== CITY.on || cur.status !== status) setUi({ city: { on: CITY.on, status } })
}

// While the server is still reading greater Nicosia from the DLS (its first run, or a month on),
// the status line says how far it has come. The tiles fill in by themselves meanwhile.
let fillTimer: number | undefined
async function watchCityFill() {
  clearTimeout(fillTimer)
  if (!CITY.on) return
  try {
    const r = await fetch("/api/buildings3d/status")
    if (r.ok) CITY.fill = await r.json()
  } catch { /* the next look */ }
  cityStatus()
  const f = CITY.fill
  if (CITY.on && (!f || f.problem || f.fill.done < f.fill.total)) fillTimer = window.setTimeout(watchCityFill, 10_000)
}

// The detailed buildings near the eye are read while at least one `who` wants them (the shadows,
// say): `cityBuildings()` holds them and `onCityChange` tells of new ones.
export function wantCityDetail(who: string, on: boolean) {
  if (on === CITY.detail.has(who)) return
  if (on) CITY.detail.add(who)
  else CITY.detail.delete(who)
  if (!map) return
  if (on) whenStyleReady(refreshCity)
  else if (!CITY.detail.size) CITY.queue = []
}

// The detailed DLS buildings loaded so far near the eye (each with its roof height `h`, read
// while wantCityDetail asks for them), and a call whenever more arrive or the 3D city is
// switched: for what is drawn from them, like their shadows.
export function cityBuildings(): { on: boolean; features: Feature[] } {
  return { on: CITY.on, features: CITY.features }
}
const citySinks = new Set<() => void>()
export function onCityChange(fn: () => void) {
  citySinks.add(fn)
  return () => { citySinks.delete(fn) }
}
function cityChanged() {
  for (const fn of citySinks) fn()
}

export function setCity3d(on: boolean) {
  CITY.on = on
  cityStatus()
  whenStyleReady(() => {
    addCityLayer()
    paintCityLayers()
    if (on) {
      if (viewFocus) applyViewBuildings()
      else if (map.getPitch() < 40 && map.getZoom() >= CITY_MIN_ZOOM) map.easeTo({ pitch: 55, duration: 900 })
      refreshCity()
      void watchCityFill()
    } else {
      CITY.queue = []
    }
    cityStatus()
    cityChanged()
  })
}

// Every building of the 3D city opens in the inspector (an answer's layer above it comes first).
let cityPopupBound = false
function bindCityPopup() {
  if (cityPopupBound) return
  cityPopupBound = true
  map.on("mouseenter", "city3d::fill", () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", "city3d::fill", () => { map.getCanvas().style.cursor = "" })
}

export async function showRegisters(on: boolean) {
  if (!on) { removeLayer(REGISTERS_REF); return }
  setUi({ registers: { on: true, status: "Loading the dangerous-building registers" } })
  try {
    const res = await fetch("/api/dangerous")
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
    const data = (await res.json()) as FC
    await showLayer({
      ref: REGISTERS_REF, url: "/api/dangerous", style: "buildings_3d", title: data.meta.title,
      fit: false, labels: false, legend: data.meta.legend, count: data.meta.count, value_field: null,
    }, data)
    setUi({ registers: { on: true, status: "" } })
  } catch (e) {
    setUi({ registers: { on: false, status: `Couldn't load the registers: ${(e as Error).message}` } })
  }
}

// ---------------------------------------------------------------------------------------------
// The city clock: every layer and the lamps follow one moment. lib/clock.ts owns the moment and
// reads the city at it from the server; here it is drawn. A time-lapse layer shows its frame
// nearest the moment; a layer of devices shows the states the twin's memory holds for that
// moment (the answer's own drawing while the clock is live); in a comparison, whatever lies left
// of the swipe shows the other moment.

const CLOCK_KINDS = new Set(["street_light", "parking_sensor", "waste_bin", "air_station"])
// Readings a device layer carries that belong to its moment (the rest, like names, stay).
const VALUE_KEYS = new Set(["power_w", "lit", "occupied", "level_pct", "temperature_c", "caqi", "noise_db", "pm2_5", "pm10", "no2", "o3"])
const UNKNOWN = "#a3a3a3"

const clock = {
  at: null as number | null, // the moment (an instant); null: live
  compare: null as number | null, // the other moment, in a comparison
  lamps: new Map<number, FC>(), // the ambient lamps, per moment
  states: new Map<string, StatesAt>(), // every device of a kind, per "kind@moment"
  swipe: 0.5, // where the swipe divides the open map, 0 to 1
}

// What lib/clock.ts does when the map needs it. It registers these (it imports this module, so
// this module never imports it back).
export const clockSinks = {
  range: (ref: string, title: string, iso: string[]): void => { void ref; void title; void iso },
  gone: (ref: string): void => { void ref },
  reload: (): void => {},
  covers: (t: number): boolean => { void t; return false }, // a range on the clock holds t
  seek: (t: number): void => { void t }, // pause and move the clock to t
}
export function onClock(s: Partial<typeof clockSinks>) {
  Object.assign(clockSinks, s)
}

function timedEntry(spec: LayerSpec, data: FC): Entry {
  const orig = data.features.map((f) => ({ ...f.properties }))
  const counts = new Map<string, number>()
  let points = 0
  for (const f of data.features) {
    if (f.geometry?.type !== "Point") continue
    points++
    const k = kindOfId(String(f.properties?.id ?? ""))
    if (k && CLOCK_KINDS.has(k)) counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  // The kinds the layer is about (a lamp's layer with its nearest bay counts the bay too).
  const kinds = [...counts].filter(([, n]) => n > 0 && points > 0).map(([k]) => k)
  const times = data.frames ? data.frames.iso.map((s) => nicosiaWallToDate(s).getTime()) : []
  const entry: Entry = { spec, data, ids: [], stats: null, orig, kinds, times, a: null, b: null }
  entry.a = propsAt(entry, clock.at, "a")
  entry.b = clock.compare !== null ? propsAt(entry, clock.compare, "b") : null
  entry.legendNow = legendOf(entry)
  return entry
}

function nearestFrame(entry: Entry, t: number): number | null {
  const ts = entry.times
  if (!ts.length) return null
  const step = ts.length > 1 ? ts[1] - ts[0] : 3600e3
  if (t < ts[0] - step / 2 || t > ts[ts.length - 1] + step / 2) return null
  let lo = 0, hi = ts.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (ts[mid] <= t) lo = mid
    else hi = mid
  }
  return Math.abs(ts[hi] - t) < Math.abs(ts[lo] - t) ? hi : lo
}

function frameProps(entry: Entry, k: number) {
  const fr = entry.data.frames!
  const legend = entry.spec.legend || {}
  return entry.orig.map((o, j) => {
    const p = { ...o }
    const st = fr.state_names[fr.states[k][j]]
    p.state = st
    p.color = legend[st] || UNKNOWN
    const parts: string[] = []
    for (const [field, rows] of Object.entries(fr.values)) {
      const v = rows[k][j]
      if (v === null || v === undefined) delete p[field]
      else { p[field] = v; parts.push(`${field.replace(/_/g, " ")} ${fmt(v)}`) }
    }
    p.popup = `${p.name || p.label}\n${st.replace(/_/g, " ")}${parts.length ? `, ${parts.join(", ")}` : ""}\n${fr.times[k]}`
    return p
  })
}

// The layer's devices as the memory holds them at t; null until that moment has been read.
function statesProps(entry: Entry, t: number) {
  const maps = new Map<string, StatesAt>()
  for (const k of entry.kinds) {
    const st = clock.states.get(`${k}@${t}`)
    if (!st) return null
    maps.set(k, st)
  }
  const when = clockLabel(t)
  return entry.orig.map((o) => {
    const kind = kindOfId(String(o.id ?? ""))
    const st = kind ? maps.get(kind) : undefined
    if (!st || o._kind === "footprint") return o // not a device the clock knows: as drawn
    const p: Record<string, any> = {}
    for (const [k, v] of Object.entries(o)) if (!VALUE_KEYS.has(k)) p[k] = v
    const rec = st.devices[String(o.id)]
    const s = rec?.s ?? "unknown"
    p.state = s
    p.color = entry.spec.legend?.[s] ?? st.colors[s] ?? UNKNOWN
    const parts: string[] = []
    if (rec) {
      for (const [k, v] of Object.entries(rec)) {
        if (k !== "s" && typeof v === "number") { p[k] = v; if (k !== "lit" && k !== "occupied") parts.push(`${k.replace(/_/g, " ")} ${fmt(v)}`) }
      }
    }
    const what = s === "unknown" ? "no record in the twin's memory" : s.replace(/_/g, " ")
    p.popup = `${o.name || o.label}\n${what}${parts.length ? `, ${parts.join(", ")}` : ""}\n${when}`
    return p
  })
}

// A replay of things the memory has no moments for (traffic cameras) outside its frames: their
// counts are unknown there, so they carry none.
function blankProps(entry: Entry) {
  const fields = Object.keys(entry.data.frames?.values ?? {})
  return entry.orig.map((o) => {
    const p = { ...o }
    for (const f of fields) delete p[f]
    p.popup = `${o.name || o.label}\nNo count at this moment: the replay covers ${entry.data.frames!.times[0]} to ${entry.data.frames!.times.at(-1)}`
    return p
  })
}

function propsAt(entry: Entry, t: number | null, side: "a" | "b") {
  if (t === null) {
    if (side === "a") entry.frame = null
    return null // live: as the answer drew it
  }
  const k = nearestFrame(entry, t)
  if (side === "a") entry.frame = k
  if (k !== null) return frameProps(entry, k)
  if (entry.kinds.length) return statesProps(entry, t) ?? (side === "a" ? entry.a : entry.b)
  if (entry.times.length) return blankProps(entry)
  return null
}

function legendOf(entry: Entry) {
  const base = entry.spec.legend || {}
  if (!entry.a && !entry.b) return base
  const out: Record<string, string> = { ...base }
  for (const props of [entry.a, entry.b]) for (const p of props ?? []) if (p.state && !(p.state in out) && p.color) out[p.state] = p.color
  return out
}

function featuresWith(entry: Entry, props: Record<string, any>[] | null): Feature[] {
  return entry.data.features.map((f, j) => ({ type: "Feature", geometry: f.geometry, properties: props ? props[j] : entry.orig[j] }))
}

function shownFeatures(entry: Entry): Feature[] {
  const a = featuresWith(entry, entry.a)
  if (clock.compare === null || !entry.b) return a
  return split(a, featuresWith(entry, entry.b))
}

// Where the swipe is on the canvas, in px: a share of the part of the map the panels leave open.
function swipeX() {
  const p = map.getPadding()
  const w = map.getCanvas().clientWidth
  const left = p.left ?? 0, right = p.right ?? 0
  return left + clock.swipe * Math.max(1, w - left - right)
}

function anchorOf(f: Feature): [number, number] | null {
  const g = f.geometry
  if (!g) return null
  if (g.type === "Point") return g.coordinates
  const pts = coordsOf(g, [])
  return pts.length ? (pts[0] as [number, number]) : null
}

// A comparison: what stands right of the swipe at `a`'s moment, what stands left of it at `b`'s.
function split(a: Feature[], b: Feature[]): Feature[] {
  const x = swipeX()
  const left = (f: Feature) => {
    const c = anchorOf(f)
    return c ? map.project(c).x < x : false
  }
  return [...a.filter((f) => !left(f)), ...b.filter(left)]
}

function pushData(entry: Entry) {
  const src = map.getSource(`${entry.spec.ref}::src`) as GeoJSONSource | undefined
  if (!src) return
  const features = shownFeatures(entry)
  src.setData((entry.spec.style === "columns_3d" ? columnsData(entry, features) : { type: "FeatureCollection", features }) as any)
}

function newestReplay(): Entry | null {
  let out: Entry | null = null
  for (const e of state.layers.values()) if (e.times.length) out = e
  return out
}

function publishReplays() {
  const replays: Record<string, ReplayUi> = {}
  for (const e of state.layers.values()) {
    if (!e.times.length) continue
    const total = totalsOf(e)
    const k = e.frame ?? null
    replays[e.spec.ref] = {
      ref: e.spec.ref, title: e.spec.title, frame: k, frames: e.times.length,
      reading: total && k !== null ? `${fmt(total.values[k] ?? 0)} ${total.field.replace(/_/g, " ")}` : null,
    }
  }
  setUi({ replays })
}

function renderClock() {
  let legends = false
  for (const entry of state.layers.values()) {
    if (!entry.kinds.length && !entry.times.length) continue
    entry.a = propsAt(entry, clock.at, "a")
    entry.b = clock.compare !== null ? propsAt(entry, clock.compare, "b") : null
    const legend = legendOf(entry)
    if (JSON.stringify(legend) !== JSON.stringify(entry.legendNow)) { entry.legendNow = legend; legends = true }
    if (styleReady) {
      pushData(entry)
      syncStreet(entry, entry.frame ?? null)
    }
  }
  if (styleReady) pushLamps()
  for (const c of state.charts) {
    if (c.link.key !== "frame") continue
    const e = (c.link.layer && state.layers.get(c.link.layer)) || newestReplay()
    setChartHighlight(c, e?.frame ?? null)
  }
  const replay = newestReplay()
  if (replay) for (const v of state.visuals) postToVisual(v, replay.frame ?? null, "frame")
  publishReplays()
  if (legends) publishLayers()
}

let renderQueued = false
function renderSoon() {
  if (renderQueued) return
  renderQueued = true
  requestAnimationFrame(() => { renderQueued = false; renderClock() })
}

let splitQueued = false
function splitSoon() {
  if (splitQueued || !styleReady) return
  splitQueued = true
  requestAnimationFrame(() => {
    splitQueued = false
    for (const e of state.layers.values()) if (e.b) pushData(e)
    pushLamps()
  })
}

// The clock moved (or a comparison started or ended): the sky, the lamps, every layer and every
// chart's time line follow.
export function setClockView(at: number | null, compare: number | null) {
  const moved = at !== clock.at
  clock.at = at
  clock.compare = compare
  if (!map) return
  if (moved) showSky(at === null ? new Date() : new Date(at))
  else syncNightLights()
  renderClock()
  // The clock moved: every chart's line goes to its moment, whatever a pointer last hovered.
  hoverMs = null
  setTimeAll(currentTimeMs())
}

// The lamps and the devices of a kind as they were at moment t, read by lib/clock.ts.
export function putLamps(t: number, fc: FC) {
  clock.lamps.set(t, fc)
  if (clock.lamps.size > 64) clock.lamps.delete(clock.lamps.keys().next().value as number)
  if (map && styleReady && (t === clock.at || t === clock.compare)) {
    if (map.getSource("lamps")) pushLamps()
    else addAmbientLamps()
  }
}

export function putStates(t: number, kind: string, st: StatesAt) {
  clock.states.set(`${kind}@${t}`, st)
  if (clock.states.size > 256) clock.states.delete(clock.states.keys().next().value as string)
  if (map && (t === clock.at || t === clock.compare)) renderSoon()
}

// The kinds of device the layers on the map need read at moment t (a time-lapse covering t has
// its own frames).
export function clockKinds(t: number) {
  const out = new Set<string>()
  for (const e of state.layers.values()) {
    if (e.kinds.length && nearestFrame(e, t) === null) for (const k of e.kinds) out.add(k)
  }
  return [...out]
}

export function setSwipeAt(share: number) {
  clock.swipe = share
  splitSoon()
}

// The moment's label in a feature's popup.
function clockLabel(t: number) {
  return new Date(t).toLocaleString("en-GB", { timeZone: "Asia/Nicosia", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
}

// ---------------------------------------------------------------------------------------------
// Linked highlights: one value (a device/camera id, or a time-lapse frame) shared by the map,
// linked charts and custom visuals. Hovering any of them highlights it in the others.

const HL_SRC = "hl::src"
let hlFeatures: Feature[] = []
let hlValue: string | number | null = null

function syncHighlight() {
  if (!styleReady) return
  const data = { type: "FeatureCollection", features: hlFeatures } as any
  if (!map.getSource(HL_SRC)) {
    map.addSource(HL_SRC, { type: "geojson", data })
    map.addLayer({ id: "hl::glow", type: "circle", source: HL_SRC, paint: { "circle-radius": 22, "circle-color": SODIUM, "circle-opacity": 0.22, "circle-blur": 0.45 } })
    map.addLayer({ id: "hl::ring", type: "circle", source: HL_SRC, paint: { "circle-radius": 12, "circle-opacity": 0, "circle-stroke-color": SODIUM, "circle-stroke-width": 3 } })
  } else {
    (map.getSource(HL_SRC) as GeoJSONSource).setData(data)
    map.moveLayer("hl::glow")
    map.moveLayer("hl::ring")
  }
}

function hl3dFilter(value: unknown): any {
  return ["all", ["==", ["get", "_kind"], "footprint"], ["==", ["to-string", ["get", "id"]], value === null || value === undefined ? "\u0000" : String(value)]]
}

function centerOf(geom: any) {
  const pts = coordsOf(geom, [])
  return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length]
}

function highlightOnMap(value: unknown) {
  hlFeatures = []
  if (value !== null && value !== undefined) {
    const want = String(value)
    const shapes: Feature[] = []
    for (const e of state.layers.values()) {
      for (const f of e.data.features) {
        if (String(f.properties?.id) !== want) continue
        if (f.geometry?.type === "Point") hlFeatures.push({ type: "Feature", geometry: f.geometry, properties: {} })
        else if (f.properties._kind === "footprint") shapes.push(f)
      }
    }
    // A building with no marker (get_buildings_3d) rings its footprint's centre instead.
    if (!hlFeatures.length) hlFeatures = shapes.map((f) => ({ type: "Feature", geometry: { type: "Point", coordinates: centerOf(f.geometry) }, properties: {} }))
  }
  for (const id of state.hl3d) if (map.getLayer(id)) map.setFilter(id, hl3dFilter(value))
  syncHighlight()
}

function setChartHighlight(c: LinkedChart, value: unknown) {
  try { c.view.signal("_hl", value ?? null).runAsync() } catch { /* chart was removed */ }
}

function postToVisual(v: Visual, value: unknown, kind: string) {
  v.iframe.contentWindow?.postMessage({ __host: true, type: "highlight", value: value ?? null, kind }, "*")
}

export function linkHover(key: string, value: unknown, origin: unknown) {
  if (key === "frame") {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return
    const ref = (origin as LinkedChart | null)?.link?.layer
    const entry = (ref && state.layers.get(ref)) || newestReplay()
    const t = entry?.times[Number(value)]
    if (t !== undefined) clockSinks.seek(t)
    return
  }
  const v = (value ?? null) as string | number | null
  if (v === hlValue) return
  hlValue = v
  highlightOnMap(v)
  // Pan to a single highlighted feature that is off screen, unless the hover came from the map.
  if (origin !== "map" && hlFeatures.length === 1 && !map.getBounds().contains(hlFeatures[0].geometry.coordinates)) {
    map.easeTo({ center: hlFeatures[0].geometry.coordinates, duration: 600 })
  }
  for (const c of state.charts) if (c.link.key !== "frame") setChartHighlight(c, v)
  for (const vis of state.visuals) if (vis !== origin) postToVisual(vis, v, "id")
}

// Charts and visuals on the stage register here while they are shown.
export function registerChart(view: any, link: ChartLink) {
  const c = { view, link }
  state.charts.push(c)
  const replay = link.key === "frame" ? (link.layer && state.layers.get(link.layer)) || newestReplay() : null
  if (replay && replay.frame !== null && replay.frame !== undefined) setChartHighlight(c, replay.frame)
  else if (link.key !== "frame" && hlValue !== null) setChartHighlight(c, hlValue)
  return c
}

export function registerTimeChart(view: any, min: number, max: number) {
  const tc: TimeChart = { view, min, max, last: null }
  state.timeCharts.push(tc)
  setChartTime(tc, currentTimeMs())
  return tc
}

export function unregisterView(view: any) {
  state.charts = state.charts.filter((c) => c.view !== view)
  state.timeCharts = state.timeCharts.filter((c) => c.view !== view)
}

export function registerVisual(v: Visual) {
  state.visuals.push(v)
}

export function unregisterVisual(v: Visual) {
  state.visuals = state.visuals.filter((x) => x !== v)
}

function onVisualMessage(e: MessageEvent) {
  const v = state.visuals.find((x) => x.iframe.contentWindow === e.source)
  const m = e.data
  if (!v || !m || m.__visual !== true) return
  if (m.type === "size" && Number.isFinite(m.height)) {
    v.onSize(m.height)
  } else if (m.type === "error") {
    const text = String(m.message).slice(0, 400)
    v.onError(text)
    if (!state.visualErrors.includes(`${v.title}: ${text}`)) state.visualErrors.push(`${v.title}: ${text}`)
  } else if (m.type === "ready") {
    // A visual that just (re)loaded learns what is highlighted right now.
    const replay = newestReplay()
    if (replay && replay.frame !== null && replay.frame !== undefined) postToVisual(v, replay.frame, "frame")
    if (hlValue !== null) postToVisual(v, hlValue, "id")
  } else if (m.type === "highlight") {
    const val = m.value
    if (val === null || typeof val === "string" || typeof val === "number") {
      linkHover(typeof val === "number" ? "frame" : "id", val, v)
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Time sync: every chart over real time has a rule at its `_t` param: the city clock's moment
// (now, moved on every 30 seconds, while it is live). Times are Nicosia wall-clock strings read
// as browser-local, the same way Vega reads the data.

// An instant as Nicosia wall time read as local ms ("2026-10-20 14:05" -> local).
function wallOf(t: number) {
  const s = new Date(t).toLocaleString("sv-SE", { timeZone: "Asia/Nicosia", hour12: false })
  return Date.parse(s.replace(" ", "T"))
}

// The other way: a chart's moment (wall time as local ms) as an instant.
function instantOf(ms: number) {
  const d = new Date(ms)
  const two = (n: number) => String(n).padStart(2, "0")
  return nicosiaWallToDate(`${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}T${two(d.getHours())}:${two(d.getMinutes())}`).getTime()
}

export function nicosiaNowMs() {
  return wallOf(Date.now())
}

function currentTimeMs() {
  return clock.at === null ? nicosiaNowMs() : wallOf(clock.at)
}

function setChartTime(tc: TimeChart, ms: number) {
  const v = Number.isFinite(ms) && ms >= tc.min && ms <= tc.max ? ms : null // hide outside the chart
  if (v === tc.last) return
  tc.last = v
  try { tc.view.signal("_t", v).runAsync() } catch { /* chart was removed */ }
}

function setTimeAll(ms: number) {
  for (const tc of state.timeCharts) setChartTime(tc, ms)
}

// The moment under the pointer in a chart over time (null: the pointer left). When a range on
// the city clock holds it, the clock goes there (and the map, the strip and every chart with
// it). Otherwise only the charts' line follows the pointer, and goes back to the clock's moment
// when the pointer leaves.
let hoverMs: number | null = null
export function hoverTime(ms: number | null) {
  if (ms !== null) {
    const t = instantOf(ms)
    if (clockSinks.covers(t)) { clockSinks.seek(t); ms = null }
  }
  if (ms === hoverMs) return
  hoverMs = ms
  setTimeAll(ms ?? currentTimeMs())
}

// ---------------------------------------------------------------------------------------------
// Callouts and animation

function clearCallouts() {
  state.callouts.forEach((m) => m.remove())
  state.callouts = []
}

function addCallouts(items: { title: string; text?: string; lon: number; lat: number }[], clearPrevious?: boolean) {
  if (clearPrevious !== false) clearCallouts()
  for (const it of items) {
    // The marker element is MapLibre's (absolutely placed, moved by `transform`); the callout
    // inside it can then have its own layout and pop-in animation without fighting either.
    const el = document.createElement("div")
    const box = document.createElement("div")
    box.className = "callout"
    box.innerHTML = `<b>${escapeHtml(it.title)}</b>${it.text ? escapeHtml(it.text) : ""}`
    el.appendChild(box)
    state.callouts.push(new maplibregl.Marker({ element: el, anchor: "bottom", offset: [0, -14] }).setLngLat([it.lon, it.lat]).addTo(map))
  }
}

// Pulsing halos and marching route dashes.
const DASHES = [[0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0], [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5], [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5]]
let lastDash = -1
function animate(ts: number) {
  const t = (ts % 1600) / 1600
  for (const id of state.pulses) {
    if (!map.getLayer(id)) continue
    map.setPaintProperty(id, "circle-radius", 8 + t * 22)
    map.setPaintProperty(id, "circle-opacity", 0.45 * (1 - t))
  }
  const step = Math.floor(ts / 70) % DASHES.length
  if (step !== lastDash) {
    lastDash = step
    for (const id of state.dashLayers) if (map.getLayer(id)) map.setPaintProperty(id, "line-dasharray", DASHES[step])
  }
  requestAnimationFrame(animate)
}

// ---------------------------------------------------------------------------------------------
// Fly the route: the camera walks a route at street level, tilted, looking where the walker
// goes, with the 3D city on so the register buildings along the way stand up. Any drag, wheel,
// click or touch on the map stops it; with reduced motion it jumps straight to the end.

const FLY = { zoom: 17, pitch: 60, msPerKm: 10500, lookAheadM: 70, turnS: 0.9, maxTurnDegS: 80 }
let flight: { ref: string; raf: number; stop: () => void } | null = null

function metres([lon1, lat1]: number[], [lon2, lat2]: number[]) {
  const k = Math.cos(((lat1 + lat2) / 2) * (Math.PI / 180))
  return Math.hypot((lon2 - lon1) * k, lat2 - lat1) * 111320
}

function heading([lon1, lat1]: number[], [lon2, lat2]: number[]) {
  const r = Math.PI / 180
  const y = Math.sin((lon2 - lon1) * r) * Math.cos(lat2 * r)
  const x = Math.cos(lat1 * r) * Math.sin(lat2 * r) - Math.sin(lat1 * r) * Math.cos(lat2 * r) * Math.cos((lon2 - lon1) * r)
  return (Math.atan2(y, x) / r + 360) % 360
}

// The route to fly: the walker's own line (the safer one when the usual route is drawn too),
// all its legs joined in order.
function routePath(entry: Entry): number[][] {
  const path: number[][] = []
  for (const f of entry.data.features) {
    if (f.properties?.state === "usual route") continue
    const g = f.geometry
    const lines = g?.type === "LineString" ? [g.coordinates] : g?.type === "MultiLineString" ? g.coordinates : []
    for (const line of lines) for (const p of line) {
      if (!path.length || metres(path[path.length - 1], p) > 0.5) path.push(p)
    }
  }
  return path
}

export function stopFlight() {
  flight?.stop()
}

export function flyRoute(ref: string) {
  takeOver()
  stopFlight()
  framed = null
  const entry = state.layers.get(ref)
  const path = entry ? routePath(entry) : []
  if (path.length < 2) return
  const along = [0]
  for (let i = 1; i < path.length; i++) along.push(along[i - 1] + metres(path[i - 1], path[i]))
  const total = along[along.length - 1]
  const pointAt = (d: number) => {
    d = Math.max(0, Math.min(total, d))
    let i = 1
    while (i < along.length - 1 && along[i] < d) i++
    const seg = along[i] - along[i - 1] || 1
    const f = (d - along[i - 1]) / seg
    return [path[i - 1][0] + (path[i][0] - path[i - 1][0]) * f, path[i - 1][1] + (path[i][1] - path[i - 1][1]) * f]
  }
  const end = path[path.length - 1]
  const finalBearing = heading(pointAt(total - FLY.lookAheadM), end)
  const arrive = (duration: number) => {
    // Ease out to frame the destination.
    if (duration) map.easeTo({ center: end as [number, number], zoom: 16.4, pitch: 52, bearing: finalBearing, duration, essential: true })
    else map.jumpTo({ center: end as [number, number], zoom: 16.4, pitch: 52, bearing: finalBearing })
  }

  if (!CITY.on) setCity3d(true)
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    arrive(0)
    return
  }

  const interrupt = () => stopFlight()
  const events = ["mousedown", "touchstart", "wheel", "dragstart"] as const
  const me = {
    ref,
    raf: 0,
    stop: () => {
      cancelAnimationFrame(me.raf)
      for (const e of events) map.off(e, interrupt)
      if (flight === me) flight = null
      setUi({ flying: null })
    },
  }
  flight = me
  setUi({ flying: ref })
  for (const e of events) map.on(e, interrupt)

  // A steady walking pace on screen: about 13 s for a 1.3 km walk, never under 6 s.
  const duration = Math.max(6000, Math.min(45000, (total / 1000) * FLY.msPerKm))
  let bearing = heading(path[0], pointAt(FLY.lookAheadM))
  map.flyTo({ center: path[0] as [number, number], zoom: FLY.zoom, pitch: FLY.pitch, bearing, duration: 1600, essential: true })
  map.once("moveend", () => {
    if (flight !== me) return
    let t0 = 0
    let last = 0
    const step = (ts: number) => {
      if (flight !== me) return
      if (!state.layers.has(ref)) { me.stop(); return }
      if (!t0) { t0 = ts; last = ts }
      const t = Math.min(1, (ts - t0) / duration)
      // Mostly steady, with a gentle start and finish.
      const s = 0.85 * t + 0.15 * t * t * (3 - 2 * t)
      const d = s * total
      const here = pointAt(d)
      // Look a little ahead, and turn smoothly (and no faster than 80°/s, even at a U-turn)
      // so corners don't jerk the camera.
      const want = heading(here, pointAt(d + FLY.lookAheadM))
      const diff = ((want - bearing + 540) % 360) - 180
      const dt = (ts - last) / 1000
      const turn = Math.max(-FLY.maxTurnDegS * dt, Math.min(FLY.maxTurnDegS * dt, diff * Math.min(1, dt / FLY.turnS)))
      bearing = (bearing + turn + 360) % 360
      last = ts
      map.jumpTo({ center: here as [number, number], bearing, zoom: FLY.zoom, pitch: FLY.pitch })
      if (t < 1) me.raf = requestAnimationFrame(step)
      else { me.stop(); arrive(1800) }
    }
    me.raf = requestAnimationFrame(step)
  })
}

// ---------------------------------------------------------------------------------------------
// The open thing: a steady sodium ring where it is, its outline on the ground, and its building
// raised in sodium. The inspector sets it; a map click reaches the inspector through onInspect.

export type Selection = {
  center: [number, number]
  geometry?: GeoJSON.Geometry | null
  footprints?: { geometry: GeoJSON.Geometry; height_m: number }[]
  pin?: boolean // a dropped pin (a point of the map, "What's here?") in place of the ring
}

const SEL_SRC = "sel::src"
let selection: Selection | null = null
let pin: maplibregl.Marker | null = null
const inspectSinks = new Set<(t: InspectTarget) => void>()

export function onInspect(fn: (t: InspectTarget) => void) {
  inspectSinks.add(fn)
  return () => { inspectSinks.delete(fn) }
}

export function setSelection(s: Selection | null) {
  selection = s
  if (!s && framed?.ref === "") framed = null // closed: don't fly back to it when panels move
  if (map) { syncSelection(); syncPin() }
}

// The dropped pin: a sodium marker standing on the point, dropped in (unless motion is reduced).
function syncPin() {
  if (!selection?.pin) {
    pin?.remove()
    pin = null
    return
  }
  if (pin) {
    pin.setLngLat(selection.center)
    const el = pin.getElement()
    el.classList.remove("spot-pin-drop")
    void el.offsetWidth // restart the drop for a new point
    el.classList.add("spot-pin-drop")
    return
  }
  const el = document.createElement("div")
  el.className = "spot-pin spot-pin-drop"
  el.setAttribute("aria-hidden", "true")
  el.innerHTML = '<svg viewBox="0 0 28 38" width="28" height="38"><path d="M14 1C6.8 1 1 6.7 1 13.8 1 23.4 14 37 14 37s13-13.6 13-23.2C27 6.7 21.2 1 14 1z"/><circle cx="14" cy="13.6" r="4.6"/></svg>'
  pin = new maplibregl.Marker({ element: el, anchor: "bottom" }).setLngLat(selection.center).addTo(map)
}

function syncSelection() {
  if (!styleReady) return
  const features: Feature[] = []
  if (selection) {
    if (!selection.pin) features.push({ type: "Feature", geometry: { type: "Point", coordinates: selection.center }, properties: { _k: "ring" } })
    const g = selection.geometry
    if (g && (g.type === "Polygon" || g.type === "MultiPolygon" || g.type === "LineString")) {
      features.push({ type: "Feature", geometry: g, properties: { _k: "shape" } })
    }
    for (const fp of selection.footprints ?? []) {
      features.push({ type: "Feature", geometry: fp.geometry, properties: { _k: "raise", h: fp.height_m } })
    }
  }
  const data = { type: "FeatureCollection", features } as any
  const src = map.getSource(SEL_SRC) as GeoJSONSource | undefined
  if (src) {
    src.setData(data)
    for (const id of ["sel::fill", "sel::line", "sel::raise", "sel::halo", "sel::ring"]) if (map.getLayer(id)) map.moveLayer(id)
    return
  }
  map.addSource(SEL_SRC, { type: "geojson", data })
  const shape: any = ["==", ["get", "_k"], "shape"]
  const polys: any = ["all", shape, ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false]]
  map.addLayer({ id: "sel::fill", type: "fill", source: SEL_SRC, filter: polys, paint: { "fill-color": SODIUM, "fill-opacity": 0.12 } })
  map.addLayer({ id: "sel::line", type: "line", source: SEL_SRC, filter: shape, layout: { "line-join": "round" }, paint: { "line-color": SODIUM, "line-width": 2.5 } })
  map.addLayer({
    id: "sel::raise", type: "fill-extrusion", source: SEL_SRC, filter: ["==", ["get", "_k"], "raise"],
    paint: { "fill-extrusion-color": SODIUM, "fill-extrusion-height": ["+", ["get", "h"], 0.6], "fill-extrusion-opacity": 0.6 },
  })
  const ring: any = ["==", ["get", "_k"], "ring"]
  map.addLayer({ id: "sel::halo", type: "circle", source: SEL_SRC, filter: ring, paint: { "circle-radius": 20, "circle-color": SODIUM, "circle-opacity": 0.16, "circle-blur": 0.5 } })
  map.addLayer({
    id: "sel::ring", type: "circle", source: SEL_SRC, filter: ring,
    paint: { "circle-radius": 11, "circle-opacity": 0, "circle-stroke-color": SODIUM, "circle-stroke-width": 2.5 },
  })
}

// Fly to one thing, close enough to see it among its neighbours; a building at an angle, with
// the 3D city on. With reduced motion the camera jumps.
// Opening the inspector (or the stage) changes the camera padding a moment later, which stops a
// flight under way; as with a layer's fit, the flight is kept as the framing and flown again.
export function flyToThing(center: [number, number], kind?: string) {
  if (!map) return
  takeOver()
  state.flownAt = performance.now()
  const building = kind === "building_3d" || kind === "register_building"
  if (kind === "building_3d" && !CITY.on) setCity3d(true)
  const opts = {
    center, zoom: Math.max(map.getZoom(), building ? 17 : 16.8),
    pitch: building ? Math.max(map.getPitch(), 55) : map.getPitch(), essential: true,
  }
  const go = () => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) map.jumpTo(opts)
    else map.flyTo({ ...opts, duration: 1600 })
  }
  framed = { ref: "", fit: go }
  go()
}

// Fly to fit an area, given as [west, south, east, north] (a street from search).
export function flyToBounds(bbox: [number, number, number, number]) {
  if (!map) return
  takeOver()
  state.flownAt = performance.now()
  const b = new maplibregl.LngLatBounds([bbox[0], bbox[1]], [bbox[2], bbox[3]])
  const go = () => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    map.fitBounds(b, { padding: 60, maxZoom: 17, duration: reduce ? 0 : 1400 })
  }
  framed = { ref: "", fit: go }
  go()
}

// The camera, for links that bring a view back.
export function getCamera(): Camera | null {
  if (!map) return null
  const c = map.getCenter()
  return { center: [c.lng, c.lat], zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() }
}

export function jumpToCamera(c: Camera) {
  if (!map) return
  takeOver()
  framed = null
  map.jumpTo(c)
}

export function onCameraMoved(fn: () => void) {
  if (!map) return () => {}
  map.on("moveend", fn)
  return () => { map.off("moveend", fn) }
}

// ---------------------------------------------------------------------------------------------
// Commands from the assistant

export function applyMapUi(ev: any, onError: (text: string) => void) {
  switch (ev.action) {
    case "show_layer":
      showLayer({ ...ev.layer, askedAt: performance.now() }).catch((e) => onError(`Couldn't draw layer: ${e.message}`))
      break
    case "fly_to": {
      takeOver()
      state.flownAt = performance.now()
      framed = null
      const opts: any = { essential: true, duration: 2200 }
      if (ev.center) opts.center = ev.center
      for (const k of ["zoom", "pitch", "bearing"]) if (ev[k] !== undefined) opts[k] = ev[k]
      map.flyTo(opts)
      break
    }
    case "callouts":
      addCallouts(ev.items, ev.clear_previous)
      break
    case "city_3d":
      setCity3d(Boolean(ev.on))
      break
    case "clear":
      if (ev.ref) removeLayer(ev.ref)
      else clearMap()
      break
  }
}

export function mapContext() {
  const c = map.getCenter()
  return {
    map_view: { center: [c.lng, c.lat], zoom: map.getZoom() },
    layers_on_map: [...state.layers.values()].map((l) => `${l.spec.ref} (${l.spec.title})`),
    city_3d: CITY.on,
    user_location: useMapUi.getState().me.loc,
    visual_errors: state.visualErrors.splice(0),
  }
}

export function resetForNewChat() {
  clearMap()
  hlValue = null
  state.visualErrors = []
  resetView()
}

// ---------------------------------------------------------------------------------------------
// Looking out from a spot (lib/view.ts owns the view; here is what the map does for it). The 3D
// city's tiles come out to the horizon (the DLS buildings, and OpenStreetMap's beyond them), and
// the base map's own buildings stay hidden. Answer layers don't refit the camera meanwhile, and
// any other camera move (the assistant's, the inspector's, a link's) ends the view first.

export type ViewFocus = { lon: number; lat: number; radiusM: number; hide?: number }
let viewFocus: ViewFocus | null = null
const VIEW_BUILDINGS_MINZOOM = 13
// The base map's 3D building layers as they were, to put back after the view.
const viewSaved = new Map<string, { minzoom: number; maxzoom: number; filter: any; height: any }>()
const takeovers = { leave: null as (() => void) | null }

function takeOver() {
  if (viewFocus && takeovers.leave) takeovers.leave()
}

// lib/view.ts: called when another camera move takes over from the view.
export function onViewTakeover(fn: (() => void) | null) {
  takeovers.leave = fn
}

export function viewing() {
  return viewFocus !== null
}

export function setViewFocus(f: ViewFocus | null) {
  viewFocus = f
  if (!map) return
  whenStyleReady(() => {
    applyViewBuildings()
    syncCityClaims()
    applySky()
    if (CITY.on) refreshCity()
  })
}

// The city's lights from afar: looking out after dark, a small warm point at every bend of every
// street, round and the same few pixels at any distance, so the streets km away still read as
// lights (the glow along them is too thin at that angle). Only in a view, only at night.
const VIEW_LIGHTS = "view::lights"
function syncViewLights() {
  const want = !!viewFocus && shown.dark && useMapUi.getState().nightLights
  if (!want) { if (map.getLayer(VIEW_LIGHTS)) map.removeLayer(VIEW_LIGHTS); return }
  if (map.getLayer(VIEW_LIGHTS) || !map.getSource(BASE_SOURCE)) return
  map.addLayer({
    id: VIEW_LIGHTS, type: "circle", source: BASE_SOURCE, "source-layer": "transportation", minzoom: 11,
    filter: ["match", ["get", "class"], ["primary", "secondary", "tertiary", "minor", "trunk", "service"], true, false],
    paint: {
      "circle-radius": ["match", ["get", "class"], ["primary", "trunk", "secondary"], 1.7, 1.2],
      "circle-color": "#ffcf8a", "circle-blur": 0.55, "circle-opacity": 0.85,
      "circle-pitch-alignment": "viewport", "circle-pitch-scale": "viewport",
    },
  }, firstLabelLayer())
}

// The DLS buildings in a view: stone by day, as they look, rather than coloured by height.
const VIEW_STONE = ["interpolate", ["linear"], ["get", "h"], 0, "#e9e4da", 20, "#ddd6c8", 60, "#cbc2b0"]

function applyViewBuildings() {
  if (!styleReady) return
  for (const l of map.getStyle()?.layers || []) {
    if (l.type !== "fill-extrusion" || !isBase(l.id)) continue
    const id = l.id
    if (viewFocus) {
      if (!viewSaved.has(id)) {
        viewSaved.set(id, {
          minzoom: l.minzoom ?? 0, maxzoom: l.maxzoom ?? 24, filter: map.getFilter(id) ?? null,
          height: map.getPaintProperty(id, "fill-extrusion-height"),
        })
      }
      const saved = viewSaved.get(id)!
      map.setLayerZoomRange(id, VIEW_BUILDINGS_MINZOOM, saved.maxzoom)
      map.setFilter(id, saved.filter)
      map.setPaintProperty(id, "fill-extrusion-height", ["coalesce", ["get", "render_height"], 6])
      map.setLayoutProperty(id, "visibility", "none") // the city's tiles stand in for them
    } else if (viewSaved.has(id)) {
      const saved = viewSaved.get(id)!
      map.setLayerZoomRange(id, saved.minzoom, saved.maxzoom)
      map.setFilter(id, saved.filter)
      map.setPaintProperty(id, "fill-extrusion-height", saved.height)
      map.setLayoutProperty(id, "visibility", "none")
    }
  }
  if (!viewFocus) viewSaved.clear()
  // By day a view shows the buildings in stone, as they look; otherwise as the map paints them.
  if (viewFocus && useMapUi.getState().basemap === "day" && map.getLayer("city3d::fill")) {
    map.setPaintProperty("city3d::fill", "fill-extrusion-color", VIEW_STONE)
  } else paintCityLayers()
}

// Place the camera for the view: an eye-level camera from lib/view.ts.
export function viewCamera(opts: maplibregl.JumpToOptions) {
  framed = null
  state.flownAt = performance.now()
  map.jumpTo(opts)
}

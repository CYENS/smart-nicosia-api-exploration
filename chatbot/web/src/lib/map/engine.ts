/* The map: MapLibre plus everything the assistant draws on it. Ported from the original
   app.js; React reads what it needs through `useMapUi`, and everything else stays imperative. */
import maplibregl, { type GeoJSONSource, type LayerSpecification, type Map as MlMap } from "maplibre-gl"
import { create } from "zustand"

import { BASEMAP_URLS, transformStyle, type Basemap } from "@/lib/map/basemap"
import { lightFor, nicosiaWallToDate, skyAt, skyFor, type Sky } from "@/lib/map/sky"
import { WALLS_RING } from "@/lib/walls"

/* eslint-disable @typescript-eslint/no-explicit-any */
type Feature = { type: "Feature"; id?: string | number; geometry: any; properties: Record<string, any> }
type FC = { type: "FeatureCollection"; features: Feature[]; frames?: Frames; meta?: any }
type Frames = {
  times: string[]; iso: string[]; state_names: string[]; states: number[][]
  values: Record<string, (number | null)[][]>
}
export type LayerSpec = {
  ref: string; url?: string; style: string; title: string; count: number
  legend?: Record<string, string>; value_field?: string | null; fit?: boolean; labels?: boolean
  askedAt?: number
}
type Entry = {
  spec: LayerSpec; data: FC; ids: string[]; stats: Stats | null; half?: number
  extruded?: boolean // has 3D footprints
  lamps?: boolean // street lights, which glow after dark
}
type Stats = { min: number; max: number }

export const RAMP = ["#16a34a", "#84cc16", "#eab308", "#f97316", "#dc2626"]
export const SODIUM = "#ffae45"
const NIGHT = "#0e1828"
const FONT_REGULAR = ["Noto Sans Regular"]
const FONT_BOLD = ["Noto Sans Bold"]
const HOME = { center: [33.3615, 35.1725] as [number, number], zoom: 14.2, pitch: 45, bearing: -12 }
export const REGISTERS_REF = "eoa_registers"
const SPEEDS = [1, 2, 4]

// The DLS 3D city stays dark slate and lightens only as it climbs: the skyline reads at a glance,
// and the assistant's layers (red registers, blue routes) still stand out against it.
const CITY_HEIGHTS_NIGHT = [0, "#1a2840", 6, "#203250", 12, "#2a4064", 20, "#37547f", 35, "#5577a6", 60, "#93b3dc"]
const CITY_HEIGHTS_DAY = [0, "#e5e7eb", 6, "#cbd5e1", 12, "#93c5fd", 20, "#3b82f6", 35, "#1d4ed8", 60, "#312e81"]
const CITY_SRC = "city3d::src"
const CITY_MIN_ZOOM = 14.5
const CITY_RADIUS = 3 // tiles around the centre (each ~500 m), so pitched views stay bounded

// ---------------------------------------------------------------------------------------------
// What React shows: layers with their legends, the 3D city, the registers and the time-lapse.

export type LayerSummary = {
  ref: string; title: string; count: number; style: string
  legend: Record<string, string>; stats: Stats | null; valueField: string | null
}
export type TimelapseUi = {
  ref: string; title: string; frame: number; frames: number; time: string; playing: boolean; speed: number
}
type MapUi = {
  ready: boolean
  basemap: Basemap // the base map shown
  basemapMode: "auto" | Basemap // auto follows the sun
  skyNow: Sky // the real sky right now, for the strip and the map panel
  nightLights: boolean // street lights glow after dark
  layers: LayerSummary[]
  city: { on: boolean; status: string }
  registers: { on: boolean; status: string }
  timelapse: TimelapseUi | null
  me: { loc: MyLocation | null; error: string }
  flying: string | null // the route the camera is flying along
}
export type MyLocation = { lat: number; lon: number; accuracy_m: number | null; source: string }
export const useMapUi = create<MapUi>(() => ({
  ready: false,
  basemap: "night",
  basemapMode: "auto",
  skyNow: skyAt(new Date()),
  nightLights: true,
  layers: [],
  city: { on: false, status: "" },
  registers: { on: false, status: "" },
  timelapse: null,
  me: { loc: null, error: "" },
  flying: null,
}))
const setUi = useMapUi.setState

export function cityHeights() {
  return useMapUi.getState().basemap === "night" ? CITY_HEIGHTS_NIGHT : CITY_HEIGHTS_DAY
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
  timelapse: null as null | { ref: string; frame: number; playing: boolean; speed: number; timer?: number },
  charts: [] as LinkedChart[],
  timeCharts: [] as TimeChart[],
  visuals: [] as Visual[],
  visualErrors: [] as string[],
  flownAt: 0,
}
const CITY = {
  on: false, features: [] as Feature[], ids: new Set<string | number>(), tiles: new Set<string>(),
  inflight: new Set<string>(), queue: [] as [number, number][],
}

let map = undefined as unknown as MlMap // set once by initMap
let styleReady = false

export function getMap() {
  return map
}

function whenStyleReady(fn: () => void) {
  if (styleReady) fn()
  else map.once("style.load", fn)
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

export function initMap(container: HTMLElement) {
  if (map) return map // React's development mode runs effects twice
  // No style yet: setBasemap loads the first one, recoloured. (Starting from a placeholder style
  // made that first setStyle try to diff against a style still loading, and stall.)
  map = new maplibregl.Map({
    container,
    ...HOME,
    maxPitch: 75,
    attributionControl: { compact: true },
  })
  ;(window as any).__map = map // a hook for scripted demos and browser tests (e.g. map.project)
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right")
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left")
  geolocate = new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true }, trackUserLocation: false, showUserLocation: false,
    fitBoundsOptions: { maxZoom: 16.5 },
  })
  map.addControl(geolocate, "bottom-right")
  geolocate.on("geolocate", (pos: GeolocationPosition) => setMyLocation(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, "device location"))
  geolocate.on("error", () => setUi({ me: { loc: useMapUi.getState().me.loc, error: "Couldn't read your location here. Right-click the map where you are instead." } }))
  map.on("contextmenu", (e) => {
    const button = document.createElement("button")
    button.type = "button"
    button.className = "me-btn"
    button.textContent = "I'm here"
    const popup = new maplibregl.Popup({ closeButton: false, offset: 6 }).setLngLat(e.lngLat).setDOMContent(button).addTo(map)
    button.addEventListener("click", () => { setMyLocation(e.lngLat.lat, e.lngLat.lng, NaN, "set on the map"); popup.remove() })
  })
  // A page opened in a hidden tab measures a 0-size container; keep the canvas in step.
  new ResizeObserver(() => map.resize()).observe(container)

  map.on("style.load", () => {
    styleReady = true
    addWalls()
    addAmbientLamps()
    if (CITY.on) addCityLayer()
    for (const entry of state.layers.values()) addToMap(entry)
    syncHighlight()
    applySky()
  })
  map.on("load", () => setUi({ ready: true }))
  map.on("moveend", refreshCity)
  map.on("zoomend", cityStatus)
  showSky(new Date())
  requestAnimationFrame(animate)
  setInterval(() => { if (!state.timelapse) setTimeAll(nicosiaNowMs()) }, 30e3)
  // The sky moves on: every minute the light, the sky and (at dusk and dawn) the base map follow.
  setInterval(() => {
    setUi({ skyNow: skyAt(new Date()) })
    if (!state.timelapse) showSky(new Date())
  }, 60e3)
  window.addEventListener("message", onVisualMessage)
  return map
}

let styleShown: Basemap | null = null

function loadBasemap(b: Basemap) {
  styleReady = false
  styleShown = b
  setUi({ basemap: b })
  // A full reload, not a diff: the style.load handler redraws everything on top.
  map.setStyle(BASEMAP_URLS[b], { diff: false, transformStyle: transformStyle(b) })
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
  if (want !== styleShown) loadBasemap(want) // applySky runs once it has loaded
  else applySky()
}

function applySky() {
  if (!styleReady) return
  map.setLight(lightFor(shown))
  try { map.setSky(skyFor(shown)) } catch { /* a style without sky support */ }
  syncNightLights()
}

export function setNightLights(on: boolean) {
  setUi({ nightLights: on })
  syncNightLights()
}

// Street lights glow from dusk to dawn: the city's own lamps (live from /api/lamps), any street
// light layer the assistant draws (following a time-lapse frame by frame), and a faint warm
// wash along the streets of the Night base map.
const nightLayers = new Set<string>(["street-glow"])
let lamps: FC | null = null
let lampsAt = 0

function lampLayerOnMap() {
  for (const e of state.layers.values()) if (e.lamps) return true
  return false
}

function syncNightLights() {
  if (!styleReady) return
  const on = shown.dark && useMapUi.getState().nightLights
  if (on && Date.now() - lampsAt > 5 * 60e3) loadLamps()
  const ambient = on && !lampLayerOnMap()
  for (const id of nightLayers) {
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
      const src = map.getSource("lamps") as GeoJSONSource | undefined
      if (src) src.setData(fc as any)
      else addAmbientLamps()
    })
    .catch(() => { lampsAt = 0 }) // try again on the next minute
}

function addAmbientLamps() {
  if (!lamps || map.getSource("lamps")) return
  map.addSource("lamps", { type: "geojson", data: lamps as any })
  const ids: string[] = []
  addLampGlow("lamps", "lamps", ["==", ["get", "state"], "on"], ids, glowBelow())
  syncNightLights()
}

// Light pools under the 3D buildings, so it shows in the streets between them.
function glowBelow() {
  return ["city3d::fill", "base-3d"].find((id) => map.getLayer(id))
}

function addLampGlow(prefix: string, src: string, lit: any, ids: string[], before?: string) {
  const points: any = ["all", ["==", ["geometry-type"], "Point"], lit]
  const visibility = shown.dark && useMapUi.getState().nightLights ? "visible" : "none"
  map.addLayer({
    id: `${prefix}-pool`, type: "heatmap", source: src, filter: points, layout: { visibility },
    paint: {
      "heatmap-weight": 1,
      "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 12, 1.4, 15, 1.1, 18, 0.9],
      // About 30 m of light round each lamp close up, a little more when zoomed out to stay seen.
      "heatmap-radius": ["interpolate", ["exponential", 2], ["zoom"], 12, 9, 14, 22, 15, 36, 17, 120, 19, 460],
      "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"],
        0, "rgba(255,160,60,0)", 0.1, "rgba(255,150,55,0.12)", 0.3, "rgba(255,165,70,0.3)",
        0.55, "rgba(255,190,105,0.46)", 0.8, "rgba(255,214,150,0.6)", 1, "rgba(255,236,200,0.72)"],
      "heatmap-opacity": 0.95,
    },
  }, before)
  map.addLayer({
    id: `${prefix}-bulb`, type: "circle", source: src, filter: points, minzoom: 13, layout: { visibility },
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 1, 16, 2.4, 19, 5],
      "circle-color": "#fff3d6",
      "circle-blur": 0.35,
    },
  }, before)
  for (const id of [`${prefix}-pool`, `${prefix}-bulb`]) { ids.push(id); nightLayers.add(id) }
}

// The space the floating panels cover, so the camera centres on the open part of the map.
export function setInsets(padding: { top: number; right: number; bottom: number; left: number }) {
  if (!map) return
  map.setPadding(padding)
}

export function resetView() {
  map.flyTo({ ...HOME, essential: true })
}

// "Where am I": the user's position, from the device (the locate button) or set by
// right-clicking the map. It goes to the assistant with each message, only once shared.

let geolocate: maplibregl.GeolocateControl
let meMarker: maplibregl.Marker | null = null

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

// The Venetian walls: a faint sodium ring that keeps the old town readable at any zoom.
function addWalls() {
  if (map.getSource("landmark-walls")) return
  map.addSource("landmark-walls", {
    type: "geojson",
    data: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: WALLS_RING } },
  })
  const night = useMapUi.getState().basemap === "night"
  const before = map.getStyle().layers.find((l) => l.type === "symbol")?.id
  map.addLayer({
    id: "landmark-walls-glow", type: "line", source: "landmark-walls",
    paint: { "line-color": SODIUM, "line-opacity": night ? 0.07 : 0.12, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 5, 17, 18], "line-blur": 4 },
  }, before)
  map.addLayer({
    id: "landmark-walls", type: "line", source: "landmark-walls",
    layout: { "line-join": "round" },
    paint: { "line-color": night ? SODIUM : "#b8741a", "line-opacity": night ? 0.42 : 0.6, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.8, 17, 2.4] },
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

function strokeColor() {
  return useMapUi.getState().basemap === "night" ? NIGHT : "#ffffff"
}

const boundPopups = new Set<string>() // map.on handlers outlive style changes, so bind once per id
function bindPopup(layerId: string) {
  if (boundPopups.has(layerId)) return
  boundPopups.add(layerId)
  map.on("click", layerId, (e) => {
    const f = e.features?.[0]
    if (!f) return
    const text = f.properties.popup || f.properties.label || f.properties.id
    new maplibregl.Popup({ offset: 10 }).setLngLat(e.lngLat).setHTML(escapeHtml(text).replace(/\n/g, "<br>")).addTo(map)
  })
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
  const points: any = ["==", ["geometry-type"], "Point"]
  const vf = spec.value_field ?? undefined
  const stroke = strokeColor()
  // A time-lapse keeps one scale across all frames, so heights and heat compare over time.
  entry.stats = vf ? (data.frames ? frameStats(data.frames, vf) : numericStats(data.features, vf)) : null

  if (spec.style === "columns_3d") {
    map.addSource(src, { type: "geojson", data: columnsData(entry) as any })
    addLayerSafe({
      id: `${ref}::columns`, type: "fill-extrusion", source: src,
      paint: { "fill-extrusion-color": ["get", "_c"], "fill-extrusion-height": ["get", "_h"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.9 },
    }, entry.ids)
    bindPopup(`${ref}::columns`)
    if (spec.fit === false && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 })
    return
  }

  map.addSource(src, { type: "geojson", data: data as any })

  if (spec.style === "buildings_3d") {
    // Real footprints at their DLS heights, plot outlines on the ground, and markers that
    // carry the layer when zoomed out (where the buildings are too small to see).
    addExtrusions(entry, src, 0)
    addLayerSafe({
      id: `${ref}::circle`, type: "circle", source: src, filter: points,
      paint: {
        "circle-color": ["get", "color"],
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.2, 12, 3.6, 15, 5, 17, 3.5],
        "circle-stroke-color": stroke, "circle-stroke-width": 1.1,
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
    addLayerSafe({
      id: `${ref}::heat`, type: "heatmap", source: src, filter: points,
      paint: {
        "heatmap-weight": weight,
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 11, 0.9, 16, 1.8],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 11, 18, 15, 38, 18, 70],
        "heatmap-opacity": 0.85,
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(37,99,235,0)", 0.15, "#3b82f6", 0.35, "#22c55e", 0.55, "#eab308", 0.75, "#f97316", 1, "#dc2626"],
      },
    }, entry.ids)
    addLayerSafe({
      id: `${ref}::dots`, type: "circle", source: src, filter: points, minzoom: 15.5,
      paint: { "circle-radius": 4, "circle-color": ["get", "color"], "circle-stroke-color": stroke, "circle-stroke-width": 1 },
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
    // A route avoiding dangerous buildings comes with the usual route: thinner, grey, still.
    const alt: any = ["==", ["get", "state"], "usual route"]
    addLayerSafe({ id: `${ref}::casing`, type: "line", source: src, filter: lines, layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": stroke, "line-width": ["case", alt, 6.5, 10], "line-opacity": 0.85 } }, entry.ids)
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
      "circle-stroke-color": stroke, "circle-stroke-width": 1.6, "circle-opacity": 0.95,
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

function columnsData(entry: Entry) {
  const vf = entry.spec.value_field!
  const st = entry.stats || { min: 0, max: 1 }
  const span = st.max - st.min || 1
  const pts = entry.data.features.filter((f) => f.geometry?.type === "Point")
  // Column width follows the layer's extent: 6 m in a street, up to 45 m city-wide.
  entry.half ??= Math.max(6, Math.min(45, extentMeters(pts) / 220))
  const half = entry.half
  return {
    type: "FeatureCollection",
    features: pts.map((f) => {
      const v = Number(f.properties[vf])
      const t = Number.isFinite(v) ? (v - st.min) / span : 0
      return {
        type: "Feature",
        geometry: { type: "Polygon", coordinates: squareAround(f.geometry.coordinates, half) },
        properties: { ...f.properties, _h: Number.isFinite(v) ? half * 2 + t * half * 40 : 0, _c: rampColor(t) },
      }
    }),
  }
}

function addLabels(entry: Entry, src: string, filter: any, offset: number) {
  const night = useMapUi.getState().basemap === "night"
  addLayerSafe({
    id: `${entry.spec.ref}::labels`, type: "symbol", source: src, filter,
    layout: { "text-field": ["get", "label"], "text-font": FONT_REGULAR, "text-size": 11.5, "text-offset": [0, offset], "text-anchor": "top", "text-max-width": 12 },
    paint: night
      ? { "text-color": "#e4ebf4", "text-halo-color": NIGHT, "text-halo-width": 1.6 }
      : { "text-color": "#1f2937", "text-halo-color": "#ffffff", "text-halo-width": 1.6 },
  }, entry.ids)
}

function removeFromMap(ref: string) {
  if (flight?.ref === ref) stopFlight()
  const entry = state.layers.get(ref)
  for (const id of entry?.ids || []) {
    if (map.getLayer(id)) map.removeLayer(id)
    state.pulses.delete(id)
    state.dashLayers.delete(id)
    state.hl3d.delete(id)
    nightLayers.delete(id)
  }
  if (map.getSource(`${ref}::src`)) map.removeSource(`${ref}::src`)
}

function publishLayers() {
  setUi({
    layers: [...state.layers.values()].map(({ spec, stats }) => ({
      ref: spec.ref, title: spec.title, count: spec.count, style: spec.style,
      legend: spec.legend || {}, stats, valueField: spec.value_field ?? null,
    })),
  })
  syncNightLights() // the city's own lamps make way for a street light layer
}

export function removeLayer(ref: string) {
  removeFromMap(ref)
  state.layers.delete(ref)
  if (state.timelapse?.ref === ref) stopTimelapse()
  if (ref === REGISTERS_REF) setUi({ registers: { on: false, status: "" } })
  syncCityClaims()
  publishLayers()
  syncHighlight()
}

export function clearMap() {
  for (const ref of [...state.layers.keys()]) removeFromMap(ref)
  state.layers.clear()
  setUi({ registers: { on: false, status: "" } })
  stopTimelapse()
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
  const entry: Entry = { spec, data, ids: [], stats: null }
  state.layers.set(spec.ref, entry)
  const tilts = spec.style === "columns_3d" || spec.style === "buildings_3d"
  const pitch = tilts ? Math.max(55, map.getPitch()) : map.getPitch()
  const askedAt = spec.askedAt ?? performance.now()
  const fc = data
  whenStyleReady(() => {
    addToMap(entry)
    // A camera move asked for alongside this layer (tools run in parallel, so just before it)
    // or after it, while it loaded, wins over fitting to it.
    if (spec.fit !== false && !(state.flownAt > askedAt - 3000)) fitTo(fc, pitch)
    else if (spec.style === "buildings_3d" && map.getZoom() >= 15 && map.getPitch() < 45) map.easeTo({ pitch: 55, duration: 900 })
    publishLayers()
    syncHighlight()
    if (fc.frames) startTimelapse(spec.ref)
  })
}

// ---------------------------------------------------------------------------------------------
// The DLS 3D city: every building in view at its real height (from the Department of Lands and
// Surveys), coloured by height. It replaces the base map's own 3D buildings while on. Buildings
// that a 'buildings_3d' layer redraws are hidden here (no z-fighting); an old-town block that
// only holds a register plot turns into a translucent ghost, so the plot shows inside it.

function cityPaint(opacity: number): any {
  return {
    "fill-extrusion-color": ["interpolate", ["linear"], ["get", "h"], ...cityHeights()],
    "fill-extrusion-height": ["get", "h"], "fill-extrusion-base": 0, "fill-extrusion-opacity": opacity,
  }
}

function addCityLayer() {
  if (!map.getSource(CITY_SRC)) {
    map.addSource(CITY_SRC, { type: "geojson", data: { type: "FeatureCollection", features: CITY.features } as any })
  }
  // Below any layer the assistant drew, so its buildings and markers stay on top.
  const before = map.getStyle().layers.find((l) => l.id.includes("::") && !l.id.startsWith("city3d::"))?.id
  if (!map.getLayer("city3d::fill")) map.addLayer({ id: "city3d::fill", type: "fill-extrusion", source: CITY_SRC, minzoom: CITY_MIN_ZOOM - 0.5, paint: cityPaint(0.92) }, before)
  if (!map.getLayer("city3d::ghost")) map.addLayer({ id: "city3d::ghost", type: "fill-extrusion", source: CITY_SRC, minzoom: CITY_MIN_ZOOM - 0.5, filter: ["in", ["id"], ["literal", []]], paint: cityPaint(0.28) }, before)
  setBaseBuildings(false)
  syncCityClaims()
  bindCityPopup()
}

function removeCityLayer() {
  for (const id of ["city3d::ghost", "city3d::fill"]) if (map.getLayer(id)) map.removeLayer(id)
  if (map.getSource(CITY_SRC)) map.removeSource(CITY_SRC)
  setBaseBuildings(true)
}

// The base map's own (OpenStreetMap) 3D buildings.
function setBaseBuildings(visible: boolean) {
  for (const l of map.getStyle()?.layers || []) {
    if (l.type === "fill-extrusion" && !l.id.includes("::")) map.setLayoutProperty(l.id, "visibility", visible ? "visible" : "none")
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
  map.setFilter("city3d::fill", ["!", ["in", ["id"], ["literal", [...claimed, ...ghosts]]]])
  map.setFilter("city3d::ghost", ["in", ["id"], ["literal", [...ghosts].filter((g) => !claimed.has(g))]])
}

function cityTilesInView() {
  if (map.getZoom() < CITY_MIN_ZOOM) return []
  const n = 2 ** 16
  const tx = (lon: number) => Math.floor(((lon + 180) / 360) * n)
  const ty = (lat: number) => Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n)
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
        if (add.length) (map.getSource(CITY_SRC) as GeoJSONSource | undefined)?.updateData({ add: add as any })
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
  if (CITY.on) {
    if (map.getZoom() < CITY_MIN_ZOOM) status = "Zoom in to see the 3D buildings"
    else if (CITY.inflight.size || CITY.queue.length) status = `Loading 3D buildings, ${CITY.inflight.size + CITY.queue.length} areas left`
    else status = `${CITY.features.length.toLocaleString()} DLS buildings loaded`
  }
  const cur = useMapUi.getState().city
  if (cur.on !== CITY.on || cur.status !== status) setUi({ city: { on: CITY.on, status } })
}

export function setCity3d(on: boolean) {
  CITY.on = on
  cityStatus()
  whenStyleReady(() => {
    if (on) {
      addCityLayer()
      if (map.getPitch() < 40 && map.getZoom() >= CITY_MIN_ZOOM) map.easeTo({ pitch: 55, duration: 900 })
      refreshCity()
    } else {
      CITY.queue = []
      removeCityLayer()
    }
    cityStatus()
  })
}

let cityPopupBound = false
function bindCityPopup() {
  if (cityPopupBound) return
  cityPopupBound = true
  map.on("click", "city3d::fill", (e) => {
    // An assistant layer under the pointer has its own popup.
    if (map.queryRenderedFeatures(e.point).some((f) => f.layer.id.includes("::") && !f.layer.id.startsWith("city3d::"))) return
    const f = e.features?.[0]
    if (!f) return
    const p = f.properties
    const lines = [`${p.t || "Building"}, DLS 3D #${f.id}`,
      `${p.h} m to the roof, eave ${p.e} m${p.f ? `, ${p.f} floors` : ""}`,
      [p.r, p.v ? `${Math.round(p.v).toLocaleString()} m³` : "", p.a ? `${Math.round(p.a).toLocaleString()} m² footprint` : ""].filter(Boolean).join(", ")]
    new maplibregl.Popup({ offset: 10 }).setLngLat(e.lngLat).setHTML(lines.filter(Boolean).map(escapeHtml).join("<br>")).addTo(map)
  })
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
// Time-lapse: a layer with `frames` replays per-frame states and values on its features.

function publishTimelapse() {
  const tl = state.timelapse
  const entry = tl && state.layers.get(tl.ref)
  if (!tl || !entry) { setUi({ timelapse: null }); return }
  const fr = entry.data.frames!
  setUi({
    timelapse: {
      ref: tl.ref, title: entry.spec.title, frame: tl.frame, frames: fr.times.length,
      time: fr.times[tl.frame], playing: tl.playing, speed: tl.speed,
    },
  })
}

function applyFrame(k: number) {
  const tl = state.timelapse
  const entry = tl && state.layers.get(tl.ref)
  if (!tl || !entry) return
  const fr = entry.data.frames!
  k = Math.max(0, Math.min(fr.times.length - 1, Math.round(k)))
  tl.frame = k
  const legend = entry.spec.legend || {}
  entry.data.features.forEach((f, j) => {
    const p = f.properties
    const st = fr.state_names[fr.states[k][j]]
    p.state = st
    p.color = legend[st] || "#a3a3a3"
    const parts: string[] = []
    for (const [field, rows] of Object.entries(fr.values)) {
      const v = rows[k][j]
      if (v === null || v === undefined) delete p[field]
      else { p[field] = v; parts.push(`${field.replace(/_/g, " ")} ${fmt(v)}`) }
    }
    p.popup = `${p.name || p.label}\n${st.replace(/_/g, " ")}${parts.length ? `, ${parts.join(", ")}` : ""}\n${fr.times[k]}`
  })
  const src = map.getSource(`${tl.ref}::src`) as GeoJSONSource | undefined
  if (src) src.setData((entry.spec.style === "columns_3d" ? columnsData(entry) : entry.data) as any)
  for (const c of state.charts) if (c.link.key === "frame" && c.link.layer === tl.ref) setChartHighlight(c, k)
  setTimeAll(frameMs(entry, k))
  for (const v of state.visuals) postToVisual(v, k, "frame")
  showSky(nicosiaWallToDate(fr.iso[k])) // the sun, the moon and the lamps of that moment
  publishTimelapse()
}

function startTimelapse(ref: string) {
  stopTimelapse()
  state.timelapse = { ref, frame: 0, playing: false, speed: 1 }
  applyFrame(0)
  playTimelapse()
}

export function stopTimelapse() {
  if (!state.timelapse) return
  clearTimeout(state.timelapse.timer)
  state.timelapse = null
  publishTimelapse()
  setTimeAll(nicosiaNowMs()) // back to the "now" line
  showSky(new Date()) // and back to tonight's (or today's) sky
}

export function playTimelapse() {
  const tl = state.timelapse
  if (!tl) return
  const last = state.layers.get(tl.ref)!.data.frames!.times.length - 1
  if (tl.frame >= last) applyFrame(0)
  tl.playing = true
  const step = () => {
    if (!tl.playing || state.timelapse !== tl) return
    if (tl.frame >= last) { pauseTimelapse(); return }
    applyFrame(tl.frame + 1)
    tl.timer = window.setTimeout(step, 650 / tl.speed)
  }
  clearTimeout(tl.timer)
  tl.timer = window.setTimeout(step, 650 / tl.speed)
  publishTimelapse()
}

export function pauseTimelapse() {
  const tl = state.timelapse
  if (!tl) return
  tl.playing = false
  clearTimeout(tl.timer)
  publishTimelapse()
}

export function seekFrame(k: number) {
  pauseTimelapse()
  applyFrame(k)
}

export function cycleSpeed() {
  const tl = state.timelapse
  if (!tl) return
  tl.speed = SPEEDS[(SPEEDS.indexOf(tl.speed) + 1) % SPEEDS.length]
  publishTimelapse()
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
    if (state.timelapse && value !== null && value !== undefined && Number.isFinite(Number(value))) {
      pauseTimelapse()
      applyFrame(Number(value))
    }
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
  const tl = state.timelapse
  if (link.key === "frame" && tl && tl.ref === link.layer) setChartHighlight(c, tl.frame)
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

export function timelapseFrameOf(layer: string) {
  return state.timelapse?.ref === layer ? state.timelapse.frame : null
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
    if (state.timelapse) postToVisual(v, state.timelapse.frame, "frame")
    if (hlValue !== null) postToVisual(v, hlValue, "id")
  } else if (m.type === "highlight") {
    const val = m.value
    if (val === null || typeof val === "string" || typeof val === "number") {
      linkHover(typeof val === "number" ? "frame" : "id", val, v)
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Time sync: every chart over real time has a "now" rule at its `_t` param. While a time-lapse
// plays, `_t` is the frame's moment; otherwise it is now, moved on every 30 seconds. Times are
// Nicosia wall-clock strings read as browser-local, the same way Vega reads the data.

export function nicosiaNowMs() {
  const s = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Nicosia", hour12: false })
  return Date.parse(s.replace(" ", "T")) // "2026-10-20 14:05:33" -> local
}

function frameMs(entry: Entry, k: number) {
  return Date.parse(entry.data.frames!.iso[k])
}

function currentTimeMs() {
  const tl = state.timelapse
  const entry = tl && state.layers.get(tl.ref)
  return tl && entry ? frameMs(entry, tl.frame) : nicosiaNowMs()
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

// Jump the time-lapse to the frame nearest a moment, if the moment is within it.
export function seekTimelapse(ms: number) {
  const tl = state.timelapse
  const entry = tl && state.layers.get(tl.ref)
  if (!tl || !entry) return
  const n = entry.data.frames!.iso.length
  let best = 0
  for (let k = 1; k < n; k++) if (Math.abs(frameMs(entry, k) - ms) < Math.abs(frameMs(entry, best) - ms)) best = k
  const step = n > 1 ? frameMs(entry, 1) - frameMs(entry, 0) : Infinity
  if (Math.abs(frameMs(entry, best) - ms) > step) return
  if (best !== tl.frame) { pauseTimelapse(); applyFrame(best) }
}

export function hasTimelapse() {
  return state.timelapse !== null
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
    const el = document.createElement("div")
    el.className = "callout"
    el.innerHTML = `<b>${escapeHtml(it.title)}</b>${it.text ? escapeHtml(it.text) : ""}`
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
  stopFlight()
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
// Commands from the assistant

export function applyMapUi(ev: any, onError: (text: string) => void) {
  switch (ev.action) {
    case "show_layer":
      showLayer({ ...ev.layer, askedAt: performance.now() }).catch((e) => onError(`Couldn't draw layer: ${e.message}`))
      break
    case "fly_to": {
      state.flownAt = performance.now()
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

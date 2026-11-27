/* Live mobility on the map: the buses gliding along their routes, the stops, and the roads
   coloured by speed against their free flow.

   Buses. The server knows each bus's place along its trip's route shape (metres) and its speed.
   Between reports the bus moves on along the shape at that speed (for at most 2 minutes),
   and its marker eases toward that point, so it glides along the street instead of jumping
   between fixes or cutting across blocks. A bus never slides backwards: when a fix lands a
   little behind where it was drawn, it waits there until the bus catches up. With reduced
   motion each fix is shown as it comes. Coloured by line (the operator's colour, lifted for the
   night map). From near city zoom each bus is a small 3D bus facing where it heads
   (lib/mobility/bus-model.ts, loaded when buses are first shown), with its line number in a pill
   just above it; farther out, and wherever the model can't be drawn, a dot with an arrow.

   Roads. Each Bluetooth segment runs one way; it is drawn offset to the left of its direction
   (Cyprus drives on the left), so the two directions of an avenue sit side by side. A neutral
   for roads that flow, one warm hue brightening as they slow, and wider lines the slower they
   are, so colour never carries it alone. */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"

import { useClock } from "@/lib/clock"
import { useInspector } from "@/lib/inspector"
import {
  firstLabelLayer, flyToBounds, getMap, onStyleReady, registerLayerRenderer, setSelection, themedPaint,
  type LayerData,
} from "@/lib/map/engine"
import {
  fetchBuses, fetchRoads, fetchShape, fetchStops, setBusState, setRoadState, useMobility, type BusFilter,
  type BusesBody, type LiveBus, type RoadsBody,
} from "@/lib/mobility/store"
import { registerClickHandler } from "@/lib/registries/map-clicks"

/* eslint-disable @typescript-eslint/no-explicit-any */

const BUS_SRC = "mob::buses"
const STOP_SRC = "mob::stops"
const ROAD_SRC = "mob::roads"
const POLL_MS = 15_000 // the server fetches the feed every 30 s; this catches each new one soon
const ROAD_POLL_MS = 120_000
const FRAME_MS = 66
const MAX_AHEAD_S = 120
const EASE_S = 1.2
const NIGHT = "#0e1828"
const FONT_BOLD = ["Noto Sans Bold"]
const FONT_REGULAR = ["Noto Sans Regular"]

// Speed bands, stepped for each base map (validated as one-hue ordinal ramps on both).
export const ROAD_COLORS: Record<"night" | "day", Record<string, string>> = {
  night: { moving: "#718295", busy: "#bd6350", slow: "#f17458", very_slow: "#ff9a71", no_reading: "#424e5e" },
  day: { moving: "#798898", busy: "#db795b", slow: "#cb4930", very_slow: "#a9231e", no_reading: "#b4bcc8" },
}
// While the feed is stalled: each road's usual for this hour, dotted in the muted ink, so it
// can't pass for the live ramp. Wider is usually slower.
export const USUAL_COLORS = { night: "#8798b0", day: "#64748b" } as const
export const ROAD_BANDS = [
  { state: "moving", label: "Moving freely", hint: "75% of free flow or more" },
  { state: "busy", label: "Busy", hint: "55% to 75%" },
  { state: "slow", label: "Slow", hint: "35% to 55%" },
  { state: "very_slow", label: "Very slow", hint: "under 35%" },
] as const

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches

// -- route shapes -------------------------------------------------------------------------------

type Shape = { coords: [number, number][]; cum: Float64Array; length: number }
const shapes = new Map<string, Shape | "loading" | "missing">()

function kx(lat: number) {
  return 111_320 * Math.cos((lat * Math.PI) / 180)
}

function buildShape(coords: [number, number][]): Shape {
  const cum = new Float64Array(coords.length)
  for (let i = 1; i < coords.length; i++) {
    const [x1, y1] = coords[i - 1], [x2, y2] = coords[i]
    cum[i] = cum[i - 1] + Math.hypot((x2 - x1) * kx((y1 + y2) / 2), (y2 - y1) * 110_574)
  }
  return { coords, cum, length: cum[cum.length - 1] ?? 0 }
}

function pointAt(s: Shape, along: number): [number, number, number] {
  const { coords, cum } = s
  if (coords.length < 2) return [coords[0][0], coords[0][1], 0]
  const a = Math.max(0, Math.min(along, s.length))
  let lo = 0, hi = cum.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (cum[mid] <= a) lo = mid
    else hi = mid
  }
  const span = cum[hi] - cum[lo]
  const t = span ? (a - cum[lo]) / span : 0
  const [x1, y1] = coords[lo], [x2, y2] = coords[hi]
  const heading = (Math.atan2((x2 - x1) * kx((y1 + y2) / 2), (y2 - y1) * 110_574) * 180) / Math.PI
  return [x1 + t * (x2 - x1), y1 + t * (y2 - y1), (heading + 360) % 360]
}

function wantShape(id: string) {
  if (shapes.has(id)) return
  shapes.set(id, "loading")
  fetchShape(id)
    .then((s) => shapes.set(id, buildShape(s.coordinates)))
    .catch(() => shapes.set(id, "missing"))
}

// -- buses ----------------------------------------------------------------------------------------

type Track = { bus: LiveBus; along: number | null; pos: [number, number]; heading: number }
const tracks = new Map<string, Track>()
const answerFilters = new Map<string, BusFilter>() // answer layer ref -> what it asked about
let busTimer: number | undefined
let busCtrl: AbortController | null = null
let lastFrame = 0
let lastFollow = 0

function busDemand() {
  const { buses } = useMobility.getState()
  const open = useInspector.getState().entity
  return buses.on || answerFilters.size > 0 || (open?.kind as string) === "bus"
}

function activeFilter(): BusFilter | null {
  const all = [...answerFilters.values()]
  return all.length ? all[all.length - 1] : null
}

function ingest(body: BusesBody) {
  const seen = new Set<string>()
  for (const b of body.buses) {
    seen.add(b.id)
    if (b.shape_id) wantShape(b.shape_id)
    const t = tracks.get(b.id)
    if (!t) {
      tracks.set(b.id, { bus: b, along: b.along_m ?? null, pos: [b.lon, b.lat], heading: b.heading ?? 0 })
      continue
    }
    if (t.bus.shape_id !== b.shape_id) t.along = b.along_m ?? null // a new trip: start it where it is
    t.bus = b
  }
  for (const id of [...tracks.keys()]) if (!seen.has(id)) tracks.delete(id)
  setBusState({ status: body.status, error: body.error, count: body.buses.filter((b) => !b.finished).length, fetchedAt: body.fetched_at })
}

async function pollBuses() {
  window.clearTimeout(busTimer)
  if (!busDemand()) return
  if (!document.hidden) {
    busCtrl?.abort()
    const mine = (busCtrl = new AbortController())
    if (useMobility.getState().buses.status === "idle") setBusState({ status: "loading" })
    try {
      ingest(await fetchBuses(mine.signal))
    } catch (e) {
      if ((e as Error).name !== "AbortError") setBusState({ status: tracks.size ? "stale" : "unavailable", error: (e as Error).message })
    }
  }
  busTimer = window.setTimeout(pollBuses, POLL_MS)
}

function step(t: Track, now: number, dt: number) {
  const b = t.bus
  const shape = b.shape_id ? shapes.get(b.shape_id) : undefined
  if (shape && typeof shape !== "string" && b.along_m !== undefined) {
    const v = b.finished || b.waiting ? 0 : (b.speed_kmh ?? 0) / 3.6
    const ahead = Math.max(0, Math.min(MAX_AHEAD_S, (now - b.reported * 1000) / 1000))
    const target = Math.min(shape.length, b.along_m + v * ahead)
    if (t.along === null || reducedMotion()) t.along = reducedMotion() ? b.along_m : target
    else {
      const diff = target - t.along
      if (diff < -250 || diff > 2500) t.along = target // a jump: a new fix far away
      else if (diff > 0) t.along += diff * Math.min(1, dt / EASE_S)
      // a little behind: hold, never slide backwards
    }
    const [lon, lat, heading] = pointAt(shape, t.along)
    t.pos = [lon, lat]
    t.heading = heading
    return
  }
  const k = reducedMotion() ? 1 : Math.min(1, dt / EASE_S)
  t.pos = [t.pos[0] + (b.lon - t.pos[0]) * k, t.pos[1] + (b.lat - t.pos[1]) * k]
  if (b.heading !== undefined) t.heading = b.heading
}

function matches(b: LiveBus, f: BusFilter | null) {
  if (!f) return true
  if (f.lines && !(b.line_id && f.lines.includes(b.line_id))) return false
  if (f.near) {
    const d = Math.hypot((b.lon - f.near.lon) * kx(f.near.lat), (b.lat - f.near.lat) * 110_574)
    if (d > f.near.radius_m) return false
  }
  return true
}

function openBusId() {
  const { open, entity } = useInspector.getState()
  return open && (entity?.kind as string) === "bus" ? entity!.id : null
}

function busFeatures(): any {
  const f = activeFilter()
  const features: any[] = []
  // The live feed is only now: while the clock shows the past, the buses step aside.
  if (useClock.getState().at !== null) return { type: "FeatureCollection", features }
  const openId = openBusId()
  for (const t of tracks.values()) {
    const b = t.bus
    if (b.finished || !matches(b, f)) continue
    const color = b.color || "#8798b0"
    features.push({
      type: "Feature", geometry: { type: "Point", coordinates: t.pos },
      properties: { id: b.id, line: b.line, color, text_color: b.text_color || NIGHT, heading: Math.round(t.heading),
                    pill: `mob-pill-${color}`, arrow: `mob-arrow-${color}`, open: b.id === openId },
    })
  }
  return { type: "FeatureCollection", features }
}

function frame(ts: number) {
  requestAnimationFrame(frame)
  const map = getMap()
  if (!map || ts - lastFrame < FRAME_MS) return
  const dt = lastFrame ? Math.min(1, (ts - lastFrame) / 1000) : 0
  lastFrame = ts
  if (!tracks.size) return
  const now = Date.now()
  for (const t of tracks.values()) step(t, now, dt)
  const fc = busFeatures()
  const src = map.getSource(BUS_SRC) as GeoJSONSource | undefined
  if (src) src.setData(fc)
  if (modelState === "ready" && map.getLayer(BUS_MODEL)) {
    busModel!.setBuses(fc.features.map((ft: any) => {
      const p = ft.properties
      return { id: p.id, lon: ft.geometry.coordinates[0], lat: ft.geometry.coordinates[1], heading: p.heading, color: p.color, open: p.open }
    }))
  }
  followOpenBus(ts)
}

// -- buses in 3D ----------------------------------------------------------------------------------
// The model is loaded the first time buses are shown. Until it is ready, and for good if it
// fails, the dots and arrows below are the buses. Once it draws, the dot stays as the bus's far
// zoom look and, transparent and as big as the model, as what a click or the pointer finds.

type BusModelModule = typeof import("@/lib/mobility/bus-model")
const BUS_MODEL = "mob::bus-model"
let busModel: import("@/lib/mobility/bus-model").BusModel | null = null
let modelMod: BusModelModule | null = null
let modelState: "off" | "loading" | "ready" | "failed" = "off"

function wantBusModel(map: MlMap) {
  if (modelState === "ready") placeBusModel(map)
  if (modelState !== "off") return
  modelState = "loading"
  import("@/lib/mobility/bus-model")
    .then(async (mod) => {
      modelMod = mod
      busModel = await mod.loadBusModel(map, (why) => busModelFailed(map, why))
      modelState = "ready"
      placeBusModel(map)
    })
    .catch((e) => busModelFailed(map, (e as Error).message))
}

function busModelFailed(map: MlMap, why: string) {
  if (modelState === "failed") return
  console.warn(`3D buses off: ${why}`)
  modelState = "failed"
  if (map.getLayer(BUS_MODEL)) map.removeLayer(BUS_MODEL)
  lookOfMarkers(map)
}

function placeBusModel(map: MlMap) {
  if (modelState !== "ready" || !busModel || !map.getLayer("mob::bus-dot") || map.getLayer(BUS_MODEL)) return
  map.addLayer(busModel.layer, "mob::bus-arrow") // over the dots, under the arrows and the numbers
  lookOfMarkers(map)
}

// The dots, arrows and pills as the buses are drawn: flat markers, or around the 3D model.
function lookOfMarkers(map: MlMap) {
  if (!map.getLayer("mob::bus-dot")) return
  const model = modelState === "ready" && !!modelMod && !!map.getLayer(BUS_MODEL)
  if (!model) {
    map.setPaintProperty("mob::bus-dot", "circle-opacity", 1)
    map.setPaintProperty("mob::bus-dot", "circle-stroke-opacity", 1)
    map.setPaintProperty("mob::bus-dot", "circle-radius", DOT_RADIUS)
    map.setLayoutProperty("mob::bus-arrow", "visibility", "visible")
    map.setLayoutProperty("mob::bus-label", "text-offset", [0, 0])
    map.setLayerZoomRange("mob::bus-label", 14.5, 24)
    return
  }
  const { SHOW_ZOOM, busScreenLength } = modelMod!
  const zooms = [SHOW_ZOOM, 14, 14.5, 15, 15.5, 16, 16.5, 17, 17.5, 18, 18.5, 19, 19.5, 20, 21, 22]
  const fade: any = ["interpolate", ["linear"], ["zoom"], SHOW_ZOOM, 1, SHOW_ZOOM + 0.4, 0]
  map.setPaintProperty("mob::bus-dot", "circle-opacity", fade)
  map.setPaintProperty("mob::bus-dot", "circle-stroke-opacity", fade)
  // As big as the bus on screen (half its length), so a click anywhere on it finds it.
  map.setPaintProperty("mob::bus-dot", "circle-radius",
    ["interpolate", ["linear"], ["zoom"], 10, 2.5, 13, 4.5, ...zooms.flatMap((z) => [z, Math.max(5, busScreenLength(z) / 2)])])
  map.setLayoutProperty("mob::bus-arrow", "visibility", "none")
  // The number floats just over the bus: half its length (as much as it can reach upward on
  // screen, from any pitch and bearing), then half the pill and a gap. In ems of 12 px.
  map.setLayoutProperty("mob::bus-label", "text-offset",
    ["interpolate", ["linear"], ["zoom"], ...zooms.flatMap((z) => [z, ["literal", [0, -(busScreenLength(z) / 2 + 13) / 12]]])])
  map.setLayerZoomRange("mob::bus-label", SHOW_ZOOM, 24)
}

// The open bus keeps its sodium ring as it moves.
function followOpenBus(ts: number) {
  if (ts - lastFollow < 250) return
  lastFollow = ts
  const { open, entity } = useInspector.getState()
  if (!open || (entity?.kind as string) !== "bus") return
  const t = tracks.get(entity!.id)
  if (t) setSelection({ center: t.pos, geometry: entity!.geometry })
}

// -- images -----------------------------------------------------------------------------------

function canvas(w: number, h: number) {
  const c = document.createElement("canvas")
  c.width = w
  c.height = h
  return [c, c.getContext("2d")!] as const
}

function pill(color: string) {
  const [c, g] = canvas(44, 36)
  g.beginPath()
  g.roundRect(2, 2, 40, 32, 9)
  g.fillStyle = color
  g.fill()
  g.lineWidth = 3
  g.strokeStyle = NIGHT
  g.stroke()
  return g.getImageData(0, 0, c.width, c.height)
}

function arrow(color: string) {
  const [c, g] = canvas(28, 28)
  g.beginPath()
  g.moveTo(14, 3)
  g.lineTo(24, 23)
  g.lineTo(14, 17)
  g.lineTo(4, 23)
  g.closePath()
  g.fillStyle = color
  g.fill()
  g.lineJoin = "round"
  g.lineWidth = 2.5
  g.strokeStyle = NIGHT
  g.stroke()
  return g.getImageData(0, 0, c.width, c.height)
}

function onMissingImage(map: MlMap, id: string) {
  if (map.hasImage(id)) return
  if (id.startsWith("mob-pill-")) {
    map.addImage(id, pill(id.slice(9)), { pixelRatio: 2, stretchX: [[14, 30]], stretchY: [[13, 23]], content: [10, 7, 34, 29] })
  } else if (id.startsWith("mob-arrow-")) {
    map.addImage(id, arrow(id.slice(10)), { pixelRatio: 2 })
  }
}

// -- layers -------------------------------------------------------------------------------------

function cursorOn(map: MlMap, id: string) {
  map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", id, () => { map.getCanvas().style.cursor = "" })
}

const bound = new Set<string>()
function bindOnce(map: MlMap, id: string) {
  if (bound.has(id)) return
  bound.add(id)
  cursorOn(map, id)
}

const DOT_RADIUS: any = ["interpolate", ["linear"], ["zoom"], 10, 2.5, 13, 4.5, 15, 6]

function addBusLayers(map: MlMap) {
  if (map.getSource(BUS_SRC)) { wantBusModel(map); return }
  map.addSource(BUS_SRC, { type: "geojson", data: busFeatures() })
  map.addLayer({
    id: "mob::bus-dot", type: "circle", source: BUS_SRC,
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": DOT_RADIUS,
      "circle-stroke-color": NIGHT, "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 0.8, 14, 1.5],
    },
  })
  map.addLayer({
    id: "mob::bus-arrow", type: "symbol", source: BUS_SRC, minzoom: 11.5,
    layout: {
      "icon-image": ["get", "arrow"], "icon-rotate": ["get", "heading"], "icon-rotation-alignment": "map",
      "icon-allow-overlap": true, "icon-ignore-placement": true,
      "icon-size": ["interpolate", ["linear"], ["zoom"], 11.5, 0.55, 14, 0.8, 16, 1],
      "icon-offset": ["step", ["zoom"], ["literal", [0, -12]], 14.5, ["literal", [0, -21]]],
    },
  })
  map.addLayer({
    id: "mob::bus-label", type: "symbol", source: BUS_SRC, minzoom: 14.5,
    layout: {
      "text-field": ["get", "line"], "text-font": FONT_BOLD, "text-size": 12,
      "icon-image": ["get", "pill"], "icon-text-fit": "both", "icon-text-fit-padding": [1, 3, 1, 3],
      // Never on top of each other: where they crowd, the open bus's number is placed first.
      "text-allow-overlap": false, "icon-allow-overlap": false,
      "symbol-sort-key": ["case", ["boolean", ["get", "open"], false], -1, ["get", "heading"]],
    },
    paint: { "text-color": ["get", "text_color"] },
  })
  for (const id of ["mob::bus-dot", "mob::bus-arrow", "mob::bus-label"]) bindOnce(map, id)
  wantBusModel(map)
}

function removeBusLayers(map: MlMap) {
  for (const id of [BUS_MODEL, "mob::bus-label", "mob::bus-arrow", "mob::bus-dot", "mob::stop-name", "mob::stop"]) if (map.getLayer(id)) map.removeLayer(id)
  for (const id of [BUS_SRC, STOP_SRC]) if (map.getSource(id)) map.removeSource(id)
}

let stopsData: any = null
function addStopLayers(map: MlMap) {
  if (map.getSource(STOP_SRC) || !stopsData) return
  map.addSource(STOP_SRC, { type: "geojson", data: stopsData })
  const before = map.getLayer("mob::bus-dot") ? "mob::bus-dot" : undefined
  map.addLayer({
    id: "mob::stop", type: "circle", source: STOP_SRC, minzoom: 14.5,
    paint: {
      ...themedPaint("mob::stop", { "circle-color": { night: NIGHT, day: "#ffffff" }, "circle-stroke-color": { night: "#e4ebf4", day: "#334155" } }),
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 14.5, 2.5, 17, 4.5],
      "circle-stroke-width": 1.4,
      "circle-opacity": ["interpolate", ["linear"], ["zoom"], 14.5, 0, 15, 1],
      "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 14.5, 0, 15, 1],
    },
  }, before)
  map.addLayer({
    id: "mob::stop-name", type: "symbol", source: STOP_SRC, minzoom: 16.5,
    layout: { "text-field": ["get", "name"], "text-font": FONT_REGULAR, "text-size": 11, "text-offset": [0, 1.1], "text-anchor": "top", "text-optional": true },
    paint: { ...themedPaint("mob::stop-name", { "text-color": { night: "#8798b0", day: "#475569" }, "text-halo-color": { night: NIGHT, day: "#ffffff" } }), "text-halo-width": 1.2 },
  }, before)
  bindOnce(map, "mob::stop")
}

function syncBuses() {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    if (busDemand()) {
      addBusLayers(map)
      if (!stopsData) fetchStops().then((fc) => { stopsData = fc; if (busDemand()) addStopLayers(map) }).catch(() => {})
      else addStopLayers(map)
      void pollBuses()
    } else {
      removeBusLayers(map)
      window.clearTimeout(busTimer)
      busCtrl?.abort()
      tracks.clear()
      setBusState({ status: "idle", count: 0 })
    }
  })
}

export function setBusesOn(on: boolean) {
  setBusState({ on })
  syncBuses()
}

// -- roads --------------------------------------------------------------------------------------

const FACTOR: any = ["match", ["get", "state"], "moving", 1, "busy", 1.2, "slow", 1.45, "very_slow", 1.75, 1]
// Wider the slower; the casing is the same line 2 to 3 px wider underneath, so it reads on any street.
const WIDTH: any = ["interpolate", ["linear"], ["zoom"], 11, ["*", 1.3, FACTOR], 14, ["*", 2.8, FACTOR], 16, ["*", 4.5, FACTOR], 18, ["*", 7, FACTOR]]
const CASING: any = ["interpolate", ["linear"], ["zoom"], 11, ["+", 2, ["*", 1.3, FACTOR]], 14, ["+", 2.5, ["*", 2.8, FACTOR]], 16, ["+", 3, ["*", 4.5, FACTOR]], 18, ["+", 3, ["*", 7, FACTOR]]]
// Left of the direction of travel: positive line-offset is to the right, so negative.
const OFFSET: any = ["interpolate", ["linear"], ["zoom"], 11, -1.2, 14, -2.8, 16, -4.6, 18, -7.5]

function roadColor(base: "night" | "day"): any {
  const c = ROAD_COLORS[base]
  return ["match", ["get", "state"], "moving", c.moving, "busy", c.busy, "slow", c.slow, "very_slow", c.very_slow, c.no_reading]
}

const USUAL_FACTOR: any = ["match", ["get", "usual_state"], "busy", 1.2, "slow", 1.45, "very_slow", 1.75, 1]
const USUAL_WIDTH: any = ["interpolate", ["linear"], ["zoom"], 11, ["*", 1.3, USUAL_FACTOR], 14, ["*", 2.6, USUAL_FACTOR], 16, ["*", 4, USUAL_FACTOR], 18, ["*", 6, USUAL_FACTOR]]

function addRoadLayers(map: MlMap, prefix: string, src: string, ids: string[]) {
  const before = firstLabelLayer()
  const measured: any = ["!=", ["get", "state"], "no_reading"]
  map.addLayer({
    id: `${prefix}-none`, type: "line", source: src, filter: ["all", ["==", ["get", "state"], "no_reading"], ["!", ["has", "usual_state"]]],
    layout: { "line-cap": "butt", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-none`, { "line-color": { night: ROAD_COLORS.night.no_reading, day: ROAD_COLORS.day.no_reading } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 16, 2.5], "line-offset": OFFSET, "line-dasharray": [2, 2] },
  }, before)
  // A stalled feed: the usual for this hour, as round dots (never the live ramp, never solid).
  map.addLayer({
    id: `${prefix}-usual`, type: "line", source: src, filter: ["has", "usual_state"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-usual`, { "line-color": { night: USUAL_COLORS.night, day: USUAL_COLORS.day } }),
             "line-width": USUAL_WIDTH, "line-offset": OFFSET, "line-dasharray": [0, 2], "line-opacity": 0.9 },
  }, before)
  map.addLayer({
    id: `${prefix}-casing`, type: "line", source: src, filter: measured,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-casing`, { "line-color": { night: NIGHT, day: "#ffffff" } }), "line-width": CASING,
             "line-offset": OFFSET, "line-opacity": 0.9 },
  }, before)
  map.addLayer({
    id: `${prefix}-line`, type: "line", source: src, filter: measured,
    layout: { "line-cap": "round", "line-join": "round", "line-sort-key": ["match", ["get", "state"], "very_slow", 4, "slow", 3, "busy", 2, 1] },
    paint: { ...themedPaint(`${prefix}-line`, { "line-color": { night: roadColor("night"), day: roadColor("day") } }),
             "line-width": WIDTH, "line-offset": OFFSET },
  }, before)
  for (const id of [`${prefix}-none`, `${prefix}-usual`, `${prefix}-casing`, `${prefix}-line`]) {
    ids.push(id)
    bindOnce(map, id)
  }
}

const ROAD_LAYERS = ["mob::roads-none", "mob::roads-usual", "mob::roads-casing", "mob::roads-line"]

let roadTimer: number | undefined
let roadCtrl: AbortController | null = null
const answerRoads = new Set<string>() // answer layers that asked for the live roads
const pastRoads = new Set<string>() // answer layers that draw a recorded moment themselves
let roadMoment: string | null = null // the city clock's moment, when it isn't now

function roadDemand() {
  const open = useInspector.getState().entity
  return useMobility.getState().roads.on || answerRoads.size > 0 || (open?.kind as string) === "road_segment"
}

async function pollRoads() {
  window.clearTimeout(roadTimer)
  if (!roadDemand()) return
  const map = getMap()
  if (!document.hidden && map) {
    roadCtrl?.abort()
    const mine = (roadCtrl = new AbortController())
    if (useMobility.getState().roads.status === "idle") setRoadState({ status: "loading" })
    try {
      const fc: RoadsBody = await fetchRoads(roadMoment, mine.signal)
      const src = map.getSource(ROAD_SRC) as GeoJSONSource | undefined
      src?.setData(fc as any)
      setRoadState({ status: fc.meta.status as any, error: fc.meta.error, measured: fc.meta.measured, total: fc.meta.total,
                     at: fc.meta.at, freeFlow: fc.meta.free_flow,
                     lastMeasuredAt: fc.meta.last_measured_at ?? null, usual: fc.meta.usual ?? 0 })
    } catch (e) {
      if ((e as Error).name !== "AbortError") setRoadState({ status: "unavailable", error: (e as Error).message })
    }
  }
  if (!roadMoment) roadTimer = window.setTimeout(pollRoads, ROAD_POLL_MS)
}

function syncRoads() {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    const ids: string[] = []
    if (roadDemand()) {
      if (!map.getSource(ROAD_SRC)) {
        map.addSource(ROAD_SRC, { type: "geojson", data: { type: "FeatureCollection", features: [] } as any })
        addRoadLayers(map, "mob::roads", ROAD_SRC, ids)
      }
      const visible = pastRoads.size ? "none" : "visible" // a recorded moment on the map stands in for now
      for (const id of ROAD_LAYERS) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible)
      void pollRoads()
    } else {
      for (const id of [...ROAD_LAYERS].reverse()) if (map.getLayer(id)) map.removeLayer(id)
      if (map.getSource(ROAD_SRC)) map.removeSource(ROAD_SRC)
      window.clearTimeout(roadTimer)
      roadCtrl?.abort()
      setRoadState({ status: "idle" })
    }
  })
}

export function setRoadsOn(on: boolean) {
  setRoadState({ on })
  syncRoads()
}

// For the city clock: road speeds as the twin's memory recorded them at a moment (ISO), or now.
export function setRoadMoment(at: string | null) {
  if (at === roadMoment) return
  roadMoment = at
  if (roadDemand()) void pollRoads()
}

// -- answers --------------------------------------------------------------------------------------

registerLayerRenderer({
  id: "live-buses",
  claims: (d: LayerData) => d.meta?.live?.kind === "buses",
  add: (ref, _src, data) => {
    const live = data.meta.live
    answerFilters.set(ref, { lines: live.lines ?? null, near: live.near ?? null })
    syncBuses()
  },
  remove: (ref) => {
    answerFilters.delete(ref)
    syncBuses()
  },
})

registerLayerRenderer({
  id: "road-speeds",
  claims: (d: LayerData) => d.meta?.live?.kind === "roads",
  add: (ref, src, data, ids) => {
    const map = getMap()
    if (data.meta.live.at) {
      // A moment from the memory: drawn from the answer's own data, in place of now.
      pastRoads.add(ref)
      addRoadLayers(map, `${ref}::mob-roads`, src, ids)
    } else answerRoads.add(ref)
    syncRoads()
  },
  remove: (ref) => {
    answerRoads.delete(ref)
    pastRoads.delete(ref)
    syncRoads()
  },
})

// -- clicks ---------------------------------------------------------------------------------------

registerClickHandler({
  id: "mobility",
  priority: 12,
  layers: (l) => l.startsWith("mob::") || l.includes("::mob-roads"),
  resolve: (f, at) => {
    const id = f.properties?.id
    if (!id) return null
    const center = f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
    return { id: String(id), at: center }
  },
})

// -- the Mobility workspace's view ------------------------------------------------------------------

let before: { buses: boolean; roads: boolean } | null = null

export function enterMobilityView() {
  const s = useMobility.getState()
  before = { buses: s.buses.on, roads: s.roads.on }
  setBusesOn(true)
  setRoadsOn(true)
  if (getMap()) flyToBounds([33.318, 35.125, 33.395, 35.178]) // the measured avenues and the centre
}

export function leaveMobilityView() {
  if (!before) return
  setBusesOn(before.buses)
  setRoadsOn(before.roads)
  before = null
}

// -- start ----------------------------------------------------------------------------------------

let started = false
export function startMobilityMap() {
  if (started) return
  started = true
  requestAnimationFrame(frame)
  const wait = () => {
    const map = getMap()
    if (!map) { window.setTimeout(wait, 200); return }
    map.on("styleimagemissing", (e: any) => onMissingImage(map, e.id))
  }
  wait()
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return
    if (busDemand()) void pollBuses()
    if (roadDemand()) void pollRoads()
  })
  // Road speeds follow the city clock (read from the twin's memory at its moment); the buses
  // hide while it is in the past. A moment that keeps moving (a replay) is read once it rests.
  let clockTimer: number | undefined
  useClock.subscribe((st, prev) => {
    if (st.at === prev.at) return
    window.clearTimeout(clockTimer)
    clockTimer = window.setTimeout(() => setRoadMoment(st.at === null ? null : String(st.at)), st.playing ? 600 : 150)
    setBusState({ paused: st.at !== null })
  })
  // Opening a bus or a road brings its live layer with it, and closing it lets it go again.
  let kind: string | undefined
  useInspector.subscribe((st) => {
    const k = st.open ? (st.entity?.kind as string | undefined) : undefined
    if (k === kind) return
    const was = kind
    kind = k
    if (k === "bus" || was === "bus") syncBuses()
    if (k === "road_segment" || was === "road_segment") syncRoads()
  })
}

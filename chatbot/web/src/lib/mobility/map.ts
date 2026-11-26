/* Live mobility on the map: the buses gliding along their routes, the stops, and the roads
   coloured by speed against their free flow.

   Buses. The server knows each bus's place along its trip's route shape (metres) and its speed.
   Between reports the bus moves on along the shape at that speed (for at most 2 minutes),
   and its marker eases toward that point, so it glides along the street instead of jumping
   between fixes or cutting across blocks. A bus never slides backwards: when a fix lands a
   little behind where it was drawn, it waits there until the bus catches up. With reduced
   motion each fix is shown as it comes. Coloured by line (the operator's colour, lifted for the
   night map), pointing where it heads; at street zoom its line number sits in a pill.

   Roads. Each Bluetooth segment runs one way; it is drawn offset to the left of its direction
   (Cyprus drives on the left), so the two directions of an avenue sit side by side. A neutral
   for roads that flow, one warm hue brightening as they slow, and wider lines the slower they
   are, so colour never carries it alone. */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"

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

function busFeatures(): any {
  const f = activeFilter()
  const features = []
  for (const t of tracks.values()) {
    const b = t.bus
    if (b.finished || !matches(b, f)) continue
    const color = b.color || "#8798b0"
    features.push({
      type: "Feature", geometry: { type: "Point", coordinates: t.pos },
      properties: { id: b.id, line: b.line, color, text_color: b.text_color || NIGHT, heading: Math.round(t.heading),
                    pill: `mob-pill-${color}`, arrow: `mob-arrow-${color}` },
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
  const src = map.getSource(BUS_SRC) as GeoJSONSource | undefined
  if (src) src.setData(busFeatures())
  followOpenBus(ts)
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

function addBusLayers(map: MlMap) {
  if (map.getSource(BUS_SRC)) return
  map.addSource(BUS_SRC, { type: "geojson", data: busFeatures() })
  map.addLayer({
    id: "mob::bus-dot", type: "circle", source: BUS_SRC,
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 13, 4.5, 15, 6],
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
      "text-allow-overlap": false, "icon-allow-overlap": false, "symbol-sort-key": ["get", "heading"],
    },
    paint: { "text-color": ["get", "text_color"] },
  })
  for (const id of ["mob::bus-dot", "mob::bus-arrow", "mob::bus-label"]) bindOnce(map, id)
}

function removeBusLayers(map: MlMap) {
  for (const id of ["mob::bus-label", "mob::bus-arrow", "mob::bus-dot", "mob::stop-name", "mob::stop"]) if (map.getLayer(id)) map.removeLayer(id)
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

function addRoadLayers(map: MlMap, prefix: string, src: string, ids: string[]) {
  const before = firstLabelLayer()
  const measured: any = ["!=", ["get", "state"], "no_reading"]
  map.addLayer({
    id: `${prefix}-none`, type: "line", source: src, filter: ["==", ["get", "state"], "no_reading"],
    layout: { "line-cap": "butt", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-none`, { "line-color": { night: ROAD_COLORS.night.no_reading, day: ROAD_COLORS.day.no_reading } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 16, 2.5], "line-offset": OFFSET, "line-dasharray": [2, 2] },
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
  for (const id of [`${prefix}-none`, `${prefix}-casing`, `${prefix}-line`]) {
    ids.push(id)
    bindOnce(map, id)
  }
}

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
                     at: fc.meta.at, freeFlow: fc.meta.free_flow })
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
      for (const id of ["mob::roads-none", "mob::roads-casing", "mob::roads-line"]) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible)
      void pollRoads()
    } else {
      for (const id of ["mob::roads-line", "mob::roads-casing", "mob::roads-none"]) if (map.getLayer(id)) map.removeLayer(id)
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

/* The street rules on the map, in styles that read on the night map without shouting:

   - speed limits as soft bands along their streets in the limit's colour (coolest for the
     slowest), the number on them from street zoom; where the signs disagree the band is dashed,
     and where only OpenStreetMap gives the limit it is fainter; the signs themselves as small
     discs from zoom 15;
   - 20 and 30 zones as soft areas with a dashed edge (all are planned), their entrances as dots;
   - traffic signals and pedestrian crossings as small marks from street zoom;
   - cycleways in green with the gaps between pieces dashed and their loose ends ringed;
   - bus lanes in pink, planned ones dashed; enforcement cameras as small lilac marks.

   The Layers sheet's Streets rows draw them from /api/streets/layers; an answer's layer (its
   meta.streets) is drawn the same way from its own data. A click opens the thing. */
import type { Map as MlMap } from "maplibre-gl"

import { firstLabelLayer, getMap, onStyleReady, registerLayerRenderer, themedPaint, useMapUi, type LayerData } from "@/lib/map/engine"
import { registerClickHandler } from "@/lib/registries/map-clicks"
import { fetchStreetLayer, patchStreet, useStreets, type StreetLayerId } from "@/lib/streets/store"

/* eslint-disable @typescript-eslint/no-explicit-any */

const NIGHT = "#0e1828"
const FONT_BOLD = ["Noto Sans Bold"]
const FONT_REGULAR = ["Noto Sans Regular"]

// Posted limits: one calm ramp from mint (slowest) to rose (fastest), stepped for each base map.
export const LIMIT_COLORS: Record<"night" | "day", Record<number, string>> = {
  night: { 20: "#8be3c9", 30: "#4fc39a", 40: "#44b3c2", 50: "#5b8fd9", 65: "#8f7ae0", 80: "#b46fd6", 100: "#d86bb3" },
  day: { 20: "#1f9a7c", 30: "#16855e", 40: "#167a8a", 50: "#2f5fb3", 65: "#6247bd", 80: "#8a37ad", 100: "#a8307f" },
}
export const STREET_COLORS = {
  zone: { night: "#4fc39a", day: "#16855e" },
  cycle: { night: "#74d68a", day: "#25913f" },
  gap: { night: "#ff9a71", day: "#c2502b" },
  busLane: { night: "#f08fb8", day: "#b8336c" },
  camera: { night: "#c4a2ff", day: "#6a3fc4" },
  signal: { night: "#d6dee9", day: "#334155" },
  crossing: { night: "#e4ebf4", day: "#1e293b" },
}

function limitColor(base: "night" | "day"): any {
  const c = LIMIT_COLORS[base]
  return ["match", ["to-number", ["get", "kmh"]], 20, c[20], 30, c[30], 40, c[40], 50, c[50], 65, c[65], 80, c[80], 100, c[100],
          ["<", ["to-number", ["get", "kmh"]], 45], c[40], ["<", ["to-number", ["get", "kmh"]], 70], c[65], c[80]]
}
const themed = (night: string, day: string) => ({ night, day })

// -- marks drawn on a canvas --------------------------------------------------------------------

function canvas(w: number, h: number) {
  const c = document.createElement("canvas")
  c.width = w
  c.height = h
  return [c, c.getContext("2d")!] as const
}

function signalMark(color: string) {
  const [c, g] = canvas(22, 34)
  g.beginPath()
  g.roundRect(3, 2, 16, 30, 6)
  g.fillStyle = NIGHT
  g.fill()
  g.lineWidth = 2
  g.strokeStyle = color
  g.stroke()
  for (const y of [9, 17, 25]) {
    g.beginPath()
    g.arc(11, y, 3, 0, Math.PI * 2)
    g.fillStyle = color
    g.fill()
  }
  return g.getImageData(0, 0, c.width, c.height)
}

function zebraMark(color: string) {
  const [c, g] = canvas(30, 30)
  g.beginPath()
  g.roundRect(2, 2, 26, 26, 6)
  g.fillStyle = NIGHT
  g.fill()
  g.lineWidth = 1.5
  g.strokeStyle = color
  g.stroke()
  g.fillStyle = color
  for (const x of [8, 13.5, 19]) g.fillRect(x, 8, 3, 14)
  return g.getImageData(0, 0, c.width, c.height)
}

function cameraMark(color: string) {
  const [c, g] = canvas(32, 28)
  g.beginPath()
  g.roundRect(2, 6, 22, 16, 4)
  g.fillStyle = NIGHT
  g.fill()
  g.lineWidth = 2
  g.strokeStyle = color
  g.stroke()
  g.beginPath()
  g.moveTo(24, 11)
  g.lineTo(30, 7)
  g.lineTo(30, 21)
  g.lineTo(24, 17)
  g.closePath()
  g.fillStyle = color
  g.fill()
  g.beginPath()
  g.arc(13, 14, 4, 0, Math.PI * 2)
  g.fillStyle = color
  g.fill()
  return g.getImageData(0, 0, c.width, c.height)
}

const MARKS: Record<string, (color: string) => ImageData> = { "str-signal": signalMark, "str-zebra": zebraMark, "str-camera": cameraMark }

function ensureImages(map: MlMap) {
  for (const [name, draw] of Object.entries(MARKS)) {
    for (const base of ["night", "day"] as const) {
      const id = `${name}-${base}`
      const color = name === "str-signal" ? STREET_COLORS.signal[base] : name === "str-zebra" ? STREET_COLORS.crossing[base] : STREET_COLORS.camera[base]
      if (!map.hasImage(id)) map.addImage(id, draw(color), { pixelRatio: 2 })
    }
  }
}

// -- the layers, for one source -----------------------------------------------------------------

const BAND: any = ["interpolate", ["linear"], ["zoom"], 11, 1.2, 13, 2.5, 15, 5.5, 17, 11, 19, 20]
const TIER_OPACITY: any = ["match", ["get", "tier"], "signed", 0.62, "likely", 0.42, "mapped", 0.3, 0.5]

function bind(map: MlMap, id: string, ids: string[]) {
  ids.push(id)
  map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", id, () => { map.getCanvas().style.cursor = "" })
}

function isLayer(name: string): any {
  return ["==", ["get", "layer"], name]
}

function addLimitLayers(map: MlMap, p: string, src: string, ids: string[]) {
  const before = firstLabelLayer()
  map.addLayer({
    id: `${p}-limit-band`, type: "line", source: src,
    filter: ["all", isLayer("limits"), ["!=", ["get", "tier"], "uncertain"]],
    layout: { "line-cap": "round", "line-join": "round", "line-sort-key": ["match", ["get", "tier"], "mapped", 0, "likely", 1, 2] },
    paint: { ...themedPaint(`${p}-limit-band`, { "line-color": { night: limitColor("night"), day: limitColor("day") } }),
             "line-width": BAND, "line-opacity": TIER_OPACITY, "line-blur": 0.4 },
  }, before)
  map.addLayer({
    id: `${p}-limit-unsure`, type: "line", source: src,
    filter: ["all", isLayer("limits"), ["==", ["get", "tier"], "uncertain"]],
    layout: { "line-cap": "butt", "line-join": "round" },
    paint: { ...themedPaint(`${p}-limit-unsure`, { "line-color": { night: limitColor("night"), day: limitColor("day") } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 15, 3.5, 18, 7], "line-opacity": 0.75, "line-dasharray": [1.2, 1.6] },
  }, before)
  map.addLayer({
    id: `${p}-limit-number`, type: "symbol", source: src, minzoom: 15,
    filter: ["all", isLayer("limits"), ["!=", ["get", "tier"], "uncertain"], [">", ["get", "length_m"], 60]],
    layout: { "symbol-placement": "line", "symbol-spacing": 320, "text-field": ["get", "label"], "text-font": FONT_BOLD,
              "text-size": ["interpolate", ["linear"], ["zoom"], 15, 10.5, 18, 13], "text-keep-upright": true, "text-padding": 6 },
    paint: { ...themedPaint(`${p}-limit-number`, { "text-color": { night: limitColor("night"), day: limitColor("day") },
                                                    "text-halo-color": { night: NIGHT, day: "#ffffff" } }), "text-halo-width": 1.6 },
  })
  map.addLayer({
    id: `${p}-sign`, type: "circle", source: src, minzoom: 14.5, filter: isLayer("signs"),
    paint: { ...themedPaint(`${p}-sign`, { "circle-color": { night: NIGHT, day: "#ffffff" },
                                            "circle-stroke-color": { night: limitColor("night"), day: limitColor("day") } }),
             "circle-radius": ["interpolate", ["linear"], ["zoom"], 14.5, 2.5, 16.5, 8, 18, 10],
             "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 14.5, 1.2, 16.5, 2.2] },
  })
  map.addLayer({
    id: `${p}-sign-number`, type: "symbol", source: src, minzoom: 16.5, filter: isLayer("signs"),
    layout: { "text-field": ["get", "label"], "text-font": FONT_BOLD, "text-size": 10, "text-allow-overlap": true },
    paint: { ...themedPaint(`${p}-sign-number`, { "text-color": { night: "#e4ebf4", day: "#0e1828" } }) },
  })
  for (const id of [`${p}-limit-band`, `${p}-limit-unsure`, `${p}-sign`]) bind(map, id, ids)
  ids.push(`${p}-limit-number`, `${p}-sign-number`)
}

function addZoneLayers(map: MlMap, p: string, src: string, ids: string[]) {
  const before = firstLabelLayer()
  const z = STREET_COLORS.zone
  map.addLayer({
    id: `${p}-zone-fill`, type: "fill", source: src, filter: isLayer("zones"),
    paint: { ...themedPaint(`${p}-zone-fill`, { "fill-color": themed(z.night, z.day) }), "fill-opacity": 0.1 },
  }, before)
  map.addLayer({
    id: `${p}-zone-edge`, type: "line", source: src, filter: isLayer("zones"),
    layout: { "line-join": "round" },
    paint: { ...themedPaint(`${p}-zone-edge`, { "line-color": themed(z.night, z.day) }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 16, 2.2], "line-opacity": 0.75, "line-dasharray": [3, 2] },
  }, before)
  map.addLayer({
    id: `${p}-zone-street`, type: "line", source: src, filter: isLayer("zone_streets"),
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...themedPaint(`${p}-zone-street`, { "line-color": themed(z.night, z.day) }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1, 16, 3.5], "line-opacity": 0.55 },
  }, before)
  map.addLayer({
    id: `${p}-zone-entry`, type: "circle", source: src, minzoom: 14, filter: isLayer("zone_entrances"),
    paint: { ...themedPaint(`${p}-zone-entry`, { "circle-color": themed(z.night, z.day), "circle-stroke-color": themed(NIGHT, "#ffffff") }),
             "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 2, 17, 4.5], "circle-stroke-width": 1 },
  })
  map.addLayer({
    id: `${p}-zone-name`, type: "symbol", source: src, minzoom: 12.5, filter: isLayer("zones"),
    layout: { "text-field": ["concat", ["get", "label"], "\n(planned)"], "text-font": FONT_REGULAR, "text-size": 12,
              "symbol-placement": "point", "text-max-width": 9 },
    paint: { ...themedPaint(`${p}-zone-name`, { "text-color": themed("#8fe0c0", "#0f5f43"), "text-halo-color": themed(NIGHT, "#ffffff") }),
             "text-halo-width": 1.4 },
  })
  for (const id of [`${p}-zone-fill`, `${p}-zone-entry`]) bind(map, id, ids)
  ids.push(`${p}-zone-edge`, `${p}-zone-street`, `${p}-zone-name`)
}

function addPointMarks(map: MlMap, p: string, src: string, ids: string[], layer: "signals" | "crossings" | "cameras", minzoom: number) {
  ensureImages(map)
  const icon = { signals: "str-signal", crossings: "str-zebra", cameras: "str-camera" }[layer]
  const id = `${p}-${layer}`
  map.addLayer({
    id, type: "symbol", source: src, minzoom, filter: isLayer(layer),
    layout: { "icon-image": `${icon}-night`, "icon-size": ["interpolate", ["linear"], ["zoom"], minzoom, 0.55, 17, 0.9],
              "icon-allow-overlap": layer === "cameras", "icon-padding": 1,
              "symbol-sort-key": layer === "cameras" ? 2 : layer === "signals" ? 1 : 0 },
    paint: { "icon-opacity": ["interpolate", ["linear"], ["zoom"], minzoom, 0, minzoom + 0.4, 1] },
  })
  themedLayout(id, `${icon}-night`, `${icon}-day`)
  bind(map, id, ids)
}

// The marks are images: a change of base map swaps them (themedPaint covers paint only).
const themedIcons = new Map<string, [string, string]>()
function themedLayout(id: string, night: string, day: string) {
  themedIcons.set(id, [night, day])
  syncIcons()
}
function syncIcons() {
  const map = getMap()
  if (!map) return
  const base = useMapUi.getState().basemap
  for (const [id, [n, d]] of themedIcons) {
    if (!map.getLayer(id)) { themedIcons.delete(id); continue }
    map.setLayoutProperty(id, "icon-image", base === "day" ? d : n)
  }
}

function addLineLayers(map: MlMap, p: string, src: string, ids: string[], which: "cycleways" | "bus_lanes") {
  const before = firstLabelLayer()
  if (which === "cycleways") {
    const c = STREET_COLORS.cycle, g = STREET_COLORS.gap
    map.addLayer({
      id: `${p}-cycle`, type: "line", source: src, filter: isLayer("cycleways"),
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { ...themedPaint(`${p}-cycle`, { "line-color": themed(c.night, c.day) }),
               "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.4, 14, 2.6, 17, 5], "line-opacity": 0.9 },
    }, before)
    map.addLayer({
      id: `${p}-cycle-gap`, type: "line", source: src, filter: isLayer("cycle_gaps"),
      layout: { "line-cap": "butt" },
      paint: { ...themedPaint(`${p}-cycle-gap`, { "line-color": themed(g.night, g.day) }),
               "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.2, 16, 3], "line-dasharray": [1.5, 1.5], "line-opacity": 0.9 },
    }, before)
    map.addLayer({
      id: `${p}-cycle-end`, type: "circle", source: src, minzoom: 12.5, filter: isLayer("cycle_ends"),
      paint: { ...themedPaint(`${p}-cycle-end`, { "circle-color": themed(NIGHT, "#ffffff"), "circle-stroke-color": themed(g.night, g.day) }),
               "circle-radius": ["interpolate", ["linear"], ["zoom"], 12.5, 2.5, 17, 5], "circle-stroke-width": 1.6 },
    })
    for (const id of [`${p}-cycle`, `${p}-cycle-end`]) bind(map, id, ids)
    ids.push(`${p}-cycle-gap`)
  } else {
    const b = STREET_COLORS.busLane
    const width: any = ["interpolate", ["linear"], ["zoom"], 11, 1.4, 14, 2.6, 17, 5]
    map.addLayer({
      id: `${p}-bus-lane`, type: "line", source: src, filter: ["all", isLayer("bus_lanes"), ["==", ["get", "status"], "active"]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { ...themedPaint(`${p}-bus-lane`, { "line-color": themed(b.night, b.day) }), "line-width": width, "line-opacity": 0.9 },
    }, before)
    map.addLayer({
      id: `${p}-bus-lane-planned`, type: "line", source: src, filter: ["all", isLayer("bus_lanes"), ["!=", ["get", "status"], "active"]],
      layout: { "line-join": "round" },
      paint: { ...themedPaint(`${p}-bus-lane-planned`, { "line-color": themed(b.night, b.day) }), "line-width": width,
               "line-opacity": 0.6, "line-dasharray": [2, 1.5] },
    }, before)
    for (const id of [`${p}-bus-lane`, `${p}-bus-lane-planned`]) bind(map, id, ids)
  }
}

function addSchoolLayers(map: MlMap, p: string, src: string, ids: string[]) {
  map.addLayer({
    id: `${p}-school`, type: "circle", source: src, filter: ["any", isLayer("schools"), isLayer("stops")],
    paint: { "circle-color": ["get", "color"], ...themedPaint(`${p}-school`, { "circle-stroke-color": themed(NIGHT, "#ffffff") }),
             "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 3, 14, 5, 17, 8], "circle-stroke-width": 1.4 },
  })
  map.addLayer({
    id: `${p}-school-name`, type: "symbol", source: src, minzoom: 15, filter: ["any", isLayer("schools"), isLayer("stops")],
    layout: { "text-field": ["get", "label"], "text-font": FONT_REGULAR, "text-size": 11, "text-offset": [0, 1.1], "text-anchor": "top",
              "text-optional": true, "text-max-width": 10 },
    paint: { ...themedPaint(`${p}-school-name`, { "text-color": themed("#8798b0", "#475569"), "text-halo-color": themed(NIGHT, "#ffffff") }),
             "text-halo-width": 1.2 },
  })
  bind(map, `${p}-school`, ids)
  ids.push(`${p}-school-name`)
}

// Everything a Streets source can hold, drawn for one source (a Layers row or an answer).
function addAll(map: MlMap, p: string, src: string, ids: string[], what: StreetLayerId | "answer", layers: Set<string>) {
  if (what === "limits" || layers.has("limits") || layers.has("signs")) addLimitLayers(map, p, src, ids)
  if (what === "zones" || layers.has("zones")) addZoneLayers(map, p, src, ids)
  if (what === "cycleways" || layers.has("cycleways")) addLineLayers(map, p, src, ids, "cycleways")
  if (what === "bus_lanes" || layers.has("bus_lanes")) addLineLayers(map, p, src, ids, "bus_lanes")
  if (what === "crossings" || layers.has("crossings")) addPointMarks(map, p, src, ids, "crossings", what === "answer" ? 13 : 15)
  if (what === "signals" || layers.has("signals")) addPointMarks(map, p, src, ids, "signals", what === "answer" ? 13 : 14.5)
  if (what === "cameras" || layers.has("cameras")) addPointMarks(map, p, src, ids, "cameras", 11)
  if (layers.has("schools") || layers.has("stops")) addSchoolLayers(map, p, src, ids)
}

// -- the Layers sheet's rows ----------------------------------------------------------------------

const rowIds = new Map<StreetLayerId, string[]>()
const rowData = new Map<StreetLayerId, any>()
const ctrls = new Map<StreetLayerId, AbortController>()

function srcOf(id: StreetLayerId) {
  return `str::${id}`
}

function draw(id: StreetLayerId) {
  const map = getMap()
  const data = rowData.get(id)
  if (!map || !data) return
  onStyleReady(() => {
    if (!useStreets.getState()[id].on) return
    undraw(id)
    map.addSource(srcOf(id), { type: "geojson", data })
    const ids: string[] = []
    const layers = new Set<string>(data.features.map((f: any) => f.properties?.layer))
    addAll(map, `str::${id}`, srcOf(id), ids, id, layers)
    rowIds.set(id, ids)
  })
}

function undraw(id: StreetLayerId) {
  const map = getMap()
  if (!map) return
  for (const l of rowIds.get(id) ?? []) if (map.getLayer(l)) map.removeLayer(l)
  rowIds.delete(id)
  if (map.getSource(srcOf(id))) map.removeSource(srcOf(id))
}

export async function setStreetLayer(id: StreetLayerId, on: boolean) {
  if (on === useStreets.getState()[id].on && (!on || useStreets.getState()[id].status !== "error")) return
  patchStreet(id, { on })
  if (!on) {
    ctrls.get(id)?.abort()
    undraw(id)
    patchStreet(id, { status: "idle", error: null })
    return
  }
  if (rowData.has(id) && useStreets.getState()[id].meta?.status === "ready") {
    patchStreet(id, { status: "ready" })
    draw(id)
    return
  }
  const ctrl = new AbortController()
  ctrls.get(id)?.abort()
  ctrls.set(id, ctrl)
  patchStreet(id, { status: "loading", error: null })
  try {
    const fc = await fetchStreetLayer(id, ctrl.signal)
    if (!useStreets.getState()[id].on) return
    rowData.set(id, fc)
    patchStreet(id, { status: "ready", meta: fc.meta })
    draw(id)
    // The limits come before the street network sometimes (the first minute after a start):
    // the signs show at once, and the stretches when they are worked out.
    if (id === "limits" && fc.meta.status === "loading") window.setTimeout(() => { if (useStreets.getState().limits.on) void reload("limits") }, 20_000)
  } catch (e) {
    if ((e as Error).name === "AbortError") return
    patchStreet(id, { status: "error", error: (e as Error).message })
  }
}

async function reload(id: StreetLayerId) {
  rowData.delete(id)
  patchStreet(id, { on: false })
  await setStreetLayer(id, true)
}

export function streetLayerOn(id: StreetLayerId) {
  return useStreets.getState()[id].on
}

// -- answers ----------------------------------------------------------------------------------------

registerLayerRenderer({
  id: "streets",
  claims: (d: LayerData) => !!d.meta?.streets,
  add: (ref, src, data, ids) => {
    const map = getMap()
    if (!map) return
    const layers = new Set<string>(data.features.map((f: any) => f.properties?.layer))
    addAll(map, `${ref}::str`, src, ids, "answer", layers)
  },
})

// -- clicks ---------------------------------------------------------------------------------------

const NOT_THINGS = /^(gap-|street-)/

registerClickHandler({
  id: "streets",
  priority: 11,
  layers: (l) => l.startsWith("str::") || l.includes("::str-"),
  resolve: (f, at) => {
    const id = f.properties?.id
    if (!id || NOT_THINGS.test(String(id))) return null
    const center = f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
    return { id: String(id), at: center }
  },
})

// -- start ----------------------------------------------------------------------------------------

let started = false
export function startStreetsMap() {
  if (started) return
  started = true
  const wait = () => {
    const map = getMap()
    if (!map) { window.setTimeout(wait, 200); return }
    map.on("styleimagemissing", (e: any) => { if (String(e.id).startsWith("str-")) ensureImages(map) })
  }
  wait()
  useMapUi.subscribe((st, prev) => { if (st.basemap !== prev.basemap) syncIcons() })
}

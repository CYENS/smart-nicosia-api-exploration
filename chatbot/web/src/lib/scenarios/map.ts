/* The Scenario lab on the map: today against the scenario, split by a swipe.

   A run's layers come from the server (chatbot/scenarios/base.py). Layers with variant
   "baseline" are drawn left of the swipe and "scenario" right of it (each feature by its first
   coordinate). Comparing two runs, the open run's scenario is on the left and the compared
   run's on the right. Layers without a variant are either overlays drawn over both sides, on by
   default when `meta.show` is true (the lighting's safety flags), or other views of the run that
   replace the split (what each lamp saves). Points and lines take `properties.color`; the
   selected feature gets a sodium ring.

   For a scenario's own sections: showScenarioLayer, flyToScenarioFeature, selectScenarioFeature. */
import type { GeoJSONSource } from "maplibre-gl"
import { create } from "zustand"

import { firstLabelLayer, flyToBounds, getMap, onStyleReady, SODIUM } from "@/lib/map/engine"
import { registerClickHandler } from "@/lib/registries/map-clicks"
import { useWorkspace } from "@/lib/registries/workspaces"
import type { ScenarioFeature, ScenarioLayer, ScenarioRun } from "@/lib/registries/scenarios"
import { useLab } from "@/lib/scenarios/store"

/* eslint-disable @typescript-eslint/no-explicit-any */

const SRC = "scn::src"
const SEL = "scn::sel"
const LAYERS = ["scn::line-casing", "scn::line", "scn::point", "scn::sel"] as const
const NIGHT = "#0e1828"

export type MapView = "split" | string // "split", or the name of a layer drawn alone

type LabMap = {
  view: MapView
  overlays: Record<string, boolean> // overlay layers' switches
  swipe: number // where the swipe divides the open map, 0 to 1
  selected: string | null
}

export const useLabMap = create<LabMap>(() => ({ view: "split", overlays: {}, swipe: 0.5, selected: null }))

// The lab draws only while it is the open workspace.
const labOpen = () => useWorkspace.getState().active === "scenarios"

export function variantLayers(run: ScenarioRun | null, variant: "baseline" | "scenario"): [string, ScenarioLayer][] {
  return Object.entries(run?.result.layers ?? {}).filter(([, l]) => l.meta?.variant === variant)
}

// Layers with no variant: overlays (meta.show) and other views of the run.
export function plainLayers(run: ScenarioRun | null): { overlays: [string, ScenarioLayer][]; views: [string, ScenarioLayer][] } {
  const plain = Object.entries(run?.result.layers ?? {}).filter(([, l]) => !l.meta?.variant)
  return {
    overlays: plain.filter(([, l]) => (l.meta as any)?.show),
    views: plain.filter(([, l]) => !(l.meta as any)?.show),
  }
}

function anchor(f: ScenarioFeature): [number, number] | null {
  const g = f.geometry as any
  if (!g) return null
  if (g.type === "Point") return g.coordinates
  if (g.type === "LineString") return g.coordinates[0] ?? null
  if (g.type === "MultiLineString") return g.coordinates[0]?.[0] ?? null
  return null
}

function swipeX() {
  const map = getMap()
  const p = map.getPadding()
  const w = map.getCanvas().clientWidth
  const left = p.left ?? 0, right = p.right ?? 0
  return left + useLabMap.getState().swipe * Math.max(1, w - left - right)
}

function features(): ScenarioFeature[] {
  const { run, compare } = useLab.getState()
  const { view, overlays } = useLabMap.getState()
  if (!run || !labOpen()) return []
  const out: ScenarioFeature[] = []
  const shownOverlays = (r: ScenarioRun) => plainLayers(r).overlays.filter(([name]) => overlays[name] ?? true)
  if (view === "split") {
    const map = getMap()
    const x = swipeX()
    const side = (f: ScenarioFeature) => {
      const c = anchor(f)
      return c ? map.project(c as [number, number]).x < x : false
    }
    const left = compare ? variantLayers(run, "scenario") : variantLayers(run, "baseline")
    const right = compare ? variantLayers(compare, "scenario") : variantLayers(run, "scenario")
    for (const [, l] of left) out.push(...l.features.filter(side))
    for (const [, l] of right) out.push(...l.features.filter((f) => !side(f)))
    // An overlay belongs to a run, not to today: on the run's side of the swipe only.
    if (compare) for (const [, l] of shownOverlays(run)) out.push(...l.features.filter(side))
    for (const [, l] of shownOverlays(compare ?? run)) out.push(...l.features.filter((f) => !side(f)))
  } else {
    const l = run.result.layers[view]
    if (l) out.push(...l.features)
    for (const [, l2] of shownOverlays(run)) out.push(...l2.features)
  }
  return out
}

function ensure() {
  const map = getMap()
  if (!map || map.getSource(SRC)) return Boolean(map)
  const empty = { type: "FeatureCollection", features: [] } as any
  map.addSource(SRC, { type: "geojson", data: empty })
  map.addSource(SEL, { type: "geojson", data: empty })
  const before = firstLabelLayer()
  const isLine = ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]]
  map.addLayer({ id: "scn::line-casing", type: "line", source: SRC, filter: isLine as any,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": NIGHT, "line-width": ["interpolate", ["linear"], ["zoom"], 13, 4, 18, 9], "line-opacity": 0.7 } }, before)
  map.addLayer({ id: "scn::line", type: "line", source: SRC, filter: isLine as any,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["coalesce", ["get", "color"], SODIUM], "line-width": ["interpolate", ["linear"], ["zoom"], 13, 2, 18, 5] } }, before)
  map.addLayer({ id: "scn::point", type: "circle", source: SRC, filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-color": ["coalesce", ["get", "color"], SODIUM],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 3, 16, 6, 19, 10],
      "circle-stroke-color": NIGHT, "circle-stroke-width": 1.2,
    } })
  map.addLayer({ id: "scn::sel", type: "circle", source: SEL,
    paint: {
      "circle-color": "rgba(0,0,0,0)", "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 7, 16, 11, 19, 16],
      "circle-stroke-color": SODIUM, "circle-stroke-width": 3,
    } })
  return true
}

function remove() {
  const map = getMap()
  if (!map) return
  for (const id of LAYERS) if (map.getLayer(id)) map.removeLayer(id)
  for (const s of [SRC, SEL]) if (map.getSource(s)) map.removeSource(s)
}

function selectedPoint(): any {
  const id = useLabMap.getState().selected
  const f = id ? allFeatures().find((x) => x.properties.id === id) : undefined
  const c = f ? anchor(f) : null
  return { type: "FeatureCollection", features: c ? [{ type: "Feature", geometry: { type: "Point", coordinates: c }, properties: {} }] : [] }
}

let frame = 0
export function redraw() {
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    const map = getMap()
    if (!map) return
    const { run } = useLab.getState()
    if (!run || !labOpen()) {
      remove()
      return
    }
    if (!ensure()) return
    ;(map.getSource(SRC) as GeoJSONSource).setData({ type: "FeatureCollection", features: features() } as any)
    ;(map.getSource(SEL) as GeoJSONSource).setData(selectedPoint())
  })
}

function allFeatures(): ScenarioFeature[] {
  const { run, compare } = useLab.getState()
  return [run, compare].flatMap((r) => Object.values(r?.result.layers ?? {}).flatMap((l) => l.features))
}

function bounds(fs: ScenarioFeature[]): [number, number, number, number] | null {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity
  const add = (c: number[]) => { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]) }
  for (const f of fs) {
    const g = f.geometry as any
    if (g.type === "Point") add(g.coordinates)
    else if (g.type === "LineString") g.coordinates.forEach(add)
  }
  return Number.isFinite(w) ? [w - 0.0015, s - 0.001, e + 0.0015, n + 0.001] : null
}

export function frameRun() {
  const b = bounds(allFeatures())
  if (b && getMap()) flyToBounds(b)
}

// Frame the run once the panels have settled (a phone's sheet sets the map's padding as it opens),
// unless it is already in view: a shared link keeps the camera it was shared with.
let frameTimer = 0
function frameSoon() {
  clearTimeout(frameTimer)
  frameTimer = window.setTimeout(() => {
    const map = getMap()
    const b = bounds(allFeatures())
    if (!map || !b || !labOpen()) return
    const view = map.getBounds()
    const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2
    const inView = view.contains([cx, cy]) && map.getZoom() >= 13
    const p = map.project([cx, cy])
    const pad = map.getPadding()
    const canvas = map.getCanvas()
    const clear = p.y > (pad.top ?? 0) && p.y < canvas.clientHeight - (pad.bottom ?? 0)
    if (!inView || !clear) frameRun()
  }, 450)
}

// -- for the lab's panel and a scenario's own sections -----------------------------------------

export function setMapView(view: MapView) {
  useLabMap.setState({ view })
}

export function setSwipe(share: number) {
  useLabMap.setState({ swipe: Math.max(0.04, Math.min(0.96, share)) })
}

// Turn a layer of the open run on: an overlay's switch, or another view (a variant layer is
// always on the split).
export function showScenarioLayer(name: string, on = true) {
  const run = useLab.getState().run
  const l = run?.result.layers[name]
  if (!l) return
  if (l.meta?.variant) {
    useLabMap.setState({ view: "split" })
    return
  }
  if ((l.meta as any)?.show) useLabMap.setState((s) => ({ overlays: { ...s.overlays, [name]: on } }))
  else useLabMap.setState({ view: on ? name : "split" })
}

export function selectScenarioFeature(id: string | null) {
  useLabMap.setState({ selected: id })
}

// Fly to a feature of the open run (or the compared one) by its id, and ring it.
export function flyToScenarioFeature(id: string) {
  const f = allFeatures().find((x) => x.properties.id === id)
  if (!f) return
  selectScenarioFeature(id)
  const g = f.geometry as any
  if (g.type === "Point") {
    const map = getMap()
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    map?.[reduce ? "jumpTo" : "easeTo"]({ center: g.coordinates, zoom: Math.max(map.getZoom(), 17.2), ...(reduce ? {} : { duration: 700 }) } as any)
  } else {
    const b = bounds([f])
    if (b) flyToBounds(b)
  }
}

export function enterLabMap() {
  if (useLab.getState().run) frameSoon()
  redraw()
}

export function leaveLabMap() {
  useLabMap.setState({ selected: null })
  redraw()
}

// -- clicks: a lamp (or any device) opens in the inspector -------------------------------------------

registerClickHandler({
  id: "scenarios",
  priority: 14,
  layers: (l) => l.startsWith("scn::") && l !== "scn::sel",
  resolve: (f, at) => {
    const id = f.properties?.id
    if (!id) return null
    selectScenarioFeature(String(id))
    if (String(id).startsWith("street:")) return null // a flagged street: selected, nothing to inspect
    const center = f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
    return { id: String(id), at: center }
  },
})

let started = false
export function startLabMap() {
  if (started) return
  started = true
  useLab.subscribe((s, prev) => {
    if (s.run !== prev.run || s.compare !== prev.compare) {
      const { overlays } = plainLayers(s.run)
      useLabMap.setState({
        overlays: Object.fromEntries(overlays.map(([k]) => [k, true])),
        view: s.run && useLabMap.getState().view !== "split" && !s.run.result.layers[useLabMap.getState().view] ? "split" : useLabMap.getState().view,
      })
      if (s.run && s.run !== prev.run && labOpen()) frameSoon()
      redraw()
    }
  })
  useLabMap.subscribe(redraw)
  useWorkspace.subscribe((s, prev) => { if (s.active !== prev.active) redraw() })
  const wait = () => {
    const map = getMap()
    if (!map) {
      requestAnimationFrame(wait)
      return
    }
    onStyleReady(redraw)
    map.on("style.load", redraw)
    map.on("move", () => { if (useLabMap.getState().view === "split" && labOpen()) redraw() })
  }
  wait()
}

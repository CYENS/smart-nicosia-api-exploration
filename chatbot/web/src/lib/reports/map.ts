/* Road reports on the map. Each kind is told apart by its shape as well as its colour, so it
   reads on the night map at a glance and without colour:

     closure    a no-entry disc (ink white, a bar across)     accident   a burst, signal red
     jam        a queue of three cars on a pill               hazard     a warning triangle, amber
     roadworks  a diamond with a cone, teal                   diversion  a square with a bent arrow, blue

   Signal red is for accidents only. A closed stretch is a dashed ink line along the road, a jam
   line (Waze's jam lines) a warm line widening with its level, and a drive an answer asked about
   a thin grey line under them. A report made in the last half hour wears a thin sodium ring
   (now); one older than a week (a long closure, long works) is drawn smaller and fainter, and
   works that start later fainter still.

   The Layers switch and the Traffic card draw every report at the city clock's moment (from
   /api/road-reports, read again every minute at now); an answer (get_road_reports) draws its own
   reports the same way, and the general layer steps back while it is on. A click opens the
   report in the inspector. */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"

import { useClock } from "@/lib/clock"
import { useInspector } from "@/lib/inspector"
import {
  firstLabelLayer, getMap, linkHover, onStyleReady, registerLayerRenderer, themedPaint, type LayerData,
} from "@/lib/map/engine"
import { fetchReports, useReports, type ReportKind } from "@/lib/reports/store"
import { registerClickHandler } from "@/lib/registries/map-clicks"

/* eslint-disable @typescript-eslint/no-explicit-any */

const SRC = "rr::reports"
const PREFIX = "rr::"
const POLL_MS = 60_000 // the server reads Waze every 3 minutes at most; this catches each read soon
const NIGHT = "#0e1828"
const SODIUM = "#ffae45"
const FONT = ["Noto Sans Regular"]

// The kinds' colours on the night map (chatbot/mobility/reports.py KIND_COLORS).
export const REPORT_COLORS: Record<ReportKind, string> = {
  accident: "#ff5a4e", closure: "#e4ebf4", jam: "#ff9a71", hazard: "#f5b94a", roadworks: "#4fc1b0", diversion: "#7aa7ff",
}
export const REPORT_WORDS: Record<ReportKind, string> = {
  accident: "Accident", closure: "Road closed", jam: "Jam", hazard: "Hazard", roadworks: "Roadworks", diversion: "Diversion",
}

// -- the marks, drawn once on a canvas per kind ----------------------------------------------------

const SIZE = 48 // drawn at twice the size it shows (pixelRatio 2): 24 px

function star(ctx: CanvasRenderingContext2D, cx: number, cy: number, outer: number, inner: number, n: number) {
  ctx.beginPath()
  for (let i = 0; i < n * 2; i++) {
    const r = i % 2 === 0 ? outer : inner
    const a = (Math.PI * i) / n - Math.PI / 2
    ctx.lineTo(cx + r * Math.cos(a), cy + r * Math.sin(a))
  }
  ctx.closePath()
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.roundRect(x, y, w, h, r)
}

function bang(ctx: CanvasRenderingContext2D, top: number, bottom: number, dot: number) {
  ctx.fillStyle = NIGHT
  roundRect(ctx, 21.6, top, 4.8, bottom - top, 2.2)
  ctx.fill()
  ctx.beginPath()
  ctx.arc(24, dot, 2.7, 0, Math.PI * 2)
  ctx.fill()
}

// The shape of a kind, filled with `fill`, outlined in the night colour so it reads on either map.
export function drawMark(ctx: CanvasRenderingContext2D, kind: ReportKind, fill: string) {
  ctx.lineJoin = "round"
  ctx.lineCap = "round"
  ctx.lineWidth = 3.5
  ctx.strokeStyle = NIGHT
  ctx.fillStyle = fill
  switch (kind) {
    case "closure":
      ctx.beginPath()
      ctx.arc(24, 24, 18, 0, Math.PI * 2)
      ctx.fill()
      ctx.stroke()
      ctx.fillStyle = NIGHT
      roundRect(ctx, 12.5, 20.5, 23, 7, 1.5)
      ctx.fill()
      break
    case "accident":
      star(ctx, 24, 24, 21, 14, 8)
      ctx.fill()
      ctx.stroke()
      bang(ctx, 13, 27, 32.5)
      break
    case "hazard":
      ctx.beginPath()
      ctx.moveTo(24, 5)
      ctx.lineTo(43.5, 40)
      ctx.lineTo(4.5, 40)
      ctx.closePath()
      ctx.fill()
      ctx.stroke()
      bang(ctx, 16, 29.5, 34.6)
      break
    case "jam":
      roundRect(ctx, 3.5, 12.5, 41, 23, 9)
      ctx.fill()
      ctx.stroke()
      ctx.fillStyle = NIGHT
      for (const x of [10.5, 20.5, 30.5]) { roundRect(ctx, x, 19.5, 7, 9, 2); ctx.fill() }
      break
    case "roadworks":
      ctx.beginPath()
      ctx.moveTo(24, 3)
      ctx.lineTo(45, 24)
      ctx.lineTo(24, 45)
      ctx.lineTo(3, 24)
      ctx.closePath()
      ctx.fill()
      ctx.stroke()
      ctx.fillStyle = NIGHT // a cone on its base
      ctx.beginPath()
      ctx.moveTo(24, 13)
      ctx.lineTo(29.5, 29)
      ctx.lineTo(18.5, 29)
      ctx.closePath()
      ctx.fill()
      roundRect(ctx, 15, 29.5, 18, 4, 1.5)
      ctx.fill()
      break
    case "diversion":
      roundRect(ctx, 5, 5, 38, 38, 8)
      ctx.fill()
      ctx.stroke()
      ctx.strokeStyle = NIGHT // a road that bends away: up, then off to the right
      ctx.lineWidth = 4.2
      ctx.beginPath()
      ctx.moveTo(17, 36)
      ctx.lineTo(17, 26)
      ctx.quadraticCurveTo(17, 18, 25, 18)
      ctx.lineTo(28, 18)
      ctx.stroke()
      ctx.fillStyle = NIGHT
      ctx.beginPath()
      ctx.moveTo(35, 18)
      ctx.lineTo(27, 11.5)
      ctx.lineTo(27, 24.5)
      ctx.closePath()
      ctx.fill()
      break
  }
}

function markImage(kind: ReportKind) {
  const c = document.createElement("canvas")
  c.width = SIZE
  c.height = SIZE
  const ctx = c.getContext("2d")!
  drawMark(ctx, kind, REPORT_COLORS[kind])
  return ctx.getImageData(0, 0, SIZE, SIZE)
}

function ensureImages(map: MlMap) {
  for (const kind of Object.keys(REPORT_COLORS) as ReportKind[]) {
    const id = `rr-${kind}`
    if (!map.hasImage(id)) map.addImage(id, markImage(kind), { pixelRatio: 2 })
  }
}

// -- layers ------------------------------------------------------------------------------------------

const POINT: any = ["==", ["geometry-type"], "Point"]
const OLD: any = ["case", ["boolean", ["get", "old"], false], 0.8, 1]
const ORDER: any = ["match", ["get", "kind"], "accident", 6, "jam", 5, "closure", 4, "hazard", 3, "roadworks", 2, "diversion", 1, 0]

const bound = new Set<string>()
function bindOnce(map: MlMap, id: string) {
  if (bound.has(id)) return
  bound.add(id)
  map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; linkHover("id", null, "map") })
  map.on("mousemove", id, (e: any) => linkHover("id", e.features?.[0]?.properties?.id ?? null, "map"))
}

// Every layer a set of reports needs, ids `${prefix}-...`, from the source `src`.
export function addReportLayers(map: MlMap, prefix: string, src: string, ids: string[]) {
  ensureImages(map)
  const before = firstLabelLayer()
  const add = (def: any, under?: string) => {
    map.addLayer(def, under)
    ids.push(def.id)
  }
  add({
    id: `${prefix}-route`, type: "line", source: src, filter: ["==", ["get", "part"], "route"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-route`, { "line-color": { night: "#8798b0", day: "#64748b" } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.5, 16, 3.5], "line-opacity": 0.8 },
  }, before)
  const jam: any = ["all", ["==", ["get", "part"], "line"], ["!=", ["get", "kind"], "closure"]]
  const width: any = ["interpolate", ["linear"], ["zoom"], 11, ["+", 1, ["*", 0.5, ["coalesce", ["get", "level"], 3]]],
                      16, ["+", 3, ["*", 1.2, ["coalesce", ["get", "level"], 3]]]]
  add({
    id: `${prefix}-jam-casing`, type: "line", source: src, filter: jam,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-jam-casing`, { "line-color": { night: NIGHT, day: "#ffffff" } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 4, 16, 9] },
  }, before)
  add({
    id: `${prefix}-jam`, type: "line", source: src, filter: jam,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": width },
  }, before)
  const stretch: any = ["all", ["==", ["get", "part"], "line"], ["==", ["get", "kind"], "closure"]]
  add({
    id: `${prefix}-stretch-casing`, type: "line", source: src, filter: stretch,
    layout: { "line-cap": "butt", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-stretch-casing`, { "line-color": { night: NIGHT, day: "#ffffff" } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 4, 16, 8] },
  }, before)
  add({
    id: `${prefix}-stretch`, type: "line", source: src, filter: stretch,
    layout: { "line-cap": "butt", "line-join": "round" },
    paint: { ...themedPaint(`${prefix}-stretch`, { "line-color": { night: REPORT_COLORS.closure, day: "#334155" } }),
             "line-width": ["interpolate", ["linear"], ["zoom"], 11, 2, 16, 4.5], "line-dasharray": [1.4, 1.1] },
  }, before)
  // Above the base map's labels: the marks are what the layer is about.
  add({
    id: `${prefix}-ring`, type: "circle", source: src,
    filter: ["all", POINT, ["boolean", ["get", "recent"], false]],
    paint: { "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": SODIUM, "circle-stroke-width": 2,
             "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 9.5, 14, 13, 17, 15.5] },
  })
  add({
    id: `${prefix}-mark`, type: "symbol", source: src, filter: POINT,
    layout: {
      "icon-image": ["concat", "rr-", ["get", "kind"]],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 11, ["*", 0.62, OLD], 14, ["*", 0.85, OLD], 17, ["*", 1.05, OLD]],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
      "symbol-sort-key": ["-", ORDER, ["case", ["boolean", ["get", "old"], false], 0.5, 0]],
    },
    paint: { "icon-opacity": ["case", ["==", ["get", "status"], "planned"], 0.5, ["boolean", ["get", "old"], false], 0.7, 1] },
  })
  add({
    id: `${prefix}-label`, type: "symbol", source: src, filter: POINT, minzoom: 15.5,
    layout: { "text-field": ["get", "label"], "text-font": FONT, "text-size": 11, "text-offset": [0, 1.35],
              "text-anchor": "top", "text-optional": true, "text-max-width": 9 },
    paint: { ...themedPaint(`${prefix}-label`, { "text-color": { night: "#c3cedc", day: "#334155" },
                                                 "text-halo-color": { night: NIGHT, day: "#ffffff" } }),
             "text-halo-width": 1.2 },
  })
  for (const id of [`${prefix}-mark`, `${prefix}-jam`, `${prefix}-stretch`]) bindOnce(map, id)
}

// -- the general layer: every report, at the clock's moment ------------------------------------------

const answers = new Set<string>() // answer layers drawing reports of their own
let timer: number | undefined
let ctrl: AbortController | null = null
let moment: string | null = null
let added: string[] = []

function demand() {
  const open = useInspector.getState()
  return useReports.getState().on || (open.open && (open.entity?.kind as string) === "road_report")
}

async function poll() {
  window.clearTimeout(timer)
  if (!demand()) return
  if (!document.hidden) {
    ctrl?.abort()
    const mine = (ctrl = new AbortController())
    if (useReports.getState().status === "idle" || useReports.getState().at !== moment) useReports.setState({ status: "loading" })
    try {
      const body = await fetchReports(moment, mine.signal)
      const src = getMap()?.getSource(SRC) as GeoJSONSource | undefined
      src?.setData(body as any)
      useReports.setState({ status: "ready", error: null, body, at: moment })
    } catch (e) {
      if ((e as Error).name !== "AbortError") useReports.setState({ status: "error", error: (e as Error).message })
    }
  }
  // A feed the server hasn't read yet (it just started) is asked about again in a few seconds.
  const waiting = Object.values(useReports.getState().body?.meta.feeds ?? {}).some((f) => f.state === "waiting")
  if (!moment) timer = window.setTimeout(poll, waiting ? 5_000 : POLL_MS)
}

function sync() {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    if (demand()) {
      if (!map.getSource(SRC)) {
        map.addSource(SRC, { type: "geojson", data: useReports.getState().body ?? { type: "FeatureCollection", features: [] } } as any)
        added = []
        addReportLayers(map, PREFIX + "all", SRC, added)
      }
      const visible = answers.size ? "none" : "visible" // an answer's own reports stand in
      for (const id of added) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible)
      void poll()
    } else {
      for (const id of [...added].reverse()) if (map.getLayer(id)) map.removeLayer(id)
      added = []
      if (map.getSource(SRC)) map.removeSource(SRC)
      window.clearTimeout(timer)
      ctrl?.abort()
    }
  })
}

export function setReportsOn(on: boolean) {
  useReports.setState({ on })
  sync()
}

export function refreshReports() {
  if (demand()) void poll()
}

// -- answers ---------------------------------------------------------------------------------------------

registerLayerRenderer({
  id: "road-reports",
  claims: (d: LayerData) => (d as any).meta?.live?.kind === "road_reports",
  add: (ref, src, _data, ids) => {
    const map = getMap()
    answers.add(ref)
    addReportLayers(map, `${ref}::rr`, src, ids)
    sync()
  },
  remove: (ref) => {
    answers.delete(ref)
    sync()
  },
})

registerClickHandler({
  id: "road-reports",
  priority: 14, // above the road speeds: a report sits on its road
  layers: (l) => l.startsWith(PREFIX) || l.includes("::rr-"),
  resolve: (f, at) => {
    const id = f.properties?.id
    if (!id || f.properties?.part === "route") return null
    const center = f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
    return { id: String(id), at: center }
  },
})

// -- start --------------------------------------------------------------------------------------------

let started = false
export function startReportMap() {
  if (started) return
  started = true
  const wait = () => {
    const map = getMap()
    if (!map) { window.setTimeout(wait, 200); return }
    map.on("styleimagemissing", (e: any) => { if (String(e.id).startsWith("rr-")) ensureImages(map) })
  }
  wait()
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshReports() })
  // The reports follow the city clock (read from the twin's memory at its moment). A moment that
  // keeps moving (a replay) is read once it rests.
  let clockTimer: number | undefined
  useClock.subscribe((st, prev) => {
    if (st.at === prev.at) return
    window.clearTimeout(clockTimer)
    clockTimer = window.setTimeout(() => {
      moment = st.at === null ? null : String(st.at)
      refreshReports()
    }, st.playing ? 600 : 150)
  })
  // Opening a report brings the others with it, and closing it lets them go again.
  let kind: string | undefined
  useInspector.subscribe((st) => {
    const k = st.open ? (st.entity?.kind as string | undefined) : undefined
    if (k === kind) return
    const was = kind
    kind = k
    if (k === "road_report" || was === "road_report") sync()
  })
}

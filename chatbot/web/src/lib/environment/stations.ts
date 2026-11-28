/* The measuring stations on the page: the Department of Meteorology's weather stations and the
   national network's reference air stations, at the clock's moment. One store for the Layers
   sheet's row, the Outside card's readout and the map; the map draws the weather stations as
   dots with their temperature and the air stations as rings with their NO₂, and a click opens
   the station in the inspector. The server never asks the services for a page: it answers from
   what its poller read and the twin's memory recorded (/api/environment/stations). */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"
import { create } from "zustand"

import { useClock } from "@/lib/clock"
import { getJson } from "@/lib/environment/store"
import { getMap, onStyleReady, themedPaint, useMapUi } from "@/lib/map/engine"
import { registerClickHandler } from "@/lib/registries/map-clicks"
import { registerActiveSources } from "@/lib/sources"

/* eslint-disable @typescript-eslint/no-explicit-any */

export type StationReading = { value: number; unit: string; label: string; as_of: string; age_min: number; until?: string; kmh?: number }
export type MeasuredStation = {
  id: string; kind: "weather_station" | "reference_air_station"; code: string; name: string; detail: string
  lat: number; lon: number; type: string | null; known: boolean; as_of: string | null; age_min: number | null
  readings: Record<string, StationReading>; rain_last_hour_mm?: number
}
export type StationsData = {
  at: string | null; stations: MeasuredStation[]
  feeds: Record<"met" | "eea", { read_at: string | null; ok: boolean; error: string | null }>
}
export type Comparison = {
  sensor_id: string; sensor: string; metric: string; label: string; unit: string; reference_id: string; reference: string
  distance_m: number; hours: number; days: number; bias?: number; bias_pct?: number; rmse?: number; r?: number
  text?: string; note?: string
}
type Status = "idle" | "loading" | "ready" | "error"

type Stations = { on: boolean; status: Status; error: string | null; data: StationsData | null; at: number | null }

export const useStations = create<Stations>(() => ({ on: false, status: "idle", error: null, data: null, at: null }))

const SRC = "env-measured-src"
const DOTS = "env-measured-dots"
const VALUES = "env-measured-values"
const FONT_BOLD = ["Noto Sans Bold"]
const REFRESH_MS = 5 * 60e3
// Weather stations a calm blue, the reference air stations white rings: neither is sodium, which
// stays for what is live or selected.
export const WEATHER_DOT = "#8fb8e8"
export const AIR_RING = "#f4f7fb"

export const fetchStations = (at: number | null, signal?: AbortSignal) =>
  getJson<StationsData>(`/api/environment/stations${at === null ? "" : `?at=${at}`}`, signal)

export const fetchComparison = (params: { sensor?: string; station?: string }, signal?: AbortSignal) => {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][])
  return getJson<{ days: number; rows: Comparison[] }>(`/api/environment/compare?${q}`, signal)
}

let abort: AbortController | null = null

// Read the stations at the clock's moment (the store follows the clock while anything shows them).
export function loadStations() {
  abort?.abort()
  const ac = (abort = new AbortController())
  const at = useClock.getState().at
  useStations.setState({ status: "loading", at })
  fetchStations(at, ac.signal)
    .then((data) => {
      if (ac.signal.aborted) return
      useStations.setState({ data, status: "ready", error: null })
      draw()
    })
    .catch((e) => {
      if (ac.signal.aborted) return
      useStations.setState({ status: "error", error: (e as Error).message })
    })
}

export function setStationsOn(on: boolean) {
  useStations.setState({ on })
  if (on) loadStations()
  draw()
}

// The headline of a station: its temperature, or its NO₂ (the traffic station's PM10 when dust).
export function headline(s: MeasuredStation): { key: string; text: string } | null {
  const r = s.kind === "weather_station" ? s.readings.temperature_c ?? s.readings.rain_10min_mm : s.readings.no2
  if (!r) return null
  if (s.kind === "weather_station") {
    return s.readings.temperature_c
      ? { key: "temperature_c", text: `${r.value.toFixed(1)}°` }
      : { key: "rain_10min_mm", text: `${r.value} mm` }
  }
  return { key: "no2", text: `NO₂ ${Math.round(r.value)}` }
}

function features(data: StationsData | null) {
  return {
    type: "FeatureCollection" as const,
    features: (data?.stations ?? []).map((s) => ({
      type: "Feature" as const,
      geometry: { type: "Point" as const, coordinates: [s.lon, s.lat] },
      properties: { id: s.id, air: s.kind === "reference_air_station" ? 1 : 0, known: s.known ? 1 : 0, value: headline(s)?.text ?? "" },
    })),
  }
}

function addLayers(map: MlMap) {
  if (!map.getLayer(DOTS)) {
    map.addLayer({
      id: DOTS, type: "circle", source: SRC,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, ["case", ["==", ["get", "air"], 1], 6, 5], 15, ["case", ["==", ["get", "air"], 1], 10, 8]],
        "circle-color": ["case", ["==", ["get", "air"], 1], "rgba(0,0,0,0)", WEATHER_DOT],
        "circle-opacity": ["case", ["==", ["get", "known"], 1], 1, 0.45],
        ...themedPaint(DOTS, { "circle-stroke-color": { night: "#e4ebf4", day: "#0e1828" } }),
        "circle-stroke-width": ["case", ["==", ["get", "air"], 1], 3, 1.4],
      },
    })
  }
  if (!map.getLayer(VALUES)) {
    map.addLayer({
      id: VALUES, type: "symbol", source: SRC,
      // Below the dot: a surface's own value for the same station sits above it.
      layout: { "text-field": ["get", "value"], "text-font": FONT_BOLD, "text-size": 12, "text-offset": [0, 1.55], "text-allow-overlap": true },
      paint: {
        ...themedPaint(VALUES, { "text-color": { night: "#e4ebf4", day: "#0e1828" }, "text-halo-color": { night: "#0e1828", day: "#ffffff" } }),
        "text-halo-width": 1.6,
      },
    })
  }
}

function draw() {
  const map = getMap()
  if (!map || !map.isStyleLoaded()) return
  const { on, data } = useStations.getState()
  if (!on) {
    for (const l of [VALUES, DOTS]) if (map.getLayer(l)) map.removeLayer(l)
    if (map.getSource(SRC)) map.removeSource(SRC)
    return
  }
  const fc = features(data)
  const src = map.getSource(SRC) as GeoJSONSource | undefined
  if (src) src.setData(fc as any)
  else map.addSource(SRC, { type: "geojson", data: fc as any })
  addLayers(map)
}

registerClickHandler({
  id: "measured-stations",
  priority: 10,
  layers: (l) => l === DOTS || l === VALUES,
  resolve: (f, at) => (f.properties?.id ? { id: String(f.properties.id), at } : null),
})

registerActiveSources(() => (useStations.getState().on ? ["met_stations", "eea_air"] : []), (fn) => useStations.subscribe(fn))

let started = false

// The stations follow the clock and the base map while they show; live, they are read again
// every five minutes (the Met readings change every ten).
export function startStations() {
  if (started) return
  started = true
  let timer: number | undefined
  useClock.subscribe((st, prev) => {
    if (st.at === prev.at) return
    window.clearTimeout(timer)
    timer = window.setTimeout(() => { if (useStations.getState().on || watchers > 0) loadStations() }, st.playing ? 700 : 200)
  })
  let lastBase = useMapUi.getState().basemap
  useMapUi.subscribe((st) => {
    if (st.basemap === lastBase) return
    lastBase = st.basemap
    onStyleReady(draw)
  })
  const hook = () => {
    const map = getMap()
    if (!map) { window.setTimeout(hook, 200); return }
    map.on("style.load", draw)
    draw()
  }
  hook()
  window.setInterval(() => {
    if (document.hidden || useClock.getState().at !== null) return
    if (useStations.getState().on || watchers > 0) loadStations()
  }, REFRESH_MS)
}

// A readout (the Outside card) that wants the stations without drawing them.
let watchers = 0
export function watchStations() {
  watchers += 1
  const { data, status, at } = useStations.getState()
  if (status !== "loading" && (!data || at !== useClock.getState().at)) loadStations()
  return () => { watchers = Math.max(0, watchers - 1) }
}

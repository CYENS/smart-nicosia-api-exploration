/* The environment twin on the page: which of its layers are on, and what the server says about
   them at the clock's moment. The map (lib/environment/map.ts), the map panel's switches, the
   Environment workspace and the inspector's spot read this one store. */
import { create } from "zustand"

import type { Metric } from "@/lib/environment/colors"

export type Band = { to: number | null; label: string }
export type Grid = { bbox: [number, number, number, number]; nx: number; ny: number; values: (number | null)[]; confidence: number[]; min: number | null; max: number | null }
export type StationValue = { id: string; name: string; lat: number; lon: number; value: number; model: number | null; band: string | null; estimate: number | null }
export type Surface = {
  metric: Metric; label: string; unit: string; known: boolean; reason?: string; at: string | null
  model_hour?: string | null; grid?: Grid; stations?: StationValue[]; bands?: Band[]
  background?: "model" | "stations"; model_bias?: number | null; spread?: number; reach_m?: number
  notes?: string[]
}
export type Wind = {
  known: boolean; reason?: string; at: string | null; model_hour?: string
  lats: number[]; lons: number[]; u: (number | null)[]; v: (number | null)[]; speed_ms: (number | null)[]
  max_ms: number | null; centre: { speed_ms: number | null; from_deg: number | null; from: string | null; gusts_ms: number | null }
}
export type RadarFrame = { time: number; path: string; tiles: string }
export type Radar = { known: boolean; frames: RadarFrame[]; max_zoom: number; reason: string | null }
export type DayPoint = { time: string; sun_pct: number; sun_altitude_deg: number }
export type SunHours = {
  date: string; center: [number, number]; radius_m: number; bounds: [number, number][]; nx: number; ny: number
  cell_m: number; grid: string; unit: string; profile: DayPoint[]
  sun: { sunrise: string | null; solar_noon: string; sunset: string | null; noon_altitude_deg: number; daylight_h: number }
  hours: { mean: number | null; min: number | null; max: number | null; daylight_h: number; under_2h_pct?: number; over_6h_pct?: number }
  tiles_missing: number
}
export type Place = { name: string; lat: number; lon: number }
type Status = "idle" | "loading" | "ready" | "error"

export const METRICS: { id: Metric; label: string; short: string; unit: string; what: string }[] = [
  { id: "pm2_5", label: "PM2.5", short: "PM2.5", unit: "µg/m³", what: "Fine particles, stations over the air model" },
  { id: "no2", label: "NO₂", short: "NO₂", unit: "µg/m³", what: "Nitrogen dioxide, mostly traffic" },
  { id: "pm10", label: "PM10", short: "PM10", unit: "µg/m³", what: "Particles and dust" },
  { id: "o3", label: "O₃", short: "O₃", unit: "µg/m³", what: "Ozone, high on sunny afternoons" },
  { id: "caqi", label: "Air quality index", short: "CAQI", unit: "", what: "The stations' own index, near them only" },
  { id: "noise_db", label: "Noise", short: "Noise", unit: "dB", what: "Measured at the stations, very local" },
  { id: "temperature_c", label: "Temperature", short: "Temp", unit: "°C", what: "Stations over the weather model" },
]

// Squares and streets to read the sun hours of, and their middles.
export const PLACES: Place[] = [
  { name: "Eleftheria Square", lat: 35.17017, lon: 33.35992 },
  { name: "Ledras Street", lat: 35.1734, lon: 33.36138 },
  { name: "Faneromeni Square", lat: 35.17348, lon: 33.36285 },
  { name: "Solomou Square", lat: 35.17095, lon: 33.35835 },
]

type Env = {
  terrain: { on: boolean; exaggeration: number }
  shadows: { on: boolean; note: string }
  surface: { metric: Metric | null; status: Status; error: string | null; data: Surface | null }
  wind: { on: boolean; status: Status; error: string | null; data: Wind | null; answers: number }
  rain: { on: boolean; status: Status | "old"; error: string | null; frame: RadarFrame | null; radar: Radar | null }
  sunHours: { status: Status; error: string | null; place: Place; date: string; radius: number; data: SunHours | null; shown: boolean }
}

function today() {
  return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Nicosia" }) // YYYY-MM-DD
}

export const useEnvironment = create<Env>(() => ({
  terrain: { on: false, exaggeration: 1.5 },
  shadows: { on: false, note: "" },
  surface: { metric: null, status: "idle", error: null, data: null },
  wind: { on: false, status: "idle", error: null, data: null, answers: 0 },
  rain: { on: false, status: "idle", error: null, frame: null, radar: null },
  sunHours: { status: "idle", error: null, place: PLACES[0], date: today(), radius: 150, data: null, shown: false },
}))

export function patch<K extends keyof Env>(key: K, value: Partial<Env[K]>) {
  useEnvironment.setState((s) => ({ [key]: { ...s[key], ...value } }) as Pick<Env, K>)
}

export async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

function q(params: Record<string, string | number | null | undefined>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ""
}

export const fetchSurface = (metric: Metric, at: number | null, signal?: AbortSignal) =>
  getJson<Surface>(`/api/environment/surface${q({ metric, at })}`, signal)
export const fetchWind = (at: number | null, signal?: AbortSignal) => getJson<Wind>(`/api/environment/wind${q({ at })}`, signal)
export const fetchRadar = (signal?: AbortSignal) => getJson<Radar>("/api/environment/radar", signal)
export const fetchSunHours = (p: Place, date: string, radius: number, signal?: AbortSignal) =>
  getJson<SunHours>(`/api/environment/sun-hours${q({ lat: p.lat, lon: p.lon, radius_m: radius, date })}`, signal)

// A spot's id, as the server reads it: spot-<lat>-<lon> with five decimals.
export function spotId(lat: number, lon: number) {
  return `spot-${lat.toFixed(5)}-${lon.toFixed(5)}`
}

export function bandOf(bands: Band[] | undefined, v: number | null | undefined) {
  if (!bands || v === null || v === undefined) return -1
  const k = bands.findIndex((b) => b.to === null || v < b.to)
  return k < 0 ? bands.length - 1 : k
}

export function fmtValue(v: number | null | undefined, unit = "") {
  if (v === null || v === undefined) return "unknown"
  const n = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10
  return `${n.toLocaleString("en-GB")}${unit ? ` ${unit}` : ""}`
}

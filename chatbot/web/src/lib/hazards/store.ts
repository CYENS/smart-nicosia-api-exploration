/* Natural hazards on the page: what /api/hazards says at the city clock's moment (earthquakes,
   weather warnings, the Nicosia district's fire danger), read while something shows it (the
   Layers row, the Outside card) and again every five minutes while live. */
import { create } from "zustand"

import { toWall, useClock } from "@/lib/clock"
import type { FC } from "@/lib/map/engine"

export type Felt = { count?: number; located?: number; near_nicosia?: number; near_intensity?: number | null; near_words?: string | null; read_at?: string }
export type Quake = {
  id: string; title: string; start: string; lat: number; lon: number; value: number; magtype?: string
  depth_km?: number; distance_km?: number; direction?: string; where?: string; located_by?: string; felt?: Felt
}
export type Warning = {
  id: string; title: string; start: string; end: string | null; level: "yellow" | "orange" | "red"
  awareness?: string; area?: string; covers_nicosia?: boolean; in_force?: boolean; headline?: string
  description?: string; instruction?: string; withdrawn_at?: string
}
export type FireDay = {
  id: string; title: string; day: string; level: string; shares?: Record<string, number>; forecast?: boolean
  city_level?: string | null; grid?: string; size?: [number, number]; bbox?: [number, number, number, number]
}
export type FeedStatus = { source: string; read_at: string | null; error: string | null; note: string | null }
export type HazardsAt = {
  at: string; live: boolean; earthquakes: Quake[]; warnings: Warning[]; fire: FireDay | null; fire_ahead?: FireDay[]
  status: Record<"earthquakes" | "warnings" | "fire", FeedStatus>
  incidents: Record<string, string>; features: FC["features"]; legend: Record<string, string>; meta: Record<string, unknown>
}

type Load = "idle" | "loading" | "ready" | "error"

export const useHazards = create<{ data: HazardsAt | null; status: Load; error: string | null; at: number | null; users: number }>(() => ({
  data: null, status: "idle", error: null, at: null, users: 0,
}))

export const LEVEL_COLOR: Record<string, string> = { yellow: "#e8c547", orange: "#ff8a3d", red: "#ff5a4e" }
export const FIRE_COLOR: Record<string, string> = {
  low: "#8798b0", moderate: "#9fb0c6", high: "#f5a524", very_high: "#ff8a3d", extreme: "#ff5a4e", very_extreme: "#c81e3c",
}
export const FIRE_WORD: Record<string, string> = {
  low: "Low", moderate: "Moderate", high: "High", very_high: "Very high", extreme: "Extreme", very_extreme: "Very extreme",
}
export const FIRE_CLASSES = ["low", "moderate", "high", "very_high", "extreme", "very_extreme"]
export const HAZARD_FAMILIES = new Set(["quake", "warning", "fire"])

// "Thu 24 Jul, 11:00" from a wall time ("2025-07-24T11:00").
export function wallWords(s: string | null | undefined, opts: { day?: boolean } = {}) {
  if (!s) return ""
  const d = new Date(`${s}:00Z`)
  const time = s.slice(11, 16)
  if (opts.day === false) return time
  return `${d.toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short" })}, ${time}`
}

let seq = 0
let timer: number | undefined

export async function loadHazards(force = false) {
  const at = useClock.getState().at
  const s = useHazards.getState()
  if (!force && s.status === "loading" && s.at === at) return
  const mine = ++seq
  useHazards.setState({ status: s.data ? s.status : "loading", at })
  try {
    const res = await fetch(`/api/hazards${at === null ? "" : `?at=${encodeURIComponent(toWall(at))}`}`)
    if (!res.ok) throw new Error(res.status === 400 ? (await res.json()).detail ?? `HTTP ${res.status}` : `HTTP ${res.status}`)
    const data = (await res.json()) as HazardsAt
    if (mine === seq) useHazards.setState({ data, status: "ready", error: null })
  } catch (e) {
    if (mine === seq) useHazards.setState({ status: "error", error: e instanceof Error ? e.message : String(e) })
  }
}

// Something shows the hazards (the Layers row, the Outside card): read them now and keep them
// current; returns the release.
export function needHazards() {
  useHazards.setState((s) => ({ users: s.users + 1 }))
  if (useHazards.getState().status === "idle" || useHazards.getState().at !== useClock.getState().at) void loadHazards()
  return () => useHazards.setState((s) => ({ users: Math.max(0, s.users - 1) }))
}

let started = false

export function startHazardsStore() {
  if (started) return
  started = true
  let wait: number | undefined
  useClock.subscribe((s, prev) => {
    if (s.at === prev.at || useHazards.getState().users === 0) return
    window.clearTimeout(wait)
    wait = window.setTimeout(() => void loadHazards(), 250)
  })
  timer = window.setInterval(() => {
    if (useHazards.getState().users > 0 && useClock.getState().at === null && !document.hidden) void loadHazards(true)
  }, 5 * 60e3)
}

export function stopHazardsStore() {
  window.clearInterval(timer)
}

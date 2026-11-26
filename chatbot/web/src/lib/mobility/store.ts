/* Live mobility on the page: what the server says about the buses and the roads, and which of
   them the map shows. One poll of each while it is wanted, shared by the map, the strip, the
   Mobility workspace and the inspector. */
import { create } from "zustand"

export type LiveBus = {
  id: string; line: string; line_id?: string; color: string; text_color: string; headsign?: string
  lon: number; lat: number; heading?: number; speed_kmh?: number; reported: number
  along_m?: number; shape_id?: string; delay_s?: number; delay_source?: "feed" | "timetable"
  finished?: boolean; waiting?: string
}
export type BusesBody = {
  status: "live" | "stale" | "unavailable"; error: string | null; count: number
  fetched_at: number; feed_time: number | null; timetable: "ready" | "loading"; buses: LiveBus[]
}
export type RoadState = "moving" | "busy" | "slow" | "very_slow" | "no_reading"
export type RoadsBody = GeoJSON.FeatureCollection<GeoJSON.LineString, {
  id: string; label: string; state: RoadState; color: string; speed_kmh: number | null
  free_flow_kmh: number | null; share: number | null; name: string
}> & { meta: { status: string; error: string | null; measured: number; total: number; at: string | null; free_flow: string[] } }

export type LineRow = { id: string; line: string; name: string | null; operator: string | null; color: string; buses: number; late: number; mean_delay_min: number | null }
export type RoadRow = {
  id: string; name: string; road: string | null; speed_kmh: number | null; free_flow_kmh: number | null
  free_flow: string; share_of_free_flow: number | null; state: RoadState; state_label: string
  usual_kmh: number | null; measured_at: string | null
}
export type Summary = {
  buses?: { status: string; running: number; lines?: number; late?: number; by_line?: LineRow[]; error?: string | null }
  roads?: { status: string; measured?: number; total?: number; by_state?: Record<string, number>; slowest?: RoadRow[]; error?: string | null }
  cameras?: { camera: string; lat: number | null; lon: number | null; people?: number; car?: number; bus?: number; bike?: number; total?: number }[] | null
}

// Answer layers can narrow the live buses to some lines or a place.
export type BusFilter = { lines: string[] | null; near: { lat: number; lon: number; radius_m: number } | null }

type Mobility = {
  buses: {
    on: boolean; status: "idle" | "loading" | "live" | "stale" | "unavailable"; error: string | null
    count: number; fetchedAt: number; filter: BusFilter | null
  }
  roads: {
    on: boolean; status: "idle" | "loading" | "live" | "recorded" | "unavailable" | "no_readings"
    error: string | null; measured: number; total: number; at: string | null; freeFlow: string[]
  }
  summary: Summary | null
  summaryStatus: "idle" | "loading" | "ready" | "error"
}

export const useMobility = create<Mobility>(() => ({
  buses: { on: false, status: "idle", error: null, count: 0, fetchedAt: 0, filter: null },
  roads: { on: false, status: "idle", error: null, measured: 0, total: 0, at: null, freeFlow: [] },
  summary: null,
  summaryStatus: "idle",
}))

export function setBusState(patch: Partial<Mobility["buses"]>) {
  useMobility.setState((s) => ({ buses: { ...s.buses, ...patch } }))
}

export function setRoadState(patch: Partial<Mobility["roads"]>) {
  useMobility.setState((s) => ({ roads: { ...s.roads, ...patch } }))
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export const fetchBuses = (signal?: AbortSignal) => getJson<BusesBody>("/api/buses", signal)
export const fetchRoads = (at: string | null, signal?: AbortSignal) =>
  getJson<RoadsBody>(`/api/roads${at ? `?${new URLSearchParams({ at })}` : ""}`, signal)
export const fetchShape = (id: string) => getJson<{ id: string; coordinates: [number, number][] }>(`/api/transit/shapes/${encodeURIComponent(id)}`)
export const fetchStops = () => getJson<GeoJSON.FeatureCollection<GeoJSON.Point>>("/api/transit/stops")

// The workspace and the strip read the summary; it refreshes every 30 s while one of them shows.
let summaryUsers = 0
let summaryTimer: number | undefined

async function loadSummary() {
  if (useMobility.getState().summaryStatus === "idle") useMobility.setState({ summaryStatus: "loading" })
  try {
    const summary = await getJson<Summary>("/api/mobility")
    useMobility.setState({ summary, summaryStatus: "ready" })
  } catch {
    useMobility.setState((s) => ({ summaryStatus: s.summary ? "ready" : "error" }))
  }
}

export function watchSummary() {
  summaryUsers++
  if (summaryUsers === 1) {
    void loadSummary()
    summaryTimer = window.setInterval(() => { if (!document.hidden) void loadSummary() }, 30_000)
  }
  return () => {
    summaryUsers--
    if (summaryUsers === 0) window.clearInterval(summaryTimer)
  }
}

export function refreshSummary() {
  void loadSummary()
}

// "3 min late", "on time", "2 min early".
export function delayText(s: number | null | undefined) {
  if (s === null || s === undefined) return null
  const m = Math.round(s / 60)
  if (Math.abs(m) < 1) return "on time"
  return m > 0 ? `${m} min late` : `${-m} min early`
}

/* The island's grid on the page: demand, wind, solar and carbon every 15 minutes, from the grid
   operator through /api/grid (chatbot/energy). Island-wide figures, so they live on the Outside
   card and in the inspector (grid-cyprus), never painted on Nicosia's streets.

   Live, the snapshot is read once on load and every five minutes (the server reads the
   operator's page only when a new quarter hour is due). At a past moment of the city clock the
   card reads that moment from the twin's memory. The day's demand peak is a mark on the
   clock's track. */
import { ZapIcon } from "lucide-react"
import { create } from "zustand"

import { useClock, toWall } from "@/lib/clock"
import { KINDS } from "@/lib/entities"
import { setTrackMarks } from "@/lib/registries/track-marks"

export const GRID_ID = "grid-cyprus"
export const REFRESH_MS = 5 * 60e3

// The grid's own colours, shared with its chart in the answers and its map layer.
export const COLORS = { conventional_mw: "#b8835c", wind_mw: "#3fc1b0", solar_mw: "#f2cf4a" } as const
export const STACK = ["conventional_mw", "wind_mw", "solar_mw"] as const
export const NAMES: Record<(typeof STACK)[number], string> = {
  conventional_mw: "Oil-fired stations", wind_mw: "Wind", solar_mw: "Solar, estimated",
}

export type GridPoint = {
  t: string; demand_mw: number | null; conventional_mw: number | null; wind_mw: number | null
  solar_mw: number | null; capacity_mw: number | null; carbon_kg_kwh: number | null
  renewable_pct: number | null; wind_pct: number | null; solar_pct: number | null; age_min?: number
}
export type GridSnapshot = {
  status: "live" | "stale" | "unavailable" | "known" | "unknown"
  live: boolean; at: string; now: GridPoint | null; day: GridPoint[]
  day_from: string; day_span: "today" | "last_24_hours" // the curve's start: midnight, or 24 hours back
  usual: { t: string; demand_mw: number; carbon_kg_kwh: number | null }[]; usual_days: number
  peak: { t: string; demand_mw: number } | null
  cleanest: { t: string; carbon_kg_kwh: number } | null
  installed: Record<string, number>; where: string; method: string; solar_note: string
  fetched_at: string | null; error: string | null; reason?: string
}

type Load = { key: string; status: "loading" | "ready" | "error"; snap: GridSnapshot | null; error?: string }
type Power = { live: Load | null; past: Load | null }

export const usePower = create<Power>(() => ({ live: null, past: null }))

KINDS.power_grid = { label: "Electricity grid", icon: ZapIcon, chip: "place" }

async function fetchGrid(at: number | null): Promise<GridSnapshot> {
  const r = await fetch(`/api/grid${at === null ? "" : `?at=${encodeURIComponent(toWall(at))}`}`)
  if (!r.ok) throw new Error(`the server answered ${r.status}`)
  return r.json() as Promise<GridSnapshot>
}

// A moment of the clock to the quarter hour the grid has rows for.
function quarterKey(at: number) {
  return toWall(at - (at % (15 * 60e3)))
}

export function loadLive(): Promise<void> {
  const key = `live ${Math.floor(Date.now() / REFRESH_MS)}`
  const was = usePower.getState().live
  if (was?.key === key && was.status !== "error") return Promise.resolve()
  usePower.setState({ live: { key, status: "loading", snap: was?.snap ?? null } })
  return fetchGrid(null).then(
    (snap) => { usePower.setState({ live: { key, status: "ready", snap } }); markPeak(snap) },
    (e: Error) => usePower.setState({ live: { key, status: "error", snap: was?.snap ?? null, error: e.message } }),
  )
}

export function loadPast(at: number): Promise<void> {
  const key = quarterKey(at)
  const was = usePower.getState().past
  if (was?.key === key && was.status !== "error") return Promise.resolve()
  usePower.setState({ past: { key, status: "loading", snap: null } })
  return fetchGrid(at).then(
    (snap) => { if (usePower.getState().past?.key === key) usePower.setState({ past: { key, status: "ready", snap } }) },
    (e: Error) => { if (usePower.getState().past?.key === key) usePower.setState({ past: { key, status: "error", snap: null, error: e.message } }) },
  )
}

// The snapshot for the clock's moment: live, or the past one once it has loaded.
export function useGridAt(): Load | null {
  const past = useClock((s) => s.at !== null)
  return usePower((s) => (past ? s.past : s.live))
}

export function hm(iso: string) {
  return iso.slice(11, 16)
}

function markPeak(snap: GridSnapshot) {
  const p = snap.peak
  setTrackMarks("grid-peak", p ? [{
    t: Date.parse(p.t), color: "#ffae45", id: GRID_ID,
    label: `The island's demand peak ${snap.day_span === "today" ? "so far today" : "in the last 24 hours"}: ${p.demand_mw.toLocaleString("en-GB")} MW at ${hm(p.t)}`,
  }] : [])
}

// Live: read now and every five minutes while the page is open; at a past moment, when it moves.
if (typeof window !== "undefined") {
  void loadLive()
  window.setInterval(() => { if (document.visibilityState === "visible") void loadLive() }, REFRESH_MS)
  useClock.subscribe((s, prev) => {
    if (s.at !== null && (prev.at === null || quarterKey(s.at) !== quarterKey(prev.at))) void loadPast(s.at)
  })
}

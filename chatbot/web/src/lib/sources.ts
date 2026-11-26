/* Where the data comes from: the server's list of sources (/api/sources, chatbot/sources.py)
   with how fresh each live feed is, which of them the map is drawing now, and the data sources
   panel that lists them all. A "From ..." line anywhere on the page opens the panel at its
   source. */
import { create } from "zustand"

import { useMapUi } from "@/lib/map/engine"
import { useMobility } from "@/lib/mobility/store"

export type SourceState = "live" | "stale" | "unavailable" | "waiting"
export type SourcePart = { name: string; count: number | null; state: SourceState; as_of: string | null; note: string | null }
export type SourceStatus = {
  state: SourceState | null; as_of: string | null; note: string | null
  parts?: SourcePart[]; holds_from?: string | null
}
export type DataSource = {
  id: string; name: string; short: string; publisher: string; gives: string; group: string
  url: string | null; licence: string | null; licence_url: string | null; refresh: string | null
  credit: string | null; status: SourceStatus | null
}

type Sources = {
  list: DataSource[]
  groups: string[]
  status: "idle" | "loading" | "ready" | "error"
  open: boolean
  focus: string | null // the source the panel opened at
  opener: HTMLElement | null // what opened the panel: focus goes back there when it closes
}

export const useSources = create<Sources>(() => ({
  list: [], groups: [], status: "idle", open: false, focus: null, opener: null,
}))

let inflight: Promise<void> | null = null

export function loadSources(): Promise<void> {
  if (inflight) return inflight
  if (!useSources.getState().list.length) useSources.setState({ status: "loading" })
  inflight = fetch("/api/sources")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((body: { groups: string[]; sources: DataSource[] }) => {
      useSources.setState({ list: body.sources, groups: body.groups, status: "ready" })
    })
    .catch(() => { useSources.setState({ status: useSources.getState().list.length ? "ready" : "error" }) })
    .finally(() => { inflight = null })
  return inflight
}

export function openSources(focus: string | null = null, opener: Element | null = null) {
  const el = opener instanceof HTMLElement ? opener : document.activeElement instanceof HTMLElement ? document.activeElement : null
  useSources.setState({ open: true, focus, opener: el })
  void loadSources()
}

export function closeSources() {
  useSources.setState({ open: false })
}

export function sourceById(id: string): DataSource | undefined {
  return useSources.getState().list.find((s) => s.id === id)
}

// "Smart Nicosia", "the live bus feed"... (the id itself until the list has loaded).
export function shortName(id: string, list = useSources.getState().list): string {
  return list.find((s) => s.id === id)?.short ?? id.replace(/_/g, " ")
}

// The sources of what the map draws now: the assistant's layers, the 3D city, the registers,
// the lamps that glow after dark, and the live buses and roads.
export function activeSourceIds(): string[] {
  const ui = useMapUi.getState()
  const mob = useMobility.getState()
  const ids: string[] = []
  for (const l of ui.layers) ids.push(...(l.sources ?? []))
  if (ui.nightLights) ids.push("smart_nicosia")
  if (ui.city.on) ids.push("dls")
  if (ui.terrain) ids.push("terrain")
  if (ui.registers.on) ids.push("registers", "dls")
  if (mob.buses.on) ids.push("bus_live", "bus_timetable")
  if (mob.roads.on) ids.push("road_speeds")
  return [...new Set(ids)]
}

export function useActiveSourceIds(): string[] {
  // Subscribing to the parts keeps the list fresh; the ids are cheap to work out again.
  useMapUi((s) => s.layers)
  useMapUi((s) => s.nightLights)
  useMapUi((s) => s.city.on)
  useMapUi((s) => s.terrain)
  useMapUi((s) => s.registers.on)
  useMobility((s) => s.buses.on)
  useMobility((s) => s.roads.on)
  return activeSourceIds()
}

// "read 2 min ago", "read at 09:40", "read on 20 Nov".
export function readAgo(iso: string | null, now = Date.now()): string | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const min = Math.round((now - t) / 60_000)
  if (min < 1) return "just now"
  if (min < 60) return `${min} min ago`
  const d = new Date(t)
  const sameDay = new Date(now).toDateString() === d.toDateString()
  return sameDay
    ? `at ${d.toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })}`
    : `on ${d.toLocaleDateString("en-GB", { timeZone: "Asia/Nicosia", day: "numeric", month: "short" })}`
}

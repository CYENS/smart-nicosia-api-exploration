/* Road reports on the page: closures, jams, accidents, hazards, roadworks and diversions in
   Greater Nicosia, from /api/road-reports (now, or at the city clock's moment). One read while
   they are wanted (the Layers switch, the Traffic card, an open report), shared by the map, the
   card and the Layers sheet. */
import { create } from "zustand"

export type ReportKind = "accident" | "closure" | "jam" | "hazard" | "roadworks" | "diversion"
export const REPORT_KINDS: ReportKind[] = ["accident", "closure", "jam", "hazard", "roadworks", "diversion"]

export type ReportItem = {
  id: string; kind: ReportKind; kind_label: string; label: string; street: string | null; color: string
  source: string; status: "active" | "planned" | "ended"; since: string | null; until: string | null
  age: string | null; old: boolean; lon: number; lat: number
}
export type FeedState = { state: string; error?: string | null; note?: string | null; count?: number; name: string }
export type ReportsMeta = {
  status: "live" | "stale" | "unavailable" | "recorded" | "unknown"
  error: string | null; at: string | null; label: string | null
  feeds: Record<string, FeedState>; legend: Record<string, string>
  kinds: Record<ReportKind, { label: string; one: string; many: string; color: string }>
  counts: Partial<Record<ReportKind, number>>; in_words: string; total: number
  recent: Partial<Record<ReportKind, number>>; recent_words: string | null
  newest_jam: ReportItem | null; long_closures: number; planned: number; list: ReportItem[]
}
export type ReportsBody = GeoJSON.FeatureCollection<GeoJSON.Geometry, Record<string, any>> & { meta: ReportsMeta } // eslint-disable-line @typescript-eslint/no-explicit-any

type Reports = {
  on: boolean // the Layers sheet's switch (the Traffic card turns it on while it shows)
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  body: ReportsBody | null
  at: string | null // the moment the body is for (null: now)
}

export const useReports = create<Reports>(() => ({ on: false, status: "idle", error: null, body: null, at: null }))

export async function fetchReports(at: string | null, signal?: AbortSignal): Promise<ReportsBody> {
  const res = await fetch(`/api/road-reports${at ? `?${new URLSearchParams({ at })}` : ""}`, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  }
  return res.json() as Promise<ReportsBody>
}

// "Road closed: Agiou Georgiou", "Standstill traffic on Laerti", "Construction work".
export function reportTitle(r: Pick<ReportItem, "kind" | "kind_label" | "label" | "street">) {
  if (!r.street) return r.label || r.kind_label
  return r.kind === "closure" ? `${r.kind_label}: ${r.street}` : `${r.label} on ${r.street}`
}

export function countWords(counts: Partial<Record<ReportKind, number>>, kinds: ReportsMeta["kinds"] | undefined) {
  const parts = REPORT_KINDS.filter((k) => counts[k]).map((k) => {
    const n = counts[k] ?? 0
    const w = kinds?.[k]
    return `${n} ${n === 1 ? (w?.one ?? k) : (w?.many ?? `${k}s`)}`
  })
  if (parts.length <= 1) return parts[0] ?? "nothing"
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`
}

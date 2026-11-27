/* The street rules' state on the page: which of the Streets layers are on, what each one read
   from the server (/api/streets/layers/<name>), and how old its inventory is. The layers are
   national inventories, not readings: they are read once when switched on and stay the same
   at every moment of the city clock. */
import { create } from "zustand"

export type StreetLayerId = "limits" | "zones" | "signals" | "crossings" | "cycleways" | "bus_lanes" | "cameras"

export type Inventory = {
  key: string; title: string; count: number | null; island?: number; published?: string | null
  updated?: string | null; copy: string; read_at?: string | null; error?: string | null; publisher?: string; url?: string
}
export type StreetMeta = {
  title: string; count: number; legend: Record<string, string>; status: "ready" | "loading"
  by: Record<string, number>; inventory: Inventory; sources: string[]; network_error?: string | null
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type StreetFC = { type: "FeatureCollection"; features: any[]; meta: StreetMeta }

type One = { on: boolean; status: "idle" | "loading" | "ready" | "error"; error: string | null; meta: StreetMeta | null }
const idle: One = { on: false, status: "idle", error: null, meta: null }

export const STREET_LAYERS: StreetLayerId[] = ["limits", "zones", "signals", "crossings", "cycleways", "bus_lanes", "cameras"]

export const useStreets = create<Record<StreetLayerId, One>>(() =>
  Object.fromEntries(STREET_LAYERS.map((id) => [id, idle])) as Record<StreetLayerId, One>)

export function patchStreet(id: StreetLayerId, p: Partial<One>) {
  useStreets.setState((s) => ({ [id]: { ...s[id], ...p } }) as Partial<Record<StreetLayerId, One>>)
}

export async function fetchStreetLayer(id: StreetLayerId, signal?: AbortSignal): Promise<StreetFC> {
  const res = await fetch(`/api/streets/layers/${id}`, { signal })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  return body as StreetFC
}

// "10 Mar 2025" from "2025-03-10".
export function inventoryDay(iso: string | null | undefined) {
  if (!iso) return null
  const d = new Date(`${iso.slice(0, 10)}T12:00:00Z`)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10)
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
}

// How old an inventory is, in a few words: "inventory of 10 Mar 2025".
export function inventoryAge(inv: Inventory | null | undefined) {
  if (!inv) return null
  const day = inventoryDay(inv.updated) ?? inventoryDay(inv.published)
  const saved = inv.copy === "saved" && inv.error ? ", a saved copy" : ""
  return day ? `inventory of ${day}${saved}` : null
}

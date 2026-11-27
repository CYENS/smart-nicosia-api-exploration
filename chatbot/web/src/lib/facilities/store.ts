/* Where people park, charge and hire, on the page: which of the six inventories are on the map,
   how old each is, and what is live on them (the sensed bays beside each car park, the bikes at
   each Nextbike station). The server keeps one copy of each national inventory
   (chatbot/mobility/facilities.py); this store only holds what the page fetched of it. */
import { create } from "zustand"

export type FacKind = "car_park" | "park_and_ride" | "charger" | "bike_station" | "scooter_zone" | "bike_rack"

export const FAC_KINDS: FacKind[] = ["car_park", "park_and_ride", "charger", "bike_station", "scooter_zone", "bike_rack"]

// The same colours as the server's (chatbot/mobility/facilities.py COLORS): five hues apart, each
// with its own glyph, so colour never carries the kind alone.
export const FAC_COLORS: Record<FacKind, string> = {
  car_park: "#4c8dff", park_and_ride: "#4c8dff", charger: "#34d399", bike_station: "#b48cff",
  scooter_zone: "#f472b6", bike_rack: "#9fb3c8",
}

export const FAC_LABELS: Record<FacKind, string> = {
  car_park: "Car parks", park_and_ride: "Park and ride", charger: "EV chargers",
  bike_station: "Nextbike stations", scooter_zone: "Bolt scooter zones", bike_rack: "Bicycle racks",
}

export const FAC_ONE: Record<FacKind, string> = {
  car_park: "Car park", park_and_ride: "Park and ride", charger: "EV charger",
  bike_station: "Nextbike station", scooter_zone: "Bolt scooter zone", bike_rack: "Bicycle rack",
}

export const FAC_SOURCES: Record<FacKind, string> = {
  car_park: "nap_public_works", park_and_ride: "nap_public_works", charger: "nap_chargers",
  bike_station: "nap_nextbike", scooter_zone: "nap_bolt", bike_rack: "nap_public_works",
}

export type FacMeta = {
  kind: FacKind; count: number; total: number; skipped: number; in_nicosia: number
  published: string | null; updated: string | null; read_at: string | null; error: string | null
  updated_label?: string | null; published_label?: string | null; label: string; plural: string; color: string
  sensed?: number // car parks with the city's bay sensors beside them
  fast_in_nicosia?: number // chargers of 43 kW or more
  listed_out_of_service_in_nicosia?: number
  capacity_in_nicosia?: number // docks, or scooters the zones hold
}

export type Bays = { bays: number; free: number; taken: number; no_reading: number; known: boolean }
export type BikeLive = { bikes: number; ebikes: number; docks: number | null; renting: boolean }

export type KindState = { on: boolean; status: "idle" | "loading" | "ready" | "error"; error: string | null; meta: FacMeta | null }

export type Live = {
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  at: string | null // the clock's moment it was read for (null: now)
  bays: Record<string, Bays>
  baysAsOf: string | null
  baysError: string | null
  bikes: Record<string, BikeLive>
  bikesStatus: "live" | "stale" | "unavailable" | "past" | "idle"
  bikesError: string | null
  bikesAsOf: string | null
}

type Facilities = { kinds: Record<FacKind, KindState>; live: Live }

const idle = (): KindState => ({ on: false, status: "idle", error: null, meta: null })

export const useFacilities = create<Facilities>(() => ({
  kinds: Object.fromEntries(FAC_KINDS.map((k) => [k, idle()])) as Record<FacKind, KindState>,
  live: { status: "idle", error: null, at: null, bays: {}, baysAsOf: null, baysError: null, bikes: {},
          bikesStatus: "idle", bikesError: null, bikesAsOf: null },
}))

export function setKindState(kind: FacKind, patch: Partial<KindState>) {
  useFacilities.setState((s) => ({ kinds: { ...s.kinds, [kind]: { ...s.kinds[kind], ...patch } } }))
}

export function setLive(patch: Partial<Live>) {
  useFacilities.setState((s) => ({ live: { ...s.live, ...patch } }))
}

export function anyOn(kinds: FacKind[] = FAC_KINDS) {
  const k = useFacilities.getState().kinds
  return kinds.some((x) => k[x].on)
}

// "11 Mar 2025": how old an inventory is, said the same way everywhere.
export function inventoryAge(meta: FacMeta | null | undefined): string | null {
  if (!meta) return null
  if (meta.updated_label) return `Inventory last changed ${meta.updated_label}`
  if (meta.published_label) return `Inventory republished ${meta.published_label}`
  return null
}

async function json<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

/* eslint-disable @typescript-eslint/no-explicit-any */
export type FacFC = { type: "FeatureCollection"; features: any[]; meta: FacMeta }

const kindCache = new Map<FacKind, { at: number; body: FacFC }>()

export async function fetchKind(kind: FacKind, signal?: AbortSignal): Promise<FacFC> {
  const hit = kindCache.get(kind)
  if (hit && Date.now() - hit.at < 10 * 60_000) return hit.body
  const body = await json<FacFC>(`/api/facilities/${kind}`, signal)
  kindCache.set(kind, { at: Date.now(), body })
  return body
}

export type LiveBody = {
  at: string | null
  bays?: { car_parks: Record<string, Bays>; as_of: string | null; origin: string; error: string | null }
  bikes?: { status: Live["bikesStatus"]; error?: string | null; as_of?: string; stations: Record<string, BikeLive>; note?: string }
}

export function fetchLive(kinds: FacKind[], at: string | null, signal?: AbortSignal): Promise<LiveBody> {
  const p = new URLSearchParams({ kinds: kinds.join(",") })
  if (at) p.set("at", at)
  return json<LiveBody>(`/api/facilities/live?${p}`, signal)
}

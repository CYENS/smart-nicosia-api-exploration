/* Tonight in Nicosia store: duty pharmacies, Visit Cyprus events, currently open OSM places,
   and walking directions from the user's shared location or Nicosia centre. */
import { create } from "zustand"

import { useClock } from "@/lib/clock"
import { useMapUi } from "@/lib/map/engine"

export type TonightFilter = "all" | "pharmacies" | "events" | "open_now"
export type Precision = "exact" | "street" | "area" | "unplaced"

export type WalkInfo = {
  distance_m: number | null
  distance_km: number | null
  duration_min: number | null
  directions: string[]
  source: string | null
  from_label?: string
  placed?: boolean
  precision?: Precision
  precision_label?: string | null
  landmark_directions?: string | null
}

export type DutyPharmacy = {
  id: string
  duty_date: string
  reg_no: string
  name: string
  pharmacist?: string
  address?: string
  directions?: string
  municipality?: string
  pharmacy_tel?: string
  lat: number | null
  lon: number | null
  precision: Precision
  precision_label?: string
  located_by?: string
  distance_m?: number
  walk_min?: number
  walking?: WalkInfo
}

export type CityEvent = {
  id: string
  title: string
  start_ts: string
  end_ts: string
  all_day?: boolean
  categories?: string
  venue_name?: string
  venue_address?: string
  municipality?: string
  url?: string | null
  description?: string
  lat: number | null
  lon: number | null
  precision: Precision
  precision_label?: string
  located_by?: string
  event_state?: string
  distance_m?: number
  walk_min?: number
}

export type OpenNowPlace = {
  id: string
  name: string
  category: string
  street?: string | null
  opening_hours: string
  status_text: string
  until?: string | null
  always_open?: boolean
  lat: number
  lon: number
  distance_m: number
  walk_min: number
  phone?: string | null
  website?: string | null
}

export type TonightSnapshot = {
  at: string
  duty_date: string
  shift_note: string
  is_past: boolean
  known: boolean
  status: { pharmacies: string; events: string; error: string | null }
  walk_origin: { lat: number; lon: number; label: string; shared: boolean }
  pharmacies: DutyPharmacy[]
  events: CityEvent[]
  upcoming_events: CityEvent[]
  open_now: OpenNowPlace[]
  counts: { pharmacies: number; events: number; upcoming_events: number; open_now: number }
  source_url?: string | null
  fetched_at?: string | null
}

type TonightState = {
  on: boolean
  filter: TonightFilter
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  data: TonightSnapshot | null
  lastKey: string | null
}

export const useTonight = create<TonightState>(() => ({
  on: false,
  filter: "all",
  status: "idle",
  error: null,
  data: null,
  lastKey: null,
}))

let ctrl: AbortController | null = null
let inFlightKey: string | null = null

export function setTonightFilter(filter: TonightFilter) {
  useTonight.setState({ filter })
}

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export async function loadTonight(opts: { force?: boolean } = {}) {
  const at = useClock.getState().at
  const loc = useMapUi.getState().me.loc
  const key = `${at ?? "now"}:${loc ? `${loc.lat.toFixed(4)},${loc.lon.toFixed(4)}` : "center"}`
  const state = useTonight.getState()

  if (!opts.force) {
    if (state.status === "ready" && state.lastKey === key) return
    if (inFlightKey === key) return
  }

  ctrl?.abort()
  const ac = new AbortController()
  ctrl = ac
  inFlightKey = key
  useTonight.setState({ status: "loading", error: null })

  const qs = new URLSearchParams({ walk: "true" })
  if (at !== null) qs.set("at", String(at))
  if (loc) {
    qs.set("lat", String(loc.lat))
    qs.set("lon", String(loc.lon))
  }

  try {
    const data = await fetchJson<TonightSnapshot>(`/api/tonight?${qs.toString()}`, ac.signal)
    if (ctrl !== ac) return
    inFlightKey = null
    useTonight.setState({ status: "ready", data, error: null, lastKey: key })
  } catch (err) {
    if ((err as Error)?.name === "AbortError" || ctrl !== ac) return
    inFlightKey = null
    useTonight.setState({
      status: "error",
      error: err instanceof Error ? err.message : "Could not read Tonight in Nicosia",
    })
  }
}

export async function fetchWalk(id: string, signal?: AbortSignal): Promise<WalkInfo> {
  const loc = useMapUi.getState().me.loc
  const qs = new URLSearchParams({ id })
  if (loc) {
    qs.set("from_lat", String(loc.lat))
    qs.set("from_lon", String(loc.lon))
  }
  return fetchJson<WalkInfo>(`/api/tonight/walk?${qs.toString()}`, signal)
}

/* State and API fetches for Living Sky (adsb.lol live aircraft + 24 h replay, CelesTrak ISS
   visible passes) and CyNAP ITS infrastructure (PWD CCTV and urban loop detectors + Swarco probe). */
import { create } from "zustand"

export type AircraftItem = {
  id: string
  hex: string
  callsign: string
  flight?: string | null
  registration?: string | null
  type?: string | null
  lat: number
  lon: number
  altitude_ft?: number | null
  altitude_m?: number | null
  on_ground: boolean
  ground_speed_kt?: number | null
  speed_kmh?: number | null
  track_deg?: number | null
  heading?: number | null
  vertical_rate_fpm?: number | null
  squawk?: string | null
  category?: string | null
  distance_nm?: number | null
  distance_km?: number | null
  seen_s?: number | null
  reported: number
}

export type AircraftBody = {
  status: "live" | "recorded" | "stale" | "unavailable"
  error: string | null
  note?: string | null
  known: boolean
  recorded: boolean
  count: number
  fetched_at: number
  as_of?: string
  coverage_note?: string
  aircraft: AircraftItem[]
}

export type ISSPass = {
  start: string
  peak: string
  end: string
  start_ms: number
  peak_ms: number
  end_ms: number
  duration_s: number
  duration_min: number
  max_elevation_deg: number
  start_azimuth_deg: number
  start_dir: string
  peak_azimuth_deg: number
  peak_dir: string
  end_azimuth_deg: number
  end_dir: string
}

export type ISSPosition = {
  at: string
  lat: number
  lon: number
  altitude_km: number
  speed_kms: number
  azimuth_deg: number
  direction: string
  elevation_deg: number
  range_km: number
  above_horizon: boolean
  sunlit: boolean
  visible_now: boolean
}

export type ISSBody = {
  status: "live" | "stale" | "unavailable"
  error: string | null
  tle_epoch: string | null
  position: ISSPosition | null
  next_pass: ISSPass | null
  upcoming: ISSPass[]
  recent: ISSPass[]
}

export type ITSCounts = {
  cctv: number
  detectors: number
  vehicle_detectors: number
  cycle_detectors: number
}

export type SwarcoStatus = {
  has_values: boolean
  site_refs: number
  measured_count: number
  publication_time: string | null
  error: string | null
}

export type ITSBody = {
  type: "FeatureCollection"
  features: GeoJSON.Feature[]
  meta: {
    title: string
    count: number
    counts: ITSCounts
    stale: boolean
    swarco: SwarcoStatus
    legend: Record<string, string>
    sources: string[]
  }
}

export type SkyLayersState = {
  aircraft: {
    on: boolean
    status: "idle" | "loading" | "live" | "recorded" | "stale" | "unavailable"
    error: string | null
    known: boolean
    count: number
    airborne: number
    onGround: number
    coverageNote: string | null
    fetchedAt: number | null
    past: boolean
  }
  its: {
    on: boolean
    status: "idle" | "loading" | "live" | "stale" | "unavailable"
    error: string | null
    counts: ITSCounts | null
    swarco: SwarcoStatus | null
  }
  iss: {
    status: "idle" | "loading" | "live" | "stale" | "unavailable"
    error: string | null
    tleEpoch: string | null
    position: ISSPosition | null
    nextPass: ISSPass | null
    upcoming: ISSPass[]
    recent: ISSPass[]
  }
}

export const useSkyLayers = create<SkyLayersState>(() => ({
  aircraft: {
    on: false,
    status: "idle",
    error: null,
    known: false,
    count: 0,
    airborne: 0,
    onGround: 0,
    coverageNote: null,
    fetchedAt: null,
    past: false,
  },
  its: {
    on: false,
    status: "idle",
    error: null,
    counts: null,
    swarco: null,
  },
  iss: {
    status: "idle",
    error: null,
    tleEpoch: null,
    position: null,
    nextPass: null,
    upcoming: [],
    recent: [],
  },
}))

export function setAircraftState(patch: Partial<SkyLayersState["aircraft"]>) {
  useSkyLayers.setState((s) => ({ aircraft: { ...s.aircraft, ...patch } }))
}

export function setItsState(patch: Partial<SkyLayersState["its"]>) {
  useSkyLayers.setState((s) => ({ its: { ...s.its, ...patch } }))
}

export function setIssState(patch: Partial<SkyLayersState["iss"]>) {
  useSkyLayers.setState((s) => ({ iss: { ...s.iss, ...patch } }))
}

export async function fetchAircraft(at: string | null, signal?: AbortSignal): Promise<AircraftBody> {
  const qs = at ? `?at=${encodeURIComponent(at)}` : ""
  const r = await fetch(`/api/sky/aircraft${qs}`, { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as AircraftBody
}

export async function fetchISS(at: string | null, signal?: AbortSignal): Promise<ISSBody> {
  const qs = at ? `?at=${encodeURIComponent(at)}` : ""
  const r = await fetch(`/api/sky/iss${qs}`, { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as ISSBody
}

export async function fetchITS(signal?: AbortSignal): Promise<ITSBody> {
  const r = await fetch("/api/sky/its", { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as ITSBody
}

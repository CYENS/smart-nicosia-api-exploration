/* Store and server fetchers for Nicosia Urban Fabric layers:
   Summer morning ground surface heat (Landsat), Modelled 2022 population (Kontur),
   15-minute access (standard & step-free), and CyNAP road gradients. */
import type { FeatureCollection } from "geojson"
import { create } from "zustand"

import { inflateZlibUint16Grid } from "@/lib/fabric/colors"

type Status = "idle" | "loading" | "ready" | "error"
export type AccessMode = "standard" | "step_free"
export type SlopeFilter = "all" | "steep"

export type FabricSummary = {
  ready: boolean
  hex_count: number
  population_total_2022: number
  population_note: string
  heat: {
    min_c: number | null
    median_c: number | null
    p90_c?: number | null
    max_c: number | null
    scenes_used: number
    date_from: string | null
    date_to: string | null
    overpass_local: string
    label: string
  }
  access: { mean_15min: number | null; mean_step_free: number | null }
  slopes: {
    total: number
    gentle: number
    moderate_gt5: number
    gt5?: number
    steep_gt8: number
    max_slope_pct: number | null
    health?: { state: string; as_of: number | null; note: string }
  }
  overture_release?: string | null
  infra_counts?: Record<string, number>
  caveats?: Record<string, string>
}

export type HeatGridPayload = {
  known: boolean
  reason?: string
  bbox: [number, number, number, number]
  nx: number
  ny: number
  scale: number
  nodata: number
  grid_b64: string
  min_c: number | null
  median_c: number | null
  p90_c: number | null
  max_c: number | null
  scenes_used: number
  date_from: string | null
  date_to: string | null
  overpass_local: string
  temperature_kind: string
  note: string
}

export type FabricFC = FeatureCollection & {
  meta?: {
    title?: string
    metric?: string
    value_field?: string
    count?: number
    total_points?: number
    status?: string
    note?: string
    summary?: {
      total: number
      gentle: number
      moderate_gt5: number
      gt5?: number
      steep_gt8: number
      max_slope_pct: number | null
    }
    legend?: Record<string, string>
    sources?: string[]
  }
}

type FabricState = {
  summary: { status: Status; error: string | null; data: FabricSummary | null }
  heat: { on: boolean; status: Status; error: string | null; payload: HeatGridPayload | null; cells: Uint16Array | null }
  people: { on: boolean; status: Status; error: string | null; data: FabricFC | null }
  access: {
    on: boolean
    mode: AccessMode
    status: Status
    error: string | null
    data: Partial<Record<AccessMode, FabricFC>>
  }
  slopes: { on: boolean; filter: SlopeFilter; status: Status; error: string | null; data: FabricFC | null }
}

export const useFabric = create<FabricState>(() => ({
  summary: { status: "idle", error: null, data: null },
  heat: { on: false, status: "idle", error: null, payload: null, cells: null },
  people: { on: false, status: "idle", error: null, data: null },
  access: { on: false, mode: "standard", status: "idle", error: null, data: {} },
  slopes: { on: false, filter: "all", status: "idle", error: null, data: null },
}))

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export function patchFabric<K extends keyof FabricState>(key: K, value: Partial<FabricState[K]>) {
  useFabric.setState((s) => ({ [key]: { ...s[key], ...value } }) as Pick<FabricState, K>)
}

let summaryPromise: Promise<void> | null = null
export function loadFabricSummary(force = false) {
  const cur = useFabric.getState().summary
  if (!force && (cur.data || cur.status === "loading") && summaryPromise) return summaryPromise
  patchFabric("summary", { status: "loading", error: null })
  summaryPromise = getJson<FabricSummary>("/api/fabric/summary")
    .then((data) => patchFabric("summary", { status: "ready", data, error: null }))
    .catch((e: Error) => patchFabric("summary", { status: "error", error: e.message }))
  return summaryPromise
}

export async function loadHeatGrid(): Promise<void> {
  const st = useFabric.getState().heat
  if (st.cells && st.payload) {
    patchFabric("heat", { status: "ready", error: null })
    return
  }
  patchFabric("heat", { status: "loading", error: null })
  try {
    const payload = await getJson<HeatGridPayload>("/api/fabric/heat")
    if (!payload.known) {
      patchFabric("heat", { status: "error", error: payload.reason ?? "Heat grid not built yet", payload })
      return
    }
    const cells = await inflateZlibUint16Grid(payload.grid_b64)
    patchFabric("heat", { status: "ready", error: null, payload, cells })
  } catch (e) {
    patchFabric("heat", { status: "error", error: (e as Error).message })
  }
}

export async function loadPeopleHexes(): Promise<void> {
  if (useFabric.getState().people.data) {
    patchFabric("people", { status: "ready", error: null })
    return
  }
  patchFabric("people", { status: "loading", error: null })
  try {
    const data = await getJson<FabricFC>("/api/fabric/hexagons?metric=people")
    patchFabric("people", { status: "ready", error: null, data })
  } catch (e) {
    patchFabric("people", { status: "error", error: (e as Error).message })
  }
}

export async function loadAccessHexes(mode: AccessMode): Promise<void> {
  const cached = useFabric.getState().access.data[mode]
  if (cached) {
    patchFabric("access", { status: "ready", error: null })
    return
  }
  patchFabric("access", { status: "loading", error: null })
  try {
    const metric = mode === "step_free" ? "access_step_free" : "access_15min"
    const data = await getJson<FabricFC>(`/api/fabric/hexagons?metric=${metric}`)
    useFabric.setState((s) => ({
      access: { ...s.access, status: "ready", error: null, data: { ...s.access.data, [mode]: data } },
    }))
  } catch (e) {
    patchFabric("access", { status: "error", error: (e as Error).message })
  }
}

export async function loadGradients(): Promise<void> {
  if (useFabric.getState().slopes.data) {
    patchFabric("slopes", { status: "ready", error: null })
    return
  }
  patchFabric("slopes", { status: "loading", error: null })
  try {
    const data = await getJson<FabricFC>("/api/fabric/gradients")
    patchFabric("slopes", { status: "ready", error: null, data })
  } catch (e) {
    patchFabric("slopes", { status: "error", error: (e as Error).message })
  }
}

/* The city's own device layers, on the map without asking the assistant: parking bays free or
   taken, bins by fill, air stations by band, traffic cameras, and the alarms raised now. Drawn
   as ordinary point layers (lib/map/engine.ts), so a click opens the device in the inspector and
   the city clock restates them at a past moment from the twin's memory. Read again every two
   minutes while on and live. */
import { create } from "zustand"

import { useClock } from "@/lib/clock"
import { removeLayer, showLayer, useMapUi, type FC } from "@/lib/map/engine"

export type SystemLayerId = "parking_sensor" | "waste_bin" | "air_station" | "camera" | "alarms"

const URLS: Record<SystemLayerId, string> = {
  parking_sensor: "/api/devices/parking_sensor",
  waste_bin: "/api/devices/waste_bin",
  air_station: "/api/devices/air_station",
  camera: "/api/devices/camera",
  alarms: "/api/alarms",
}

export type SystemMeta = {
  title: string; count: number; total?: number; left_out_stale?: number
  by_state?: Record<string, number>; legend: Record<string, string>
}
type One = { on: boolean; status: "idle" | "loading" | "ready" | "error"; error: string | null; meta: SystemMeta | null }

const idle: One = { on: false, status: "idle", error: null, meta: null }

export const useSystemLayers = create<Record<SystemLayerId, One>>(() => ({
  parking_sensor: idle, waste_bin: idle, air_station: idle, camera: idle, alarms: idle,
}))

export const refOf = (id: SystemLayerId) => `sys:${id}`
export const isSystemRef = (ref: string) => ref.startsWith("sys:")

function patch(id: SystemLayerId, p: Partial<One>) {
  useSystemLayers.setState((s) => ({ [id]: { ...s[id], ...p } }) as Partial<Record<SystemLayerId, One>>)
}

const timers: Partial<Record<SystemLayerId, number>> = {}

async function load(id: SystemLayerId) {
  if (!useSystemLayers.getState()[id].on) return
  if (!useSystemLayers.getState()[id].meta) patch(id, { status: "loading", error: null })
  try {
    const res = await fetch(URLS[id])
    const body = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
    if (!useSystemLayers.getState()[id].on) return // switched off while it loaded
    const fc = body as FC & { meta: SystemMeta }
    await showLayer({
      ref: refOf(id), url: URLS[id], style: "points", title: fc.meta.title, count: fc.meta.count,
      legend: fc.meta.legend, value_field: null, fit: false, labels: false, sources: ["smart_nicosia"],
    }, fc)
    patch(id, { status: "ready", error: null, meta: fc.meta })
  } catch (e) {
    patch(id, { status: "error", error: (e as Error).message })
  }
}

function schedule(id: SystemLayerId) {
  window.clearTimeout(timers[id])
  timers[id] = window.setTimeout(() => {
    // Live only: at a past moment the map restates the layer from the twin's memory.
    if (!document.hidden && useClock.getState().at === null) void load(id).finally(() => schedule(id))
    else schedule(id)
  }, 120_000)
}

export function setSystemLayer(id: SystemLayerId, on: boolean) {
  if (on === useSystemLayers.getState()[id].on) return
  patch(id, { on })
  if (on) {
    void load(id)
    schedule(id)
  } else {
    window.clearTimeout(timers[id])
    patch(id, { status: "idle", error: null })
    if (useMapUi.getState().layers.some((l) => l.ref === refOf(id))) removeLayer(refOf(id))
  }
}

export function systemLayerOn(id: SystemLayerId) {
  return useSystemLayers.getState()[id].on
}

// A new chat (or the assistant clearing the map) leaves the map empty. These are the reader's own
// switches, as the buses and the street lights are, so they come straight back. One taken off
// while other layers stay (the assistant removing just that one) switches off.
useMapUi.subscribe((s, prev) => {
  if (s.layers === prev.layers) return
  const refs = new Set(s.layers.map((l) => l.ref))
  const gone = prev.layers.filter((l) => !refs.has(l.ref)).map((l) => l.ref)
  for (const id of Object.keys(URLS) as SystemLayerId[]) {
    const one = useSystemLayers.getState()[id]
    if (!one.on || !gone.includes(refOf(id))) continue
    if (s.layers.length === 0) void load(id)
    else {
      window.clearTimeout(timers[id])
      patch(id, { on: false, status: "idle" })
    }
  }
})

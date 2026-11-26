/* What a click on the map opens. Each handler claims the map layers it knows and turns the
   feature under the pointer into something to inspect; the first handler (by priority) that
   answers for the topmost feature wins. A later feature registers its own (buses, road
   segments, parcels). */
import type { MapGeoJSONFeature } from "maplibre-gl"

import { isLocalId, type LocalThing } from "@/lib/entities"

export type InspectTarget =
  | { id: string; at?: [number, number]; fallback?: LocalThing; note?: LocalThing }
  | { local: LocalThing }

export type ClickHandler = {
  id: string
  priority: number // higher first
  layers: (layerId: string) => boolean
  resolve: (f: MapGeoJSONFeature, at: [number, number]) => InspectTarget | null
}

const handlers: ClickHandler[] = []

export function registerClickHandler(h: ClickHandler) {
  const i = handlers.findIndex((x) => x.id === h.id)
  if (i >= 0) handlers.splice(i, 1)
  handlers.push(h)
  handlers.sort((a, b) => b.priority - a.priority)
}

export function clickableLayer(layerId: string) {
  return handlers.some((h) => h.layers(layerId))
}

// Features come topmost first (as MapLibre renders them).
export function resolveClick(features: MapGeoJSONFeature[], at: [number, number]): InspectTarget | null {
  for (const f of features) {
    for (const h of handlers) {
      if (!h.layers(f.layer.id)) continue
      const t = h.resolve(f, at)
      if (t) return t
    }
  }
  return null
}

// -- empty map --------------------------------------------------------------------------------
// What a click with nothing under the pointer does, a long press on touch, and "What's here?"
// in the right-click menu. `how` says which: a plain click may close what is open instead (as
// in Google Maps), a press or the menu always asks about the point. The first handler (by
// priority) that returns true takes it; "What's here?" (lib/here.ts) is the default, and a
// workspace that places things by clicking (a square to measure) registers above it.

export type EmptyHow = "click" | "press" | "menu" | "keyboard"

export type EmptyClickHandler = {
  id: string
  priority: number // higher first
  handle: (at: [number, number], how: EmptyHow) => boolean
}

const emptyHandlers: EmptyClickHandler[] = []

export function registerEmptyClickHandler(h: EmptyClickHandler) {
  const i = emptyHandlers.findIndex((x) => x.id === h.id)
  if (i >= 0) emptyHandlers.splice(i, 1)
  emptyHandlers.push(h)
  emptyHandlers.sort((a, b) => b.priority - a.priority)
}

export function handleEmptyClick(at: [number, number], how: EmptyHow) {
  return emptyHandlers.some((h) => h.handle(at, how))
}

// -- the built-in handlers ------------------------------------------------------------------------

const LOCAL_KIND: [RegExp, string][] = [
  [/^route/, "route"], [/^street$/, "street"], [/^stop\d+/, "stop"], [/^iso/, "area"], [/^origin$/, "place"], [/^you$/, "you"],
]

function centerOf(f: MapGeoJSONFeature, at: [number, number]): [number, number] {
  return f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
}

// What the layer itself says about a feature, for things with no record (and as a fallback).
function local(f: MapGeoJSONFeature, at: [number, number]): LocalThing {
  const p = f.properties ?? {}
  const id = String(p.id ?? "")
  const lines = String(p.popup ?? "").split("\n").filter(Boolean)
  const title = lines.shift() || String(p.label || id || "On the map")
  return {
    title, lines, color: p.color, center: centerOf(f, at), geometry: f.geometry,
    kind: LOCAL_KIND.find(([re]) => re.test(id))?.[1] ?? (f.geometry.type === "Point" ? "place" : "area"),
  }
}

// Everything an answer draws: its layers are named "<ref>::<part>".
registerClickHandler({
  id: "answer-layers",
  priority: 10,
  layers: (l) => l.includes("::") && !/^(city3d|hl|sel)::/.test(l) && !l.endsWith("::street-glow") && !l.endsWith("-pool"),
  resolve: (f, at) => {
    const id = f.properties?.id
    if (id === undefined || id === null || id === "") return { local: local(f, at) }
    const s = String(id)
    if (isLocalId(s)) return { local: local(f, at) }
    // The layer may show another moment (a replay frame) or more (an alarm, a count):
    // the inspector shows what it says beside the live record.
    const thing = local(f, at)
    return { id: s, at: centerOf(f, at), fallback: thing, note: thing.lines.length ? thing : undefined }
  },
})

// The DLS 3D city: every building, by its DLS id.
registerClickHandler({
  id: "city-3d",
  priority: 5,
  layers: (l) => l === "city3d::fill" || l === "city3d::ghost",
  resolve: (f, at) => (f.id !== undefined ? { id: `B${f.id}`, at } : null),
})

// The street lamps that glow after dark.
registerClickHandler({
  id: "night-lamps",
  priority: 8,
  layers: (l) => l === "lamps-bulb",
  resolve: (f, at) => (f.properties?.id ? { id: String(f.properties.id), at: centerOf(f, at) } : null),
})

/* The inspector: which thing is open, its record, its last 24 hours, and what opening one does to
   the map (a selection ring, a flight). Map clicks, search, chips, the URL and the assistant all
   open things through `inspect`. */
import { create } from "zustand"

import {
  EntityError, cachedEntity, fetchEntity, fetchSeries, type Entity, type EntityRef, type LocalThing,
  type Series,
} from "@/lib/entities"
import { flyToThing, onInspect, setSelection } from "@/lib/map/engine"
import type { InspectTarget } from "@/lib/registries/map-clicks"

type Status = "idle" | "loading" | "ready" | "missing" | "error"

type InspectorState = {
  open: boolean
  target: { id: string; at?: [number, number] } | null
  local: LocalThing | null
  note: LocalThing | null // what the clicked answer layer says about it (a replay frame, an alarm)
  entity: Entity | null
  status: Status
  error: string | null
  series: Series | null
  seriesStatus: "idle" | "loading" | "ready" | "error"
  recent: EntityRef[]
  focusAt: number // set when an opener wants the keyboard in the inspector
}

const RECENT_KEY = "twin.recent"

function readRecent(): EntityRef[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]")
    return Array.isArray(v) ? v.filter((r) => typeof r?.id === "string").slice(0, 6) : []
  } catch {
    return []
  }
}

export const useInspector = create<InspectorState>(() => ({
  open: false, target: null, local: null, note: null, entity: null, status: "idle", error: null,
  series: null, seriesStatus: "idle", recent: readRecent(), focusAt: 0,
}))
const set = useInspector.setState

function remember(e: Entity) {
  const recent = [{ id: e.id, kind: e.kind, name: e.name }, ...useInspector.getState().recent.filter((r) => r.id !== e.id)].slice(0, 6)
  set({ recent })
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(recent)) } catch { /* private mode: recent stays for this visit */ }
}

function selectEntity(e: Entity) {
  if (!e.center) { setSelection(null); return }
  setSelection({ center: e.center, geometry: e.geometry, footprints: e.footprints })
}

let ctrl: AbortController | null = null

// fly: move the map to it; focus: put the keyboard in the inspector (search, chips, links);
// opener: where the keyboard goes back to when the inspector closes (by default, whatever had
// focus when it opened; pass it when that element is about to go, as a search result does).
export type InspectOptions = { fly?: boolean; focus?: boolean; opener?: HTMLElement | null }

// The element that opened the inspector, for the keyboard to go back to on close. Opening one
// thing from inside the inspector (a relation row, which is about to be replaced) keeps the
// opener of the first, so closing the second still goes back to where the user started.
let opener: HTMLElement | null = null

function rememberOpener(given: HTMLElement | null | undefined) {
  if (given !== undefined) {
    opener = given
    return
  }
  const a = document.activeElement as HTMLElement | null
  if (a && a !== document.body && !a.closest("[data-inspector]") && !a.closest("[role=dialog]")) opener = a
  else if (!useInspector.getState().open) opener = null
}

function canFocus(el: HTMLElement | null): el is HTMLElement {
  return !!el && el.isConnected && !el.closest("[data-inspector]") && el.getClientRects().length > 0
}

// The keyboard back to the opener, or to the map (arrow keys pan it) when the opener is gone (it
// was the map, or a row that has since been replaced).
function returnFocus(to: HTMLElement | null) {
  const target = canFocus(to) ? to : document.querySelector<HTMLElement>(".maplibregl-canvas")
  target?.focus({ preventScroll: true })
}

export function inspect(target: InspectTarget, opts: InspectOptions = {}) {
  ctrl?.abort()
  const mine = (ctrl = new AbortController())
  rememberOpener(opts.opener)
  if (opts.focus) set({ focusAt: Date.now() })
  if ("local" in target) {
    showLocal(target.local, opts)
    return
  }
  const { id, at, fallback, note } = target
  const cached = cachedEntity(id)
  set({
    open: true, target: { id, at }, local: null, note: note ?? null, entity: cached, status: cached ? "ready" : "loading",
    error: null, series: null, seriesStatus: "idle",
  })
  if (cached) selectEntity(cached)
  else if (at) setSelection({ center: at })
  if (opts.fly && (cached?.center ?? at)) flyToThing((cached?.center ?? at)!, cached?.kind)
  fetchEntity(id, at, mine.signal)
    .then((entity) => {
      if (mine.signal.aborted) return
      set({ entity, status: "ready", target: { id: entity.id, at } })
      selectEntity(entity)
      remember(entity)
      if (opts.fly && !cached && !at && entity.center) flyToThing(entity.center, entity.kind)
      if (entity.has_series) loadSeries(entity, at, mine)
    })
    .catch((e: Error) => {
      if (mine.signal.aborted || e.name === "AbortError") return
      if (e instanceof EntityError && e.status === 404 && fallback) { showLocal(fallback, {}); return }
      set({ status: e instanceof EntityError && e.status === 404 ? "missing" : "error", error: e.message })
    })
}

function loadSeries(entity: Entity, at: [number, number] | undefined, mine: AbortController) {
  set({ seriesStatus: "loading" })
  fetchSeries(entity.id, at, mine.signal)
    .then((series) => { if (!mine.signal.aborted) set({ series, seriesStatus: series?.error ? "error" : "ready" }) })
    .catch((e: Error) => { if (!mine.signal.aborted && e.name !== "AbortError") set({ seriesStatus: "error" }) })
}

function showLocal(thing: LocalThing, opts: InspectOptions) {
  set({ open: true, target: null, local: thing, note: null, entity: null, status: "ready", error: null, series: null, seriesStatus: "idle" })
  setSelection({ center: thing.center, geometry: thing.geometry })
  if (opts.fly) flyToThing(thing.center)
}

export function inspectId(id: string, opts: InspectOptions & { at?: [number, number] } = {}) {
  inspect({ id, at: opts.at }, opts)
}

export function retryInspector() {
  const { target } = useInspector.getState()
  if (target) inspect(target, {})
}

// Closing hands the keyboard back to the opener when it was in the inspector (Esc, the close
// button); a close from elsewhere (a new chat, a link) leaves the keyboard where it is.
export function closeInspector(opts: { returnFocus?: boolean } = {}) {
  const a = document.activeElement
  const inside = !!a && (a === document.body || !!a.closest("[data-inspector]"))
  const back = opener
  opener = null
  ctrl?.abort()
  ctrl = null
  set({ open: false, target: null, local: null, note: null, entity: null, status: "idle", error: null, series: null, seriesStatus: "idle" })
  setSelection(null)
  if (opts.returnFocus !== false && inside) returnFocus(back)
}

// What the assistant hears about the open thing with each message.
export function inspectingRef(): EntityRef | null {
  const { open, entity } = useInspector.getState()
  return open && entity ? { id: entity.id, kind: entity.kind, name: entity.name } : null
}

// A click on the map opens what is under it.
onInspect((t) => inspect(t))

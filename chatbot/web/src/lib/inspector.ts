/* The inspector: which thing is open, its record, its last 24 hours, and what opening one does to
   the map (a selection ring, a flight). Map clicks, search, chips, the URL and the assistant all
   open things through `inspect`. */
import { create } from "zustand"

import { toWall, useClock } from "@/lib/clock"
import {
  EntityError, cachedEntity, fetchBand, fetchEntity, fetchSeries, kindOfId, placeholderEntity, type Band, type Entity,
  type EntityRef, type LocalThing, type Series,
} from "@/lib/entities"
import { flyToThing, onInspect, setSelection } from "@/lib/map/engine"
import { routeFor } from "@/lib/registries/inspect-routes"
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
  band: Band | null // the usual behind the sparkline
  moment: string | null // the clock's moment the record is read at (null: now)
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
  series: null, seriesStatus: "idle", band: null, moment: null, recent: readRecent(), focusAt: 0,
}))
const set = useInspector.setState

function remember(e: Entity) {
  // Points of the map ("What's here?") come and go with every click: only the latest is kept.
  const older = useInspector.getState().recent.filter((r) => r.id !== e.id && !(e.kind === "spot" && r.kind === "spot"))
  const recent = [{ id: e.id, kind: e.kind, name: e.name }, ...older].slice(0, 6)
  set({ recent })
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(recent)) } catch { /* private mode: recent stays for this visit */ }
}

function selectEntity(e: Entity) {
  if (!e.center) { setSelection(null); return }
  setSelection({ center: e.center, geometry: e.geometry, footprints: e.footprints, pin: e.kind === "spot" })
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
  // Some things open in a place of their own (an incident in the incident inbox).
  const route = "id" in target ? routeFor(target.id) : null
  if (route && "id" in target) {
    if (useInspector.getState().open) closeInspector({ returnFocus: false })
    route.open(target.id, opts)
    return
  }
  ctrl?.abort()
  const mine = (ctrl = new AbortController())
  rememberOpener(opts.opener)
  if (opts.focus) set({ focusAt: Date.now() })
  if ("local" in target) {
    showLocal(target.local, opts)
    return
  }
  const { id, at, fallback, note } = target
  const moment = clockMoment()
  // A kind whose id says enough (a spot) shows at once, and fills in when its record comes.
  const cached = cachedEntity(id, moment) ?? placeholderEntity(id)
  set({
    open: true, target: { id, at }, local: null, note: note ?? null, entity: cached, status: cached ? "ready" : "loading",
    error: null, series: null, seriesStatus: "idle", band: null, moment,
  })
  if (cached) selectEntity(cached)
  else if (at) setSelection({ center: at })
  if (opts.fly && (cached?.center ?? at)) flyToThing((cached?.center ?? at)!, cached?.kind ?? kindOfId(id))
  fetchEntity(id, at, mine.signal, moment)
    .then((entity) => {
      if (mine.signal.aborted) return
      set({ entity, status: "ready", target: { id: entity.id, at } })
      selectEntity(entity)
      remember(entity)
      // The clock moved while it loaded: read it again at the clock's moment.
      if (clockMoment() !== moment) followClock()
      if (opts.fly && !cached && !at && entity.center) flyToThing(entity.center, entity.kind)
      if (entity.has_series) loadSeries(entity, at, mine)
    })
    .catch((e: Error) => {
      if (mine.signal.aborted || e.name === "AbortError") return
      if (e instanceof EntityError && e.status === 404 && fallback) { showLocal(fallback, {}); return }
      const pending = useInspector.getState().entity?.pending // a placeholder isn't a record to keep showing
      set({ status: e instanceof EntityError && e.status === 404 ? "missing" : "error", error: e.message, ...(pending ? { entity: null } : {}) })
    })
}

function loadSeries(entity: Entity, at: [number, number] | undefined, mine: AbortController, keep = false) {
  const moment = useInspector.getState().moment
  set(keep ? {} : { seriesStatus: "loading", band: null })
  fetchSeries(entity.id, at, mine.signal, seriesMoment(moment))
    .then((series) => {
      if (mine.signal.aborted) return
      set({ series, seriesStatus: series?.error ? "error" : "ready" })
      // The usual behind it, from the twin's memory; the sparkline draws without it meanwhile.
      if (series && !series.error && series.points?.length) {
        fetchBand(entity, series, mine.signal)
          .then((band) => { if (!mine.signal.aborted) set({ band }) })
          .catch(() => { if (!mine.signal.aborted) set({ band: null }) })
      }
    })
    .catch((e: Error) => { if (!mine.signal.aborted && e.name !== "AbortError") set({ seriesStatus: "error" }) })
}

// The clock's moment as the API reads it, or null while it is live.
function clockMoment() {
  const at = useClock.getState().at
  return at === null ? null : toWall(at)
}

// The 24 hours around a past moment end 12 hours after its hour (as the server works it out),
// so moving the clock within the hour keeps the sparkline and only moves its cursor.
function seriesMoment(moment: string | null) {
  return moment ? `${moment.slice(0, 13)}:00` : null
}

// The open thing follows the clock: its record at the new moment, and its 24 hours when the
// moment has moved to another hour.
let followTimer: number | undefined
function followClock() {
  clearTimeout(followTimer)
  followTimer = window.setTimeout(() => {
    const s = useInspector.getState()
    const moment = clockMoment()
    if (!s.open || !s.entity || s.local || moment === s.moment) return
    const sameHour = seriesMoment(moment) === seriesMoment(s.moment)
    ctrl?.abort()
    const mine = (ctrl = new AbortController())
    const entity = s.entity
    const at = s.target?.at
    set({ moment })
    fetchEntity(entity.id, at, mine.signal, moment)
      .then((e) => { if (!mine.signal.aborted) set({ entity: e }) })
      .catch(() => { /* the record stays as it was; the next move tries again */ })
    if (entity.has_series && !sameHour) loadSeries(entity, at, mine, true)
  }, 300)
}
useClock.subscribe((s, prev) => { if (s.at !== prev.at) followClock() })

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
  set({ open: false, target: null, local: null, note: null, entity: null, status: "idle", error: null, series: null, seriesStatus: "idle", band: null })
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

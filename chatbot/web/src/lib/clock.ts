/* The city clock: one moment for the whole twin. Live by default; moved back, the night lamps,
   the layers on the map, the strip, the sky, the inspector and every chart's time line show that
   moment from the twin's memory. A replay (a chat time-lapse, or a range from set_city_time) is a
   range on the clock with play, pause and speed. Compare puts a second moment beside the first,
   as a swipe across the map.

   Moments are instants (epoch ms). The API and links use Nicosia wall time ("2026-11-21T21:00");
   charts read wall time as browser-local ms (see wallMs). */
import { create } from "zustand"

import {
  clockKinds, onClock, putLamps, putStates, setClockView, setSwipeAt, type FC, type StatesAt,
} from "@/lib/map/engine"
import { nicosiaWallToDate, skyAt } from "@/lib/map/sky"

export const STEP_MS = 15 * 60e3 // the clock's moments are quarter hours
const HOUR = 3600e3
const DAY = 24 * HOUR
export const MAX_BACK_MS = 62 * DAY

export type Span = "6h" | "day" | "week" | "month"
export const SPANS: { id: Span; label: string; ms: number; play: number }[] = [
  { id: "6h", label: "6 h", ms: 6 * HOUR, play: STEP_MS },
  { id: "day", label: "Day", ms: DAY, play: STEP_MS },
  { id: "week", label: "Week", ms: 7 * DAY, play: HOUR },
  { id: "month", label: "Month", ms: 31 * DAY, play: 3 * HOUR },
]
export const SPEEDS = [1, 2, 4, 8]

export type Range = {
  start: number; end: number; step: number; title: string
  ref?: string // the map layer whose frames these are
  times?: number[] // that layer's frame moments
  live?: boolean // the range runs up to now
}
export type Compare = { offset: number } | { at: number }
export type Pin = { at: number; label: string }
export type Readout = { unknown?: boolean; reason?: string | null; coverage_pct?: number }
export type Strip = {
  at?: string; label?: string; live?: boolean
  lights?: Readout & { on?: number; total?: number; as_of?: string | null }
  parking?: Readout & { free?: number; total?: number }
  bins?: Readout & { full?: number; total?: number }
  air?: Readout & { caqi?: number; band?: string; stations?: number }
  weather?: { temp_c: number; sky: string; daylight: boolean }
  registers?: { buildings: number }
  // Live mobility (now only: the strip leaves them out at a past moment).
  buses?: { running: number; lines?: number; late?: number }
  slowest_road?: { id: string; name: string; segment: string; speed_kmh: number; free_flow_kmh: number | null; state: string }
}
export type Track = {
  status: string; message?: string
  start: number // the first hour of the coverage arrays
  coverage: number[] // per hour, share of every device the memory covers (0 to 100)
  kinds: Record<string, number[]>
  events: { t: number; full_bins: number; air: number; faults: number }[]
  earliest: number | null
}

type ClockState = {
  now: number
  at: number | null // null: live
  range: Range | null
  playing: boolean
  speed: number
  compare: Compare | null
  span: Span
  windowEnd: number // the track shows [windowEnd - span, windowEnd]
  track: Track | null
  pins: Pin[]
  strip: Strip | null // the strip at the clock's moment (live or past)
  compareStrip: Strip | null
  loading: boolean // the moment's data is on its way
  error: string | null
  expanded: boolean // the console shown, or folded into the now line
  swipe: number // where the compare swipe divides the open map, 0 to 1
}

// Folded into the now line until someone opens it (then remembered on this device). A new key:
// the old one remembered the console open, as it used to start.
const EXPANDED_KEY = "twin.clock.shown"
function readExpanded() {
  try {
    const v = localStorage.getItem(EXPANDED_KEY)
    if (v !== null) return v === "1"
  } catch { /* private mode */ }
  return false
}

export const useClock = create<ClockState>(() => ({
  now: Date.now(), at: null, range: null, playing: false, speed: 1, compare: null, span: "day",
  windowEnd: Date.now(), track: null, pins: [], strip: null, compareStrip: null, loading: false,
  error: null, expanded: readExpanded(), swipe: 0.5,
}))
const set = useClock.setState
const get = useClock.getState

// -- time ---------------------------------------------------------------------------------------

export const snap = (t: number) => Math.floor(t / STEP_MS) * STEP_MS

// Nicosia wall time, as the API and links write it: "2026-11-21T21:00".
export function toWall(t: number) {
  return new Date(t).toLocaleString("sv-SE", { timeZone: "Asia/Nicosia", hour12: false }).slice(0, 16).replace(" ", "T")
}

export function fromWall(s: string) {
  return nicosiaWallToDate(s).getTime()
}

// Wall time read as browser-local ms: how Vega reads the charts' times, and how the charts'
// `_t` line is set.
export function wallMs(t: number) {
  return Date.parse(toWall(t))
}

export function fmtMoment(t: number, opts: { year?: boolean; weekday?: boolean } = {}) {
  const d = new Date(t)
  const day = d.toLocaleDateString("en-GB", {
    timeZone: "Asia/Nicosia", weekday: opts.weekday === false ? undefined : "short", day: "numeric", month: "short",
    year: opts.year ? "numeric" : undefined,
  })
  const time = d.toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })
  return `${day}, ${time}`
}

export function fmtTime(t: number) {
  return new Date(t).toLocaleTimeString("en-GB", { timeZone: "Asia/Nicosia", hour: "2-digit", minute: "2-digit" })
}

export function ago(t: number, now: number) {
  const min = Math.round((now - t) / 60e3)
  if (min < 60) return `${min} min ago`
  const h = Math.round(min / 60)
  if (h < 36) return `${h} h ago`
  const d = Math.round(h / 24)
  return d === 7 ? "a week ago" : `${d} days ago`
}

// "last week", "yesterday" or the compared moment itself.
export function compareName(c: Compare | null, other: number | null) {
  if (!c || other === null) return null
  if ("offset" in c) return c.offset === -7 * DAY ? "last week" : c.offset === -DAY ? "yesterday" : fmtMoment(other)
  return fmtMoment(other)
}

export function spanMs(span: Span) {
  return SPANS.find((s) => s.id === span)!.ms
}

// The oldest moment the clock goes to: the memory's oldest record, and never past 62 days.
export function earliest(s = get()) {
  const floor = s.now - MAX_BACK_MS
  return snap(Math.max(floor, s.track?.earliest ?? floor))
}

export function compareAt(s = get()): number | null {
  if (!s.compare) return null
  const base = s.at ?? s.now
  return "offset" in s.compare ? snap(base + s.compare.offset) : s.compare.at
}

export function moment(s = get()) {
  return s.at ?? s.now
}

// -- moving the clock ---------------------------------------------------------------------------

function clampWindow(end: number, span: Span, now: number) {
  const ms = spanMs(span)
  return Math.min(now, Math.max(end, now - MAX_BACK_MS + ms))
}

// Keep `t` inside the track's window, moving the window as little as possible (with a tenth of
// it to spare at the edge it moved towards).
function follow(t: number, s = get()) {
  const ms = spanMs(s.span)
  let end = s.windowEnd
  if (t > end - ms * 0.02) end = t + ms * 0.1
  else if (t < end - ms * 0.98) end = t + ms * 0.9
  return clampWindow(end, s.span, s.now)
}

export function setAt(t: number | null, opts: { keepPlaying?: boolean } = {}) {
  const s = get()
  if (t !== null && t >= s.now - 60e3) t = null
  if (t !== null) t = Math.max(earliest(s), snap(t))
  const playing = opts.keepPlaying ? s.playing : false
  if (playing !== s.playing) stopLoop()
  set({ at: t, playing, windowEnd: t === null ? s.now : follow(t, s) })
}

export function goLive() {
  stopLoop()
  set((s) => ({ at: null, playing: false, windowEnd: s.now }))
}

export function stepBy(ms: number) {
  const s = get()
  const from = s.at ?? s.now
  setAt(from + ms)
}

export function setSpan(span: Span) {
  const s = get()
  const t = s.at ?? s.now
  const ms = spanMs(span)
  set({ span, windowEnd: clampWindow(t === s.now ? s.now : t + ms / 2, span, s.now) })
}

export function panWindow(direction: -1 | 1) {
  const s = get()
  const ms = spanMs(s.span)
  set({ windowEnd: clampWindow(s.windowEnd + direction * ms * 0.75, s.span, s.now) })
}

// Put the keyboard on a part of the city clock (marked data-clock-part) once the page has
// redrawn: the track's slider, the toggle that shows or hides the track, play or compare.
export function focusClock(part: "track" | "toggle" | "play" | "compare") {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = document.querySelector<HTMLElement>(`[data-clock-part="${part}"]`)
    const target = el?.querySelector<HTMLElement>("input") ?? el
    target?.focus({ preventScroll: true })
  }))
}

export function setExpanded(expanded: boolean) {
  set({ expanded })
  try { localStorage.setItem(EXPANDED_KEY, expanded ? "1" : "0") } catch { /* private mode */ }
}

export function setSwipe(x: number) {
  set({ swipe: Math.max(0.04, Math.min(0.96, x)) })
}

export function setCompare(c: Compare | null) {
  set({ compare: c, compareStrip: null })
}

export function addPin(at: number, label: string) {
  set((s) => ({ pins: [...s.pins.filter((p) => Math.abs(p.at - at) > STEP_MS / 2), { at, label }].slice(-12) }))
}

// -- ranges and playback ------------------------------------------------------------------------

export function setRange(r: Range | null, opts: { play?: boolean; at?: number } = {}) {
  stopLoop()
  if (!r) { set({ range: null, playing: false }); return }
  const s = get()
  const ms = spanMs(s.span)
  const len = r.end - r.start
  // Show the whole range: the smallest span that holds it, ending a little after it.
  const span = len <= ms ? s.span : (SPANS.find((x) => x.ms >= len * 1.1) ?? SPANS[SPANS.length - 1]).id
  set({ range: r, span, windowEnd: clampWindow(r.end + spanMs(span) * 0.04, span, s.now) })
  setAt(opts.at ?? r.start)
  if (opts.play) play()
}

export function clearRange() {
  setRange(null)
}

// The moments a play steps through: a layer's own frames, else every `step` across the range
// (or, with no range, forward from the moment to now at the span's pace).
function nextMoment(s = get()): number | null {
  const t = s.at ?? s.now
  const r = s.range
  if (r?.times?.length) {
    const next = r.times.find((x) => x > t + 1)
    return next ?? null
  }
  const step = r?.step ?? SPANS.find((x) => x.id === s.span)!.play
  const next = snap(t + step)
  const end = r ? (r.live ? s.now : r.end) : s.now
  return next > end + 1 ? null : next
}

let loop: number | undefined
function stopLoop() {
  clearTimeout(loop)
  loop = undefined
}

export function play() {
  const s = get()
  // At the end (or live), start over from the range's start, or a span back.
  const atEnd = s.at === null || nextMoment(s) === null
  if (atEnd) {
    if (s.range) setAt(s.range.start)
    else if (s.at === null) setAt(s.now - spanMs(s.span) * 0.5)
    else return
  }
  set({ playing: true })
  loop = window.setTimeout(tick, FRAME_MS / get().speed)
}

export function pause() {
  stopLoop()
  set({ playing: false })
}

export function togglePlay() {
  if (get().playing) pause()
  else play()
}

export function cycleSpeed() {
  set((s) => ({ speed: SPEEDS[(SPEEDS.indexOf(s.speed) + 1) % SPEEDS.length] }))
}

const FRAME_MS = 650
async function tick() {
  const s = get()
  if (!s.playing) return
  const next = nextMoment(s)
  if (next === null) {
    if (!s.range || s.range.live) goLive() // played up to now: the clock is live again
    else pause()
    return
  }
  const t0 = performance.now()
  setAt(next, { keepPlaying: true })
  // A moment read from the server is on the map before the next one is asked for.
  await settled()
  if (!get().playing) return
  loop = window.setTimeout(tick, Math.max(0, FRAME_MS / get().speed - (performance.now() - t0)))
}

// -- what the twin looks like at the moment -----------------------------------------------------

let ctrl: AbortController | null = null
let pending: Promise<void> = Promise.resolve()
let timer: number | undefined

function settled() {
  return Promise.race([pending, new Promise<void>((r) => setTimeout(r, 4000))])
}

async function json<T>(url: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

const stripCache = new Map<number, Strip>()
const lampsCache = new Map<number, FC>()
const statesCache = new Map<string, StatesAt>()

function remember<K, V>(m: Map<K, V>, k: K, v: V) {
  m.set(k, v)
  if (m.size > 160) m.delete(m.keys().next().value as K)
}

// Everything the map and the strip need for one moment, from the caches or the server: the
// strip, the lamps (when it was dark) and the devices of every kind a layer on the map shows.
async function loadMoment(t: number, signal: AbortSignal) {
  const q = `at=${encodeURIComponent(toWall(t))}`
  const jobs: Promise<unknown>[] = []
  if (!stripCache.has(t)) jobs.push(json<Strip>(`/api/overview?${q}`, signal).then((s) => remember(stripCache, t, s)))
  const dark = skyAt(new Date(t)).dark
  if (dark && !lampsCache.has(t)) jobs.push(json<FC>(`/api/lamps?${q}`, signal).then((fc) => remember(lampsCache, t, fc)))
  const kinds = clockKinds(t)
  for (const kind of kinds) {
    const key = `${kind}@${t}`
    if (!statesCache.has(key)) jobs.push(json<StatesAt>(`/api/clock/states?kind=${kind}&${q}`, signal).then((st) => remember(statesCache, key, st)))
  }
  await Promise.all(jobs)
  if (signal.aborted) return
  if (lampsCache.has(t)) putLamps(t, lampsCache.get(t)!)
  for (const kind of kinds) {
    const st = statesCache.get(`${kind}@${t}`)
    if (st) putStates(t, kind, st)
  }
  return stripCache.get(t)
}

function load(immediate: boolean) {
  clearTimeout(timer)
  const run = () => {
    ctrl?.abort()
    const mine = (ctrl = new AbortController())
    const s = get()
    const at = s.at
    const other = compareAt(s)
    const jobs: Promise<unknown>[] = []
    if (at !== null) {
      jobs.push(loadMoment(at, mine.signal).then((strip) => { if (!mine.signal.aborted && strip) set({ strip }) }))
    } else {
      jobs.push(loadLive(mine.signal))
    }
    if (other !== null) {
      jobs.push(loadMoment(other, mine.signal).then((strip) => { if (!mine.signal.aborted && strip) set({ compareStrip: strip }) }))
    }
    set({ loading: true, error: null })
    pending = Promise.all(jobs).then(
      () => { if (!mine.signal.aborted) set({ loading: false }) },
      (e: Error) => { if (!mine.signal.aborted && e.name !== "AbortError") set({ loading: false, error: e.message }) },
    )
  }
  // Scrubbing asks for the moment it stops on; playing and keys ask at once.
  if (immediate) run()
  else timer = window.setTimeout(run, 140)
}

let liveAt = 0
async function loadLive(signal: AbortSignal) {
  if (get().strip?.live && Date.now() - liveAt < 55e3) return
  const strip = await json<Strip>("/api/overview", signal)
  liveAt = Date.now()
  if (get().at === null) set({ strip })
}

export async function loadTrack() {
  try {
    const t = await json<any>("/api/clock?days=31", new AbortController().signal) // eslint-disable-line @typescript-eslint/no-explicit-any
    const start = fromWall(t.coverage.start)
    set({
      track: {
        status: t.status, message: t.message, start, coverage: t.coverage.all, kinds: t.coverage.kinds,
        events: (t.events as { t: string; full_bins: number; air: number; faults: number }[]).map((e) => ({ ...e, t: fromWall(e.t) })),
        earliest: t.earliest ? fromWall(t.earliest) : null,
      },
    })
  } catch {
    set((s) => ({ track: s.track ?? { status: "error", start: s.now - 31 * DAY, coverage: [], kinds: {}, events: [], earliest: null } }))
  }
}

// -- wiring -------------------------------------------------------------------------------------

let started = false
export function startClock() {
  if (started) return
  started = true
  loadTrack()
  window.setInterval(() => {
    const s = get()
    const now = Date.now()
    // The live window follows now; a range that runs to now grows with it.
    set({
      now,
      windowEnd: s.windowEnd >= s.now - 60e3 ? now : s.windowEnd,
      range: s.range?.live ? { ...s.range, end: now } : s.range,
    })
    if (s.at === null) load(true)
  }, 30e3)
  window.setInterval(loadTrack, 5 * 60e3)
  onClock({
    range: rangeFromLayer,
    gone: layerRangeGone,
    reload: reloadMoment,
    covers: (t) => {
      const r = get().range
      return !!r && t >= r.start - r.step / 2 && t <= r.end + r.step / 2
    },
    seek: (t) => { pause(); setAt(t) },
  })
  let last = { at: get().at, compare: compareAt(), swipe: get().swipe }
  useClock.subscribe((s) => {
    if (s.swipe !== last.swipe) { last.swipe = s.swipe; setSwipeAt(s.swipe) }
    const next = { at: s.at, compare: compareAt(s), swipe: s.swipe }
    if (next.at === last.at && next.compare === last.compare) return
    const keyed = s.playing || (last.at === null) !== (next.at === null)
    last = next
    setClockView(next.at, next.compare)
    if (next.compare === null) set({ compareStrip: null })
    load(keyed)
  })
  setSwipeAt(get().swipe)
  setClockView(get().at, compareAt())
  load(true)
}

// Called by the map when what it needs changes (a layer of devices came or went): the moment
// on show is read again for it.
export function reloadMoment() {
  load(true)
}

// A layer with frames (a chat time-lapse) becomes a range on the clock, and plays.
export function rangeFromLayer(ref: string, title: string, iso: string[]) {
  const times = iso.map(fromWall)
  if (times.length < 2) return
  const step = times[1] - times[0]
  const end = times[times.length - 1]
  addPin(times[0], title)
  setRange({ start: times[0], end, step, title, ref, times, live: end >= Date.now() - step * 1.5 }, { play: true })
}

export function layerRangeGone(ref: string) {
  if (get().range?.ref === ref) {
    clearRange()
    goLive()
  }
}

// -- commands from the assistant (set_city_time) ------------------------------------------------

type ClockCommand = {
  live?: boolean; at?: string | null; play?: boolean; speed?: number
  range?: { from: string; to: string | null; step_min: number; label: string } | null
  compare?: string | null; compare_offset_min?: number | null
}

export function applyClockCommand(ev: ClockCommand) {
  if (ev.speed && SPEEDS.includes(ev.speed)) set({ speed: ev.speed })
  if (ev.compare === "off") setCompare(null)
  else if (ev.compare_offset_min) setCompare({ offset: ev.compare_offset_min * 60e3 })
  else if (ev.compare) setCompare({ at: snap(fromWall(ev.compare)) })
  if (ev.live) { clearRange(); goLive(); return }
  if (ev.range) {
    const start = fromWall(ev.range.from)
    const end = ev.range.to ? fromWall(ev.range.to) : Date.now()
    const at = ev.at ? fromWall(ev.at) : start
    addPin(start, ev.range.label)
    setRange({ start, end, step: ev.range.step_min * 60e3, title: ev.range.label, live: !ev.range.to }, { play: ev.play, at })
  } else if (ev.at) {
    const t = fromWall(ev.at)
    addPin(t, fmtMoment(t))
    setAt(t)
  }
  setExpanded(true)
}

// What the assistant hears about the clock with each message.
export function clockContext() {
  const s = get()
  const other = compareAt(s)
  return {
    at: s.at !== null ? toWall(s.at) : null,
    range: s.range ? [toWall(s.range.start), toWall(s.range.end)] : null,
    playing: s.playing,
    compare: other !== null ? toWall(other) : null,
  }
}

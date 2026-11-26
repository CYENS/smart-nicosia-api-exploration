/* Links to a view: the URL's hash holds the camera, the open thing and the city clock, so a view
   can be shared and survives a reload.

     #/@35.17250,33.36150,16.5z            the camera (lat, lon, zoom; then pitch p, bearing b)
     #/e/YL1015                            a thing in the inspector
     #/e/YL1015/@35.16680,33.36082,17z,45p a thing and the camera
     #/t/2026-11-21T21:00                  the clock at a moment (Nicosia time; none: live)
     #/r/2026-11-21T18:00_2026-11-22T02:00 a range on the clock (the moment is its playhead)
     #/vs/-7d  #/vs/2026-11-14T21:00       a comparison: a week (or n days) before, or a moment

   The hash follows the map with replaceState, so the back button isn't filled with every pan. */
import {
  compareAt, fromWall, setAt, setCompare, setRange, toWall, useClock, type Compare,
} from "@/lib/clock"
import { closeInspector, inspect, useInspector } from "@/lib/inspector"
import { getCamera, jumpToCamera, onCameraMoved, type Camera } from "@/lib/map/engine"

export type ClockLink = { at: number | null; range: [number, number] | null; compare: Compare | null }
export type Link = { camera: Camera | null; entity: string | null; clock?: ClockLink | null }

const NUM = "(-?\\d+(?:\\.\\d+)?)"
const CAMERA = new RegExp(`@${NUM},${NUM},${NUM}z(?:,${NUM}p)?(?:,${NUM}b)?`)
const WALL = "(\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d)"
const AT = new RegExp(`(?:^|/)t/${WALL}(?:/|$)`)
const RANGE = new RegExp(`(?:^|/)r/${WALL}_${WALL}(?:/|$)`)
const VS = new RegExp(`(?:^|/)vs/(?:(-\\d{1,2})d|${WALL})(?:/|$)`)
const DAY = 24 * 3600e3

function moment(wall: string | undefined) {
  if (!wall) return null
  const t = fromWall(wall)
  return Number.isFinite(t) && t <= Date.now() ? t : null
}

export function parseHash(hash: string): Link {
  const raw = hash.replace(/^#\/?/, "")
  let h = raw
  try { h = decodeURIComponent(raw) } catch { /* a malformed escape: read it as it is */ }
  let entity: string | null = null
  const e = /^e\/([^@/][^/]*?)(?:\/|$)/.exec(h)
  if (e) entity = e[1].trim() || null
  let camera: Camera | null = null
  const m = CAMERA.exec(h)
  if (m) {
    const [lat, lon, zoom, pitch, bearing] = m.slice(1).map((v) => (v === undefined ? undefined : Number(v)))
    if (lat !== undefined && lon !== undefined && zoom !== undefined
      && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && zoom >= 0 && zoom <= 22) {
      camera = {
        center: [lon, lat], zoom,
        pitch: Math.max(0, Math.min(75, pitch ?? 0)), bearing: ((bearing ?? 0) + 540) % 360 - 180,
      }
    }
  }
  const at = moment(AT.exec(h)?.[1])
  const r = RANGE.exec(h)
  const from = moment(r?.[1]), to = moment(r?.[2])
  const range: [number, number] | null = from !== null && to !== null && to > from ? [from, to] : null
  const v = VS.exec(h)
  let compare: Compare | null = null
  if (v?.[1]) compare = { offset: Math.max(-62, Number(v[1])) * DAY }
  else if (v?.[2] && moment(v[2]) !== null) compare = { at: moment(v[2])! }
  const clock = at !== null || range || compare ? { at, range, compare } : null
  return { camera, entity, clock }
}

const r = (v: number, d: number) => Number(v.toFixed(d)).toString()

export function formatHash({ camera, entity, clock }: Link): string {
  const parts: string[] = []
  if (entity) parts.push(`e/${encodeURIComponent(entity)}`)
  if (clock?.at !== null && clock?.at !== undefined) parts.push(`t/${toWall(clock.at)}`)
  if (clock?.range) parts.push(`r/${toWall(clock.range[0])}_${toWall(clock.range[1])}`)
  if (clock?.compare) {
    const c = clock.compare
    if ("offset" in c && c.offset % DAY === 0) parts.push(`vs/${Math.round(c.offset / DAY)}d`)
    else {
      const t = "offset" in c ? compareAt() : c.at
      if (t !== null) parts.push(`vs/${toWall(t)}`)
    }
  }
  if (camera) {
    const [lon, lat] = camera.center
    let cam = `@${r(lat, 5)},${r(lon, 5)},${r(camera.zoom, 2)}z`
    if (Math.round(camera.pitch) || Math.round(camera.bearing)) cam += `,${Math.round(camera.pitch)}p`
    if (Math.round(camera.bearing)) cam += `,${Math.round(camera.bearing)}b`
    parts.push(cam)
  }
  return parts.length ? `#/${parts.join("/")}` : ""
}

function clockNow(): ClockLink {
  const s = useClock.getState()
  return { at: s.at, range: s.range ? [s.range.start, s.range.end] : null, compare: s.compare }
}

function current(): Link {
  const { open, entity, target } = useInspector.getState()
  return { camera: getCamera(), entity: open ? (entity?.id ?? target?.id ?? null) : null, clock: clockNow() }
}

let written = ""

function write() {
  const hash = formatHash(current())
  if (hash === written) return
  written = hash
  history.replaceState(null, "", `${location.pathname}${location.search}${hash}`)
}

// A link to what is on the screen now.
export function linkToView() {
  write()
  return location.href
}

// A link to one thing, at the camera and the clock's moment now.
export function linkTo(entity: string) {
  return `${location.origin}${location.pathname}${location.search}${formatHash({ camera: getCamera(), entity, clock: clockNow() })}`
}

let timer: number | undefined
function soon() {
  clearTimeout(timer)
  timer = window.setTimeout(write, 250)
}

function stepFor(ms: number) {
  const h = ms / 3600e3
  return (h <= 24 ? 15 : h <= 48 ? 30 : h <= 192 ? 60 : 180) * 60e3
}

// Set the clock as a link says (a shared replay opens paused at its playhead).
function applyClock(c: ClockLink | null | undefined) {
  if (!c) return
  setCompare(c.compare)
  if (c.range) {
    const [start, end] = c.range
    setRange({ start, end, step: stepFor(end - start), title: "A shared replay" }, { at: c.at ?? start })
  } else {
    setAt(c.at)
  }
}

// Call once the map exists: the hash follows the camera, the inspector and the clock, and a
// hash typed or pasted into the address bar is applied.
let started = false
export function startDeepLinks(initial: Link) {
  if (started) return // React's development mode runs effects twice
  started = true
  written = location.hash
  onCameraMoved(soon)
  useInspector.subscribe((s, prev) => {
    if (s.open !== prev.open || s.entity?.id !== prev.entity?.id || s.target?.id !== prev.target?.id) soon()
  })
  useClock.subscribe((s, prev) => {
    if (s.at !== prev.at || s.range !== prev.range || s.compare !== prev.compare) soon()
  })
  window.addEventListener("hashchange", () => {
    if (location.hash === written) return
    const link = parseHash(location.hash)
    written = location.hash
    if (link.camera) jumpToCamera(link.camera)
    applyClock(link.clock ?? { at: null, range: null, compare: null })
    if (link.entity && link.entity !== useInspector.getState().entity?.id) inspect({ id: link.entity }, { fly: !link.camera })
    else if (!link.entity && useInspector.getState().open) closeInspector()
  })
  applyClock(initial.clock)
  if (initial.entity) inspect({ id: initial.entity }, { fly: !initial.camera })
}

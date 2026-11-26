/* The view from a place: the camera stands at a spot, at eye level (or on a roof, or higher), and
   looks out over Nicosia in 3D, by day or at night. The assistant's view_from sends a view; a
   shared link (lib/deeplink.ts) and "View from here" in the inspector ask /api/view for one.

   The view is an eye (where it stands and how high, in metres above sea level as the map draws
   the ground) and where it looks (heading and tilt). MapLibre has no camera of that kind, so the
   map's centre is put far out along the line of sight, in the air, and
   calculateCameraOptionsFromTo works out the rest; every look, the flight in and the slow turn
   around place the camera that way, frame by frame. While viewing, the ground is raised
   (lib/map/terrain.ts), the base map's buildings draw to the horizon, the DLS buildings stand
   near the eye only, and the city clock can hold last night. Back to the map puts every one of
   those back as it was, and the keyboard where it was. */
import maplibregl, { LngLat } from "maplibre-gl"
import { create } from "zustand"

import { fromWall, setAt, useClock } from "@/lib/clock"
import { closeInspector } from "@/lib/inspector"
import { useLayout } from "@/lib/layout"
import {
  getCamera, getMap, jumpToCamera, onViewTakeover, setCity3d, setViewFocus, useMapUi, viewCamera,
  type Camera,
} from "@/lib/map/engine"
import { EXAGGERATION, TERRAIN_SOURCE, groundAt, setTerrain } from "@/lib/map/terrain"
import { WALLS_RING } from "@/lib/walls"

export type Landmark = {
  title: string; kind?: string; lat: number; lon: number; top_m?: number; visible: boolean; distance_m: number
  hidden_by?: string
}
// One place to look from, as the tool and /api/view give it (heights in real metres above sea
// level; the map draws the ground EXAGGERATION times higher).
export type ViewStop = {
  id: string; name: string; lat: number; lon: number; ground_m: number; eye_m: number; height_m: number
  above_centre_m: number; bearing: number; rank?: number; building_id?: string
}

const buildingNumber = (id?: string) => (id && /^B\d+$/.test(id) ? Number(id.slice(1)) : undefined)
export type ViewSpec = ViewStop & {
  landmarks?: Landmark[]; look_around?: boolean; building_id?: string
  toward?: { name: string; lat: number; lon: number; ground_m: number; in_sight: boolean }
  at?: string | null; dark?: boolean; when?: string; tour?: ViewStop[]
  pitch?: number // from a link
}

type ViewState = {
  open: boolean
  spec: ViewSpec | null
  yaw: number // where the eye looks, degrees from north
  pitch: number // MapLibre's pitch: 90 looks at the horizon
  arriving: boolean // flying in
  turning: boolean // looking all the way round
  tour: ViewStop[] // Next view goes through these (the places of the same answer)
  index: number
  loading: boolean // a view on its way from /api/view
  error: string | null
  facts: string[] // what /api/view said about it (for a link or "View from here")
}

export const useView = create<ViewState>(() => ({
  open: false, spec: null, yaw: 0, pitch: 84, arriving: false, turning: false, tour: [], index: 0,
  loading: false, error: null, facts: [],
}))
const set = useView.setState
const get = useView.getState

// Level with the horizon at most. (At 85 the horizon sat a quarter of the way up the frame and
// most of it was the ground at your feet; a person looking out looks about level.)
export const VIEW_MAX_PITCH = 89
export const VIEW_MIN_PITCH = 50
const EYE_PITCH = 87 // looking out over a city on the horizon: a little under level
const FOV = 50 // degrees, top to bottom: closer to what an eye takes in than the map's 37°
const LOOK_STEP = 15
const PITCH_STEP = 4
const AROUND_MS = 60_000 // a slow turn all the way round
const DLS_RADIUS_M = 700 // the DLS buildings drawn around the eye
const LABEL_SIDE = 70 // degrees either side of the heading a label stays shown
const RAD = Math.PI / 180

const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches
const wrap = (d: number) => ((d % 360) + 360) % 360
const turn = (from: number, to: number) => ((to - from + 540) % 360) - 180
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

function destination(lon: number, lat: number, bearing: number, m: number): [number, number] {
  const d = m / 6371008.8, b = bearing * RAD, p1 = lat * RAD, l1 = lon * RAD
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b))
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2))
  return [l2 / RAD, p2 / RAD]
}

function metres(lon1: number, lat1: number, lon2: number, lat2: number) {
  const k = Math.cos(((lat1 + lat2) / 2) * RAD)
  return Math.hypot((lon2 - lon1) * k, lat2 - lat1) * 111320
}

export function bearingTo(lon1: number, lat1: number, lon2: number, lat2: number) {
  const y = Math.sin((lon2 - lon1) * RAD) * Math.cos(lat2 * RAD)
  const x = Math.cos(lat1 * RAD) * Math.sin(lat2 * RAD) - Math.sin(lat1 * RAD) * Math.cos(lat2 * RAD) * Math.cos((lon2 - lon1) * RAD)
  return wrap(Math.atan2(y, x) / RAD)
}

const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
export function compass(b: number) {
  return COMPASS[Math.floor((wrap(b) + 22.5) / 45) % 8]
}

// -- the eye and the camera ---------------------------------------------------------------------

// Where the eye stands, in metres above sea level as the map draws the ground under it.
function eyeAltitude(stop: ViewStop) {
  const map = getMap()
  const g = groundAt(map, [stop.lon, stop.lat])
  return (g ?? stop.ground_m * EXAGGERATION) + stop.height_m
}

// The camera at an eye (lon, lat, metres above sea level) looking along a heading and tilt.
function cameraAt(lon: number, lat: number, alt: number, yaw: number, pitch: number, below: number | null) {
  const map = getMap()
  const dip = (90 - pitch) * RAD // how far under the horizon the eye looks
  // The map's centre: where the line of sight meets the ground, or 2 km out along it.
  let far = 2000
  if (dip > 0.01 && below !== null && alt - below > 0) far = Math.min(far, Math.max(30, (alt - below) / Math.tan(dip)))
  const [tlon, tlat] = destination(lon, lat, yaw, far * Math.cos(dip))
  return map.calculateCameraOptionsFromTo(new LngLat(lon, lat), alt, new LngLat(tlon, tlat), alt - far * Math.sin(dip))
}

let eye: { lon: number; lat: number; alt: number } | null = null

function place() {
  const s = get()
  if (!eye || !s.spec) return
  const ground = groundAt(getMap(), [eye.lon, eye.lat]) ?? s.spec.ground_m * EXAGGERATION
  viewCamera(cameraAt(eye.lon, eye.lat, eye.alt, s.yaw, s.pitch, ground))
  syncLabels()
}

// The ground under the eye comes in finer as its tiles load: stand on it.
function settleOnGround() {
  const s = get()
  if (!s.open || !s.spec || !eye || s.arriving) return
  const alt = eyeAltitude(s.spec)
  if (Math.abs(alt - eye.alt) > 0.3) {
    eye = { ...eye, alt }
    place()
  }
}

// The tilt that puts what the view looks at a little above the middle of the frame: from a roof
// over a near target the eye looks down at it, from a hill over a far one about level.
function aimAt(stop: ViewSpec | ViewStop) {
  const t = "toward" in stop ? stop.toward : undefined
  if (!t) return EYE_PITCH
  const d = metres(stop.lon, stop.lat, t.lon, t.lat)
  const drop = stop.eye_m - (t.ground_m + 12)
  return clamp(90 - Math.atan2(drop, d) / RAD - 1.5, 70, VIEW_MAX_PITCH)
}

// -- the flight in --------------------------------------------------------------------------------

let raf = 0
const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2)

function stopMotion() {
  cancelAnimationFrame(raf)
  raf = 0
  if (get().arriving || get().turning) set({ arriving: false, turning: false })
}

// Rise, travel and come down to the eye, turning to the heading on the way. With reduced motion
// the camera is simply there.
function flyTo(stop: ViewStop, yaw: number, pitch: number, onDone: () => void) {
  const map = getMap()
  stopMotion()
  if (reduced()) {
    eye = { lon: stop.lon, lat: stop.lat, alt: eyeAltitude(stop) }
    set({ yaw, pitch })
    place()
    onDone()
    return
  }
  const startLL = map.transform.getCameraLngLat()
  const start = { lon: startLL.lng, lat: startLL.lat, alt: map.transform.getCameraAltitude(), yaw: map.getBearing(), pitch: map.getPitch() }
  const dist = metres(start.lon, start.lat, stop.lon, stop.lat)
  const duration = clamp(2600 + dist * 0.45, 3200, 7000)
  const rise = clamp(dist * 0.22, 250, 2200) // metres above the straight path, at the middle
  const cruise = 62 // the tilt on the way: looking ahead and down at where it goes
  const yawTurn = turn(start.yaw, yaw)
  let t0 = 0
  set({ arriving: true })
  const step = (ts: number) => {
    if (!get().arriving) return
    if (!t0) t0 = ts
    const t = Math.min(1, (ts - t0) / duration)
    const k = ease(t)
    const lon = start.lon + (stop.lon - start.lon) * k
    const lat = start.lat + (stop.lat - start.lat) * k
    const end = eyeAltitude(stop)
    const alt = start.alt + (end - start.alt) * ease(Math.min(1, t * 1.1)) + rise * Math.sin(Math.PI * t)
    const p = t < 0.5 ? start.pitch + (cruise - start.pitch) * ease(t * 2) : cruise + (pitch - cruise) * ease((t - 0.5) * 2)
    const y = wrap(start.yaw + yawTurn * ease(Math.min(1, t * 1.25)))
    const ground = groundAt(map, [lon, lat])
    viewCamera(cameraAt(lon, lat, Math.max(alt, (ground ?? -1e9) + 2), y, p, ground))
    if (t < 1) { raf = requestAnimationFrame(step); return }
    raf = 0
    eye = { lon: stop.lon, lat: stop.lat, alt: end }
    set({ arriving: false, yaw, pitch })
    place()
    onDone()
  }
  raf = requestAnimationFrame(step)
}

// -- looking ----------------------------------------------------------------------------------------

export function look(dYaw: number, dPitch = 0, animate = true) {
  const s = get()
  if (!s.open || s.arriving) return
  if (s.turning) stopMotion()
  const yaw = wrap(s.yaw + dYaw), pitch = clamp(s.pitch + dPitch, VIEW_MIN_PITCH, VIEW_MAX_PITCH)
  if (!animate || reduced()) { set({ yaw, pitch }); place(); return }
  const from = { yaw: s.yaw, pitch: s.pitch }, d = turn(from.yaw, yaw)
  let t0 = 0
  cancelAnimationFrame(raf)
  const step = (ts: number) => {
    if (!t0) t0 = ts
    const t = Math.min(1, (ts - t0) / 320)
    const k = 1 - (1 - t) ** 3
    set({ yaw: wrap(from.yaw + d * k), pitch: from.pitch + (pitch - from.pitch) * k })
    place()
    raf = t < 1 ? requestAnimationFrame(step) : 0
  }
  raf = requestAnimationFrame(step)
}

// Look all the way round, slowly, from where the eye stands; again to stop. Off with reduced motion.
export function toggleLookAround() {
  const s = get()
  if (!s.open || s.arriving) return
  if (s.turning) { stopMotion(); return }
  if (reduced()) return
  cancelAnimationFrame(raf)
  const startYaw = s.yaw
  let t0 = 0
  set({ turning: true })
  const step = (ts: number) => {
    if (!get().turning) return
    if (!t0) t0 = ts
    const t = Math.min(1, (ts - t0) / AROUND_MS)
    set({ yaw: wrap(startYaw + 360 * t) })
    place()
    if (t < 1) raf = requestAnimationFrame(step)
    else { raf = 0; set({ turning: false, yaw: startYaw }) }
  }
  raf = requestAnimationFrame(step)
}

// -- the labels ---------------------------------------------------------------------------------------

type Label = { marker: maplibregl.Marker; el: HTMLElement; lon: number; lat: number; rank: number; w: number; h: number }
// Which label stays when two would overlap: what is in sight, then the centre, the walls, the
// tower on Ledras Street, the mountains, the other tall buildings.
const LABEL_RANK: Record<string, number> = { target: 0, centre: 0, walls: 1, mountains: 3, tower: 4 }
let labels: Label[] = []

function km(m: number) {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m / 10) * 10} m`
}

function clearLabels() {
  for (const l of labels) l.marker.remove()
  labels = []
}

// The walls' nearest stretch, from where the eye stands.
function wallsPoint(lon: number, lat: number): [number, number] | null {
  let best: [number, number] | null = null, bestD = Infinity
  for (const p of WALLS_RING) {
    const d = metres(lon, lat, p[0], p[1])
    if (d < bestD) { bestD = d; best = p }
  }
  return best && bestD > 250 && bestD < 8000 ? best : null
}

function makeLabels(spec: ViewSpec) {
  clearLabels()
  const map = getMap()
  const items: { title: string; text: string; lon: number; lat: number; hidden: boolean; rank: number }[] = []
  for (const m of spec.landmarks ?? []) {
    const rank = (m.title === "Shacolas Tower" ? 2 : LABEL_RANK[m.kind ?? "tower"] ?? 4) + (m.visible ? 0 : 10)
    items.push({
      title: m.title, lon: m.lon, lat: m.lat, hidden: !m.visible, rank,
      text: m.visible ? `${km(m.distance_m)} away` : `${km(m.distance_m)}, hidden by ${m.hidden_by ?? "what stands between"}`,
    })
  }
  const w = wallsPoint(spec.lon, spec.lat)
  if (w) items.push({ title: "The Venetian walls", text: `${km(metres(spec.lon, spec.lat, w[0], w[1]))} away`, lon: w[0], lat: w[1], hidden: false, rank: 1 })
  items.sort((a, b) => a.rank - b.rank)
  for (const it of items) {
    const el = document.createElement("div")
    el.className = "view-label"
    const box = document.createElement("div")
    box.className = it.hidden ? "callout is-hidden" : "callout"
    const b = document.createElement("b")
    b.textContent = it.title
    box.append(b, document.createTextNode(it.text))
    el.appendChild(box)
    const marker = new maplibregl.Marker({ element: el, anchor: "bottom", offset: [0, -10], opacityWhenCovered: "0.55" })
      .setLngLat([it.lon, it.lat]).addTo(map)
    labels.push({ marker, el, lon: it.lon, lat: it.lat, rank: it.rank, w: box.offsetWidth, h: box.offsetHeight })
  }
  syncLabels()
}

// Only the labels ahead of the eye (one behind would be drawn mirrored on the screen), and of
// two that would overlap, the one that matters more.
function syncLabels() {
  const s = get()
  if (!eye || !labels.length) return
  const map = getMap()
  const kept: [number, number, number, number][] = []
  for (const l of labels) {
    const off = Math.abs(turn(s.yaw, bearingTo(eye.lon, eye.lat, l.lon, l.lat)))
    let show = off <= LABEL_SIDE
    if (show) {
      const p = map.project([l.lon, l.lat])
      const box: [number, number, number, number] = [p.x - l.w / 2 - 4, p.y - l.h - 18, p.x + l.w / 2 + 4, p.y - 6]
      show = !kept.some((k) => box[0] < k[2] && k[0] < box[2] && box[1] < k[3] && k[1] < box[3])
      if (show) kept.push(box)
    }
    l.el.style.display = show ? "" : "none"
  }
}

// -- entering and leaving ---------------------------------------------------------------------------

type Saved = {
  camera: Camera | null; maxPitch: number; maxZoom: number; fov: number; clamped: boolean
  city: boolean; clockAt: number | null; clockSet: boolean; chatOpen: boolean
  handlers: Record<string, boolean>; focus: HTMLElement | null
}
let saved: Saved | null = null
const HANDLERS = ["dragPan", "dragRotate", "scrollZoom", "boxZoom", "keyboard", "doubleClickZoom", "touchZoomRotate", "touchPitch"] as const

function saveAndPrepare() {
  const map = getMap()
  const a = document.activeElement as HTMLElement | null
  saved = {
    camera: getCamera(), maxPitch: map.getMaxPitch(), maxZoom: map.getMaxZoom(),
    fov: map.getVerticalFieldOfView(), clamped: map.getCenterClampedToGround(),
    city: useMapUi.getState().city.on, clockAt: useClock.getState().at, clockSet: false,
    chatOpen: useLayout.getState().chatOpen,
    handlers: Object.fromEntries(HANDLERS.map((h) => [h, map[h].isEnabled()])),
    focus: a && a !== document.body ? a : null,
  }
  for (const h of HANDLERS) map[h].disable()
  map.setMaxPitch(VIEW_MAX_PITCH)
  map.setMaxZoom(24)
  map.setVerticalFieldOfView(FOV)
  map.setCenterClampedToGround(false)
  setTerrain(map, true)
  map.getContainer().classList.add("is-viewing")
  map.getCanvasContainer().addEventListener("pointerdown", onPointerDown)
  map.on("sourcedata", onTerrainData)
  window.addEventListener("keydown", onKey)
  onViewTakeover(() => leaveView({ restoreCamera: false }))
  // The right column and a phone's sheet make room for the card.
  closeInspector({ returnFocus: false })
  const phone = !window.matchMedia("(min-width: 768px)").matches
  useLayout.setState({ layersOpen: false, ...(phone ? { chatOpen: false } : {}) })
}

function onTerrainData(e: maplibregl.MapSourceDataEvent) {
  if (e.sourceId === TERRAIN_SOURCE && e.isSourceLoaded) settleOnGround()
}

// Show a view. A view already open moves on to the new one (from where the eye is).
export function enterView(spec: ViewSpec, opts: { fly?: boolean } = {}) {
  const map = getMap()
  if (!map) return
  const wasOpen = get().open
  if (!wasOpen) saveAndPrepare()
  const tour = spec.tour && spec.tour.length ? spec.tour : wasOpen && get().tour.some((t) => t.id === spec.id) ? get().tour : [spec]
  const index = Math.max(0, tour.findIndex((t) => t.id === spec.id))
  set({ open: true, spec, tour, index, error: null, loading: false, facts: wasOpen ? get().facts : [] })
  setViewFocus({ lon: spec.lon, lat: spec.lat, radiusM: DLS_RADIUS_M, hide: buildingNumber(spec.building_id) })
  setCity3d(true)
  if (spec.at) { // a night view in daylight: the city as it was then
    const t = fromWall(spec.at)
    if (Number.isFinite(t) && saved) { saved.clockSet = true; setAt(t) }
  }
  clearLabels()
  const yaw = wrap(spec.bearing), pitch = clamp(spec.pitch ?? aimAt(spec), VIEW_MIN_PITCH, VIEW_MAX_PITCH)
  const arrive = () => {
    makeLabels(spec)
    if (spec.look_around) toggleLookAround()
  }
  if (opts.fly === false) {
    eye = { lon: spec.lon, lat: spec.lat, alt: eyeAltitude(spec) }
    set({ yaw, pitch })
    place()
    arrive()
  } else {
    // Wait a frame for the panels to settle, so the camera's padding doesn't cut the flight short.
    requestAnimationFrame(() => requestAnimationFrame(() => flyTo(spec, yaw, pitch, arrive)))
  }
}

export function leaveView(opts: { restoreCamera?: boolean } = {}) {
  const s = get()
  if (!s.open) return
  const map = getMap()
  stopMotion()
  clearLabels()
  eye = null
  window.removeEventListener("keydown", onKey)
  map.getCanvasContainer().removeEventListener("pointerdown", onPointerDown)
  map.getContainer().classList.remove("is-viewing")
  map.off("sourcedata", onTerrainData)
  onViewTakeover(null)
  set({ open: false, spec: null, arriving: false, turning: false, tour: [], index: 0, facts: [], loading: false })
  setViewFocus(null)
  setTerrain(map, false)
  const sv = saved
  saved = null
  if (!sv) return
  setCity3d(sv.city)
  map.setCenterClampedToGround(sv.clamped)
  map.setVerticalFieldOfView(sv.fov)
  map.setMaxZoom(sv.maxZoom)
  if (opts.restoreCamera !== false && sv.camera) jumpToCamera({ ...sv.camera, pitch: Math.min(sv.camera.pitch, sv.maxPitch) })
  else if (map.getPitch() > sv.maxPitch) map.jumpTo({ pitch: sv.maxPitch })
  map.setMaxPitch(sv.maxPitch)
  for (const h of HANDLERS) if (sv.handlers[h]) map[h].enable()
  if (sv.clockSet && useClock.getState().at !== sv.clockAt) setAt(sv.clockAt)
  if (sv.chatOpen && !useLayout.getState().chatOpen) useLayout.setState({ chatOpen: true })
  if (opts.restoreCamera !== false) {
    const back = sv.focus?.isConnected ? sv.focus : document.querySelector<HTMLElement>("textarea")
    back?.focus()
  }
}

// The next place of the same answer, flown to from here.
export function nextView() {
  const s = get()
  if (!s.open || s.tour.length < 2 || s.arriving) return
  const index = (s.index + 1) % s.tour.length
  const stop = s.tour[index]
  stopMotion()
  clearLabels()
  set({ index, spec: { ...stop, at: s.spec?.at, dark: s.spec?.dark, when: s.spec?.when }, facts: [] })
  setViewFocus({ lon: stop.lon, lat: stop.lat, radiusM: DLS_RADIUS_M, hide: buildingNumber(stop.building_id) })
  flyTo(stop, wrap(stop.bearing), EYE_PITCH, () => {
    // Its labels, worked out as for any view.
    fetchView({ from: `${stop.lat},${stop.lon}`, height: String(stop.height_m), name: stop.name })
      .then((v) => {
        const cur = get()
        if (!cur.open || cur.spec?.id !== stop.id) return
        const spec = { ...cur.spec, landmarks: v.view.landmarks }
        set({ spec, facts: v.facts })
        makeLabels(spec)
      })
      .catch(() => { /* the view stands without its labels */ })
  })
}

// -- input ----------------------------------------------------------------------------------------

const LISTS = "[role=listbox], [role=menu], [role=tablist], [role=radiogroup], [role=slider], [role=grid], [role=tree], [role=toolbar]"

function onKey(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
  if (t?.closest("input, textarea, select, [contenteditable], [role=dialog]")) return
  if (e.key === "Escape") { e.preventDefault(); leaveView(); return }
  if (t?.closest(LISTS)) return
  const big = e.shiftKey ? 3 : 1
  if (e.key === "ArrowLeft") { e.preventDefault(); look(-LOOK_STEP * big) }
  else if (e.key === "ArrowRight") { e.preventDefault(); look(LOOK_STEP * big) }
  else if (e.key === "ArrowUp") { e.preventDefault(); look(0, PITCH_STEP) }
  else if (e.key === "ArrowDown") { e.preventDefault(); look(0, -PITCH_STEP) }
}

// Drag the view: the scene follows the pointer, as if turning your head.
function onPointerDown(e: PointerEvent) {
  if (e.button !== 0 || !get().open || get().arriving) return
  const el = e.currentTarget as HTMLElement
  let x = e.clientX, y = e.clientY
  if (get().turning) stopMotion()
  el.setPointerCapture(e.pointerId)
  const move = (ev: PointerEvent) => {
    const s = get()
    const dx = ev.clientX - x, dy = ev.clientY - y
    x = ev.clientX; y = ev.clientY
    set({ yaw: wrap(s.yaw - dx * 0.18), pitch: clamp(s.pitch + dy * 0.12, VIEW_MIN_PITCH, VIEW_MAX_PITCH) })
    place()
  }
  const up = () => {
    el.removeEventListener("pointermove", move)
    el.removeEventListener("pointerup", up)
    el.removeEventListener("pointercancel", up)
  }
  el.addEventListener("pointermove", move)
  el.addEventListener("pointerup", up)
  el.addEventListener("pointercancel", up)
}

// -- from the assistant, a link, or the inspector ---------------------------------------------------

export function applyViewCommand(ev: { view: ViewSpec }) {
  if (ev.view) enterView(ev.view)
}

type ViewQuery = { from: string; height?: string; bearing?: number; toward?: string; name?: string }

async function fetchView(q: ViewQuery): Promise<{ view: ViewSpec; facts: string[] }> {
  const p = new URLSearchParams({ from: q.from })
  if (q.height) p.set("height", q.height)
  if (q.bearing !== undefined) p.set("bearing", String(Math.round(q.bearing)))
  if (q.toward) p.set("toward", q.toward)
  if (q.name) p.set("name", q.name)
  const res = await fetch(`/api/view?${p}`)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`)
  return body
}

// "View from here" (the inspector) and links: ask the server for the view, then show it.
export async function viewFrom(q: ViewQuery & { pitch?: number }) {
  set({ loading: true, error: null })
  try {
    const v = await fetchView(q)
    enterView({ ...v.view, ...(q.pitch !== undefined ? { pitch: q.pitch } : {}) })
    set({ facts: v.facts })
  } catch (e) {
    set({ loading: false, error: `Couldn't work out that view: ${(e as Error).message}` })
  }
}

// A hook for scripted demos and browser tests, as the map engine keeps window.__map.
;(window as unknown as { __view: unknown }).__view = { viewFrom, enterView, leaveView, look, nextView, toggleLookAround, useView }

// What the assistant hears about the view with each message.
export function viewContext() {
  const s = get()
  if (!s.open || !s.spec) return null
  return { name: s.spec.name, lat: s.spec.lat, lon: s.spec.lon, eye_m: s.spec.eye_m, looking: compass(s.yaw) }
}

// For links (lib/deeplink.ts): where the eye stands and looks.
export function viewLink() {
  const s = get()
  if (!s.open || !s.spec) return null
  return { lat: s.spec.lat, lon: s.spec.lon, height: s.spec.height_m, yaw: s.yaw, pitch: s.pitch, name: s.spec.name }
}

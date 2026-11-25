/* Links to a view: the URL's hash holds the camera and the open thing, so a view can be shared and
   survives a reload.

     #/@35.17250,33.36150,16.5z            the camera (lat, lon, zoom; then pitch p, bearing b)
     #/e/YL1015                            a thing in the inspector
     #/e/YL1015/@35.16680,33.36082,17z,45p a thing and the camera

   The hash follows the map with replaceState, so the back button isn't filled with every pan. */
import { closeInspector, inspect, useInspector } from "@/lib/inspector"
import { getCamera, jumpToCamera, onCameraMoved, type Camera } from "@/lib/map/engine"

export type Link = { camera: Camera | null; entity: string | null }

const NUM = "(-?\\d+(?:\\.\\d+)?)"
const CAMERA = new RegExp(`@${NUM},${NUM},${NUM}z(?:,${NUM}p)?(?:,${NUM}b)?`)

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
  return { camera, entity }
}

const r = (v: number, d: number) => Number(v.toFixed(d)).toString()

export function formatHash({ camera, entity }: Link): string {
  const parts: string[] = []
  if (entity) parts.push(`e/${encodeURIComponent(entity)}`)
  if (camera) {
    const [lon, lat] = camera.center
    let cam = `@${r(lat, 5)},${r(lon, 5)},${r(camera.zoom, 2)}z`
    if (Math.round(camera.pitch) || Math.round(camera.bearing)) cam += `,${Math.round(camera.pitch)}p`
    if (Math.round(camera.bearing)) cam += `,${Math.round(camera.bearing)}b`
    parts.push(cam)
  }
  return parts.length ? `#/${parts.join("/")}` : ""
}

function current(): Link {
  const { open, entity, target } = useInspector.getState()
  return { camera: getCamera(), entity: open ? (entity?.id ?? target?.id ?? null) : null }
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

// A link to one thing, at the camera now.
export function linkTo(entity: string) {
  return `${location.origin}${location.pathname}${location.search}${formatHash({ camera: getCamera(), entity })}`
}

let timer: number | undefined
function soon() {
  clearTimeout(timer)
  timer = window.setTimeout(write, 250)
}

// Call once the map exists: the hash follows the camera and the inspector, and a hash typed or
// pasted into the address bar is applied.
let started = false
export function startDeepLinks(initial: Link) {
  if (started) return // React's development mode runs effects twice
  started = true
  written = location.hash
  onCameraMoved(soon)
  useInspector.subscribe((s, prev) => {
    if (s.open !== prev.open || s.entity?.id !== prev.entity?.id || s.target?.id !== prev.target?.id) soon()
  })
  window.addEventListener("hashchange", () => {
    if (location.hash === written) return
    const link = parseHash(location.hash)
    written = location.hash
    if (link.camera) jumpToCamera(link.camera)
    if (link.entity && link.entity !== useInspector.getState().entity?.id) inspect({ id: link.entity }, { fly: !link.camera })
    else if (!link.entity && useInspector.getState().open) closeInspector()
  })
  if (initial.entity) inspect({ id: initial.entity }, { fly: !initial.camera })
}

/* The camera walkthrough's data: every counting camera, its lines and their roles in the
   counting-lines register (GET /api/traffic-lines, chatbot/routes/traffic_lines.py), the edits
   made on the page before they are saved, and saving one camera (PUT, behind the admin
   password). The order is fixed on first load (cameras to review first), so a saved camera
   doesn't jump while someone works through them. */
import { create } from "zustand"

import { adminHeaders } from "@/lib/collectors"

export type Measures = "people" | "vehicles" | "both"
export type Direction = "in" | "out" | "both" | "one way"
export type AddsUp = "yes" | "no" | "review"

export type LineRole = {
  analytics_id: number; camera: string; line_id: number | null; line_name: string
  measures: Measures; direction: Direction; street: string; adds_up: AddsUp
  reviewed: string; note: string
}

export type ReviewLine = LineRole & {
  suggested: LineRole
  in_file: boolean
  setting: number | null // 0: counts both ways; 1 or 2: one way only
  count_type: number | null
  types: number[]
  ends: [number | null, number | null, number | null, number | null] // x1, y1, x2, y2 (0 to 1)
  yesterday: { kinds: Record<string, number>; way_1: number; way_2: number; hours: number[] } | null
}

export type ReviewCamera = {
  analytics_id: number; name: string; stream_name: string | null; status: string | null
  lat: number | null; lon: number | null; location_source: "device" | "manual" | null
  lines: ReviewLine[]; reviewed: boolean
}

export type Walkthrough = {
  cameras: ReviewCamera[]
  reviewed: number
  hours: string[]
  yesterday_missed_hours: number
  admin: { configured: boolean; unlocked: boolean }
}

export type Draft = Pick<LineRole, "measures" | "direction" | "street" | "adds_up" | "note">

type State = {
  data: Walkthrough | null
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  order: number[] // analytics ids, fixed on first load
  current: number | null // the analytics id shown
  drafts: Record<number, Record<string, Draft>> // per camera, per line name
  saving: boolean
}

export const useReview = create<State>(() => ({
  data: null, status: "idle", error: null, order: [], current: null, drafts: {}, saving: false,
}))

async function call(path: string, init?: RequestInit): Promise<Walkthrough> {
  const headers: Record<string, string> = { ...adminHeaders() }
  if (init?.method && init.method !== "GET") headers["Content-Type"] = "application/json"
  let res: Response
  try {
    res = await fetch(`/api/traffic-lines${path}`, { ...init, headers })
  } catch {
    throw new Error("The server didn't answer. It may be restarting; try again in a moment.")
  }
  if (!res.ok) {
    let detail = ""
    try { detail = (await res.json()).detail ?? "" } catch { /* not JSON */ }
    throw new Error(detail ? String(detail).replace(/^./, (c) => c.toUpperCase()) : `The server answered ${res.status}.`)
  }
  return res.json()
}

function take(data: Walkthrough) {
  const s = useReview.getState()
  const known = new Set(s.order)
  // First load: the server's order (to review first). Later: keep it, new cameras at the end.
  const order = s.order.length ? [...s.order, ...data.cameras.map((c) => c.analytics_id).filter((id) => !known.has(id))]
    : data.cameras.map((c) => c.analytics_id)
  useReview.setState({ data, status: "ready", error: null, order, current: s.current ?? order[0] ?? null })
}

let inflight: Promise<void> | null = null

export function loadReview(): Promise<void> {
  if (inflight) return inflight
  if (!useReview.getState().data) useReview.setState({ status: "loading" })
  inflight = call("")
    .then(take)
    .catch((e: Error) => useReview.setState((s) => ({ status: s.data ? "ready" : "error", error: e.message })))
    .finally(() => { inflight = null })
  return inflight
}

export function cameraById(id: number | null): ReviewCamera | undefined {
  return useReview.getState().data?.cameras.find((c) => c.analytics_id === id)
}

export function showCamera(id: number) {
  useReview.setState({ current: id })
}

export function step(by: 1 | -1) {
  const { order, current } = useReview.getState()
  if (!order.length) return
  const i = Math.max(0, order.indexOf(current ?? order[0]))
  showCamera(order[(i + by + order.length) % order.length])
}

// What the page shows for a line: the edit in progress, or what the register says.
export function lineValue(cam: ReviewCamera, line: ReviewLine): Draft {
  const d = useReview.getState().drafts[cam.analytics_id]?.[line.line_name]
  return d ?? { measures: line.measures, direction: line.direction, street: line.street, adds_up: line.adds_up, note: line.note }
}

export function editLine(cam: ReviewCamera, line: ReviewLine, patch: Partial<Draft>) {
  const value = { ...lineValue(cam, line), ...patch }
  useReview.setState((s) => ({
    drafts: { ...s.drafts, [cam.analytics_id]: { ...s.drafts[cam.analytics_id], [line.line_name]: value } },
  }))
}

export function useEdited(id: number | null): boolean {
  return useReview((s) => id !== null && !!s.drafts[id] && Object.keys(s.drafts[id]).length > 0)
}

// Save every line of the camera as shown, marking it confirmed today.
export async function saveCamera(id: number): Promise<boolean> {
  const cam = cameraById(id)
  if (!cam || useReview.getState().saving) return false
  useReview.setState({ saving: true })
  try {
    const lines = cam.lines.map((l) => ({ line_name: l.line_name, ...lineValue(cam, l) }))
    take(await call(`/${id}`, { method: "PUT", body: JSON.stringify({ lines }) }))
    useReview.setState((s) => {
      const drafts = { ...s.drafts }
      delete drafts[id]
      return { drafts }
    })
    return true
  } catch (e) {
    useReview.setState({ error: (e as Error).message })
    return false
  } finally {
    useReview.setState({ saving: false })
  }
}

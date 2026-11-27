/* The admin page's data: the collectors report from /api/admin/collectors (chatbot/collectors.py)
   and its controls. Every change answers with the whole report, so the page redraws from one
   shape. The report is fetched again every few seconds while the page is visible, faster while a
   job is running or waiting to run. */
import { create } from "zustand"

export type JobRun = {
  status: "ok" | "partial" | "error" | "skipped" | null
  started: string | null
  seconds: number | null
  requests: number | null
  rows: number | null
  message: string | null
  last_ok: string | null
  errors_24h: number
}

export type JobSource = {
  id: string; name: string; short?: string; publisher?: string; gives?: string; url?: string | null
  licence?: string | null; licence_url?: string | null
}

export type JobSettings = { enabled: boolean; every_minutes: number; night_only: boolean }

export type CollectorJob = JobSettings & {
  name: string
  title: string
  summary: string // one short line under the title
  records: string
  uses: string
  how: string // how it is collected, in plain words
  group: string
  tables: string[]
  source: JobSource
  defaults: JobSettings
  changed: (keyof JobSettings)[]
  counts_against_budget: boolean
  requests_per_run: number
  requests_per_day: number
  requests_24h: number
  waiting_for_dark: boolean
  state: "running" | "queued" | null
  running_since: string | null
  last: JobRun | null
  next_due: string | null
}

export type CollectorsReport = {
  now: string
  collecting: boolean
  decided_on_page: boolean
  collect_default: boolean
  elsewhere_now: boolean
  other_collector_last_run: string | null // the newest run another collector made (Task Scheduler, Docker)
  other_collector_recent: boolean
  last_pass_here: { at: string; outcome: "ran" | "nothing due" | "elsewhere" | "error"; jobs?: string[]; failed?: string[]; message?: string } | null
  running: string | null
  queued: string[]
  night: boolean
  night_fraction: number
  store: { path: string; status: "ok" | "missing" | "busy" | "error"; size_mb?: number; message?: string }
  budget: { per_day: number; limit: number; last_24h: number; other_per_day: number }
  recorder: {
    on: boolean; recording: boolean; paused?: boolean; rows_written?: number; pending_rows?: number
    last_flush?: string | null; last_error?: string | null; dropped_rows?: number
  }
  jobs: CollectorJob[]
  groups: string[]
  unrecorded: { source: JobSource; why: string }[]
  recent_errors: { at: string; job: string; title: string; kind: string; message: string }[]
  cadences: number[]
  changed_at: string | null
  // configured: the server has an admin password; unlocked: this page sent the right one.
  admin: { configured: boolean; unlocked: boolean }
}

// One job in depth: GET /api/admin/collectors/jobs/{name}/how (chatbot/collectors.py, how()).
export type HowItsCollected = {
  name: string; title: string; summary: string; how: string; records: string; uses: string
  source: JobSource
  doc: string | null
  code: {
    path: string; line: number; url: string; commit: string | null
    on_github: boolean | null; changed_since_commit: boolean | null
  }
  calls: { endpoint: string; method: string; url: string }[]
  cost: { per_run: number; per_day: number; why: string; counts_against_budget: boolean }
  settings: { key: string; label: string; value: unknown; default: unknown }[]
  tables: {
    name: string; rows: number; oldest: string | null; newest: string | null
    shared_with: string[]; columns: { name: string; type: string }[]
  }[]
  runs: { started: string; status: string; seconds: number | null; requests: number | null; rows: number | null; message: string | null }[]
  store_note: string | null
}

type State = {
  report: CollectorsReport | null
  status: "loading" | "ready" | "error"
  error: string | null // the last failed load or change, said for people
  pending: Record<string, boolean> // what a change is in flight for: "collecting", "recording", a job name
}

export const useCollectors = create<State>(() => ({ report: null, status: "loading", error: null, pending: {} }))

// Which job's "How it's collected" sheet is open; /admin#job=<name> links to it.
export const useHow = create<{ name: string | null }>(() => ({ name: null }))

export function openHow(name: string) {
  useHow.setState({ name })
  history.replaceState(null, "", `#job=${encodeURIComponent(name)}`)
}

export function closeHow() {
  useHow.setState({ name: null })
  history.replaceState(null, "", location.pathname)
}

const BASE = "/api/admin/collectors"

// The admin password, once entered, lives for this tab only (sessionStorage), and goes with every
// request in its own header. A browser sends a custom header only from this page, never from
// another site, so no other site can make changes here on someone's behalf.
const PASSWORD_KEY = "twin.admin-password"

function adminPassword(): string | null {
  try { return sessionStorage.getItem(PASSWORD_KEY) } catch { return null }
}

function keepAdminPassword(password: string | null) {
  try {
    if (password) sessionStorage.setItem(PASSWORD_KEY, password)
    else sessionStorage.removeItem(PASSWORD_KEY)
  } catch { /* private mode: it lasts until the page reloads */ }
  memoryPassword = password
}

let memoryPassword: string | null = null

class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

// The admin password's header, when this tab has one: for any admin request (the camera
// walkthrough's saves too).
export function adminHeaders(): Record<string, string> {
  const password = adminPassword() ?? memoryPassword
  return password ? { "X-Admin-Password": password } : {}
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...adminHeaders() }
  // Every change goes as JSON, even one with nothing to say.
  if (init?.method && init.method !== "GET") headers["Content-Type"] = "application/json"
  let res: Response
  try {
    res = await fetch(BASE + path, {
      ...init,
      body: init?.method && init.method !== "GET" ? (init.body ?? "{}") : undefined,
      headers,
    })
  } catch {
    throw new ApiError("The server didn't answer. It may be restarting; try again in a moment.", 0)
  }
  if (!res.ok) {
    let detail = ""
    try {
      const body = await res.json()
      detail = typeof body.detail === "string" ? body.detail : ""
    } catch { /* not JSON */ }
    throw new ApiError(detail ? detail.replace(/^./, (c) => c.toUpperCase()) : `The server answered ${res.status}.`, res.status)
  }
  return res.json()
}

const call = (path: string, init?: RequestInit) => request<CollectorsReport>(path, init)

export const loadHow = (name: string) => request<HowItsCollected>(`/jobs/${encodeURIComponent(name)}/how`)

// Unlock changes for this tab: keeps the password only if the server takes it.
export async function unlockChanges(password: string): Promise<boolean> {
  keepAdminPassword(password)
  try {
    const report = await call("")
    useCollectors.setState({ report, status: "ready", error: null })
    if (report.admin.unlocked) return true
  } catch { /* said below */ }
  keepAdminPassword(null)
  return false
}

export async function lockChanges() {
  keepAdminPassword(null)
  await loadCollectors()
}

let inflight: Promise<void> | null = null

export function loadCollectors(): Promise<void> {
  if (inflight) return inflight
  inflight = call("")
    .then((report) => useCollectors.setState({ report, status: "ready", error: null }))
    .catch((e: Error) => useCollectors.setState((s) => ({ status: s.report ? "ready" : "error", error: e.message })))
    .finally(() => { inflight = null })
  return inflight
}

// Runs a change, keeps the answer, and says what went wrong if it failed (the caller shows it).
async function change(key: string, path: string, init: RequestInit): Promise<boolean> {
  useCollectors.setState((s) => ({ pending: { ...s.pending, [key]: true } }))
  try {
    const report = await call(path, init)
    useCollectors.setState({ report, status: "ready", error: null })
    return true
  } catch (e) {
    useCollectors.setState({ error: (e as Error).message })
    // A refused password (changed on the server, say) locks the page again.
    if (e instanceof ApiError && e.status === 403) {
      keepAdminPassword(null)
      void loadCollectors()
    }
    return false
  } finally {
    useCollectors.setState((s) => {
      const pending = { ...s.pending }
      delete pending[key]
      return { pending }
    })
  }
}

const json = (body: unknown) => JSON.stringify(body)

export const setCollecting = (on: boolean) =>
  change("collecting", "/collecting", { method: "PUT", body: json({ on }) })

export const setRecording = (on: boolean) =>
  change("recording", "/recording", { method: "PUT", body: json({ on }) })

export const updateJob = (name: string, patch: Partial<JobSettings>) =>
  change(name, `/jobs/${encodeURIComponent(name)}`, { method: "PATCH", body: json(patch) })

export const resetJob = (name: string) =>
  change(name, `/jobs/${encodeURIComponent(name)}/changes`, { method: "DELETE" })

export const runJobNow = (name: string) =>
  change(name, `/jobs/${encodeURIComponent(name)}/run`, { method: "POST" })

// Keeps the report fresh while the page is visible: every 5 s, every 1.5 s while a job runs.
export function startPolling(): () => void {
  let timer: number | undefined
  const tick = async () => {
    if (document.visibilityState === "visible") await loadCollectors()
    const r = useCollectors.getState().report
    const busy = !!r && (r.running !== null || r.queued.length > 0)
    timer = window.setTimeout(tick, busy ? 1500 : 5000)
  }
  const onVisible = () => { if (document.visibilityState === "visible") void loadCollectors() }
  document.addEventListener("visibilitychange", onVisible)
  void tick()
  return () => {
    window.clearTimeout(timer)
    document.removeEventListener("visibilitychange", onVisible)
  }
}

// -- words and numbers -------------------------------------------------------------------------

export function cadenceLabel(minutes: number): string {
  if (minutes === 1) return "Every minute"
  if (minutes < 60) return `Every ${fmt(minutes)} minutes`
  if (minutes === 60) return "Every hour"
  if (minutes < 1440) return `Every ${fmt(minutes / 60)} hours`
  if (minutes === 1440) return "Once a day"
  return `Every ${fmt(minutes / 1440)} days`
}

const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1))

// What a job would cost a day with these settings, as the server counts it.
export function requestsPerDay(job: CollectorJob, s: JobSettings, nightFraction: number): number {
  if (!s.enabled) return 0
  const runs = (1440 / s.every_minutes) * (s.night_only ? nightFraction : 1)
  return Math.round(runs * job.requests_per_run)
}

const NICOSIA = "Asia/Nicosia"

export function ago(iso: string | null, now = Date.now()): string | null {
  if (!iso) return null
  const s = Math.round((now - new Date(iso).getTime()) / 1000)
  if (s < 45) return "just now"
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 36) return `${h} h ago`
  return `${Math.round(h / 24)} days ago`
}

export function until(iso: string | null, now = Date.now()): string | null {
  if (!iso) return null
  const s = Math.round((new Date(iso).getTime() - now) / 1000)
  if (s < 45) return "due now"
  const m = Math.round(s / 60)
  if (m < 60) return `in ${m} min`
  const h = Math.round(m / 60)
  return h < 36 ? `in ${h} h` : `in ${Math.round(h / 24)} days`
}

export function clockTime(iso: string | null): string {
  if (!iso) return ""
  return new Date(iso).toLocaleString("en-GB", {
    timeZone: NICOSIA, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  })
}

export const number = (n: number | null | undefined) => (n == null ? "" : n.toLocaleString("en-GB"))

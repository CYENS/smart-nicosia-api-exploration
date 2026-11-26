/* The twin's watch on the page: the incident inbox (its filters, its rows and the incident open
   in it), what people do about an incident, the watches, and the counts the live stream keeps
   current. Everything comes from /api/incidents and /api/watches (chatbot/routes/watch.py). */
import { create } from "zustand"

import { linkHover } from "@/lib/map/engine"

export type Severity = "high" | "medium" | "low"
export type IncidentStatus = "open" | "acknowledged" | "snoozed" | "resolved"

export type IncidentRow = {
  id: string; title: string; severity: Severity; status: IncidentStatus; active: boolean
  category: string; family: string; place: string; lat: number | null; lon: number | null
  start: string; start_ms: number; last_seen: string; last_seen_ms: number
  snoozed_until: string | null; devices: number; ids: string[]; kinds: Record<string, number>
  source: string; last_action: { action: string; by: string | null; at: string } | null
  updated_ms: number
}
export type Member = {
  id: string; kind: string; label: string; street: string | null; lat: number | null; lon: number | null
  what: string; severity: Severity; start: string; end: string | null; active: boolean; source: string
}
export type Evidence = {
  time: string; text: string; device?: string; kind?: string; value?: number; unit?: string
  [k: string]: unknown
}
export type IncidentAction = { at: string; t_ms: number; action: string; by: string | null; until: string | null; note: string | null }
export type IncidentDetail = IncidentRow & {
  summary: string; members: Member[]; evidence: Evidence[]; actions: IncidentAction[]; created: string
}
export type Counts = {
  unresolved: number; open: number; acknowledged: number; snoozed: number; high: number; medium: number; low: number
}
export type WatchRow = {
  id: string; label: string; kind: string; rule: Record<string, unknown>; created: string; by: string | null
  watching: number; true_now: string[]; last_fired: string | null
  fires: { at: string; t_ms: number; device: string; message: string; value: number | null }[]
}
export type WatchProposal = {
  proposal_id: string; label: string; rule: Record<string, unknown>; watching: number
  true_now: { id: string; value: number | null; state: string }[]
  values_now: { id: string; value: number | null; state: string }[]
}

// What the inbox shows: everything still to see to, the snoozed, the resolved or all of them.
export type StatusFilter = "unresolved" | "snoozed" | "resolved" | "all"
export type SeverityFilter = "" | "medium" | "high"

type Load = "idle" | "loading" | "ready" | "error"

type WatchState = {
  counts: Counts | null
  status: StatusFilter
  severity: SeverityFilter
  rows: IncidentRow[]
  rowsStatus: Load
  rowsError: string | null
  selected: string | null // the incident open in the inbox's detail view
  detail: IncidentDetail | null
  detailStatus: Load | "missing"
  detailError: string | null
  acting: string | null // the action on its way to the server
  actError: string | null
  watches: WatchRow[]
  watchesStatus: Load
  stream: "connecting" | "live" | "down"
  name: string // who is acting, remembered on this device ("who did what")
}

export const SEVERITY_COLOR: Record<Severity, string> = { high: "#ff5a4e", medium: "#f5a524", low: "#8798b0" }
export const SEVERITY_WORD: Record<Severity, string> = { high: "High", medium: "Medium", low: "Low" }
export const STATUS_WORD: Record<IncidentStatus, string> = {
  open: "Open", acknowledged: "Acknowledged", snoozed: "Snoozed", resolved: "Resolved",
}
export const INCIDENT_ID = /^INC-\d{1,7}$/i

const NAME_KEY = "twin.watch.name"

function readName() {
  try { return localStorage.getItem(NAME_KEY) ?? "" } catch { return "" }
}

export const useWatch = create<WatchState>(() => ({
  counts: null, status: "unresolved", severity: "", rows: [], rowsStatus: "idle", rowsError: null,
  selected: null, detail: null, detailStatus: "idle", detailError: null, acting: null, actError: null,
  watches: [], watchesStatus: "idle", stream: "connecting", name: readName(),
}))
const set = useWatch.setState
const get = useWatch.getState

export function setName(name: string) {
  set({ name })
  try { localStorage.setItem(NAME_KEY, name) } catch { /* private mode: kept for this visit */ }
}

async function json<T>(url: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { ...init, signal })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const d = (body as { detail?: unknown }).detail
    throw Object.assign(new Error(typeof d === "string" ? d : `The server answered ${res.status}`), { status: res.status })
  }
  return body as T
}

// -- the inbox -------------------------------------------------------------------------------------

let rowsCtrl: AbortController | null = null

export async function loadIncidents() {
  rowsCtrl?.abort()
  const mine = (rowsCtrl = new AbortController())
  const { status, severity, rows } = get()
  if (!rows.length) set({ rowsStatus: "loading", rowsError: null })
  const q = new URLSearchParams({ status, limit: "200" })
  if (severity) q.set("severity", severity)
  try {
    const body = await json<{ incidents: IncidentRow[]; counts: Counts }>(`/api/incidents?${q}`, undefined, mine.signal)
    if (mine.signal.aborted) return
    set({ rows: body.incidents, counts: body.counts, rowsStatus: "ready", rowsError: null })
  } catch (e) {
    if (mine.signal.aborted || (e as Error).name === "AbortError") return
    set({ rowsStatus: "error", rowsError: (e as Error).message })
  }
}

export function setFilter(p: { status?: StatusFilter; severity?: SeverityFilter }) {
  set({ ...p, rows: [], rowsStatus: "loading" })
  void loadIncidents()
}

// -- one incident ----------------------------------------------------------------------------------

let detailCtrl: AbortController | null = null

export async function loadDetail(id: string, quiet = false) {
  detailCtrl?.abort()
  const mine = (detailCtrl = new AbortController())
  if (!quiet) set({ detailStatus: "loading", detailError: null })
  try {
    const d = await json<IncidentDetail>(`/api/incidents/${encodeURIComponent(id)}`, undefined, mine.signal)
    if (mine.signal.aborted || get().selected !== id) return
    set({ detail: d, detailStatus: "ready", detailError: null })
  } catch (e) {
    if (mine.signal.aborted || (e as Error).name === "AbortError") return
    const status = (e as { status?: number }).status
    set({ detailStatus: status === 404 ? "missing" : "error", detailError: (e as Error).message })
  }
}

// Open an incident in the inbox's detail view (the workspace opens it; see lib/watch/index.ts).
export function selectIncident(id: string | null) {
  const key = id ? id.toUpperCase() : null
  if (key === get().selected) return
  // A row hovered or focused as it opened rings its mark; it unmounts without telling.
  linkHover("id", null, "inbox")
  set({ selected: key, detail: null, detailStatus: key ? "loading" : "idle", detailError: null, actError: null })
  if (key) void loadDetail(key)
}

export type ActionName = "acknowledge" | "snooze" | "unsnooze" | "resolve" | "reopen"

// Acknowledge, snooze (until a time), unsnooze, resolve or reopen, with the name if given.
export async function act(id: string, action: ActionName, opts: { until?: string; note?: string } = {}) {
  set({ acting: action, actError: null })
  try {
    const d = await json<IncidentDetail>(`/api/incidents/${encodeURIComponent(id)}/actions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, by: get().name.trim() || null, until: opts.until ?? null, note: opts.note ?? null }),
    })
    if (get().selected === d.id) set({ detail: d, detailStatus: "ready" })
    set({ acting: null })
    void loadIncidents()
    return d
  } catch (e) {
    set({ acting: null, actError: (e as Error).message })
    return null
  }
}

// -- watches ---------------------------------------------------------------------------------------

export async function loadWatches() {
  if (!get().watches.length) set({ watchesStatus: "loading" })
  try {
    const body = await json<{ watches: WatchRow[] }>("/api/watches")
    set({ watches: body.watches, watchesStatus: "ready" })
  } catch {
    set({ watchesStatus: "error" })
  }
}

export async function deleteWatch(id: string) {
  await json(`/api/watches/${encodeURIComponent(id)}`, { method: "DELETE" })
  set((s) => ({ watches: s.watches.filter((w) => w.id !== id) }))
}

// A person's click saves a watch the assistant proposed.
export async function confirmWatch(proposalId: string) {
  const saved = await json<WatchRow>("/api/watches/confirm", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ proposal_id: proposalId, by: get().name.trim() || null }),
  })
  void loadWatches()
  return saved
}

// -- words -----------------------------------------------------------------------------------------

// "2026-11-25T17:46" (Nicosia wall time) as "25 Nov, 17:46", or "today, 17:46".
export function when(wall: string | null | undefined, now = Date.now()) {
  if (!wall) return ""
  const [date, time] = wall.split("T")
  const today = new Date(now).toLocaleDateString("sv-SE", { timeZone: "Asia/Nicosia" })
  const yesterday = new Date(now - 864e5).toLocaleDateString("sv-SE", { timeZone: "Asia/Nicosia" })
  const hm = (time ?? "").slice(0, 5)
  if (date === today) return `today, ${hm}`
  if (date === yesterday) return `yesterday, ${hm}`
  const d = new Date(`${date}T12:00:00Z`)
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })}, ${hm}`
}

// How long ago a moment in ms was, in a few words.
export function since(ms: number, now = Date.now()) {
  const m = Math.max(0, Math.round((now - ms) / 60e3))
  if (m < 60) return `${m} min`
  const h = Math.round(m / 60)
  if (h < 48) return `${h} h`
  return `${Math.round(h / 24)} days`
}

export function actionWords(a: { action: string; by: string | null; until?: string | null }) {
  const verb: Record<string, string> = {
    acknowledged: "Acknowledged", snoozed: "Snoozed", unsnoozed: "Woken", resolved: "Resolved",
    reopened: "Reopened", merged: "Merged", opened: "Opened", escalated: "Escalated",
  }
  const v = verb[a.action] ?? a.action.charAt(0).toUpperCase() + a.action.slice(1)
  const until = a.until ? ` until ${when(a.until)}` : ""
  return `${v}${until}${a.by ? ` by ${a.by}` : ""}`
}

export function countsWords(c: Counts | null) {
  if (!c) return null
  const open = c.unresolved - c.snoozed
  if (!c.unresolved) return "Nothing needs attention"
  if (!open) return `${c.snoozed} snoozed, none open`
  const parts = [c.high ? `${c.high} high` : "", c.medium ? `${c.medium} medium` : "", c.low ? `${c.low} low` : ""].filter(Boolean)
  return `${open} open${parts.length ? `: ${parts.join(", ")}` : ""}${c.snoozed ? `, ${c.snoozed} snoozed` : ""}`
}

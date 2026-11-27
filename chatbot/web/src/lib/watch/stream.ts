/* The watch's live stream (/api/watch/stream, server-sent events) while the page is open: the
   counts stay current, the inbox and the open incident follow what changed, and a watch that
   comes true (or a new high-severity incident) is raised as a toast, announced politely and
   reachable from the keyboard (Alt T). With the page in the background, and the reader's
   permission, it is a browser notification too. */
import { toast } from "sonner"
import { create } from "zustand"

import { inspectId } from "@/lib/inspector"
import { useIncidentLayer } from "@/lib/watch/map"
import {
  loadDetail, loadIncidents, loadWatches, useWatch, type Counts, type IncidentRow,
} from "@/lib/watch/store"

// -- browser notifications -------------------------------------------------------------------------

const NOTIFY_KEY = "twin.watch.notify"
const canNotify = typeof window !== "undefined" && "Notification" in window

function readNotify() {
  try { return canNotify && localStorage.getItem(NOTIFY_KEY) === "1" && Notification.permission === "granted" } catch { return false }
}

export const useNotify = create<{ on: boolean; supported: boolean; denied: boolean }>(() => ({
  on: readNotify(), supported: canNotify, denied: canNotify && Notification.permission === "denied",
}))

// Asked only from a click (browsers ignore it otherwise).
export async function setNotify(on: boolean) {
  if (!canNotify) return
  if (on && Notification.permission !== "granted") {
    const p = await Notification.requestPermission().catch(() => "denied" as NotificationPermission)
    useNotify.setState({ denied: p === "denied" })
    if (p !== "granted") on = false
  }
  useNotify.setState({ on })
  try { localStorage.setItem(NOTIFY_KEY, on ? "1" : "0") } catch { /* private mode */ }
}

function notifyBrowser(title: string, body: string, open: () => void) {
  if (!useNotify.getState().on || !document.hidden || Notification.permission !== "granted") return
  try {
    const n = new Notification(title, { body, tag: `${title}:${body}` })
    n.onclick = () => { window.focus(); open(); n.close() }
  } catch { /* some browsers only notify from a service worker */ }
}

// -- what the stream says --------------------------------------------------------------------------

type WatchFire = {
  type: "watch"; watch: { id: string; label: string }; message: string; at: string
  entity: { id: string; kind: string; name: string; lat: number | null; lon: number | null }
}
type StreamEvent =
  | { type: "hello"; counts: Counts }
  | { type: "incident"; change: string; incident: IncidentRow; counts: Counts }
  | { type: "incidents"; counts: Counts }
  | WatchFire
  | { type: "watches" }

let reloadTimer: number | undefined

// The inbox (and its marks on the map) read again shortly after a change, once for a burst.
function reloadSoon(changed?: string) {
  window.clearTimeout(reloadTimer)
  reloadTimer = window.setTimeout(() => {
    const s = useWatch.getState()
    if (s.rowsStatus !== "idle" || useIncidentLayer.getState().on) void loadIncidents()
    if (s.selected && (!changed || changed === s.selected)) void loadDetail(s.selected, true)
  }, 600)
}

// New high-severity incidents, gathered for a moment so a burst is one toast.
let fresh: IncidentRow[] = []
let freshTimer: number | undefined

function raiseFresh() {
  const xs = fresh
  fresh = []
  if (!xs.length) return
  const open = (id?: string) => inspectId(id ?? xs[0].id, { fly: true })
  if (xs.length === 1) {
    const x = xs[0]
    toast.warning(`${x.id}: ${x.title}`, {
      description: `High severity, ${x.place}. Since ${x.start.slice(11, 16)}`,
      action: { label: "Open", onClick: () => open() }, duration: 20_000,
    })
    notifyBrowser(`New incident ${x.id}`, x.title, () => open())
  } else {
    toast.warning(`${xs.length} new high-severity incidents`, {
      description: xs.slice(0, 3).map((x) => x.title).join("; "),
      action: { label: "Open the inbox", onClick: () => open() }, duration: 20_000,
    })
    notifyBrowser(`${xs.length} new incidents`, xs.map((x) => x.title).join("; "), () => open())
  }
}

function onFire(ev: WatchFire) {
  const e = ev.entity
  const show = () => inspectId(e.id, { fly: true, focus: true, at: e.lon !== null && e.lat !== null ? [e.lon, e.lat] : undefined })
  toast(ev.message, {
    description: `Your watch: ${ev.watch.label}`,
    action: { label: "Show", onClick: show },
    duration: 30_000,
  })
  notifyBrowser("The twin's watch", ev.message, show)
}

function handle(ev: StreamEvent) {
  switch (ev.type) {
    case "hello":
      useWatch.setState({ counts: ev.counts })
      reloadSoon()
      break
    case "incident":
      useWatch.setState({ counts: ev.counts })
      reloadSoon(ev.incident.id)
      if ((ev.change === "new" || ev.change === "escalated" || ev.change === "reopened")
        && ev.incident.severity === "high" && ev.incident.status !== "snoozed") {
        fresh.push(ev.incident)
        window.clearTimeout(freshTimer)
        freshTimer = window.setTimeout(raiseFresh, 1500)
      }
      break
    case "incidents":
      useWatch.setState({ counts: ev.counts })
      reloadSoon()
      break
    case "watch":
      onFire(ev)
      void loadWatches()
      break
    case "watches":
      void loadWatches()
      break
  }
}

let source: EventSource | null = null

export function startWatchStream() {
  if (source || typeof EventSource === "undefined") return
  source = new EventSource("/api/watch/stream")
  source.onopen = () => useWatch.setState({ stream: "live" })
  // The browser tries again by itself (the server asks for 5 s).
  source.onerror = () => useWatch.setState({ stream: source?.readyState === EventSource.CLOSED ? "down" : "connecting" })
  source.onmessage = (m) => {
    try { handle(JSON.parse(m.data) as StreamEvent) } catch { /* a keep-alive or a malformed line */ }
  }
}

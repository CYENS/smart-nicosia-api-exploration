/* The twin's watch plugs into the page here: the incident inbox as a workspace (the Layers sheet,
   Ctrl K and Alt 4), an Incidents row under the Layers sheet's Alerts, incident ids opening in
   the inbox (chips in answers, map clicks, search, links), the inspector's Incidents section,
   incidents as marks on the city clock's track, the card that saves a proposed watch, and the
   live stream that raises toasts. */
import { MapPinnedIcon, SirenIcon } from "lucide-react"

import "@/components/watch/entity-section"
import { IncidentInbox } from "@/components/watch/inbox"
import { SeverityLegend } from "@/components/watch/parts"
import { WatchCard } from "@/components/watch/watch-card"
import { Button } from "@/components/ui/button"
import { fromWall, useClock } from "@/lib/clock"
import { closeLayers } from "@/lib/layers-sheet"
import { registerInspectRoute } from "@/lib/registries/inspect-routes"
import { registerSystemLayer } from "@/lib/registries/layers"
import { matches, registerSearchProvider } from "@/lib/registries/search"
import { setTrackMarks } from "@/lib/registries/track-marks"
import { registerTurnCard } from "@/lib/registries/turn-cards"
import { openWorkspace, registerWorkspace, useWorkspace } from "@/lib/registries/workspaces"
import { rememberInboxOpener, wantInboxFocus } from "@/lib/watch/focus"
import { setIncidentsLayer, startIncidentMap, useIncidentLayer } from "@/lib/watch/map"
import {
  INCIDENT_ID, SEVERITY_COLOR, countsWords, loadIncidents, loadWatches, selectIncident, setFilter, useWatch, type Severity,
} from "@/lib/watch/store"
import { startWatchStream } from "@/lib/watch/stream"

// -- the inbox workspace ---------------------------------------------------------------------------

let layerWasOn = false

registerWorkspace({
  id: "incidents", label: "Incidents", description: "Incidents: what the twin noticed is wrong, to see to",
  icon: SirenIcon, order: 30, key: "4", Side: IncidentInbox,
  onEnter: () => {
    rememberInboxOpener()
    wantInboxFocus("title")
    layerWasOn = useIncidentLayer.getState().on
    setIncidentsLayer(true)
    void loadIncidents()
    void loadWatches()
  },
  onLeave: () => {
    selectIncident(null)
    // The layer, if it stays on, shows what its Layers row counts: the incidents to see to.
    const { status, severity } = useWatch.getState()
    if (status !== "unresolved" || severity) setFilter({ status: "unresolved", severity: "" })
    if (!layerWasOn) setIncidentsLayer(false)
  },
})

// An incident (INC-12) opens in the inbox: from a chip in an answer, a map click, Ctrl K or a link.
registerInspectRoute({
  id: "incidents",
  match: (id) => INCIDENT_ID.test(id),
  open: (id, opts) => {
    // By keyboard (or from inside the inbox), the keyboard goes to the incident's heading; a
    // click on the map leaves it where it was.
    const inside = !!document.activeElement?.closest("[data-workspace]")
    if (useWorkspace.getState().active !== "incidents") openWorkspace("incidents")
    wantInboxFocus(opts.focus || inside ? "detail" : null)
    selectIncident(id)
  },
  current: () => (useWorkspace.getState().active === "incidents" ? useWatch.getState().selected : null),
  subscribe: (fn) => {
    const a = useWatch.subscribe((s, prev) => { if (s.selected !== prev.selected) fn() })
    const b = useWorkspace.subscribe((s, prev) => { if (s.active !== prev.active) fn() })
    return () => { a(); b() }
  },
})

// -- the Layers sheet ------------------------------------------------------------------------------

registerSystemLayer({
  id: "incidents", group: "alerts", order: 10, label: "Incidents",
  useOn: () => useIncidentLayer((s) => s.on),
  setOn: setIncidentsLayer,
  Summary: function Summary() {
    const counts = useWatch((s) => s.counts)
    const stream = useWatch((s) => s.stream)
    // The inbox is the incidents as they stand now; at a past moment the row says so, and the
    // clock's track marks where each one began.
    const past = useClock((s) => s.at !== null)
    const words = countsWords(counts)
    if (!words) return <>{stream === "down" ? "The twin's watch isn't answering" : "What the twin noticed is wrong, by place and time"}</>
    return <>{past ? `Now, not then: ${words.charAt(0).toLowerCase()}${words.slice(1)}. The track marks when each began` : words}</>
  },
  Legend: function Legend() {
    return (
      <div className="flex flex-col items-start gap-1.5">
        <SeverityLegend />
        <Button variant="link" size="sm" className="h-auto px-0 text-[12.5px]" onClick={() => { closeLayers(); openWorkspace("incidents") }}>
          Open the incident inbox
        </Button>
      </div>
    )
  },
})

// -- Ctrl K ----------------------------------------------------------------------------------------

registerSearchProvider({
  id: "watch-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    const on = useIncidentLayer.getState().on
    const items = [
      { key: "cmd:incidents-layer", icon: MapPinnedIcon, group: "Commands", title: on ? "Hide the incidents on the map" : "Show the incidents on the map",
        detail: countsWords(useWatch.getState().counts) ?? "Where the twin found something wrong", run: () => setIncidentsLayer(!on),
        words: "incidents alerts problems faults outages map" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

// -- the city clock's track ------------------------------------------------------------------------

type Mark = { id: string; t: string; severity: Severity; title: string; status: string }

async function loadMarks() {
  try {
    const res = await fetch("/api/incidents/marks?days=31")
    if (!res.ok) return
    const { marks } = (await res.json()) as { marks: Mark[] }
    setTrackMarks("incidents", marks.map((m) => ({
      t: fromWall(m.t), id: m.id, color: SEVERITY_COLOR[m.severity] ?? SEVERITY_COLOR.low, label: `${m.id} began: ${m.title}`,
    })))
  } catch { /* the track goes without them until the next change */ }
}

let marksTimer: number | undefined
useWatch.subscribe((s, prev) => {
  if (s.counts === prev.counts) return
  window.clearTimeout(marksTimer)
  marksTimer = window.setTimeout(loadMarks, 1500)
})

// -- the conversation ------------------------------------------------------------------------------

registerTurnCard("watch_confirm", WatchCard)

// -- start -----------------------------------------------------------------------------------------

startIncidentMap()
startWatchStream()
void loadMarks()


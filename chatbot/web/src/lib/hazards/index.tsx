/* Natural hazards plug into the page here: the Natural hazards row under the Layers sheet's
   Alerts (earthquakes, weather warnings and fire danger at the city clock's moment), marks on the
   clock's track while it is on, a line on the Outside card, the extent of a hazard incident while
   it is open in the inbox, a command in Ctrl K, and how the three kinds look in the inspector.
   The incidents themselves, their toasts and their inbox are the watch's (lib/watch). */
import { ActivityIcon, CloudAlertIcon, FlameIcon, MapPinnedIcon } from "lucide-react"
import { create } from "zustand"

import { HazardsLegend, HazardsOutside, HazardsSummary, ISLAND } from "@/components/hazards/parts"
import { fromWall } from "@/lib/clock"
import { KINDS } from "@/lib/entities"
import "@/lib/hazards/map"
import {
  HAZARD_FAMILIES, LEVEL_COLOR, FIRE_COLOR, loadHazards, startHazardsStore, useHazards, needHazards, type HazardsAt,
} from "@/lib/hazards/store"
import { KIND_ZOOM, flyToBounds, removeLayer, showLayer, useMapUi, type FC } from "@/lib/map/engine"
import { registerSystemLayer } from "@/lib/registries/layers"
import { matches, registerSearchProvider } from "@/lib/registries/search"
import { registerTopicSection } from "@/lib/registries/topics"
import { setTrackMarks } from "@/lib/registries/track-marks"
import { useWatch } from "@/lib/watch/store"

export const HAZARDS_REF = "sys:hazards"
export const HAZARD_INCIDENT_REF = "sys:hazard-incident"
const SOURCES = ["emsc", "meteoalarm", "effis", "geoboundaries"]
const SEVERITY_COLOR: Record<string, string> = { high: "#ff5a4e", medium: "#f5a524", low: "#8798b0" }

KINDS.earthquake = { label: "Earthquake", icon: ActivityIcon, chip: "danger" }
KINDS.warning = { label: "Weather warning", icon: CloudAlertIcon, chip: "danger" }
KINDS.fire_danger = { label: "Fire danger", icon: FlameIcon, chip: "danger" }
KIND_ZOOM.earthquake = 9 // the epicentre with the coast around it, not a street
KIND_ZOOM.warning = 10.5
KIND_ZOOM.fire_danger = 10.5

function fc(d: Pick<HazardsAt, "features" | "meta">): FC {
  return { type: "FeatureCollection", features: d.features, meta: { hazards: d.meta } }
}

function onMap(ref: string) {
  return useMapUi.getState().layers.some((l) => l.ref === ref)
}

// -- the Layers row ----------------------------------------------------------------------------------

export const useHazardLayer = create<{ on: boolean }>(() => ({ on: false }))
let release: (() => void) | null = null

function draw() {
  const d = useHazards.getState().data
  if (!d || !useHazardLayer.getState().on) return
  const count = d.features.filter((f) => f.properties.kind !== "felt").length
  void showLayer({
    ref: HAZARDS_REF, style: "points", title: d.live ? "Natural hazards" : `Natural hazards, ${d.at.replace("T", " ")}`,
    count, legend: d.legend, value_field: null, fit: false, labels: false, sources: SOURCES,
  }, fc(d))
}

type Mark = { id: string; t: string; kind: string; severity: string; level?: string; title: string }

async function loadMarks() {
  try {
    const res = await fetch("/api/hazards/marks?days=31")
    if (!res.ok) return
    const { marks } = (await res.json()) as { marks: Mark[] }
    if (!useHazardLayer.getState().on) return
    setTrackMarks("hazards", marks.map((m) => ({
      t: fromWall(m.t), id: m.id, label: m.title,
      color: m.kind === "warning" ? LEVEL_COLOR[m.level ?? "yellow"] : m.kind === "fire" ? FIRE_COLOR[m.level ?? "high"] : SEVERITY_COLOR[m.severity],
    })))
  } catch { /* the track goes without them until the next read */ }
}

export function setHazardsLayer(on: boolean) {
  if (on === useHazardLayer.getState().on) return
  useHazardLayer.setState({ on })
  if (on) {
    release = needHazards()
    draw()
    void loadMarks()
  } else {
    release?.()
    release = null
    if (onMap(HAZARDS_REF)) removeLayer(HAZARDS_REF)
    setTrackMarks("hazards", [])
  }
}

registerSystemLayer({
  id: "hazards", group: "alerts", order: 20, label: "Natural hazards",
  useOn: () => useHazardLayer((s) => s.on),
  setOn: setHazardsLayer,
  Summary: HazardsSummary,
  Legend: HazardsLegend,
})

// -- the Outside card ----------------------------------------------------------------------------------

registerTopicSection({ id: "hazards", topic: "outside", order: 5, Section: HazardsOutside })

// -- Ctrl K ----------------------------------------------------------------------------------------------

registerSearchProvider({
  id: "hazards-commands",
  heading: "Commands",
  order: 23,
  search: (q) => {
    const on = useHazardLayer.getState().on
    const items = [
      { key: "cmd:hazards-layer", icon: MapPinnedIcon, group: "Commands",
        title: on ? "Hide the natural hazards" : "Show earthquakes, weather warnings and fire danger",
        detail: "From EMSC with the Geological Survey Department, Meteoalarm and EFFIS",
        run: () => setHazardsLayer(!on), words: "earthquake quake seismic warning weather storm heat fire danger hazards" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

// -- an incident's extent while it is open in the inbox ------------------------------------------------

let shownFor: string | null = null
let framedFor: string | null = null

async function drawIncident(id: string, family: string, members: { id: string; lat: number | null; lon: number | null }[]) {
  try {
    const res = await fetch(`/api/hazards/items?ids=${encodeURIComponent(members.map((m) => m.id).join(","))}`)
    if (!res.ok || useWatch.getState().selected !== id) return
    const d = (await res.json()) as HazardsAt
    shownFor = id
    void showLayer({
      ref: HAZARD_INCIDENT_REF, style: "points", title: `${id}: where it reached`, count: d.features.length,
      legend: d.legend, value_field: null, fit: false, labels: false, sources: SOURCES,
    }, fc(d))
    if (framedFor === id) return
    framedFor = id
    if (family === "quake") {
      const pts = [...members.filter((m) => m.lat !== null && m.lon !== null).map((m) => [m.lon!, m.lat!]), [33.3615, 35.1725]]
      const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1])
      flyToBounds([Math.min(...lons) - 0.15, Math.min(...lats) - 0.15, Math.max(...lons) + 0.15, Math.max(...lats) + 0.15])
    } else flyToBounds(ISLAND)
  } catch { /* the inbox still shows it; the map goes without its extent */ }
}

useWatch.subscribe((s, prev) => {
  if (s.detail === prev.detail && s.selected === prev.selected) return
  const d = s.detail
  if (d && d.id === s.selected && HAZARD_FAMILIES.has(d.family)) {
    if (shownFor !== d.id || d !== prev.detail) void drawIncident(d.id, d.family, d.members)
    return
  }
  if (!s.selected || (d && d.id === s.selected)) {
    shownFor = null
    framedFor = null
    if (onMap(HAZARD_INCIDENT_REF)) removeLayer(HAZARD_INCIDENT_REF)
  }
})

// -- following the data and the map ----------------------------------------------------------------------

useHazards.subscribe((s, prev) => {
  if (s.data === prev.data) return
  draw()
  if (useHazardLayer.getState().on) void loadMarks()
})

// A new chat clears the map: the switch is the reader's own, so the layer comes back. Taken off on
// its own while other layers stay, the switch goes off.
useMapUi.subscribe((s, prev) => {
  if (s.layers === prev.layers || !useHazardLayer.getState().on) return
  const gone = prev.layers.some((l) => l.ref === HAZARDS_REF) && !s.layers.some((l) => l.ref === HAZARDS_REF)
  if (!gone) return
  if (s.layers.length === 0) draw()
  else setHazardsLayer(false)
})

startHazardsStore()
export { loadHazards }

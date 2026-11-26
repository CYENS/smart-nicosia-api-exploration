/* Incidents on the map. The inbox's incidents as marks at their places, coloured by severity
   (the Layers sheet's Incidents switch, and on while the inbox is open), and the devices of the
   incident open in the inbox. They are ordinary point layers (lib/map/engine.ts): a click on an
   incident opens it in the inbox (an INC id routes there), a click on a device opens the
   inspector, and hovering a row in the inbox rings its mark. */
import { create } from "zustand"

import { flyToBounds, flyToThing, getMap, removeLayer, setSelection, showLayer, useMapUi, type FC } from "@/lib/map/engine"
import {
  SEVERITY_COLOR, SEVERITY_WORD, STATUS_WORD, loadIncidents, useWatch, when, type IncidentDetail, type IncidentRow,
} from "@/lib/watch/store"

export const INCIDENTS_REF = "sys:incidents"
export const INCIDENT_REF = "sys:incident"
const SOURCES = ["twin_memory", "smart_nicosia"]
const LEGEND = { "high severity": SEVERITY_COLOR.high, "medium severity": SEVERITY_COLOR.medium, "low severity": SEVERITY_COLOR.low }

export const useIncidentLayer = create<{ on: boolean }>(() => ({ on: false }))

function pt(lon: number, lat: number, properties: Record<string, unknown>) {
  return { type: "Feature" as const, geometry: { type: "Point", coordinates: [lon, lat] }, properties }
}

export function incidentsFC(rows: IncidentRow[]): FC {
  const features = rows.filter((r) => r.lat !== null && r.lon !== null).map((r) => pt(r.lon!, r.lat!, {
    id: r.id, label: r.title, color: SEVERITY_COLOR[r.severity], state: `${r.severity} severity`, incident: r.id,
    popup: `${r.id}: ${r.title}\n${SEVERITY_WORD[r.severity]} severity, ${STATUS_WORD[r.status].toLowerCase()}\nSince ${when(r.start)}, ${r.devices} device${r.devices === 1 ? "" : "s"}`,
  }))
  return { type: "FeatureCollection", features }
}

export function devicesFC(d: IncidentDetail): FC {
  const features = d.members.filter((m) => m.lat !== null && m.lon !== null).map((m) => pt(m.lon!, m.lat!, {
    id: m.id, label: m.label, color: SEVERITY_COLOR[m.severity], state: m.active ? "still happening" : "ended", incident: d.id,
    popup: `${m.label}\n${m.what}\nPart of ${d.id}: ${d.title}`,
  }))
  return { type: "FeatureCollection", features }
}

function onMap(ref: string) {
  return useMapUi.getState().layers.some((l) => l.ref === ref)
}

function drawIncidents() {
  const rows = useWatch.getState().rows
  void showLayer({
    ref: INCIDENTS_REF, style: "points", title: "Incidents", count: rows.length, legend: LEGEND,
    value_field: null, fit: false, labels: false, sources: SOURCES,
  }, incidentsFC(rows))
}

export function setIncidentsLayer(on: boolean) {
  if (on === useIncidentLayer.getState().on) return
  useIncidentLayer.setState({ on })
  if (on) {
    if (useWatch.getState().rowsStatus === "ready") drawIncidents()
    else void loadIncidents()
  } else if (onMap(INCIDENTS_REF)) removeLayer(INCIDENTS_REF)
}

// The incident open in the inbox: its devices on the map, a ring at its place, and the camera
// framing them (once, when it opens).
let framedFor: string | null = null

function drawDetail(d: IncidentDetail | null, fly: boolean) {
  if (!d) {
    if (onMap(INCIDENT_REF)) removeLayer(INCIDENT_REF)
    framedFor = null
    return
  }
  const fc = devicesFC(d)
  void showLayer({
    ref: INCIDENT_REF, style: "points", title: `${d.id}: its devices`, count: fc.features.length, legend: LEGEND,
    value_field: null, fit: false, labels: false, sources: SOURCES,
  }, fc)
  if (d.lat !== null && d.lon !== null) setSelection({ center: [d.lon, d.lat] })
  if (!fly || framedFor === d.id) return
  framedFor = d.id
  frame(d)
}

// The incident's devices in view, or its place when it has one device.
export function frame(d: IncidentDetail) {
  const pts = d.members.filter((m) => m.lat !== null && m.lon !== null).map((m) => [m.lon!, m.lat!] as [number, number])
  if (d.lat !== null && d.lon !== null) pts.push([d.lon, d.lat])
  if (!pts.length || !getMap()) return
  if (pts.length === 1) { flyToThing(pts[0]); return }
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1])
  const pad = 0.0012
  flyToBounds([Math.min(...lons) - pad, Math.min(...lats) - pad, Math.max(...lons) + pad, Math.max(...lats) + pad])
}

let started = false

export function startIncidentMap() {
  if (started) return
  started = true
  // The rows changed (a filter, the live stream): redraw while the layer is on.
  useWatch.subscribe((s, prev) => {
    if (s.rows !== prev.rows && useIncidentLayer.getState().on) drawIncidents()
    if (s.detail !== prev.detail || s.selected !== prev.selected) {
      if (!s.selected) { drawDetail(null, false); if (prev.selected) setSelection(null) }
      else if (s.detail && s.detail.id === s.selected) drawDetail(s.detail, s.detail.id !== prev.detail?.id)
    }
  })
  // A new chat clears the map: the switch is the reader's own, so the layer comes back. Taken
  // off on its own (the assistant removing it) while other layers stay, the switch goes off.
  useMapUi.subscribe((s, prev) => {
    if (s.layers === prev.layers) return
    const gone = (ref: string) => prev.layers.some((l) => l.ref === ref) && !s.layers.some((l) => l.ref === ref)
    if (useIncidentLayer.getState().on && gone(INCIDENTS_REF)) {
      if (s.layers.length === 0) drawIncidents()
      else useIncidentLayer.setState({ on: false })
    }
    if (gone(INCIDENT_REF) && s.layers.length === 0) {
      const d = useWatch.getState().detail
      if (d && d.id === useWatch.getState().selected) drawDetail(d, false)
    }
  })
}

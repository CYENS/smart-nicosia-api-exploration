/* Map layers for Living Sky (aircraft over Nicosia with gliding heading glyphs, honouring
   prefers-reduced-motion) and CyNAP ITS sites (13 PWD CCTV cameras and 39 loop detectors). */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"

import { useClock } from "@/lib/clock"
import { useInspector } from "@/lib/inspector"
import {
  firstLabelLayer, getMap, onStyleReady, registerLayerRenderer, setSelection, themedPaint,
  type LayerData,
} from "@/lib/map/engine"
import { setTrackMarks } from "@/lib/registries/track-marks"
import { registerClickHandler } from "@/lib/registries/map-clicks"
import { registerActiveSources } from "@/lib/sources"
import {
  fetchAircraft, fetchISS, fetchITS, setAircraftState, setIssState, setItsState, useSkyLayers,
  type AircraftBody, type AircraftItem, type ITSBody,
} from "@/lib/sky/store"

const AC_SRC = "sky::aircraft"
const ITS_SRC = "sky::its"
const AC_POLL_MS = 30_000
const ISS_POLL_MS = 5 * 60_000
const FRAME_MS = 100
const EASE_S = 0.8
const MAX_AHEAD_S = 25

const AC_COLOR = "#38bdf8"
const AC_GROUND = "#8798b0"
const ISS_MARK_COLOR = "#38bdf8"
const NIGHT = "#0e1828"
const FONT_BOLD = ["Noto Sans Bold"]
const FONT_REGULAR = ["Noto Sans Regular"]

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches

// -- Aircraft icons & gliding interpolation -------------------------------------------------------

function drawPlane(color: string) {
  const c = document.createElement("canvas")
  c.width = 40
  c.height = 40
  const g = c.getContext("2d")!
  // Crisp silhouette pointing North (0 deg), rotated on map by aircraft track.
  g.beginPath()
  g.moveTo(20, 4)
  g.lineTo(22.5, 13)
  g.lineTo(35, 22)
  g.lineTo(35, 25)
  g.lineTo(22.5, 21)
  g.lineTo(22, 30)
  g.lineTo(27, 34)
  g.lineTo(27, 36.5)
  g.lineTo(20, 34.5)
  g.lineTo(13, 36.5)
  g.lineTo(13, 34)
  g.lineTo(18, 30)
  g.lineTo(17.5, 21)
  g.lineTo(5, 25)
  g.lineTo(5, 22)
  g.lineTo(17.5, 13)
  g.closePath()
  g.fillStyle = color
  g.fill()
  g.lineJoin = "round"
  g.lineWidth = 1.8
  g.strokeStyle = NIGHT
  g.stroke()
  return g.getImageData(0, 0, c.width, c.height)
}

function onMissingImage(map: MlMap, id: string) {
  if (map.hasImage(id)) return
  if (id === "sky-plane-air") map.addImage(id, drawPlane(AC_COLOR), { pixelRatio: 2 })
  else if (id === "sky-plane-ground") map.addImage(id, drawPlane(AC_GROUND), { pixelRatio: 2 })
}

type AcTrack = { item: AircraftItem; pos: [number, number]; heading: number }
const acTracks = new Map<string, AcTrack>()
const answerAircraft = new Map<string, string | null>() // ref -> at | null
let acMoment: string | null = null
let acTimer: number | undefined
let acCtrl: AbortController | null = null
let lastFrame = 0
let lastFollow = 0

function acDemand() {
  const { aircraft } = useSkyLayers.getState()
  const open = useInspector.getState().entity
  return aircraft.on || answerAircraft.size > 0 || (open?.kind as string) === "aircraft"
}

function ingestAircraft(body: AircraftBody) {
  const seen = new Set<string>()
  for (const a of body.aircraft) {
    seen.add(a.id)
    const prev = acTracks.get(a.id)
    if (!prev || body.recorded) {
      acTracks.set(a.id, { item: a, pos: [a.lon, a.lat], heading: a.track_deg ?? 0 })
    } else {
      // Snap on large jumps (or reduced motion); otherwise glide smoothly.
      const jump = Math.hypot(a.lon - prev.pos[0], a.lat - prev.pos[1]) > 0.25 || reducedMotion()
      if (jump) prev.pos = [a.lon, a.lat]
      prev.item = a
      if (a.track_deg != null) prev.heading = a.track_deg
    }
  }
  for (const id of [...acTracks.keys()]) if (!seen.has(id)) acTracks.delete(id)

  const airborne = body.aircraft.filter((x) => !x.on_ground).length
  const onGround = body.aircraft.length - airborne
  setAircraftState({
    status: body.status,
    error: body.error,
    known: body.known,
    count: body.aircraft.length,
    airborne,
    onGround,
    coverageNote: body.coverage_note ?? null,
    fetchedAt: body.fetched_at,
    past: acMoment !== null,
  })
}

function stepAircraft(t: AcTrack, nowMs: number, dt: number) {
  const a = t.item
  if (acMoment !== null || reducedMotion() || a.on_ground || !a.speed_kmh || a.track_deg == null) {
    t.pos = [a.lon, a.lat]
    t.heading = a.track_deg ?? t.heading
    return
  }
  const ahead = Math.max(0, Math.min(MAX_AHEAD_S, (nowMs - a.reported * 1000) / 1000))
  const v = a.speed_kmh / 3.6
  const rad = (a.track_deg * Math.PI) / 180
  const mLat = 110_574
  const mLon = 111_320 * Math.cos((a.lat * Math.PI) / 180)
  const targetLon = a.lon + (Math.sin(rad) * v * ahead) / mLon
  const targetLat = a.lat + (Math.cos(rad) * v * ahead) / mLat
  const k = Math.min(1, dt / EASE_S)
  t.pos = [t.pos[0] + (targetLon - t.pos[0]) * k, t.pos[1] + (targetLat - t.pos[1]) * k]
  t.heading = a.track_deg
}

function acFeatures(): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = []
  for (const t of acTracks.values()) {
    const a = t.item
    const altPart = a.on_ground ? "ground" : a.altitude_ft != null ? `${a.altitude_ft.toLocaleString("en-GB")} ft` : ""
    const tag = altPart ? `${a.callsign}\n${altPart}` : a.callsign
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: t.pos },
      properties: {
        id: a.id,
        callsign: a.callsign,
        label: tag,
        heading: Math.round(t.heading),
        on_ground: a.on_ground,
        icon: a.on_ground ? "sky-plane-ground" : "sky-plane-air",
        color: a.on_ground ? AC_GROUND : AC_COLOR,
      },
    })
  }
  return { type: "FeatureCollection", features }
}

const AC_LAYERS = ["sky::ac-dot", "sky::ac-icon", "sky::ac-label"]
const bound = new Set<string>()
function bindCursor(map: MlMap, id: string) {
  if (bound.has(id)) return
  bound.add(id)
  map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", id, () => { map.getCanvas().style.cursor = "" })
}

function addAircraftLayers(map: MlMap, prefix = "sky::ac", src = AC_SRC, collect?: string[]) {
  if (!collect && map.getLayer("sky::ac-icon")) return
  for (const imgId of ["sky-plane-air", "sky-plane-ground"]) onMissingImage(map, imgId)
  const before = firstLabelLayer()
  const dotId = `${prefix}-dot`
  const iconId = `${prefix}-icon`
  const labelId = `${prefix}-label`
  map.addLayer({
    id: dotId, type: "circle", source: src, maxzoom: 9.5,
    paint: {
      "circle-color": ["coalesce", ["get", "color"], AC_COLOR],
      "circle-radius": 3.5,
      "circle-stroke-color": NIGHT,
      "circle-stroke-width": 1,
    },
  }, before)
  map.addLayer({
    id: iconId, type: "symbol", source: src, minzoom: 8.5,
    layout: {
      "icon-image": ["case", ["==", ["get", "on_ground"], true], "sky-plane-ground", "sky-plane-air"],
      "icon-rotate": ["coalesce", ["get", "heading"], 0],
      "icon-rotation-alignment": "map",
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
      "icon-size": ["interpolate", ["linear"], ["zoom"], 8.5, 0.55, 11, 0.78, 14, 0.95],
    },
  }, before)
  map.addLayer({
    id: labelId, type: "symbol", source: src, minzoom: 9.5,
    layout: {
      "text-field": ["coalesce", ["get", "label"], ["get", "callsign"]],
      "text-font": FONT_BOLD,
      "text-size": 11,
      "text-line-height": 1.1,
      "text-offset": [0, 1.35],
      "text-anchor": "top",
      "text-optional": true,
    },
    paint: {
      ...themedPaint(labelId, {
        "text-color": { night: "#e4ebf4", day: "#0f172a" },
        "text-halo-color": { night: NIGHT, day: "#ffffff" },
      }),
      "text-halo-width": 1.4,
    },
  }, before)
  for (const id of [dotId, iconId, labelId]) {
    collect?.push(id)
    bindCursor(map, id)
  }
}

function removeAircraftLayers(map: MlMap) {
  for (const id of [...AC_LAYERS].reverse()) if (map.getLayer(id)) map.removeLayer(id)
  if (map.getSource(AC_SRC)) map.removeSource(AC_SRC)
}

async function pollAircraft() {
  window.clearTimeout(acTimer)
  if (!acDemand()) return
  const map = getMap()
  if (!document.hidden && map) {
    acCtrl?.abort()
    const mine = (acCtrl = new AbortController())
    if (useSkyLayers.getState().aircraft.status === "idle") {
      setAircraftState({ status: "loading", past: acMoment !== null })
    }
    try {
      const body = await fetchAircraft(acMoment, mine.signal)
      ingestAircraft(body)
      const src = map.getSource(AC_SRC) as GeoJSONSource | undefined
      src?.setData(acFeatures())
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setAircraftState({
          status: acTracks.size ? "stale" : "unavailable",
          error: (e as Error).message,
          past: acMoment !== null,
        })
      }
    }
  }
  if (acMoment === null) acTimer = window.setTimeout(pollAircraft, AC_POLL_MS)
}

function syncAircraft() {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    if (acDemand()) {
      if (!map.getSource(AC_SRC)) {
        map.addSource(AC_SRC, { type: "geojson", data: acFeatures() })
        addAircraftLayers(map)
      }
      void pollAircraft()
    } else {
      removeAircraftLayers(map)
      window.clearTimeout(acTimer)
      acCtrl?.abort()
      acTracks.clear()
      setAircraftState({ status: "idle", count: 0, airborne: 0, onGround: 0, error: null })
    }
  })
}

export function setAircraftOn(on: boolean) {
  setAircraftState({ on })
  syncAircraft()
}

function frameAircraft(ts: number) {
  requestAnimationFrame(frameAircraft)
  const map = getMap()
  if (!map || !acTracks.size || ts - lastFrame < FRAME_MS) return
  if (reducedMotion() || acMoment !== null) return
  const dt = lastFrame ? Math.min(1, (ts - lastFrame) / 1000) : 0
  lastFrame = ts
  const nowMs = Date.now()
  for (const t of acTracks.values()) stepAircraft(t, nowMs, dt)
  const src = map.getSource(AC_SRC) as GeoJSONSource | undefined
  if (src) src.setData(acFeatures())
  if (ts - lastFollow >= 250) {
    lastFollow = ts
    const { open, entity } = useInspector.getState()
    if (open && (entity?.kind as string) === "aircraft") {
      const tr = acTracks.get(entity!.id)
      if (tr) setSelection({ center: tr.pos, geometry: entity!.geometry })
    }
  }
}

// -- CyNAP ITS infrastructure (13 PWD CCTV + 39 loop detectors) ---------------------------------

let itsData: ITSBody | null = null
let itsCtrl: AbortController | null = null

function itsDemand() {
  const open = useInspector.getState().entity?.kind as string | undefined
  return useSkyLayers.getState().its.on || open === "its_cctv" || open === "its_detector"
}

const ITS_LAYERS = ["sky::its-circle", "sky::its-label"]

function addItsLayers(map: MlMap, data: ITSBody) {
  const fc: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: data.features }
  const existing = map.getSource(ITS_SRC) as GeoJSONSource | undefined
  if (existing) {
    existing.setData(fc)
    return
  }
  const before = firstLabelLayer()
  map.addSource(ITS_SRC, { type: "geojson", data: fc })
  map.addLayer({
    id: "sky::its-circle",
    type: "circle",
    source: ITS_SRC,
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": [
        "interpolate", ["linear"], ["zoom"],
        10, ["case", ["==", ["get", "entity_kind"], "its_cctv"], 4.2, 3],
        13, ["case", ["==", ["get", "entity_kind"], "its_cctv"], 6.2, 4.6],
        16, ["case", ["==", ["get", "entity_kind"], "its_cctv"], 8, 6.2],
      ],
      "circle-stroke-width": ["case", ["==", ["get", "entity_kind"], "its_cctv"], 2, 1.4],
      ...themedPaint("sky::its-circle", {
        "circle-stroke-color": { night: NIGHT, day: "#ffffff" },
      }),
    },
  }, before)
  map.addLayer({
    id: "sky::its-label",
    type: "symbol",
    source: ITS_SRC,
    minzoom: 15,
    layout: {
      "text-field": ["get", "label"],
      "text-font": FONT_REGULAR,
      "text-size": 10.5,
      "text-offset": [0, 1.15],
      "text-anchor": "top",
      "text-optional": true,
    },
    paint: {
      ...themedPaint("sky::its-label", {
        "text-color": { night: "#cbd5e1", day: "#334155" },
        "text-halo-color": { night: NIGHT, day: "#ffffff" },
      }),
      "text-halo-width": 1.2,
    },
  }, before)
  for (const id of ITS_LAYERS) bindCursor(map, id)
}

function removeItsLayers(map: MlMap) {
  for (const id of [...ITS_LAYERS].reverse()) if (map.getLayer(id)) map.removeLayer(id)
  if (map.getSource(ITS_SRC)) map.removeSource(ITS_SRC)
}

async function loadItsData() {
  const map = getMap()
  if (!map || !itsDemand()) return
  if (itsData) {
    addItsLayers(map, itsData)
    setItsState({
      status: itsData.meta.stale ? "stale" : "live",
      error: null,
      counts: itsData.meta.counts,
      swarco: itsData.meta.swarco,
    })
    return
  }
  itsCtrl?.abort()
  const mine = (itsCtrl = new AbortController())
  setItsState({ status: "loading", error: null })
  try {
    const body = await fetchITS(mine.signal)
    itsData = body
    setItsState({
      status: body.meta.stale ? "stale" : "live",
      error: null,
      counts: body.meta.counts,
      swarco: body.meta.swarco,
    })
    if (itsDemand()) addItsLayers(map, body)
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      setItsState({ status: "unavailable", error: (e as Error).message })
    }
  }
}

function syncIts() {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    if (itsDemand()) {
      void loadItsData()
    } else {
      itsCtrl?.abort()
      removeItsLayers(map)
    }
  })
}

export function setItsOn(on: boolean) {
  setItsState({ on })
  syncIts()
}

// -- ISS passes & City Clock marks ----------------------------------------------------------------

let issTimer: number | undefined
let issCtrl: AbortController | null = null
let issMoment: string | null = null

export async function refreshIss(at: string | null = issMoment) {
  window.clearTimeout(issTimer)
  issCtrl?.abort()
  const mine = (issCtrl = new AbortController())
  if (useSkyLayers.getState().iss.status === "idle") setIssState({ status: "loading" })
  try {
    const body = await fetchISS(at, mine.signal)
    setIssState({
      status: body.status,
      error: body.error,
      tleEpoch: body.tle_epoch,
      position: body.position,
      nextPass: body.next_pass,
      upcoming: body.upcoming,
      recent: body.recent,
    })
    const all = [...body.recent, ...body.upcoming]
    const marks = all.map((p) => ({
      t: p.peak_ms,
      color: ISS_MARK_COLOR,
      id: `iss-${p.peak_ms}`,
      label: `ISS pass (${p.max_elevation_deg}° max, ${p.start_dir} to ${p.end_dir})`,
    }))
    setTrackMarks("iss", marks)
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      setIssState({ status: "unavailable", error: (e as Error).message })
    }
  }
  if (at === null && !document.hidden) {
    issTimer = window.setTimeout(() => void refreshIss(null), ISS_POLL_MS)
  }
}

// -- Answer layers & map clicks & data sources ----------------------------------------------------

registerLayerRenderer({
  id: "sky-aircraft",
  claims: (d: LayerData) => d.meta?.sky?.kind === "aircraft",
  add: (ref, src, data, ids) => {
    answerAircraft.set(ref, data.meta?.sky?.at ?? null)
    addAircraftLayers(getMap(), `${ref}::sky-ac`, src, ids)
  },
  remove: (ref) => {
    answerAircraft.delete(ref)
  },
})

registerClickHandler({
  id: "sky",
  priority: 14,
  layers: (l) => l.startsWith("sky::") || l.includes("::sky-ac"),
  resolve: (f, at) => {
    const id = f.properties?.id
    if (!id) return null
    const center = f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at
    return { id: String(id), at: center }
  },
})

registerActiveSources(
  () => {
    const s = useSkyLayers.getState()
    const ids: string[] = []
    if (s.aircraft.on) {
      ids.push("adsb_lol")
      if (s.aircraft.past) ids.push("twin_memory")
    }
    if (s.its.on) ids.push("cynap_its", "swarco_traffic")
    return ids
  },
  (fn) => useSkyLayers.subscribe(fn),
)

// -- Lifecycle ------------------------------------------------------------------------------------

let started = false
export function startSkyMap() {
  if (started) return
  started = true
  requestAnimationFrame(frameAircraft)

  const waitMap = () => {
    const map = getMap()
    if (!map) {
      window.setTimeout(waitMap, 200)
      return
    }
    map.on("styleimagemissing", (e: { id: string }) => onMissingImage(map, e.id))
  }
  waitMap()

  void refreshIss(null)

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return
    if (acDemand() && acMoment === null) void pollAircraft()
    if (issMoment === null) void refreshIss(null)
  })

  let clockTimer: number | undefined
  useClock.subscribe((st, prev) => {
    if (st.at === prev.at) return
    window.clearTimeout(clockTimer)
    clockTimer = window.setTimeout(() => {
      const next = st.at === null ? null : String(st.at)
      acMoment = next
      issMoment = next
      if (acDemand()) void pollAircraft()
      void refreshIss(next)
    }, st.playing ? 500 : 150)
  })

  let lastKind: string | undefined
  useInspector.subscribe((st) => {
    const k = st.open ? (st.entity?.kind as string | undefined) : undefined
    if (k === lastKind) return
    const prev = lastKind
    lastKind = k
    if (k === "aircraft" || prev === "aircraft") syncAircraft()
    if (k === "its_cctv" || k === "its_detector" || prev === "its_cctv" || prev === "its_detector") syncIts()
  })
}

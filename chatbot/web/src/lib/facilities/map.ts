/* Park, charge and hire on the map: car parks, park and ride, EV chargers, Nextbike stations,
   Bolt scooter zones and bicycle racks.

   Each kind has its own badge: a rounded square in the kind's colour with its glyph (a P, P+R,
   a bolt, a bike, a scooter, a rack stand) in night ink, so the kinds tell apart by shape as well
   as colour. At city zoom the badges gather into clusters that count them; a click on a cluster
   zooms in. Park and ride sites are drawn as their areas, scooter zones too once the street is
   close enough to see them. What is live rides on the badges: the free bays the city's sensors
   see beside a car park, and the bikes at a Nextbike station (a station with none goes grey).
   Both follow the city clock: the bays from the twin's memory then, the bikes are now only. */
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"

import { useClock } from "@/lib/clock"
import {
  FAC_COLORS, FAC_KINDS, FAC_SOURCES, anyOn, fetchKind, fetchLive, setKindState, setLive, useFacilities,
  type FacFC, type FacKind,
} from "@/lib/facilities/store"
import { firstLabelLayer, getMap, onStyleReady, registerLayerRenderer, themedPaint, type LayerData } from "@/lib/map/engine"
import { registerClickHandler, registerEmptyClickHandler } from "@/lib/registries/map-clicks"
import { registerActiveSources } from "@/lib/sources"

/* eslint-disable @typescript-eslint/no-explicit-any */

const NIGHT = "#0e1828"
const DIM = "#5b6b80"
const FONT_BOLD = ["Noto Sans Bold"]
const FONT_REGULAR = ["Noto Sans Regular"]
const LIVE_POLL_MS = 120_000
const CLUSTER_MAX_ZOOM = 14
const AREA_ZOOM: Partial<Record<FacKind, number>> = { scooter_zone: 16.5 }
const src = (k: FacKind) => `facility/${k}`
const areaSrc = (k: FacKind) => `facility/${k}-area`

// -- the badges ---------------------------------------------------------------------------------

function canvas(w: number, h: number) {
  const c = document.createElement("canvas")
  c.width = w
  c.height = h
  return [c, c.getContext("2d")!] as const
}

// Glyphs on a 24-unit grid, stroked in night ink.
function glyph(g: CanvasRenderingContext2D, kind: FacKind) {
  g.lineWidth = 2.3
  g.lineCap = "round"
  g.lineJoin = "round"
  g.strokeStyle = NIGHT
  g.fillStyle = NIGHT
  const stroke = (d: string) => g.stroke(new Path2D(d))
  const ring = (x: number, y: number, r: number) => { g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.stroke() }
  switch (kind) {
    case "car_park":
      g.font = "bold 17px 'Sofia Sans Condensed', 'Noto Sans', sans-serif"
      g.textAlign = "center"; g.textBaseline = "middle"
      g.fillText("P", 12, 13)
      break
    case "park_and_ride":
      g.font = "bold 10.5px 'Sofia Sans Condensed', 'Noto Sans', sans-serif"
      g.textAlign = "center"; g.textBaseline = "middle"
      g.fillText("P+R", 12, 12.5)
      break
    case "charger":
      g.fill(new Path2D("M13.5 3 L6 13.5 h5.5 L10.5 21 L18 10.5 h-5.5 Z"))
      break
    case "bike_station":
      ring(6, 16, 3.6); ring(18, 16, 3.6)
      stroke("M6 16 L9.5 8.5 H15 L18 16 M9.5 8.5 L12.5 16 L15 8.5 M8 6.5 H11")
      break
    case "scooter_zone":
      ring(6, 17.5, 2.3); ring(18, 17.5, 2.3)
      stroke("M8.5 17.5 H14 L17 5.5 H19.5 M17 5.5 L18 15.2")
      break
    case "bike_rack":
      stroke("M7.5 20 V11.5 a4.5 4.5 0 0 1 9 0 V20 M4.5 20 H19.5")
      break
  }
}

function badge(kind: FacKind, fill: string, size: number) {
  const scale = 2
  const [c, g] = canvas(size * scale, size * scale)
  g.scale(scale, scale)
  g.beginPath()
  g.roundRect(1.25, 1.25, size - 2.5, size - 2.5, size * 0.28)
  g.fillStyle = fill
  g.fill()
  g.lineWidth = 2
  g.strokeStyle = NIGHT
  g.stroke()
  const inner = size - 8
  g.save()
  g.translate(4, 4)
  g.scale(inner / 24, inner / 24)
  glyph(g, kind)
  g.restore()
  return g.getImageData(0, 0, c.width, c.height)
}

// The same badge as a picture, for legends and rows beside the map.
const urls = new Map<string, string>()
export function badgeUrl(kind: FacKind, dim = false): string {
  const key = `${kind}${dim ? "-dim" : ""}`
  let url = urls.get(key)
  if (!url) {
    const img = badge(kind, dim ? DIM : FAC_COLORS[kind], 26)
    const [c, g] = canvas(img.width, img.height)
    g.putImageData(img, 0, 0)
    url = c.toDataURL()
    urls.set(key, url)
  }
  return url
}

function ensureImages(map: MlMap) {
  for (const k of FAC_KINDS) {
    const size = k === "bike_rack" ? 20 : 26
    if (!map.hasImage(`fac-${k}`)) map.addImage(`fac-${k}`, badge(k, FAC_COLORS[k], size), { pixelRatio: 2 })
    if (!map.hasImage(`fac-${k}-dim`)) map.addImage(`fac-${k}-dim`, badge(k, DIM, size), { pixelRatio: 2 })
  }
}

// -- each kind's layers ----------------------------------------------------------------------------

const data = new Map<FacKind, FacFC>()
const bound = new Set<string>()

function bindCursor(map: MlMap, id: string) {
  if (bound.has(id)) return
  bound.add(id)
  map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", id, () => { map.getCanvas().style.cursor = "" })
}

function points(fc: FacFC) {
  return { type: "FeatureCollection", features: fc.features.filter((f) => f.properties.part !== "area").map(withLive) }
}

function areas(fc: FacFC) {
  return { type: "FeatureCollection", features: fc.features.filter((f) => f.properties.part === "area") }
}

// What is live rides on the feature: a car park's free sensed bays, a station's bikes.
function withLive(f: any) {
  const p = f.properties
  const live = useFacilities.getState().live
  const out = { ...p, dim: false, live: "" }
  if (p.kind === "car_park" && live.bays[p.id]) {
    const b = live.bays[p.id]
    out.live = b.known ? `${b.free} free` : ""
  } else if (p.kind === "bike_station") {
    const s = live.bikes[p.id]
    if (s) {
      out.live = String(s.bikes)
      out.dim = s.bikes === 0 || !s.renting
    }
  } else if (p.kind === "charger") {
    out.dim = p.listed === "unavailable"
  }
  return { ...f, properties: out }
}

function layerIds(k: FacKind) {
  return [`facility/${k}-fill`, `facility/${k}-line`, `facility/${k}-cluster`, `facility/${k}-count`, `facility/${k}-icon`, `facility/${k}-name`]
}

function addKind(map: MlMap, k: FacKind, fc: FacFC) {
  ensureImages(map)
  const color = FAC_COLORS[k]
  if (!map.getSource(src(k))) {
    map.addSource(src(k), { type: "geojson", data: points(fc) as any, cluster: true, clusterMaxZoom: CLUSTER_MAX_ZOOM, clusterRadius: 42 })
  }
  const hasAreas = fc.features.some((f) => f.properties.part === "area")
  if (hasAreas && !map.getSource(areaSrc(k))) {
    map.addSource(areaSrc(k), { type: "geojson", data: areas(fc) as any })
    const before = firstLabelLayer()
    const minzoom = AREA_ZOOM[k] ?? 0
    map.addLayer({ id: `facility/${k}-fill`, type: "fill", source: areaSrc(k), minzoom,
                   paint: { "fill-color": color, "fill-opacity": k === "scooter_zone" ? 0.1 : 0.16 } }, before)
    map.addLayer({ id: `facility/${k}-line`, type: "line", source: areaSrc(k), minzoom,
                   paint: { "line-color": color, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1, 17, 2.2], "line-opacity": 0.9 } }, before)
    bindCursor(map, `facility/${k}-fill`)
  }
  const clustered: any = ["has", "point_count"]
  map.addLayer({
    id: `facility/${k}-cluster`, type: "circle", source: src(k), filter: clustered,
    paint: {
      "circle-color": color, "circle-opacity": 0.92,
      "circle-radius": ["step", ["get", "point_count"], 11, 10, 14, 40, 18],
      ...themedPaint(`facility/${k}-cluster`, { "circle-stroke-color": { night: NIGHT, day: "#ffffff" } }),
      "circle-stroke-width": 2,
    },
  })
  map.addLayer({
    id: `facility/${k}-count`, type: "symbol", source: src(k), filter: clustered,
    layout: { "text-field": ["get", "point_count_abbreviated"], "text-font": FONT_BOLD, "text-size": 12, "text-allow-overlap": true },
    paint: { "text-color": NIGHT },
  })
  const single: any = ["!", ["has", "point_count"]]
  // The badge, and beside it what is live (free bays at a car park, bikes at a station), in one
  // symbol: the count shows only with its badge, and gives way before the badge does.
  map.addLayer({
    id: `facility/${k}-icon`, type: "symbol", source: src(k), filter: single,
    layout: {
      "icon-image": ["case", ["get", "dim"], `fac-${k}-dim`, `fac-${k}`],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 11, 0.7, 14, 0.9, 17, 1.1],
      "icon-allow-overlap": ["step", ["zoom"], false, 15, true],
      "icon-padding": 1,
      "symbol-sort-key": ["case", ["get", "dim"], 2, 1],
      "text-field": ["step", ["zoom"], "", 13.5, ["get", "live"]],
      "text-font": FONT_BOLD, "text-size": 12, "text-anchor": "left", "text-offset": [1.15, 0],
      "text-optional": true, "text-padding": 1,
    },
    paint: {
      ...themedPaint(`facility/${k}-icon`, { "text-color": { night: "#e4ebf4", day: "#0f172a" }, "text-halo-color": { night: NIGHT, day: "#ffffff" } }),
      "text-halo-width": 1.6,
    },
  })
  map.addLayer({
    id: `facility/${k}-name`, type: "symbol", source: src(k), filter: ["all", single, ["get", "named"]], minzoom: 16.5,
    layout: { "text-field": ["get", "name"], "text-font": FONT_REGULAR, "text-size": 11, "text-anchor": "top",
              "text-offset": [0, 1.25], "text-optional": true, "text-max-width": 9 },
    paint: {
      ...themedPaint(`facility/${k}-name`, { "text-color": { night: "#8798b0", day: "#475569" }, "text-halo-color": { night: NIGHT, day: "#ffffff" } }),
      "text-halo-width": 1.2,
    },
  })
  for (const id of [`facility/${k}-icon`, `facility/${k}-cluster`]) bindCursor(map, id)
}

function removeKind(map: MlMap, k: FacKind) {
  for (const id of layerIds(k).reverse()) if (map.getLayer(id)) map.removeLayer(id)
  for (const id of [src(k), areaSrc(k)]) if (map.getSource(id)) map.removeSource(id)
}

const loading = new Map<FacKind, AbortController>()

function syncKind(k: FacKind) {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    const st = useFacilities.getState().kinds[k]
    if (!st.on) {
      loading.get(k)?.abort()
      loading.delete(k)
      removeKind(map, k)
      return
    }
    const have = data.get(k)
    if (have) {
      if (!map.getSource(src(k))) addKind(map, k, have)
      return
    }
    if (loading.has(k)) return
    const ctrl = new AbortController()
    loading.set(k, ctrl)
    setKindState(k, { status: "loading", error: null })
    fetchKind(k, ctrl.signal)
      .then((fc) => {
        data.set(k, fc)
        setKindState(k, { status: "ready", meta: fc.meta, error: null })
        if (useFacilities.getState().kinds[k].on && !map.getSource(src(k))) addKind(map, k, fc)
      })
      .catch((e) => {
        if ((e as Error).name !== "AbortError") setKindState(k, { status: "error", error: (e as Error).message })
      })
      .finally(() => loading.delete(k))
  })
}

export function setFacilityOn(k: FacKind, on: boolean) {
  setKindState(k, { on })
  syncKind(k)
  syncLive()
}

export function setFacilitiesOn(kinds: FacKind[], on: boolean) {
  for (const k of kinds) setFacilityOn(k, on)
}

// For the Layers sheet's summaries: read a kind's inventory without drawing it.
export function loadKindMeta(k: FacKind) {
  const st = useFacilities.getState().kinds[k]
  if (st.meta || st.status === "loading") return
  fetchKind(k).then((fc) => {
    data.set(k, fc)
    setKindState(k, { meta: fc.meta, status: useFacilities.getState().kinds[k].on ? "ready" : useFacilities.getState().kinds[k].status })
  }).catch(() => { /* the summary says the plain words instead */ })
}

// -- what is live ----------------------------------------------------------------------------------

const LIVE_KINDS: FacKind[] = ["car_park", "bike_station"]
let liveTimer: number | undefined
let liveCtrl: AbortController | null = null

function liveWanted(): FacKind[] {
  const k = useFacilities.getState().kinds
  return LIVE_KINDS.filter((x) => k[x].on || answerKinds().has(x))
}

function redraw() {
  const map = getMap()
  if (!map) return
  for (const k of FAC_KINDS) {
    const fc = data.get(k)
    const s = map.getSource(src(k)) as GeoJSONSource | undefined
    if (fc && s) s.setData(points(fc) as any)
  }
}

async function pollLive() {
  window.clearTimeout(liveTimer)
  const kinds = liveWanted()
  if (!kinds.length) { setLive({ status: "idle" }); return }
  const at = useClock.getState().at
  const moment = at === null ? null : String(at)
  if (!document.hidden) {
    liveCtrl?.abort()
    const mine = (liveCtrl = new AbortController())
    if (useFacilities.getState().live.status === "idle") setLive({ status: "loading" })
    try {
      const body = await fetchLive(kinds, moment, mine.signal)
      setLive({
        status: "ready", error: null, at: moment,
        bays: body.bays?.car_parks ?? {}, baysAsOf: body.bays?.as_of ?? null, baysError: body.bays?.error ?? null,
        bikes: body.bikes?.stations ?? {}, bikesStatus: body.bikes?.status ?? "idle",
        bikesError: body.bikes?.error ?? body.bikes?.note ?? null, bikesAsOf: body.bikes?.as_of ?? null,
        bikesHere: body.bikes?.in_nicosia ?? null,
      })
      redraw()
    } catch (e) {
      if ((e as Error).name !== "AbortError") setLive({ status: "error", error: (e as Error).message })
    }
  }
  if (moment === null) liveTimer = window.setTimeout(pollLive, LIVE_POLL_MS)
}

function syncLive() {
  if (liveWanted().length) void pollLive()
  else { window.clearTimeout(liveTimer); liveCtrl?.abort() }
}

// -- answers ---------------------------------------------------------------------------------------

const answers = new Map<string, FacKind[]>() // answer layer ref -> the kinds it shows

function answerKinds() {
  return new Set([...answers.values()].flat())
}

registerLayerRenderer({
  id: "facilities",
  claims: (d: LayerData) => !!d.meta?.facilities,
  add: (ref, source, d, ids) => {
    const map = getMap()
    ensureImages(map)
    answers.set(ref, d.meta.facilities.kinds ?? [])
    const isFac: any = ["has", "facility_kind"]
    const bay: any = ["==", ["get", "entity_kind"], "parking_sensor"]
    map.addLayer({
      id: `${ref}::fac-bay`, type: "circle", source, filter: bay,
      paint: {
        "circle-color": ["get", "color"], "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 3, 17, 6],
        ...themedPaint(`${ref}::fac-bay`, { "circle-stroke-color": { night: NIGHT, day: "#ffffff" } }), "circle-stroke-width": 1.4,
      },
    })
    map.addLayer({
      id: `${ref}::fac-origin`, type: "circle", source, filter: ["==", ["get", "id"], "origin"],
      paint: { "circle-color": "#e4ebf4", "circle-radius": 6, "circle-stroke-color": NIGHT, "circle-stroke-width": 2.5 },
    })
    map.addLayer({
      id: `${ref}::fac-icon`, type: "symbol", source, filter: isFac,
      layout: {
        "icon-image": ["concat", "fac-", ["get", "facility_kind"]], "icon-allow-overlap": true,
        "icon-size": ["interpolate", ["linear"], ["zoom"], 11, 0.8, 15, 1, 17, 1.15],
        "text-field": ["coalesce", ["to-string", ["get", "free_bays"]], ["to-string", ["get", "bikes"]], ""],
        "text-font": FONT_BOLD, "text-size": 12, "text-anchor": "left", "text-offset": [1.2, 0], "text-optional": true,
      },
      paint: {
        ...themedPaint(`${ref}::fac-icon`, { "text-color": { night: "#e4ebf4", day: "#0f172a" }, "text-halo-color": { night: NIGHT, day: "#ffffff" } }),
        "text-halo-width": 1.6,
      },
    })
    ids.push(`${ref}::fac-bay`, `${ref}::fac-origin`, `${ref}::fac-icon`)
    for (const id of ids) bindCursor(map, id)
    syncLive()
  },
  remove: (ref) => {
    answers.delete(ref)
    syncLive()
  },
})

// -- clicks ------------------------------------------------------------------------------------------

registerClickHandler({
  id: "facilities",
  priority: 12,
  layers: (l) => /^facility\/[a-z_]+-(icon|fill)$/.test(l),
  resolve: (f, at) => {
    const id = f.properties?.id
    if (!id) return null
    return { id: String(id), at: f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : at }
  },
})

// A click on a cluster zooms in to where it parts.
registerEmptyClickHandler({
  id: "facility-clusters",
  priority: 50,
  handle: (at, how) => {
    if (how !== "click") return false
    const map = getMap()
    if (!map) return false
    const layers = FAC_KINDS.map((k) => `facility/${k}-cluster`).filter((id) => map.getLayer(id))
    if (!layers.length) return false
    const p = map.project(at)
    const hit = map.queryRenderedFeatures([[p.x - 4, p.y - 4], [p.x + 4, p.y + 4]], { layers })[0]
    if (!hit) return false
    const kind = hit.layer.id.replace(/^facility\//, "").replace(/-cluster$/, "") as FacKind
    const s = map.getSource(src(kind)) as GeoJSONSource | undefined
    s?.getClusterExpansionZoom(hit.properties?.cluster_id).then((z) => {
      map.easeTo({ center: (hit.geometry as any).coordinates, zoom: Math.min(z + 0.3, 17), duration: 500 })
    }).catch(() => {})
    return true
  },
})

// -- start --------------------------------------------------------------------------------------------

let started = false
export function startFacilitiesMap() {
  if (started) return
  started = true
  const wait = () => {
    const map = getMap()
    if (!map) { window.setTimeout(wait, 200); return }
    map.on("styleimagemissing", (e: any) => { if (String(e.id).startsWith("fac-")) ensureImages(map) })
    // A new base map brings a new style: put the kinds that are on back.
    map.on("style.load", () => { for (const k of FAC_KINDS) if (useFacilities.getState().kinds[k].on) syncKind(k) })
  }
  wait()
  document.addEventListener("visibilitychange", () => { if (!document.hidden && liveWanted().length) void pollLive() })
  // The bays follow the city clock (from the twin's memory then); a moving clock is read once it rests.
  let clockTimer: number | undefined
  useClock.subscribe((st, prev) => {
    if (st.at === prev.at) return
    window.clearTimeout(clockTimer)
    clockTimer = window.setTimeout(() => syncLive(), st.playing ? 800 : 200)
  })
  registerActiveSources(
    () => {
      const k = useFacilities.getState().kinds
      const ids = FAC_KINDS.filter((x) => k[x].on).map((x) => FAC_SOURCES[x])
      if (k.car_park.on) ids.push("smart_nicosia")
      if (k.bike_station.on) ids.push("nextbike_live")
      return ids
    },
    (fn) => { useFacilities.subscribe((s, p) => { if (s.kinds !== p.kinds) fn() }) },
  )
}

export function anyFacilityOn() {
  return anyOn()
}

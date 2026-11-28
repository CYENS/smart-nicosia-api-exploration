/* Map rendering for Nicosia Urban Fabric layers & assistant fabric tool layers:
   - Summer morning ground surface temperature (Landsat ~10:30 local): raster canvas under labels
   - Modelled 2022 population (Kontur H3 res 8): extruded 3D hexagons + clickable fill
   - 15-minute access (standard vs step-free/gentle slope): hexagon fill + outline under labels
   - Street slopes (CyNAP 100 m gradients): circle points colored by gentle / 5-8% / >8%
   - Assistant layers (`heat_exposure`, `accessibility_15min`, `population_reach`) via registerLayerRenderer */
import type { FeatureCollection } from "geojson"
import type { CanvasSource, GeoJSONSource, LayerSpecification, Map as MlMap } from "maplibre-gl"

import { HEAT_COLORS, heatBandIndex, hexToRgb } from "@/lib/fabric/colors"
import {
  loadAccessHexes, loadGradients, loadHeatGrid, loadPeopleHexes, patchFabric, useFabric,
  type AccessMode, type SlopeFilter,
} from "@/lib/fabric/store"
import type { Basemap } from "@/lib/map/basemap"
import {
  firstLabelLayer, getMap, linkHover, onStyleReady, registerLayerRenderer, themedPaint,
  useMapUi, type LayerData,
} from "@/lib/map/engine"
import { registerClickHandler } from "@/lib/registries/map-clicks"

/* eslint-disable @typescript-eslint/no-explicit-any */

const HEAT_SRC = "fabric::heat-src"
const HEAT_LAYER = "fabric::heat"
const PEOPLE_SRC = "fabric::people-src"
const PEOPLE_FILL = "fabric::people-fill"
const PEOPLE_EXTRUDE = "fabric::people-extrude"
const PEOPLE_LINE = "fabric::people-line"
const ACCESS_SRC = "fabric::access-src"
const ACCESS_FILL = "fabric::access-fill"
const ACCESS_LINE = "fabric::access-line"
const SLOPES_SRC = "fabric::slopes-src"
const SLOPES_LAYER = "fabric::slopes-dots"

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches
const base = (): Basemap => useMapUi.getState().basemap
const fab = useFabric.getState

const heatCanvas = document.createElement("canvas")
const boundHover = new Set<string>()

function bindLayerHover(map: MlMap, layerId: string) {
  if (boundHover.has(layerId)) return
  boundHover.add(layerId)
  map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer" })
  map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; linkHover("id", null, "map") })
  map.on("mousemove", layerId, (e) => linkHover("id", e.features?.[0]?.properties?.id ?? null, "map"))
}

function drop(map: MlMap, layers: string[], sources: string[]) {
  for (const id of layers) if (map.getLayer(id)) map.removeLayer(id)
  for (const id of sources) if (map.getSource(id)) map.removeSource(id)
}

function place() {
  const map = getMap()
  if (!map) return
  const label = firstLabelLayer()
  if (!label) return
  for (const id of [HEAT_LAYER, ACCESS_FILL, ACCESS_LINE, PEOPLE_FILL, PEOPLE_LINE, PEOPLE_EXTRUDE, SLOPES_LAYER]) {
    if (map.getLayer(id)) map.moveLayer(id, label)
  }
}

function paintHeatCanvas(theme: Basemap) {
  const { payload, cells } = fab().heat
  if (!payload || !cells) return null
  const { nx, ny, scale, nodata } = payload
  heatCanvas.width = nx
  heatCanvas.height = ny
  const ctx = heatCanvas.getContext("2d")!
  const img = ctx.createImageData(nx, ny)
  const palette = HEAT_COLORS[theme].map(hexToRgb)
  const alpha = theme === "night" ? 185 : 155
  for (let i = 0; i < cells.length; i++) {
    const raw = cells[i]
    if (raw === nodata) continue
    const [r, g, b] = palette[heatBandIndex(raw * scale)]
    const o = 4 * i
    img.data[o] = r
    img.data[o + 1] = g
    img.data[o + 2] = b
    img.data[o + 3] = alpha
  }
  ctx.putImageData(img, 0, 0)
  const [w, s, e, n] = payload.bbox
  return [[w, n], [e, n], [e, s], [w, s]]
}

function syncHeat() {
  const map = getMap()
  if (!map) return
  const { on, payload, cells } = fab().heat
  if (!on || !payload || !cells) {
    drop(map, [HEAT_LAYER], [HEAT_SRC])
    return
  }
  const coords = paintHeatCanvas(base())
  if (!coords) return
  const src = map.getSource(HEAT_SRC) as CanvasSource | undefined
  if (src) {
    src.setCoordinates(coords as any)
    src.play()
    window.setTimeout(() => src.pause(), 160)
  } else {
    map.addSource(HEAT_SRC, { type: "canvas", canvas: heatCanvas, coordinates: coords as any, animate: false })
    const added = map.getSource(HEAT_SRC) as CanvasSource | undefined
    added?.play()
    window.setTimeout(() => added?.pause(), 160)
  }
  if (!map.getLayer(HEAT_LAYER)) {
    map.addLayer({
      id: HEAT_LAYER,
      type: "raster",
      source: HEAT_SRC,
      paint: { "raster-resampling": "linear", "raster-fade-duration": 0, "raster-opacity": 0.92 },
    } as LayerSpecification, firstLabelLayer())
  }
  place()
}

function syncPeople() {
  const map = getMap()
  if (!map) return
  const { on, data } = fab().people
  if (!on || !data) {
    drop(map, [PEOPLE_EXTRUDE, PEOPLE_LINE, PEOPLE_FILL], [PEOPLE_SRC])
    return
  }
  const src = map.getSource(PEOPLE_SRC) as GeoJSONSource | undefined
  if (src) src.setData(data as any)
  else map.addSource(PEOPLE_SRC, { type: "geojson", data: data as any })

  const theme = base()
  if (!map.getLayer(PEOPLE_FILL)) {
    map.addLayer({
      id: PEOPLE_FILL,
      type: "fill",
      source: PEOPLE_SRC,
      paint: {
        "fill-color": ["get", "color"],
        ...themedPaint(PEOPLE_FILL, { "fill-opacity": { night: 0.22, day: 0.16 } }),
      },
    }, firstLabelLayer())
    bindLayerHover(map, PEOPLE_FILL)
  }
  if (!map.getLayer(PEOPLE_LINE)) {
    map.addLayer({
      id: PEOPLE_LINE,
      type: "line",
      source: PEOPLE_SRC,
      paint: {
        "line-color": theme === "night" ? "#334155" : "#475569",
        "line-width": 0.7,
        "line-opacity": 0.45,
      },
    }, firstLabelLayer())
  }
  if (!map.getLayer(PEOPLE_EXTRUDE)) {
    map.addLayer({
      id: PEOPLE_EXTRUDE,
      type: "fill-extrusion",
      source: PEOPLE_SRC,
      paint: {
        "fill-extrusion-color": ["get", "color"],
        "fill-extrusion-height": ["*", ["min", ["coalesce", ["get", "population"], 0], 7500], 0.04],
        "fill-extrusion-base": 0,
        ...themedPaint(PEOPLE_EXTRUDE, { "fill-extrusion-opacity": { night: 0.78, day: 0.7 } }),
      },
    }, firstLabelLayer())
    bindLayerHover(map, PEOPLE_EXTRUDE)
  }
  place()
}

function syncAccess() {
  const map = getMap()
  if (!map) return
  const { on, mode, data } = fab().access
  const fc = data[mode]
  if (!on || !fc) {
    drop(map, [ACCESS_LINE, ACCESS_FILL], [ACCESS_SRC])
    return
  }
  const src = map.getSource(ACCESS_SRC) as GeoJSONSource | undefined
  if (src) src.setData(fc as any)
  else map.addSource(ACCESS_SRC, { type: "geojson", data: fc as any })

  if (!map.getLayer(ACCESS_FILL)) {
    map.addLayer({
      id: ACCESS_FILL,
      type: "fill",
      source: ACCESS_SRC,
      paint: {
        "fill-color": ["get", "color"],
        ...themedPaint(ACCESS_FILL, { "fill-opacity": { night: 0.34, day: 0.26 } }),
      },
    }, firstLabelLayer())
    bindLayerHover(map, ACCESS_FILL)
  }
  if (!map.getLayer(ACCESS_LINE)) {
    map.addLayer({
      id: ACCESS_LINE,
      type: "line",
      source: ACCESS_SRC,
      paint: {
        "line-color": ["get", "color"],
        "line-width": 0.9,
        "line-opacity": 0.55,
      },
    }, firstLabelLayer())
  }
  place()
}

function filteredSlopes(fc: FeatureCollection, filter: SlopeFilter): FeatureCollection {
  if (filter !== "steep") return fc
  return {
    ...fc,
    features: fc.features.filter((f) => Number(f.properties?.slope_pct ?? 0) > 5),
  }
}

function syncSlopes() {
  const map = getMap()
  if (!map) return
  const { on, filter, data } = fab().slopes
  if (!on || !data) {
    drop(map, [SLOPES_LAYER], [SLOPES_SRC])
    return
  }
  const fc = filteredSlopes(data, filter)
  const src = map.getSource(SLOPES_SRC) as GeoJSONSource | undefined
  if (src) src.setData(fc as any)
  else map.addSource(SLOPES_SRC, { type: "geojson", data: fc as any })

  const theme = base()
  if (!map.getLayer(SLOPES_LAYER)) {
    map.addLayer({
      id: SLOPES_LAYER,
      type: "circle",
      source: SLOPES_SRC,
      paint: {
        "circle-radius": [
          "interpolate", ["linear"], ["zoom"],
          11, ["case", [">", ["coalesce", ["get", "slope_pct"], 0], 5], 3.5, 2],
          15, ["case", [">", ["coalesce", ["get", "slope_pct"], 0], 8], 7.5, [">", ["coalesce", ["get", "slope_pct"], 0], 5], 6, 4],
        ],
        "circle-color": ["get", "color"],
        "circle-opacity": ["case", [">", ["coalesce", ["get", "slope_pct"], 0], 5], 0.94, 0.72],
        "circle-stroke-width": 1.25,
        "circle-stroke-color": theme === "night" ? "#0e1828" : "#ffffff",
      },
    }, firstLabelLayer())
    bindLayerHover(map, SLOPES_LAYER)
  } else {
    map.setPaintProperty(SLOPES_LAYER, "circle-stroke-color", theme === "night" ? "#0e1828" : "#ffffff")
  }
  place()
}

// -- public controls for the Layers sheet ---------------------------------------------------------

export function setHeatOn(on: boolean) {
  patchFabric("heat", { on, error: null })
  if (!on) { syncHeat(); return }
  void loadHeatGrid().then(() => { if (fab().heat.on) syncHeat() })
}

export function setPeopleOn(on: boolean) {
  patchFabric("people", { on, error: null })
  if (!on) { syncPeople(); return }
  const map = getMap()
  if (map && map.getPitch() < 25 && !reducedMotion()) map.easeTo({ pitch: 42, duration: 750 })
  void loadPeopleHexes().then(() => { if (fab().people.on) syncPeople() })
}

export function setAccessOn(on: boolean) {
  patchFabric("access", { on, error: null })
  if (!on) { syncAccess(); return }
  void loadAccessHexes(fab().access.mode).then(() => { if (fab().access.on) syncAccess() })
}

export function setAccessMode(mode: AccessMode) {
  patchFabric("access", { mode, error: null })
  if (!fab().access.on) return
  void loadAccessHexes(mode).then(() => { if (fab().access.on) syncAccess() })
}

export function setSlopesOn(on: boolean) {
  patchFabric("slopes", { on, error: null })
  if (!on) { syncSlopes(); return }
  void loadGradients().then(() => { if (fab().slopes.on) syncSlopes() })
}

export function setSlopeFilter(filter: SlopeFilter) {
  patchFabric("slopes", { filter })
  if (fab().slopes.on) syncSlopes()
}

// -- assistant tool layers (heat_exposure, accessibility_15min, population_reach) ----------------

registerLayerRenderer({
  id: "fabric",
  claims: (d: LayerData) => !!d.meta?.fabric,
  add: (ref, src, _data, ids) => {
    const map = getMap()
    if (!map) return
    const label = firstLabelLayer()
    const fillId = `${ref}::fabric-fill`
    const lineId = `${ref}::fabric-line`
    const dotsId = `${ref}::fabric-dots`
    map.addLayer({
      id: fillId,
      type: "fill",
      source: src,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": ["get", "color"], "fill-opacity": base() === "night" ? 0.42 : 0.34 },
    }, label)
    ids.push(fillId)
    bindLayerHover(map, fillId)

    map.addLayer({
      id: lineId,
      type: "line",
      source: src,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "line-color": ["get", "color"], "line-width": 1.2, "line-opacity": 0.75 },
    }, label)
    ids.push(lineId)

    map.addLayer({
      id: dotsId,
      type: "circle",
      source: src,
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-radius": ["case", ["==", ["get", "state"], "origin"], 8, 5.5],
        "circle-color": ["get", "color"],
        "circle-stroke-width": 1.5,
        "circle-stroke-color": base() === "night" ? "#0e1828" : "#ffffff",
      },
    }, label)
    ids.push(dotsId)
    bindLayerHover(map, dotsId)
  },
})

// -- click handling -------------------------------------------------------------------------------

registerClickHandler({
  id: "fabric",
  priority: 14,
  layers: (l) =>
    l === PEOPLE_FILL || l === PEOPLE_EXTRUDE || l === ACCESS_FILL || l === SLOPES_LAYER ||
    l.endsWith("::fabric-fill") || l.endsWith("::fabric-dots"),
  resolve: (f, at) => {
    const p = f.properties ?? {}
    const id = String(p.id ?? "")
    const center: [number, number] = f.geometry.type === "Point"
      ? (f.geometry.coordinates as [number, number])
      : at
    if (p.kind === "fabric_hex" || /^hex-[0-9a-f]+$/i.test(id)) {
      return { id, at: center }
    }
    const lines = String(p.popup ?? "").split("\n").filter(Boolean)
    const title = lines.shift() || String(p.label || id || "Urban fabric")
    if (p.kind === "road_gradient") {
      return { local: { title, lines, color: p.color, center, geometry: f.geometry, kind: "street" } }
    }
    if (id === "origin") {
      return { local: { title, lines, color: p.color, center, geometry: f.geometry, kind: "place" } }
    }
    return id ? { id, at: center } : null
  },
})

// -- map lifecycle & basemap theme transitions ----------------------------------------------------

let started = false

export function startFabricMap() {
  if (started) return
  started = true
  let lastBase = base()
  useMapUi.subscribe((st) => {
    if (st.basemap === lastBase) return
    lastBase = st.basemap
    onStyleReady(() => {
      syncHeat()
      syncPeople()
      syncAccess()
      syncSlopes()
    })
  })
  const hook = () => {
    const map = getMap()
    if (!map) { window.setTimeout(hook, 200); return }
    map.on("style.load", () => {
      syncHeat()
      syncPeople()
      syncAccess()
      syncSlopes()
    })
  }
  hook()
}

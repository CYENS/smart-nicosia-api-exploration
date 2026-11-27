/* Natural hazards on the map, drawn their own way for any layer whose data carries
   `meta.hazards` (the Layers row's, an incident's, an answer's get_hazards layer):

   - the Nicosia district's fire danger as a wash over the island, EFFIS's classes in the twin's
     colours (nothing for low, amber for high, signal red only for extreme and worse);
   - a weather warning's area as a faint tint with an outline in the warning's own colour
     (yellow, orange, and signal red only for a red warning);
   - the felt reports people sent EMSC as small cells, by how strongly they felt it;
   - quakes as rings sized by magnitude, coloured by how strongly they reached Nicosia (signal red
     when it was felt here or strong and near, amber when likely felt, ink when minor), with a
     halo when it matters and the magnitude beside it.

   Its layers are named "<ref>::<part>", so a click opens the quake, the warning or the day's
   fire danger in the inspector like any answer's feature. */
import type { CanvasSource, ExpressionSpecification, Map as MlMap } from "maplibre-gl"

import { firstLabelLayer, getMap, registerLayerRenderer, themedPaint, type LayerData } from "@/lib/map/engine"
import { FIRE_CLASSES, FIRE_COLOR } from "@/lib/hazards/store"

const FIRE_ALPHA = [0, 0.05, 0.16, 0.3, 0.42, 0.52]
const FONT = ["Noto Sans Regular"]
const canvases = new Map<string, HTMLCanvasElement>()

type Corners = [[number, number], [number, number], [number, number], [number, number]]
type FireMeta = { grid?: string; size?: [number, number]; bbox?: [number, number, number, number] }

function rgb(hex: string): [number, number, number] {
  const n = parseInt(hex.replace("#", ""), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

// The classes (runs of "<class><count>", "." for no data) painted into a canvas, one pixel a cell.
export function paintFire(canvas: HTMLCanvasElement, fire: FireMeta): Corners | null {
  if (!fire.grid || !fire.size || !fire.bbox) return null
  const [w, h] = fire.size
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext("2d")
  if (!ctx) return null
  const img = ctx.createImageData(w, h)
  const colors = FIRE_CLASSES.map((c) => rgb(FIRE_COLOR[c]))
  let k = 0
  for (const run of fire.grid.split(" ")) {
    const n = Number(run.slice(1))
    if (run[0] !== ".") {
      const cls = Number(run[0])
      const [r, g, b] = colors[cls]
      const a = Math.round(FIRE_ALPHA[cls] * 255)
      for (let i = k; i < k + n && i < w * h; i++) {
        img.data[i * 4] = r
        img.data[i * 4 + 1] = g
        img.data[i * 4 + 2] = b
        img.data[i * 4 + 3] = a
      }
    }
    k += n
  }
  ctx.putImageData(img, 0, 0)
  const [x0, y0, x1, y1] = fire.bbox
  return [[x0, y1], [x1, y1], [x1, y0], [x0, y0]]
}

// A ring's radius in pixels by zoom and magnitude, `scale` times larger for a halo.
function radius(scale = 1): ExpressionSpecification {
  const by = (stops: number[]): ExpressionSpecification =>
    ["interpolate", ["linear"], ["get", "mag"], ...stops.flatMap((r, i) => [[1, 3, 4, 5, 6.5][i], r * scale])] as ExpressionSpecification
  return ["interpolate", ["linear"], ["zoom"], 6, by([2, 4, 7, 11, 18]), 10, by([3, 6, 10, 16, 26]), 15, by([4, 9, 14, 22, 34])]
}

const isKind = (k: string): ExpressionSpecification => ["==", ["get", "kind"], k]

function add(map: MlMap, def: Parameters<MlMap["addLayer"]>[0], ids: string[], before?: string) {
  if (map.getLayer(def.id)) return
  map.addLayer(def, before)
  ids.push(def.id)
}

registerLayerRenderer({
  id: "hazards",
  claims: (d: LayerData) => !!d.meta?.hazards,
  add: (ref, src, data, ids) => {
    const map = getMap()
    const meta = data.meta.hazards as { fire: FireMeta | null }
    const below = firstLabelLayer()
    if (meta.fire?.grid) {
      const canvas = canvases.get(ref) ?? document.createElement("canvas")
      canvases.set(ref, canvas)
      const coords = paintFire(canvas, meta.fire)
      if (coords) {
        map.addSource(`${ref}::fire-src`, { type: "canvas", canvas, coordinates: coords, animate: false })
        const s = map.getSource(`${ref}::fire-src`) as CanvasSource
        s.play()
        window.setTimeout(() => s.pause(), 160) // a paused canvas source shows what was drawn after a play
        add(map, { id: `${ref}::fire`, type: "raster", source: `${ref}::fire-src`,
          paint: { "raster-opacity": 1, "raster-resampling": "linear", "raster-fade-duration": 0 } }, ids, below)
      }
    }
    add(map, { id: `${ref}::warn-fill`, type: "fill", source: src, filter: isKind("warning"),
      paint: { "fill-color": ["get", "color"], "fill-opacity": ["case", ["get", "in_force"], 0.1, 0.05] } }, ids, below)
    add(map, { id: `${ref}::warn-line`, type: "line", source: src, filter: isKind("warning"),
      paint: { "line-color": ["get", "color"], "line-width": 1.6, "line-opacity": 0.9 } }, ids)
    add(map, { id: `${ref}::district`, type: "line", source: src, filter: isKind("fire_district"),
      paint: { "line-color": ["get", "color"], "line-width": 1.3, "line-opacity": 0.8, "line-dasharray": [2, 1.6] } }, ids)
    add(map, { id: `${ref}::felt`, type: "circle", source: src, filter: isKind("felt"),
      paint: {
        "circle-color": ["get", "color"], "circle-opacity": 0.55, "circle-blur": 0.25,
        "circle-radius": ["interpolate", ["linear"], ["zoom"],
          7, ["interpolate", ["linear"], ["get", "n"], 1, 1.5, 20, 3, 200, 5.5],
          12, ["interpolate", ["linear"], ["get", "n"], 1, 3, 20, 6, 200, 11]],
      } }, ids)
    const quake = ["all", isKind("earthquake"), ["in", ["get", "severity"], ["literal", ["high", "medium"]]]] as ExpressionSpecification
    add(map, { id: `${ref}::halo`, type: "circle", source: src, filter: quake,
      paint: { "circle-radius": radius(1.8), "circle-color": ["get", "color"], "circle-opacity": 0.1,
        "circle-stroke-width": 1, "circle-stroke-color": ["get", "color"], "circle-stroke-opacity": 0.4 } }, ids)
    add(map, { id: `${ref}::quake`, type: "circle", source: src, filter: isKind("earthquake"),
      paint: {
        "circle-radius": radius(), "circle-color": ["get", "color"],
        "circle-opacity": ["match", ["get", "severity"], "minor", 0.18, 0.32],
        "circle-stroke-color": ["get", "color"],
        "circle-stroke-width": ["match", ["get", "severity"], "high", 2.4, "minor", 1, 1.8],
      } }, ids)
    add(map, { id: `${ref}::mag`, type: "symbol", source: src, minzoom: 6.5,
      filter: ["all", isKind("earthquake"), [">=", ["get", "mag"], 2.5]] as ExpressionSpecification,
      layout: { "text-field": ["get", "label"], "text-font": FONT, "text-size": 11.5, "text-anchor": "left",
        "text-offset": [0.9, 0], "text-allow-overlap": false },
      paint: { "text-halo-width": 1.4, ...themedPaint(`${ref}::mag`, {
        "text-color": { night: "#e4ebf4", day: "#1f2937" }, "text-halo-color": { night: "#0e1828", day: "#ffffff" } }) } }, ids)
  },
  remove: (ref) => {
    const map = getMap()
    canvases.delete(ref)
    if (!map) return
    if (map.getLayer(`${ref}::fire`)) map.removeLayer(`${ref}::fire`)
    if (map.getSource(`${ref}::fire-src`)) map.removeSource(`${ref}::fire-src`)
  },
})

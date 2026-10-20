import type { StyleSpecification, LayerSpecification } from "maplibre-gl"

// Two base maps. Night is OpenFreeMap's dark style recoloured into the console palette, with
// OpenStreetMap buildings raised in 3D; Day is OpenFreeMap's liberty style as published.
export type Basemap = "night" | "day"

export const BASEMAP_URLS: Record<Basemap, string> = {
  night: "https://tiles.openfreemap.org/styles/dark",
  day: "https://tiles.openfreemap.org/styles/liberty",
}

const NIGHT = {
  ground: "#0e1828",
  residential: "#101c2e",
  green: "#0f2027",
  water: "#08192d",
  waterLine: "#0c2440",
  building: "#16233a",
  buildingEdge: "#1d2d47",
  minor: "#1a2a42",
  path: "#17253a",
  major: "#24364f",
  majorCasing: "rgba(88, 112, 146, 0.32)",
  motorway: "#2c4060",
  rail: "#1e2c42",
  boundary: "#2b3c55",
  label: "#7d8fa8",
  placeLabel: "#93a4bb",
  halo: "#0e1828",
}

type Paint = Record<string, unknown>

// Paint overrides by layer id (exact), then by prefix.
const EXACT: Record<string, Paint> = {
  background: { "background-color": NIGHT.ground },
  water: { "fill-color": NIGHT.water },
  waterway: { "line-color": NIGHT.waterLine },
  landuse_residential: { "fill-color": NIGHT.residential },
  landcover_wood: { "fill-color": NIGHT.green },
  landuse_park: { "fill-color": NIGHT.green },
  building: { "fill-color": NIGHT.building, "fill-outline-color": NIGHT.buildingEdge },
  highway_path: { "line-color": NIGHT.path },
  highway_minor: { "line-color": NIGHT.minor },
  highway_major_casing: { "line-color": NIGHT.majorCasing },
  highway_major_inner: { "line-color": NIGHT.major },
  highway_major_subtle: { "line-color": NIGHT.major },
  highway_motorway_casing: { "line-color": NIGHT.majorCasing },
  highway_motorway_inner: { "line-color": NIGHT.motorway },
  highway_motorway_subtle: { "line-color": NIGHT.major },
  "aeroway-taxiway": { "line-color": NIGHT.minor },
  "aeroway-area": { "fill-color": NIGHT.residential },
  "aeroway-runway": { "line-color": NIGHT.major },
  water_name: { "text-color": "#3d5a7c", "text-halo-color": NIGHT.halo },
  highway_name_other: { "text-color": NIGHT.label, "text-halo-color": NIGHT.halo, "text-halo-width": 1.4 },
  highway_name_motorway: { "text-color": NIGHT.label },
}
const PREFIX: [string, Paint][] = [
  ["landcover_", { "fill-color": NIGHT.ground }],
  ["road_", { "line-color": NIGHT.path, "fill-color": NIGHT.ground }],
  ["railway", { "line-color": NIGHT.rail }],
  ["boundary_", { "line-color": NIGHT.boundary }],
  ["place_", { "text-color": NIGHT.placeLabel, "text-halo-color": NIGHT.halo, "text-halo-width": 1.4 }],
]

// OpenStreetMap buildings at their mapped heights. The DLS 3D city replaces them when it's on.
const BASE_3D: LayerSpecification = {
  id: "base-3d",
  type: "fill-extrusion",
  source: "openmaptiles",
  "source-layer": "building",
  minzoom: 14,
  filter: ["!=", ["get", "hide_3d"], true],
  paint: {
    "fill-extrusion-color": [
      "interpolate", ["linear"], ["coalesce", ["get", "render_height"], 6],
      0, "#17253c", 15, "#1d2e4a", 40, "#263b5c",
    ],
    "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"], 14, 0, 14.6, ["coalesce", ["get", "render_height"], 6]],
    "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
    "fill-extrusion-opacity": 0.88,
  },
}

function recolour(layer: LayerSpecification): LayerSpecification {
  const over = EXACT[layer.id] ?? PREFIX.find(([p]) => layer.id.startsWith(p))?.[1]
  if (!over || !("paint" in layer)) return layer
  const paint = { ...(layer.paint as Paint) }
  for (const [k, v] of Object.entries(over)) {
    // Only recolour properties the layer type actually has.
    const kind = k.split("-")[0]
    const type = layer.type === "fill-extrusion" ? "fill-extrusion" : layer.type
    if (kind === "text" ? layer.type === "symbol" : type.startsWith(kind)) paint[k] = v
  }
  return { ...layer, paint } as LayerSpecification
}

export function transformStyle(basemap: Basemap) {
  return (_prev: StyleSpecification | undefined, next: StyleSpecification): StyleSpecification => {
    if (basemap !== "night") return next
    const layers = next.layers.map(recolour)
    // The 3D buildings go under the first label, so street names stay on top.
    const firstLabel = layers.findIndex((l) => l.type === "symbol")
    layers.splice(firstLabel < 0 ? layers.length : firstLabel, 0, BASE_3D)
    return { ...next, layers }
  }
}

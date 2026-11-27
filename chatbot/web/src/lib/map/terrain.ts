/* The ground in 3D: Mapzen's terrain tiles (Terrarium PNGs on AWS, open, no key) as MapLibre
   terrain. One module, so any feature can raise the ground: the view from a place (lib/view.ts)
   turns it on while looking out, and the environment twin can draw hillshade from the same
   source (addHillshade). The server reads the same tiles (chatbot/terrain.py), so the heights the
   assistant states and the ground the map draws agree.

   It stays off on the everyday map: Nicosia is flat (about 120 to 230 m), so raised ground adds
   little there while it costs a stream of height tiles and GPU time on every pan, and the flat
   map's readouts (the lamps' glow, the heatmaps, the routes) read more plainly on flat ground.
   Looking out at eye level is where the hills, the horizon and the Kyrenia mountains matter. */
import type { LngLatLike, Map as MlMap } from "maplibre-gl"

import { useMapUi } from "@/lib/map/engine"

export const TERRAIN_SOURCE = "terrain::dem"
export const TERRAIN_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
// The credit the tiles ask for: Mapzen's, and the elevation data under Cyprus.
export const TERRAIN_ATTRIBUTION =
  '<a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md" target="_blank" rel="noopener">Terrain: Mapzen, SRTM, EU-DEM (Copernicus)</a>'
// A modest lift, so low hills read as hills from a few km away. Heights drawn on the map are the
// real ones times this; groundAt gives them as drawn, which is what a camera must stand on.
export const EXAGGERATION = 1.2

export function addTerrainSource(map: MlMap) {
  if (map.getSource(TERRAIN_SOURCE)) return
  map.addSource(TERRAIN_SOURCE, {
    type: "raster-dem", tiles: [TERRAIN_TILES], tileSize: 256, encoding: "terrarium", maxzoom: 15,
    attribution: TERRAIN_ATTRIBUTION,
  })
}

// Raise the ground (or lay it flat again). Safe to call before the style has loaded its sources.
export function setTerrain(map: MlMap, on: boolean, exaggeration = EXAGGERATION) {
  if (on) {
    addTerrainSource(map)
    map.setTerrain({ source: TERRAIN_SOURCE, exaggeration })
  } else if (map.getTerrain()) {
    map.setTerrain(null)
  }
  useMapUi.setState({ terrain: on })
}

export function terrainOn(map: MlMap) {
  return !!map.getTerrain()
}

// The ground's height at a point as the map draws it (exaggerated), or null until its tile is in.
export function groundAt(map: MlMap, at: LngLatLike): number | null {
  if (!map.getTerrain()) return null
  const h = map.queryTerrainElevation(at)
  return h === null || h === undefined || !Number.isFinite(h) ? null : h
}

// Shaded relief from the same tiles, under the given layer (for the environment twin's terrain).
export function addHillshade(map: MlMap, id = "terrain::hillshade", before?: string) {
  addTerrainSource(map)
  if (map.getLayer(id)) return
  map.addLayer({
    id, type: "hillshade", source: TERRAIN_SOURCE,
    paint: { "hillshade-exaggeration": 0.35, "hillshade-shadow-color": "#0b1322", "hillshade-highlight-color": "#2b4266" },
  }, before)
}

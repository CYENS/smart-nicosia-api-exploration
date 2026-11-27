/* The ground in 3D: the terrain model the Department of Lands and Surveys made from its 2019
   LiDAR flight (a 1 m grid, CC BY 4.0), as Mapterhorn's open terrain tiles (Terrarium heights in
   WebP, 512 px, no key) and MapLibre terrain. At 1 m the Venetian walls, their moat and bastions
   and the bed of the Pedieos have their shape. North of the buffer zone the tiles are filled from
   Copernicus GLO-30 (30 m), so the ground there is smooth.

   One module, so any feature can raise the ground: the view from a place (lib/view.ts) turns it
   on while looking out, and the environment twin draws its hillshade from the same tiles
   (terrainSourceSpec). The server reads these tiles too (chatbot/terrain.py), so the heights the
   assistant states and the ground the map draws agree.

   It stays off on the everyday map: Nicosia is flat (about 120 to 230 m), so raised ground adds
   little there while it costs a stream of height tiles and GPU time on every pan, and the flat
   map's readouts (the lamps' glow, the heatmaps, the routes) read more plainly on flat ground.
   Looking out at eye level is where the hills, the horizon and the Kyrenia mountains matter. */
import type { LngLatLike, Map as MlMap, RasterDEMSourceSpecification } from "maplibre-gl"

import { useMapUi } from "@/lib/map/engine"

export const TERRAIN_SOURCE = "terrain::dem"
export const TERRAIN_TILES = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
// A 512 px tile at level 16 is about 1 m a pixel at Nicosia: the model's own grid. The tiles go
// one level further, which would only cut the same heights finer.
export const TERRAIN_TILE_SIZE = 512
export const TERRAIN_MAX_ZOOM = 16
// The credit the tiles ask for: Mapterhorn's page names every model under them, the DLS one and
// Copernicus GLO-30 among them.
export const TERRAIN_ATTRIBUTION =
  '<a href="https://mapterhorn.com/attribution" target="_blank" rel="noopener">Ground: DLS Cyprus 2019 (CC BY 4.0), Copernicus, via Mapterhorn</a>'
// A modest lift, so low hills read as hills from a few km away. Heights drawn on the map are the
// real ones times this; groundAt gives them as drawn, which is what a camera must stand on.
export const EXAGGERATION = 1.2

// The one description of the height tiles, for every source that reads them (the terrain, the
// environment's hillshade, an overlay map of its own).
export function terrainSourceSpec(): RasterDEMSourceSpecification {
  return {
    type: "raster-dem", tiles: [TERRAIN_TILES], tileSize: TERRAIN_TILE_SIZE, encoding: "terrarium",
    maxzoom: TERRAIN_MAX_ZOOM, attribution: TERRAIN_ATTRIBUTION,
  }
}

export function addTerrainSource(map: MlMap) {
  if (map.getSource(TERRAIN_SOURCE)) return
  map.addSource(TERRAIN_SOURCE, terrainSourceSpec())
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

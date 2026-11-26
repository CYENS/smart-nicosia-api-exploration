/* The ground under the city: the base map's own drawing, or DLS's 2019 aerial photo (about
   7.5 cm a pixel, through /api/imagery, which keeps every tile it fetches). The photo lies over
   the base map's fields and streets and under everything else: its labels and buildings, the
   streets' glow at night, the walls and whatever the twin draws. It drapes over the terrain in a
   view by itself. After dark it is dimmed, as the streets around it are.

   The choice is remembered on this device. The photo covers greater Nicosia; past it, and north
   of the Green Line where DLS has no photo, the base map shows through. */
import { create } from "zustand"

import { getMap, groundSlot, onStyleReady, themedPaint, useMapUi } from "@/lib/map/engine"
import { registerActiveSources } from "@/lib/sources"

export type Ground = "map" | "photo"
type GroundState = { ground: Ground; loading: boolean }

const KEY = "twin.ground"
const SRC = "ground::photo-src"
const LAYER = "ground::photo"
// Greater Nicosia, as the server serves it (chatbot/imagery.py COVER).
const COVER: [number, number, number, number] = [33.18, 35.04, 33.55, 35.27]

function remembered(): Ground {
  try {
    return localStorage.getItem(KEY) === "photo" ? "photo" : "map"
  } catch { return "map" }
}

export const useGround = create<GroundState>(() => ({ ground: remembered(), loading: false }))

registerActiveSources(() => (useGround.getState().ground === "photo" ? ["dls_imagery"] : []), (fn) => useGround.subscribe(fn))

export function setGround(ground: Ground) {
  if (ground === useGround.getState().ground) return
  useGround.setState({ ground })
  try { localStorage.setItem(KEY, ground) } catch { /* private mode */ }
  sync()
}

// Over the base map's fields and streets, under the rest (engine.ts groundSlot).
function place() {
  const map = getMap()
  if (!map?.getLayer(LAYER)) return
  const before = groundSlot()
  if (before && before !== LAYER) map.moveLayer(LAYER, before)
}

function sync() {
  const map = getMap()
  if (!map) return
  onStyleReady(() => {
    const on = useGround.getState().ground === "photo"
    if (!on) {
      if (map.getLayer(LAYER)) map.removeLayer(LAYER)
      useGround.setState({ loading: false })
      return
    }
    if (!map.getSource(SRC)) {
      map.addSource(SRC, {
        type: "raster", tileSize: 256, minzoom: 10, maxzoom: 19, bounds: COVER,
        tiles: [`${location.origin}/api/imagery/2019/{z}/{x}/{y}`],
      })
    }
    if (!map.getLayer(LAYER)) {
      map.addLayer({
        id: LAYER, type: "raster", source: SRC, minzoom: 10,
        paint: {
          ...themedPaint(LAYER, {
            "raster-brightness-max": { night: 0.36, day: 1 },
            "raster-saturation": { night: -0.4, day: 0.05 },
            "raster-contrast": { night: 0.08, day: 0.04 },
          }),
          "raster-fade-duration": 200,
          "raster-resampling": "linear",
        },
      }, groundSlot())
    }
    place()
  })
}

// Loading: while the photo's tiles for the view are still on their way (DLS is throttled, so a
// new area takes a few seconds the first time).
function watchLoading() {
  const map = getMap()
  const check = () => {
    const on = useGround.getState().ground === "photo" && !!map.getSource(SRC)
    const loading = on && !map.isSourceLoaded(SRC)
    if (loading !== useGround.getState().loading) useGround.setState({ loading })
  }
  map.on("sourcedata", (e) => { if (e.sourceId === SRC) check() })
  map.on("idle", check)
}

let started = false
export function startGround() {
  const map = getMap()
  if (!map || started) return
  started = true
  // A new base map comes in under the old one, labels and all: the photo goes back under them.
  let base = useMapUi.getState().basemap
  useMapUi.subscribe((s) => {
    if (s.basemap === base) return
    base = s.basemap
    place()
  })
  watchLoading()
  sync()
}

/* A layer drawn by a feature's own renderer can bring its own legend to the map panel (a ramp
   that follows the base map, a note on how sure the surface is). The panel asks here before
   drawing its usual swatches. */
import type { ComponentType } from "react"

import type { LayerSummary } from "@/lib/map/engine"

export type LayerKey = ComponentType<{ layer: LayerSummary }>

const keys = new Map<string, LayerKey>()

export function registerLayerKey(rendererId: string, Key: LayerKey) {
  keys.set(rendererId, Key)
}

export function layerKeyFor(rendererId: string | null): LayerKey | null {
  return rendererId ? keys.get(rendererId) ?? null : null
}

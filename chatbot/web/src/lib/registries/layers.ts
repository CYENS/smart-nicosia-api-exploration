/* What the Layers sheet lists under "City systems": groups (Sensors, Buildings, Alerts), and in
   each the layers of the city's own systems, each with its switch and its live summary. A later
   feature adds a row with registerSystemLayer (the environment twin's air and noise surfaces
   under Sensors, the incident inbox under Alerts), or a group of its own with
   registerLayerGroup. */
import type { ComponentType } from "react"
import { create } from "zustand"

export type LayerGroup = { id: string; label: string; order: number }

export type SystemLayer = {
  id: string
  group: string
  order: number
  label: string
  useOn: () => boolean // whether it is on the map (a hook: the sheet calls it while it shows)
  setOn: (on: boolean) => void
  Summary: ComponentType // the live summary, one muted line
  Legend?: ComponentType // shown under the row while it is on
}

type Registry = { groups: LayerGroup[]; layers: SystemLayer[] }

export const useLayerRegistry = create<Registry>(() => ({ groups: [], layers: [] }))

export function registerLayerGroup(g: LayerGroup) {
  useLayerRegistry.setState((s) => ({
    groups: [...s.groups.filter((x) => x.id !== g.id), g].sort((a, b) => a.order - b.order),
  }))
}

export function registerSystemLayer(l: SystemLayer) {
  useLayerRegistry.setState((s) => ({
    layers: [...s.layers.filter((x) => x.id !== l.id), l].sort((a, b) => a.order - b.order),
  }))
}

registerLayerGroup({ id: "sensors", label: "Sensors", order: 0 })
registerLayerGroup({ id: "buildings", label: "Buildings", order: 10 })
registerLayerGroup({ id: "alerts", label: "Alerts", order: 20 })

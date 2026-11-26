/* The map's credits corner: the base map's credits as before, then a short credit for each source
   whose data the map draws now (the live buses, the road detectors, the DLS for the 3D city...)
   and a "Data sources" link. Each credit opens the data sources panel at its source. It stays
   compact (the ⓘ), as MapLibre's own control does. */
import { AttributionControl } from "maplibre-gl"

import { getMap, useMapUi } from "@/lib/map/engine"
import { useMobility } from "@/lib/mobility/store"
import { activeSourceIds, loadSources, openSources, useSources } from "@/lib/sources"

let control: AttributionControl | null = null
let shown: string | null = null

const escape = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!)

function credits(ids: string[]): string[] {
  const list = useSources.getState().list
  const out: string[] = []
  for (const id of ids) {
    const s = list.find((x) => x.id === id)
    if (s?.credit) out.push(`<a href="#" data-source="${escape(s.id)}" title="Where this comes from">${escape(s.credit)}</a>`)
  }
  out.push(`<a href="#" data-source="" class="credits-all">Data sources</a>`)
  return out
}

function sync() {
  const map = getMap()
  if (!map) return
  const next = credits(activeSourceIds())
  const key = next.join("|")
  if (key === shown) return
  shown = key
  // Swapping the control is MapLibre's public way to change custom credits. Keep it as the
  // reader left it: closed stays closed.
  const wasOpen = !control || control._container?.classList.contains("maplibregl-compact-show")
  if (control) map.removeControl(control)
  control = new AttributionControl({ compact: true, customAttribution: next })
  map.addControl(control, "bottom-right")
  const box = control._container
  // MapLibre stacks a new bottom control on top of the others: the credits belong at the foot.
  box?.parentElement?.appendChild(box)
  if (!wasOpen) box?.classList.remove("maplibregl-compact-show")
}

let started = false
export function startCredits() {
  const map = getMap()
  if (!map || started) return
  started = true
  map.getContainer().addEventListener("click", (e) => {
    const a = (e.target as Element | null)?.closest?.("a[data-source]")
    if (!(a instanceof HTMLElement)) return
    e.preventDefault()
    openSources(a.dataset.source || null, a)
  })
  useMapUi.subscribe(sync)
  useMobility.subscribe(sync)
  useSources.subscribe(sync)
  sync()
  void loadSources()
}

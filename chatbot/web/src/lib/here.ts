/* "What's here?": any point of the map opens in the inspector as a spot (the kind the environment
   twin adds, spot-35.17000-33.36060), with the everyday sections the server reads for it from
   what the twin already holds (chatbot/here.py): the address, buses, parking, dangerous
   buildings, the building under the pin and the street lights after dark.

   How it opens, as in Google Maps: a plain click on empty map drops a pin and opens the card;
   with something open, that click closes it instead (the next click asks again). A long press on
   touch, "What's here?" in the right-click menu and the Ctrl K command always ask, open or not. */
import { MapPinIcon } from "lucide-react"

import "@/components/inspector/here-sections"
import { KINDS, registerPlaceholder, type Entity } from "@/lib/entities"
import { closeInspector, inspect, useInspector } from "@/lib/inspector"
import { searchOpenerElement } from "@/lib/layout"
import { getCamera } from "@/lib/map/engine"
import { registerEmptyClickHandler } from "@/lib/registries/map-clicks"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"

KINDS.spot = { label: "Spot", icon: MapPinIcon, chip: "place" }

const SPOT_ID = /^spot-(\d{1,2}\.\d{1,6})-(\d{1,2}\.\d{1,6})$/i

// A point's id, as the server writes it: latitude then longitude, five decimals (about a metre).
export function spotId([lon, lat]: [number, number]) {
  return `spot-${lat.toFixed(5)}-${lon.toFixed(5)}`
}

// Where a spot id is, as [lon, lat].
export function spotWhere(id: string): [number, number] | null {
  const m = SPOT_ID.exec(id.trim())
  return m ? [Number(m[2]), Number(m[1])] : null
}

registerPlaceholder((id): Entity | null => {
  const at = spotWhere(id)
  if (!at) return null
  return {
    id: spotId(at), kind: "spot", kind_label: "Spot", name: "Dropped pin",
    subtitle: `${at[1].toFixed(5)}, ${at[0].toFixed(5)}`,
    geometry: { type: "Point", coordinates: at }, center: at, footprints: [], state: null, readings: [],
    attributes: [], relations: [], sources: [], as_of: null, has_series: false,
  }
})

export type SpotOptions = { focus?: boolean; opener?: HTMLElement | null; fly?: boolean }

export function inspectSpot(at: [number, number], opts: SpotOptions = {}) {
  inspect({ id: spotId(at), at }, opts)
}

registerEmptyClickHandler({
  id: "whats-here",
  priority: 0,
  handle: (at, how) => {
    if (how === "click" && useInspector.getState().open) {
      closeInspector()
      return true
    }
    const canvas = document.querySelector<HTMLElement>(".maplibregl-canvas")
    inspectSpot(at, how === "menu" ? { focus: true, opener: canvas } : {})
    return true
  },
})

registerSearchProvider({
  id: "whats-here",
  heading: "Commands",
  order: 19, // first among the commands
  search: (q) => {
    const item: SearchItem = {
      key: "cmd:whats-here", icon: MapPinIcon, title: "What's here? (centre of the map)",
      detail: "The address, buses, parking and more at the middle of the map", opens: true,
      run: () => {
        const c = getCamera()
        if (c) inspectSpot(c.center, { focus: true, opener: searchOpenerElement() })
      },
    }
    return matches(q, item.title, item.detail, "spot point pin address around near this place") ? [item] : []
  },
})

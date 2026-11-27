/* The street rules plug into the page here: their kinds in the inspector (a speed limit, a slow
   zone, signals, a crossing, a camera, a cycleway, a bus lane, a school), the Layers sheet's
   Streets group, their map styles and clicks, their sections in the inspector, their commands
   in Ctrl K and the sources their layers draw from. Zones, cameras, lanes, signals and crossings
   are searched by the server's entity search ("Street rules"). */
import {
  BikeIcon, BusFrontIcon, CctvIcon, FootprintsIcon, GaugeIcon, HexagonIcon, SchoolIcon, SignpostBigIcon,
} from "lucide-react"

import "@/components/streets/rows"
import "@/components/streets/sections"
import { KINDS } from "@/lib/entities"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { registerActiveSources } from "@/lib/sources"
import { setStreetLayer, startStreetsMap } from "@/lib/streets/map"
import { useStreets, type StreetLayerId } from "@/lib/streets/store"

KINDS.speed_limit = { label: "Speed limit", icon: GaugeIcon, chip: "place" }
KINDS.slow_zone = { label: "Slow zone", icon: HexagonIcon, chip: "place" }
KINDS.slow_zone_entrance = { label: "Zone entrance", icon: HexagonIcon, chip: "place" }
KINDS.traffic_signal = { label: "Traffic signals", icon: SignpostBigIcon, chip: "place" }
KINDS.pedestrian_crossing = { label: "Pedestrian crossing", icon: FootprintsIcon, chip: "place" }
KINDS.enforcement_camera = { label: "Enforcement camera", icon: CctvIcon, chip: "device" }
KINDS.cycleway = { label: "Cycleway", icon: BikeIcon, chip: "place" }
KINDS.bus_lane = { label: "Bus lane", icon: BusFrontIcon, chip: "place" }
KINDS.school = { label: "School", icon: SchoolIcon, chip: "place" }

const SOURCES: Record<StreetLayerId, string[]> = {
  limits: ["speed_limits", "openstreetmap"], zones: ["slow_zones"], signals: ["traffic_signals"],
  crossings: ["pedestrian_crossings"], cycleways: ["cycleways"], bus_lanes: ["bus_lanes"], cameras: ["enforcement_cameras"],
}

// What the Streets rows draw from, for the map's credits and the sources panel.
registerActiveSources(() => {
  const s = useStreets.getState()
  return (Object.keys(SOURCES) as StreetLayerId[]).filter((id) => s[id].on).flatMap((id) => SOURCES[id])
}, (fn) => useStreets.subscribe(fn))

const COMMANDS: { id: StreetLayerId[]; icon: typeof GaugeIcon; show: string; hide: string; detail: string; words: string }[] = [
  { id: ["limits"], icon: GaugeIcon, show: "Show the speed limits", hide: "Hide the speed limits", detail: "Posted limits along the streets", words: "speed limit km/h 30 50 signs" },
  { id: ["zones"], icon: HexagonIcon, show: "Show the 30 zones", hide: "Hide the 30 zones", detail: "20 and 30 km/h zones, planned", words: "slow zone 30 20 lez" },
  { id: ["signals", "crossings"], icon: FootprintsIcon, show: "Show signals and crossings", hide: "Hide signals and crossings", detail: "Where to cross", words: "pedestrian crossing zebra traffic lights signals" },
  { id: ["cycleways"], icon: BikeIcon, show: "Show the cycleways", hide: "Hide the cycleways", detail: "And where the network breaks", words: "cycle bike lanes bicycle" },
  { id: ["bus_lanes"], icon: BusFrontIcon, show: "Show the bus lanes", hide: "Hide the bus lanes", detail: "In use and planned", words: "bus lane tram corridor" },
  { id: ["cameras"], icon: CctvIcon, show: "Show the enforcement cameras", hide: "Hide the enforcement cameras", detail: "At the junctions", words: "speed camera red light enforcement" },
]

registerSearchProvider({
  id: "streets-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    if (!q) return []
    const s = useStreets.getState()
    const items: SearchItem[] = []
    for (const c of COMMANDS) {
      const on = c.id.some((id) => s[id].on)
      const title = on ? c.hide : c.show
      if (!matches(q, title, c.detail, c.words)) continue
      items.push({ key: `cmd:streets:${c.id.join("+")}`, icon: c.icon, group: "Commands", title, detail: c.detail,
                   run: () => { for (const id of c.id) void setStreetLayer(id, !on) } })
    }
    return items
  },
})

startStreetsMap()

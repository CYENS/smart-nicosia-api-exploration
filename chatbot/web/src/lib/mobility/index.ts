/* Live mobility plugs into the twin here: the Mobility workspace on the rail, the kinds it adds
   to the inspector (bus, bus stop, bus line, road segment), its inspector sections, its map
   layers and click handlers, and its commands in Ctrl K. Stops, lines and roads are searched by
   the server's entity search. */
import { BusFrontIcon, GaugeIcon } from "lucide-react"

import "@/components/mobility/sections"
import { MobilityPanel } from "@/components/mobility/workspace"
import { KINDS } from "@/lib/entities"
import { enterMobilityView, leaveMobilityView, setBusesOn, setRoadsOn, startMobilityMap } from "@/lib/mobility/map"
import { useMobility } from "@/lib/mobility/store"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { registerWorkspace } from "@/lib/registries/workspaces"

KINDS.bus = { label: "Bus", icon: BusFrontIcon, chip: "place" }
KINDS.bus_stop = { label: "Bus stop", icon: BusFrontIcon, chip: "place" }
KINDS.bus_line = { label: "Bus line", icon: BusFrontIcon, chip: "place" }
KINDS.road_segment = { label: "Road segment", icon: GaugeIcon, chip: "place" }

registerWorkspace({
  id: "mobility", label: "Mobility", description: "Mobility: buses, road speeds and counts now",
  icon: BusFrontIcon, order: 10, key: "2",
  Side: MobilityPanel, onEnter: enterMobilityView, onLeave: leaveMobilityView,
})

registerSearchProvider({
  id: "mobility-commands",
  heading: "Commands",
  order: 21,
  search: (q) => {
    const { buses, roads } = useMobility.getState()
    const items: (SearchItem & { words: string })[] = [
      { key: "cmd:buses", icon: BusFrontIcon, group: "Commands", title: buses.on ? "Hide the buses" : "Show the buses",
        detail: "Live, gliding along their routes", run: () => setBusesOn(!buses.on), words: "bus buses transit live map" },
      { key: "cmd:roads", icon: GaugeIcon, group: "Commands", title: roads.on ? "Hide road speeds" : "Show road speeds",
        detail: "Main roads against their free flow", run: () => setRoadsOn(!roads.on), words: "traffic congestion speed roads" },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

startMobilityMap()

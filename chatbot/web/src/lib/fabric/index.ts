/* Urban fabric integration entry point:
   Registers Layers sheet rows, Inspector sections, entity icon meta, layer key legend,
   Data sources active tracking, Ctrl+K commands, and starts map listeners. */
import { FlameIcon, FootprintsIcon, HexagonIcon, MountainIcon, UsersIcon } from "lucide-react"
import { createElement } from "react"

import "@/components/fabric/rows"
import "@/components/fabric/sections"
import { KINDS } from "@/lib/entities"
import {
  setAccessMode, setAccessOn, setHeatOn, setPeopleOn, setSlopesOn, startFabricMap,
} from "@/lib/fabric/map"
import { useFabric } from "@/lib/fabric/store"
import { registerLayerKey } from "@/lib/registries/layer-keys"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { registerActiveSources } from "@/lib/sources"

KINDS.fabric_hex = { label: "City hexagon", icon: HexagonIcon, chip: "place" }

registerLayerKey("fabric", ({ layer }) => {
  const entries = Object.entries(layer.legend ?? {})
  return createElement(
    "div",
    { className: "flex flex-col gap-1.5" },
    entries.length > 0 &&
      createElement(
        "div",
        { className: "flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted-foreground" },
        entries.map(([label, color]) =>
          createElement(
            "span",
            { key: label, className: "inline-flex items-center gap-1.5" },
            createElement("span", {
              className: "size-2.5 shrink-0 rounded-sm",
              style: { background: color },
              "aria-hidden": "true",
            }),
            label,
          ),
        ),
      ),
    createElement(
      "p",
      { className: "text-[11.5px] leading-snug text-muted-foreground" },
      "Landsat heat shows morning ground skin temperature (~10:30 local), not air temperature; population is Kontur modelled 2022.",
    ),
  )
})

registerActiveSources(
  () => {
    const s = useFabric.getState()
    const ids: string[] = []
    if (s.heat.on) ids.push("landsat_lst")
    if (s.people.on) ids.push("kontur_population")
    if (s.access.on) {
      ids.push("openstreetmap", "overture", "overture_base", "bus_timetable", "cynap_gradients", "kontur_population")
    }
    if (s.slopes.on) ids.push("cynap_gradients")
    return ids
  },
  (fn) => useFabric.subscribe(fn),
)

registerSearchProvider({
  id: "fabric-commands",
  heading: "Commands",
  order: 15,
  search: (q) => {
    const s = useFabric.getState()
    const items: (SearchItem & { words: string })[] = [
      {
        key: "cmd:fabric-heat",
        icon: FlameIcon,
        group: "Commands",
        title: s.heat.on ? "Hide summer ground heat" : "Show summer ground heat",
        detail: "Landsat morning ground surface temperature (~10:30 local)",
        run: () => setHeatOn(!s.heat.on),
        words: "summer heat ground surface temperature landsat thermal urban fabric",
      },
      {
        key: "cmd:fabric-people",
        icon: UsersIcon,
        group: "Commands",
        title: s.people.on ? "Hide population hexagons" : "Show population hexagons",
        detail: "Kontur modelled 2022 residents in H3 hexagons",
        run: () => setPeopleOn(!s.people.on),
        words: "people population kontur density hexagons 2022 residents",
      },
      {
        key: "cmd:fabric-access",
        icon: FootprintsIcon,
        group: "Commands",
        title: s.access.on ? "Hide 15-minute access" : "Show 15-minute access",
        detail: "Standard walkability score to everyday services",
        run: () => { setAccessMode("standard"); setAccessOn(!s.access.on) },
        words: "15 minute access walkability services city",
      },
      {
        key: "cmd:fabric-step-free",
        icon: FootprintsIcon,
        group: "Commands",
        title: "Show step-free 15-minute access",
        detail: "Gentle-slope accessibility with CyNAP gradient penalties",
        run: () => { setAccessMode("step_free"); setAccessOn(true) },
        words: "step free wheelchair gentle slope accessibility 15 minute",
      },
      {
        key: "cmd:fabric-slopes",
        icon: MountainIcon,
        group: "Commands",
        title: s.slopes.on ? "Hide street slopes" : "Show street slopes",
        detail: "CyNAP 100 m road grades (>5% and >8%)",
        run: () => setSlopesOn(!s.slopes.on),
        words: "street slopes hills gradient steep wheelchair cynap",
      },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

startFabricMap()

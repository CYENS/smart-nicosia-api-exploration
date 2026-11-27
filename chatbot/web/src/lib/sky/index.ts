/* Living sky and CyNAP ITS infrastructure plug into the twin here:
   - Inspector kinds: aircraft (ac-*), PWD CCTV cameras (cctv-*), PWD loop detectors (det-*)
   - Layers rows ("Aircraft over Nicosia", "PWD traffic detectors & CCTV")
   - Outside card section (next visible ISS pass) & City Clock track marks
   - Ctrl K command search entries */
import { CctvIcon, PlaneIcon, RadarIcon } from "lucide-react"

import "@/components/sky/rows"
import "@/components/sky/sections"
import { KINDS } from "@/lib/entities"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"
import { setAircraftOn, setItsOn, startSkyMap } from "@/lib/sky/map"
import { useSkyLayers } from "@/lib/sky/store"

KINDS.aircraft = { label: "Aircraft", icon: PlaneIcon, chip: "place" }
KINDS.its_cctv = { label: "PWD CCTV", icon: CctvIcon, chip: "device" }
KINDS.its_detector = { label: "Traffic detector", icon: RadarIcon, chip: "device" }

registerSearchProvider({
  id: "sky-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    const { aircraft, its } = useSkyLayers.getState()
    const items: (SearchItem & { words: string })[] = [
      {
        key: "cmd:aircraft",
        icon: PlaneIcon,
        group: "Commands",
        title: aircraft.on ? "Hide aircraft over Nicosia" : "Show aircraft over Nicosia",
        detail: "Live flights within 40 nm via adsb.lol",
        run: () => setAircraftOn(!aircraft.on),
        words: "aircraft planes flights adsb sky fly",
      },
      {
        key: "cmd:its",
        icon: CctvIcon,
        group: "Commands",
        title: its.on ? "Hide PWD traffic detectors & CCTV" : "Show PWD traffic detectors & CCTV",
        detail: "13 CCTV sites and 39 loop detectors from CyNAP",
        run: () => setItsOn(!its.on),
        words: "cctv camera detector loop swarco cynap pwd traffic its",
      },
    ]
    return q ? items.filter((c) => matches(q, c.title, c.detail, c.words)) : []
  },
})

startSkyMap()

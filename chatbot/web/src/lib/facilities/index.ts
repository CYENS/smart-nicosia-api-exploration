/* Park, charge and hire plugs into the twin here: the six kinds it adds to the inspector (car
   park, park and ride, EV charger, Nextbike station, Bolt scooter zone, bicycle rack), their
   inspector sections, the Layers sheet's group and the Parking card's section, the map layers
   and their clicks, and the commands in Ctrl K. The facilities themselves are searched by the
   server's entity search. Everything is off until asked for: the default screen stays calm. */
import { BikeIcon, CircleParkingIcon, EvChargerIcon, ParkingMeterIcon, ScooterIcon, SquareParkingIcon } from "lucide-react"

import "@/components/facilities/rows"
import "@/components/facilities/sections"
import { KINDS } from "@/lib/entities"
import { setFacilityOn, startFacilitiesMap } from "@/lib/facilities/map"
import { FAC_LABELS, useFacilities, type FacKind } from "@/lib/facilities/store"
import { matches, registerSearchProvider, type SearchItem } from "@/lib/registries/search"

KINDS.car_park = { label: "Car park", icon: SquareParkingIcon, chip: "place" }
KINDS.park_and_ride = { label: "Park and ride", icon: CircleParkingIcon, chip: "place" }
KINDS.charger = { label: "EV charger", icon: EvChargerIcon, chip: "place" }
KINDS.bike_station = { label: "Nextbike station", icon: BikeIcon, chip: "place" }
KINDS.scooter_zone = { label: "Bolt scooter zone", icon: ScooterIcon, chip: "place" }
KINDS.bike_rack = { label: "Bicycle rack", icon: ParkingMeterIcon, chip: "place" }

const COMMANDS: { kind: FacKind; words: string; detail: string }[] = [
  { kind: "car_park", words: "parking car park where to park space", detail: "With live bays where the city has sensors" },
  { kind: "charger", words: "ev electric car charging charger plug", detail: "Power and connectors of every public charger" },
  { kind: "bike_station", words: "bike bicycle nextbike hire rent e-bike", detail: "With the bikes there now" },
  { kind: "scooter_zone", words: "scooter bolt e-scooter", detail: "Where Bolt's e-scooters are parked" },
  { kind: "park_and_ride", words: "park and ride p+r pame express", detail: "Leave the car, take the bus in" },
  { kind: "bike_rack", words: "bike rack bicycle parking stand", detail: "For your own bike" },
]

registerSearchProvider({
  id: "facility-commands",
  heading: "Commands",
  order: 22,
  search: (q) => {
    if (!q) return []
    const kinds = useFacilities.getState().kinds
    const items: (SearchItem & { words: string })[] = COMMANDS.map((c) => ({
      key: `cmd:facility:${c.kind}`, icon: KINDS[c.kind].icon, group: "Commands",
      title: `${kinds[c.kind].on ? "Hide" : "Show"} ${FAC_LABELS[c.kind].toLowerCase()}`,
      detail: c.detail, words: c.words, run: () => setFacilityOn(c.kind, !useFacilities.getState().kinds[c.kind].on),
    }))
    return items.filter((c) => matches(q, c.title, c.detail, c.words))
  },
})

startFacilitiesMap()

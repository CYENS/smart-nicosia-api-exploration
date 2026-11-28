/* Park, charge and hire in the calm layout: its own group in the Layers sheet (car parks, park
   and ride, EV chargers, Nextbike stations, Bolt scooter zones and bicycle racks, each with its
   switch, a line on what it shows and how old its inventory is), and a section on the Parking
   card that puts the four most asked for on the map in one press. All default off. */
import { useId } from "react"

import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useClock } from "@/lib/clock"
import { badgeUrl, setFacilityOn } from "@/lib/facilities/map"
import {
  FAC_KINDS, FAC_LABELS, inventoryAge, useFacilities, type FacKind, type FacMeta, type KindState,
} from "@/lib/facilities/store"
import { openLayers } from "@/lib/layers-sheet"
import { fmtCount } from "@/lib/readouts"
import { registerLayerGroup, registerSystemLayer } from "@/lib/registries/layers"
import { registerTopicSection } from "@/lib/registries/topics"

export const HIRE_GROUP = "hire"

const ABOUT: Record<FacKind, string> = {
  car_park: "Where to park, with live bays where the city has sensors beside a car park",
  park_and_ride: "Leave the car and take the Pame Express bus in",
  charger: "Public charging points with their power and connectors",
  bike_station: "Where to pick up and leave a bike, with the bikes there now",
  scooter_zone: "Where Bolt's e-scooters are parked",
  bike_rack: "Public racks for your own bike",
}

export function Badge({ kind, dim, className }: { kind: FacKind; dim?: boolean; className?: string }) {
  return <img src={badgeUrl(kind, dim)} alt="" aria-hidden="true" width={18} height={18} className={className ?? "size-[18px] shrink-0"} />
}

function countLine(kind: FacKind, meta: FacMeta, live: ReturnType<typeof useFacilities.getState>["live"], past: boolean) {
  const n = meta.in_nicosia
  switch (kind) {
    case "car_park": {
      const withBays = Object.values(live.bays).filter((b) => b.known)
      const free = withBays.reduce((s, b) => s + b.free, 0)
      const sensed = meta.sensed ?? Object.keys(live.bays).length
      const bays = withBays.length ? `, ${fmtCount(free)} sensed bays free beside ${fmtCount(withBays.length)} of them${past ? " then" : ""}` : sensed ? `, ${fmtCount(sensed)} with bay sensors beside them` : ""
      return `${fmtCount(n)} in Greater Nicosia${bays}`
    }
    case "charger":
      return `${fmtCount(n)} in Greater Nicosia, ${fmtCount(meta.fast_in_nicosia ?? 0)} fast` +
        (meta.listed_out_of_service_in_nicosia ? `, ${fmtCount(meta.listed_out_of_service_in_nicosia)} listed out of service` : "")
    case "bike_station": {
      if (past) return `${fmtCount(n)} in Greater Nicosia. Live bikes are known now only`
      if (live.bikesStatus === "unavailable") return `${fmtCount(n)} in Greater Nicosia. Nextbike's live feed isn't answering`
      const here = live.bikesHere
      return `${fmtCount(n)} in Greater Nicosia` +
        (here ? `, ${fmtCount(here.bikes)} bikes at them now${here.ebikes ? `, ${fmtCount(here.ebikes)} electric` : ""}` : "")
    }
    case "scooter_zone":
      return `${fmtCount(n)} zones holding ${fmtCount(meta.capacity_in_nicosia ?? 0)} scooters`
    default:
      return `${fmtCount(n)} in Greater Nicosia`
  }
}

// One kind's line in the Layers sheet: what it is, or what it shows now and how old it is.
export function kindLine(kind: FacKind, st: KindState, live: ReturnType<typeof useFacilities.getState>["live"], past: boolean): string {
  if (st.on && st.status === "loading") return "Reading the national inventory"
  if (st.on && st.status === "error") return `Couldn't load it: ${st.error}. Switch it off and on to try again.`
  if (!st.on || !st.meta) return ABOUT[kind]
  const age = inventoryAge(st.meta)
  const stale = st.meta.error ? " An older copy: the national feed didn't answer." : ""
  return `${countLine(kind, st.meta, live, past)}. ${age ?? "From the national inventory"}.${stale}`
}

registerLayerGroup({ id: HIRE_GROUP, label: "Park, charge and hire", order: 3 })

FAC_KINDS.forEach((kind, i) => {
  registerSystemLayer({
    id: `facility_${kind}`, group: HIRE_GROUP, order: i * 10, label: FAC_LABELS[kind],
    useOn: () => useFacilities((s) => s.kinds[kind].on),
    setOn: (on) => setFacilityOn(kind, on),
    Summary: function Summary() {
      const st = useFacilities((s) => s.kinds[kind])
      const live = useFacilities((s) => s.live)
      const past = useClock((s) => s.at !== null)
      return <span aria-live="polite">{kindLine(kind, st, live, past)}</span>
    },
    Legend: function Legend() {
      const dim = kind === "bike_station" ? "No bike now" : kind === "charger" ? "Listed out of service" : null
      return (
        <ul className="flex flex-wrap items-center gap-x-3.5 gap-y-1.5 text-[12.5px] text-muted-foreground" aria-label={`${FAC_LABELS[kind]} legend`}>
          <li className="flex items-center gap-1.5"><Badge kind={kind} />{FAC_LABELS[kind]}</li>
          {dim && <li className="flex items-center gap-1.5"><Badge kind={kind} dim />{dim}</li>}
          {kind === "car_park" && <li>Beside a badge: free bays the city's sensors see</li>}
          {kind === "bike_station" && <li>Beside a badge: bikes there now</li>}
        </ul>
      )
    },
  })
})

// -- the Parking card's section ------------------------------------------------------------------

const SHOWN: { id: FacKind; label: string }[] = [
  { id: "car_park", label: "Car parks" }, { id: "charger", label: "Chargers" },
  { id: "bike_station", label: "Bikes" }, { id: "scooter_zone", label: "Scooters" },
]

function ParkChargeHire() {
  const labelId = useId()
  const kinds = useFacilities((s) => s.kinds)
  const live = useFacilities((s) => s.live)
  const past = useClock((s) => s.at !== null)
  const on = SHOWN.filter((s) => kinds[s.id].on)
  // Kinds whose inventory is read and on: their lines, most useful first.
  const lines = on.map((s) => ({ id: s.id, text: kindLine(s.id, kinds[s.id], live, past) }))
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <span id={labelId} className="text-[12.5px] text-muted-foreground">Park, charge and hire on the map</span>
        <ToggleGroup
          multiple
          aria-labelledby={labelId}
          variant="outline" size="sm" spacing={0} className="w-full"
          value={on.map((s) => s.id)}
          onValueChange={(next) => {
            for (const s of SHOWN) {
              const want = next.includes(s.id)
              if (want !== kinds[s.id].on) setFacilityOn(s.id, want)
            }
          }}
        >
          {SHOWN.map((s) => (
            <ToggleGroupItem key={s.id} value={s.id} className="flex-1 gap-1.5">
              <Badge kind={s.id} className="size-3.5 shrink-0" />
              {s.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      {lines.length > 0 ? (
        <ul className="flex flex-col gap-1.5 text-[12.5px] leading-snug text-muted-foreground" aria-live="polite">
          {lines.map((l) => <li key={l.id} className="flex gap-2"><Badge kind={l.id} className="mt-px size-3.5 shrink-0" /><span>{l.text}</span></li>)}
        </ul>
      ) : (
        <p className="text-[12.5px] leading-snug text-muted-foreground">
          The national inventories of car parks, chargers, Nextbike stations and Bolt scooter zones. Pick any to put them on the map.
        </p>
      )}
      <Button variant="outline" size="sm" className="self-start" onClick={() => openLayers(HIRE_GROUP)}>
        Park and ride and bike racks in Layers
      </Button>
    </div>
  )
}

registerTopicSection({ id: "park-charge-hire", topic: "parking", order: 10, Section: ParkChargeHire })

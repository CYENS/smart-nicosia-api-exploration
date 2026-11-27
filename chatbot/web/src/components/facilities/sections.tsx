/* The inspector's sections for where people park, charge and hire: the headline (a car park's
   free sensed bays, a station's bikes now, a charger's power), and a note on how old the
   inventory is. A station's bikes are read again while it is open. */
import { useEffect } from "react"

import { Badge } from "@/components/facilities/rows"
import type { Entity } from "@/lib/entities"
import type { FacKind } from "@/lib/facilities/store"
import { useInspector } from "@/lib/inspector"
import { nicosiaTime } from "@/lib/map/engine"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

type Bays = { bays: number; free: number; taken: number; no_reading: number; known: boolean; origin: string; radius_m: number; error?: string | null }
type Bikes = { bikes: number; ebikes: number; docks: number | null; renting: boolean; virtual: boolean; capacity: number | null }
type Charger = { kw: number | null; max_kw: number | null; speed: string; power_type: string | null; connector_types: string[]; connectors: number | null; status: string | null }
type Inventory = { updated_label?: string | null; published_label?: string | null; read_at?: string | null; error?: string | null }
type FacilityEntity = Entity & { bays?: Bays | null; bikes?: Bikes | null; charger?: Charger; inventory?: Inventory; facility_kind?: FacKind }

const FAC_KINDS = ["car_park", "park_and_ride", "charger", "bike_station", "scooter_zone", "bike_rack"]
const CONNECTORS: Record<string, string> = { type2: "Type 2", comboType2: "CCS", comboType1: "CCS 1", chademo: "CHAdeMO", type1: "Type 1" }

function Big({ value, unit, live }: { value: string | number; unit?: string; live?: boolean }) {
  return (
    <p className="flex items-baseline gap-1.5 font-readout leading-none">
      <span className={cn("text-[40px] font-semibold tracking-[-0.01em] tabular-nums", live ? "text-sodium" : "text-foreground")}>{value}</span>
      {unit && <span className="text-[17px] text-muted-foreground">{unit}</span>}
    </p>
  )
}

// A share as a bar: what is free against what is there, read as a meter by screen readers.
function Share({ part, whole, label, color }: { part: number; whole: number; label: string; color: string }) {
  return (
    <div role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={whole} aria-valuenow={part}
      aria-valuetext={`${part} of ${whole}`} className="relative h-2.5 w-full rounded-full bg-raised">
      <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${whole ? (100 * part) / whole : 0}%`, background: color }} />
    </div>
  )
}

// While a Nextbike station is open its bikes are read again every minute.
function useRefresh(entity: Entity, ms: number, on: boolean) {
  useEffect(() => {
    if (!on) return
    const t = window.setInterval(async () => {
      if (document.hidden) return
      try {
        const res = await fetch(`/api/entities/${encodeURIComponent(entity.id)}?series=false`)
        if (!res.ok) return
        const fresh = (await res.json()) as Entity
        const st = useInspector.getState()
        if (st.open && st.entity?.id === entity.id && !st.entity.moment) useInspector.setState({ entity: fresh })
      } catch { /* keep the last one */ }
    }, ms)
    return () => window.clearInterval(t)
  }, [entity.id, ms, on])
}

function FacilityHeadline({ entity }: SectionProps) {
  const e = entity as FacilityEntity
  useRefresh(entity, 60_000, e.kind === "bike_station" && !e.moment)
  if (e.kind === "car_park" && e.bays) {
    const b = e.bays
    const sensed = b.free + b.taken
    if (!b.known) {
      return <p className="text-[13.5px] leading-snug text-muted-foreground">{b.bays} of the city's bay sensors are beside it, but none has reported lately, so it isn't known which are free.</p>
    }
    return (
      <div className="flex flex-col gap-2" aria-live="polite">
        <Big value={b.free} unit={`of ${sensed} sensed bays free`} live={!e.moment} />
        <Share part={b.free} whole={sensed} label="Sensed bays free" color="#16a34a" />
        <p className="text-[12.5px] leading-snug text-muted-foreground">
          The city's sensors within {b.radius_m} m of it{e.moment ? ", as the twin's memory had them then" : ""}. The car park's own spaces aren't counted: the national inventory gives none.
        </p>
      </div>
    )
  }
  if (e.kind === "bike_station" && e.bikes) {
    const s = e.bikes
    return (
      <div className="flex flex-col gap-2" aria-live="polite">
        <Big value={s.bikes} unit={s.bikes === 1 ? "bike now" : "bikes now"} live={s.bikes > 0} />
        <p className="text-[13.5px] leading-snug">
          {s.ebikes ? `${s.ebikes} electric` : "None electric"}
          {s.virtual ? ", a marked spot with no docks" : s.docks !== null ? `, ${s.docks} free dock${s.docks === 1 ? "" : "s"} to return one` : ""}
          {!s.renting ? ". Not renting now" : ""}
        </p>
        {e.state?.as_of && <p className="text-[12px] text-muted-foreground">Live from Nextbike, reported {nicosiaTime(new Date(e.state.as_of))}</p>}
      </div>
    )
  }
  if (e.kind === "charger" && e.charger?.kw) {
    const c = e.charger
    return (
      <div className="flex flex-col gap-2">
        <Big value={c.kw ?? "?"} unit={`kW${c.speed === "fast" ? ", fast" : ""}`} />
        <ul className="flex flex-wrap gap-1.5" aria-label="Connectors">
          {c.connector_types.map((t) => (
            <li key={t} className="rounded-md border border-border px-1.5 py-0.5 font-condensed text-[12.5px]">{CONNECTORS[t] ?? t}</li>
          ))}
          {c.power_type && <li className="rounded-md border border-border px-1.5 py-0.5 font-condensed text-[12.5px]">{c.power_type.toUpperCase()}</li>}
        </ul>
        <p className="text-[12.5px] leading-snug text-muted-foreground">
          {c.status === "unavailable" ? "The inventory lists it as out of service. " : c.status === "operational" ? "The inventory lists it as in service. " : ""}
          That is last night's listing, not whether a connector is free now.
        </p>
      </div>
    )
  }
  return null
}

function InventoryNote({ entity }: SectionProps) {
  const e = entity as FacilityEntity
  const inv = e.inventory
  if (!inv) return null
  const kind = (e.facility_kind ?? e.kind) as FacKind
  const when = inv.updated_label ? `last changed ${inv.updated_label}` : inv.published_label ? `republished ${inv.published_label}` : null
  return (
    <section aria-label="Where it comes from" className="flex gap-2.5 rounded-lg bg-raised/60 px-3 py-2.5">
      <Badge kind={kind} className="mt-0.5 size-[18px] shrink-0" />
      <p className="text-[12.5px] leading-snug text-muted-foreground">
        From the national inventory{when ? `, ${when}` : ""}. A place, not a reading: on the city clock it stands as it is now.
        {inv.error ? ` ${inv.error[0].toUpperCase()}${inv.error.slice(1)}.` : ""}
      </p>
    </section>
  )
}

registerInspectorSection({ id: "facility-headline", order: 12, kinds: FAC_KINDS, Component: FacilityHeadline })
registerInspectorSection({ id: "facility-inventory", order: 85, kinds: FAC_KINDS, Component: InventoryNote })

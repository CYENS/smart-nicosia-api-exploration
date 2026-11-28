/* Layers sheet rows for Nicosia Urban Fabric:
   - Summer ground heat (Landsat ~10:30 local morning surface temperature, not air temperature)
   - People (Kontur modelled 2022 population, H3 res-8)
   - 15-minute access (Standard walk vs Step-free / gentle slope)
   - Street slopes (CyNAP 100 m road gradients, all points vs steep >5%) */
import { useEffect, useId } from "react"

import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { ACCESS_LEGEND, HEAT_BANDS, HEAT_COLORS, PEOPLE_LEGEND, SLOPE_LEGEND } from "@/lib/fabric/colors"
import {
  setAccessMode, setAccessOn, setHeatOn, setPeopleOn, setSlopeFilter, setSlopesOn,
} from "@/lib/fabric/map"
import { loadFabricSummary, useFabric, type AccessMode, type SlopeFilter } from "@/lib/fabric/store"
import { useMapUi } from "@/lib/map/engine"
import { registerLayerGroup, registerSystemLayer } from "@/lib/registries/layers"
import { cn } from "@/lib/utils"

export const FABRIC_GROUP = "fabric"

registerLayerGroup({ id: FABRIC_GROUP, label: "Urban fabric", order: 7 })

function useEnsureSummary() {
  useEffect(() => { void loadFabricSummary() }, [])
  return useFabric((s) => s.summary)
}

function Steps({ colors, labels, label, className }: { colors: readonly string[]; labels: readonly string[]; label: string; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-1", className)} role="img" aria-label={`${label}: ${labels.join(", ")}`}>
      <div className="flex h-2.5 gap-[2px] overflow-hidden rounded-sm">
        {colors.map((c, i) => <span key={i} className="flex-1" style={{ background: c }} />)}
      </div>
      <div className="flex gap-[2px] font-readout text-[11px] leading-tight text-muted-foreground" aria-hidden="true">
        {labels.map((l, i) => <span key={i} className="min-w-0 flex-1 truncate">{l}</span>)}
      </div>
    </div>
  )
}

function RetryRow({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 text-[12px] text-destructive" role="status" aria-live="polite">
      <span className="min-w-0 flex-1">{error}</span>
      <Button variant="outline" size="xs" onClick={onRetry}>Try again</Button>
    </div>
  )
}

// -- 1. Summer ground heat (Landsat 8/9 morning surface temperature) -----------------------------

function HeatSummary() {
  const sum = useEnsureSummary()
  const heat = useFabric((s) => s.heat)
  if (heat.status === "loading") return <span aria-live="polite">Loading Landsat morning surface grid...</span>
  if (heat.status === "error") return <span aria-live="polite">Could not load ground heat: {heat.error}</span>
  const h = heat.payload ?? sum.data?.heat
  if (sum.status === "loading" && !h) return <Skeleton className="h-3.5 w-44" />
  if (h && h.min_c !== null && h.max_c !== null) {
    return (
      <span aria-live="polite">
        {h.min_c} to {h.max_c} °C morning ground (~10:30 local, {h.scenes_used} summer scenes)
      </span>
    )
  }
  return <>Morning ground surface temperature from Landsat (~10:30 local)</>
}

function HeatLegend() {
  const base = useMapUi((s) => s.basemap)
  const heat = useFabric((s) => s.heat)
  return (
    <div className="flex flex-col gap-2">
      {heat.status === "error" && heat.error && <RetryRow error={heat.error} onRetry={() => setHeatOn(true)} />}
      <Steps
        colors={HEAT_COLORS[base]}
        labels={HEAT_BANDS.map((b) => b.short)}
        label="Summer morning ground surface temperature bands"
      />
      <p className="text-[12px] leading-snug text-muted-foreground">
        Landsat 8/9 morning ground skin temperature (~10:30 local), not shaded air temperature.
      </p>
    </div>
  )
}

registerSystemLayer({
  id: "fabric_heat",
  group: FABRIC_GROUP,
  order: 0,
  label: "Summer ground heat",
  useOn: () => useFabric((s) => s.heat.on),
  setOn: setHeatOn,
  Summary: HeatSummary,
  Legend: HeatLegend,
})

// -- 2. People (Kontur modelled 2022 population hexagons) ----------------------------------------

function PeopleSummary() {
  const sum = useEnsureSummary()
  const people = useFabric((s) => s.people)
  if (people.status === "loading") return <span aria-live="polite">Loading population hexagons...</span>
  if (people.status === "error") return <span aria-live="polite">Could not load population: {people.error}</span>
  if (sum.status === "loading" && !sum.data) return <Skeleton className="h-3.5 w-44" />
  if (sum.data?.ready) {
    return (
      <span aria-live="polite">
        {sum.data.population_total_2022.toLocaleString("en-GB")} modelled 2022 residents across {sum.data.hex_count} hexagons
      </span>
    )
  }
  return <>Modelled 2022 population across Greater Nicosia (Kontur H3)</>
}

function PeopleLegend() {
  const people = useFabric((s) => s.people)
  return (
    <div className="flex flex-col gap-2">
      {people.status === "error" && people.error && <RetryRow error={people.error} onRetry={() => setPeopleOn(true)} />}
      <Steps
        colors={PEOPLE_LEGEND.map((b) => b.color)}
        labels={PEOPLE_LEGEND.map((b) => b.label)}
        label="Modelled 2022 population per hexagon"
      />
      <p className="text-[12px] leading-snug text-muted-foreground">
        Kontur modelled 2022 population in ~0.7 km² hexagons across both sides of the buffer zone. Click any hexagon to inspect.
      </p>
    </div>
  )
}

registerSystemLayer({
  id: "fabric_people",
  group: FABRIC_GROUP,
  order: 10,
  label: "People",
  useOn: () => useFabric((s) => s.people.on),
  setOn: setPeopleOn,
  Summary: PeopleSummary,
  Legend: PeopleLegend,
})

// -- 3. 15-minute access (standard walk vs step-free / gentle slope) -----------------------------

function AccessSummary() {
  const sum = useEnsureSummary()
  const acc = useFabric((s) => s.access)
  if (acc.status === "loading") return <span aria-live="polite">Loading 15-minute access scores...</span>
  if (acc.status === "error") return <span aria-live="polite">Could not load access scores: {acc.error}</span>
  if (sum.status === "loading" && !sum.data) return <Skeleton className="h-3.5 w-44" />
  const a = sum.data?.access
  if (a && a.mean_15min !== null && a.mean_step_free !== null) {
    return (
      <span aria-live="polite">
        Mean {acc.mode === "step_free" ? `${a.mean_step_free}/100 step-free` : `${a.mean_15min}/100 walk`} (step-free {a.mean_step_free}/100)
      </span>
    )
  }
  return <>Everyday services within a 15-minute walk or step-free reach</>
}

function AccessLegend() {
  const id = useId()
  const acc = useFabric((s) => s.access)
  return (
    <div className="flex flex-col gap-2.5">
      {acc.status === "error" && acc.error && <RetryRow error={acc.error} onRetry={() => setAccessOn(true)} />}
      <Field>
        <FieldLabel id={id} className="text-[12px] font-normal text-muted-foreground">Walking profile</FieldLabel>
        <ToggleGroup
          aria-labelledby={id}
          variant="outline"
          size="sm"
          spacing={0}
          className="w-full"
          value={[acc.mode]}
          onValueChange={(v) => { if (v[0]) setAccessMode(v[0] as AccessMode) }}
        >
          <ToggleGroupItem value="standard" className="flex-1" aria-label="Standard 15-minute walk at 4.8 km/h">
            Standard walk
          </ToggleGroupItem>
          <ToggleGroupItem value="step_free" className="flex-1" aria-label="Step-free gentle slope profile at 3.6 km/h">
            Step-free
          </ToggleGroupItem>
        </ToggleGroup>
      </Field>
      <Steps
        colors={ACCESS_LEGEND.map((b) => b.color)}
        labels={ACCESS_LEGEND.map((b) => b.label)}
        label="15-minute access score bands out of 100"
      />
      <p className="text-[12px] leading-snug text-muted-foreground">
        Bus stops, groceries, pharmacies, parks, schools and civic points. Step-free penalises CyNAP grades over 5% and 8%.
      </p>
    </div>
  )
}

registerSystemLayer({
  id: "fabric_access",
  group: FABRIC_GROUP,
  order: 20,
  label: "15-minute access",
  useOn: () => useFabric((s) => s.access.on),
  setOn: setAccessOn,
  Summary: AccessSummary,
  Legend: AccessLegend,
})

// -- 4. Street slopes (CyNAP 100 m road gradients) -----------------------------------------------

function SlopesSummary() {
  const sum = useEnsureSummary()
  const slopes = useFabric((s) => s.slopes)
  if (slopes.status === "loading") return <span aria-live="polite">Loading CyNAP road slopes...</span>
  if (slopes.status === "error") return <span aria-live="polite">Could not load street slopes: {slopes.error}</span>
  const st = slopes.data?.meta?.summary ?? sum.data?.slopes
  if (st && st.total > 0) {
    const gt5 = st.gt5 ?? ((st.moderate_gt5 ?? 0) + (st.steep_gt8 ?? 0))
    return (
      <span aria-live="polite">
        {st.total.toLocaleString("en-GB")} road points ({gt5} over 5%, {st.steep_gt8} over 8%)
      </span>
    )
  }
  return <>CyNAP 100 m road gradients for step-free access and cycling</>
}

function SlopesLegend() {
  const id = useId()
  const slopes = useFabric((s) => s.slopes)
  return (
    <div className="flex flex-col gap-2.5">
      {slopes.status === "error" && slopes.error && <RetryRow error={slopes.error} onRetry={() => setSlopesOn(true)} />}
      <Field>
        <FieldLabel id={id} className="text-[12px] font-normal text-muted-foreground">Show road points</FieldLabel>
        <ToggleGroup
          aria-labelledby={id}
          variant="outline"
          size="sm"
          spacing={0}
          className="w-full"
          value={[slopes.filter]}
          onValueChange={(v) => { if (v[0]) setSlopeFilter(v[0] as SlopeFilter) }}
        >
          <ToggleGroupItem value="all" className="flex-1" aria-label="Show all measured road gradient points">
            All grades
          </ToggleGroupItem>
          <ToggleGroupItem value="steep" className="flex-1" aria-label="Show only moderate and steep road points over 5 percent">
            Steep (&gt;5%)
          </ToggleGroupItem>
        </ToggleGroup>
      </Field>
      <ul className="flex flex-wrap gap-x-3.5 gap-y-1 text-[12px] text-muted-foreground">
        {SLOPE_LEGEND.map((item) => (
          <li key={item.key} className="flex items-center gap-1.5">
            <span className="size-2.5 shrink-0 rounded-full" style={{ background: item.color }} aria-hidden="true" />
            <span>{item.label}</span>
          </li>
        ))}
      </ul>
      <p className="text-[12px] leading-snug text-muted-foreground">
        CyNAP 100 m measurements cover classified roads mostly south of the buffer zone.
      </p>
    </div>
  )
}

registerSystemLayer({
  id: "fabric_slopes",
  group: FABRIC_GROUP,
  order: 30,
  label: "Street slopes",
  useOn: () => useFabric((s) => s.slopes.on),
  setOn: setSlopesOn,
  Summary: SlopesSummary,
  Legend: SlopesLegend,
})

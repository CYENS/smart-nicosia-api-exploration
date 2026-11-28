/* Inspector sections for Urban Fabric:
   - FabricHexSection (kinds: ["fabric_hex"]): headline heat exposure + 3-part transparent breakdown
     (surface heat, modelled 2022 population, green/shade context), 15-min walk vs step-free scores
     with category timings, and nearby CyNAP slopes / Overture civic amenities.
   - SpotFabricSection (kinds: ["spot"]): compact urban fabric summary for any clicked point,
     linking cleanly to the enclosing H3 hexagon. */
import { ArrowRightIcon, CheckIcon, FlameIcon, FootprintsIcon, MountainIcon, UsersIcon, XIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { Entity } from "@/lib/entities"
import { inspect } from "@/lib/inspector"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

type NearestService = {
  name: string
  distance_m: number
  walk_min: number
  step_free_min: number
  slope_barrier?: boolean
}

type HeatParts = {
  surface_heat_c?: number | null
  heat_norm?: number
  population_2022?: number
  pop_norm?: number
  green_pct?: number
  cool_factor?: number
  lack_of_cool_norm?: number
}

type FabricHexData = {
  heat_exposure_score: number | null
  heat_band_label?: string
  heat_parts?: HeatParts
  lst_median_c: number | null
  lst_min_c?: number | null
  lst_max_c?: number | null
  population_2022: number
  pop_density_km2?: number
  green_pct?: number
  access_15min_score: number | null
  access_step_free_score: number | null
  step_free_gap?: number | null
  access_color?: string
  step_free_color?: string
  nearest_services?: Record<string, NearestService>
  slopes?: { points?: number; gt5?: number; gt8?: number; max_pct?: number | null; coverage?: string }
  infra?: { crossings?: number; benches?: number; recycling?: number; waste_baskets?: number; bicycle_rental?: number }
}

type SpotFabricData = {
  known?: boolean
  hex_id?: string | null
  hex_name?: string | null
  population_2022?: number | null
  pop_density_km2?: number | null
  population_note?: string
  surface_heat_c?: number | null
  hex_lst_median_c?: number | null
  overpass_local?: string
  temperature_note?: string
  heat_exposure_score?: number | null
  heat_band_label?: string | null
  heat_parts?: HeatParts
  green_pct?: number | null
  access_15min_score?: number | null
  access_step_free_score?: number | null
  step_free_gap?: number | null
  slopes_nearby?: {
    radius_m?: number
    points?: number
    gt5?: number
    gt8?: number
    max_pct?: number | null
    steepest?: { road?: string; slope_pct?: number; distance_m?: number } | null
  }
}

type FabricEntity = Entity & { fabric?: FabricHexData & SpotFabricData }

const SERVICE_LABELS: Record<string, string> = {
  bus_stop: "Bus stop",
  grocery: "Grocery",
  pharmacy: "Pharmacy",
  park: "Park or green space",
  school: "School or university",
  civic: "Civic point",
}

function MeterRow({
  label,
  detail,
  pct,
  color,
}: {
  label: string
  detail: string
  pct: number
  color: string
}) {
  const clamped = Math.max(0, Math.min(100, Math.round(pct)))
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2 text-[12.5px]">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-readout font-medium tabular-nums text-foreground">{detail}</span>
      </div>
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-raised"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={clamped}
        aria-valuetext={detail}
      >
        <div className="h-full rounded-full" style={{ width: `${clamped}%`, background: color }} />
      </div>
    </div>
  )
}

function FabricHexBreakdown({ entity }: SectionProps) {
  const f = (entity as FabricEntity).fabric
  if (!f) return null
  const parts = f.heat_parts ?? {}
  const services = Object.entries(f.nearest_services ?? {})
  const slopes = f.slopes ?? {}
  const infra = f.infra ?? {}

  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-3 gap-2">
        <div className="flex flex-col gap-0.5 rounded-lg border border-line bg-raised/60 p-2.5">
          <span className="flex items-center gap-1 text-[11.5px] text-muted-foreground">
            <FlameIcon className="size-3.5 text-sodium" aria-hidden="true" />
            Ground (~10:30)
          </span>
          <span className="font-readout text-[20px] font-semibold tabular-nums leading-tight">
            {f.lst_median_c !== null && f.lst_median_c !== undefined ? `${f.lst_median_c}°` : "n/a"}
          </span>
          <span className="text-[11px] text-muted-foreground">surface, not air</span>
        </div>
        <div className="flex flex-col gap-0.5 rounded-lg border border-line bg-raised/60 p-2.5">
          <span className="flex items-center gap-1 text-[11.5px] text-muted-foreground">
            <UsersIcon className="size-3.5 text-sky-400" aria-hidden="true" />
            People (2022)
          </span>
          <span className="font-readout text-[20px] font-semibold tabular-nums leading-tight">
            {(f.population_2022 ?? 0).toLocaleString("en-GB")}
          </span>
          <span className="text-[11px] text-muted-foreground">Kontur model</span>
        </div>
        <div className="flex flex-col gap-0.5 rounded-lg border border-line bg-raised/60 p-2.5">
          <span className="flex items-center gap-1 text-[11.5px] text-muted-foreground">
            <FootprintsIcon className="size-3.5 text-emerald-400" aria-hidden="true" />
            15-min walk
          </span>
          <span className="font-readout text-[20px] font-semibold tabular-nums leading-tight">
            {f.access_15min_score ?? "n/a"}
            <span className="text-[12px] font-normal text-muted-foreground">/100</span>
          </span>
          <span className="text-[11px] text-muted-foreground">
            step-free {f.access_step_free_score ?? "n/a"}
          </span>
        </div>
      </div>

      <section aria-label="Heat exposure components" className="flex flex-col gap-2.5">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="text-[12.5px] text-muted-foreground">Heat exposure parts</h3>
          <span className="font-readout text-[14px] font-semibold tabular-nums">
            {f.heat_exposure_score ?? "n/a"} / 100
          </span>
        </div>
        <div className="flex flex-col gap-2.5 rounded-lg border border-line bg-surface/60 p-3">
          <MeterRow
            label="Morning ground heat (~10:30 local, 45%)"
            detail={parts.surface_heat_c !== null && parts.surface_heat_c !== undefined ? `${parts.surface_heat_c} °C` : "Unknown"}
            pct={(parts.heat_norm ?? 0) * 100}
            color="#f97316"
          />
          <MeterRow
            label="Modelled 2022 population (35%)"
            detail={`${(parts.population_2022 ?? f.population_2022 ?? 0).toLocaleString("en-GB")} people`}
            pct={(parts.pop_norm ?? 0) * 100}
            color="#38bdf8"
          />
          <MeterRow
            label="Low green and shade cover (20%)"
            detail={`${f.green_pct ?? parts.green_pct ?? 0}% mapped green`}
            pct={(parts.lack_of_cool_norm ?? 0) * 100}
            color="#eab308"
          />
        </div>
        <p className="text-[12px] leading-snug text-muted-foreground">
          Landsat measures morning ground surface skin temperature (~10:30 local), not shaded air temperature. Population is Kontur modelled 2022.
        </p>
      </section>

      {services.length > 0 && (
        <section aria-label="15-minute everyday access" className="flex flex-col gap-2.5">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="text-[12.5px] text-muted-foreground">15-minute access (walk vs step-free)</h3>
            {f.step_free_gap !== null && f.step_free_gap !== undefined && f.step_free_gap >= 5 && (
              <Badge variant="outline" className="font-readout text-[11px]">
                {f.step_free_gap} pt slope gap
              </Badge>
            )}
          </div>
          <ul className="flex flex-col gap-1.5">
            {services.map(([cat, info]) => {
              const okWalk = info.walk_min <= 15
              const okSf = info.step_free_min <= 15 && !info.slope_barrier
              return (
                <li
                  key={cat}
                  className="flex items-center justify-between gap-2 rounded-md border border-line/70 bg-raised/40 px-2.5 py-1.5 text-[12.5px]"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-foreground">{SERVICE_LABELS[cat] ?? cat}</p>
                    <p className="truncate text-[11.5px] text-muted-foreground">{info.name} ({info.distance_m} m)</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2 font-readout text-[12px] tabular-nums">
                    <span className={cn("inline-flex items-center gap-0.5", okWalk ? "text-emerald-400" : "text-muted-foreground")}>
                      {okWalk ? <CheckIcon className="size-3" aria-hidden="true" /> : <XIcon className="size-3" aria-hidden="true" />}
                      {info.walk_min}m
                    </span>
                    <span
                      className={cn("inline-flex items-center gap-0.5", okSf ? "text-sky-400" : "text-amber-400")}
                      title={info.slope_barrier ? "Steep (>8%) CyNAP grade along route" : "Step-free gentle slope estimate"}
                    >
                      {info.slope_barrier ? "steep" : `${info.step_free_min}m sf`}
                    </span>
                  </div>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      <section aria-label="Slopes and civic infrastructure" className="flex flex-col gap-2">
        <h3 className="text-[12.5px] text-muted-foreground">Street slopes and civic points</h3>
        <div className="flex flex-wrap gap-1.5 text-[12px]">
          <Badge variant="secondary">
            <MountainIcon className="size-3" data-icon="inline-start" aria-hidden="true" />
            {slopes.points
              ? `${slopes.points} grades (${slopes.gt5 ?? 0} >5%, ${slopes.gt8 ?? 0} >8%)`
              : "No CyNAP road grades inside"}
          </Badge>
          <Badge variant="outline">{infra.crossings ?? 0} crossings</Badge>
          <Badge variant="outline">{infra.benches ?? 0} benches</Badge>
          <Badge variant="outline">{infra.recycling ?? 0} recycling</Badge>
        </div>
      </section>
    </div>
  )
}

function SpotFabricSection({ entity }: SectionProps) {
  const f = (entity as FabricEntity).fabric
  if (!f || !f.known) return null
  const sn = f.slopes_nearby

  return (
    <section aria-label="Urban fabric and 15-minute access" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[12.5px] text-muted-foreground">Urban fabric and 15-minute access</h3>
        {f.hex_id && (
          <Button
            variant="ghost"
            size="xs"
            onClick={() => inspect({ id: f.hex_id! }, { fly: false, focus: true })}
          >
            Open hexagon
            <ArrowRightIcon className="size-3" data-icon="inline-end" aria-hidden="true" />
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="flex flex-col gap-0.5 rounded-lg border border-line bg-raised/50 p-2.5">
          <span className="text-[11.5px] text-muted-foreground">Morning ground heat</span>
          <span className="font-readout text-[18px] font-semibold tabular-nums">
            {f.surface_heat_c !== null && f.surface_heat_c !== undefined ? `${f.surface_heat_c} °C` : "Unknown"}
          </span>
          <span className="text-[11px] text-muted-foreground">Landsat ~10:30 surface, not air</span>
        </div>
        <div className="flex flex-col gap-0.5 rounded-lg border border-line bg-raised/50 p-2.5">
          <span className="text-[11.5px] text-muted-foreground">15-minute reach</span>
          <span className="font-readout text-[18px] font-semibold tabular-nums">
            {f.access_15min_score ?? "n/a"}
            <span className="text-[12px] font-normal text-muted-foreground"> / {f.access_step_free_score ?? "n/a"} step-free</span>
          </span>
          <span className="text-[11px] text-muted-foreground">
            {f.population_2022 !== null && f.population_2022 !== undefined
              ? `${f.population_2022.toLocaleString("en-GB")} people (2022 model)`
              : "Modelled 2022 population"}
          </span>
        </div>
      </div>

      {sn && (sn.points ?? 0) > 0 && (
        <p className="text-[12.5px] leading-snug text-muted-foreground">
          Within {sn.radius_m ?? 400} m: {sn.points} measured CyNAP road points ({sn.gt5 ?? 0} over 5%, {sn.gt8 ?? 0} over 8%
          {sn.steepest?.road ? `, steepest ${sn.steepest.slope_pct}% on ${sn.steepest.road}` : ""}).
        </p>
      )}
    </section>
  )
}

registerInspectorSection({
  id: "fabric-hex-breakdown",
  order: 12,
  kinds: ["fabric_hex"],
  when: (e) => !!(e as FabricEntity).fabric,
  Component: FabricHexBreakdown,
})

registerInspectorSection({
  id: "spot-fabric",
  order: 14,
  kinds: ["spot"],
  when: (e) => !!(e as FabricEntity).fabric?.known,
  Component: SpotFabricSection,
})

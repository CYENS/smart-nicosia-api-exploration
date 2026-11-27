/* Inspector sections for `aircraft`, `its_cctv`, and `its_detector`. */
import { Marker, MarkerContent } from "@/components/ui/marker"
import type { Entity } from "@/lib/entities"
import { registerInspectorSection, type SectionProps } from "@/lib/registries/inspector-sections"
import { cn } from "@/lib/utils"

type SkyAircraftInfo = {
  hex: string
  callsign: string
  type?: string | null
  registration?: string | null
  altitude_ft?: number | null
  speed_kmh?: number | null
  track_deg?: number | null
  on_ground?: boolean
  coverage_note?: string
}

type ItsSiteInfo = {
  site_id: string
  kind: "cctv" | "detector"
  subtype?: string | null
  direction?: string | null
  live_values?: boolean
}

type SkyEntity = Entity & {
  aircraft?: SkyAircraftInfo
  its?: ItsSiteInfo
}

function AircraftSection({ entity }: SectionProps) {
  const e = entity as SkyEntity
  const ac = e.aircraft
  if (!ac) return null
  const m = e.moment?.applies ? e.moment : null
  const live = e.state?.value !== "on_ground" && e.state?.value !== "out_of_range" && !m
  return (
    <div className="flex flex-col gap-3">
      {!ac.on_ground && ac.altitude_ft != null && (
        <div className="flex flex-col gap-1">
          <p className="flex items-baseline gap-1.5 font-readout leading-none">
            <span className={cn("text-[40px] font-semibold tracking-[-0.01em]", live ? "text-sodium" : "text-foreground")}>
              {ac.altitude_ft.toLocaleString("en-GB")}
            </span>
            <span className="text-[17px] text-muted-foreground">ft</span>
          </p>
          <p className="text-[13px] text-muted-foreground">
            Barometric altitude{m ? ` at ${m.label}` : ""}
          </p>
        </div>
      )}
      <Marker className="items-start rounded-lg border border-border/70 bg-raised/70 px-3 py-2.5 text-[12.5px] leading-snug text-muted-foreground">
        <MarkerContent>
          {ac.coverage_note ?? "Community ADS-B receiver coverage around Cyprus is partial, especially at low altitude and on quiet evenings."}
        </MarkerContent>
      </Marker>
    </div>
  )
}

function ItsSiteSection({ entity }: SectionProps) {
  const e = entity as SkyEntity
  const site = e.its
  if (!site) return null
  const isCctv = site.kind === "cctv"
  return (
    <Marker className="items-start rounded-lg border border-border/70 bg-raised/70 px-3 py-2.5 text-[12.5px] leading-snug text-muted-foreground">
      <MarkerContent>
        {isCctv
          ? "Static CyNAP site inventory for Public Works Department highway and arterial CCTV. No live camera stream is published openly."
          : site.live_values
            ? "CyNAP PWD loop detector with active Swarco MeasuredDataPublication flow values."
            : "Static CyNAP PWD loop detector location. Swarco MeasuredDataPublication currently lists site references without measured vehicle counts."}
      </MarkerContent>
    </Marker>
  )
}

registerInspectorSection({ id: "sky-aircraft", order: 10, kinds: ["aircraft"], Component: AircraftSection })
registerInspectorSection({ id: "sky-its", order: 30, kinds: ["its_cctv", "its_detector"], Component: ItsSiteSection })
